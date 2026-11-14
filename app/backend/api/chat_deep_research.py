"""
對話中的深度研究（`POST /api/chat/deep-research`）
==================================================
把「深度研究」接進一般對話流程，成為聊天模式的第三個選項（一般對話／股市 Agent／深度研究）。

和 `/api/deep-research/runs`（獨立的深度研究頁）的差別：

| 面向       | 深度研究頁                         | 對話中的深度研究（本檔）                     |
|------------|------------------------------------|----------------------------------------------|
| 結果保存   | 只在記憶體 session，重新整理就沒了 | 寫進 `messages`，和其他對話一樣留在歷史裡     |
| 上下文     | 無                                 | 帶入同一則對話的前幾輪（有字數預算，見下）    |
| 斷線       | 前端斷線即取消研究                 | 背景任務繼續跑完並存檔（與其他聊天模式一致） |
| 產出檔案   | 可再產報告／簡報                   | 不提供（要產檔請用深度研究頁）               |

上下文預算：研究過程中每輪 web search 都會把搜尋結果塞回 prompt，前面再疊整串歷史
很容易超過 context，也會讓單次成本失控。因此歷史另走一份更緊的預算
（`DEEP_SEARCH_CHAT_HISTORY_*`，見 deep_research/config.py）：每則先截斷、
總量超出時從最舊的開始丟。其餘輸入本來就各自有上限 —— 題目 4000 字、
試算表合計 30000 字、圖片 4 張 —— 所以整份輸入有可預期的上界。

上傳檔案沿用 `deep_research.sources.read_uploads()` 的驗證
（數量、單檔／圖片／總量大小、檔頭與實際格式、zip bomb、圖片像素、檔名清洗）。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from app.backend.api.chat import (
    _delete_chat_if_no_messages,
    _finalize_title_task,
    _generate_title_via_llm,
    _insert_assistant_message,
    _insert_user_message,
    _spawn_background,
    _sse,
)
from app.backend.api.deep_research import _active_runs, _spawn_cleanup
from app.backend.database.postgresql import get_db
from app.backend.deep_research.config import (
    CHAT_HISTORY_ASSISTANT_MAX_CHARS,
    CHAT_HISTORY_MAX_CHARS,
    CHAT_HISTORY_USER_MAX_CHARS,
    QUERY_MAX_CHARS,
    resolve_model,
)
from app.backend.deep_research.researcher import history_to_input, stream_research
from app.backend.deep_research.sources import PreparedSources, prepare_sources, read_uploads
from app.backend.deep_research.usage import (
    CALLER_CHAT_RESEARCH,
    AgentUsage,
    record_agent_usage,
)
from app.backend.module.chat_context import fetch_prior_context_for_agent
from app.backend.module.jwt import get_current_user_id
from app.backend.module.token_usage import attach_token_usage_logs_to_message
from app.backend.module.usage_quota import assert_preflight_llm_quota

router = APIRouter(tags=["Chat"])

# 研究中途沒有任何事件時（web search 有時一輪要數十秒），每隔這麼久送一個註解行，
# 避免 Cloudflare／proxy 判定連線閒置而切斷（Cloudflare 免費方案 100 秒）
_HEARTBEAT_SECONDS = 15

# 單次研究的牆鐘上限：hosted tool 卡住時不能無限期占著「每人一個研究」的名額
_RUN_TIMEOUT_SECONDS = int(os.getenv("DEEP_SEARCH_CHAT_TIMEOUT_SECONDS", "900") or 900)

# researcher 吐的是中文標籤；前端的工具列以 id 對照顯示名稱
_TOOL_IDS = {"網路搜尋": "dr_web_search", "文件檢索": "dr_file_search"}

RESPONSE_MODE = "deep_research"


def _history_from_rows(rows: List[asyncpg.Record]) -> List[Dict[str, Any]]:
    turns = [(r["role"], r["content"] or "") for r in rows]
    return history_to_input(
        turns,
        total_max_chars=CHAT_HISTORY_MAX_CHARS,
        assistant_max_chars=CHAT_HISTORY_ASSISTANT_MAX_CHARS,
        user_max_chars=CHAT_HISTORY_USER_MAX_CHARS,
    )


@router.post("/api/chat/deep-research")
async def chat_deep_research(
    chat_id: UUID = Form(...),
    query: str = Form(...),
    model: Optional[str] = Form(None, max_length=64),
    files: List[UploadFile] = File(default=[]),
    db: asyncpg.Connection = Depends(get_db),
    current_user_id: UUID = Depends(get_current_user_id),
):
    """
    在指定對話中執行一次深度研究（multipart：chat_id、query、model、files[]），以 SSE 回傳。

    事件與 `/api/chat/messages` 相同，前端可共用同一套渲染：
      thinking   — 進度文字（讀取檔案、規劃查證方向、推理中…）
      notice     — 個別檔案處理失敗等提醒（不中斷研究）
      tool_start / tool_done — 網路搜尋、文件檢索
      token      — 報告逐字輸出
      title_update / done / error
    """
    # 1. 對話擁有權（找不到與不屬於你一律 404）
    chat_row = await db.fetchrow(
        "SELECT title_generated FROM chats WHERE id = $1 AND user_id = $2",
        chat_id,
        current_user_id,
    )
    if chat_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat not found or you don't have permission.")
    should_generate_title = not chat_row["title_generated"]

    clean_query = (query or "").strip()
    if not clean_query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "請輸入想研究的問題。")
    if len(clean_query) > QUERY_MAX_CHARS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"研究題目請控制在 {QUERY_MAX_CHARS} 字以內。")

    user_key = str(current_user_id)
    try:
        # 2. 每人同時只能跑一個研究（與深度研究頁共用同一把鎖）
        if user_key in _active_runs:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "您已有一個深度研究正在執行，請等它完成後再開始下一個。",
            )
        # 3. 配額 pre-flight：一次研究動輒數十萬 token，超額就連檔案都不讀
        await assert_preflight_llm_quota(current_user_id)
        # 4. 讀檔與驗證（格式錯誤要回一般的 4xx，而不是包在 SSE 裡）
        uploads = await read_uploads(files or [])
    except HTTPException:
        # 這一輪還沒寫進任何訊息；剛建立的新對話若因此是空的，刪掉免得側欄留下孤兒
        await _delete_chat_if_no_messages(db, chat_id, current_user_id)
        raise
    chosen_model = resolve_model(model)
    attachments = [
        {"name": u.filename, "kind": u.kind, "size": len(u.content)} for u in uploads
    ]

    # 5. 寫入 user 訊息並取上下文
    try:
        user_message_id = await _insert_user_message(
            db,
            chat_id,
            clean_query,
            metadata={"response_mode": RESPONSE_MODE, "attachments": attachments},
        )
        prior_rows = await fetch_prior_context_for_agent(
            db, chat_id=chat_id, user_message_id=user_message_id
        )
    except asyncpg.PostgresError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to persist user message: {exc}"
        )
    history = _history_from_rows(prior_rows)

    chat_id_str = str(chat_id)
    event_queue: asyncio.Queue = asyncio.Queue()
    _active_runs.add(user_key)

    async def runner() -> None:
        started = time.monotonic()
        usage = AgentUsage()
        usage_recorded = False
        log_ids: List[UUID] = []
        prepared: Optional[PreparedSources] = None
        title_task: Optional[asyncio.Task] = (
            asyncio.create_task(_generate_title_via_llm(clean_query, current_user_id, chat_id))
            if should_generate_title else None
        )

        async def record_usage() -> None:
            nonlocal usage_recorded
            if usage_recorded:
                return
            usage_recorded = True
            log_id = await record_agent_usage(
                user_id=current_user_id,
                model=chosen_model,
                usage=usage,
                caller=CALLER_CHAT_RESEARCH,
                chat_id=chat_id,
            )
            if log_id is not None:
                log_ids.append(log_id)

        try:
            async with asyncio.timeout(_RUN_TIMEOUT_SECONDS):
                if uploads:
                    await event_queue.put(_sse("thinking", {"text": "讀取與索引上傳檔案…"}))
                prepared = await prepare_sources(uploads)
                if prepared.warnings:
                    await event_queue.put(_sse("notice", {"messages": prepared.warnings}))

                await event_queue.put(_sse("thinking", {"text": "開始研究，規劃查證方向…"}))

                outcome: Dict[str, Any] = {}
                async for event in stream_research(
                    query=clean_query,
                    model=chosen_model,
                    prepared=prepared,
                    usage=usage,
                    history=history,
                ):
                    name = event.get("event")
                    if name == "delta":
                        await event_queue.put(_sse("token", {"text": event.get("text", "")}))
                    elif name in ("tool_start", "tool_done"):
                        tool = _TOOL_IDS.get(event.get("tool", ""), "dr_web_search")
                        await event_queue.put(_sse(name, {"tool": tool}))
                    elif name in ("thinking", "writing"):
                        await event_queue.put(_sse("thinking", {"text": event.get("text", "")}))
                    elif name == "complete":
                        outcome = event

            markdown = (outcome.get("markdown") or "").strip()
            if not markdown:
                raise RuntimeError("研究沒有產出內容，請換個問法再試一次。")

            await record_usage()
            sources = [
                {"tool": "web", "title": c.get("title") or c.get("url"), "url": c.get("url")}
                for c in outcome.get("citations") or []
                if c.get("url")
            ]
            elapsed = round(time.monotonic() - started, 3)
            await event_queue.put(_sse("done", {
                "status": "success",
                "chat_id": chat_id_str,
                "total_execution_time": elapsed,
                "steps": [],
                "final_content": markdown,
                "retrieval_sources": sources,
                "response_mode": RESPONSE_MODE,
                "model": chosen_model,
                "usage": usage.as_dict(),
            }))

            assistant_id = await _insert_assistant_message(
                chat_id=chat_id,
                parent_id=user_message_id,
                content=markdown,
                context_refs=sources or None,
                metadata={
                    "response_mode": RESPONSE_MODE,
                    "model": chosen_model,
                    "total_execution_time": elapsed,
                    "tools_used": outcome.get("tools_used") or [],
                    "attachments": [a["name"] for a in attachments],
                    "warnings": prepared.warnings if prepared else [],
                },
            )
            if assistant_id and log_ids:
                await attach_token_usage_logs_to_message(current_user_id, assistant_id, log_ids)
            await _finalize_title_task(
                title_task,
                chat_id=chat_id,
                chat_id_str=chat_id_str,
                user_id=current_user_id,
                assistant_message_id=assistant_id,
                event_queue=event_queue,
            )

        except Exception as exc:  # noqa: BLE001 — 任何失敗都要讓前端收到並留下紀錄
            import traceback

            traceback.print_exc()
            if title_task is not None and not title_task.done():
                title_task.cancel()
            if isinstance(exc, TimeoutError):
                reason = f"研究超過 {_RUN_TIMEOUT_SECONDS // 60} 分鐘仍未完成，已中止。"
            elif isinstance(exc, HTTPException):
                reason = str(exc.detail)
            else:
                reason = str(exc) or type(exc).__name__
            err_text = f"深度研究執行失敗：{reason}"
            await event_queue.put(_sse("error", {"message": err_text}))
            # 失敗也要記帳：已經打出去的那幾輪 OpenAI 照樣收錢
            await record_usage()
            err_id = await _insert_assistant_message(
                chat_id=chat_id,
                parent_id=user_message_id,
                content=err_text,
                metadata={"error": reason, "response_mode": RESPONSE_MODE, "model": chosen_model},
            )
            if err_id and log_ids:
                await attach_token_usage_logs_to_message(current_user_id, err_id, log_ids)
        finally:
            _active_runs.discard(user_key)
            if not usage_recorded:
                _spawn_cleanup(record_agent_usage(
                    user_id=current_user_id, model=chosen_model, usage=usage,
                    caller=CALLER_CHAT_RESEARCH, chat_id=chat_id,
                ))
            # 不論成功失敗，OpenAI 上的臨時 vector store 與檔案一律刪掉
            if prepared is not None:
                _spawn_cleanup(prepared.cleanup())
            with suppress(asyncio.CancelledError):
                await event_queue.put(None)

    _spawn_background(runner())

    async def event_generator():
        # 背景任務獨立於連線：前端斷線時研究照樣跑完並寫進對話紀錄
        while True:
            try:
                msg = await asyncio.wait_for(event_queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                break
            yield msg

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
