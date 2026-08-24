"""
深度研究（Deep Research）
────────────────────────
以 OpenAI Agents SDK 的 hosted tools（WebSearchTool / FileSearchTool）執行
一次性的研究任務，並可再把研究結果交給「報告」與「簡報」兩個 skill 產出可下載檔案。

MVP 定位（功能驗證）：
- 不寫資料庫、不存 S3；session 只放記憶體，逾時或重啟即消失。
- 上傳的文件只在研究期間存在於 OpenAI vector store，研究一結束立刻刪除。
"""

from .config import (
    DEEP_SEARCH_DEFAULT_MODEL,
    MODEL_CATALOG,
    resolve_model,
)
from .session import ResearchSession, session_store

__all__ = [
    "DEEP_SEARCH_DEFAULT_MODEL",
    "MODEL_CATALOG",
    "resolve_model",
    "ResearchSession",
    "session_store",
]
