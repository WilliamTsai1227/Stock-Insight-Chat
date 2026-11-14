"""從對話訊息產生報告／簡報：素材組裝（截斷、題目、來源過濾）。"""

import json

from app.backend.api import chat_artifacts
from app.backend.api.chat_artifacts import build_source


def _row(role, content, refs=None):
    return {"role": role, "content": content, "context_refs": refs}


def test_query_comes_from_first_user_message():
    rows = [_row("assistant", "前文"), _row("user", "台積電  2nm\n進度？"), _row("assistant", "回答")]
    query, md, _, truncated = build_source(rows)
    assert query == "台積電 2nm 進度？"
    assert "## 使用者提問" in md and md.count("## AI 回覆") == 2
    assert truncated is False


def test_falls_back_to_assistant_when_no_user_selected():
    query, _, _, _ = build_source([_row("assistant", "只選了回覆")])
    assert query == "只選了回覆"


def test_long_messages_are_clipped_and_flagged(monkeypatch):
    monkeypatch.setattr(chat_artifacts, "MESSAGE_MAX_CHARS", 100)
    monkeypatch.setattr(chat_artifacts, "TOTAL_MAX_CHARS", 150)
    rows = [_row("user", "q" * 500), _row("assistant", "a" * 500), _row("assistant", "b" * 500)]
    _, md, _, truncated = build_source(rows)
    assert truncated is True
    assert "b" * 10 not in md            # 第三則已超出總量預算
    assert md.count("（以下省略）") >= 2


def test_citations_only_http_dedup_and_capped(monkeypatch):
    monkeypatch.setattr(chat_artifacts, "MAX_CITATIONS", 2)
    refs = [
        {"title": "A", "url": "https://a.example"},
        {"title": "A dup", "url": "https://a.example"},
        {"title": "evil", "url": "javascript:alert(1)"},
        {"title": "no url"},
        {"title": "B", "url": "http://b.example"},
        {"title": "C", "url": "https://c.example"},
    ]
    _, _, cites, _ = build_source([_row("assistant", "x", json.dumps(refs))])
    assert [c["url"] for c in cites] == ["https://a.example", "http://b.example"]


def test_max_messages_is_five():
    assert chat_artifacts.MAX_MESSAGES == 5
    from pydantic import ValidationError
    import uuid
    import pytest
    with pytest.raises(ValidationError):
        chat_artifacts.ChatArtifactRequest(chat_id=uuid.uuid4(), message_ids=[uuid.uuid4() for _ in range(6)], kind="deck")
    with pytest.raises(ValidationError):
        chat_artifacts.ChatArtifactRequest(chat_id=uuid.uuid4(), message_ids=[], kind="deck")
