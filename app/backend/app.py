from dotenv import load_dotenv
import os
from contextlib import asynccontextmanager

# 0. 載入環境變數 (務必放在最頂部，確保所有模組都能讀取到)
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.backend.api import api_router
from app.backend.database.postgresql import create_pool, close_pool


# 1. 使用 lifespan 管理 asyncpg Connection Pool 生命週期
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- 啟動時建立 Connection Pool ---
    await create_pool()
    yield
    # --- 關閉時釋放所有連線 ---
    await close_pool()


# 2. 初始化 FastAPI 應用程式
app = FastAPI(
    title="Stock-Insight-Chat API",
    description="股市生成式聊天對話應用後端",
    version="0.2.0",
    lifespan=lifespan
)

# 3. 設定 CORS (跨域資源共享)
# allow_origins 不可與 allow_credentials=True 同時使用萬用字元 *，
# 瀏覽器安全規範要求帶 credentials 的請求必須指定明確 origin。
_ALLOWED_ORIGINS = os.getenv(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost,http://localhost:80,http://127.0.0.1,http://127.0.0.1:80"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. 註冊所有子路由
app.include_router(api_router)

# 5. 健康檢查接口
@app.get("/", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "message": "Stock-Insight-Chat Backend is running."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
