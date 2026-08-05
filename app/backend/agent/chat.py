"""
chat.py（股市 Agent 核心）
─────────────────────────────────────────────
思考模式（thinking）的 LangGraph Agent 全部定義都在這裡。

主要職責
  1. 工具定義（@tool）
     - search_stock_news          向量新聞庫混合搜尋（dense + BM25 RRF）
     - search_market_ai_analysis  AI 分析報告向量搜尋
     - get_market_recommendations 推薦個股 / 產業向量搜尋

  2. 模型實例
     - router_model_base       思考模式 Router，綁工具、多輪、支援串流 usage
     - flash_router_model      快捷模式 Router，單次 ainvoke，不帶 stream_options
     - flash_rewrite_model     快捷前置 query 收成模型（小型低延遲）
     - analyst_model           思考模式 Analyst（gpt-5 等大模型，串流）
     - flash_analyst_model     快捷模式 Analyst（gpt-5-mini 等，max_completion_tokens 限制）

  3. LangGraph 圖結構（思考模式）
     router → retry_check → tools（→ router） → analyst → END
     - call_router     決定呼叫哪些工具（支援 ROUTER_MAX_CYCLES 限制）
     - call_tools      並行執行工具，累積 retrieved_data
     - call_analyst    組裝【完整參考資料】後串流輸出分析報告
     - retry_check     空結果偵測，觸發重試提示

  4. 輔助函式
     - _format_retrieved_data_for_analyst  retrieved_data → Analyst 參考區塊
     - _slim_messages_for_router           裁切歷史 context 給 Router
     - _analyst_turn_messages              去除 ToolMessage 給 Analyst

注意：一般對話（chat_mode=general）不使用本模組，請見 general_chat.py。
"""
import os
import time
import asyncio
from typing import TypedDict, Annotated, List, Tuple, Union, Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv

from langchain_openai import OpenAIEmbeddings

from app.backend.agent.prompts import (
    build_analyst_system_prompt,
    build_router_system_prompt,
    build_analyst_length_suffix,
)
from app.backend.agent.stream_usage_chat_openai import StreamUsageChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END

# 導入我們之前寫好的工具函式
from app.backend.tools.news import search_news
from app.backend.tools.ai_analysis import search_ai_analysis, search_recommendations

load_dotenv()

# 共用 Embedding 客戶端（避免每次工具呼叫重建 HTTP 連線設定）
_embeddings: OpenAIEmbeddings | None = None


def _get_shared_embeddings() -> OpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    return _embeddings


def _clip_for_llm(text: str, max_chars: int) -> str:
    """限制單則片段長度，避免 ToolMessage 過長拖慢 Router / Analyst。"""
    t = text or ""
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _format_retrieved_data_for_analyst(
    items: List[Dict[str, Any]],
    *,
    max_body_chars: Optional[int] = None,
) -> str:
    """
    將 state['retrieved_data'] 轉成 Analyst 專用、未截斷正文的參考區塊。
    （Router 仍只經由 ToolMessage 讀取 _clip_for_llm 後的摘要。）
    """
    if not items:
        return ""
    blocks: List[str] = []
    for i, it in enumerate(items, start=1):
        st = it.get("source_tool") or "unknown"
        title = it.get("title") or "（無標題）"
        body = str(it.get("content", "") or "").strip()
        if max_body_chars is not None and len(body) > max_body_chars:
            body = body[: max_body_chars - 1] + "…"
        lines: List[str] = [f"### [{i}] 來源: {st} ｜ {title}"]

        if st == "news":
            if it.get("stock_names"):
                lines.append(f"相關個股: {', '.join(it['stock_names'])}")
            if it.get("publishAt"):
                lines.append(f"時間: {it['publishAt']}")
        elif st == "ai_analysis":
            if it.get("sentiment_label"):
                lines.append(f"情緒: {it['sentiment_label']}")
            if it.get("industry_list"):
                lines.append(f"產業: {', '.join(it['industry_list'])}")
            if it.get("publishAt"):
                lines.append(f"時間: {it['publishAt']}")
            snt = it.get("source_news_titles") or []
            if snt:
                lines.append("參考新聞: " + "; ".join(str(x) for x in snt[:5]))
        elif st == "web":
            if it.get("url"):
                lines.append(f"網址: {it['url']}")
            if it.get("publishAt"):
                lines.append(f"時間: {it['publishAt'][:10] if it['publishAt'] else ''}")
        elif st == "recommendations":
            if it.get("publishAt"):
                lines.append(f"時間: {it['publishAt']}")
            if it.get("sentiment_label"):
                lines.append(f"情緒: {it['sentiment_label']}")

        lines.append("")
        lines.append(body if body else "（無內文）")
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


# 預設檢索筆數與注入 Analyst 的單則字元上限（降低以加快回應速度）
RETRIEVAL_TOP_K = 5
# Router／工具回覆寫入 LLM 時，單一檢索片段字元上限（超過截斷；向量庫原文仍保存在 retrieved_data）
MAX_TOOL_ITEM_CHARS = int(os.getenv("MAX_TOOL_ITEM_CHARS", "500"))
# Router 歷史 slim context：只保留最近 N 輪的對話（每輪 = 1 問 + 1 答 = 2 則）
ROUTER_HISTORY_TURNS = int(os.getenv("ROUTER_HISTORY_TURNS", "2"))
# 每次使用者提問，tavily_global_search 最多可呼叫幾次（跨工具輪次累計）
TAVILY_MAX_CALLS_PER_TURN: int = int(os.getenv("TAVILY_MAX_CALLS_PER_TURN", "2"))


