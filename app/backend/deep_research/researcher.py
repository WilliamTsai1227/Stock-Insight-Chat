"""
深度研究：Agents SDK 研究流程。

一個 Agent，兩個 hosted tool：
- `WebSearchTool`  —— 由 OpenAI 端執行網路搜尋，回覆會自帶 url_citation 標註
- `FileSearchTool` —— 檢索使用者這次上傳的臨時 vector store（沒上傳文件就不掛）

以 `Runner.run_streamed()` 執行，把 SDK 事件轉成前端能直接渲染的 SSE 事件：
工具開始／結束、模型思考中、正文 token、最後的完成事件。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from agents import Agent, FileSearchTool, ModelSettings, Runner, WebSearchTool

from .config import RESEARCH_MAX_TURNS, supports_reasoning_effort
from .sources import PreparedSources
from .usage import AgentUsage

# 工具的中文顯示名稱（前端直接顯示這串）
TOOL_LABELS = {
    "web_search_call": "網路搜尋",
    "web_search": "網路搜尋",
    "file_search_call": "文件檢索",
    "file_search": "文件檢索",
}

# hosted tool 的即時進度只在 raw response 事件裡；SDK 的 run item 事件要等整個
# model turn 跑完才吐 ToolCallItem（而且 hosted tool 根本不會有 ToolCallOutputItem），
# 拿它做進度會變成「工具跑完才顯示開始、而且永遠不會結束」。
_HOSTED_TOOL_EVENTS = {
    "response.web_search_call.in_progress": ("網路搜尋", "start"),
    "response.web_search_call.completed": ("網路搜尋", "done"),
    "response.file_search_call.in_progress": ("文件檢索", "start"),
    "response.file_search_call.completed": ("文件檢索", "done"),
}

RESEARCH_INSTRUCTIONS = """你是一位資深研究分析師。使用者會給你一個研究題目，可能附上文件、試算表或圖片。

工作方式（以下四步都在心裡做，不要寫進輸出）：
1. 先拆解題目成子問題，決定查證順序。
2. 有附件時，**先用 file_search 讀使用者自己的資料**，那是最貼近他情境的事實來源。
3. 用 web_search 補齊外部事實：市場數據、時間軸、對照案例、最新進展。
   同一個關鍵事實至少從兩個獨立來源交叉確認；查到的數字要標明時間點。
4. 遇到彼此矛盾的說法，不要挑一個講，要把分歧與各自依據都寫出來。

輸出一份 Markdown 研究簡報，結構如下：

## 摘要
三到五個要點，每點一句話講結論，不要鋪陳。

## 關鍵發現
分小節展開。每個論斷後面用 [來源標題](網址) 標註依據；
數據要寫出單位、時間點與出處。

## 交叉比對與分歧
不同來源說法不一致之處，以及你判斷哪一邊較可信、理由是什麼。

## 尚待釐清
現有資料回答不了、但對結論有實質影響的問題。

## 參考來源
條列所有引用過的網址與文件名稱。

寫作要求：
- 用繁體中文（台灣用語）。
- **輸出的第一個字元就是 `## 摘要`**。不要開場白、不要自我介紹、
  不要說明你打算怎麼做，也不要把上面的拆解步驟寫出來。
- 只寫查證得到的內容；推測必須標記為「推論」並說明依據。
- 不要寫「根據我的搜尋」這類過程描述，直接給結論與證據。
- 不確定的數字寧可不寫，也不要編造。
- 使用者有附件時，凡是來自附件的事實都要標明是哪一個檔案，
  與外部查證到的內容清楚區隔。
