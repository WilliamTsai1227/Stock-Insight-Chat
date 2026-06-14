"""
每月 LLM token 配額：Pre-flight 與原子遞增。

- 上限來自 `subscription_tiers.monthly_token_limit`（JOIN `users`）。
- `tier_id` 為 NULL 時視同 free，使用 `DEFAULT_FALLBACK_MONTHLY_LIMIT`（與種子 free 列一致）。
- Pre-flight：僅在 `used_tokens >= monthly_limit` 時阻擋；允許最後一輪請求略超上限。
- 原子遞增：一律累加實際用量（可超過上限），下次 preflight 再擋。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from app.backend.database.postgresql import get_pool

# 與 init_db 種子 `free` 列一致；無 tier 時 fallback
DEFAULT_FALLBACK_MONTHLY_LIMIT = 200_000

# 配額週期長度（自 current_period_start 起算）
QUOTA_RESET_PERIOD_DAYS = 30


class QuotaStatus(NamedTuple):
    used_tokens: int
    monthly_limit: int
    tier_name: Optional[str]
    current_period_start: Optional[datetime]


def compute_quota_resets_at(
    period_start: Optional[datetime],
) -> Optional[datetime]:
    """自週期起始日加上 QUOTA_RESET_PERIOD_DAYS，回傳下次重置時間。"""
    if period_start is None:
        return None
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=timezone.utc)
    return period_start + timedelta(days=QUOTA_RESET_PERIOD_DAYS)


def quota_exceeded_detail(
    used: int,
    limit: int,
    period_start: Optional[datetime] = None,
) -> dict[str, Any]:
    """HTTP 429 / SSE 用的結構化配額錯誤（前端依此渲染中文）。"""
    resets_at = compute_quota_resets_at(period_start)
    payload: dict[str, Any] = {
        "code": "quota_exceeded",
        "used_tokens": used,
        "monthly_token_limit": limit,
    }
    if period_start is not None:
        payload["current_period_start"] = period_start.isoformat()
    if resets_at is not None:
        payload["quota_resets_at"] = resets_at.isoformat()
    return payload


async def ensure_quota_row_exists(user_id: UUID) -> None:
    """舊帳號可能尚無 `user_usage_quotas` 列時補上一筆（與註冊流程一致）。"""
    async with get_pool().acquire() as conn:
        await ensure_quota_row_on_conn(conn, user_id)


async def ensure_quota_row_on_conn(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
        VALUES ($1, date_trunc('month', NOW() AT TIME ZONE 'UTC'), 0)
        ON CONFLICT (user_id) DO NOTHING
        """,
        user_id,
    )


async def fetch_quota_status_on_conn(
    conn: asyncpg.Connection,
    user_id: UUID,
) -> QuotaStatus:
    """在既有 transaction / connection 上讀取配額（與 fetch_quota_status 邏輯相同）。"""
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(q.used_tokens, 0)::bigint AS used_tokens,
            COALESCE(st.monthly_token_limit, $2::bigint)::bigint AS monthly_limit,
            st.name AS tier_name,
            q.current_period_start
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
        row["current_period_start"],
    )


async def fetch_quota_status(user_id: UUID) -> QuotaStatus:
    """讀取目前用量與當月上限（含 tier 名稱供除錯／日誌）。"""
    async with get_pool().acquire() as conn:
        return await fetch_quota_status_on_conn(conn, user_id)


async def assert_preflight_llm_quota(user_id: UUID) -> None:
    """
    發 LLM / 進 LangGraph 前呼叫：已達或超過當月上限則 HTTP 429。
    未達上限時允許請求完成（即使本輪 token 會略超上限）。
    """
    await ensure_quota_row_exists(user_id)
    used, limit, _, period_start = await fetch_quota_status(user_id)
    if used >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=quota_exceeded_detail(used, limit, period_start),
        )


async def increment_used_tokens(
    conn: asyncpg.Connection,
    user_id: UUID,
    delta: int,
) -> Optional[int]:
    """
    無條件遞增 used_tokens（允許單次請求略超 monthly_limit）。
    須在已開啟的 transaction 內呼叫；成功回傳更新後的 used_tokens。
    """
    if delta <= 0:
        return None
    row = await conn.fetchrow(
        """
        UPDATE user_usage_quotas q
        SET
            used_tokens = q.used_tokens + $2,
            updated_at = NOW()
        WHERE q.user_id = $1
        RETURNING q.used_tokens
        """,
        user_id,
        delta,
    )
    return int(row["used_tokens"]) if row else None
