"""
general_chat.py（一般對話管線）
─────────────────────────────────────────────
提供「一般對話」模式（chat_mode=general）的 SSE 串流輸出。

與股市 Agent 的差異
  - 不使用 LangGraph 圖。
  - 直接以對話歷史 + 系統提示打一次 LLM，逐 token 串流回傳。
  - 若 GENERAL_CHAT_ENABLE_WEB_SEARCH=1，API 層會在呼叫本函式前先呼叫
    Tavily，並將搜尋結果透過 web_context 參數注入系統提示，提供 LLM 即時資料。

對外介面
  general_chat_astream(messages_lc, web_context, extra_system) → AsyncIterator[chunk]
  GENERAL_CHAT_MODEL                               → StreamUsageChatOpenAI 實例（供 API 層計費）
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Dict, List, Optional

from langchain_core.messages import BaseMessage, SystemMessage

from app.backend.agent.stream_usage_chat_openai import StreamUsageChatOpenAI

# ── 模型設定 ──────────────────────────────────────────────────────────────────
_GENERAL_CHAT_MODEL_NAME = os.getenv("GENERAL_CHAT_MODEL", "gpt-4o-mini").strip()
_OPENAI_STREAM_OPTS: dict[str, Any] = {"stream_options": {"include_usage": True}}

# GENERAL_CHAT_ENABLE_WEB_SEARCH：是否在一般對話前呼叫 Tavily（預設開啟）
GENERAL_CHAT_ENABLE_WEB_SEARCH: bool = (
    os.getenv("GENERAL_CHAT_ENABLE_WEB_SEARCH", "1").strip() in ("1", "true", "yes")
)

GENERAL_CHAT_MODEL = StreamUsageChatOpenAI(
    model=_GENERAL_CHAT_MODEL_NAME,
    temperature=1,
    model_kwargs=_OPENAI_STREAM_OPTS,
)

# ── 系統提示 ──────────────────────────────────────────────────────────────────
# _GENERAL_SYSTEM_PROMPT = (
#     "你是一位友善、知識豐富的 AI 助理，能夠回答使用者各種問題。\n"
#     "請以繁體中文回答，語氣自然且清晰。\n"
#     "\n"
#     "【重要：先讀對話歷史，再回答】\n"
#     "回答前，請先完整閱讀對話歷史，從上下文推斷使用者的意圖。\n"
#     "若上下文已提供足夠資訊（例如城市、主題、偏好），可以根據資訊回答，不要再反問已知的事情。\n"
#     "只有在上下文完全無法判斷使用者意圖時，才可以提問——且一次只問一個最關鍵的問題，不要列出多項選項讓使用者回答。\n"
#     "\n"
#     "【格式規範：程式碼與結構化資料】\n"
#     "凡是回覆中包含以下內容，**必須**用 Markdown code fence（三個反引號 + 語言標籤）包起來：\n"
#     "- 任何程式語言的程式碼（Python、JavaScript、SQL、Bash、C# 等）\n"
#     "- JSON、YAML、TOML、XML 等結構化資料\n"
#     "- 命令列指令（shell、docker、git 等）\n"
#     "- 設定檔內容（nginx、docker-compose 等）\n"
#     "範例：```json\\n{...}\\n```  或  ```python\\nprint('hello')\\n```\n"
#     "絕對不可以將上述內容以純文字段落輸出，否則前端無法正確渲染。\n"
#     "\n"
#     "【搜尋結果使用規則】\n"
#     "若【即時網路資料】有內容，請先對照對話歷史確認與使用者問題的相關性，再決定如何使用：\n"
#     "- 相關的資料：直接從 content 逐筆提取，合併去重後**盡量詳細**列出，不要精簡省略。\n"
#     "  資料有多少就整理多少，清單要完整，每項附簡短說明（地址、特色等）與來源網址。\n"
#     "- 不相關的資料：忽略，改根據對話歷史或自身知識回答。\n"
#     "- 資料確實不足：直接說明缺什麼，請使用者補充提問，嚴禁列出 A/B/C 或 1/2/3 選項。\n"
#     "\n"
#     "【強制順序：先輸出搜尋內容，再追問】\n"
#     "即使你認為需要使用者提供更多資訊（如出發城市、日期、預算），也**絕對不可以**略過搜尋結果直接反問。\n"
#     "正確做法：先把所有相關搜尋結果整理輸出（詳細列出），最後才在回覆末尾追加一個問題。\n"
#     "錯誤做法：搜尋到機票/住宿資料，卻只說「請問你從哪個城市出發？」而不輸出搜尋到的內容。\n"
#     "\n"
#     "【重要：禁止捏造連結】\n"
#     "若系統提示中**沒有**【即時網路資料】區塊，絕對不可以在回覆中附上任何 URL 或「參考來源」段落。\n"
#     "只根據對話歷史與自身知識回答，不得憑空生成連結或假裝有查詢網路。\n"
#     "\n"
#     "若使用者詢問股市深度分析（個股財報、技術面、法人動向等），"
#     "請建議切換到「股市 Agent」模式以取得更精準的報告。"
# )

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
_GENERAL_SYSTEM_PROMPT = """\
你是一位友善、知識豐富的 AI 助理。請以繁體中文回答，語氣自然、清晰。

