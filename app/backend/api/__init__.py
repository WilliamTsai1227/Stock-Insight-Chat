from fastapi import APIRouter
from .file import router as file_router
from .chat import router as chat_router
from .auth import router as auth_router
from .user import router as user_router
from .project import router as project_router
from .explore import router as explore_router
from .deep_research import router as deep_research_router
from .mcp import router as mcp_router
from .notes import router as notes_router
from .market_feed import router as market_feed_router
from .portfolio import router as portfolio_router
from .chat_deep_research import router as chat_deep_research_router
from .chat_artifacts import router as chat_artifacts_router
from .skills import router as skills_router


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

# 5. 探索（Kinetic Charts 反向代理，KINETIC_UPSTREAM 未設定時回 404）
api_router.include_router(explore_router)

# 6. 深度研究（OpenAI Agents SDK：Web Search / File Search）
api_router.include_router(deep_research_router)

# 7. 外部 MCP Server（第一階段：本專案作為 MCP Client）
api_router.include_router(mcp_router)

# 8. 筆記（把對話整理成筆記）
api_router.include_router(notes_router)

# 9. 新聞瀏覽／AI 分析瀏覽（MongoDB，沿用 MONGO_URI，可用 FEED_MONGO_URI 覆寫）
api_router.include_router(market_feed_router)

# 投資組合排程分析（庫存／觀察清單 → Zentra stock-suite）
api_router.include_router(portfolio_router)

# 對話中的深度研究（聊天模式第三個選項，可上傳檔案）
api_router.include_router(chat_deep_research_router)

# 從對話訊息產生報告／簡報（最多 5 則，沿用深度研究的 skill 與下載路由）
api_router.include_router(chat_artifacts_router)

# 金融分析 skill 目錄（輸入框 @dcf、@財報解讀…；股市 Agent 與深度研究共用）
api_router.include_router(skills_router)

__all__ = ["api_router"]
