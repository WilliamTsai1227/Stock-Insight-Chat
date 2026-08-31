"""
深度研究 API（`/api/deep-research/*`）
========================================
以 OpenAI Agents SDK 執行一次性的研究任務，再把結果交給報告／簡報 skill 產出檔案。

MVP 定位是功能驗證，因此刻意不落地：
- 不寫 PostgreSQL、不進 Qdrant、不上 S3
- session 只放在記憶體（`session_store`），逾時或後端重啟即消失
- 上傳的文件只在研究期間存在於 OpenAI 的臨時 vector store，研究一結束立刻刪除

三個階段各自對應一個端點：

    POST /runs                              multipart → SSE：上傳 + 研究
    POST /runs/{sid}/artifacts              JSON      → SSE：跑 skill 產生報告／簡報
    GET  /runs/{sid}/artifacts/{kind}       →  下載主要格式（報告 docx／簡報 pptx）
    GET  /runs/{sid}/artifacts/{kind}/{fmt} →  下載指定格式

SSE 用 POST 而非 EventSource，因為 EventSource 帶不了 Authorization header；
前端沿用既有的 `authFetch` + ReadableStream 解析方式（同 /api/chat/messages）。

計費與配額與聊天共用同一套（`module/usage_quota` + `module/token_usage`）：
研究與產檔前各做一次 pre-flight（已超額回 429），結束後把 Agents SDK 回報的
用量寫進 `user_usage_quotas` 與 `token_usage_logs`（`chat_id` 為 NULL，
以 `caller` 分辨來源）。深度研究單次動輒數十萬 token，不記帳等於開一個
繞過配額的後門。
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

from app.backend.deep_research.config import (
    LENGTH_HARD_MAX,
    LENGTH_SPECS,
    QUERY_MAX_CHARS,
    exceeds_hard_max,
    public_config,
    resolve_length,
    resolve_model,
)
from app.backend.deep_research.researcher import stream_research
from app.backend.deep_research.session import Artifact, session_store
from app.backend.deep_research.skills import FORMATS, SKILLS, generate_artifact
from app.backend.deep_research.templates import resolve_theme
from app.backend.deep_research.sources import (
    PreparedSources,
    prepare_sources,
    read_uploads,
    source_manifest,
)
from app.backend.deep_research.usage import (
    AgentUsage,
    CALLER_RESEARCH,
    caller_for_artifact,
    record_agent_usage,
)
from app.backend.module.jwt import get_current_user_id
from app.backend.module.usage_quota import assert_preflight_llm_quota

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

    # Token 配額 pre-flight：已達上限直接 429，不進 OpenAI。
    # 一次深度研究比一輪聊天貴上兩個數量級，因此擋在最前面 ——
    # 連檔案都還沒讀，使用者也不會白等一個注定要失敗的上傳。
    await assert_preflight_llm_quota(user_id)

    # 讀檔與格式驗證放在 handler：格式錯誤要回正常的 4xx，而不是包在 SSE 裡
    uploads = await read_uploads(files or [])

    chosen_model = resolve_model(model)
    session = await session_store.create(
        user_id=user_key, model=chosen_model, query=cleaned_query
    )
    session.source_names = [u.filename for u in uploads]
    _active_runs.add(user_key)

    usage = AgentUsage()

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
                query=cleaned_query,
                model=chosen_model,
                prepared=prepared,
                usage=usage,
            ):
                name = event.pop("event")
                if name == "complete":
                    session.markdown = event["markdown"]
                    session.citations = event["citations"]
                    session.tools_used = event["tools_used"]
                    session.elapsed_ms = event["elapsed_ms"]
                    session.status = "done"
                    session.total_tokens += usage.total_tokens
                    await queue.put(
                        _sse(
                            "done",
                            {
                                "session_id": session.id,
                                "markdown": session.markdown,
                                "citations": session.citations,
                                "tools_used": session.tools_used,
                                "elapsed_ms": session.elapsed_ms,
                                "usage": usage.as_dict(),
                                "skills": [
                                    {
                                        "kind": k,
                                        "label": v["label"],
                                        "formats": [
                                            {"fmt": f, "label": FORMATS[f]["label"]}
                                            for f in v["renderers"]
                                        ],
                                    }
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
            # 記帳同樣不分成功失敗：中途斷線、逾時、跑到一半出錯的那幾輪
            # OpenAI 一樣收錢，不扣配額就等於使用者可以靠中斷來免費研究。
            # 走 _spawn_cleanup 而非直接 await —— 被 cancel 時 finally 裡的
            # await 會立刻再拋 CancelledError，記帳根本不會發生。
            _spawn_cleanup(
                record_agent_usage(
                    user_id=user_id,
                    model=chosen_model,
                    usage=usage,
                    caller=CALLER_RESEARCH,
                    session_id=session.id,
                )
            )
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
    length: Optional[int] = None   # 簡報頁數／報告小節數；超出範圍會被 clamp


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

    # 越過絕對上限的一律拒絕：正常前端送不出這種值，會出現只可能是直打 API。
    # 區間內的偏差交給 resolve_length() clamp，不必為了 25 頁回一個錯誤。
    if exceeds_hard_max(payload.length):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"篇幅請介於 1 到 {LENGTH_HARD_MAX} 之間。",
        )

    # 產檔是獨立的一次模型呼叫（研究結果重寫成報告／簡報），因此獨立 pre-flight：
    # 研究本身的用量就是在這之前記進配額的，剛好用爆的人會擋在這一步。
    await assert_preflight_llm_quota(user_id)

    label = SKILLS[kind]["label"]
    # clamp 在這裡做，好讓 done 事件回報「實際採用的數字」而非使用者送來的值
    length = resolve_length(kind, payload.length)
    unit = LENGTH_SPECS[kind]["unit"]
    usage = AgentUsage()

    async def produce(queue: asyncio.Queue) -> None:
        try:
            await queue.put(
                _sse("status", {"text": f"{label}撰寫中（{length}{unit}）…"})
            )

            files = await generate_artifact(
                kind=kind,
                model=session.model,
                query=session.query,
                research_markdown=session.markdown,
                citations=session.citations,
                theme=payload.theme,
                length=length,
                usage=usage,
            )
            session.total_tokens += usage.total_tokens

            artifact = Artifact(kind=kind, files={f.fmt: f for f in files})
            session.artifacts[kind] = artifact

            base = f"/api/deep-research/runs/{session.id}/artifacts/{kind}"
            await queue.put(
                _sse(
                    "done",
                    {
                        "kind": kind,
                        "label": label,
                        # 不帶格式的欄位一律指主要格式（報告 docx、簡報 pptx）
                        "filename": artifact.primary.filename,
                        "size": artifact.primary.size,
                        "download_path": base,
                        "theme": resolve_theme(payload.theme).id,
                        "length": length,
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
            # 與研究流程同理：前端斷線時 await 會再拋 CancelledError，記帳要丟出去跑
            _spawn_cleanup(
                record_agent_usage(
                    user_id=user_id,
                    model=session.model,
                    usage=usage,
                    caller=caller_for_artifact(kind),
                    session_id=session.id,
                )
            )
            with suppress(asyncio.CancelledError):
                await queue.put(None)

    return _sse_response(produce)


# ─────────────────────────────────────────────────────────────
# 4. 下載
# ─────────────────────────────────────────────────────────────
async def _artifact_response(
    session_id: str, kind: str, fmt: Optional[str], user_id: UUID
) -> Response:
    """
    取出指定產出並包成下載回應。

    `fmt=None` 代表主要格式 —— 舊版前端（Cloudflare 上還沒換掉的 JS）打的是
    不帶格式的網址，讓它繼續拿得到檔案比回 404 好。
    """
    session = await session_store.get(session_id, str(user_id))
    artifact = session.artifacts.get(kind) if session else None
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="檔案不存在或已逾時，請重新產生。",
        )

    if fmt is None:
        item = artifact.primary
    else:
        item = artifact.files.get(fmt)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"這份產出沒有 {fmt} 格式，可用的格式：{'、'.join(artifact.files)}。",
            )

    # 檔名含中文，必須用 RFC 5987 的 filename* 才不會在下載時變成亂碼
    disposition = f"attachment; filename*=UTF-8''{quote(item.filename)}"
    return Response(
        content=item.content,
        media_type=item.media_type,
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
        },
    )


@router.get("/runs/{session_id}/artifacts/{kind}")
async def download_artifact(
    session_id: str,
    kind: str,
    user_id: UUID = Depends(get_current_user_id),
):
    """下載主要格式；前端以 authFetch 取 blob 後再觸發瀏覽器下載。"""
    return await _artifact_response(session_id, kind, None, user_id)


@router.get("/runs/{session_id}/artifacts/{kind}/{fmt}")
async def download_artifact_format(
    session_id: str,
    kind: str,
    fmt: str,
    user_id: UUID = Depends(get_current_user_id),
):
    """下載指定格式（報告：docx / html；簡報：pptx / html）。"""
    return await _artifact_response(session_id, kind, fmt, user_id)


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
