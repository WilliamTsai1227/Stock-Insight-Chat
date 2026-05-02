import os
import time
import json
import asyncio
import asyncpg
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from uuid import UUID
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.backend.agent.chat import create_chat_agent
from app.backend.database.postgresql import get_db, get_pool
from app.backend.module.jwt import get_current_user, get_current_user_id

router = APIRouter(tags=["Chat"])


class AgentConfig(BaseModel):
    enabled_tools: Optional[List[str]] = None


class MessageRequest(BaseModel):
    query: str
    chat_id: UUID                       # 必填：必須先呼叫 POST /api/chat 取得
    agent_config: Optional[AgentConfig] = None


class CreateChatRequest(BaseModel):
    query: str                          # 第一條訊息（用於產生 placeholder title）
    project_id: Optional[UUID] = None   # 可選：指定隸屬的 project


# Title 長度上限（DB 為 VARCHAR(255)，這裡再多保一層防呆）
_TITLE_MAX_LEN = 50
# Placeholder：截斷至此字數 + 省略號
_PLACEHOLDER_LEN = 30


agent_app = create_chat_agent()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_placeholder_title(query: str) -> str:
    """從第一條訊息產生 placeholder title：截斷 + 省略號。"""
    stripped = query.strip()
    if len(stripped) <= _PLACEHOLDER_LEN:
        return stripped
    return stripped[:_PLACEHOLDER_LEN] + "…"


# ─────────────────────────────────────────────────────────────────────────────
# 訊息持久化 helpers
# ─────────────────────────────────────────────────────────────────────────────
# 設計原則：
# - user 訊息：在 agent 開跑前 INSERT，失敗 → 回 500，避免進到 SSE 才出問題
# - assistant 訊息：在 SSE 'done' / 'error' 時 INSERT，使用 pool 取新連線（避免
#   依賴 Depends 注入連線在 StreamingResponse 期間的生命週期）
# - parent_id 設計：assistant.parent_id = user_message_id，方便前端把問答配對
#   user 訊息 parent_id 一律 NULL（對話樹起點）


async def _insert_user_message(
    db: asyncpg.Connection,
    chat_id: UUID,
    content: str,
) -> UUID:
    """在 agent 跑之前同步寫入 user 訊息，回傳新插入的 message id。"""
    row = await db.fetchrow(
        """
        INSERT INTO messages (chat_id, parent_id, role, content)
        VALUES ($1, NULL, 'user', $2)
        RETURNING id
        """,
        chat_id,
        content,
    )
    return row["id"]


