import os
import time
import json
import asyncio
import asyncpg
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Any
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

    # 3. SSE 必要參數
    chat_id_str = str(request.chat_id)
    enabled_tools: List[str] = []
    if request.agent_config and request.agent_config.enabled_tools:
        enabled_tools = request.agent_config.enabled_tools

    initial_state = {
        "messages": [HumanMessage(content=request.query)],
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
            yield _sse("done", {
                "status": "success",
                "chat_id": chat_id_str,
                "total_execution_time": total_time,
                "steps": accumulated_steps,
                "final_content": accumulated_final_analyst.get("content", ""),
                "retrieval_sources": [
                    {
                        "tool": item.get("source_tool"),
                        "title": item.get("title"),
                        "publishAt": item.get("publishAt"),
                        "url": item.get("url"),
                        "mongo_id": item.get("mongo_id"),
                        "content_preview": item.get("content", "")[:100] + "...",
                    }
                    for item in accumulated_retrieved
                ],
            })

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
            yield _sse("error", {"message": f"Agent 執行失敗: {str(e)}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
