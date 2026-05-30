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
_GENERAL_SYSTEM_PROMPT = (
    "你是一位友善、知識豐富的 AI 助理，能夠回答使用者各種問題。\n"
    "請以繁體中文回答，語氣自然且清晰。\n"
    "\n"
    "【重要：先讀對話歷史，再回答】\n"
    "回答前，請先完整閱讀對話歷史，從上下文推斷使用者的意圖。\n"
    "若上下文已提供足夠資訊（例如城市、主題、偏好），可以根據資訊回答，不要再反問已知的事情。\n"
    "只有在上下文完全無法判斷使用者意圖時，才可以提問——且一次只問一個最關鍵的問題，不要列出多項選項讓使用者回答。\n"
    "\n"
    "【使用即時網路資料】\n"
    "若【即時網路資料】區塊有內容，請以其為主要事實依據作答，並在回覆中標示來源網址。\n"
    "\n"
    "若使用者詢問股市深度分析（個股財報、技術面、法人動向等），"
    "請建議切換到「股市 Agent」模式以取得更精準的報告。"
)

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