def _strip_tool_calls_for_orphan_ai(m: BaseMessage) -> BaseMessage:
    """
    OpenAI Chat：若 AIMessage 帶 tool_calls，下一則必須是對應的 ToolMessage。
    精簡本輪訊息時若已丟棄舊批 ToolMessage，必須一併清掉先前 AIMessage 的 tool_calls，
    否則會 400 invalid_request_error（missing tool responses）。

    僅建新 AIMessage(content=…) 不可靠：LC 會帶 residual tool_calls / additional_kwargs。
    """
    if not isinstance(m, AIMessage):
        return m
    tcalls = getattr(m, "tool_calls", None) or []
    inv = getattr(m, "invalid_tool_calls", None) or []
    ak = getattr(m, "additional_kwargs", None) or {}
    has_shadow_tool = isinstance(ak, dict) and bool(
        ak.get("tool_calls") or ak.get("function_call")
    )
    if not tcalls and not inv and not has_shadow_tool:
        return m
    new_kwargs: Dict[str, Any] = {}
    if isinstance(ak, dict):
        new_kwargs = {
            k: v
            for k, v in ak.items()
            if k not in ("tool_calls", "function_call")
        }
    update_payload = {
        "tool_calls": [],
        "invalid_tool_calls": [],
        "additional_kwargs": new_kwargs,
    }
    # Pydantic v2：`model_copy`；部分 LangChain / 復原來源的 AIMessage 可能沒有此法（環境漂移或序列化）。
    dup = getattr(m, "model_copy", None)
    if callable(dup):
        return dup(update=update_payload)
    # Pydantic v1 相容：`BaseModel.copy(update=…)`
    legacy = getattr(m, "copy", None)
    if callable(legacy):
        try:
            return legacy(update=update_payload)
        except TypeError:
            pass
    # 最後手段：建新訊息（不把 tool_calls 殘留给 OpenAI）。
    return AIMessage(
        content=getattr(m, "content", "") or "",
        additional_kwargs=dict(new_kwargs),
        tool_calls=[],
        invalid_tool_calls=[],
        id=getattr(m, "id", None),
        name=getattr(m, "name", None),
        response_metadata=dict(getattr(m, "response_metadata", None) or {}),
    )

# --- 1. 定義狀態 (State) ---
class AgentState(TypedDict):
    # 紀錄完整的對話歷史
    messages: Annotated[List[BaseMessage], lambda x, y: x + y]
    # 紀錄各階段的執行時間與中間細節 (例如: router_thought, tool_times, etc.)
    trace: Dict[str, Any]
    # 儲存檢索到的原始結構化數據，供 API 讀取 Metadata
    retrieved_data: Annotated[List[Dict[str, Any]], lambda x, y: x + y]
    # 前端指定的可用工具列表 (可選)
    enabled_tools: List[str]
    # 本輪（單次使用者提問）已呼叫 tavily_global_search 的累計次數
    web_search_calls: int

# --- 2. 封裝工具 (Tool Wrappers) ---
# 這裡將之前寫好的 async 函式封裝成 LangChain 可識別的 @tool

@tool
async def search_stock_news(
    query: str,
    start_date: str = None,
    end_date: str = None,
    stock_code: str = None,
    news_type: str = None,
    keyword: str = None,
    stock_name: str = None,
):
    """
    搜尋股市相關新聞。
    注意：若使用者詢問「最近」、「最新」或「這幾天」，請根據當前提問時間計算出具體日期範圍。
    - 最近：通常指過去 14 天。
    範例：若今天是 2026-04-18，最近 14 天則 start_date="2026-04-04T00:00:00Z"。
    參數:
        query: 搜尋關鍵字。
        start_date: 開始時間 (ISO 格式，如 2026-04-01T00:00:00Z)。
        end_date: 結束時間 (ISO 格式)。
        stock_code: 股票代碼過濾 (如 "2330" 代表台積電)。若使用者提及特定個股，請填入其代碼。
        news_type: 新聞類型過濾 (如 "台股新聞" 或 "國際新聞")。
        keyword: 關鍵字標籤過濾 (如 "國巨"、"台積電"、"AI")。
            用於匹配新聞的 keywords 標籤。
        stock_name: 股票名稱過濾 (如 "勤誠"、"台積電")。
            用於匹配新聞的 stock_names 標籤。
        keyword、stock_code、stock_name 三者以 OR 邏輯匹配，只要其中任一命中即可。
        建議查詢個股時同時填入三者以最大化搜尋命中率。
    """
    emb = _get_shared_embeddings()
    query_vector = await emb.aembed_query(query)

    result = await search_news(
        query=query,
        query_embedding=query_vector,
        chat_id="agent_session",
        top_k=RETRIEVAL_TOP_K,
        start_date=start_date,
        end_date=end_date,
        stock_code=stock_code,
        news_type=news_type,
        keyword=keyword,
        stock_name=stock_name,
    )

    # 格式化回傳給 LLM 讀取的字串
    if not result["context"]:
        return "找不到相關新聞。"

    output = "找到以下新聞片段：\n"
    for item in result["context"]:
        stocks_info = ""
        if item.get("stock_names"):
            stocks_info = f" [相關個股: {', '.join(item['stock_names'])}]"
        body = _clip_for_llm(str(item.get("content", "")), MAX_TOOL_ITEM_CHARS)
        output += f"- 【{item['title']}】{stocks_info}: {body}\n"
    return output

