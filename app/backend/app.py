from dotenv import load_dotenv
import os

# 0. 載入環境變數 (務必放在最頂部，確保所有模組都能讀取到)
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.backend.api import api_router  # 統一使用 app.backend 開頭

# 1. 初始化 FastAPI 應用程式
app = FastAPI(
    title="Stock-Insight-Chat API",
    description="股市生成式聊天對話應用後端",
    version="0.1.0"
)

# 2. 設定 CORS (跨域資源共享)
# 確保前端可以正常與後端溝通
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 開發環境允許所有
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 註冊所有子路由 (API Router)
# 這會一次載入 file.py, chat.py 等所有 Router 模塊
app.include_router(api_router)

# 4. 健康檢查接口
@app.get("/", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "message": "Stock-Insight-Chat Backend is running."
    }

if __name__ == "__main__":
    import uvicorn
    # 啟動開發伺服器
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
