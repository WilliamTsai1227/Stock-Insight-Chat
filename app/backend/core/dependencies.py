from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
from uuid import UUID

from app.backend.database.postgresql import get_db
from app.backend.models.orm import User
from app.backend.core.security import SECRET_KEY, ALGORITHM

# 定義 Token 取得方式 (從 Header 的 Authorization: Bearer <token>)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_current_user(
    token: str = Depends(oauth2_scheme), 
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    從 Access Token 中解析出使用者，並從資料庫撈取完整的 User 物件。
    這是所有需要「登入」才能存取的 API 的共同依賴。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # 1. 解碼 JWT
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
            
        # 2. 驗證 UUID 格式
        try:
            user_uuid = UUID(user_id)
        except ValueError:
            raise credentials_exception
            
    except JWTError:
        raise credentials_exception

    # 3. 從資料庫查詢使用者 (純 SQL)
    result = await db.execute(
        text("SELECT id, email, username, status, tier_id, last_login_at, created_at, updated_at FROM users WHERE id = :u_id"),
        {"u_id": user_uuid}
    )
    user = result.fetchone()
    
    if user is None:
        raise credentials_exception
        
    return user
