from fastapi import APIRouter
from .file import router as file_router
from .chat import router as chat_router
from .auth import router as auth_router
from .user import router as user_router
from .project import router as project_router


api_router = APIRouter()

# 在這裡「統一」註冊所有子模組，並分配它們的標籤與子路徑

# 1. 聊天核心邏輯
api_router.include_router(chat_router)

# 2. 檔案管理相關
api_router.include_router(file_router)

# 3. 使用者與認證相關
api_router.include_router(auth_router)
api_router.include_router(user_router)

# 4. 專案管理
api_router.include_router(project_router)

__all__ = ["api_router"]