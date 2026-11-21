"""對話公開分享：快照內容、上限截斷、公開端點的 token 驗證與擁有權。"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response

from app.backend.api import chat_share
from app.backend.api.chat_share import build_snapshot

T0 = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)


def _row(role, content, i=0, metadata=None):
    return {
        "role": role,
        "content": content,
        "metadata": json.dumps(metadata) if metadata is not None else None,
        "created_at": T0 + timedelta(minutes=i),
    }


def test_snapshot_keeps_only_role_content_time():
    rows = [_row("user", "台積電怎麼看？", 0), _row("assistant", "結論…", 1, {"model": "x", "attachments": ["a.pdf"]})]
    messages, truncated = build_snapshot(rows)
    assert truncated is False
    assert messages == [
        {"role": "user", "content": "台積電怎麼看？", "created_at": rows[0]["created_at"].isoformat()},
        {"role": "assistant", "content": "結論…", "created_at": rows[1]["created_at"].isoformat()},
    ]


def test_snapshot_skips_failed_replies_and_empty_and_unknown_roles():
    rows = [
        _row("user", "q", 0),
        _row("assistant", "深度研究執行失敗：timeout", 1, {"error": "timeout"}),
        _row("assistant", "   ", 2),
        _row("system", "internal", 3),
        _row("assistant", "ok", 4),
    ]
    messages, _ = build_snapshot(rows)
    assert [m["content"] for m in messages] == ["q", "ok"]


def test_snapshot_truncates_oldest_first(monkeypatch):
    monkeypatch.setattr(chat_share, "SNAPSHOT_MAX_MESSAGES", 2)
    rows = [_row("user", f"m{i}", i) for i in range(5)]
    messages, truncated = build_snapshot(rows)
    assert truncated is True
    assert [m["content"] for m in messages] == ["m3", "m4"]


def test_snapshot_char_budget(monkeypatch):
    monkeypatch.setattr(chat_share, "SNAPSHOT_MAX_CHARS", 10)
    rows = [_row("user", "a" * 6, 0), _row("assistant", "b" * 6, 1)]
    messages, truncated = build_snapshot(rows)
    assert truncated is True and [m["content"] for m in messages] == ["b" * 6]


class _FakeDB:
    def __init__(self, chat=None, share=None):
        self.chat, self.share, self.calls = chat, share, []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM chats" in sql:
            return self.chat
        return self.share

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


@pytest.mark.parametrize("token", ["short", "../../etc", "a" * 65, "abc$%^" * 8])
def test_public_endpoint_rejects_malformed_token_without_db(token):
    db = _FakeDB()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_share.read_public_share(token, Response(), db=db))
    assert exc.value.status_code == 404
    assert db.calls == []


def test_public_endpoint_unknown_token_404():
    db = _FakeDB(share=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_share.read_public_share("a" * 43, Response(), db=db))
    assert exc.value.status_code == 404


def test_public_endpoint_sets_noindex_and_decodes_snapshot():
    share = {
        "title": "t",
        "snapshot": json.dumps([{"role": "user", "content": "q", "created_at": None}]),
        "message_count": 1,
        "truncated": False,
        "updated_at": T0,
    }
    response = Response()
    out = asyncio.run(chat_share.read_public_share("a" * 43, response, db=_FakeDB(share=share)))
    assert out["data"]["messages"][0]["content"] == "q"
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_owner_endpoints_404_for_other_users_chat():
    db = _FakeDB(chat=None)
    for fn in (chat_share.get_share_status, chat_share.create_or_refresh_share, chat_share.revoke_share):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(fn(uuid.uuid4(), db=db, current_user_id=uuid.uuid4()))
        assert exc.value.status_code == 404


def test_create_share_rejects_empty_chat():
    db = _FakeDB(chat={"id": uuid.uuid4(), "title": "t"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_share.create_or_refresh_share(uuid.uuid4(), db=db, current_user_id=uuid.uuid4()))
    assert exc.value.status_code == 422