"""


@dataclass
class ResearchOutcome:
    markdown: str = ""
    citations: List[Dict[str, str]] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    elapsed_ms: int = 0


def _build_agent(model: str, prepared: PreparedSources) -> Agent:
    tools: List[Any] = [WebSearchTool(search_context_size="high")]
    if prepared.has_file_search:
        tools.append(
            FileSearchTool(
                vector_store_ids=[prepared.vector_store_id],
                max_num_results=8,
                include_search_results=True,
            )
        )

    return Agent(
        name="Deep Research Analyst",
        instructions=RESEARCH_INSTRUCTIONS,
        model=model,
        tools=tools,
        model_settings=(
            ModelSettings(reasoning={"effort": "medium"})
            if supports_reasoning_effort(model)
            else ModelSettings()
        ),
    )


def _build_input(query: str, prepared: PreparedSources) -> List[Dict[str, Any]]:
    """組出 Responses API 格式的 user message：文字 + 試算表附文 + 圖片。"""
    parts: List[Dict[str, Any]] = [{"type": "input_text", "text": query}]

    if prepared.has_file_search:
        names = "、".join(
            n for n in prepared.names
            if n not in {x for x, _ in prepared.spreadsheets}
            and n not in {x for x, _ in prepared.images}
        )
        parts.append(
            {
                "type": "input_text",
                "text": f"使用者已上傳下列文件，請用 file_search 檢索：{names}",
            }
        )

    sheet_block = prepared.spreadsheet_prompt_block()
    if sheet_block:
        parts.append({"type": "input_text", "text": sheet_block})

    for name, data_url in prepared.images:
        parts.append({"type": "input_text", "text": f"附件圖片：{name}"})
        parts.append({"type": "input_image", "image_url": data_url, "detail": "auto"})

    return [{"role": "user", "content": parts}]


def _tool_name_of(raw_item: Any) -> str:
    raw_type = getattr(raw_item, "type", "") or ""
    return TOOL_LABELS.get(raw_type, raw_type or "工具")


def _collect_citations(items: List[Any]) -> List[Dict[str, str]]:
    """從模型輸出的 url_citation 標註蒐集來源，依網址去重並保留出現順序。"""
    citations: List[Dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if getattr(item, "type", None) != "message_output_item":
            continue
        for block in getattr(item.raw_item, "content", None) or []:
            for ann in getattr(block, "annotations", None) or []:
                url = getattr(ann, "url", None)
                if not url or url in seen:
                    continue
                seen.add(url)
                citations.append({"title": getattr(ann, "title", "") or url, "url": url})

    return citations


async def stream_research(
    *,
    query: str,
    model: str,
    prepared: PreparedSources,
    usage: Optional[AgentUsage] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """
    執行研究並逐步 yield 事件字典。

    yield 的每個 dict 都有 `event` 鍵，路由層再包成 SSE。
    最後一個事件必為 `complete`（成功）或由呼叫端捕捉例外轉成 error。

    傳入 `usage` 時會把 token 用量寫回那個物件（呼叫端據此扣配額）。
    更新放在 finally：研究中途失敗或使用者關掉頁面時，已經打出去的那幾輪
    OpenAI 照樣計費，不記帳等於讓這些成本從配額裡消失。
    """
    started = time.monotonic()
    agent = _build_agent(model, prepared)
    agent_input = _build_input(query, prepared)

    outcome = ResearchOutcome()
    open_tools: Dict[str, str] = {}      # item_id → 中文標籤
    text_started = False

    result = Runner.run_streamed(agent, input=agent_input, max_turns=RESEARCH_MAX_TURNS)

    try:
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                raw_type = getattr(event.data, "type", "")

                if raw_type == "response.output_text.delta":
                    delta = getattr(event.data, "delta", None)
                    if not delta:
                        continue
                    if not text_started:
                        text_started = True
                        yield {"event": "writing", "text": "整理研究結果中…"}
                    yield {"event": "delta", "text": delta}
                    continue

                hosted = _HOSTED_TOOL_EVENTS.get(raw_type)
                if hosted is None:
                    continue

                label, phase = hosted
                item_id = getattr(event.data, "item_id", "") or raw_type
                if phase == "start":
                    open_tools[item_id] = label
                    if label not in outcome.tools_used:
                        outcome.tools_used.append(label)
                    yield {"event": "tool_start", "tool": label, "id": item_id}
                elif open_tools.pop(item_id, None):
                    yield {"event": "tool_done", "tool": label, "id": item_id}
                continue

            if event.type != "run_item_stream_event":
                continue

            # run item 只用來補記工具名稱（raw 事件沒吐時的保險），不再驅動進度
            item = event.item
            item_type = getattr(item, "type", "")
            if item_type == "tool_call_item":
                label = _tool_name_of(item.raw_item)
                if label not in outcome.tools_used:
                    outcome.tools_used.append(label)
            elif item_type == "reasoning_item":
                yield {"event": "thinking", "text": "推理中…"}

    finally:
        # 用量在串流途中就會累加，因此這裡即使是被 cancel 進來的也讀得到已花掉的量
        if usage is not None:
            usage.absorb(result)

    # 收尾：模型可能沒吐 completed 事件（例如中途切換 turn），別讓前端一直轉圈
    for item_id, label in list(open_tools.items()):
        yield {"event": "tool_done", "tool": label, "id": item_id}
    open_tools.clear()

    outcome.markdown = str(result.final_output or "").strip()
    outcome.citations = _collect_citations(list(result.new_items))
    outcome.elapsed_ms = int((time.monotonic() - started) * 1000)

    yield {
        "event": "complete",
        "markdown": outcome.markdown,
        "citations": outcome.citations,
        "tools_used": outcome.tools_used,
        "elapsed_ms": outcome.elapsed_ms,
    }
