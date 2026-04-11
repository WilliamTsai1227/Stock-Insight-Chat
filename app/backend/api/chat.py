from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from uuid import UUID
from ..models import MessageModel, ChatModel

# 這裡移除 prefix="/api" 與 tags=["Chat"]，改由 __init__.py 統一管理
router = APIRouter()

# --- 請求相關格式 (Request Schemas) ---

class AttachmentSchema(BaseModel):
    image_ids: List[str] = []
    file_ids: List[str] = []
    use_project_files: bool = True

class AgentConfigSchema(BaseModel):
    tool_choice: str = "auto"
    enabled_tools: List[str] = ["news_vector_search", "AI_analysis_vector_search", "file_retriever"]

class MessageRequest(BaseModel):
    project_id: UUID
    chat_id: UUID
    query: str  # 已根據 api_spec.md 更新為 query
    attachments: AttachmentSchema
    agent_config: AgentConfigSchema

# --- 回應相關格式 (Response Schemas) ---

class SourceItem(BaseModel):
    id: str
    type: str  # e.g., file, image, news, ai_analysis
    title: str
    chunks: Optional[int] = None
    s3_url: Optional[str] = None

class UsageInfo(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    is_cached: bool = False

class MessageResponseData(BaseModel):
    message_id: str
    ai_response: str
    chat_id: UUID
    project_id: UUID
    sources: List[SourceItem] = []
    usage: UsageInfo

class MessageResponse(BaseModel):
    status: str = "success"
    data: MessageResponseData

# --- API 接口 ---

@router.post("/getAIResponse", response_model=MessageResponse)
async def get_ai_response(request: MessageRequest):
    """
    實作規格書 ## 2. 訊息發送接口
    1. 驗證權限與資料完整性
    2. 根據 chat_id 檢索歷史對話
    3. 組裝上下文 (專案文件 + 歷史 + Query)
    4. 啟動 Agent 調用核心工具 (Qdrant/MongoDB/S3)
    5. 回傳最終生成的分析內容
    """
    try:
        # TODO: 這裡未來將實作 Agent 執行流程 (詳見 api_spec.md 流程圖)
        # 示範回傳內容，內容需符合 MessageResponse 結構
        mock_response = {
            "status": "success",
            "data": {
                "message_id": "postgresql_id_example",
                "ai_response": "根據分析，台積電在 2021-2023 年間成長顯著...",
                "chat_id": request.chat_id,
                "project_id": request.project_id,
                "sources": [
                    { "id": "file_001", "type": "file", "title": "2024Q3營收報告.pdf", "chunks": 2 },
                    { "id": "news_001", "type": "news", "title": "台積電近期新聞" }
                ],
                "usage": {
                    "prompt_tokens": 120500,
                    "completion_tokens": 500,
                    "is_cached": True
                }
            }
        }
        return mock_response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