@tool
async def search_market_ai_analysis(
    query: str,
    start_date: str = None,
    end_date: str = None,
    sentiment: str = None,
    industry: str = None,
):
    """
    搜尋投研機構產出的 AI 市場分析報告。這比一般新聞更具備專業深度。
    注意：若問題包含「最近」、「最近一月」或「近況」，必須填入具體的 start_date。
    - 最近一月：指過去 30 天。
    參數:
        query: 搜尋關鍵字。
        start_date: 開始時間 (ISO 格式)。
        end_date: 結束時間 (ISO 格式)。
        sentiment: 情緒過濾 ("positive" / "negative" / "neutral")。若使用者問「利空」用 negative，「利多」用 positive。
        industry: 產業標籤過濾 (如 "半導體測試"、"能源")。
    """
    emb = _get_shared_embeddings()
    query_vector = await emb.aembed_query(query)

    result = await search_ai_analysis(
        query=query,
        query_embedding=query_vector,
        chat_id="agent_session",
        top_k=RETRIEVAL_TOP_K,
        start_date=start_date,
        end_date=end_date,
        sentiment=sentiment,
        industry=industry,
    )

    if not result["context"]:
        return "找不到相關的 AI 分析報告。"

    output = "找到以下 AI 分析報告：\n"
    for item in result["context"]:
        meta_parts = []
        if item.get("sentiment_label"):
            meta_parts.append(f"情緒: {item['sentiment_label']}")
        if item.get("industry_list"):
            meta_parts.append(f"產業: {', '.join(item['industry_list'])}")
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""

        output += f"\n--- 【{item['title']}】{meta} ---\n"
        output += f"{_clip_for_llm(str(item.get('content', '')), MAX_TOOL_ITEM_CHARS)}\n"
        if item.get("source_news_titles"):
            output += f"  參考新聞: {'; '.join(item['source_news_titles'][:3])}\n"

    return output

@tool
async def get_market_recommendations(start_date: str = None, end_date: str = None):
    """
    從分析報告中提取推薦的股票清單與關注產業。
    當使用者詢問「推薦什麼」、「有哪些潛力股」時必須使用。
    強烈規範：本工具請務必計算並填入時間範圍。預設建議搜尋過去 14 天內容。
    參數:
        start_date: 開始時間 (ISO 格式)。
        end_date: 結束時間 (ISO 格式)。
    """
    emb = _get_shared_embeddings()
    # 使用固定關鍵字來抓取推薦類型的報告
    query_vector = await emb.aembed_query("推薦股票、強勢產業、潛力標的、看好板塊")

    result = await search_recommendations(
        query_embedding=query_vector,
        start_date=start_date,
        end_date=end_date,
        top_k=RETRIEVAL_TOP_K,
    )

    if not result["stocks"] and not result["industries"]:
        return "在此時間區間內找不到特定推薦的股票或產業。"

    output = "根據近期分析報告，整理出的推薦資訊如下：\n"
    if result["stocks"]:
        output += f"📈 推薦股票：{', '.join(result['stocks'])}\n"
    if result["industries"]:
        output += f"🏭 關注產業：{', '.join(result['industries'])}\n"

    output += "\n資料來源報告：\n"
    for src in result["sources"][:5]:
        sentiment_tag = f" [{src.get('sentiment_label', '')}]" if src.get("sentiment_label") else ""
        publish_date = src.get("publishAt", "")[:10] if src.get("publishAt") else "未知日期"
        output += f"- {src['title']} ({publish_date}){sentiment_tag}\n"
        if src.get("source_news_titles"):
            output += f"  參考新聞: {'; '.join(src['source_news_titles'][:2])}\n"

    return output

@tool
async def tavily_global_search(query: str):
    """
    搜尋即時網路資訊（透過 Tavily Search API）。
    適用於：最新時事、一般知識、產品說明、政策法規、非股市專業問題等。
    注意：若問題能由股市新聞庫（search_stock_news）或 AI 分析報告（search_market_ai_analysis）回答，請優先使用那些工具。
    參數:
        query: 搜尋關鍵字（中英文皆可）。
    """
    from app.backend.tools.global_search import tavily_search, TavilySearchError
    try:
        result = await tavily_search(query)
    except TavilySearchError as e:
        return f"網路搜尋失敗：{e}"

    if not result["results"]:
        return "找不到相關網路資訊。"

    lines: List[str] = []
    if result.get("answer"):
        lines.append(f"即時摘要：{result['answer']}\n")
    for r in result["results"]:
        date_str = f" ({r['published_date'][:10]})" if r.get("published_date") else ""
        body = _clip_for_llm(r.get("content", ""), MAX_TOOL_ITEM_CHARS)
        lines.append(f"【{r['title']}】{date_str}\n來源：{r['url']}\n{body}")
    return "\n\n---\n".join(lines)


