"""
每月 LLM token 配額：Pre-flight 與原子遞增。

- 上限來自 `subscription_tiers.monthly_token_limit`（JOIN `users`）。
- `tier_id` 為 NULL 時視同 free，使用 `DEFAULT_FALLBACK_MONTHLY_LIMIT`（與種子 free 列一致）。
- Pre-flight：進 LangGraph / 送 OpenAI 前阻擋已達上限。
- 原子遞增：`record_token_usage` 內與流水帳同一 transaction，避免「只寫 log 未扣額」或超額累加。
"""

from __future__ import annotations

from typing import NamedTuple, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from app.backend.database.postgresql import get_pool

# 與 init_db 種子 `free` 列一致；無 tier 時 fallback
DEFAULT_FALLBACK_MONTHLY_LIMIT = 200_000


class QuotaStatus(NamedTuple):
    used_tokens: int
    monthly_limit: int
    tier_name: Optional[str]


async def ensure_quota_row_exists(user_id: UUID) -> None:
    """舊帳號可能尚無 `user_usage_quotas` 列時補上一筆（與註冊流程一致）。"""
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
            VALUES ($1, date_trunc('month', NOW() AT TIME ZONE 'UTC'), 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )


async def fetch_quota_status(user_id: UUID) -> QuotaStatus:
    """讀取目前用量與當月上限（含 tier 名稱供除錯／日誌）。"""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(q.used_tokens, 0)::bigint AS used_tokens,
                COALESCE(st.monthly_token_limit, $2::bigint)::bigint AS monthly_limit,
                st.name AS tier_name
            FROM users u
            LEFT JOIN subscription_tiers st ON st.id = u.tier_id
            LEFT JOIN user_usage_quotas q ON q.user_id = u.id
            WHERE u.id = $1
            """,
            user_id,
            DEFAULT_FALLBACK_MONTHLY_LIMIT,
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return QuotaStatus(
        int(row["used_tokens"]),
        int(row["monthly_limit"]),
        row["tier_name"],
    )


async def assert_preflight_llm_quota(user_id: UUID) -> None:
    """
    發 LLM / 進 LangGraph 前呼叫：已達或超過當月上限則 HTTP 429。
    """
    await ensure_quota_row_exists(user_id)
    used, limit, _ = await fetch_quota_status(user_id)
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Monthly token quota exceeded ({used}/{limit}). "
                "Upgrade your plan or wait until the next billing period."
            ),
        )


async def try_increment_used_tokens(
    conn: asyncpg.Connection,
    user_id: UUID,
    delta: int,
) -> bool:
    """
    僅當 `used_tokens + delta <= monthly_limit` 時遞增配額。
    須在已開啟的 transaction 內呼叫；成功回傳 True。
    """
    if delta <= 0:
        return True
    row = await conn.fetchrow(
        """
        UPDATE user_usage_quotas q
        SET
            used_tokens = q.used_tokens + $2,
            updated_at = NOW()
        FROM users u
        LEFT JOIN subscription_tiers st ON st.id = u.tier_id
        WHERE q.user_id = u.id
          AND u.id = $1
          AND q.used_tokens + $2 <= COALESCE(st.monthly_token_limit, $3::bigint)
        RETURNING q.used_tokens
        """,
        user_id,
        delta,
        DEFAULT_FALLBACK_MONTHLY_LIMIT,
    )
    return row is not None
