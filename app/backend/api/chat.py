import time
import uuid
import json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Any
from uuid import UUID
from langchain_core.messages import HumanMessage

from app.backend.agent.chat import create_chat_agent
from app.backend.module.jwt import get_current_user_id

router = APIRouter(tags=["Chat"])


class AgentConfig(BaseModel):
    enabled_tools: Optional[List[str]] = None


class MessageRequest(BaseModel):
    query: str
    chat_id: Optional[str] = None
    agent_config: Optional[AgentConfig] = None


agent_app = create_chat_agent()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/api/chat/messages")
async def get_ai_response(
    request: MessageRequest,
    current_user_id: UUID = Depends(get_current_user_id),
):
    """
    SSE 串流端點：
      thinking   — Router 思考文字（小型 pill）
      tool_start — 工具開始執行
      tool_done  — 工具執行完畢
      token      — Analyst LLM 逐字輸出（只在 analyst 節點期間）
      done       — 全部完成，含 steps / sources
      error      — 例外
    """
    current_chat_id = request.chat_id if request.chat_id else str(uuid.uuid4())
    enabled_tools: List[str] = []
    if request.agent_config and request.agent_config.enabled_tools:
        enabled_tools = request.agent_config.enabled_tools

    initial_state = {
        "messages": [HumanMessage(content=request.query)],
        "trace": {},
        "retrieved_data": [],
        "enabled_tools": enabled_tools,
    }
    config = {"configurable": {"thread_id": current_chat_id}}

    async def event_generator():
        start_total = time.time()

        # 備援累積資料（不依賴 final_state）
        accumulated_steps: list = []
        accumulated_retrieved: list = []
        accumulated_final_analyst: dict = {}

        # ── 關鍵：用 chain_start / chain_end 追蹤 analyst 節點是否正在執行 ──
        in_analyst = False

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
                "chat_id": current_chat_id,
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

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield _sse("error", {"message": f"Agent 執行失敗: {str(e)}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
