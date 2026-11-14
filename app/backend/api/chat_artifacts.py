"""
從對話訊息產生報告／簡報（`POST /api/chat/artifacts`）
====================================================
使用者在對話中勾選最多 5 則訊息（提問與回覆皆可），交給深度研究頁同一套
報告／簡報 skill 產檔：同樣的主題、篇幅設定，同樣產出 Office 檔 + HTML。

產出的檔案放進深度研究的記憶體 session（`session_store`），下載沿用
`GET /api/deep-research/runs/{sid}/artifacts/{kind}/{fmt}` —— 那支端點本來就以
(session_id, user_id) 驗證擁有權，不必再開一套下載路由。代價與深度研究頁相同：
檔案只保留 `DEEP_SEARCH_SESSION_TTL_MINUTES`（預設 120 分鐘），後端重啟就沒了。

安全與成本：
- 訊息必須屬於呼叫者自己的對話，否則等同可讀取他人對話（找不到一律 404）
- 最多 5 則；每則與總字數各有上限，超過就截斷（產檔的輸入量 = 成本）
- 配額 pre-flight；每人同時只能產一份（檢查與佔位之間沒有 await）
- 用量以 `deep_research_report`／`deep_research_deck` 記帳並帶 chat_id
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backend.api.deep_research import _spawn_cleanup
from app.backend.database.postgresql import get_db
from app.backend.deep_research.config import (
    LENGTH_HARD_MAX,
    LENGTH_SPECS,
    exceeds_hard_max,
    resolve_length,
    resolve_model,
)
from app.backend.deep_research.session import Artifact, session_store
from app.backend.deep_research.skills import FORMATS, SKILLS, generate_artifact
from app.backend.deep_research.templates import resolve_theme
from app.backend.deep_research.usage import AgentUsage, caller_for_artifact, record_agent_usage
from app.backend.module.jwt import get_current_user_id
from app.backend.module.usage_quota import assert_preflight_llm_quota

router = APIRouter(tags=["Chat"])

MAX_MESSAGES = 5
# 單則與總量上限：深度研究的回覆動輒上萬字，五則全塞進去一次就是數萬 token
MESSAGE_MAX_CHARS = 12000
TOTAL_MAX_CHARS = 40000
MAX_CITATIONS = 40

_HEARTBEAT_SECONDS = 15

# 每位使用者同時只能產一份（產一份簡報就要數十秒、數萬 token）
_active_artifacts: set[str] = set()


class ChatArtifactRequest(BaseModel):
    chat_id: UUID
    message_ids: List[UUID] = Field(..., min_length=1, max_length=MAX_MESSAGES)
    kind: str = Field(..., max_length=20)
    theme: Optional[str] = Field(default=None, max_length=40)
    length: Optional[int] = None


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n\n…（以下省略）"


def build_source(rows: List[asyncpg.Record]) -> tuple[str, str, List[Dict[str, str]], bool]:
    """
    把選取的訊息組成 skill 的輸入：(題目, 研究筆記 Markdown, 來源, 是否有截斷)。

    題目取第一則使用者提問；只選了 AI 回覆時，退而取回覆的開頭。
    來源取 AI 回覆附帶的網址（深度研究、網路搜尋的引用），依網址去重。
    """
    truncated = False
    blocks: List[str] = []
    used = 0
    for row in rows:
        speaker = "使用者提問" if row["role"] == "user" else "AI 回覆"
        body = _clip(row["content"], MESSAGE_MAX_CHARS)
        truncated = truncated or len(body) < len((row["content"] or "").strip())
        room = TOTAL_MAX_CHARS - used
        if room <= 0:
            truncated = True
            break
        if len(body) > room:
            body = body[:room] + "\n\n…（以下省略）"
            truncated = True
        used += len(body)
        blocks.append(f"## {speaker}\n\n{body}")

    first_user = next((r for r in rows if r["role"] == "user"), None)
    topic_src = (first_user or rows[0])["content"] or ""
    query = " ".join(topic_src.split())[:300] or "對話整理"

    citations: List[Dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        refs = row["context_refs"]
        if isinstance(refs, str):
            with suppress(ValueError):
                refs = json.loads(refs)
        for ref in refs if isinstance(refs, list) else []:
            url = (ref or {}).get("url") if isinstance(ref, dict) else None
            if not url or not str(url).startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            citations.append({"title": str(ref.get("title") or url)[:200], "url": str(url)[:500]})
            if len(citations) >= MAX_CITATIONS:
                break

    markdown = (
        f"以下是使用者從對話中選取的 {len(blocks)} 則訊息，請以此為素材整理。\n\n"
        + "\n\n".join(blocks)
    )
    return query, markdown, citations, truncated


@router.post("/api/chat/artifacts")
async def create_chat_artifact(
    payload: ChatArtifactRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    """
    以選取的對話訊息產生報告或簡報，SSE 回傳：status → done（含各格式下載路徑）／error。
    """
    kind = (payload.kind or "").strip()
    if kind not in SKILLS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知的產出類型：{payload.kind}")
    if exceeds_hard_max(payload.length):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"篇幅請介於 1 到 {LENGTH_HARD_MAX} 之間。")

    ids = list(dict.fromkeys(payload.message_ids))     # 去重、保留順序
    # 擁有權：訊息 → 對話 → 使用者，一次 JOIN 驗完；數量對不上就是有人亂塞 id
    rows = await db.fetch(
        """
        SELECT m.id, m.role, m.content, m.context_refs
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.chat_id = $1 AND c.user_id = $2 AND m.id = ANY($3::uuid[])
          AND m.role IN ('user', 'assistant')
        ORDER BY m.created_at, m.id
        """,
        payload.chat_id,
        current_user_id,
        ids,
    )
    if len(rows) != len(ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到選取的訊息，或訊息不屬於這則對話。")
    if not any((r["content"] or "").strip() for r in rows):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "選取的訊息沒有內容。")

    user_key = str(current_user_id)
    if user_key in _active_artifacts:
        raise HTTPException(status.HTTP_409_CONFLICT, "已有一份報告／簡報正在產生，請等它完成。")
    _active_artifacts.add(user_key)      # 檢查與佔位之間不能有 await
    try:
        await assert_preflight_llm_quota(current_user_id)
        query, markdown, citations, truncated = build_source(list(rows))
        model = resolve_model(None)
        session = await session_store.create(user_id=user_key, model=model, query=query)
        session.markdown = markdown
        session.citations = citations
        session.status = "done"
    except BaseException:
        _active_artifacts.discard(user_key)
        raise

    label = SKILLS[kind]["label"]
    length = resolve_length(kind, payload.length)
    unit = LENGTH_SPECS[kind]["unit"]
    theme = resolve_theme(payload.theme)
    usage = AgentUsage()
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            if truncated:
                await queue.put(_sse("notice", {"messages": [
                    f"選取的內容較長，已截斷到 {TOTAL_MAX_CHARS} 字以內再產生。"
                ]}))
            await queue.put(_sse("status", {"text": f"{label}撰寫中（{theme.label}風格、約 {length}{unit}）…"}))
            files = await generate_artifact(
                kind=kind,
                model=model,
                query=query,
                research_markdown=markdown,
                citations=citations,
                theme=theme,
                length=length,
                usage=usage,
            )
            session.total_tokens += usage.total_tokens
            artifact = Artifact(kind=kind, files={f.fmt: f for f in files})
            session.artifacts[kind] = artifact
            base = f"/api/deep-research/runs/{session.id}/artifacts/{kind}"
            await queue.put(_sse("done", {
                "kind": kind,
                "label": label,
                "title": query,
                "theme": theme.id,
                "length": length,
                "message_count": len(rows),
                "usage": usage.as_dict(),
                "formats": [
                    {
                        "fmt": f.fmt,
                        "label": FORMATS[f.fmt]["label"],
                        "filename": f.filename,
                        "size": f.size,
                        "download_path": f"{base}/{f.fmt}",
                    }
                    for f in files
                ],
            }))
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            await queue.put(_sse("error", {"message": f"{label}產生失敗：{type(exc).__name__}: {exc}"}))
        finally:
            _active_artifacts.discard(user_key)
            _spawn_cleanup(record_agent_usage(
                user_id=current_user_id,
                model=model,
                usage=usage,
                caller=caller_for_artifact(kind),
                session_id=session.id,
                chat_id=payload.chat_id,
            ))
            with suppress(asyncio.CancelledError):
                await queue.put(None)

    # 產檔不寫進對話，前端斷線就沒人拿得到檔案；但已經開始的模型呼叫照樣計費，
    # 因此讓它在背景跑完（記帳在 finally），而不是跟著連線一起取消
    _spawn_cleanup(produce())

    async def stream():
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield item

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = ["router", "build_source", "MAX_MESSAGES"]
