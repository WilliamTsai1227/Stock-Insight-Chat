from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

# --- 資料庫實體模型 (Database Entites / Models) ---

class ProjectModel(BaseModel):
    """
    專案頂層容器 (projects table)
    """
    id: UUID = Field(default_factory=uuid4)
    name: str
    user_id: str
    created_at: datetime = Field(default_factory=datetime.now)

class ChatModel(BaseModel):
    """
    隸屬於專案下的對話 (chats table)
    """
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    title: str
    created_at: datetime = Field(default_factory=datetime.now)

class MessageModel(BaseModel):
    """
    對話歷史紀錄 (messages table)
    透過 parent_id 實現訊息與回覆的精遲對齊
    """
    id: UUID = Field(default_factory=uuid4)
    chat_id: UUID
    parent_id: Optional[UUID] = None # 父訊息 ID，用於 Q&A 溯源
    role: str  # user / assistant
    content: str
    # 存儲結構化的 Token 資訊
    tokens: Dict[str, Any] = Field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    # 存儲檢索到的參考片段
    context_refs: Optional[List[Dict[str, Any]]] = None
    # 存儲系統元數據
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.now)

class FileModel(BaseModel):
    """
    專案或對話相關的文件 (files table)
    """
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    chat_id: Optional[UUID] = None
    file_name: str
    s3_url: str
    file_type: str  # image, pdf, etc.
    status: str  # uploading, ready, failed
    created_at: datetime = Field(default_factory=datetime.now)
