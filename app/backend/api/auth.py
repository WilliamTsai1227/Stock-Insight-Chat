from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie
from pydantic import BaseModel, EmailStr
from typing import Optional
from uuid import UUID
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.backend.database.postgresql import get_db
from app.backend.models.orm import User, RefreshToken, UserUsageQuota, SubscriptionTier
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
    # 1. 檢查 Email 是否已存在
    result = await db.execute(select(User).where(User.email == request.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. 檢查 Username 是否已存在
    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    # 3. 密碼雜湊與建立使用者
    new_user = User(
        email=request.email,
        username=request.username,
        password_hash=hash_password(request.password)
    )
    db.add(new_user)
    await db.flush() # 取得新使用者的 ID

    # 4. 初始化配額 (預設為 Free 等級)
    # 這裡假設已經有 'free' 這個等級在 subscription_tiers 表中
    tier_result = await db.execute(select(SubscriptionTier).where(SubscriptionTier.name == "free"))
    free_tier = tier_result.scalar_one_or_none()
    
    if free_tier:
        new_user.tier_id = free_tier.id
    
    quota = UserUsageQuota(
        user_id=new_user.id,
        current_period_start=datetime.now(timezone.utc),
        used_tokens=0
    )
    db.add(quota)
    
    await db.commit()
    return {
        "status": "success",
        "message": "User registered successfully",
        "user_id": new_user.id
    }

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """
    使用者登入接口
    """
    # 1. 驗證使用者
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. 產生 Tokens
    user_data = {"sub": str(user.id), "email": user.email}
    access_token = create_access_token(data=user_data)
    refresh_token_str = create_refresh_token(data=user_data)

    # 3. 將 Refresh Token 存入資料庫
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db_refresh_token = RefreshToken(
        user_id=user.id,
        token=refresh_token_str,
        expires_at=expires_at
    )
    db.add(db_refresh_token)
    
    # 4. 更新最後登入時間
    user.last_login_at = datetime.now(timezone.utc)
    
    await db.commit()

    # 5. 設定 HttpOnly Cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token_str,
        httponly=True,
        secure=True, # 生產環境應設為 True
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    )

    return {
        "status": "success",
        "access_token": access_token,
        "token_type": "bearer",
        "user": user
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
        # 從資料庫刪除該 Refresh Token
        await db.execute(delete(RefreshToken).where(RefreshToken.token == refresh_token))
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

    # 1. 檢查資料庫中是否存在此 Refresh Token
    result = await db.execute(select(RefreshToken).where(RefreshToken.token == refresh_token))
    db_token = result.scalar_one_or_none()
    
    if not db_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid refresh token"
        )

    # 2. 檢查是否過期
    if db_token.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        await db.delete(db_token)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Refresh token expired"
        )

    # 3. 取得使用者並產生新 Access Token
    user_result = await db.execute(select(User).where(User.id == db_token.user_id))
    user = user_result.scalar_one_or_none()
    
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
