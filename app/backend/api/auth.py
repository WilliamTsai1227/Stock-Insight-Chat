"""
Auth API（Google SSO 認證）
============================
僅支援 Google OAuth 2.0 + OIDC 登入。

流程：
  1. GET /api/user/auth/google/start   → 產生 state、設 HttpOnly Cookie、302 到 Google
  2. GET /api/user/auth/google/callback → 驗 state、換 token、upsert user、簽發 RT Cookie
  3. POST /api/user/logout              → 撤銷 RT、清 Cookie
  4. POST /api/user/refresh             → RT Rotation：換新 AT + 新 RT

所需環境變數：
  GOOGLE_CLIENT_ID         Google OAuth Client ID
  GOOGLE_CLIENT_SECRET     Google OAuth Client Secret
  GOOGLE_OAUTH_REDIRECT_URI  Callback URI（需與 Google Console 完全一致）
  FRONTEND_URL             前端根網址（callback 後重導用）
  COOKIE_SECURE            true = 僅 HTTPS 送出 Cookie（正式環境）

asyncpg 語法：$1 $2 位置參數；fetchrow / fetchval / execute。
"""

import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse
from uuid import UUID

import asyncpg
import httpx
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import (
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
)

# ── 設定 ──────────────────────────────────────────────────────────────────────
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8000/api/user/auth/google/callback",
)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost").rstrip("/")
_CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
# 區網 dev：允許 192.168.0.x 任意 port（與 app.py CORS regex 對齊）
_FRONTEND_RETURN_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|192\.168\.0\.\d+)(:\d+)?$",
    re.IGNORECASE,
)

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

router = APIRouter(tags=["User Authentication"])


# ── Response Schemas ──────────────────────────────────────────────────────────

