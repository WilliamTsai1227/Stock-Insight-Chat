"""
LLM token 用量：從 LangChain / OpenAI 事件輸出解析 usage，並寫入 PostgreSQL。

- user_usage_quotas：週期內累計 used_tokens
- token_usage_logs：單次對話流水（對帳 / 估價）
"""

from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from app.backend.database.postgresql import get_pool

# 費用估算常數（USD per 1M tokens，可依 OpenAI 定價調整）
TOKEN_COST_TABLE: Dict[str, Dict[str, float]] = {
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4o": {"prompt": 5.00, "completion": 15.00},
    "gpt-5-mini": {"prompt": 0.40, "completion": 1.60},
    "gpt-5": {"prompt": 10.00, "completion": 30.00},
}


def parse_usage_from_llm_message(output: Any) -> Tuple[int, int]:
    """
    自 `on_chat_model_end` 取本輪 prompt / completion tokens。

    - `astream_events` 常送出 **ChatResult**（含 `generations`、`llm_output`），不一定是 AIMessage。
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
        g0 = gens[0]
        msg_obj = getattr(g0, "message", None)
        if isinstance(g0, dict) and msg_obj is None:
            msg_obj = g0.get("message")
        p2, c2 = parse_usage_from_llm_message(msg_obj)
        if p2 or c2:
            return p2, c2

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


async def record_token_usage(
    user_id: UUID,
    chat_id: UUID,
    message_id: Optional[UUID],
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """
    在 Agent 背景任務結束後，一次性將 token 用量寫入兩張表（同一 transaction）：
      1. user_usage_quotas  ── UPSERT 累計計數器（同週期內只有一列，O(1) 查詢）
      2. token_usage_logs   ── INSERT 流水帳（對帳 / 費用報表）

    - 從 pool 取新連線，不依賴 Depends 注入的連線（StreamingResponse 生命週期已結束）
    - 任何 DB 失敗只印 log，不丟例外（token 統計失敗不該打斷已完成的串流）
    """
    if prompt_tokens == 0 and completion_tokens == 0:
        print(
            f"[TOKEN] skip record (no usage from LLM events) "
            f"user={user_id} chat={chat_id}"
        )
        return

    total = prompt_tokens + completion_tokens
    cost = estimate_cost_usd(model_name, prompt_tokens, completion_tokens)

    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens, updated_at)
                    VALUES ($1, date_trunc('month', NOW()), $2, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                        SET used_tokens = user_usage_quotas.used_tokens + EXCLUDED.used_tokens,
                            updated_at  = NOW()
                    """,
                    user_id,
                    total,
                )

                await conn.execute(
                    """
                    INSERT INTO token_usage_logs
                        (user_id, chat_id, message_id, model_name,
                         prompt_tokens, completion_tokens, total_tokens, cost_usd)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    user_id,
                    chat_id,
                    message_id,
                    model_name,
                    prompt_tokens,
                    completion_tokens,
                    total,
                    cost,
                )
        print(
            f"[TOKEN] user={user_id} chat={chat_id} model={model_name} "
            f"prompt={prompt_tokens} completion={completion_tokens} "
            f"total={total} cost=${cost:.6f}"
        )
    except Exception as e:
        print(f"[TOKEN] record_token_usage failed, user={user_id}: {type(e).__name__}: {e}")
