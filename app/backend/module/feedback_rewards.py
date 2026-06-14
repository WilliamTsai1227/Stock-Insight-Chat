"""
建議回饋 Token 獎勵：每日次數上限與配額回補。

規則（可經 .env 調整）：
- 每位使用者每曆日（預設 Asia/Taipei）最多提交 FEEDBACK_DAILY_MAX 次
- 每次成功提交發放 FEEDBACK_TOKEN_REWARD tokens（從 used_tokens 扣除，下限 0）
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException, status

from app.backend.module.usage_quota import (
    ensure_quota_row_on_conn,
    fetch_quota_status_on_conn,
)

FEEDBACK_DAILY_MAX = int(os.getenv("FEEDBACK_DAILY_MAX", "3"))
FEEDBACK_TOKEN_REWARD = int(os.getenv("FEEDBACK_TOKEN_REWARD", "2500"))
FEEDBACK_DAILY_TIMEZONE = os.getenv("FEEDBACK_DAILY_TIMEZONE", "Asia/Taipei")


def public_feedback_reward_config() -> dict[str, Any]:
    return {
        "token_reward": FEEDBACK_TOKEN_REWARD,
        "daily_max": FEEDBACK_DAILY_MAX,
        "daily_timezone": FEEDBACK_DAILY_TIMEZONE,
    }


async def count_feedback_submissions_today(
    conn: asyncpg.Connection,
    user_id: UUID,
) -> int:
    """統計使用者於「當地曆日」已提交的回饋次數。"""
    return int(
        await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM user_feedback
            WHERE user_id = $1
              AND (created_at AT TIME ZONE $2)::date
                  = (NOW() AT TIME ZONE $2)::date
            """,
            user_id,
            FEEDBACK_DAILY_TIMEZONE,
        )
        or 0
    )


async def get_feedback_eligibility(
    conn: asyncpg.Connection,
    user_id: UUID,
) -> dict[str, Any]:
    submissions_today = await count_feedback_submissions_today(conn, user_id)
    remaining = max(0, FEEDBACK_DAILY_MAX - submissions_today)
    return {
        "submissions_today": submissions_today,
        "remaining_today": remaining,
        "daily_max": FEEDBACK_DAILY_MAX,
        "token_reward": FEEDBACK_TOKEN_REWARD,
        "daily_timezone": FEEDBACK_DAILY_TIMEZONE,
        "can_submit_today": remaining > 0,
    }


async def assert_daily_feedback_limit(
    conn: asyncpg.Connection,
    user_id: UUID,
) -> None:
    submissions_today = await count_feedback_submissions_today(conn, user_id)
    if submissions_today >= FEEDBACK_DAILY_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "feedback_daily_limit",
                "message": (
                    f"今日建議回饋次數已達上限（{FEEDBACK_DAILY_MAX} 次），請明天再試。"
                ),
                "submissions_today": submissions_today,
                "daily_max": FEEDBACK_DAILY_MAX,
            },
        )


async def apply_feedback_token_reward(
    conn: asyncpg.Connection,
    user_id: UUID,
    reward: int = FEEDBACK_TOKEN_REWARD,
) -> int:
    """發放 Token 獎勵：減少 used_tokens（等同增加可用額度）。"""
    if reward <= 0:
        raise ValueError("reward must be positive")

    await ensure_quota_row_on_conn(conn, user_id)
    row = await conn.fetchrow(
        """
        UPDATE user_usage_quotas
        SET
            used_tokens = GREATEST(0, used_tokens - $2),
            updated_at = NOW()
        WHERE user_id = $1
        RETURNING used_tokens
        """,
        user_id,
        reward,
    )
    if row is None:
        raise RuntimeError(f"user_usage_quotas row missing for user_id={user_id}")
    return int(row["used_tokens"])


async def build_feedback_success_payload(
    conn: asyncpg.Connection,
    user_id: UUID,
    *,
    feedback_id: str,
    feedback_status: str,
    created_at: str,
    tokens_granted: int,
) -> dict[str, Any]:
    q = await fetch_quota_status_on_conn(conn, user_id)
    remaining = max(0, q.monthly_limit - q.used_tokens)
    eligibility = await get_feedback_eligibility(conn, user_id)
    return {
        "id": feedback_id,
        "status": feedback_status,
        "created_at": created_at,
        "tokens_granted": tokens_granted,
        "used_tokens": q.used_tokens,
        "monthly_token_limit": q.monthly_limit,
        "remaining_tokens": remaining,
        "quota_exhausted": q.used_tokens >= q.monthly_limit,
        "submissions_today": eligibility["submissions_today"],
        "remaining_today": eligibility["remaining_today"],
        "daily_max": eligibility["daily_max"],
    }
