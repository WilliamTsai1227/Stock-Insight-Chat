import time
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime

# 導入 Agent 核心
from app.backend.agent.chat import create_chat_agent
from langchain_core.messages import HumanMessage, ToolMessage

router = APIRouter(prefix="/chat", tags=["Chat"])

# --- 請求相關格式 (Request Schemas) ---

class AgentConfig(BaseModel):
    enabled_tools: Optional[List[str]] = None

class MessageRequest(BaseModel):
    query: str
    chat_id: Optional[str] = None  # UUID string
    agent_config: Optional[AgentConfig] = None

# --- 回應相關格式 (Response Schemas) ---

class RouterTrace(BaseModel):
    execution_time: float
    tool_calls: List[Dict[str, Any]]
    router_thought: str
class MessageResponse(BaseModel):
    status: str
    chat_id: str
    total_execution_time: float
    steps: List[Dict[str, Any]] # 改為儲存所有步驟
    final_content: str        # 最後的分析結果截讀
    retrieval_sources: List[Dict[str, Any]]

# --- 初始化 Agent ---
agent_app = create_chat_agent()

@router.post("/messages", response_model=MessageResponse)
async def get_ai_response(request: MessageRequest):
    start_total = time.time()
    
    # 1. Session 隔離機制
    current_chat_id = request.chat_id if request.chat_id else str(uuid.uuid4())
    
    # 2. 準備 Agent 初始狀態
    enabled_tools = []
    if request.agent_config and request.agent_config.enabled_tools:
        enabled_tools = request.agent_config.enabled_tools
        
    initial_state = {
        "messages": [HumanMessage(content=request.query)],
        "trace": {},
        "retrieved_data": [], # 初始化結構化數據存儲
        "enabled_tools": enabled_tools # 傳遞自選工具清單
    }
    
    # 3. 配置 Thread
    config = {"configurable": {"thread_id": current_chat_id}}
    
    try:
        # 4. 執行 Agent Graph
        final_state = await agent_app.ainvoke(initial_state, config=config)
        
        # 5. 總結執行數據
        end_total = time.time()
        total_time = round(end_total - start_total, 3)
        
        # 6. 提取 Trace 資訊
        trace_data = final_state.get("trace", {})
        steps = trace_data.get("steps", [])
        
        # 找有工具調用的 Router 紀錄
        router_info = next((s for s in steps if s["node"] == "router" and s.get("tool_calls")), {})
        if not router_info and steps:
            router_info = next((s for s in steps if s["node"] == "router"), 
                               {"execution_time": 0, "tool_calls": [], "thought": ""})
            
        analyst_info = trace_data.get("final_analyst", {"execution_time": 0, "content": "No content generated."})
        
        # 7. 提取 檢索來源 (從新的 retrieved_data 欄位提取結構化 Metadata)
        sources = []
        raw_sources = final_state.get("retrieved_data", [])
        for item in raw_sources:
            sources.append({
                "tool": item.get("source_tool"),
                "title": item.get("title"),
                "publishAt": item.get("publishAt"),
                "url": item.get("url"),
                "mongo_id": item.get("mongo_id"),
                "content_preview": item.get("content", "")[:100] + "..." 
            })

        return {
            "status": "success",
            "chat_id": current_chat_id,
            "total_execution_time": total_time,
            "steps": steps, # 展示所有 ReAct 歷程
            "final_content": analyst_info.get("content", ""),
            "retrieval_sources": sources
        }
        
    except Exception as e:
        print(f"Agent Execution Error: {e}")
        raise HTTPException(status_code=500, detail=f"Agent 執行失敗: {str(e)}")
