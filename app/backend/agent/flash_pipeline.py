"""
快捷（flash）模式：預設略過 Router LLM + 僅 `search_news`（經 `search_stock_news`）+ 輕量 Analyst。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from langchain_core.messages import AIMessage, SystemMessage

from app.backend.agent.chat import (
    flash_analyst_model,
    call_tools,
    flash_router_model,
    tools,
    _analyst_turn_messages,
    _format_retrieved_data_for_analyst,
    _slim_messages_for_router,
)
from app.backend.agent.prompts import (
    FLASH_ROUTER_MODE_SUFFIX,
    build_flash_analyst_system_prompt,
    build_router_system_prompt,
)


def _truthy_env(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _safe_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


# 預設略過 Router：省下 ~數秒～十餘秒的規劃 LLM；設 FLASH_SKIP_ROUTER=0 可改回 Router 規劃
FLASH_SKIP_ROUTER = _truthy_env("FLASH_SKIP_ROUTER", "1")
FLASH_RETRIEVAL_TOP_K = max(1, _safe_int_env("FLASH_RETRIEVAL_TOP_K", 10))
FLASH_REF_MAX_BODY_CHARS = max(400, _safe_int_env("FLASH_REF_MAX_BODY_CHARS", 2200))
FLASH_DATE_RANGE_DAYS = max(1, _safe_int_env("FLASH_DATE_RANGE_DAYS", 80))

FLASH_TOOL_NAMES: Tuple[str, ...] = ("search_stock_news",)


def _default_date_range_iso() -> Tuple[str, str]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=FLASH_DATE_RANGE_DAYS)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _coerce_args(a: Any) -> Dict[str, Any]:
    if isinstance(a, dict):
        return dict(a)
    return {}


def _infer_search_stock_news_extras(user_query: str) -> Dict[str, Any]:
    """
    不經 Router LLM：由問句規則試抽過濾欄位，提高「點名股號／KY 標的」命中率。
    僅填入仍為空的欄位；Router 規劃已有值者不覆蓋。
    """
    hints: Dict[str, Any] = {}
    raw = (user_query or "").strip()
    if not raw:
        return hints
    bracket = re.search(r"[（(]\s*(\d{4})\s*[）)]", raw)
    if bracket:
        hints["stock_code"] = bracket.group(1).strip()
    ky = re.search(
        r"([\u4e00-\u9fff0-9、·‧．‧]+)\s*[-‑－—]+\s*[Kｋ][Yｙ]\b",
        raw,
        re.IGNORECASE,
    )
    if ky:
        name_g = ky.group(1).strip().replace(" ", "")
        if len(name_g) >= 2:
            hints.setdefault("stock_name", name_g)
            short_kw = (
                name_g.replace("股份有限公司", "").replace("有限公司", "").replace("電子", "")
            )
            kw_src = short_kw if len(short_kw) >= 2 else name_g
            hints.setdefault("keyword", kw_src[:12])
    return hints


def normalize_flash_tool_calls(
    router_msg: AIMessage,
    user_query: str,
    start_iso: str,
    end_iso: str,
) -> List[Dict[str, Any]]:
    """
    保證 `search_stock_news` 呼叫一次；合併 Router 產生的參數與預設（使用者問句 + FLASH_DATE_RANGE_DAYS 回溯天數）。
    """
    q = (user_query or "").strip() or "股市 概況"
    by_name: Dict[str, Dict[str, Any]] = {}
    raw = getattr(router_msg, "tool_calls", None) or []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name")
        if name not in FLASH_TOOL_NAMES:
            continue
        by_name[name] = _coerce_args(tc.get("args"))

    defaults: Dict[str, Dict[str, Any]] = {
        "search_stock_news": {
            "query": q,
            "start_date": start_iso,
            "end_date": end_iso,
        },
    }

    out: List[Dict[str, Any]] = []
    for idx, name in enumerate(FLASH_TOOL_NAMES):
        merged = dict(defaults[name])
        if name in by_name:
            for k, v in by_name[name].items():
                if v is not None and v != "":
                    merged[k] = v
        if name == "search_stock_news":
            for k_inf, v_inf in _infer_search_stock_news_extras(q).items():
                if merged.get(k_inf) in (None, ""):
                    merged[k_inf] = v_inf
        out.append({
            "name": name,
            "args": merged,
            "id": f"flash-{name}-{idx}",
            "type": "tool_call",
        })
    return out


async def flash_router_phase(
    messages_lc: List[Any],
    user_query: str,
) -> Tuple[AIMessage, List[Dict[str, Any]], Dict[str, Any], float]:
    """
    預設略過 Router LLM：直接以使用者問題填滿檢索參數；可設 FLASH_SKIP_ROUTER=0
    啟用規劃模型（會增加延遲但可細調 query）。

    回傳：(router_msg, normalized_tool_calls, router_trace_step, router_elapsed)
    """
    start_iso, end_iso = _default_date_range_iso()
    router_elapsed = 0.0
    router_msg: AIMessage

    if FLASH_SKIP_ROUTER:
        router_msg = AIMessage(
            content="（FAST：FLASH_SKIP_ROUTER=1，已略過規劃用 LLM，以本輪使用者問題檢索新聞向量庫。）",
            tool_calls=[],
        )
    else:
        current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        system = (
            build_router_system_prompt(current_now, list(FLASH_TOOL_NAMES))
            + FLASH_ROUTER_MODE_SUFFIX
        )
        slim = _slim_messages_for_router(messages_lc)
        bound = flash_router_model.bind_tools([t for t in tools if t.name in FLASH_TOOL_NAMES])
        t0 = time.time()
        router_msg = await bound.ainvoke([SystemMessage(content=system)] + slim)
        router_elapsed = time.time() - t0

    normalized = normalize_flash_tool_calls(router_msg, user_query, start_iso, end_iso)

    formatted_tool_calls: List[Dict[str, Any]] = []
    for tc in normalized:
        a = tc.get("args") or {}
        formatted_tool_calls.append({
            "name": tc["name"],
            "query": a.get("query"),
            "start_date": a.get("start_date"),
            "end_date": a.get("end_date"),
            "raw_args": a,
        })

    thought = (
        "快捷（快速）：已略過 Router LLM（FLASH_SKIP_ROUTER），以使用者問題檢索新聞（search_stock_news）。"
        if FLASH_SKIP_ROUTER else
        f"快捷模式：檢索新聞向量庫（{', '.join(FLASH_TOOL_NAMES)}）。"
    )
    trace_step = {
        "node": "router",
        "execution_time": round(router_elapsed, 3),
        "tool_calls": formatted_tool_calls,
        "thought": thought,
        "mode": "flash",
        "router_skipped": FLASH_SKIP_ROUTER,
    }
    return router_msg, normalized, trace_step, router_elapsed


async def flash_run_tools(normalized_tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """執行新聞工具（與 LangGraph call_tools 同源邏輯）。"""
    fake_ai = AIMessage(content="", tool_calls=normalized_tool_calls)
    tool_out = await call_tools(
        {"messages": [fake_ai]},
        retrieval_top_k=FLASH_RETRIEVAL_TOP_K,
    )
    return tool_out.get("retrieved_data") or []


async def flash_plan_and_retrieve(
    messages_lc: List[Any],
    user_query: str,
) -> Tuple[AIMessage, List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """
    Router 規劃（可省略）→ 正規化 `search_stock_news` → call_tools。

    回傳：(router_ai_message, trace_router_steps, retrieved_data, router_seconds)
    """
    router_msg, normalized, trace_step, router_elapsed = await flash_router_phase(
        messages_lc, user_query
    )
    retrieved = await flash_run_tools(normalized)
    return router_msg, [trace_step], retrieved, router_elapsed


def build_flash_analyst_messages(
    messages_lc: List[Any],
    retrieved_data: List[Dict[str, Any]],
) -> List[Any]:
    """與 LangGraph call_analyst 對齊的訊息組裝（無 ToolMessage；依賴【完整參考資料】）。"""
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    analyst_prompt = build_flash_analyst_system_prompt(current_now)
    full_ref = _format_retrieved_data_for_analyst(
        retrieved_data,
        max_body_chars=FLASH_REF_MAX_BODY_CHARS,
    )
    tail: List[Any] = []
    if full_ref.strip():
        tail.append(SystemMessage(
            content=(
                "以下【完整參考資料】為本輪**唯一可當事實引用**來源。\n"
                "對話中較早的助理回覆若無法在其中逐句核對，則不可用於本輪結論；"
                "亦不可臆造數字。"
                "\n\n"
                "【完整參考資料】（快捷模式：向量庫取回正文；"
                f"單段至多約 {FLASH_REF_MAX_BODY_CHARS} 字以控制延遲）\n\n"
            )
            + full_ref
        ))
    else:
        tail.append(SystemMessage(
            content=(
                "【檢索狀態】本輪未寫入可引用之完整參考段落"
                "（資料庫可能無命中或結果未進入向量正文）。"
                "請依對話誠實說明資料缺口，切勿臆撰具體數據、股價或標的細節。"
            )
        ))
    chat_turns = _analyst_turn_messages(messages_lc)
    return [SystemMessage(content=analyst_prompt)] + chat_turns + tail


async def flash_analyst_astream(full_messages: List[Any]):
    """逐 chunk 串流 Analyst（由 API 層負責累積 usage / SSE）。"""
    async for chunk in flash_analyst_model.astream(full_messages):
        yield chunk