class TokenRefreshResponse(BaseModel):
    status: str = "success"
    access_token: str
    token_type: str = "bearer"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_frontend_origin(url: Optional[str]) -> Optional[str]:
    """將 URL 正規化為 origin（scheme + host[:port]），失敗回傳 None。"""
    if not url or not str(url).strip():
        return None
    try:
        parsed = urlparse(str(url).strip())
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.path not in ("", "/"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _is_allowed_frontend_origin(origin: str) -> bool:
    """只允許 FRONTEND_URL、CORS 白名單、或本機/192.168.0.* dev origin。"""
    normalized = origin.rstrip("/")
    if normalized == FRONTEND_URL.rstrip("/"):
        return True
    if normalized in _CORS_ALLOWED_ORIGINS:
        return True
    return bool(_FRONTEND_RETURN_ORIGIN_RE.match(normalized))


def resolve_frontend_return_url(candidate: Optional[str]) -> str:
    """
    決定 OAuth 完成後要回到哪個前端 origin。
    優先使用 start 階段帶入且通過白名單的 return_url，否則 fallback FRONTEND_URL。
    """
    origin = _normalize_frontend_origin(candidate)
    if origin and _is_allowed_frontend_origin(origin):
        return origin
    return FRONTEND_URL.rstrip("/")


def _fe_error(error_code: str, return_base: Optional[str] = None) -> RedirectResponse:
    """重導到前端登入頁並帶上錯誤代碼（Query String）。"""
    base = resolve_frontend_return_url(return_base)
    return RedirectResponse(
        url=f"{base}/login.html?error={error_code}",
        status_code=302,
    )


async def _issue_refresh_token(
    db: asyncpg.Connection,
    user_id: UUID,
    user_data: dict,
) -> str:
    """產生並寫入 Refresh Token（含 jti 碰撞重試），回傳 token 字串。"""
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    for _ in range(3):
        candidate = create_refresh_token(data=user_data)
        try:
            await db.execute(
                "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
                user_id,
                candidate,
                expires_at,
            )
            return candidate
        except UniqueViolationError:
            continue
    raise RuntimeError("Failed to generate unique refresh token after 3 attempts")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/user/auth/google/start")
async def google_start(return_url: Optional[str] = None):
    """
    啟動 Google OAuth 登入流程。

    產生隨機 state → 存入 HttpOnly Cookie → 302 重導到 Google 授權頁面。
    前端請帶 `return_url`（例如 `window.location.origin`），callback 會回到同一 origin，
    避免區網 IP 登入後被重導到 FRONTEND_URL 預設的 localhost。
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured on this server.",
        )

    state = secrets.token_urlsafe(32)
    frontend_return = resolve_frontend_return_url(return_url)

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",  # 讓使用者每次都能切換 Google 帳號
    }
    # RedirectResponse 必須直接呼叫 set_cookie；
    # 若用 injected Response 物件設 Cookie 再 return RedirectResponse，
    # FastAPI 不會合併兩者的 headers，Cookie 不會送出。
    redirect = RedirectResponse(
        url=f"{_GOOGLE_AUTH_URL}?{urlencode(params)}",
        status_code=302,
    )
    redirect.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=600,  # 10 分鐘內完成 OAuth flow
    )
    redirect.set_cookie(
        key="oauth_return_url",
        value=frontend_return,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=600,
    )
    return redirect


@router.get("/api/user/auth/google/callback")
async def google_callback(
    db: asyncpg.Connection = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    oauth_state: Optional[str] = Cookie(None),
    oauth_return_url: Optional[str] = Cookie(None),
):
    """
    Google OAuth Callback。

    流程：驗 state → 換 token → 取 UserInfo → Upsert user → 簽發 RT Cookie → 302 回前端。
    前端偵測到 refresh_token Cookie 後，呼叫 POST /api/user/refresh 取得 Access Token。
    """
    frontend_base = resolve_frontend_return_url(oauth_return_url)

    # 0. 使用者在 Google 頁面取消授權
    if error:
        return _fe_error("oauth_cancelled", frontend_base)

    # 1. 防 CSRF：驗 state
    if not state or not oauth_state or state != oauth_state:
        return _fe_error("invalid_state", frontend_base)
    # oauth_state Cookie 有 max_age=600，驗過後自然過期，無需主動刪除

    # 2. 用 code 換 Google Access Token
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
    except httpx.HTTPError as e:
        print(f"[Google OAuth] Token exchange failed: {e}")
        return _fe_error("token_exchange_failed", frontend_base)

    google_at = token_data.get("access_token")
    if not google_at:
        return _fe_error("no_access_token", frontend_base)

    # 3. 取 Google UserInfo（sub、email、name）
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {google_at}"},
            )
            userinfo_resp.raise_for_status()
            userinfo = userinfo_resp.json()
    except httpx.HTTPError as e:
        print(f"[Google OAuth] UserInfo fetch failed: {e}")
        return _fe_error("userinfo_failed", frontend_base)

    google_sub: Optional[str] = userinfo.get("sub")
    email: Optional[str] = userinfo.get("email")
    display_name: str = userinfo.get("name") or (email.split("@")[0] if email else "user")

    if not google_sub or not email:
        return _fe_error("missing_user_info", frontend_base)

    # 4. Upsert user
    now = datetime.now(timezone.utc)
    try:
        user = await db.fetchrow(
            "SELECT id, email, username FROM users WHERE google_sub = $1",
            google_sub,
        )

        if not user:
            # 4a. 相同 email 的既有帳號（舊密碼帳號升級或帳號合併）→ 補綁 google_sub
            existing = await db.fetchrow(
                "SELECT id, email, username FROM users WHERE email = $1",
                email,
            )
            if existing:
                await db.execute(
                    """
                    UPDATE users
                    SET google_sub = $1, last_login_at = $2, last_login_provider = 'google'
                    WHERE id = $3
                    """,
                    google_sub,
                    now,
                    existing["id"],
                )
                user = existing
            else:
                # 4b. 全新帳號 — 確保 username 唯一
                base = display_name.lower().replace(" ", "_")[:40]
                username = base
                i = 1
                while await db.fetchval(
                    "SELECT id FROM users WHERE username = $1", username
                ):
                    username = f"{base}_{i}"
                    i += 1

                async with db.transaction():
                    user_id = await db.fetchval(
                        """
                        INSERT INTO users
                            (email, username, google_sub, last_login_provider, last_login_at, tier_id)
                        VALUES
                            ($1, $2, $3, 'google', $4,
                             (SELECT id FROM subscription_tiers WHERE name = 'free' LIMIT 1))
                        RETURNING id
                        """,
                        email,
                        username,
                        google_sub,
                        now,
                    )
                    await db.execute(
                        """
                        INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
                        VALUES ($1, $2, 0)
                        """,
                        user_id,
                        now,
                    )

                user = await db.fetchrow(
                    "SELECT id, email, username FROM users WHERE id = $1",
                    user_id,
                )
        else:
            # 4c. 已存在帳號 — 更新最後登入時間
            await db.execute(
                "UPDATE users SET last_login_at = $1, last_login_provider = 'google' WHERE id = $2",
                now,
                user["id"],
            )

    except Exception as e:
        print(f"[Google OAuth] DB error: {e}")
        return _fe_error("db_error", frontend_base)

    # 5. 簽發自有 AT + RT
    user_data = {"sub": str(user["id"]), "email": user["email"]}
    try:
        rt = await _issue_refresh_token(db, user["id"], user_data)
    except RuntimeError as e:
        print(f"[Google OAuth] RT issue error: {e}")
        return _fe_error("session_error", frontend_base)

    # 清理該使用者過期的 RT（fire-and-forget）
    await db.execute(
        "DELETE FROM refresh_tokens WHERE user_id = $1 AND expires_at <= NOW()",
        user["id"],
    )

    # 6. 設定 HttpOnly RT Cookie 並 302 回前端
    # 注意：必須在 RedirectResponse 上直接呼叫 set_cookie，
    # 不可用 injected Response 物件——FastAPI 不會合併其 headers 到 RedirectResponse。
    redirect = RedirectResponse(url=f"{frontend_base}/index.html", status_code=302)
    redirect.set_cookie(
        key="refresh_token",
        value=rt,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )
    return redirect


@router.post("/api/user/logout")
async def logout(
    response: Response,
    refresh_token: Optional[str] = Cookie(None),
    db: asyncpg.Connection = Depends(get_db),
):
    """登出：撤銷 Refresh Token 並清除 Cookie。"""
    if refresh_token:
        await db.execute(
            "DELETE FROM refresh_tokens WHERE token = $1",
            refresh_token,
        )
    response.delete_cookie("refresh_token")
    return {"status": "success", "message": "Logged out successfully"}


@router.post("/api/user/refresh", response_model=TokenRefreshResponse)
async def refresh_access_token(
    response: Response,
    refresh_token: Optional[str] = Cookie(None),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    RT Rotation：使用 Refresh Token Cookie 換取新的 AT + 新的 RT。

    安全機制：
    - DELETE...RETURNING 原子消費舊 RT，確保同一 RT 只能用一次。
    - 若 RT 已被消費（JWT 有效但 DB 無記錄）→ Token Reuse 攻擊，撤銷該 user 所有 Session。
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    # 1. Stateless 驗簽名 + exp
    payload = decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token invalid or expired. Please login again.",
        )

    # 2. 原子消費
    consumed = await db.fetchrow(
        """
        DELETE FROM refresh_tokens
        WHERE token = $1 AND expires_at > NOW()
        RETURNING user_id
        """,
        refresh_token,
    )

    if not consumed:
        # Token Reuse 偵測：撤銷該 user 所有 Session
        user_id_str = payload.get("sub")
        if user_id_str:
            try:
                uid = UUID(user_id_str)
                await db.execute(
                    "DELETE FROM refresh_tokens WHERE user_id = $1",
                    uid,
                )
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Security alert: Token reuse detected. All sessions have been revoked. Please login again.",
        )

    # 3. 取使用者資料
    user = await db.fetchrow(
        "SELECT id, email FROM users WHERE id = $1",
        consumed["user_id"],
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    # 4. 簽發新 AT
    user_data = {"sub": str(user["id"]), "email": user["email"]}
    new_at = create_access_token(data=user_data)

    # 5. RT Rotation：新 RT 寫入 DB
    try:
        new_rt = await _issue_refresh_token(db, user["id"], user_data)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rotate refresh token. Please login again.",
        )

    # 6. 設定新 RT Cookie
    response.set_cookie(
        key="refresh_token",
        value=new_rt,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )

    return {
        "status": "success",
        "access_token": new_at,
        "token_type": "bearer",
    }