tools = [search_stock_news, search_market_ai_analysis, get_market_recommendations, tavily_global_search]

# OpenAI streaming：在尾包帶 usage（供 astream_events / on_chat_model_end 聚合）
_OPENAI_STREAM_OPTS: Dict[str, Any] = {"stream_options": {"include_usage": True}}

# reasoning 模型（gpt-5 / gpt-5-mini）的思考強度：延遲對此極度敏感。
# 目前 langchain-openai 版本無 reasoning_effort 欄位，故經 model_kwargs 直接帶進 OpenAI API。
# 合法值：minimal / low / medium / high。Router 只需選工具 → minimal；Analyst 需論述 → low。
# 若要恢復預設深度思考，設為 medium/high 或留空（留空則不帶此參數，走模型預設）。
_ROUTER_REASONING_EFFORT = os.getenv("ROUTER_REASONING_EFFORT", "minimal").strip()
_ANALYST_REASONING_EFFORT = os.getenv("ANALYST_REASONING_EFFORT", "low").strip()


def _with_reasoning_effort(base: Dict[str, Any], effort: str) -> Dict[str, Any]:
    """把 reasoning_effort 併入 model_kwargs；effort 為空字串時不帶（走模型預設）。"""
    kw = dict(base)
    if effort:
        kw["reasoning_effort"] = effort
    return kw


# --- 模型配置 ---
# 1. 導航模型 (Router): 速度快、工具調用準確（搭配 LangGraph / astream_events 串流）
router_model_base = StreamUsageChatOpenAI(
    model="gpt-5-mini",
    temperature=1,
    model_kwargs=_with_reasoning_effort(_OPENAI_STREAM_OPTS, _ROUTER_REASONING_EFFORT),
)
# Flash 快捷模式：單次 `ainvoke` 非串流；OpenAI 不允許在非 stream 請求帶 `stream_options`
flash_router_model = StreamUsageChatOpenAI(
    model="gpt-5-mini",
    temperature=1,
    model_kwargs={},
)

# Flash 前置「檢索用問句整理」：`ainvoke` 非串流，禁 `stream_options`；宜用小型／廉價模型減輕成本與延遲
_FLASH_REWRITE_MODEL = os.getenv("FLASH_REWRITE_MODEL", "gpt-4o-mini").strip()
_flash_rewrite_kw: Dict[str, Any] = {}
_fr_mx = os.getenv("FLASH_REWRITE_MAX_COMPLETION_TOKENS", "256").strip()
if _fr_mx:
    try:
        _flash_rewrite_kw["max_completion_tokens"] = int(_fr_mx)
    except ValueError:
        pass
flash_rewrite_model = StreamUsageChatOpenAI(
    model=_FLASH_REWRITE_MODEL,
    temperature=0,
    model_kwargs=_flash_rewrite_kw,
)
# 思考模式 Analyst 最終報告的「可見字數」目標（軟性，由 prompt 約束）。
# 三者任一設為空或 <=0 → 不加篇幅約束（恢復不限長度的舊行為）。
_ANALYST_TARGET_MIN_WORDS = os.getenv("ANALYST_TARGET_MIN_WORDS", "1200").strip()
_ANALYST_TARGET_MAX_WORDS = os.getenv("ANALYST_TARGET_MAX_WORDS", "1800").strip()
_ANALYST_HARD_CAP_WORDS = os.getenv("ANALYST_HARD_CAP_WORDS", "2600").strip()


def _compute_analyst_length_suffix() -> str:
    try:
        mn = int(_ANALYST_TARGET_MIN_WORDS)
        mx = int(_ANALYST_TARGET_MAX_WORDS)
        cap = int(_ANALYST_HARD_CAP_WORDS)
    except ValueError:
        return ""
    if mn <= 0 or mx <= 0 or cap <= 0:
        return ""
    return build_analyst_length_suffix(mn, mx, cap)


# 於 module 載入時算好，避免每次 call_analyst 重建字串
_ANALYST_LENGTH_SUFFIX = _compute_analyst_length_suffix()

# 2. 分析模型 (Analyst): 深度思考、文筆詳盡
# 可選硬上限：ANALYST_MAX_COMPLETION_TOKENS（預設不設）。
# ⚠️ gpt-5 為 reasoning 模型，max_completion_tokens 會「連思考 token 一起算」，
#    設太低可能把報告從中間截斷甚至無輸出；請當作寬鬆保險絲，篇幅控制以 prompt 為主。
_analyst_model_kwargs = _with_reasoning_effort(_OPENAI_STREAM_OPTS, _ANALYST_REASONING_EFFORT)
_an_max = os.getenv("ANALYST_MAX_COMPLETION_TOKENS", "").strip()
if _an_max:
    try:
        _analyst_model_kwargs["max_completion_tokens"] = int(_an_max)
    except ValueError:
        pass
analyst_model = StreamUsageChatOpenAI(
    model="gpt-5",
    temperature=1,
    model_kwargs=_analyst_model_kwargs,
)
# 快捷模式專用：`gpt-5` 等新模型不接受 `max_tokens`，須改用 `max_completion_tokens`
# （置於 model_kwargs；環境變數名仍為 FLASH_ANALYST_MAX_TOKENS）
_FLASH_ANALYST_MODEL = os.getenv("FLASH_ANALYST_MODEL", "gpt-5-mini").strip()
_flash_an_kw: Dict[str, Any] = dict(_OPENAI_STREAM_OPTS)
_mx = os.getenv("FLASH_ANALYST_MAX_TOKENS", "5000").strip()
if _mx:
    try:
        _flash_an_kw["max_completion_tokens"] = int(_mx)
    except ValueError:
        pass

