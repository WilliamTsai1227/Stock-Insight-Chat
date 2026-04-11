import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

# 1. 取得連線環境變數 (目前優先讀取 Docker 內部或本地環境)
# 預設值包含 asyncpg 驅動前綴，這是高併發非同步運作的關鍵
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password123@db:5432/Stock_Insight_Chat"
)

# 2. 建立非同步引擎 (Async Engine)
# pool_size: 定連線池基礎大小 (對應高併發)
# max_overflow: 尖峰時段可擴充連線數量
# pool_recycle: 定期回收連線 (避免長時間不使用被 DB 斷線)
engine = create_async_engine(
    DATABASE_URL,
    echo=False,                # 生產環境建議設為 False 以優化效能
    pool_size=20,              # 同時維持最多 20 個活動連線
    max_overflow=10,           # 最高支持同時 30 個並發
    pool_recycle=3600,         # 每小時自動回收舊連線
    pool_pre_ping=True         # 每次取連線前都會偵測存活狀態
)

# 3. 建立非同步 Session 工廠
# 指定 class_ 為 AsyncSession，並關閉自動 Flush 提升效能
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,    # 非同步模式下不可在 Commit 後重新讀取物件
    autoflush=False
)

# 4. 定義 SQLAlchemy 模型基礎類別 (Model Base)
# 未來 models/ 下的所有實體類別都需繼承此類別
class Base(DeclarativeBase):
    pass

# 5. FastAPI 的依賴注入函式 (Dependency)
async def get_db():
    """
    透過 FastAPI 的 Depends 提供數據庫連線 Session。
    採用 context manager 確保在 API 請求結束後正確釋放連線。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
