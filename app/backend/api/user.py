from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.backend.database.postgresql import get_db
from app.backend.models.orm import User, RefreshToken
from app.backend.core.dependencies import get_current_user
from app.backend.core.security import hash_password, verify_password

router = APIRouter(prefix="/user", tags=["User Management"])

# --- Request/Response Schemas ---

class UserUpdate(BaseModel):
    username: Optional[str] = None

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class UserProfile(BaseModel):
    id: str
    email: EmailStr
    username: str
    status: str
    tier_id: Optional[str]

    class Config:
        from_attributes = True

# --- API Endpoints ---

@router.get("", response_model=UserProfile)
async def get_my_profile(current_user: User = Depends(get_current_user)):
    """
    取得目前登入使用者的個人資料
    """
    return current_user

@router.patch("", response_model=UserProfile)
async def update_my_profile(
    update_data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    修改個人資料 (目前僅限修改 username)
    """
    if update_data.username:
        current_user.username = update_data.username
    
    await db.commit()
    await db.refresh(current_user)
    return current_user

@router.patch("/password")
async def change_password(
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    修改密碼。修改成功後會強制登出所有裝置（刪除所有 Refresh Tokens）。
    """
    # 1. 驗證舊密碼
    if not verify_password(data.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect old password"
        )
    
    # 2. 更新密碼
    current_user.password_hash = hash_password(data.new_password)
    
    # 3. 安全性增強：刪除該使用者的所有 Refresh Tokens (強制所有裝置重新登入)
    await db.execute(delete(RefreshToken).where(RefreshToken.user_id == current_user.id))
    
    await db.commit()
    return {"status": "success", "message": "Password updated successfully. Please login again on all devices."}

@router.delete("")
async def delete_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    刪除帳號 (永久刪除)
    """
    await db.delete(current_user)
    await db.commit()
    return {"status": "success", "message": "Account has been permanently deleted."}
