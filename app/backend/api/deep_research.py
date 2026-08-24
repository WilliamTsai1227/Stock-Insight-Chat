"""
深度研究 API（`/api/deep-research/*`）
========================================
以 OpenAI Agents SDK 執行一次性的研究任務，再把結果交給報告／簡報 skill 產出檔案。

MVP 定位是功能驗證，因此刻意不落地：
- 不寫 PostgreSQL、不進 Qdrant、不上 S3
- session 只放在記憶體（`session_store`），逾時或後端重啟即消失
- 上傳的文件只在研究期間存在於 OpenAI 的臨時 vector store，研究一結束立刻刪除

三個階段各自對應一個端點：

    POST /runs                          multipart → SSE：上傳 + 研究
    POST /runs/{sid}/artifacts          JSON      → SSE：跑 skill 產生報告／簡報
    GET  /runs/{sid}/artifacts/{kind}   →  下載已產生的檔案

SSE 用 POST 而非 EventSource，因為 EventSource 帶不了 Authorization header；
前端沿用既有的 `authFetch` + ReadableStream 解析方式（同 /api/chat/messages）。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.backend.deep_research.config import QUERY_MAX_CHARS, public_config, resolve_model
from app.backend.deep_research.researcher import stream_research
from app.backend.deep_research.session import Artifact, session_store
from app.backend.deep_research.skills import SKILLS, generate_artifact
from app.backend.deep_research.templates import resolve_theme
from app.backend.deep_research.sources import (
    PreparedSources,
    prepare_sources,
    read_uploads,
    source_manifest,
)
from app.backend.module.jwt import get_current_user_id

router = APIRouter(prefix="/api/deep-research", tags=["Deep Research"])

# SSE 沉默上限：超過就送一個註解行，避免 nginx / ALB 判定連線閒置而中斷
_HEARTBEAT_SECONDS = 15

# 每位使用者同時只能跑一個研究任務（hosted web search 很貴，也避免併發打爆額度）
_active_runs: set[str] = set()

# 脫離請求生命週期的清理任務；必須保留強參考，否則會被 GC 回收
_cleanup_tasks: set[asyncio.Task] = set()


def _spawn_cleanup(coro) -> None:
    """
    把清理工作丟到獨立 task 執行。

    前端斷線時生產者 task 會被 cancel，此時它的 finally 裡任何 `await` 都會立刻
    再次拋出 CancelledError —— 直接 `await cleanup()` 會導致 vector store 清不掉。
    """
    task = asyncio.create_task(coro)
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_with_heartbeat(
    produce: Any,
) -> AsyncIterator[str]:
    """
    把「往 queue 丟 SSE 字串」的生產者包成帶心跳的 StreamingResponse 來源。

    生產者結束時要往 queue 放一個 None。前端斷線時本 generator 會被
    cancel，finally 會連帶取消生產者 —— 研究結果沒人看就沒有續跑的意義。
    """
    queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    task = asyncio.create_task(produce(queue))

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


def _sse_response(produce: Any) -> StreamingResponse:
    return StreamingResponse(
        _stream_with_heartbeat(produce),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────
# 1. 設定：可選模型與各種上限
# ─────────────────────────────────────────────────────────────
@router.get("/config")
async def get_deep_research_config(
    _user_id: UUID = Depends(get_current_user_id),
):
    """前端初始化用：模型清單、預設模型（DEEP_SEARCH_MODEL）、檔案上限。"""
    return {"status": "success", "data": public_config()}


# ─────────────────────────────────────────────────────────────
# 2. 執行研究
# ─────────────────────────────────────────────────────────────
@router.post("/runs")
async def create_research_run(
    query: str = Form(...),
    model: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    上傳檔案 + 執行研究，以 SSE 回傳進度。

    事件序：session → status → (tool_start / tool_done / thinking / delta)* → done
    任何階段出錯都會送 `error` 後結束。
    """
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="請輸入想研究的問題。",
        )
    if len(cleaned_query) > QUERY_MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"研究題目請控制在 {QUERY_MAX_CHARS} 字以內。",
        )

    user_key = str(user_id)
    if user_key in _active_runs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="您已有一個研究任務正在執行，請等它完成後再開始下一個。",
        )

    # 讀檔與格式驗證放在 handler：格式錯誤要回正常的 4xx，而不是包在 SSE 裡
    uploads = await read_uploads(files or [])

    chosen_model = resolve_model(model)
    session = await session_store.create(
        user_id=user_key, model=chosen_model, query=cleaned_query
    )
    session.source_names = [u.filename for u in uploads]
    _active_runs.add(user_key)

    async def produce(queue: asyncio.Queue) -> None:
        prepared: Optional[PreparedSources] = None
        try:
            await queue.put(
                _sse(
                    "session",
                    {
                        "session_id": session.id,
                        "model": chosen_model,
                        "sources": [u.filename for u in uploads],
                    },
                )
            )

            if uploads:
                await queue.put(
                    _sse("status", {"stage": "ingest", "text": "讀取與索引上傳檔案…"})
                )
            prepared = await prepare_sources(uploads)

            if prepared.warnings:
                await queue.put(_sse("warning", {"messages": prepared.warnings}))
            if uploads:
                await queue.put(
                    _sse("sources_ready", {"sources": source_manifest(prepared)})
                )

            await queue.put(
                _sse("status", {"stage": "research", "text": "開始研究，規劃查證方向…"})
            )

            async for event in stream_research(
                query=cleaned_query, model=chosen_model, prepared=prepared
            ):
                name = event.pop("event")
                if name == "complete":
                    session.markdown = event["markdown"]
                    session.citations = event["citations"]
                    session.tools_used = event["tools_used"]
                    session.elapsed_ms = event["elapsed_ms"]
                    session.status = "done"
                    await queue.put(
                        _sse(
                            "done",
                            {
                                "session_id": session.id,
                                "markdown": session.markdown,
                                "citations": session.citations,
                                "tools_used": session.tools_used,
                                "elapsed_ms": session.elapsed_ms,
                                "skills": [
                                    {"kind": k, "label": v["label"]}
                                    for k, v in SKILLS.items()
                                ],
                            },
                        )
                    )
                else:
                    await queue.put(_sse(name, event))

        except asyncio.CancelledError:
            session.status = "error"
            session.error = "前端已中斷連線"
            raise
        except HTTPException as exc:
            session.status = "error"
            session.error = str(exc.detail)
            await queue.put(_sse("error", {"message": str(exc.detail)}))
        except Exception as exc:  # noqa: BLE001 — 任何失敗都要讓前端收到訊息
            import traceback

            traceback.print_exc()
            session.status = "error"
            session.error = f"{type(exc).__name__}: {exc}"
            await queue.put(
                _sse("error", {"message": f"研究執行失敗：{type(exc).__name__}: {exc}"})
            )
        finally:
            # 同步狀態先清：被 cancel 時 finally 裡的第一個 await 就會再拋 CancelledError，
            # 放在 await 後面的 discard 永遠不會執行，使用者會被鎖住再也開不了新研究。
            _active_runs.discard(user_key)
            # 不論成功、失敗或前端斷線，都要把 OpenAI 上的暫存檔清掉
            if prepared is not None:
                _spawn_cleanup(prepared.cleanup())
            with suppress(asyncio.CancelledError):
                await queue.put(None)

    return _sse_response(produce)


