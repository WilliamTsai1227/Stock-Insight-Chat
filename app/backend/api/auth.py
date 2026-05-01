"""
Auth API (認證相關接口)
========================
使用 asyncpg 原生連線操作 PostgreSQL。

asyncpg 語法注意事項：
- 參數佔位符：$1, $2, $3...（位置參數）
- 取單列：await db.fetchrow(sql, *args)
- 取單值：await db.fetchval(sql, *args)
- 寫入/更新/刪除：await db.execute(sql, *args)
- 多個寫入需原子性時，用 async with db.transaction()
"""

import os
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie
from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta
from asyncpg.exceptions import UniqueViolationError

# secure=True 只在 HTTPS 下 Cookie 才會被瀏覽器送出
# 本機 HTTP 開發時必須設為 False，否則 RT Cookie 永遠不會被帶回來
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

from app.backend.database.postgresql import get_db
from app.backend.module.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    REFRESH_TOKEN_EXPIRE_DAYS
)

router = APIRouter(tags=["User Authentication"])


# --- Request/Response Schemas ---

class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: UUID
    username: str
    email: str
    tier: Optional[str] = "free"

class LoginResponse(BaseModel):
    status: str = "success"
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


# --- API Endpoints ---

@router.post("/api/user/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """使用者註冊接口"""

    # 所有寫入操作包在一個 transaction 中，確保原子性
    async with db.transaction():

        # 1. 檢查 Email 是否已存在
        existing_email = await db.fetchval(
            "SELECT id FROM users WHERE email = $1",
            request.email
        )
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")

        # 2. 檢查 Username 是否已存在
        existing_username = await db.fetchval(
            "SELECT id FROM users WHERE username = $1",
            request.username
        )
        if existing_username:
            raise HTTPException(status_code=400, detail="Username already taken")

        # 3. 建立使用者，tier_id 指向 free 等級
        new_user_id = await db.fetchval(
            """
            INSERT INTO users (email, username, password_hash, tier_id)
            VALUES ($1, $2, $3,
                    (SELECT id FROM subscription_tiers WHERE name = 'free' LIMIT 1))
            RETURNING id
            """,
            request.email,
            request.username,
            hash_password(request.password)
        )

        # 4. 初始化使用量配額
        await db.execute(
            """
            INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
            VALUES ($1, $2, 0)
            """,
            new_user_id,
            datetime.now(timezone.utc)
        )

    return {
        "status": "success",
        "message": "User registered successfully",
        "user_id": str(new_user_id)
    }


@router.post("/api/user/login", response_model=LoginResponse)
async def login(
    request: LoginRequest,
    response: Response,
    db: asyncpg.Connection = Depends(get_db)
):
    """使用者登入接口"""

    # 1. 查詢使用者
    user = await db.fetchrow(
        "SELECT id, email, username, password_hash FROM users WHERE email = $1",
        request.email
    )

    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. 產生 Tokens
    user_id_str = str(user["id"])
    user_data = {"sub": user_id_str, "email": user["email"]}
    access_token = create_access_token(data=user_data)

    # 3. 寫入 Refresh Token 並更新最後登入時間（原子操作）
    # 避免極端情況下 refresh token 撞 unique constraint，做少量重試
    refresh_token_str = None
    expires_at = None
    for _ in range(3):
        candidate = create_refresh_token(data=user_data)
        candidate_expires = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        try:
            async with db.transaction():
                await db.execute(
                    "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
                    user["id"],
                    candidate,
                    candidate_expires
                )
                await db.execute(
                    "UPDATE users SET last_login_at = $1 WHERE id = $2",
                    datetime.now(timezone.utc),
                    user["id"]
                )
            refresh_token_str = candidate
            expires_at = candidate_expires
            break
        except UniqueViolationError:
            continue

    if not refresh_token_str:
        raise HTTPException(status_code=500, detail="Failed to create refresh token. Please try again.")

    # 4. 清理該用戶自己的過期 RT（fire-and-forget）
    await db.execute(
        "DELETE FROM refresh_tokens WHERE user_id = $1 AND expires_at <= NOW()",
        user["id"]
    )

    # 5. 設定 HttpOnly Cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=COOKIE_SECURE,   # 本機 HTTP: False；正式 HTTPS: True
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    )

    return {
        "status": "success",
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"]
        }
    }


@router.post("/api/user/logout")
async def logout(
    response: Response,
    refresh_token: Optional[str] = Cookie(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """使用者登出接口"""
    if refresh_token:
        await db.execute(
            "DELETE FROM refresh_tokens WHERE token = $1",
            refresh_token
        )

    response.delete_cookie("refresh_token")
    return {"status": "success", "message": "Logged out successfully"}


@router.post("/api/user/refresh")
async def refresh_access_token(
    response: Response,
    refresh_token: Optional[str] = Cookie(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    RT Rotation：使用 Refresh Token 換取新的 AT + 新的 RT。

    安全機制：
    - 使用 DELETE...RETURNING 原子操作消費舊 RT，確保同一 RT 只能被使用一次。
    - 若 RT 不在 DB（已被消費）但簽名仍有效 → 判定為 Token Reuse 攻擊，
      立刻撤銷該 user 所有 Session，駭客與正常用戶同時被踢下線。
    - 若 RT 簽名無效或已過期 → 直接 401，不查 DB。
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing"
        )

    # 1. 驗證 JWT 簽名與過期（不查 DB，pure stateless 驗證）
    from app.backend.module.security import decode_token
    payload = decode_token(refresh_token)

    # 2. 原子消費：DELETE...RETURNING（只有一個 concurrent request 能成功）
    #    asyncpg 單一 statement 自動 commit，不需包在 transaction 裡
    consumed = await db.fetchrow(
        """
        DELETE FROM refresh_tokens
        WHERE token = $1 AND expires_at > NOW()
        RETURNING user_id
        """,
        refresh_token
    )

    if not consumed:
        # 區分兩種情境：
        # (A) payload 有效但 DB 找不到 → RT 已被消費 → Token Reuse 攻擊
        # (B) payload 無效（過期/簽名錯誤）→ 單純的非法請求
        if payload and payload.get("type") == "refresh":
            # 情境 A：撤銷該 user 所有 Session
            user_id_str = payload.get("sub")
            if user_id_str:
                try:
                    uid = UUID(user_id_str)
                    await db.execute(
                        "DELETE FROM refresh_tokens WHERE user_id = $1",
                        uid
                    )
                except Exception:
                    pass
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Security alert: Token reuse detected. All sessions have been revoked. Please login again."
            )
        else:
            # 情境 B：過期或非法 token
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token invalid or expired. Please login again."
            )

    # 3. 取得使用者資料
    user = await db.fetchrow(
        "SELECT id, email FROM users WHERE id = $1",
        consumed["user_id"]
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    # 4. 產生新 AT
    user_data = {"sub": str(user["id"]), "email": user["email"]}
    new_access_token = create_access_token(data=user_data)

    # 5. RT Rotation：產生新 RT 並存入 DB（含 jti 碰撞重試）
    new_refresh_token_str = None
    new_expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    for _ in range(3):
        candidate = create_refresh_token(data=user_data)
        try:
            await db.execute(
                "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
                user["id"],
                candidate,
                new_expires_at
            )
            new_refresh_token_str = candidate
            break
        except UniqueViolationError:
            continue

    if not new_refresh_token_str:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rotate refresh token. Please login again."
        )

    # 6. 設定新 RT Cookie（舊 RT 已在步驟 2 被原子刪除）
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token_str,
        httponly=True,
        secure=COOKIE_SECURE,   # 本機 HTTP: False；正式 HTTPS: True
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    )

    return {
        "status": "success",
        "access_token": new_access_token,
        "token_type": "bearer"
    }
