"""
Core Dependencies
==================
提供 FastAPI 依賴注入函式，包含 JWT 驗證與取得目前登入使用者。
已從 SQLAlchemy 遷移至純 asyncpg。
"""

import asyncpg
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from typing import Any
from uuid import UUID

from app.backend.database.postgresql import get_db
from app.backend.module.security import SECRET_KEY, ALGORITHM

# 定義 Token 取得方式 (從 Header 的 Authorization: Bearer <token>)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/user/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: asyncpg.Connection = Depends(get_db)
) -> asyncpg.Record:
    """
    從 Access Token 中解析出使用者，並從資料庫撈取完整資料列。
    這是所有需要「登入」才能存取的 API 的共同依賴。

    回傳值為 asyncpg.Record，可透過 record['column_name'] 存取欄位。
    注意：asyncpg 使用 $1, $2 作為參數佔位符。
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

    # 3. 從資料庫查詢使用者 (asyncpg 純 SQL)
    # asyncpg 使用位置參數 $1, $2... 而非命名參數
    user = await db.fetchrow(
        """
        SELECT id, email, username, password_hash, status, tier_id,
               last_login_at, created_at, updated_at
        FROM users
        WHERE id = $1
        """,
        user_uuid
    )

    if user is None:
        raise credentials_exception

    return user
