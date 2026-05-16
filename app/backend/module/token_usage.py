"""
LLM token 用量：從 LangChain / OpenAI 事件輸出解析 usage，並寫入 PostgreSQL。

- user_usage_quotas：週期內累計 used_tokens
- token_usage_logs：每次 LLM `on_chat_model_end` 一列（對帳／估價）；同一對話可多列

除錯：環境變數 **TOKEN_PARSE_DEBUG**
- `1` / `true` / `all`：每次 `on_chat_model_end` 印 `log_token_usage_parse_shape`（結構摘要，不含正文）
- `zero`：僅在該輪解析結果 batch 皆為 0 時印
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.backend.database.postgresql import get_pool
from app.backend.module.usage_quota import try_increment_used_tokens

# 費用估算：OpenAI Platform「Text / Standard」每 1M tokens（input / output）
# 對照 https://platform.openai.com/pricing ；實際帳單以帳戶為準。
# 順序重要：`estimate_cost_usd` 以第一個 prefix 命中為準，`gpt-5-mini-*` 須排在 `gpt-5` 之前。
TOKEN_COST_TABLE: Dict[str, Dict[str, float]] = {
    "gpt-5.5-pro": {"prompt": 30.00, "completion": 180.00},
    "gpt-5.5": {"prompt": 5.00, "completion": 30.00},
    "gpt-5.4-pro": {"prompt": 30.00, "completion": 180.00},
    "gpt-5.4-mini": {"prompt": 0.75, "completion": 4.50},
    "gpt-5.4-nano": {"prompt": 0.20, "completion": 1.25},
    "gpt-5.4": {"prompt": 2.50, "completion": 15.00},
    "gpt-5.2-pro": {"prompt": 21.00, "completion": 168.00},
    "gpt-5.2": {"prompt": 1.75, "completion": 14.00},
    "gpt-5.1": {"prompt": 1.25, "completion": 10.00},
    "gpt-5-pro": {"prompt": 15.00, "completion": 120.00},
    "gpt-5-nano": {"prompt": 0.05, "completion": 0.40},
    "gpt-5-mini": {"prompt": 0.25, "completion": 2.00},
    "gpt-5": {"prompt": 1.25, "completion": 10.00},
    "gpt-4.1-nano": {"prompt": 0.10, "completion": 0.40},
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60},
    "gpt-4.1": {"prompt": 2.00, "completion": 8.00},
    "gpt-4o-2024-05-13": {"prompt": 5.00, "completion": 15.00},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "o1-pro": {"prompt": 150.00, "completion": 600.00},
    "o3-pro": {"prompt": 20.00, "completion": 80.00},
    "o4-mini": {"prompt": 1.10, "completion": 4.40},
    "o3-mini": {"prompt": 1.10, "completion": 4.40},
    "o1-mini": {"prompt": 1.10, "completion": 4.40},
    "o1": {"prompt": 15.00, "completion": 60.00},
    "o3": {"prompt": 2.00, "completion": 8.00},
}


def _first_generation_from_outputs(generations: Any) -> Any:
    """
    LangChain 結尾事件可能是：
    - **ChatResult**：`generations` 為 `List[ChatGeneration]`（單層）
    - **LLMResult**（含 `BaseChatModel.astream` → `on_llm_end`）：`generations` 為 `List[List[Generation]]`（批次外層）
    取不到時回傳 None。
    """
    if not generations:
        return None
    head = generations[0]
    if isinstance(head, list):
        return head[0] if head else None
    return head


def extract_model_label_from_lc_output(output: Any) -> Optional[str]:
    """
    從 `on_chat_model_end` 的 LangChain 輸出推斷 model 字串（供 token_usage_logs）。
    外層 ChatResult/LLMResult 常沒有 `response_metadata`，要從 generations 內層讀。
    """
    if output is None:
        return None

    def _pick(s: Any) -> Optional[str]:
        if isinstance(s, str) and s.strip():
            return s.strip()
        return None

    llmo = getattr(output, "llm_output", None)
    if llmo is None and isinstance(output, dict):
        llmo = output.get("llm_output")
    if isinstance(llmo, dict):
        m = _pick(llmo.get("model_name"))
        if m:
            return m

    rm_top = getattr(output, "response_metadata", None)
    if rm_top is None and isinstance(output, dict):
        rm_top = output.get("response_metadata")
    if isinstance(rm_top, dict):
        m = _pick(rm_top.get("model_name"))
        if m:
            return m

    gens = getattr(output, "generations", None)
    if gens is None and isinstance(output, dict):
        gens = output.get("generations")
    if not gens:
        return None
    g0 = _first_generation_from_outputs(gens)
    if g0 is None:
        return None

    gi = getattr(g0, "generation_info", None)
    if gi is None and isinstance(g0, dict):
        gi = g0.get("generation_info")
    if isinstance(gi, dict):
        m = _pick(gi.get("model_name"))
        if m:
            return m

    msg_obj = getattr(g0, "message", None)
    if isinstance(g0, dict) and msg_obj is None:
        msg_obj = g0.get("message")
    if isinstance(msg_obj, dict):
        rm = msg_obj.get("response_metadata")
        if isinstance(rm, dict):
            m = _pick(rm.get("model_name"))
            if m:
                return m
    elif msg_obj is not None:
        rm = getattr(msg_obj, "response_metadata", None)
        if isinstance(rm, dict):
            m = _pick(rm.get("model_name"))
            if m:
                return m
    return None


def _token_parse_debug_mode() -> str:
    return os.environ.get("TOKEN_PARSE_DEBUG", "").strip().lower()


def _should_log_token_shape(batch_p: int, batch_c: int) -> bool:
    mode = _token_parse_debug_mode()
    if mode in ("", "0", "false", "no"):
        return False
    if mode in ("1", "true", "yes", "all"):
        return True
    if mode == "zero":
        return batch_p == 0 and batch_c == 0
    return False


def _safe_token_usage_preview(tu: Any) -> str:
    if tu is None:
        return "null"
    if isinstance(tu, dict):
        slim = {
            k: tu.get(k)
            for k in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens",
                "output_tokens",
            )
            if k in tu
        }
        return json.dumps(slim, ensure_ascii=False)
    return type(tu).__name__


def _summarize_lc_output(output: Any) -> str:
    """單行可讀摘要，不包含 message 正文。"""
    if output is None:
        return "output=None"
    cls = f"{type(output).__module__}.{type(output).__name__}"
    parts: List[str] = [f"cls={cls}"]

    if isinstance(output, dict):
        parts.append(f"dict_keys={list(output.keys())[:20]}")

    gens = getattr(output, "generations", None)
    if gens is None and isinstance(output, dict):
        gens = output.get("generations")
    if gens is not None:
        parts.append(f"gens_outer_len={len(gens)}")
        if gens:
            h0 = gens[0]
            parts.append(f"gens[0]_type={type(h0).__name__}")
            if isinstance(h0, list):
                parts.append(f"gens[0]_inner_len={len(h0)}")
                inner0 = h0[0] if h0 else None
                parts.append(f"gens[0][0]_type={type(inner0).__name__ if inner0 is not None else 'None'}")
                g_pick = inner0
            else:
                g_pick = h0

            gi = getattr(g_pick, "generation_info", None) if g_pick is not None else None
            if gi is None and isinstance(g_pick, dict):
                gi = g_pick.get("generation_info")
            if isinstance(gi, dict):
                parts.append(f"gen_info_keys={list(gi.keys())[:15]}")
                if "token_usage" in gi:
                    parts.append(f"gen_info.token_usage={_safe_token_usage_preview(gi.get('token_usage'))}")

            msg_obj = getattr(g_pick, "message", None) if g_pick is not None else None
            if isinstance(g_pick, dict) and msg_obj is None:
                msg_obj = g_pick.get("message")
            if isinstance(msg_obj, dict):
                parts.append(f"msg_dict_keys={list(msg_obj.keys())[:15]}")
                rm = msg_obj.get("response_metadata")
                parts.append(f"msg.response_meta_keys={list(rm.keys()) if isinstance(rm, dict) else type(rm).__name__}")
                if isinstance(rm, dict) and rm.get("token_usage") is not None:
                    parts.append(f"msg.token_usage={_safe_token_usage_preview(rm.get('token_usage'))}")
            elif msg_obj is not None:
                parts.append(f"msg_cls={type(msg_obj).__name__}")
                rm = getattr(msg_obj, "response_metadata", None)
                if isinstance(rm, dict):
                    parts.append(f"msg.response_meta_keys={list(rm.keys())[:15]}")
                    tu = rm.get("token_usage")
                    if tu is not None:
                        parts.append(f"msg.token_usage={_safe_token_usage_preview(tu)}")
                um = getattr(msg_obj, "usage_metadata", None)
                if um is not None:
                    parts.append(f"msg.usage_meta={type(um).__name__}")

    llmo = getattr(output, "llm_output", None)
    if llmo is None and isinstance(output, dict):
        llmo = output.get("llm_output")
    if isinstance(llmo, dict):
        parts.append(f"llm_output_keys={list(llmo.keys())[:15]}")
        if llmo.get("token_usage") is not None:
            parts.append(f"llm_output.token_usage={_safe_token_usage_preview(llmo.get('token_usage'))}")
        if llmo.get("model_name"):
            parts.append(f"llm_output.model_name={llmo.get('model_name')!r}")

    resp_top = getattr(output, "response_metadata", None)
    if isinstance(resp_top, dict):
        parts.append(f"top.response_meta_keys={list(resp_top.keys())[:15]}")

    return " | ".join(parts)


def log_token_usage_parse_shape(
    *,
    event_name: str,
    batch_p: int,
    batch_c: int,
    model_label: str,
    output: Any,
    tags: Optional[Any] = None,
    meta_keys: Optional[List[str]] = None,
) -> None:
    """
    在 api/chat.py 的 `on_chat_model_end` 呼叫。
    需設定 TOKEN_PARSE_DEBUG=1（全印）或 zero（僅批次為 0 時）。
    """
    if not _should_log_token_shape(batch_p, batch_c):
        return
    extras = ""
    if tags:
        extras += f" tags={tags!r}"
    if meta_keys:
        extras += f" metadata_keys={meta_keys!r}"
    try:
        summary = _summarize_lc_output(output)
        print(
            f"[TOKEN-PARSE-DEBUG] run={event_name!r} model_hint={model_label!r} "
            f"parsed_batch_p={batch_p} parsed_batch_c={batch_c}{extras} | {summary}",
            flush=True,
        )
    except Exception as e:
        print(
            f"[TOKEN-PARSE-DEBUG] run={event_name!r}{extras} summarize_failed "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


def parse_usage_from_llm_message(output: Any) -> Tuple[int, int]:
    """
    自 `on_chat_model_end` 取本輪 prompt / completion tokens。

    - `astream_events` 常送出 **ChatResult**（含 `generations`、`llm_output`），不一定是 AIMessage。
    - **LLMResult** 的 `generations` 為巢狀 list（先解一層再讀 `.message`），否則會永遠得到 0。
    - AIMessage／chunk 類路徑可見 `usage_metadata`；OpenAI Chat 也常把數字放在 `response_metadata["token_usage"]`。
    """
    if output is None:
        return 0, 0

    def _from_usage_obj(usage_obj: Any) -> Optional[Tuple[int, int]]:
        if usage_obj is None:
            return None
        inp = outp = None
        for pk, ck in (
            ("input_tokens", "output_tokens"),
            ("prompt_tokens", "completion_tokens"),
        ):
            if isinstance(usage_obj, dict):
                inp, outp = usage_obj.get(pk), usage_obj.get(ck)
            else:
                inp, outp = getattr(usage_obj, pk, None), getattr(usage_obj, ck, None)
            if inp is not None or outp is not None:
                break
        if inp is None and outp is None:
            return None
        try:
            return int(inp or 0), int(outp or 0)
        except (TypeError, ValueError):
            return None

    um = getattr(output, "usage_metadata", None)
    if um is None and isinstance(output, dict):
        um = output.get("usage_metadata")
    counted = _from_usage_obj(um)
    if counted is not None:
        return counted

    resp_meta = getattr(output, "response_metadata", None)
    if resp_meta is None and isinstance(output, dict):
        resp_meta = output.get("response_metadata")
    if isinstance(resp_meta, dict):
        tu = resp_meta.get("token_usage")
        if isinstance(tu, dict):
            try:
                p = int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0)
                c = int(tu.get("completion_tokens") or tu.get("output_tokens") or 0)
                return p, c
            except (TypeError, ValueError):
                pass

    llm_output = getattr(output, "llm_output", None)
    if llm_output is None and isinstance(output, dict):
        llm_output = output.get("llm_output")
    if isinstance(llm_output, dict):
        tu = llm_output.get("token_usage")
        counted = _from_usage_obj(tu)
        if counted is not None:
            return counted

    gens = getattr(output, "generations", None)
    if gens is None and isinstance(output, dict):
        gens = output.get("generations")
    if gens:
        g0 = _first_generation_from_outputs(gens)
        msg_obj = getattr(g0, "message", None)
        if isinstance(g0, dict) and msg_obj is None:
            msg_obj = g0.get("message")
        p2, c2 = parse_usage_from_llm_message(msg_obj)
        if p2 or c2:
            return p2, c2

        gi = getattr(g0, "generation_info", None)
        if gi is None and isinstance(g0, dict):
            gi = g0.get("generation_info")
        if isinstance(gi, dict):
            tu_gi = gi.get("token_usage")
            counted_gi = _from_usage_obj(tu_gi)
            if counted_gi is not None:
                return counted_gi

    return 0, 0


def estimate_cost_usd(model: str, prompt_tok: int, completion_tok: int) -> float:
    """估算 USD 費用（以百萬 token 計）。未知 model 回 0.0。"""
    rates = None
    for key, r in TOKEN_COST_TABLE.items():
        if model.startswith(key):
            rates = r
            break
    if rates is None:
        return 0.0
    return (prompt_tok * rates["prompt"] + completion_tok * rates["completion"]) / 1_000_000


async def attach_token_usage_logs_to_message(
    user_id: UUID,
    message_id: UUID,
    log_ids: List[UUID],
) -> None:
    """
    將本輪對話已 INSERT 之多筆 token_usage_logs 繫結至同一則 assistant message。
    """
    if not log_ids:
        return
    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE token_usage_logs
                SET message_id = $1
                WHERE user_id = $2 AND id = ANY($3::uuid[])
                """,
                message_id,
                user_id,
                log_ids,
            )
    except Exception as e:
        print(
            f"[TOKEN] attach_token_usage_logs_to_message failed user={user_id}: "
            f"{type(e).__name__}: {e}"
        )


