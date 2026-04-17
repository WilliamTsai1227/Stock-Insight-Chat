import os
from typing import TypedDict, Annotated, List, Union
from datetime import datetime
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END

# 導入我們之前寫好的工具函式
from app.backend.tools.news import search_news
from app.backend.tools.ai_analysis import search_ai_analysis, search_recommendations

load_dotenv()

# --- 1. 定義狀態 (State) ---
class AgentState(TypedDict):
    # 這是 Graph 的主狀態，紀錄所有對話訊息
    messages: Annotated[List[BaseMessage], lambda x, y: x + y]

# --- 2. 封裝工具 (Tool Wrappers) ---
# 這裡將之前寫好的 async 函式封裝成 LangChain 可識別的 @tool

@tool
async def search_stock_news(query: str, start_date: str = None, end_date: str = None):
    """
    搜尋新聞片段。支援時間過濾。
    參數:
        query: 關鍵字。
        start_date: 開始時間 (ISO 格式，如 2026-04-01T00:00:00Z)。
        end_date: 結束時間 (ISO 格式)。
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    query_vector = await embeddings.aembed_query(query)
    
    result = await search_news(
        query=query, 
        query_embedding=query_vector, 
        chat_id="agent_session",
        start_date=start_date,
        end_date=end_date
    )
    
    # 格式化回傳給 LLM 讀取的字串
    if not result["context"]:
        return "找不到相關新聞。"
    
    output = "找到以下新聞片段：\n"
    for item in result["context"]:
        output += f"- 【{item['title']}】: {item['content']}\n"
    return output

@tool
async def search_market_ai_analysis(query: str, start_date: str = None, end_date: str = None):
    """
    搜尋 AI 市場分析報告。支援時間過濾。
    參數:
        query: 關鍵字。
        start_date: 開始時間 (ISO 格式)。
        end_date: 結束時間 (ISO 格式)。
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    query_vector = await embeddings.aembed_query(query)
    
    result = await search_ai_analysis(
        query=query, 
        query_embedding=query_vector, 
        chat_id="agent_session",
        start_date=start_date,
        end_date=end_date
    )
    
@tool
async def get_market_recommendations(start_date: str = None, end_date: str = None):
    """
    獲取近期推薦的股票清單與關注產業。當使用者詢問「推薦什麼股票」、「有哪些潛力股」或「關注哪些產業」時使用。
    參數:
        start_date: 開始時間 (ISO 格式)。
        end_date: 結束時間 (ISO 格式)。
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    # 使用固定關鍵字來抓取推薦類型的報告
    query_vector = await embeddings.aembed_query("推薦股票、強勢產業、潛力標的、看好板塊")
    
    result = await search_recommendations(
        query_embedding=query_vector,
        start_date=start_date,
        end_date=end_date
    )
    
    if not result["stocks"] and not result["industries"]:
        return "在此時間區間內找不到特定推薦的股票或產業。"
    
    output = "根據近期分析報告，整理出的推薦資訊如下：\n"
    if result["stocks"]:
        output += f"📈 推薦股票：{', '.join(result['stocks'])}\n"
    if result["industries"]:
        output += f"🏭 關注產業：{', '.join(result['industries'])}\n"
        
    output += "\n資料來源報告：\n"
    for src in result["sources"][:3]: # 只列出前三個來源標題
        output += f"- {src['title']} ({src['publishAt'][:10]})\n"
        
    return output

tools = [search_stock_news, search_market_ai_analysis, get_market_recommendations]

# --- 模型配置 ---
# 1. 導航模型 (Router): 速度快、工具調用準確
router_model = ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(tools)
# 2. 分析模型 (Analyst): 深度思考、文筆詳盡
analyst_model = ChatOpenAI(model="gpt-4o", temperature=0.5)

# --- 3. 定義節點 (Nodes) ---

async def call_router(state: AgentState):
    """Router 節點：負責決定要調用哪些工具"""
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 使用簡單的、速度快的提示詞來決定工具
    router_prompt = f"你是一個專業助手。當前提問時間為：{current_now}。請判斷是否需要調用工具來回答使用者的股市問題。"
    
    full_messages = [SystemMessage(content=router_prompt)] + messages
    response = await router_model.ainvoke(full_messages)
    return {"messages": [response]}

async def call_analyst(state: AgentState):
    """Analyst 節點：負責整合所有工具回傳的資料，產出深度研究報告"""
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 使用最強的模型和最詳盡的指令來撰寫最後的回覆
    analyst_prompt = f"""你是一位資深首席股市分析師。現在是 {current_now}。
