"""
Notes API（筆記）
================
把一段對話交給 LLM 整理成筆記，存進使用者自己的筆記頁。

資料模型（見 database/migrations/V009__notes.sql）：
- note_pages：筆記頁，一個使用者可有多頁，每頁是一塊獨立空間
- notes     ：單則筆記；canvas_* 欄位保留給之後的白板視圖，本階段不寫入

安全設計：
- 所有端點需登入；user_id 一律取自 JWT，前端傳的一概不信
- 每個查詢都帶 user_id 當條件，找不到一律 404（不區分「不存在」與「不屬於你」，
  避免洩漏其他人的資源 id）
- 來源訊息必須屬於呼叫者自己的對話，否則等同可讀取他人對話內容
"""

import json
import re
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user
from app.backend.module.token_usage import record_token_usage
from app.backend.module.usage_quota import assert_preflight_llm_quota

router = APIRouter(tags=["Notes"])


# ── 限制 ──────────────────────────────────────────────────────────
_PAGE_TITLE_MAX = 100
_NOTE_TITLE_MAX = 200
_NOTE_CONTENT_MAX = 20000
#  單次最多整理幾則訊息；再多就該拆成多則筆記，而不是塞爆 context
_MAX_SOURCE_MESSAGES = 50
#  送進 LLM 的來源字數上限（超過就從最舊的開始截斷）
_MAX_SOURCE_CHARS = 48000
#  每位使用者的頁數 / 每頁筆記數上限，避免被當成免費儲存空間
_MAX_PAGES_PER_USER = 50
_MAX_NOTES_PER_PAGE = 500

_DEFAULT_PAGE_TITLE = "我的筆記"

# 頁面標題白名單（與 project.py 的 name 規則一致）。
# 筆記「內容」不套這個 —— 它是 LLM 產生的 Markdown，前端以純文字渲染。
_VALID_TITLE_PATTERN = re.compile(
    r'^[\w'
    r'一-鿿'
    r'㐀-䶿'
    r'぀-ゟ'
    r'゠-ヿ'
    r'＀-￯'
    r'À-ɏ'
    r'\s\-_.()（）【】「」『』·'
    r']+$',
    re.UNICODE,
)

# 控制字元（LLM 產出的標題偶爾會夾帶換行）
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _validate_page_title(title: str) -> str:
    stripped = (title or "").strip()
    if not stripped:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "頁面名稱不可為空。")
    if len(stripped) > _PAGE_TITLE_MAX:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"頁面名稱不可超過 {_PAGE_TITLE_MAX} 字（目前 {len(stripped)}）。",
        )
    if not _VALID_TITLE_PATTERN.match(stripped):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "頁面名稱含有不允許的字元（例如 < > \" ' ; / \\ & $ `）。",
        )
    return stripped


def _clean_note_title(title: str) -> str:
    """筆記標題可能來自 LLM，只做去控制字元與長度裁切，不套白名單。"""
    cleaned = _CONTROL_CHARS.sub("", (title or "").strip()).strip()
    if not cleaned:
        cleaned = "未命名筆記"
    return cleaned[:_NOTE_TITLE_MAX]


# ── Schema ────────────────────────────────────────────────────────

class CreatePageRequest(BaseModel):
    title: str


class RenamePageRequest(BaseModel):
    title: str


class GenerateNoteRequest(BaseModel):
    chat_id: UUID
    message_ids: List[UUID] = Field(min_length=1)
    # 省略時寫進預設頁（沒有就自動建一頁）
    page_id: Optional[UUID] = None
    # 使用者想強調的重點，可省略
    instruction: Optional[str] = None


class UpdateNoteRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    page_id: Optional[UUID] = None


# ── 共用 ──────────────────────────────────────────────────────────