flash_analyst_model = StreamUsageChatOpenAI(
    model=_FLASH_ANALYST_MODEL,
    temperature=1,
    model_kwargs=_flash_an_kw,
)

# Router / 檢索反覆上限（與 trace 裡 node=="router" 筆數對齊）
# 前 N 次進入 router 可綁定工具；自第 N+1 次起卸除工具強制收斂（N = 此常數）。
ROUTER_MAX_CYCLES = 3


def _slim_messages_for_router(
    messages: List[BaseMessage],
    history_turns: int = ROUTER_HISTORY_TURNS,
) -> List[BaseMessage]:
    """
    為 Router 提供精簡版的對話上下文，大幅降低 prompt tokens。

    Router 只需要：
    1. 最近 history_turns 輪的歷史（每輪 = HumanMessage + AIMessage = 2 則）
       讓 Router 知道對話主題，避免重複問相同問題
    2. 當前輪次訊息，但只保留「最新一批 ToolMessages」：
       - 所有 AIMessage 全部保留（Router 知道自己已試過哪些工具）
       - 舊批次的 ToolMessages 丟棄（節省 token）
       - 最後一個 AIMessage 之後的 ToolMessages 全部保留（最新結果）

    Before（Router 第 3 次呼叫）：
      HumanMsg | AIMsg(calls=A) | ToolMsg_A1 ToolMsg_A2 | AIMsg(calls=B) | ToolMsg_B1 ToolMsg_B2
    After：
      HumanMsg | AIMsg(calls=A) |                       | AIMsg(calls=B) | ToolMsg_B1 ToolMsg_B2

    （Analyst 另見 `_analyst_turn_messages`：不依賴 ToolMessage 冗長摘要。）
    """
    # 找出最後一個 HumanMessage 的位置（= 本輪 user query）
    last_human_idx = 0
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            last_human_idx = i

    # 本輪訊息：從最後一則 HumanMessage 起
    current_round = messages[last_human_idx:]

    # 歷史訊息：最後一則 HumanMessage 以前的所有訊息
    history = messages[:last_human_idx]

    # 歷史只保留最近 N 輪（HumanMessage + AIMessage，忽略歷史中的 ToolMessage）
    clean_history = [
        _strip_tool_calls_for_orphan_ai(m)
        for m in history
        if isinstance(m, (HumanMessage, AIMessage))
    ]
    slim_history = clean_history[-(history_turns * 2):] if clean_history else []

    # ── 方向 A：當前輪只保留最新一批 ToolMessages ──
    # 找出 current_round 中最後一個 AIMessage 的位置
    last_ai_idx_in_round = -1
    for i, m in enumerate(current_round):
        if isinstance(m, AIMessage):
            last_ai_idx_in_round = i

    if last_ai_idx_in_round < 0:
        # 本輪還沒有 AIMessage（第一次進 Router），直接使用完整 current_round
        slim_current = current_round
    else:
        # 最後一個 AIMessage 之前：丟棄舊批次 ToolMessages；若砍掉工具回覆，
        # 必須把前面的 AIMessage 之 tool_calls 一併剝掉，否則 API 規格不符
        pre_last_ai = [
            _strip_tool_calls_for_orphan_ai(m)
            for m in current_round[:last_ai_idx_in_round]
            if not isinstance(m, ToolMessage)
        ]
        # 最後一個 AIMessage 之後：完整保留（最新一批 ToolMessages 結果）
        post_last_ai = current_round[last_ai_idx_in_round:]
        slim_current = pre_last_ai + post_last_ai

    return slim_history + slim_current


def _analyst_turn_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    """
    Analyst 請求不包含 ToolMessage 全文：檢索正文改由末尾【完整參考資料】提供（來自 retrieved_data，未套用 MAX_TOOL_ITEM_CHARS）。
    略過 ToolMessage 時須對帶 tool_calls 的 AIMessage 剝除外掛呼叫，以免違反 OpenAI 「tool_calls 後須接 tool」規則。
    """
    out: List[BaseMessage] = []
    for m in messages:
        if isinstance(m, ToolMessage):
            continue
        if isinstance(m, AIMessage):
            out.append(_strip_tool_calls_for_orphan_ai(m))
        else:
            out.append(m)
    return out


def _state_had_any_tool_messages(messages: List[BaseMessage]) -> bool:
    return any(isinstance(m, ToolMessage) for m in messages)


# --- 3. 定義節點 (Nodes) ---

