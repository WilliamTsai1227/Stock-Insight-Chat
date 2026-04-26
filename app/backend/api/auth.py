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

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie
from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta

from app.backend.database.postgresql import get_db
from app.backend.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    REFRESH_TOKEN_EXPIRE_DAYS
)

router = APIRouter(prefix="/user", tags=["User Authentication"])


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

@router.post("/register", status_code=status.HTTP_201_CREATED)
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


@router.post("/login", response_model=LoginResponse)
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
    refresh_token_str = create_refresh_token(data=user_data)

    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    # 3. 寫入 Refresh Token 並更新最後登入時間（原子操作）
    async with db.transaction():
        await db.execute(
            "INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES ($1, $2, $3)",
            user["id"],
            refresh_token_str,
            expires_at
        )
        await db.execute(
            "UPDATE users SET last_login_at = $1 WHERE id = $2",
            datetime.now(timezone.utc),
            user["id"]
        )

    # 4. 設定 HttpOnly Cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=True,
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


@router.post("/logout")
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


@router.post("/refresh")
async def refresh_access_token(
    refresh_token: Optional[str] = Cookie(None),
    db: asyncpg.Connection = Depends(get_db)
):
    """使用 Refresh Token 換取新的 Access Token"""
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing"
        )

    # 1. 查詢 Refresh Token
    db_token = await db.fetchrow(
        "SELECT user_id, expires_at FROM refresh_tokens WHERE token = $1",
        refresh_token
    )

    if not db_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )

    # 2. 檢查是否過期
    # asyncpg 從 TIMESTAMPTZ 欄位回傳的 datetime 已帶有 UTC 時區資訊，
    # 不可使用 .replace() 覆蓋（會破壞原有時區）。直接與 now(UTC) 比較即可。
    expires_at = db_token["expires_at"]
    if expires_at < datetime.now(timezone.utc):
        await db.execute(
            "DELETE FROM refresh_tokens WHERE token = $1",
            refresh_token
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired. Please login again."
        )

    # 3. 取得使用者並產生新 Access Token
    user = await db.fetchrow(
        "SELECT id, email FROM users WHERE id = $1",
        db_token["user_id"]
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    new_access_token = create_access_token(
        data={"sub": str(user["id"]), "email": user["email"]}
    )

    return {
        "status": "success",
        "access_token": new_access_token,
        "token_type": "bearer"
    }
