"""
使用者回饋 API 安全檢查：rate limit、重複提交、context/page_url 驗證、Turnstile CAPTCHA。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import HTTPException, Request, status

# --- 可透過 .env 調整 ---
FEEDBACK_RATE_LIMIT_MAX = int(os.getenv("FEEDBACK_RATE_LIMIT_MAX", "5"))
FEEDBACK_RATE_LIMIT_WINDOW_MINUTES = int(
    os.getenv("FEEDBACK_RATE_LIMIT_WINDOW_MINUTES", "10")
)
FEEDBACK_DUPLICATE_WINDOW_MINUTES = int(
    os.getenv("FEEDBACK_DUPLICATE_WINDOW_MINUTES", "30")
)
FEEDBACK_MIN_SUBMIT_SECONDS = int(os.getenv("FEEDBACK_MIN_SUBMIT_SECONDS", "2"))

CONTEXT_MAX_KEYS = 20
CONTEXT_MAX_BYTES = 4096
CONTEXT_MAX_DEPTH = 2
CONTEXT_MAX_STRING_LEN = 500
CONTEXT_KEY_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

PAGE_URL_MAX_LEN = 500
_PAGE_URL_PATTERN = re.compile(r"^/[a-zA-Z0-9/._-]*$")

TURNSTILE_SECRET_KEY = (
    os.getenv("TURNSTILE_SECRET_KEY") or os.getenv("CF_TURNSTILE_SECRET_KEY") or ""
).strip()
TURNSTILE_SITE_KEY = (
    os.getenv("TURNSTILE_SITE_KEY") or os.getenv("CF_TURNSTILE_SITE_KEY") or ""
).strip()

_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def turnstile_enabled() -> bool:
    return bool(TURNSTILE_SECRET_KEY and TURNSTILE_SITE_KEY)


def public_feedback_config() -> dict[str, Any]:
    return {
        "turnstile_site_key": TURNSTILE_SITE_KEY if turnstile_enabled() else None,
        "min_submit_seconds": FEEDBACK_MIN_SUBMIT_SECONDS,
        "rate_limit_max": FEEDBACK_RATE_LIMIT_MAX,
        "rate_limit_window_minutes": FEEDBACK_RATE_LIMIT_WINDOW_MINUTES,
    }


def assert_honeypot_empty(website: str | None) -> None:
    if website and website.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid submission.",
        )


def assert_min_form_duration(form_opened_at: datetime | None) -> None:
    if form_opened_at is None:
        return
    if form_opened_at.tzinfo is None:
        form_opened_at = form_opened_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - form_opened_at).total_seconds()
    if elapsed < FEEDBACK_MIN_SUBMIT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Please wait a moment before submitting.",
        )
    if elapsed > 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Form session expired. Please reopen the feedback form.",
        )


def validate_page_url(page_url: str | None) -> str | None:
    if page_url is None:
        return None
    value = page_url.strip()
    if not value:
        return None
    if len(value) > PAGE_URL_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"page_url must be at most {PAGE_URL_MAX_LEN} characters.",
        )
    lowered = value.lower()
    if (
        "://" in value
        or lowered.startswith("javascript:")
        or lowered.startswith("data:")
        or lowered.startswith("//")
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_url must be an internal path starting with /.",
        )
    if not _PAGE_URL_PATTERN.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_url contains invalid characters.",
        )
    return value


def sanitize_user_agent(user_agent: str | None) -> str | None:
    if user_agent is None:
        return None
    value = user_agent.strip()
    if not value:
        return None
    # 移除控制字元，避免 log 注入
    value = "".join(ch for ch in value if ch >= " " or ch in "\t")
    return value[:512]


def sanitize_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    if len(raw) > CONTEXT_MAX_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"context may contain at most {CONTEXT_MAX_KEYS} keys.",
        )

    def _walk(obj: Any, depth: int) -> Any:
        if depth > CONTEXT_MAX_DEPTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"context nesting exceeds max depth {CONTEXT_MAX_DEPTH}.",
            )
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, str):
            if len(obj) > CONTEXT_MAX_STRING_LEN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"context string values must be at most {CONTEXT_MAX_STRING_LEN} characters.",
                )
            return obj
        if isinstance(obj, dict):
            cleaned: dict[str, Any] = {}
            for k, v in obj.items():
                if not isinstance(k, str) or not CONTEXT_KEY_PATTERN.match(k):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="context keys must match [a-zA-Z_][a-zA-Z0-9_]{0,63}.",
                    )
                cleaned[k] = _walk(v, depth + 1)
            return cleaned
        if isinstance(obj, list):
            if len(obj) > 20:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="context arrays may contain at most 20 items.",
                )
            return [_walk(item, depth + 1) for item in obj]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="context supports only JSON primitives, objects, and arrays.",
        )

    cleaned = _walk(raw, 0)
    encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > CONTEXT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"context serialized size exceeds {CONTEXT_MAX_BYTES} bytes.",
        )
    return cleaned


async def verify_turnstile(captcha_token: str | None, request: Request) -> None:
    if not turnstile_enabled():
        return
    if not captcha_token or not captcha_token.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CAPTCHA verification required.",
        )
    payload: dict[str, str] = {
        "secret": TURNSTILE_SECRET_KEY,
        "response": captcha_token.strip(),
    }
    client_host = request.client.host if request.client else None
    if client_host:
        payload["remoteip"] = client_host

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_TURNSTILE_VERIFY_URL, data=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CAPTCHA verification service unavailable. Please try again later.",
        )

    if not data.get("success"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CAPTCHA verification failed.",
        )


async def assert_feedback_rate_limit(
    db: asyncpg.Connection,
    user_id: UUID,
) -> None:
    count = await db.fetchval(
        """
        SELECT COUNT(*)::int
        FROM user_feedback
        WHERE user_id = $1
          AND created_at >= NOW() - ($2::text || ' minutes')::interval
        """,
        user_id,
        str(FEEDBACK_RATE_LIMIT_WINDOW_MINUTES),
    )
    if count >= FEEDBACK_RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "feedback_rate_limited",
                "message": (
                    f"提交過於頻繁，請 {FEEDBACK_RATE_LIMIT_WINDOW_MINUTES} 分鐘後再試"
                    f"（每 {FEEDBACK_RATE_LIMIT_WINDOW_MINUTES} 分鐘最多 "
                    f"{FEEDBACK_RATE_LIMIT_MAX} 次）。"
                ),
                "retry_after_minutes": FEEDBACK_RATE_LIMIT_WINDOW_MINUTES,
            },
        )


async def assert_not_duplicate_feedback(
    db: asyncpg.Connection,
    user_id: UUID,
    message: str,
) -> None:
    exists = await db.fetchval(
        """
        SELECT 1
        FROM user_feedback
        WHERE user_id = $1
          AND message = $2
          AND created_at >= NOW() - ($3::text || ' minutes')::interval
        LIMIT 1
        """,
        user_id,
        message,
        str(FEEDBACK_DUPLICATE_WINDOW_MINUTES),
    )
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_feedback",
                "message": "您最近已提交過相同內容的回饋，請稍後再試或修改內容。",
            },
        )


async def run_pre_insert_checks(
    db: asyncpg.Connection,
    user_id: UUID,
    *,
    message: str,
    page_url: str | None,
    user_agent: str | None,
    context: dict[str, Any] | None,
    website: str | None,
    form_opened_at: datetime | None,
    captcha_token: str | None,
    request: Request,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """回傳清洗後的 (page_url, user_agent, context)。"""
    assert_honeypot_empty(website)
    assert_min_form_duration(form_opened_at)
    await verify_turnstile(captcha_token, request)
    await assert_feedback_rate_limit(db, user_id)
    await assert_not_duplicate_feedback(db, user_id, message)

    safe_page_url = validate_page_url(page_url)
    safe_user_agent = sanitize_user_agent(user_agent)
    safe_context = sanitize_context(context)
    return safe_page_url, safe_user_agent, safe_context