<Context_Rules>
1. 優先閱讀對話歷史，由上下文推斷使用者意圖與已知資訊（如：城市、主題、偏好）。
2. 已知資訊嚴禁重複反問。
3. 直接回答問題；只有當問題語意完全不清、且對話歷史也無法推斷意圖時，才可在回覆末尾追加【一個】最關鍵的問題。資訊「只是不夠精確」時仍應直接回答，不得以此為由追問。嚴禁列出選項讓使用者選擇。
</Context_Rules>

<Formatting_Rules>
凡包含以下內容，必須使用 Markdown code fence（三個反引號 + 語言標籤，如 ```json）包裹，嚴禁以純文字輸出：
- 程式碼（Python, JavaScript, SQL, Bash 等）
- 結構化資料（JSON, YAML, TOML, XML 等）
- 命令列指令（shell, docker, git 等）
- 設定檔（nginx, docker-compose 等）
</Formatting_Rules>

<Search_Rules>
當系統提供【即時網路資料】時，依據相關性嚴格執行：
- 相關資料：由 content 逐筆提取並合併去重。必須完整、詳細列出所有清單，每項需附簡短說明（如地址、特色）與來源網址。嚴禁精簡省略。
- 不相關資料：直接忽略，改依據自身知識回答。
- 資料不足：直接說明缺漏資訊，請使用者補充。
- 【強制順序】：必須先輸出完整的搜尋結果整理，才能在回覆末尾追加提問。嚴禁因資訊不全而隱藏或不輸出已搜尋到的資料。
- 【防捏造限制】：若無【即時網路資料】區塊，回覆中嚴禁出現任何 URL 或「參考來源」段落，不可憑空造假。
</Search_Rules>

<Special_Modes>
- 股市深度分析（個股財報、技術面、法人動向等）：直接建議使用者切換至「股市 Agent」模式以取得精準報告。
</Special_Modes>
"""

_WEB_CONTEXT_HEADER = "【即時網路資料】（由 Tavily 網路搜尋取得，請以此為事實依據）\n\n"


def _format_web_context(web_results: List[Dict[str, Any]]) -> str:
    """將 Tavily 結果格式化為注入系統提示的文字區塊。"""
    if not web_results:
        return ""
    lines: List[str] = []
    for i, r in enumerate(web_results, 1):
        date_str = f" ({r['published_date'][:10]})" if r.get("published_date") else ""
        content = (r.get("content") or "")[:1500]
        lines.append(
            f"[{i}] {r.get('title', '（無標題）')}{date_str}\n"
            f"來源：{r.get('url', '')}\n"
            f"{content}"
        )
    return _WEB_CONTEXT_HEADER + "\n\n---\n\n".join(lines)


async def general_chat_astream(
    messages_lc: List[BaseMessage],
    *,
    web_context: Optional[List[Dict[str, Any]]] = None,
    extra_system: str | None = None,
) -> AsyncIterator[Any]:
    """
    一般對話串流。

    Parameters
    ----------
    messages_lc:
        含對話歷史的 LangChain messages（不含 system）。
    web_context:
        Tavily 搜尋結果列表（由 API 層傳入）；None 或空列表表示無網路資料。
    extra_system:
        可選：額外附加到系統提示後面的指令。
    """
    system_content = _GENERAL_SYSTEM_PROMPT
    if web_context:
        system_content = system_content + "\n\n" + _format_web_context(web_context)
    if extra_system:
        system_content = system_content + "\n\n" + extra_system.strip()

    full_messages: List[BaseMessage] = [
        SystemMessage(content=system_content),
        *messages_lc,
    ]

    async for chunk in GENERAL_CHAT_MODEL.astream(full_messages):
        yield chunk
