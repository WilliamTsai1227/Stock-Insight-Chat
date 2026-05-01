from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

# --- 資料庫實體模型 (Database Entites / Models) ---

class SubscriptionTierModel(BaseModel):
    """
    訂閱等級模型 (subscription_tiers table)
    """
    id: UUID = Field(default_factory=uuid4)
    name: str
    monthly_token_limit: int
    max_projects: int = 3
    features: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)

class UserModel(BaseModel):
    """
    使用者模型 (users table)
    """
    id: UUID = Field(default_factory=uuid4)
    email: str
    username: str
    password_hash: str
    status: str = "active"
    tier_id: Optional[UUID] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

class UserUsageQuotaModel(BaseModel):
    """
    使用者用量配額模型 (user_usage_quotas table)
    """
    user_id: UUID
    current_period_start: datetime
    used_tokens: int = 0
    updated_at: datetime = Field(default_factory=datetime.now)

class TokenUsageLogModel(BaseModel):
    """
    Token 使用日誌模型 (token_usage_logs table)
    """
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    message_id: Optional[UUID] = None
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.now)

    model_config = {
        "protected_namespaces": ()
    }

class RoleModel(BaseModel):
    """
    角色模型 (roles table)
    """
    id: UUID = Field(default_factory=uuid4)
    name: str
    description: Optional[str] = None

class UserRoleModel(BaseModel):
    """
    使用者角色關聯模型 (user_roles table)
    """
    user_id: UUID
    role_id: UUID

class UserSettingModel(BaseModel):
    """
    使用者設定模型 (user_settings table)
    """
    user_id: UUID
    theme: str = "dark"
    language: str = "zh-TW"
    notifications_enabled: bool = True
    settings: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=datetime.now)

class ProjectModel(BaseModel):
    """
    專案頂層容器 (projects table)
    """
    id: UUID = Field(default_factory=uuid4)
    name: str
    user_id: UUID
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

class RefreshTokenModel(BaseModel):
    """
    JWT 刷新權杖 (refresh_tokens table)

    RT Rotation 機制說明：
    - token：完整 JWT 字串，作為 DB 主查詢 key（含 jti claim 在 payload 內）
    - jti：JWT ID，即 token payload 中的 `jti` 欄位（uuid4），供稽核/索引用
    - 每次 /refresh 均原子消費（DELETE...RETURNING）舊 token 並插入新 token
    - 若同一 token 被二次使用（Reuse Attack），DELETE 回傳 0 rows，
      後端立刻撤銷該 user 所有 Session
    """
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    token: str                          # 完整 RT JWT 字串（UNIQUE）
    jti: Optional[str] = None           # 從 token payload 提取的 UUID，供索引
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.now)