async def call_router(state: AgentState):
    """Router 節點：負責決定要調用哪些工具"""
    start_time = time.time()
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 取得前端指定的工具清單
    enabled = state.get("enabled_tools", [])
    # 這裡列出我們系統中「真正實作」的工具名稱
    all_tool_names = ["search_stock_news", "search_market_ai_analysis", "get_market_recommendations", "tavily_global_search"]
    
    # 邏輯：過濾掉前端傳入但不認識的名稱，確保 AI 不會試圖呼叫不存在的工具
    valid_enabled = [t for t in enabled if t in all_tool_names]
    
    # 決定最終要告訴 AI 的「可用工具箱」
    # 如果前端傳入的清單有合法項，就以該清單為主；否則全開
    target_tools = valid_enabled if valid_enabled else all_tool_names
    
    system_prompt = build_router_system_prompt(current_now, target_tools)
    
    # 核心修正：動態挑選工具物件實體，進行硬性綁定
    # 從 tools 全域變數中，找出名稱符合 target_tools 的物件
    current_tools_to_bind = [t for t in tools if t.name in target_tools]
    
    # 萃取目前的 trace 紀錄，計算已經走過幾次 router 節點
    trace = state.get("trace", {})
    router_cycles = sum(1 for step in trace.get("steps", []) if step.get("node") == "router")

    if router_cycles >= ROUTER_MAX_CYCLES:
        # 達到上限：清空可用工具，並在 prompt 加上強制終止指令
        current_tools_to_bind = []
        system_prompt += f"\n\n[系統通知 - 極重要] 檢索次數已達上限 ({ROUTER_MAX_CYCLES}次)。請立刻根據你目前手邊已獲取的所有資料進行總結與回覆。"
        # 不綁定工具，強迫產出純文字
        dynamic_router = router_model_base
    else:
        # 這裡我們將 Router 模型動態綁定選後的工具
        dynamic_router = router_model_base.bind_tools(current_tools_to_bind)

    # Router 使用精簡版 context：只看最近 ROUTER_HISTORY_TURNS 輪歷史 + 本輪訊息
    # Analyst 另組訊息（見 call_analyst 的 `_analyst_turn_messages`）
    slim_msgs = _slim_messages_for_router(messages)
    router_prompt = [SystemMessage(content=system_prompt)] + slim_msgs
    response = await dynamic_router.ainvoke(router_prompt)
    
    execution_time = time.time() - start_time
    
    # 建立更有意義的思考軌跡
    thought = ""
    if hasattr(response, "tool_calls") and response.tool_calls:
        tool_names = [tc["name"] for tc in response.tool_calls]
        thought = f"為了精準回答，啟動 {', '.join(tool_names)} 來獲取股市資訊。"
    else:
        thought = response.content if response.content else "資料已備齊，準備撰寫專業報告。"

    # 紀錄 Router 階段資訊 (使用更加詳細且平坦的結構)
    trace = state.get("trace", {})
    if "steps" not in trace:
        trace["steps"] = []
    
    formatted_tool_calls = []
    if hasattr(response, "tool_calls") and response.tool_calls:
        for tc in response.tool_calls:
            args = tc.get("args", {})
            formatted_tool_calls.append({
                "name": tc["name"],
                "query": args.get("query"),
                "start_date": args.get("start_date"),
                "end_date": args.get("end_date"),
                "raw_args": args # 保留原始參數供參考
            })

    trace["steps"].append({
        "node": "router",
        "execution_time": round(execution_time, 3),
        "tool_calls": formatted_tool_calls,
        "thought": thought
    })
    
    return {"messages": [response], "trace": trace}

async def call_analyst(state: AgentState):
    """Analyst 節點：負責整合資料與產出研究報告（支援 astream_events token 串流）"""
    start_time = time.time()
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    analyst_prompt = build_analyst_system_prompt(current_now)
    if _ANALYST_LENGTH_SUFFIX:
        analyst_prompt += "\n" + _ANALYST_LENGTH_SUFFIX
    full_ref = _format_retrieved_data_for_analyst(state.get("retrieved_data", []))
    tail: List[BaseMessage] = []
    if full_ref.strip():
        tail.append(SystemMessage(
            content="【完整參考資料】（供撰寫報告，以下為向量庫取回之完整正文，未套用 Router 側單片段字數截斷）\n\n" + full_ref
        ))
    elif _state_had_any_tool_messages(messages):
        tail.append(SystemMessage(
            content="【檢索狀態】本輪未寫入可引用之完整參考段落（資料庫可能無命中或結果未進入向量正文）。請依對話誠實說明資料缺口，切勿臆撰具體數據、股價或標的細節。"
        ))
    chat_turns = _analyst_turn_messages(messages)
    full_messages = [SystemMessage(content=analyst_prompt)] + chat_turns + tail

    # 使用 astream 逐 chunk 累積，讓 astream_events 能在 analyst 節點期間
    # 發出 on_chat_model_stream 事件供 API 層轉發 token
    full_content = ""
    async for chunk in analyst_model.astream(full_messages):
        full_content += chunk.content or ""

    from langchain_core.messages import AIMessage
    response = AIMessage(content=full_content)
    
    execution_time = time.time() - start_time
    trace = state.get("trace", {})
    if "steps" not in trace:
        trace["steps"] = []
        
    trace["steps"].append({
        "node": "analyst",
        "execution_time": round(execution_time, 3),
        "content": response.content
    })
    
    # 同時保留一個最後的摘要紀錄方便 API 讀取
    trace["final_analyst"] = {
        "execution_time": round(execution_time, 3),
        "content": response.content
    }
    
    return {"messages": [response], "trace": trace}

