"""
深度研究：記憶體 session 存放區。

MVP 刻意不落地任何東西 —— 研究結果與產出的檔案都只放在這裡，
後端重啟或 TTL 到期就消失（前端重新整理即失去 session_id，效果等同）。

因為只在單一 process 的 event loop 內存取，用 dict + 一把 asyncio.Lock 即可；
若之後要跑多 worker，這層要換成 Redis。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import MAX_SESSIONS_PER_USER, SESSION_TTL_MINUTES


@dataclass
class ArtifactFile:
    """
    一份可下載的檔案。

    內容一律存 bytes：docx / pptx 本來就是二進位，HTML 也統一先編成 UTF-8，
    下載端點就不必為兩種型別分岔。
    """

    fmt: str                  # "html" | "docx" | "pptx"
    filename: str
    media_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class Artifact:
    """
    一次 skill 產出：同一份內容的數種格式。

    報告是 docx + html、簡報是 pptx + html —— 兩種都留著，是因為 Office 檔
    可以直接改、HTML 檔則能在瀏覽器裡預覽與離線放映，用途不重疊。
    """

    kind: str                              # "report" | "deck"
    files: Dict[str, ArtifactFile]         # fmt → 檔案；插入順序的第一個是主要格式
    created_at: float = field(default_factory=time.time)

    @property
    def primary(self) -> ArtifactFile:
        """主要格式（報告是 docx、簡報是 pptx）；前端的主要下載按鈕指向它。"""
        return next(iter(self.files.values()))

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.values())


@dataclass
class ResearchSession:
    id: str
    user_id: str
    model: str
    query: str
    created_at: float = field(default_factory=time.time)
    status: str = "running"           # running | done | error
    error: Optional[str] = None
    # 研究產出
    markdown: str = ""
    citations: List[Dict[str, str]] = field(default_factory=list)
    source_names: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    elapsed_ms: int = 0
    # 研究 + 每次產檔的累計 token；實際計費以 token_usage_logs 為準，
    # 這個欄位只是讓前端與 log 看得到「這個 session 花了多少」
    total_tokens: int = 0
    # 產出的檔案（kind → Artifact）
    artifacts: Dict[str, Artifact] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.id,
            "model": self.model,
            "status": self.status,
            "sources": self.source_names,
            "tools_used": self.tools_used,
            "citations": self.citations,
            "elapsed_ms": self.elapsed_ms,
            "total_tokens": self.total_tokens,
            "artifacts": [
                {
                    "kind": a.kind,
                    "filename": a.primary.filename,
                    "size": a.size,
                    "formats": list(a.files),
                }
                for a in self.artifacts.values()
            ],
        }


class SessionStore:
    def __init__(self, ttl_minutes: int = SESSION_TTL_MINUTES) -> None:
        self._ttl_seconds = ttl_minutes * 60
        self._sessions: Dict[str, ResearchSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, user_id: str, model: str, query: str) -> ResearchSession:
        session = ResearchSession(
            id=uuid.uuid4().hex,
            user_id=user_id,
            model=model,
            query=query,
        )
        async with self._lock:
            self._evict_expired_locked()
            self._evict_surplus_for_user_locked(user_id)
            self._sessions[session.id] = session
        return session

    async def get(self, session_id: str, user_id: str) -> Optional[ResearchSession]:
        """取回 session；不存在、已逾時或不屬於這位使用者一律當作不存在。"""
        async with self._lock:
            self._evict_expired_locked()
            session = self._sessions.get(session_id)
            if session is None or session.user_id != user_id:
                return None
            return session

    async def drop(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    # ── 內部：呼叫時必須已持有 self._lock ──────────────────────
    def _evict_expired_locked(self) -> None:
        deadline = time.time() - self._ttl_seconds
        for sid in [s.id for s in self._sessions.values() if s.created_at < deadline]:
            self._sessions.pop(sid, None)

    def _evict_surplus_for_user_locked(self, user_id: str) -> None:
        owned = sorted(
            (s for s in self._sessions.values() if s.user_id == user_id),
            key=lambda s: s.created_at,
        )
        # 留一個位子給即將建立的新 session
        while len(owned) >= MAX_SESSIONS_PER_USER:
            self._sessions.pop(owned.pop(0).id, None)


session_store = SessionStore()
