"""
User Management API (使用者管理接口)
======================================
使用 asyncpg 原生連線操作 PostgreSQL。
asyncpg.Record 的欄位存取語法：record['column_name']

注意：此系統僅支援 Google SSO 登入，無本地密碼功能。
"""

import asyncpg
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Any, Dict, Literal, Optional
from datetime import datetime, timezone

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user
from app.backend.module.feedback_security import (
    public_feedback_config,
    run_pre_insert_checks,
)
from app.backend.module.feedback_rewards import (
    FEEDBACK_TOKEN_REWARD,
    apply_feedback_token_reward,
    build_feedback_success_payload,
    get_feedback_eligibility,
    public_feedback_reward_config,
)
from app.backend.module.usage_quota import (
    DEFAULT_FALLBACK_MONTHLY_LIMIT,
    compute_quota_resets_at,
    ensure_quota_row_exists,
    fetch_quota_status,
)

router = APIRouter(tags=["User Management"])
logger = logging.getLogger(__name__)


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
    current_period_start: Optional[str] = None
    quota_resets_at: Optional[str] = None


def _quota_payload_from_status(q) -> dict:
    remaining = max(0, q.monthly_limit - q.used_tokens)
    resets_at = compute_quota_resets_at(q.current_period_start)
    return {
        "used_tokens": q.used_tokens,
        "monthly_token_limit": q.monthly_limit,
        "remaining_tokens": remaining,
        "quota_exhausted": q.used_tokens >= q.monthly_limit,
        "current_period_start": (
            q.current_period_start.isoformat() if q.current_period_start else None
        ),
        "quota_resets_at": resets_at.isoformat() if resets_at else None,
    }


class UserUsageStats(BaseModel):
    tier_name: str = "free"
    used_tokens: int
    monthly_token_limit: int
    remaining_tokens: int
    usage_percent: int
    quota_exhausted: bool
    current_period_start: Optional[str] = None
    quota_resets_at: Optional[str] = None


FeedbackCategory = Literal["feature", "bug", "other"]

_MESSAGE_MIN_LEN = 10
_MESSAGE_MAX_LEN = 2000
_PAGE_URL_MAX_LEN = 500
_USER_AGENT_MAX_LEN = 512


class SubmitFeedbackRequest(BaseModel):
    category: FeedbackCategory
    message: str = Field(..., min_length=_MESSAGE_MIN_LEN, max_length=_MESSAGE_MAX_LEN)
    page_url: Optional[str] = Field(default=None, max_length=_PAGE_URL_MAX_LEN)
    user_agent: Optional[str] = Field(default=None, max_length=_USER_AGENT_MAX_LEN)
    context: Optional[Dict[str, Any]] = None
    captcha_token: Optional[str] = Field(default=None, max_length=2048)
    website: Optional[str] = Field(default=None, max_length=200)
    form_opened_at: Optional[datetime] = None

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < _MESSAGE_MIN_LEN:
            raise ValueError(f"Message must be at least {_MESSAGE_MIN_LEN} characters.")
        return stripped


class SubmitFeedbackResponse(BaseModel):
    id: str
    status: str
    created_at: str
    tokens_granted: int = 0
    used_tokens: int = 0
    monthly_token_limit: int = 0
    remaining_tokens: int = 0
    quota_exhausted: bool = False
    submissions_today: int = 0
    remaining_today: int = 0
    daily_max: int = 3


class FeedbackEligibilityResponse(BaseModel):
    submissions_today: int
    remaining_today: int
    daily_max: int
    token_reward: int
    daily_timezone: str
    can_submit_today: bool


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
    return _serialize_user_profile(row, _quota_payload_from_status(q))


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
    return _serialize_user_profile(updated, _quota_payload_from_status(q))


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
    resets_at = compute_quota_resets_at(period_start)

    return {
        "tier_name": tier_name,
        "used_tokens": used,
        "monthly_token_limit": limit,
        "remaining_tokens": remaining,
        "usage_percent": usage_percent,
        "quota_exhausted": used >= limit,
        "current_period_start": period_start.isoformat() if period_start else None,
        "quota_resets_at": resets_at.isoformat() if resets_at else None,
    }


@router.get("/api/public/feedback-config")
async def get_feedback_public_config():
    """前端建議回饋表單：Turnstile site key、防刷與 Token 獎勵規則（無需登入）。"""
    return {
        **public_feedback_config(),
        **public_feedback_reward_config(),
    }


@router.get("/api/user/feedback/eligibility", response_model=FeedbackEligibilityResponse)
async def get_feedback_eligibility_status(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """目前登入使用者今日尚可提交幾次建議回饋。"""
    return await get_feedback_eligibility(db, current_user["id"])


@router.post("/api/user/feedback", status_code=status.HTTP_201_CREATED, response_model=SubmitFeedbackResponse)
async def submit_feedback(
    body: SubmitFeedbackRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    提交使用者建議或問題回饋（需登入）。

    安全：rate limit、重複內容檢查、context/page_url 驗證、honeypot、
    可選 Cloudflare Turnstile CAPTCHA（見 TURNSTILE_* 環境變數）。

    HTTP 回應：
    - 201 Created : 已收到
    - 401         : 未登入
    - 409         : 短時間內重複相同內容
    - 422         : 欄位驗證失敗
    - 429         : 提交過於頻繁，或今日回饋次數已達上限（`feedback_daily_limit`）
    - 500         : 伺服器錯誤（不含 DB 細節）

    成功提交後：寫入 `user_feedback.tokens_granted`，並從 `user_usage_quotas.used_tokens` 扣除獎勵 Token。
    """
    user_id = current_user["id"]
    now_utc = datetime.now(timezone.utc)

    safe_page_url, safe_user_agent, ctx = await run_pre_insert_checks(
        db,
        user_id,
        message=body.message,
        page_url=body.page_url,
        user_agent=body.user_agent,
        context=body.context,
        website=body.website,
        form_opened_at=body.form_opened_at,
        captcha_token=body.captcha_token,
        request=request,
    )

    try:
        result = None
        async with db.transaction():
            row = await db.fetchrow(
                """
                INSERT INTO user_feedback
                    (user_id, category, message, page_url, user_agent, context,
                     status, tokens_granted, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'new', $7, $8, $8)
                RETURNING id, status, created_at
                """,
                user_id,
                body.category,
                body.message,
                safe_page_url,
                safe_user_agent,
                json.dumps(ctx, ensure_ascii=False),
                FEEDBACK_TOKEN_REWARD,
                now_utc,
            )
            new_used = await apply_feedback_token_reward(db, user_id, FEEDBACK_TOKEN_REWARD)
            logger.info(
                "feedback reward granted user_id=%s tokens=%s used_tokens_after=%s",
                user_id,
                FEEDBACK_TOKEN_REWARD,
                new_used,
            )
            result = await build_feedback_success_payload(
                db,
                user_id,
                feedback_id=str(row["id"]),
                feedback_status=row["status"],
                created_at=row["created_at"].isoformat(),
                tokens_granted=FEEDBACK_TOKEN_REWARD,
            )
        return result
    except asyncpg.PostgresError as e:
        logger.exception("submit_feedback database error user_id=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit feedback. Please try again later.",
        ) from e


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