async def call_tools(state: AgentState, *, retrieval_top_k: Optional[int] = None):
    """執行工具呼叫節點 (v3: 並行執行 + Embedding 快取)。
    retrieval_top_k：快捷模式傳較小值可縮短向量查詢時間與上下文。"""
    last_message = state["messages"][-1]
    top_k_eff = retrieval_top_k if retrieval_top_k is not None else RETRIEVAL_TOP_K

    # 🆕 P4: 統一建立 Embedding 實例 + 快取，避免重複 API 呼叫
    emb = _get_shared_embeddings()
    embedding_cache: Dict[str, List[float]] = {}

    async def get_cached_embedding(text: str) -> List[float]:
        if text not in embedding_cache:
            embedding_cache[text] = await emb.aembed_query(text)
        return embedding_cache[text]

    # 🆕 P3: 定義單一工具的執行邏輯，供並行調用
    async def execute_single_tool(tool_call) -> Tuple[ToolMessage, List[Dict[str, Any]]]:
        tool_name = tool_call["name"]
        args = tool_call["args"]
        retrieved = []

        if tool_name == "search_stock_news":
            query_text = args.get("query", "")
            raw_result = await search_news(
                query=query_text,
                query_embedding=(await get_cached_embedding(query_text)),
                chat_id="api_call",
                top_k=top_k_eff,
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                stock_code=args.get("stock_code"),
                news_type=args.get("news_type"),
                keyword=args.get("keyword"),
                stock_name=args.get("stock_name"),
            )
            parts = []
            for c in raw_result.get("context", []):
                stock_info = ""
                if c.get("stock_names"):
                    stock_info = f" [相關: {', '.join(c['stock_names'])}]"
                body = _clip_for_llm(str(c.get("content", "")), MAX_TOOL_ITEM_CHARS)
                parts.append(f"[{c.get('title')}]{stock_info}: {body}")
            ai_content = "\n\n".join(parts) if parts else "找不到相關新聞。"
            for c in raw_result.get("context", []):
                retrieved.append({**c, "source_tool": "news"})

        elif tool_name == "search_market_ai_analysis":
            query_text = args.get("query", "")
            raw_result = await search_ai_analysis(
                query=query_text,
                query_embedding=(await get_cached_embedding(query_text)),
                chat_id="api_call",
                top_k=top_k_eff,
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                sentiment=args.get("sentiment"),
                industry=args.get("industry"),
            )
            parts = []
            for c in raw_result.get("context", []):
                meta = ""
                meta_parts = []
                if c.get("sentiment_label"):
                    meta_parts.append(f"情緒:{c['sentiment_label']}")
                if c.get("industry_list"):
                    meta_parts.append(f"產業:{','.join(c['industry_list'])}")
                if meta_parts:
                    meta = f" ({', '.join(meta_parts)})"
                body = _clip_for_llm(str(c.get("content", "")), MAX_TOOL_ITEM_CHARS)
                entry = f"[{c.get('title')}]{meta}: {body}"
                if c.get("source_news_titles"):
                    entry += f"\n  參考新聞: {'; '.join(c['source_news_titles'][:3])}"
                parts.append(entry)
            ai_content = "\n\n".join(parts) if parts else "找不到相關的 AI 分析報告。"
            for c in raw_result.get("context", []):
                retrieved.append({**c, "source_tool": "ai_analysis"})

        elif tool_name == "get_market_recommendations":
            raw_result = await search_recommendations(
                query_embedding=(await get_cached_embedding("推薦股票與強勢產業")),
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                top_k=top_k_eff,
            )
            lines = []
            if raw_result.get("stocks"):
                lines.append(f"推薦股票清單: {', '.join(raw_result['stocks'])}")
            if raw_result.get("industries"):
                lines.append(f"推薦產業清單: {', '.join(raw_result['industries'])}")
            for src in raw_result.get("sources", [])[:5]:
                sentiment_tag = f" [{src.get('sentiment_label', '')}]" if src.get("sentiment_label") else ""
                lines.append(f"- {src.get('title')} ({src.get('publishAt', '')[:10]}){sentiment_tag}: {src.get('content', '')[:200]}")
            ai_content = "\n".join(lines) if lines else "找不到推薦資訊。"
            for s in raw_result.get("sources", []):
                retrieved.append({**s, "source_tool": "recommendations"})
        elif tool_name == "tavily_global_search":
            from app.backend.tools.global_search import tavily_search, TavilySearchError
            query_text = args.get("query", "")
            # 檢查本輪已呼叫次數是否達上限
            if state.get("web_search_calls", 0) >= TAVILY_MAX_CALLS_PER_TURN:
                ai_content = f"本輪網路搜尋已達上限（{TAVILY_MAX_CALLS_PER_TURN} 次），請根據已有資料作答。"
                raw_result = {"results": [], "answer": None}
            else:
                try:
                    raw_result = await tavily_search(query_text)
                except TavilySearchError as e:
                    ai_content = f"網路搜尋失敗：{e}"
                    raw_result = {"results": [], "answer": None}
                else:
                    lines: List[str] = []
                    if raw_result.get("answer"):
                        lines.append(f"即時摘要：{raw_result['answer']}\n")
                    for r in raw_result.get("results", []):
                        date_str = f" ({r['published_date'][:10]})" if r.get("published_date") else ""
                        body = _clip_for_llm(r.get("content", ""), MAX_TOOL_ITEM_CHARS)
                        lines.append(f"【{r['title']}】{date_str}\n來源：{r['url']}\n{body}")
                    ai_content = "\n\n---\n".join(lines) if lines else "找不到相關網路資訊。"
            for r in raw_result.get("results", []):
                retrieved.append({
                    "source_tool": "web",
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "publishAt": r.get("published_date"),
                    "score": r.get("score", 0),
                })

        else:
            ai_content = f"錯誤：找不到工具 {tool_name}"

        msg = ToolMessage(content=ai_content, tool_call_id=tool_call["id"], name=tool_name)
        return msg, retrieved

    # 🆕 P3: 並行執行所有工具 (asyncio.gather)
    results = await asyncio.gather(*[
        execute_single_tool(tc) for tc in last_message.tool_calls
    ])

    tool_messages = [r[0] for r in results]
    new_retrieved_data = []
    for r in results:
        new_retrieved_data.extend(r[1])

    # 計算本批次實際執行了幾次 tavily_global_search（未達上限才真正執行的才算）
    tavily_executed = sum(
        1 for tc in last_message.tool_calls
        if tc["name"] == "tavily_global_search"
        and state.get("web_search_calls", 0) < TAVILY_MAX_CALLS_PER_TURN
    )

    return {
        "messages": tool_messages,
        "retrieved_data": new_retrieved_data,
        "web_search_calls": state.get("web_search_calls", 0) + tavily_executed,
    }

