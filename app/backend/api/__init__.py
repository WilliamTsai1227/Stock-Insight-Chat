from fastapi import APIRouter
from .file import router as file_router
from .chat import router as chat_router

# 建立全局路由容器，並統一加上 "/api" 前綴
api_router = APIRouter(prefix="/api")

# 在這裡「統一」註冊所有子模組，並分配它們的標籤與子路徑
# 1. 聊天核心邏輯：路徑為 /api/getAIResponse
api_router.include_router(chat_router, tags=["Chat Core"])

# 2. 檔案管理相關：路徑為 /api/files/upload 與 /api/files/{id}
api_router.include_router(file_router, prefix="/files", tags=["File Management"])

__all__ = ["api_router"]