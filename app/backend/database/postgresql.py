"""
PostgreSQL Connection Pool (asyncpg)
=====================================
使用原生 asyncpg 套件建立高效能連線池，完全取代 SQLAlchemy。

- pool_min_size: 最少維持的閒置連線數
- pool_max_size: 最多允許的並發連線數
- max_inactive_connection_lifetime: 超過此秒數的閒置連線將被回收
"""

import os
import ssl
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import asyncpg

# --- 全域 Connection Pool 物件 ---
_pool: asyncpg.Pool | None = None


def _parse_dsn() -> tuple[str, ssl.SSLContext | None]:
    """
    從環境變數讀取 PostgreSQL 連線字串 (asyncpg 格式，無需 +asyncpg 前綴)。

    asyncpg 不支援 URL query 的 ?ssl=require（會觸發 CantChangeRuntimeParamError），
    因此需剝離後改以 create_pool(ssl=...) 傳入。
    """
    raw = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:password123@db:5432/Insight",
    ).replace("postgresql+asyncpg://", "postgresql://")

    parsed = urlparse(raw)
    query = parse_qs(parsed.query, keep_blank_values=False)

    ssl_required = os.getenv("DATABASE_SSL", "").lower() in ("require", "true", "1")

    ssl_param = (query.pop("ssl", [None]) or [None])[0]
    if ssl_param in ("require", "true", "1"):
        ssl_required = True

    sslmode = (query.pop("sslmode", [None]) or [None])[0]
    if sslmode in ("require", "verify-ca", "verify-full"):
        ssl_required = True

    flat_query = {k: v[0] for k, v in query.items() if v}
    dsn = urlunparse(parsed._replace(query=urlencode(flat_query) if flat_query else ""))

    ssl_context = None
    if ssl_required:
        ssl_context = ssl.create_default_context()
        # RDS 憑證鏈在本機 Docker 環境常無法完整驗證，仍啟用 TLS 加密
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    return dsn, ssl_context


async def create_pool() -> None:
    """
    建立 asyncpg Connection Pool。
    應在 FastAPI 啟動時 (lifespan startup) 呼叫。
    """
    global _pool
    dsn, ssl_context = _parse_dsn()
    pool_kwargs: dict = {
        "dsn": dsn,
        "min_size": 5,              # 最少維持 5 條閒置連線
        "max_size": 20,             # 最多 20 條並發連線
        "max_inactive_connection_lifetime": 3600.0,  # 閒置超過 1 小時自動回收
        "command_timeout": 30.0,    # 單一 SQL 指令逾時 30 秒
    }
    if ssl_context is not None:
        pool_kwargs["ssl"] = ssl_context

    _pool = await asyncpg.create_pool(**pool_kwargs)
    print("[DB] asyncpg connection pool created.")


async def close_pool() -> None:
    """
    關閉 Connection Pool。
    應在 FastAPI 關閉時 (lifespan shutdown) 呼叫。
    """
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        print("[DB] asyncpg connection pool closed.")


def get_pool() -> asyncpg.Pool:
    """取得全域 Pool 物件（確保 Pool 已建立）"""
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Call create_pool() first.")
    return _pool


async def get_db():
    """
    FastAPI Depends 注入用函式。
    每次請求從 Pool 取出一條連線，請求結束後自動歸還。

    使用方式：
        db: asyncpg.Connection = Depends(get_db)
    """
    pool = get_pool()
    async with pool.acquire() as connection:
        yield connection