# --- 4. 定義邊界邏輯 (Conditional Edges) ---

async def retry_check(state: AgentState):
    """Safety net：若工具全部返回空結果且未達上限，注入重試提示強制 Router 重新搜尋"""
    last_message = state["messages"][-1]

    # 如果 Router 已經發起新的工具呼叫，直接通過
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return {"messages": [], "trace": state.get("trace", {})}

    trace = state.get("trace", {})
    router_cycles = sum(1 for s in trace.get("steps", []) if s.get("node") == "router")

    if router_cycles < ROUTER_MAX_CYCLES:
        # 檢查最近的 Tool Messages 是否全為空結果
        tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
        if tool_msgs:
            empty_keywords = ["找不到", "找不到相關", "找不到推薦"]
            recent_tools = tool_msgs[-3:]  # 檢查最近幾則
            all_empty = all(
                any(kw in m.content for kw in empty_keywords)
                for m in recent_tools
            )
            if all_empty:
                hint = SystemMessage(content=(
                    "[系統提示] 前一次搜尋所有工具均回傳空結果。"
                    "請參考你上一段回覆中提出的備選方案，選擇最合適的策略重試搜尋。"
                    "優先嘗試：擴大時間範圍、更換關鍵字（中/英文）、移除過濾條件。"
                    "不要詢問使用者，直接執行搜尋。"
                ))
                return {"messages": [hint], "trace": trace}

    return {"messages": [], "trace": state.get("trace", {})}

def should_continue_after_check(state: AgentState):
    """retry_check 之後的路由判斷"""
    last_message = state["messages"][-1]

    # 如果最後一條是 SystemMessage（重試提示），回到 router
    if isinstance(last_message, SystemMessage):
        return "router"

    # 如果有 tool_calls，去執行工具
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"

    # 否則進入分析
    return "analyst"

# --- 5. 建立 Graph ---
# 流程: Router → retry_check → (tools / router 重試 / analyst)

def create_chat_agent():
    workflow = StateGraph(AgentState)

    # 添加節點
    workflow.add_node("router", call_router)
    workflow.add_node("retry_check", retry_check)
    workflow.add_node("tools", call_tools)
    workflow.add_node("analyst", call_analyst)

    # 設置起點
    workflow.set_entry_point("router")

    # Router 產出後先經過 retry_check
    workflow.add_edge("router", "retry_check")

    # retry_check 判斷是否重試、執行工具、或進入分析
    workflow.add_conditional_edges("retry_check", should_continue_after_check)

    # 工具執行完回到 Router
    workflow.add_edge("tools", "router")
    workflow.add_edge("analyst", END)

    return workflow.compile()

# --- 實例化 Agent ---
app = create_chat_agent()

# --- 測試執行 (範例) ---
if __name__ == "__main__":
    import asyncio
    
    async def run_test():
        print("🤖 Stock Insight Agent 啟動測試...")
        inputs = {"messages": [HumanMessage(content="最近台積電表現如何，也告訴我一些近期推薦的股票跟要關注的產業，還有一些股市分析結果。")]}
        
        async for output in app.astream(inputs):
            for key, value in output.items():
                print(f"\n[進入節點: {key}]")
                last_msg = value["messages"][-1]
                
                # 判斷訊息類型並印出內容
                if isinstance(last_msg, AIMessage):
                    if last_msg.tool_calls:
                        for tc in last_msg.tool_calls:
                            print(f"🛠️  AI 決定調用工具: {tc['name']}")
                            print(f"   參數: {tc['args']}")
                    else:
                        print(f"💬 AI 回答: {last_msg.content}")
                
                elif isinstance(last_msg, ToolMessage):
                    print(f"✅ 工具回傳成功 (長度: {len(last_msg.content)} 字元)")
                    # 如果你想看工具抓到的具體內容，可以取消下面這行註釋
                    # print(f"   內容摘要: {last_msg.content[:200]}...")

    asyncio.run(run_test())
