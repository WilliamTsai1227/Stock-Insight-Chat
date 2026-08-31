"""
深度研究（Deep Research）
────────────────────────
以 OpenAI Agents SDK 的 hosted tools（WebSearchTool / FileSearchTool）執行
一次性的研究任務，並可再把研究結果交給「報告」與「簡報」兩個 skill 產出可下載檔案。

MVP 定位（功能驗證）：
- 研究結果與產出檔案不落地：session 只放記憶體，逾時或重啟即消失，也不存 S3。
- 上傳的文件只在研究期間存在於 OpenAI vector store，研究一結束立刻刪除。
- 唯一會寫進 PostgreSQL 的是 token 用量（`usage.py` → `module/token_usage`）：
  深度研究與聊天吃同一份月配額，不記帳就等於開一個繞過配額的後門。

模型固定為 `MODEL_CATALOG` 白名單內的 id（目前只有 gpt-5.6-luna），
理由見 `config.py` 的註解：換模型等於換費率，而費率表與配額扣點都綁在那個 id。
"""

from .config import (
    ALLOWED_MODEL_IDS,
    DEEP_SEARCH_DEFAULT_MODEL,
    MODEL_CATALOG,
    resolve_model,
)
from .session import ResearchSession, session_store
from .usage import AgentUsage, record_agent_usage

__all__ = [
    "ALLOWED_MODEL_IDS",
    "DEEP_SEARCH_DEFAULT_MODEL",
    "MODEL_CATALOG",
    "resolve_model",
    "ResearchSession",
    "session_store",
    "AgentUsage",
    "record_agent_usage",
]
