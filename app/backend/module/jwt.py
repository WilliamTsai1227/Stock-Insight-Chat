"""
JWT 模組 (app/backend/module/jwt.py)
======================================
統一管理所有 JWT 與認證相關的邏輯，分為四個區塊：

  ① 全域設定        — 金鑰、演算法、有效期常數
  ② 密碼工具        — Argon2 雜湊 / 驗證
  ③ 簽發 Token      — 產生 AT / RT / 解碼原始 Token（auth.py 使用）
  ④ 驗收 Token      — FastAPI Depends 注入，掛在所有受保護的 endpoint

職責分界：
  ③ 簽發端 → 由 auth.py（登入 / 登出 / refresh 流程）呼叫
  ④ 驗收端 → 由各 API endpoint 透過 Depends(get_current_user) 呼叫
"""

import os
import asyncpg
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4, UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext

from app.backend.database.postgresql import get_db


# ======================================================
# ① 全域設定
# ======================================================

SECRET_KEY                 = os.getenv("SECRET_KEY", "super-secret-key-for-development")
ALGORITHM                  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS  = 7


# ======================================================
# ② 密碼工具（Argon2）
# ======================================================

# schemes=["argon2"]：目前安全等級最高的密碼雜湊演算法
_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    """將明文密碼進行 Argon2 雜湊"""
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """驗證明文密碼與 Argon2 雜湊值是否匹配"""
    return _pwd_context.verify(plain_password, hashed_password)


# ======================================================
# ③ 簽發 Token（auth.py 使用）
# ======================================================

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    產生 Access Token（短效期，無狀態）。
    預設有效期：15 分鐘（ACCESS_TOKEN_EXPIRE_MINUTES）。
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """
    產生 Refresh Token（長效期）。
    內嵌 jti（uuid4）避免同秒碰撞造成 unique constraint 衝突，
    並支援後端稽核。
    """
    now    = datetime.now(timezone.utc)
    expire = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode = data.copy()
    to_encode.update({
        "exp":  expire,
        "iat":  now,
        "jti":  str(uuid4()),
        "type": "refresh",
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """
    解碼並驗證任意 Token（AT 或 RT）。
    驗證失敗時回傳 None，由呼叫方決定如何處理。
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


# ======================================================
# ④ 驗收 Token — FastAPI Depends（各受保護 endpoint 使用）
# ======================================================

# 從 Header Authorization: Bearer <token> 提取 AT
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/user/login")

# 共用 401 例外（避免重複建立）
_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _decode_user_id(token: str) -> UUID:
    """
    解碼 AT 並回傳 user UUID（sub claim）。
    簽名錯誤、過期、格式不合一律拋出 401。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str: str = payload.get("sub")
        if not user_id_str:
            raise _CREDENTIALS_EXCEPTION
        return UUID(user_id_str)
    except (JWTError, ValueError):
        raise _CREDENTIALS_EXCEPTION


async def get_current_user_id(
    token: str = Depends(_oauth2_scheme),
) -> UUID:
    """
    【輕量版 Depends】純 JWT 驗證，不查資料庫。

    適用情境：
    - SSE 串流端點（/api/chat/messages）：連線可持續數秒，
      不應在整個串流期間占用一個 DB 連線。
    - 高頻讀取端點：只需確認 Token 合法，無需使用者詳細資料。

    回傳：使用者 UUID（來自 JWT sub claim）
    """
    return _decode_user_id(token)


async def get_current_user(
    token: str = Depends(_oauth2_scheme),
    db: asyncpg.Connection = Depends(get_db),
) -> asyncpg.Record:
    """
    【完整版 Depends】JWT 驗證 + DB 查詢，回傳完整使用者資料列。

    適用情境：
    - 需要使用者詳細資料的端點（建立專案、修改密碼、查詢配額等）。
    - 需確認帳號仍存在且狀態正常（帳號停用時應拒絕）。

    回傳：asyncpg.Record，可透過 record['column_name'] 存取欄位。
    """
    user_uuid = _decode_user_id(token)

    user = await db.fetchrow(
        """
        SELECT id, email, username, password_hash, status, tier_id,
               last_login_at, created_at, updated_at
        FROM users
        WHERE id = $1
        """,
        user_uuid,
    )

    if user is None:
        raise _CREDENTIALS_EXCEPTION

    if user["status"] == "disabled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )

    return user
