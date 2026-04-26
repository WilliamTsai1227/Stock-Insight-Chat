from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie
from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

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

    class Config:
        from_attributes = True

class LoginResponse(BaseModel):
    status: str = "success"
    access_token: str
    token_type: str = "bearer"
    user: UserResponse

# --- API Endpoints ---

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    使用者註冊接口
    """
    # 1. 檢查 Email 是否已存在 (純 SQL)
    result = await db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": request.email}
    )
    if result.fetchone():
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. 檢查 Username 是否已存在 (純 SQL)
    result = await db.execute(
        text("SELECT id FROM users WHERE username = :username"),
        {"username": request.username}
    )
    if result.fetchone():
        raise HTTPException(status_code=400, detail="Username already taken")

    # 3. 建立使用者 (純 SQL)
    insert_user_query = text("""
        INSERT INTO users (email, username, password_hash, tier_id)
        VALUES (:email, :username, :password_hash, 
                (SELECT id FROM subscription_tiers WHERE name = 'free' LIMIT 1))
        RETURNING id
    """)
    result = await db.execute(
        insert_user_query,
        {
            "email": request.email,
            "username": request.username,
            "password_hash": hash_password(request.password)
        }
    )
    new_user_id = result.scalar()

    # 4. 初始化配額 (純 SQL)
    await db.execute(
        text("""
            INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
            VALUES (:user_id, :start, 0)
        """),
        {"user_id": new_user_id, "start": datetime.now(timezone.utc)}
    )
    
    await db.commit()
    return {
        "status": "success",
        "message": "User registered successfully",
        "user_id": new_user_id
    }

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """
    使用者登入接口
    """
    # 1. 驗證使用者 (純 SQL)
    result = await db.execute(
        text("SELECT id, email, username, password_hash FROM users WHERE email = :email"),
        {"email": request.email}
    )
    user = result.fetchone()

    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. 產生 Tokens
    user_id_str = str(user.id)
    user_data = {"sub": user_id_str, "email": user.email}
    access_token = create_access_token(data=user_data)
    refresh_token_str = create_refresh_token(data=user_data)

    # 3. 將 Refresh Token 存入資料庫 (純 SQL)
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    await db.execute(
        text("INSERT INTO refresh_tokens (user_id, token, expires_at) VALUES (:u_id, :tk, :exp)"),
        {"u_id": user.id, "tk": refresh_token_str, "exp": expires_at}
    )
    
    # 4. 更新最後登入時間 (純 SQL)
    await db.execute(
        text("UPDATE users SET last_login_at = :now WHERE id = :u_id"),
        {"now": datetime.now(timezone.utc), "u_id": user.id}
    )
    
    await db.commit()

    # 5. 設定 HttpOnly Cookie
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
            "id": user.id,
            "username": user.username,
            "email": user.email
        }
    }

@router.post("/logout")
async def logout(
    response: Response, 
    refresh_token: Optional[str] = Cookie(None), 
    db: AsyncSession = Depends(get_db)
):
    """
    使用者登出接口
    """
    if refresh_token:
        # 從資料庫刪除該 Refresh Token (純 SQL)
        await db.execute(
            text("DELETE FROM refresh_tokens WHERE token = :tk"),
            {"tk": refresh_token}
        )
        await db.commit()
        
    # 清除 Cookie
    response.delete_cookie("refresh_token")
    return {"status": "success", "message": "Logged out successfully"}

@router.post("/refresh")
async def refresh_token(
    refresh_token: Optional[str] = Cookie(None), 
    db: AsyncSession = Depends(get_db)
):
    """
    使用 Refresh Token 換取新的 Access Token
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Refresh token missing"
        )

    # 1. 檢查資料庫中是否存在此 Refresh Token (純 SQL)
    result = await db.execute(
        text("SELECT user_id, expires_at FROM refresh_tokens WHERE token = :tk"),
        {"tk": refresh_token}
    )
    db_token = result.fetchone()
    
    if not db_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid refresh token"
        )

    # 2. 檢查是否過期 (純 SQL)
    if db_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        await db.execute(
            text("DELETE FROM refresh_tokens WHERE token = :tk"),
            {"tk": refresh_token}
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Refresh token expired"
        )

    # 3. 取得使用者並產生新 Access Token (純 SQL)
    user_result = await db.execute(
        text("SELECT id, email FROM users WHERE id = :u_id"),
        {"u_id": db_token.user_id}
    )
    user = user_result.fetchone()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="User not found"
        )

    user_data = {"sub": str(user.id), "email": user.email}
    new_access_token = create_access_token(data=user_data)

    return {
        "status": "success",
        "access_token": new_access_token,
        "token_type": "bearer"
    }