def _note_row(row: asyncpg.Record) -> Dict[str, Any]:
    raw_ids = row["source_message_ids"]
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except ValueError:
            raw_ids = None
    return {
        "id": str(row["id"]),
        "page_id": str(row["page_id"]),
        "title": row["title"],
        "content": row["content"],
        "source_chat_id": str(row["source_chat_id"]) if row["source_chat_id"] else None,
        "source_message_ids": raw_ids or [],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def _ensure_default_page(db: asyncpg.Connection, user_id: UUID) -> UUID:
    """取得使用者的第一頁；完全沒有頁時自動建一頁。"""
    existing = await db.fetchval(
        "SELECT id FROM note_pages WHERE user_id = $1 ORDER BY position, created_at LIMIT 1",
        user_id,
    )
    if existing:
        return existing
    return await db.fetchval(
        "INSERT INTO note_pages (user_id, title, position) VALUES ($1, $2, 0) RETURNING id",
        user_id,
        _DEFAULT_PAGE_TITLE,
    )


async def _assert_page_owned(db: asyncpg.Connection, page_id: UUID, user_id: UUID) -> None:
    owned = await db.fetchval(
        "SELECT 1 FROM note_pages WHERE id = $1 AND user_id = $2", page_id, user_id
    )
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該筆記頁。")


# ── 筆記頁 ────────────────────────────────────────────────────────

@router.get("/api/notes/pages")
async def list_pages(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """列出筆記頁（含每頁筆記數）。"""
    rows = await db.fetch(
        """
        SELECT p.id, p.title, p.position, p.created_at, p.updated_at,
               COUNT(n.id)::int AS note_count
        FROM note_pages p
        LEFT JOIN notes n ON n.page_id = p.id
        WHERE p.user_id = $1
        GROUP BY p.id
        ORDER BY p.position, p.created_at
        """,
        current_user["id"],
    )
    return {"status": "success", "data": {"pages": [
        {
            "id": str(r["id"]), "title": r["title"], "position": r["position"],
            "note_count": r["note_count"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        } for r in rows
    ]}}


@router.post("/api/notes/pages", status_code=status.HTTP_201_CREATED)
async def create_page(
    request: CreatePageRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """新增筆記頁。"""
    user_id = current_user["id"]
    title = _validate_page_title(request.title)

    count = await db.fetchval("SELECT COUNT(*) FROM note_pages WHERE user_id = $1", user_id)
    if count >= _MAX_PAGES_PER_USER:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"筆記頁數已達上限（{_MAX_PAGES_PER_USER} 頁）。",
        )

    row = await db.fetchrow(
        """
        INSERT INTO note_pages (user_id, title, position)
        VALUES ($1, $2, COALESCE((SELECT MAX(position) + 1 FROM note_pages WHERE user_id = $1), 0))
        RETURNING id, title, position, created_at, updated_at
        """,
        user_id, title,
    )
    return {"status": "success", "data": {
        "id": str(row["id"]), "title": row["title"], "position": row["position"],
        "note_count": 0,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }}


@router.patch("/api/notes/pages/{page_id}")
async def rename_page(
    page_id: UUID,
    request: RenamePageRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """重新命名筆記頁。"""
    title = _validate_page_title(request.title)
    row = await db.fetchrow(
        """
        UPDATE note_pages SET title = $1, updated_at = CURRENT_TIMESTAMP
        WHERE id = $2 AND user_id = $3
        RETURNING id, title, position, created_at, updated_at
        """,
        title, page_id, current_user["id"],
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該筆記頁。")
    return {"status": "success", "data": {
        "id": str(row["id"]), "title": row["title"], "position": row["position"],
    }}


@router.delete("/api/notes/pages/{page_id}")
async def delete_page(
    page_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """刪除筆記頁（底下的筆記一併 CASCADE 刪除）。"""
    deleted = await db.fetchval(
        "DELETE FROM note_pages WHERE id = $1 AND user_id = $2 RETURNING id",
        page_id, current_user["id"],
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該筆記頁。")
    return {"status": "success", "data": {"id": str(deleted)}}


# ── 筆記 ──────────────────────────────────────────────────────────

@router.get("/api/notes")
async def list_notes(
    page_id: Optional[UUID] = Query(default=None),
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """列出筆記；帶 page_id 就只列該頁。"""
    user_id = current_user["id"]
    if page_id is not None:
        await _assert_page_owned(db, page_id, user_id)
        rows = await db.fetch(
            """
            SELECT id, page_id, title, content, source_chat_id, source_message_ids,
                   created_at, updated_at
            FROM notes WHERE page_id = $1 AND user_id = $2
            ORDER BY created_at DESC
            """,
            page_id, user_id,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, page_id, title, content, source_chat_id, source_message_ids,
                   created_at, updated_at
            FROM notes WHERE user_id = $1 ORDER BY created_at DESC
            """,
            user_id,
        )
    return {"status": "success", "data": {"notes": [_note_row(r) for r in rows]}}


@router.patch("/api/notes/{note_id}")
async def update_note(
    note_id: UUID,
    request: UpdateNoteRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """編輯筆記標題／內容，或搬到另一頁。"""
    user_id = current_user["id"]
    if request.page_id is not None:
        await _assert_page_owned(db, request.page_id, user_id)
    if request.content is not None and len(request.content) > _NOTE_CONTENT_MAX:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"筆記內容不可超過 {_NOTE_CONTENT_MAX} 字。",
        )

    # COALESCE：沒帶的欄位維持原值，不必為每種組合各寫一句 SQL
    row = await db.fetchrow(
        """
        UPDATE notes SET
            title      = COALESCE($1, title),
            content    = COALESCE($2, content),
            page_id    = COALESCE($3, page_id),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = $4 AND user_id = $5
        RETURNING id, page_id, title, content, source_chat_id, source_message_ids,
                  created_at, updated_at
        """,
        _clean_note_title(request.title) if request.title is not None else None,
        request.content,
        request.page_id,
        note_id, user_id,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該筆記。")
    return {"status": "success", "data": _note_row(row)}


@router.delete("/api/notes/{note_id}")
async def delete_note(
    note_id: UUID,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """刪除筆記。"""
    deleted = await db.fetchval(
        "DELETE FROM notes WHERE id = $1 AND user_id = $2 RETURNING id",
        note_id, current_user["id"],
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該筆記。")
    return {"status": "success", "data": {"id": str(deleted)}}


# ── 由對話生成筆記 ────────────────────────────────────────────────

_NOTE_SYSTEM_PROMPT = """你是一位擅長把對話整理成可複習筆記的助理。

使用者會給你一段他與 AI 的對話節錄，請整理成一則結構清楚的中文筆記。

輸出格式（務必遵守）：
- 第一行是 `# ` 開頭的標題，一句話概括這則筆記的主題，20 字以內
- 之後是筆記正文，使用 Markdown

正文要求：
- 用小標題與條列整理重點，不要照抄原對話，也不要逐句複述
- 保留具體的數字、名詞、結論；這些是筆記的價值所在
- 對話中若有未解決的問題或待確認事項，獨立列一段「待確認」
- 不要加入對話裡沒有的資訊，不要臆測
- 不要寫「根據以上對話」這類贅詞，直接寫結論"""


def _build_source_text(rows: List[asyncpg.Record]) -> str:
    """把訊息組成送進 LLM 的來源文字；超長時從最舊的開始丟。"""
    blocks: List[str] = []
    for row in rows:
        speaker = "使用者" if row["role"] == "user" else "AI"
        blocks.append(f"【{speaker}】\n{(row['content'] or '').strip()}")

    total = sum(len(b) for b in blocks)
    while blocks and total > _MAX_SOURCE_CHARS:
        total -= len(blocks.pop(0))
    return "\n\n".join(blocks)


def _split_title_and_body(text: str) -> tuple[str, str]:
    """LLM 回覆的第一行 `# 標題` 拆出來；沒照格式時退而求其次取首行。"""
    content = (text or "").strip()
    if not content:
        return "未命名筆記", ""

    lines = content.split("\n")
    first = lines[0].strip()
    if first.startswith("#"):
        return _clean_note_title(first.lstrip("#").strip()), "\n".join(lines[1:]).strip()
    return _clean_note_title(first[:_NOTE_TITLE_MAX]), content


def _chunk_usage(chunk: Any) -> Optional[tuple[int, int]]:
    """
    從串流 chunk 取 prompt / completion tokens。

    專案共用的 StreamUsageChatOpenAI 會在串流結尾補一個只帶 usage 的 chunk
    （response_metadata["token_usage"]），這是本專案唯一的計費來源。
    新版 langchain 改用 usage_metadata，兩邊都試以免日後升級漏記帳。
    """
    usage = getattr(chunk, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)

    meta = getattr(chunk, "response_metadata", None) or {}
    token_usage = meta.get("token_usage")
    if not token_usage:
        return None
    return (
        int(token_usage.get("prompt_tokens") or 0),
        int(token_usage.get("completion_tokens") or 0),
    )


@router.post("/api/notes/generate", status_code=status.HTTP_201_CREATED)
async def generate_note(
    request: GenerateNoteRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    把選取的對話訊息交給 LLM 整理成一則筆記。（需登入）

    HTTP 回應：
    - 201：建立成功，回傳筆記
    - 404：對話不屬於本人，或選取的訊息不在該對話內
    - 422：選取數量超過上限
    - 429：當月 token 配額已用盡
    """
    user_id = current_user["id"]

    if len(request.message_ids) > _MAX_SOURCE_MESSAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"單次最多整理 {_MAX_SOURCE_MESSAGES} 則訊息（目前 {len(request.message_ids)}）。",
        )

    # 發 LLM 前先擋配額，與 /chat/messages、深度研究一致
    await assert_preflight_llm_quota(user_id)

    owned_chat = await db.fetchval(
        "SELECT 1 FROM chats WHERE id = $1 AND user_id = $2", request.chat_id, user_id
    )
    if not owned_chat:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到該對話。")

    # 限定 chat_id：否則帶別人對話的 message_id 就能把內容撈出來
    rows = await db.fetch(
        """
        SELECT id, role, content FROM messages
        WHERE chat_id = $1 AND id = ANY($2::uuid[])
        ORDER BY created_at
        """,
        request.chat_id, list(request.message_ids),
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "選取的訊息不存在於該對話。")

    source_text = _build_source_text(rows)
    if not source_text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "選取的訊息沒有可整理的內容。")

    page_id = request.page_id
    if page_id is not None:
        await _assert_page_owned(db, page_id, user_id)
    else:
        page_id = await _ensure_default_page(db, user_id)

    note_count = await db.fetchval("SELECT COUNT(*) FROM notes WHERE page_id = $1", page_id)
    if note_count >= _MAX_NOTES_PER_PAGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"該頁筆記數已達上限（{_MAX_NOTES_PER_PAGE} 則）。",
        )

    # 延後 import：notes 模組被 api/__init__ 直接載入，不該在匯入期就把
    # LangChain / OpenAI client 一起初始化
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.backend.agent.general_chat import get_general_chat_model, resolve_general_chat_model

    model_name = resolve_general_chat_model(None)
    user_prompt = f"以下是對話節錄：\n\n{source_text}"
    if request.instruction:
        user_prompt += f"\n\n使用者希望這則筆記特別著重：{request.instruction.strip()[:500]}"

    # 用 astream 而非 ainvoke：共用實例帶著 stream_options（OpenAI 只允許串流時使用），
    # 而且 token 用量正是靠串流結尾那個 usage chunk 取得 —— 走串流才接得上既有計費。
    # 筆記不需要即時顯示，這裡收完再一次回傳。
    parts: List[str] = []
    prompt_tokens = completion_tokens = 0
    try:
        async for chunk in get_general_chat_model(model_name).astream([
            SystemMessage(content=_NOTE_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]):
            text = chunk.content
            if isinstance(text, str) and text:
                parts.append(text)
            usage = _chunk_usage(chunk)
            if usage:
                prompt_tokens, completion_tokens = usage
    except Exception as exc:
        print(f"[NOTES] 生成失敗 user={user_id} chat={request.chat_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "筆記生成失敗，請稍後再試。")

    raw_content = "".join(parts).strip()
    if not raw_content:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "筆記生成結果為空，請稍後再試。")
    # 記帳失敗不該讓已經產好的筆記消失，record_token_usage 內部自己吞例外
    await record_token_usage(
        user_id=user_id,
        chat_id=request.chat_id,
        message_id=None,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        caller="notes_generate",
    )

    title, body = _split_title_and_body(raw_content)
    body = body[:_NOTE_CONTENT_MAX]

    row = await db.fetchrow(
        """
        INSERT INTO notes (page_id, user_id, title, content, source_chat_id, source_message_ids)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id, page_id, title, content, source_chat_id, source_message_ids,
                  created_at, updated_at
        """,
        page_id, user_id, title, body, request.chat_id,
        json.dumps([str(r["id"]) for r in rows]),
    )
    return {"status": "success", "data": _note_row(row)}
