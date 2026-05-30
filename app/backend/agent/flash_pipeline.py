"""
flash_pipeline.py（股市 Agent — 快捷模式管線）
─────────────────────────────────────────────
快捷（flash）模式的完整非同步管線，不使用 LangGraph 圖，而是直接串接：

  flash_router_phase  → flash_run_tools  → flash_analyst_astream
  （規劃/可省略）       （向量新聞檢索）   （串流輸出）

主要流程（flash_plan_and_retrieve）
  1. Router 階段（可選）
     FLASH_SKIP_ROUTER=1（預設）→ 直接以使用者原文填檢索參數，省去 Router LLM 延遲。
     FLASH_SKIP_ROUTER=0 → 呼叫 flash_router_model 規劃工具參數。

  2. 問句 LLM 收成（可選）
     FLASH_LLM_QUERY_REWRITE=1 → 呼叫 flash_rewrite_model（小型廉價模型）將使用者發話
       收成成向量庫用的 query JSON。
     FLASH_REWRITE_DUAL_SEARCH=1（預設）→ 原文與收成後 query 並行各搜一次，
       結果依 mongo_id 去重合併（牆鐘時間 ≈ max(原文搜, 收成+收成搜)）。
     FLASH_REWRITE_DUAL_SEARCH=0 → 只搜收成後 query 一次（更省算力但不提供原文備援）。

  3. 工具執行
     flash_run_tools 呼叫 search_stock_news，結果放入 retrieved_data。

  4. Analyst 串流
     build_flash_analyst_messages 組裝【完整參考資料】→ flash_analyst_astream 串流輸出。

回傳型別 FlashRetrievalOutcome（dataclass）：
  router_msg, router_trace_steps, retrieved_data,
  router_elapsed, sse_tool_specs, rewrite_message

環境變數一覽（與 README / specifications 一致）：
  FLASH_SKIP_ROUTER, FLASH_RETRIEVAL_TOP_K, FLASH_REF_MAX_BODY_CHARS,
  FLASH_DATE_RANGE_DAYS, FLASH_LLM_QUERY_REWRITE, FLASH_REWRITE_DUAL_SEARCH,
  FLASH_REWRITE_MODEL, FLASH_REWRITE_MAX_COMPLETION_TOKENS,
  FLASH_REWRITE_TIMEOUT_SEC, FLASH_MERGED_RETRIEVE_CAP

注意：一般對話（chat_mode=general）不使用本模組，請見 general_chat.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.backend.agent.chat import (
    flash_analyst_model,
    flash_rewrite_model,
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

def _positive_float_env(name: str, default: float, minimum: float) -> float:
    raw = (os.getenv(name) or "").strip()
    src = raw if raw else str(default)
    try:
        v = float(src)
    except (TypeError, ValueError):
        v = float(default)
    return max(minimum, v)


# FLASH_SKIP_ROUTER：預設略過規劃用 Router LLM（較省時）；設 `0` 啟動 Router。
FLASH_SKIP_ROUTER = _truthy_env("FLASH_SKIP_ROUTER", "1")
FLASH_RETRIEVAL_TOP_K = max(1, _safe_int_env("FLASH_RETRIEVAL_TOP_K", 10))
FLASH_REF_MAX_BODY_CHARS = max(400, _safe_int_env("FLASH_REF_MAX_BODY_CHARS", 2200))
FLASH_DATE_RANGE_DAYS = max(1, _safe_int_env("FLASH_DATE_RANGE_DAYS", 80))
# FLASH_LLM_QUERY_REWRITE：啟用小模型將使用者問句收成「適合向量新聞」的檢索句（獨立於 Router）。
FLASH_LLM_QUERY_REWRITE = _truthy_env("FLASH_LLM_QUERY_REWRITE", "0")
# 與原版問句並行各搜一次並合併去重（牆鐘時間通常接近 max(原版檢索, 改寫+改版檢索)，而非相加）
FLASH_REWRITE_DUAL_SEARCH = _truthy_env("FLASH_REWRITE_DUAL_SEARCH", "1")
FLASH_REWRITE_TIMEOUT_SEC = _positive_float_env("FLASH_REWRITE_TIMEOUT_SEC", 12.0, 0.8)
# 並行合併後最多保留多少則向量段落（再大會拖慢 Analyst 注入）
_FLASH_MERGED_DEFAULT = max(FLASH_RETRIEVAL_TOP_K * 2, 16)
FLASH_MERGED_RETRIEVE_CAP = max(
    FLASH_RETRIEVAL_TOP_K,
    _safe_int_env("FLASH_MERGED_RETRIEVE_CAP", _FLASH_MERGED_DEFAULT),
)

# FLASH_ENABLE_WEB_SEARCH：快捷模式是否並行執行 Tavily 網路搜尋（預設關閉；開啟後每次多約 $0.01）
FLASH_ENABLE_WEB_SEARCH = _truthy_env("FLASH_ENABLE_WEB_SEARCH", "0")

FLASH_TOOL_NAMES: Tuple[str, ...] = ("search_stock_news",)

_FLASH_REWRITE_SYSTEM = (
    "你是台股市場新聞向量檢索的前處理器。將使用者的發話濃縮成一條檢索用 query。\n"
    "規範：\n"
    "1）保留標的與時間線索：股號如（2330）、公司名、「OO-KY」、產業關鍵字；移除寒暄。\n"
    "2）必要時補上用於檢索的正式名稱或常見中英同義詞，勿杜撰數字或未出現的交易價位。\n"
    "3）只輸出**一個** JSON 物件，勿 markdown fences，無多餘說明。鍵：`query`（字串）。\n"
    "範例：{\"query\":\"台積電 2330 法說會 展望\"}"
)


@dataclass
class FlashRetrievalOutcome:
    """快捷模式：Router（可省略）＋檢索（含可選問句 LLM 改寫／並行）之結果."""

    router_msg: AIMessage
    router_trace_steps: List[Dict[str, Any]]
    retrieved_data: List[Dict[str, Any]]
    router_elapsed: float
    sse_tool_specs: List[Dict[str, Any]]
    rewrite_message: Optional[AIMessage] = None


def _stringify_lc_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _parse_rewrite_query_json(text: str) -> Optional[str]:
    """從 rewrite LLM 回傳中提取 `query` 字串。"""
    stripped = (text or "").strip()
    if not stripped:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped, re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1).strip()
    brace_start = stripped.find("{")
    brace_end = stripped.rfind("}")
    if brace_start < 0 or brace_end <= brace_start:
        return None
    inner = stripped[brace_start : brace_end + 1]
    try:
        obj = json.loads(inner)
    except json.JSONDecodeError:
        line = stripped.splitlines()[0].strip()
        return line[:500] if line else None
    if not isinstance(obj, dict):
        return None
    q = obj.get("query") or obj.get("q") or ""
    if not isinstance(q, str):
        return None
    q = q.strip()
    return q[:500] if q else None


def _rewrite_search_query_from_ai_message(ai: AIMessage) -> Optional[str]:
    txt = _stringify_lc_message_content(getattr(ai, "content", ""))
    return _parse_rewrite_query_json(txt)


def _merge_news_retrieved(
    primary: List[Dict[str, Any]],
    secondary: List[Dict[str, Any]],
    cap: int,
) -> List[Dict[str, Any]]:
    """
    將兩次 news 向量召回合併，依 mongo_id 去重並保留順序：
    先 primary、再穿插 secondary 中未曾出現的 mongo_id。
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()

    def _key(it: Dict[str, Any]) -> Tuple[str, Any]:
        mid = it.get("mongo_id")
        if mid is not None and str(mid).strip():
            return ("id", str(mid))
        return ("tit", str(it.get("title", "")), str(it.get("publishAt", "")))

    for lst in (primary, secondary):
        for it in lst:
            k = _key(it)
            if k in seen:
                continue
            seen.add(k)
            merged.append(it)
            if len(merged) >= cap:
                return merged
    return merged