async def record_token_usage(
    user_id: UUID,
    chat_id: UUID,
    message_id: Optional[UUID],
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    caller: Optional[str] = None,
) -> Optional[UUID]:
    """
    將單次 LLM 結算（一輪）的 token 用量寫入兩張表（同一 transaction）：
      1. user_usage_quotas  ── 條件式遞增（不超過 subscription_tiers.monthly_token_limit）
      2. token_usage_logs   ── INSERT 一列流水帳（對帳／費用報表）

    若配額不足，不配額、不寫 log，回傳 None。

    caller：建議填 router／analyst等（見 api/chat.py `on_chat_model_end` 推斷）。
    成功時回傳 `token_usage_logs.id`，跳過或失敗時回傳 None。

    - 從 pool 取新連線，不依賴 Depends 注入的連線（StreamingResponse 生命週期已結束）
    - 任何 DB 失敗只印 log，不丟例外（token 統計失敗不該打斷已完成的串流）
    """
    if prompt_tokens == 0 and completion_tokens == 0:
        print(
            f"[TOKEN] skip record (no usage from LLM events) "
            f"user={user_id} chat={chat_id}"
        )
        return None

    total = prompt_tokens + completion_tokens
    cost = estimate_cost_usd(model_name, prompt_tokens, completion_tokens)

    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                ok = await try_increment_used_tokens(conn, user_id, total)
                if not ok:
                    print(
                        f"[QUOTA] record_token_usage skipped (over limit or no quota row) "
                        f"user={user_id} chat={chat_id} delta={total}"
                    )
                    return None

                row = await conn.fetchrow(
                    """
                    INSERT INTO token_usage_logs
                        (user_id, chat_id, message_id, caller, model_name,
                         prompt_tokens, completion_tokens, total_tokens, cost_usd)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id
                    """,
                    user_id,
                    chat_id,
                    message_id,
                    caller,
                    model_name,
                    prompt_tokens,
                    completion_tokens,
                    total,
                    cost,
                )
            log_id = row["id"] if row else None
        cc = caller or ""
        print(
            f"[TOKEN] user={user_id} chat={chat_id} caller={cc!r} model={model_name} "
            f"prompt={prompt_tokens} completion={completion_tokens} "
            f"total={total} cost=${cost:.6f}"
        )
        return log_id
    except Exception as e:
        print(f"[TOKEN] record_token_usage failed, user={user_id}: {type(e).__name__}: {e}")
        return None
