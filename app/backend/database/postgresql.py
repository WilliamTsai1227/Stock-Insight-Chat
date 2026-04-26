"""
PostgreSQL Connection Pool (asyncpg)
=====================================
使用原生 asyncpg 套件建立高效能連線池，完全取代 SQLAlchemy。

- pool_min_size: 最少維持的閒置連線數
- pool_max_size: 最多允許的並發連線數
- max_inactive_connection_lifetime: 超過此秒數的閒置連線將被回收
"""

import os
import asyncpg

# --- 全域 Connection Pool 物件 ---
_pool: asyncpg.Pool | None = None


def _get_dsn() -> str:
    """從環境變數讀取 PostgreSQL 連線字串 (asyncpg 格式，無需 +asyncpg 前綴)"""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:password123@db:5432/Stock_Insight_Chat"
    ).replace("postgresql+asyncpg://", "postgresql://")


async def create_pool() -> None:
    """
    建立 asyncpg Connection Pool。
    應在 FastAPI 啟動時 (lifespan startup) 呼叫。
    """
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=_get_dsn(),
        min_size=5,              # 最少維持 5 條閒置連線
        max_size=20,             # 最多 20 條並發連線
        max_inactive_connection_lifetime=3600.0,  # 閒置超過 1 小時自動回收
        command_timeout=30.0,    # 單一 SQL 指令逾時 30 秒
    )
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
