from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional, Any
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.backend.database.postgresql import get_db

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
    current_user: Any = Depends(get_current_user)
):
    """
    修改個人資料 (目前僅限修改 username)
    """
    if update_data.username:
        await db.execute(
            text("UPDATE users SET username = :username, updated_at = :now WHERE id = :u_id"),
            {
                "username": update_data.username, 
                "now": datetime.now(timezone.utc),
                "u_id": current_user.id
            }
        )
    
    await db.commit()
    
    # 重新撈取更新後的資料
    result = await db.execute(
        text("SELECT id, email, username, status, tier_id FROM users WHERE id = :u_id"),
        {"u_id": current_user.id}
    )
    return result.fetchone()

@router.patch("/password")
async def change_password(
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user)
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
    
    # 2. 更新密碼 (純 SQL)
    await db.execute(
        text("UPDATE users SET password_hash = :pw, updated_at = :now WHERE id = :u_id"),
        {
            "pw": hash_password(data.new_password), 
            "now": datetime.now(timezone.utc),
            "u_id": current_user.id
        }
    )
    
    # 3. 安全性增強：刪除該使用者的所有 Refresh Tokens (純 SQL)
    await db.execute(
        text("DELETE FROM refresh_tokens WHERE user_id = :u_id"),
        {"u_id": current_user.id}
    )
    
    await db.commit()
    return {"status": "success", "message": "Password updated successfully. Please login again on all devices."}

@router.delete("")
async def delete_account(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user)
):
    """
    刪除帳號 (純 SQL)
    """
    await db.execute(
        text("DELETE FROM users WHERE id = :u_id"),
        {"u_id": current_user.id}
    )
    await db.commit()
    return {"status": "success", "message": "Account has been permanently deleted."}