請根據對話歷史中搜尋工具 (Tool Messages) 回傳的所有原始資料，撰寫一份極具深度且專業的投資研究報告。

你的任務指南：
1. **結構化呈現**：使用清晰的標題（如：市場動態分析、個股深度解讀、潛力投資標的、風險評估）。
2. **數據驅動**：引用搜尋結果中的具體數據（如股價、獲利預估、新聞日期）。
3. **主動聯想**：從推薦清單中，挑選與使用者問題最相關的 3-5 檔股票進行重點點評，不要只是列出名字。
4. **時效性**：確保分析是基於最新的資料。
5. **風格**：專業、客觀且具備洞察力。繁體中文輸出。
6. **風險提示**：結尾必備投資風險提醒。
"""
    
    full_messages = [SystemMessage(content=analyst_prompt)] + messages
    response = await analyst_model.ainvoke(full_messages)
    # 我們要把最後一次的分析結果回傳
    return {"messages": [response]}

async def call_tools(state: AgentState):
    """執行工具呼叫節點"""
    last_message = state["messages"][-1]
    tool_messages = []
    
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        args = tool_call["args"]
        
        # 根據工具名稱執行對應函式
        if tool_name == "search_stock_news":
            content = await search_stock_news.ainvoke(args)
        elif tool_name == "search_market_ai_analysis":
            content = await search_market_ai_analysis.ainvoke(args)
        elif tool_name == "get_market_recommendations":
            content = await get_market_recommendations.ainvoke(args)
        else:
            content = f"錯誤：找不到工具 {tool_name}"
            
        tool_messages.append(ToolMessage(content=str(content), tool_call_id=tool_call["id"]))
        
    return {"messages": tool_messages}

# --- 4. 定義邊界邏輯 (Conditional Edges) ---

def should_continue(state: AgentState):
    """判斷是否需要繼續呼叫工具，若不需要則進入分析階段"""
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    # 沒有工具調用了，進入最終分析節點
    return "analyst"

# --- 5. 建立 Graph ---

def create_chat_agent():
    workflow = StateGraph(AgentState)

    # 添加節點
    workflow.add_node("router", call_router)
    workflow.add_node("tools", call_tools)
    workflow.add_node("analyst", call_analyst)

    # 設置起點
    workflow.set_entry_point("router")

    # 添加跳轉邏輯
    workflow.add_conditional_edges("router", should_continue)
    workflow.add_edge("tools", "router")
    workflow.add_edge("analyst", END)

    return workflow.compile()

# --- 實例化 Agent ---
app = create_chat_agent()

# --- 測試執行 (範例) ---
if __name__ == "__main__":
    import asyncio
    
    async def run_test():
        print("🤖 Stock Insight Agent 啟動測試...")
        inputs = {"messages": [HumanMessage(content="最近台積電表現如何，也告訴我一些近期推薦的股票跟要關注的產業。")]}
        
        async for output in app.astream(inputs):
            for key, value in output.items():
                print(f"\n[進入節點: {key}]")
                last_msg = value["messages"][-1]
                
                # 判斷訊息類型並印出內容
                if isinstance(last_msg, AIMessage):
                    if last_msg.tool_calls:
                        for tc in last_msg.tool_calls:
                            print(f"🛠️  AI 決定調用工具: {tc['name']}")
                            print(f"   參數: {tc['args']}")
                    else:
                        print(f"💬 AI 回答: {last_msg.content}")
                
                elif isinstance(last_msg, ToolMessage):
                    print(f"✅ 工具回傳成功 (長度: {len(last_msg.content)} 字元)")
                    # 如果你想看工具抓到的具體內容，可以取消下面這行註釋
                    # print(f"   內容摘要: {last_msg.content[:200]}...")

    asyncio.run(run_test())