def _sse_tool_specs_from_normalized_batches(
    batches: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for batch in batches:
        for tc in batch:
            out.append({"name": tc["name"], "args": dict(tc.get("args") or {})})
    return out


async def _flash_invoke_query_rewrite_llm(raw_user_query: str) -> AIMessage:
    uq = (raw_user_query or "").strip()
    return await flash_rewrite_model.ainvoke([
        SystemMessage(content=_FLASH_REWRITE_SYSTEM),
        HumanMessage(content=uq),
    ])


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


async def _flash_run_tavily(user_query: str) -> List[Dict[str, Any]]:
    """快捷模式：呼叫 Tavily 並回傳 retrieved_data 格式列表。失敗時靜默回空列表。"""
    from app.backend.tools.global_search import tavily_search, TavilySearchError
    try:
        result = await tavily_search(user_query)
        return [
            {
                "source_tool": "web",
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "publishAt": r.get("published_date"),
                "score": r.get("score", 0),
            }
            for r in result.get("results", [])
        ]
    except TavilySearchError as e:
        print(f"[Flash/Tavily] 搜尋失敗（靜默略過）: {e}")
        return []
    except Exception as e:
        print(f"[Flash/Tavily] 未預期錯誤（靜默略過）: {e}")
        return []


async def flash_plan_and_retrieve(
    messages_lc: List[Any],
    user_query: str,
) -> FlashRetrievalOutcome:
    """
    Router 規劃（可省略）→ 正規化 `search_stock_news` → 可選問句 LLM 改寫 → call_tools。
    若 FLASH_ENABLE_WEB_SEARCH=1，同時並行呼叫 Tavily 網路搜尋並將結果合併至 retrieved_data。
    """
    router_msg, normalized_orig, trace_step, router_elapsed = await flash_router_phase(
        messages_lc, user_query
    )
    start_iso, end_iso = _default_date_range_iso()
    rewrite_ai: Optional[AIMessage] = None
    rewrite_meta: Dict[str, Any] = {
        "enabled": FLASH_LLM_QUERY_REWRITE,
        "dual_search": False,
        "timeout_sec": FLASH_REWRITE_TIMEOUT_SEC,
        "effective_queries_for_search": [
            ((normalized_orig[0].get("args") or {}).get("query")),
        ],
    }

    # Tavily 任務：在整個 flash 流程最開始就啟動，與後續 news 搜尋並行（牆鐘時間不增加）
    tavily_task: Optional[asyncio.Task] = (
        asyncio.create_task(_flash_run_tavily(user_query))
        if FLASH_ENABLE_WEB_SEARCH else None
    )

    if not FLASH_LLM_QUERY_REWRITE:
        retrieved = await flash_run_tools(normalized_orig)
        tavily_results = (await tavily_task) if tavily_task is not None else []
        retrieved = retrieved + tavily_results
        trace_step.setdefault("extras", {})
        trace_step["extras"]["flash_query_rewrite"] = rewrite_meta | {"pattern": "off"}
        return FlashRetrievalOutcome(
            router_msg=router_msg,
            router_trace_steps=[trace_step],
            retrieved_data=retrieved,
            router_elapsed=router_elapsed,
            sse_tool_specs=_sse_tool_specs_from_normalized_batches([normalized_orig]),
            rewrite_message=None,
        )

    batches_for_sse: List[List[Dict[str, Any]]] = []

    if FLASH_REWRITE_DUAL_SEARCH:
        rewrite_meta["dual_search"] = True
        rew_task = asyncio.create_task(_flash_invoke_query_rewrite_llm(user_query))
        orig_search_task = asyncio.create_task(flash_run_tools(normalized_orig))
        try:
            rewrite_ai = await asyncio.wait_for(
                rew_task,
                timeout=FLASH_REWRITE_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            rewrite_ai = None
        except Exception:
            rewrite_ai = None

        rew_q = _rewrite_search_query_from_ai_message(rewrite_ai) if rewrite_ai else None
        rew_q_eff = rew_q.strip() if rew_q else None
        uq_cmp = (user_query or "").strip()

        normalized_rw: Optional[List[Dict[str, Any]]] = None
        if rewrite_ai is not None and rew_q_eff and rew_q_eff != uq_cmp:
            normalized_rw = normalize_flash_tool_calls(
                router_msg, rew_q_eff, start_iso, end_iso
            )
            rewrite_meta["effective_queries_for_search"].append(
                (normalized_rw[0].get("args") or {}).get("query"))

        rw_tools_task: Optional[asyncio.Task[List[Dict[str, Any]]]] = None
        if normalized_rw is not None:
            rw_tools_task = asyncio.create_task(flash_run_tools(normalized_rw))

        r_orig = await orig_search_task
        r_rw = await rw_tools_task if rw_tools_task is not None else []
        tavily_results = (await tavily_task) if tavily_task is not None else []
        retrieved = _merge_news_retrieved(r_orig, r_rw, FLASH_MERGED_RETRIEVE_CAP) + tavily_results

        if normalized_rw is not None:
            batches_for_sse = [normalized_orig, normalized_rw]
        else:
            batches_for_sse = [normalized_orig]

        rewrite_meta["pattern"] = "parallel_dual"
        rewrite_meta["rewrite_ok"] = rewrite_ai is not None
        rewrite_meta["rewrite_applied_second_search"] = normalized_rw is not None
    else:
        try:
            rewrite_ai = await asyncio.wait_for(
                _flash_invoke_query_rewrite_llm(user_query),
                timeout=FLASH_REWRITE_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            rewrite_ai = None
        except Exception:
            rewrite_ai = None

        rew_q = _rewrite_search_query_from_ai_message(rewrite_ai) if rewrite_ai else None
        q_eff = (rew_q.strip() if rew_q else None) or (user_query or "").strip() or "股市 概況"

        normalized_eff = normalize_flash_tool_calls(
            router_msg, q_eff, start_iso, end_iso
        )
        rewrite_meta["effective_queries_for_search"] = [
            (normalized_eff[0].get("args") or {}).get("query"),
        ]
        news_retrieved = await flash_run_tools(normalized_eff)
        tavily_results = (await tavily_task) if tavily_task is not None else []
        retrieved = news_retrieved + tavily_results
        batches_for_sse = [normalized_eff]
        rewrite_meta["pattern"] = "sequential_rewrite"
        rewrite_meta["rewrite_ok"] = rewrite_ai is not None
        rewrite_meta["rewrite_applied_second_search"] = False

    trace_step.setdefault("extras", {})
    trace_step["extras"]["flash_query_rewrite"] = rewrite_meta

    return FlashRetrievalOutcome(
        router_msg=router_msg,
        router_trace_steps=[trace_step],
        retrieved_data=retrieved,
        router_elapsed=router_elapsed,
        sse_tool_specs=_sse_tool_specs_from_normalized_batches(batches_for_sse),
        rewrite_message=rewrite_ai,
    )


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

