from fastapi import APIRouter
from .file import router as file_router
from .chat import router as chat_router
from .auth import router as auth_router
from .user import router as user_router

# 建立全局路由容器，並統一加上 "/api" 前綴
api_router = APIRouter(prefix="/api")

# 在這裡「統一」註冊所有子模組，並分配它們的標籤與子路徑

# 1. 聊天核心邏輯
api_router.include_router(chat_router)

# 2. 檔案管理相關
api_router.include_router(file_router)

# 3. 使用者與認證相關
api_router.include_router(auth_router)
api_router.include_router(user_router)

__all__ = ["api_router"]