async def _insert_assistant_message(
    chat_id: UUID,
    parent_id: Optional[UUID],
    content: str,
    context_refs: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[UUID]:
    """
    在 SSE 結束（done / error）時寫入 assistant 訊息。

    - 從 pool 取新連線：避免 StreamingResponse 期間原本的 Depends 連線生命週期不確定
    - 任何 DB 錯誤一律不丟例外，只印 log；持久化失敗不該打斷已經 yield 給前端的串流
    - 回傳 message id；失敗時回傳 None
    """
    try:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO messages
                    (chat_id, parent_id, role, content, context_refs, metadata)
                VALUES ($1, $2, 'assistant', $3, $4, $5)
                RETURNING id
                """,
                chat_id,
                parent_id,
                content,
                json.dumps(context_refs, ensure_ascii=False) if context_refs is not None else None,
                json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
            )
            return row["id"]
    except Exception as e:
        print(f"[MSG] INSERT assistant failed, chat_id={chat_id}: {type(e).__name__}: {e}")
        return None


async def _generate_title_via_llm(query: str) -> Optional[str]:
    """
    用 fast LLM 產 15 字內中文標題（model 由 TITLE_MODEL 環境變數指定，預設 gpt-4o-mini）。
    任何錯誤一律回傳 None，由呼叫方決定是否保留 placeholder。
    """
    model_name = os.getenv("TITLE_MODEL", "gpt-4o-mini")
    try:
        title_llm = ChatOpenAI(model=model_name, temperature=0)
        prompt = (
            "請以 15 字內的中文短句概括這個問題作為對話標題，"
            "只回標題本文，不要加引號或前後綴：\n\n"
            f"{query}"
        )
        result = await title_llm.ainvoke([HumanMessage(content=prompt)])
        text = (result.content or "").strip()
        title = text[:_TITLE_MAX_LEN] if text else None
        print(f"[TITLE] model={model_name} query={query[:30]!r} → {title!r}")
        return title
    except Exception as e:
        print(f"[TITLE] LLM title generation failed (model={model_name}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/chat —— 建立新 chat（採 placeholder title，立即回傳 chat_id）
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/chat", status_code=status.HTTP_201_CREATED)
async def create_chat(
    request: CreateChatRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    建立新聊天。（需登入）

    流程：
    1. 驗證 query 非空
    2. 若帶 project_id，驗證該 project 屬於本人（找不到一律 404，不洩漏）
    3. title 直接用 placeholder（截斷 query），不在此呼叫 LLM
       → 端點只做純 INSERT，~10ms 即可回傳 chat_id 給前端
       → LLM 產正式 title 在後續 /api/chat/messages 並行處理
    4. INSERT chats 並 RETURNING

    HTTP 回應：
    - 201 Created : { id, project_id, title, created_at }
    - 401         : JWT 驗證失敗
    - 403         : 帳號已停用
    - 404         : project_id 不存在或無權限
    - 422         : query 為空
    - 500         : DB 錯誤
    """
    user_id = current_user["id"]

    # 1. 驗證 query
    clean_query = request.query.strip()
    if not clean_query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query cannot be empty.",
        )

    # 2. project_id ownership 驗證
    if request.project_id is not None:
        owner = await db.fetchval(
            "SELECT user_id FROM projects WHERE id = $1",
            request.project_id,
        )
        if owner is None or owner != user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found or you don't have permission.",
            )

    # 3. Placeholder title
    placeholder = _make_placeholder_title(clean_query)

    # 4. INSERT
    now_utc = datetime.now(timezone.utc)
    try:
        row = await db.fetchrow(
            """
            INSERT INTO chats (project_id, user_id, title, title_generated, created_at)
            VALUES ($1, $2, $3, FALSE, $4)
            RETURNING id, project_id, title, created_at
            """,
            request.project_id,
            user_id,
            placeholder,
            now_utc,
        )
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}",
        )

    return {
        "status": "success",
        "data": {
            "id": str(row["id"]),
            "project_id": str(row["project_id"]) if row["project_id"] else None,
            "title": row["title"],
            "created_at": row["created_at"].isoformat(),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/chat/all —— 列出當前使用者的所有聊天（時間由近到遠）
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/chat/all")
async def list_all_chats(
    db: asyncpg.Connection = Depends(get_db),
    current_user: asyncpg.Record = Depends(get_current_user),
):
    """
    讀取目前登入使用者的所有聊天，依 created_at 由近到遠排序。

    安全設計：
    - user_id 由 JWT 解析，前端不需也不應傳遞（避免越權讀取他人 chats）
    - SQL 以 user_id 過濾，確保只回傳本人擁有的資料

    HTTP 回應：
    - 200 OK : 回傳聊天陣列（可能為空）
    - 401    : JWT 驗證失敗或 Token 已過期
    - 403    : 帳號已停用
    - 500    : 其他未預期的資料庫錯誤
    """
    user_id = current_user["id"]

    try:
        rows = await db.fetch(
            """
            SELECT id, title, created_at
            FROM chats
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user_id,
        )
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}",
        )

    return {
        "status": "success",
        "data": [
            {
                "id": str(row["id"]),
                "title": row["title"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/chat —— 載入指定 chat 的歷史訊息（cursor-based 分頁）
# ─────────────────────────────────────────────────────────────────────────────

# 分頁設計（與 ChatGPT / Slack / Telegram 同款）：
# - 不用 page/offset：邊看邊有新訊息會跳號 / 漏訊息
# - 複合 cursor (created_at, id)：避免「同一 microsecond 兩筆」時只用 ts 會漏列
# - 排序：DB 取 DESC + LIMIT N+1，回給前端再轉 ASC（聊天記錄一般是時間從上到下）
# - has_more：透過 LIMIT N+1 判斷是否還有更舊的訊息（多取 1 筆當哨兵）
# - next_before：當 has_more=true 時，回傳本次頁內「時間上最舊」那筆的 { ts, id }，
#                下一頁請同時帶 before_ts + before_id；只帶其中一個 → 422

# 一頁預設 30、上限 100（避免單次拉太多打爆 frontend / network）
_DEFAULT_MESSAGES_LIMIT = 30
_MAX_MESSAGES_LIMIT = 100


@router.get("/api/chat")
async def get_chat_messages(
    chat_id: UUID = Query(..., description="要載入的 chat UUID"),
    before_ts: Optional[datetime] = Query(
        None,
        description="複合 cursor 之一：本頁內時間上最舊那筆的 created_at（須與 before_id 成對）",
    ),
    before_id: Optional[UUID] = Query(
        None,
        description="複合 cursor 之一：本頁內時間上最舊那筆的 message id（須與 before_ts 成對）",
    ),
    limit: int = Query(
        _DEFAULT_MESSAGES_LIMIT,
        ge=1,
        le=_MAX_MESSAGES_LIMIT,
        description=f"單次最多載入幾筆（預設 {_DEFAULT_MESSAGES_LIMIT}，上限 {_MAX_MESSAGES_LIMIT}）",
    ),
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    """
    讀取指定 chat 的歷史訊息（按時間「舊→新」排序）。

    安全：
    - chat_id ownership 透過 chats.user_id = current_user_id 驗證；找不到一律 404，
      不洩漏資源是否存在
    - user_id 從 JWT 來，前端不需也不該帶

    HTTP 回應：
    - 200 : { messages: [...], has_more: bool,
              next_before: { ts: iso8601, id: uuid } | null }

    Cursor 規則：
    - 首頁：不帶 before_ts / before_id → 取得「最新」limit 條（按時間DESC取，回傳轉為舊→新）。
    - 載入更舊：帶上一頁回傳的 next_before.ts 為 before_ts，next_before.id 為 before_id。
    - before_ts / before_id 須「兩個都給」或「兩個都不給」，缺一 → 422。

    - 401 : JWT 失敗
    - 403 : 帳號已停用
    - 404 : chat 不存在或不屬於本人
    - 500 : DB 錯誤
    """
    if (before_ts is None) != (before_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="before_ts and before_id must both be provided, or neither.",
        )
    # 1. Ownership 驗證
    owner_id = await db.fetchval(
        "SELECT user_id FROM chats WHERE id = $1",
        chat_id,
    )
    if owner_id is None or owner_id != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found or you don't have permission.",
        )

    # 2. 主查詢：DESC + LIMIT N+1（多取 1 筆當 has_more 哨兵）
    # SQL 用 (chat_id, created_at DESC) composite index 完成 filter + sort
    fetch_n = limit + 1
    try:
        rows = await db.fetch(
            """
            SELECT id, parent_id, role, content,
                   tokens, context_refs, metadata, created_at
            FROM messages
            WHERE chat_id = $1
              AND (
                  ($2::timestamptz IS NULL AND $3::uuid IS NULL)
                  OR created_at < $2
                  OR (created_at = $2 AND id < $3)
              )
            ORDER BY created_at DESC, id DESC
            LIMIT $4
            """,
            chat_id,
            before_ts,
            before_id,
            fetch_n,
        )
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {e}",
        )

    has_more = len(rows) > limit
    page_rows = rows[:limit]   # 丟掉 N+1 哨兵那一筆

    # 3. 翻成 ASC（前端聊天串呈現順序）
    page_rows_asc = list(reversed(page_rows))

    # 4. next_before：本頁內時間上「最舊」那筆的 (created_at, id)，供複合 cursor
    oldest = page_rows_asc[0] if page_rows_asc else None
    next_before: Optional[Dict[str, Any]] = None
    if has_more and oldest is not None:
        next_before = {
            "ts": oldest["created_at"].isoformat(),
            "id": str(oldest["id"]),
        }

    def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
        # asyncpg 對 JSONB 預設不會自動 decode，這裡安全地嘗試 parse
        def _parse_json(val: Any) -> Any:
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return val
            try:
                return json.loads(val)
            except Exception:
                return val

        return {
            "id": str(row["id"]),
            "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
            "role": row["role"],
            "content": row["content"],
            "tokens": _parse_json(row["tokens"]),
            "context_refs": _parse_json(row["context_refs"]),
            "metadata": _parse_json(row["metadata"]),
            "created_at": row["created_at"].isoformat(),
        }

    return {
        "status": "success",
        "data": {
            "chat_id": str(chat_id),
            "messages": [_row_to_dict(r) for r in page_rows_asc],
            "has_more": has_more,
            "next_before": next_before,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/chat/messages —— SSE 串流端點
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/chat/messages")
async def get_ai_response(
    request: MessageRequest,
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    """
    SSE 串流端點：
      thinking     — Router 思考文字（小型 pill）
      tool_start   — 工具開始執行
      tool_done    — 工具執行完畢
      token        — Analyst LLM 逐字輸出（只在 analyst 節點期間）
      title_update — 第一次訊息：LLM 產出正式 title 後 push 給前端動態更新
      done         — 全部完成，含 steps / sources
      error        — 例外

    安全與 title 邏輯：
    - chat_id 必填；以 (id, user_id) 同時驗證 ownership，找不到回 404
    - 同一次查詢順便取出 title_generated 旗標
        FALSE → 第一次訊息，spawn LLM title task 並行處理
        TRUE  → 已產過正式 title，跳過 LLM 與 SSE title_update
    """
    # 1. Ownership + title_generated 一次查詢
    chat_row = await db.fetchrow(
        "SELECT title_generated FROM chats WHERE id = $1 AND user_id = $2",
        request.chat_id,
        current_user_id,
    )
    if chat_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found or you don't have permission.",
        )

    # 2. 旗標：FALSE 才需要產 title
    should_generate_title = not chat_row["title_generated"]

    # 3. 同步 INSERT user 訊息（在 agent 跑之前），失敗就直接 500
    # 這樣即使後續 agent / SSE 中斷，user 仍能看到自己問了什麼，方便重試
    try:
        clean_query = request.query.strip()
        if not clean_query:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Query cannot be empty.",
            )
        user_message_id = await _insert_user_message(db, request.chat_id, clean_query)
    except HTTPException:
        raise
    except asyncpg.PostgresError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to persist user message: {e}",
        )

    # 4. SSE 必要參數
    chat_id_str = str(request.chat_id)
    enabled_tools: List[str] = []
    if request.agent_config and request.agent_config.enabled_tools:
        enabled_tools = request.agent_config.enabled_tools

    initial_state = {
        "messages": [HumanMessage(content=clean_query)],
        "trace": {},
        "retrieved_data": [],
        "enabled_tools": enabled_tools,
    }
    config = {"configurable": {"thread_id": chat_id_str}}

    async def event_generator():
        start_total = time.time()

        # 備援累積資料（不依賴 final_state）
        accumulated_steps: list = []
        accumulated_retrieved: list = []
        accumulated_final_analyst: dict = {}

        # ── 關鍵：用 chain_start / chain_end 追蹤 analyst 節點是否正在執行 ──
        in_analyst = False

        # ── 條件式 spawn：第一次訊息才呼 LLM 產 title，與主串流並行 ──
        title_task: Optional[asyncio.Task] = (
            asyncio.create_task(_generate_title_via_llm(request.query))
            if should_generate_title else None
        )

        try:
            async for event in agent_app.astream_events(
                initial_state,
                config=config,
                version="v1",
            ):
                kind = event.get("event", "")
                name = event.get("name", "")
                data = event.get("data", {})

                # ── Analyst 節點開始執行 ─────────────────────────────
                if kind == "on_chain_start" and name == "analyst":
                    in_analyst = True

                # ── Analyst 節點結束 ─────────────────────────────────
                elif kind == "on_chain_end" and name == "analyst":
                    in_analyst = False
                    output = data.get("output", {})
                    trace_frag = output.get("trace", {})
                    if trace_frag.get("final_analyst"):
                        accumulated_final_analyst = trace_frag["final_analyst"]
                    for step in trace_frag.get("steps", []):
                        if step not in accumulated_steps:
                            accumulated_steps.append(step)

                # ── Router 節點結束：tool_start / thinking ───────────
                elif kind == "on_chain_end" and name == "router":
                    output = data.get("output", {})
                    trace_frag = output.get("trace", {})
                    for step in trace_frag.get("steps", []):
                        if step not in accumulated_steps:
                            accumulated_steps.append(step)

                    msgs = output.get("messages", [])
                    for msg in msgs:
                        tool_calls = getattr(msg, "tool_calls", None)
                        if tool_calls:
                            for tc in tool_calls:
                                yield _sse("tool_start", {
                                    "tool": tc["name"],
                                    "query": tc.get("args", {}).get("query"),
                                })
                        elif getattr(msg, "content", ""):
                            steps = trace_frag.get("steps", [])
                            thought = steps[-1].get("thought", "") if steps else ""
                            if thought:
                                yield _sse("thinking", {"text": thought})

                # ── Tools 節點結束：tool_done ────────────────────────
                elif kind == "on_chain_end" and name == "tools":
                    output = data.get("output", {})
                    for item in output.get("retrieved_data", []):
                        accumulated_retrieved.append(item)
                    for msg in output.get("messages", []):
                        tool_name = getattr(msg, "name", "unknown")
                        yield _sse("tool_done", {"tool": tool_name})

                # ── LLM token：只在 analyst 節點期間才轉發 ───────────
                elif kind == "on_chat_model_stream" and in_analyst:
                    chunk = data.get("chunk")
                    if chunk:
                        token = getattr(chunk, "content", "") or ""
                        if token:
                            yield _sse("token", {"text": token})

            # ── async for 結束 = 圖已執行完畢，無條件送 done ─────────
            total_time = round(time.time() - start_total, 3)
            final_content = accumulated_final_analyst.get("content", "")
            retrieval_sources = [
                {
                    "tool": item.get("source_tool"),
                    "title": item.get("title"),
                    "publishAt": item.get("publishAt"),
                    "url": item.get("url"),
                    "mongo_id": item.get("mongo_id"),
                    "content_preview": item.get("content", "")[:100] + "...",
                }
                for item in accumulated_retrieved
            ]
            yield _sse("done", {
                "status": "success",
                "chat_id": chat_id_str,
                "total_execution_time": total_time,
                "steps": accumulated_steps,
                "final_content": final_content,
                "retrieval_sources": retrieval_sources,
            })

            # ── 持久化 assistant 訊息（store_summary 策略）─────────────
            # context_refs 只存 SSE 摘要版（title / url / preview），不存 content 全文
            # → 重整後前端能還原來源 UI，DB 體積也不會被全文撐爆
            await _insert_assistant_message(
                chat_id=request.chat_id,
                parent_id=user_message_id,
                content=final_content,
                context_refs=retrieval_sources if retrieval_sources else None,
                metadata={
                    "steps": accumulated_steps,
                    "total_execution_time": total_time,
                },
            )

            # ── 處理並行的 title task（僅第一次訊息才存在）─────────
            # 主串流結束後再 await，可同時利用主串流時間做 title 生成
            # title 失敗時 placeholder 保留、title_generated 維持 FALSE，
            # 下次第二條訊息會再嘗試一次（自動 retry）
            if title_task is not None:
                try:
                    new_title = await asyncio.wait_for(title_task, timeout=3.0)
                except asyncio.TimeoutError:
                    print(f"[TITLE] task timeout (>3s), chat_id={chat_id_str}")
                    new_title = None
                except Exception as e:
                    print(f"[TITLE] task raised: {type(e).__name__}: {e}, chat_id={chat_id_str}")
                    new_title = None

                if new_title:
                    try:
                        # 從 pool 取新連線做 UPDATE（不影響 SSE 生命週期）
                        async with get_pool().acquire() as conn:
                            await conn.execute(
                                """
                                UPDATE chats
                                SET title = $1, title_generated = TRUE
                                WHERE id = $2
                                """,
                                new_title,
                                request.chat_id,
                            )
                        print(f"[TITLE] UPDATE ok, chat_id={chat_id_str}, title={new_title!r}")
                        yield _sse("title_update", {
                            "chat_id": chat_id_str,
                            "title": new_title,
                        })
                    except Exception as e:
                        # UPDATE 失敗：placeholder 保留，旗標仍 FALSE，下次再試
                        print(f"[TITLE] UPDATE failed, chat_id={chat_id_str}: {type(e).__name__}: {e}")

        except Exception as e:
            import traceback
            traceback.print_exc()
            # 例外發生時若 title_task 還在跑，主動取消避免 leak
            if title_task is not None and not title_task.done():
                title_task.cancel()

            err_text = f"Agent 執行失敗: {str(e)}"
            yield _sse("error", {"message": err_text})

            # save_user_and_error：失敗也寫入一筆 role=assistant 的錯誤訊息
            # → user 重整後仍能看到「自己問過 + 系統回過錯誤」的完整時序
            # → 不會導致下次第一次發訊息時又被當成 first message 重新跑 title
            await _insert_assistant_message(
                chat_id=request.chat_id,
                parent_id=user_message_id,
                content=err_text,
                metadata={
                    "error": True,
                    "exception_type": type(e).__name__,
                    "total_execution_time": round(time.time() - start_total, 3),
                },
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
