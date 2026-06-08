"""
User Management API (使用者管理接口)
======================================
使用 asyncpg 原生連線操作 PostgreSQL。
asyncpg.Record 的欄位存取語法：record['column_name']

注意：此系統僅支援 Google SSO 登入，無本地密碼功能。
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, timezone

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user
from app.backend.module.usage_quota import (
    DEFAULT_FALLBACK_MONTHLY_LIMIT,
    ensure_quota_row_exists,
    fetch_quota_status,
)

router = APIRouter(tags=["User Management"])


# --- Request/Response Schemas ---

class UserUpdate(BaseModel):
    username: Optional[str] = None

class UserProfile(BaseModel):
    id: str
    email: EmailStr
    username: str
    status: str
    tier_id: Optional[str] = None
    tier_name: str = "free"
    used_tokens: int = 0
    monthly_token_limit: int = 200_000
    remaining_tokens: int = 200_000
    quota_exhausted: bool = False


class UserUsageStats(BaseModel):
    tier_name: str = "free"
    used_tokens: int
    monthly_token_limit: int
    remaining_tokens: int
    usage_percent: int
    quota_exhausted: bool
    current_period_start: Optional[str] = None


def _serialize_user_profile(row: asyncpg.Record, quota: Optional[dict] = None) -> dict:
    """將 users JOIN subscription_tiers 的查詢結果序列化為 API 回應。"""
    tier_name = row["tier_name"] if row["tier_name"] else "free"
    payload = {
        "id": str(row["id"]),
        "email": row["email"],
        "username": row["username"],
        "status": row["status"],
        "tier_id": str(row["tier_id"]) if row["tier_id"] else None,
        "tier_name": tier_name,
    }
    if quota:
        payload.update(quota)
    return payload


_USER_PROFILE_SELECT = """
    SELECT u.id, u.email, u.username, u.status, u.tier_id, st.name AS tier_name
    FROM users u
    LEFT JOIN subscription_tiers st ON st.id = u.tier_id
    WHERE u.id = $1
"""


# --- API Endpoints ---

@router.get("/api/user", response_model=UserProfile)
async def get_my_profile(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    取得目前登入使用者的個人資料
    asyncpg.Record 支援 dict-like 存取
    """
    row = await db.fetchrow(_USER_PROFILE_SELECT, current_user["id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    await ensure_quota_row_exists(current_user["id"])
    q = await fetch_quota_status(current_user["id"])
    remaining = max(0, q.monthly_limit - q.used_tokens)
    quota = {
        "used_tokens": q.used_tokens,
        "monthly_token_limit": q.monthly_limit,
        "remaining_tokens": remaining,
        "quota_exhausted": q.used_tokens >= q.monthly_limit,
    }
    return _serialize_user_profile(row, quota)


@router.patch("/api/user", response_model=UserProfile)
async def update_my_profile(
    update_data: UserUpdate,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user)
):
    """修改個人資料（目前僅支援修改 username）"""
    if update_data.username:
        await db.execute(
            "UPDATE users SET username = $1, updated_at = $2 WHERE id = $3",
            update_data.username,
            datetime.now(timezone.utc),
            current_user["id"]
        )

    # 重新撈取最新資料（含訂閱方案名稱）
    updated = await db.fetchrow(_USER_PROFILE_SELECT, current_user["id"])
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    await ensure_quota_row_exists(current_user["id"])
    q = await fetch_quota_status(current_user["id"])
    remaining = max(0, q.monthly_limit - q.used_tokens)
    quota = {
        "used_tokens": q.used_tokens,
        "monthly_token_limit": q.monthly_limit,
        "remaining_tokens": remaining,
        "quota_exhausted": q.used_tokens >= q.monthly_limit,
    }
    return _serialize_user_profile(updated, quota)


@router.get("/api/user/usage", response_model=UserUsageStats)
async def get_my_usage_stats(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """取得目前登入使用者的當月 Token 用量統計。"""
    await ensure_quota_row_exists(current_user["id"])
    row = await db.fetchrow(
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
        current_user["id"],
        DEFAULT_FALLBACK_MONTHLY_LIMIT,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    used = int(row["used_tokens"])
    limit = int(row["monthly_limit"])
    remaining = max(0, limit - used)
    usage_percent = round((used / limit) * 100) if limit > 0 else 0
    period_start = row["current_period_start"]
    tier_name = row["tier_name"] if row["tier_name"] else "free"

    return {
        "tier_name": tier_name,
        "used_tokens": used,
        "monthly_token_limit": limit,
        "remaining_tokens": remaining,
        "usage_percent": usage_percent,
        "quota_exhausted": used >= limit,
        "current_period_start": period_start.isoformat() if period_start else None,
    }


@router.delete("/api/user")
async def delete_account(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user)
):
    """永久刪除帳號（CASCADE 會自動清除相關資料）"""
    await db.execute(
        "DELETE FROM users WHERE id = $1",
        current_user["id"]
    )
    return {"status": "success", "message": "Account has been permanently deleted."}