# ─────────────────────────────────────────────────────────────
# 3. 產生報告／簡報
# ─────────────────────────────────────────────────────────────
class ArtifactRequest(BaseModel):
    kind: str                      # "report" | "deck"
    theme: Optional[str] = None    # 視覺風格；不認得的值會退回預設主題


@router.post("/runs/{session_id}/artifacts")
async def create_artifact(
    session_id: str,
    payload: ArtifactRequest,
    user_id: UUID = Depends(get_current_user_id),
):
    """
    以研究結果跑指定 skill，產出可下載的 HTML。

    產生一份簡報要跑數十秒，因此同樣走 SSE（帶心跳）避免中途被 proxy 掐斷。
    """
    kind = (payload.kind or "").strip()
    if kind not in SKILLS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未知的產出類型：{payload.kind}",
        )

    session = await session_store.get(session_id, str(user_id))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="研究結果已逾時或不存在，請重新執行一次研究。",
        )
    if session.status != "done" or not session.markdown:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="這次研究尚未完成，無法產生檔案。",
        )

    label = SKILLS[kind]["label"]

    async def produce(queue: asyncio.Queue) -> None:
        try:
            await queue.put(_sse("status", {"text": f"{label}撰寫中…"}))

            filename, media_type, content = await generate_artifact(
                kind=kind,
                model=session.model,
                query=session.query,
                research_markdown=session.markdown,
                citations=session.citations,
                theme=payload.theme,
            )

            artifact = Artifact(
                kind=kind,
                filename=filename,
                media_type=media_type,
                content=content,
            )
            session.artifacts[kind] = artifact

            await queue.put(
                _sse(
                    "done",
                    {
                        "kind": kind,
                        "label": label,
                        "filename": artifact.filename,
                        "size": artifact.size,
                        "theme": resolve_theme(payload.theme).id,
                        "download_path": (
                            f"/api/deep-research/runs/{session.id}/artifacts/{kind}"
                        ),
                    },
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            await queue.put(
                _sse("error", {"message": f"{label}產生失敗：{type(exc).__name__}: {exc}"})
            )
        finally:
            with suppress(asyncio.CancelledError):
                await queue.put(None)

    return _sse_response(produce)


# ─────────────────────────────────────────────────────────────
# 4. 下載
# ─────────────────────────────────────────────────────────────
@router.get("/runs/{session_id}/artifacts/{kind}")
async def download_artifact(
    session_id: str,
    kind: str,
    user_id: UUID = Depends(get_current_user_id),
):
    """回傳已產生的檔案；前端以 authFetch 取 blob 後再觸發瀏覽器下載。"""
    session = await session_store.get(session_id, str(user_id))
    artifact = session.artifacts.get(kind) if session else None
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="檔案不存在或已逾時，請重新產生。",
        )

    # 檔名含中文，必須用 RFC 5987 的 filename* 才不會在下載時變成亂碼
    disposition = f"attachment; filename*=UTF-8''{quote(artifact.filename)}"
    return Response(
        content=artifact.content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
        },
    )


# ─────────────────────────────────────────────────────────────
# 5. 主動釋放
# ─────────────────────────────────────────────────────────────
@router.delete("/runs/{session_id}")
async def delete_research_run(
    session_id: str,
    user_id: UUID = Depends(get_current_user_id),
):
    """前端離開頁面時可主動釋放；沒呼叫也會由 TTL 清掉。"""
    session = await session_store.get(session_id, str(user_id))
    if session is not None:
        await session_store.drop(session_id)
    return {"status": "success"}


__all__ = ["router"]
