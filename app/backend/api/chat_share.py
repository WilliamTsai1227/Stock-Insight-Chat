"""
對話公開分享（唯讀快照）
========================

- `GET    /api/chat/{chat_id}/share`  目前分享狀態（本人）
- `POST   /api/chat/{chat_id}/share`  建立分享；已存在時更新為最新快照（連結不變）
- `DELETE /api/chat/{chat_id}/share`  停止分享，連結立即失效
- `GET    /api/share/{token}`         公開讀取（免登入）

設計重點：
- 分享的是**快照**：建立／更新當下的訊息複製進 `chat_shares.snapshot`，之後的新訊息不會外流，
  使用者要分享新內容時按「更新」。
- 快照只存 role / content / created_at；不帶 tokens、context_refs、metadata（可能含附件檔名、
  模型與錯誤細節），失敗的回覆（metadata.error）也排除。
- token = `secrets.token_urlsafe(32)`，不可猜測；公開端點先驗格式再查 DB，查無一律 404。
- 公開回應帶 `X-Robots-Tag: noindex`，避免被搜尋引擎收錄。
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user_id

router = APIRouter(tags=["Chat Share"])

# 快照上限：超過時保留「最新」的訊息（分享通常是為了最後的結論）
SNAPSHOT_MAX_MESSAGES = 200
SNAPSHOT_MAX_CHARS = 400_000

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found or you don't have permission.")


def _load_json(value: Any) -> Any:
    """asyncpg 沒註冊 JSONB codec 時回字串，有註冊時回物件；兩種都接。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def build_snapshot(rows: List[Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """
    把訊息列（時間舊→新）轉成快照；回傳 (messages, truncated)。

    排除失敗的回覆與空內容；超過則數或字數上限時從最舊的開始丟。
    """
    usable: List[Dict[str, Any]] = []
    for row in rows:
        role = row["role"]
        content = (row["content"] or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        meta = _load_json(row["metadata"]) or {}
        if isinstance(meta, dict) and meta.get("error"):
            continue
        usable.append({
            "role": role,
            "content": content,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    kept: List[Dict[str, Any]] = []
    total = 0
    for msg in reversed(usable):
        if len(kept) >= SNAPSHOT_MAX_MESSAGES or total + len(msg["content"]) > SNAPSHOT_MAX_CHARS:
            break
        kept.append(msg)
        total += len(msg["content"])
    kept.reverse()
    return kept, len(kept) < len(usable)


def _status_payload(row: Optional[Any]) -> Dict[str, Any]:
    if row is None:
        return {"shared": False}
    return {
        "shared": True,
        "token": row["token"],
        "path": f"/share?t={row['token']}",
        "message_count": row["message_count"],
        "truncated": row["truncated"],
        "view_count": row["view_count"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def _assert_owner(db: asyncpg.Connection, chat_id: UUID, user_id: UUID) -> Any:
    chat = await db.fetchrow("SELECT id, title FROM chats WHERE id = $1 AND user_id = $2", chat_id, user_id)
    if chat is None:
        raise _not_found()
    return chat


_SHARE_COLUMNS = "token, message_count, truncated, view_count, created_at, updated_at"


@router.get("/api/chat/{chat_id}/share")
async def get_share_status(
    chat_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    await _assert_owner(db, chat_id, current_user_id)
    row = await db.fetchrow(f"SELECT {_SHARE_COLUMNS} FROM chat_shares WHERE chat_id = $1", chat_id)
    return {"status": "success", "data": _status_payload(row)}


@router.post("/api/chat/{chat_id}/share")
async def create_or_refresh_share(
    chat_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    """建立分享或更新快照。已有分享時沿用同一個 token（已傳出去的連結繼續有效）。"""
    chat = await _assert_owner(db, chat_id, current_user_id)
    rows = await db.fetch(
        """
        SELECT role, content, metadata, created_at
        FROM messages
        WHERE chat_id = $1
        ORDER BY created_at ASC, id ASC
        """,
        chat_id,
    )
    messages, truncated = build_snapshot(rows)
    if not messages:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "這則對話還沒有可以分享的內容。")

    row = await db.fetchrow(
        f"""
        INSERT INTO chat_shares (token, chat_id, user_id, title, snapshot, message_count, truncated)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        ON CONFLICT (chat_id) DO UPDATE SET
            title = EXCLUDED.title,
            snapshot = EXCLUDED.snapshot,
            message_count = EXCLUDED.message_count,
            truncated = EXCLUDED.truncated,
            updated_at = CURRENT_TIMESTAMP
        RETURNING {_SHARE_COLUMNS}
        """,
        secrets.token_urlsafe(32),
        chat_id,
        current_user_id,
        chat["title"],
        json.dumps(messages, ensure_ascii=False),
        len(messages),
        truncated,
    )
    return {"status": "success", "data": _status_payload(row)}


@router.delete("/api/chat/{chat_id}/share")
async def revoke_share(
    chat_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    await _assert_owner(db, chat_id, current_user_id)
    await db.execute("DELETE FROM chat_shares WHERE chat_id = $1", chat_id)
    return {"status": "success", "data": {"shared": False}}


@router.get("/api/share/{token}")
async def read_public_share(
    token: str,
    response: Response,
    db: asyncpg.Connection = Depends(get_db),
):
    """公開讀取（免登入）。格式不對、查無、已停止分享一律 404，不區分原因。"""
    if not _TOKEN_PATTERN.match(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "這個分享連結不存在或已停止分享。")
    row = await db.fetchrow(
        """
        UPDATE chat_shares SET view_count = view_count + 1
        WHERE token = $1
        RETURNING title, snapshot, message_count, truncated, updated_at
        """,
        token,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "這個分享連結不存在或已停止分享。")

    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return {
        "status": "success",
        "data": {
            "title": row["title"],
            "messages": _load_json(row["snapshot"]) or [],
            "message_count": row["message_count"],
            "truncated": row["truncated"],
            "updated_at": row["updated_at"].isoformat(),
        },
    }


__all__ = ["router", "build_snapshot"]
