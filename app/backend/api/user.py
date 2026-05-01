"""
User Management API (使用者管理接口)
======================================
使用 asyncpg 原生連線操作 PostgreSQL。
asyncpg.Record 的欄位存取語法：record['column_name']
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime, timezone

from app.backend.database.postgresql import get_db
from app.backend.module.dependencies import get_current_user
from app.backend.module.security import hash_password, verify_password

router = APIRouter(tags=["User Management"])


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
    tier_id: Optional[str] = None


# --- API Endpoints ---

@router.get("/api/user", response_model=UserProfile)
async def get_my_profile(
    current_user: asyncpg.Record = Depends(get_current_user)
):
    """
    取得目前登入使用者的個人資料
    asyncpg.Record 支援 dict-like 存取
    """
    return {
        "id": str(current_user["id"]),
        "email": current_user["email"],
        "username": current_user["username"],
        "status": current_user["status"],
        "tier_id": str(current_user["tier_id"]) if current_user["tier_id"] else None
    }


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

    # 重新撈取最新資料
    updated = await db.fetchrow(
        "SELECT id, email, username, status, tier_id FROM users WHERE id = $1",
        current_user["id"]
    )

    return {
        "id": str(updated["id"]),
        "email": updated["email"],
        "username": updated["username"],
        "status": updated["status"],
        "tier_id": str(updated["tier_id"]) if updated["tier_id"] else None
    }


@router.patch("/api/user/password")
async def change_password(
    data: PasswordChange,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user)
):
    """
    修改密碼。
    成功後強制刪除該使用者所有 Refresh Token（全裝置登出）。
    """
    # 1. 驗證舊密碼
    if not verify_password(data.old_password, current_user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect old password"
        )

    # 2. 更新密碼 + 撤銷所有 Session（原子操作）
    async with db.transaction():
        await db.execute(
            "UPDATE users SET password_hash = $1, updated_at = $2 WHERE id = $3",
            hash_password(data.new_password),
            datetime.now(timezone.utc),
            current_user["id"]
        )
        await db.execute(
            "DELETE FROM refresh_tokens WHERE user_id = $1",
            current_user["id"]
        )

    return {
        "status": "success",
        "message": "Password updated. Please login again on all devices."
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
