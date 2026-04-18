import os
import time
from typing import TypedDict, Annotated, List, Union, Dict, Any
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
    # 紀錄完整的對話歷史
    messages: Annotated[List[BaseMessage], lambda x, y: x + y]
    # 紀錄各階段的執行時間與中間細節 (例如: router_thought, tool_times, etc.)
    trace: Dict[str, Any]
    # 儲存檢索到的原始結構化數據，供 API 讀取 Metadata
    retrieved_data: Annotated[List[Dict[str, Any]], lambda x, y: x + y]
    # 前端指定的可用工具列表 (可選)
    enabled_tools: List[str]

# --- 2. 封裝工具 (Tool Wrappers) ---
# 這裡將之前寫好的 async 函式封裝成 LangChain 可識別的 @tool

@tool
async def search_stock_news(query: str, start_date: str = None, end_date: str = None):
    """
    搜尋股市相關新聞。
    注意：若使用者詢問「最近」、「最新」或「這幾天」，請根據當前提問時間計算出具體日期範圍。
    - 最近：通常指過去 14 天。
    範例：若今天是 2026-04-18，最近 14 天則 start_date="2026-04-04T00:00:00Z"。
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
    搜尋投研機構成產出的 AI 市場分析報告。這比一般新聞更具備專業深度。
    注意：若問題包含「最近」、「最近一月」或「近況」，必須填入具體的 start_date。
    - 最近一月：指過去 30 天。
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
    從分析報告中提取推薦的股票清單與關注產業。
    當使用者詢問「推薦什麼」、「有哪些潛力股」時必須使用。
    強烈規範：本工具請務必計算並填入時間範圍。預設建議搜尋過去 14 天內容。
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
router_model_base = ChatOpenAI(model="gpt-4o-mini", temperature=0)
# 2. 分析模型 (Analyst): 深度思考、文筆詳盡
analyst_model = ChatOpenAI(model="gpt-4o", temperature=0.5)

# --- 3. 定義節點 (Nodes) ---

async def call_router(state: AgentState):
    """Router 節點：負責決定要調用哪些工具"""
    start_time = time.time()
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 取得前端指定的工具清單
    enabled = state.get("enabled_tools", [])
    # 這裡列出我們系統中「真正實作」的工具名稱
    all_tool_names = ["search_stock_news", "search_market_ai_analysis", "get_market_recommendations"]
    
    # 邏輯：過濾掉前端傳入但不認識的名稱，確保 AI 不會試圖呼叫不存在的工具
    valid_enabled = [t for t in enabled if t in all_tool_names]
    
    # 決定最終要告訴 AI 的「可用工具箱」
    # 如果前端傳入的清單有合法項，就以該清單為主；否則全開
    target_tools = valid_enabled if valid_enabled else all_tool_names
    
    system_prompt = f"""你是一個專業股市助理。當前提問時間為：{current_now}。
你的任務是判斷應調用哪些工具來回答問題。

[重要執行指令 - 必讀]
1. **嚴禁憑空回答**：對於任何涉及具體標的、公司名單、產業趨勢的問題，你「絕對不得」僅憑內部記憶回答。
2. **數據優先**：你必須透過工具來獲得最新且具備來源證明的資料。
3. **工具導向**：如果使用者指定了工具 (目前可用：{', '.join(target_tools)})，代表使用者只信任這些來源。你必須從中選擇最相關的工具來執行，以獲取資訊。
4. **回覆策略**：只有在「執行完工具並拿到資料後」，你才可以在下一個階段進行分析。在 Router 階段，你的首要任務是「去查資料」。

[時間規範]
- 只要提到「最近」、「最新」或「這週」，請統一計算為「過去 14 天」並填入 start_date。
"""
    
    # 核心修正：動態挑選工具物件實體，進行硬性綁定
    # 從 tools 全域變數中，找出名稱符合 target_tools 的物件
    current_tools_to_bind = [t for t in tools if t.name in target_tools]
    
    # 這裡我們將 Router 模型動態綁定選後的工具，並改用 SystemMessage 增強指令強度
    dynamic_router = router_model_base.bind_tools(current_tools_to_bind)
    router_prompt = [SystemMessage(content=system_prompt)] + messages
    response = await dynamic_router.ainvoke(router_prompt)
    
    execution_time = time.time() - start_time
    
    # 建立更有意義的思考軌跡
    thought = ""
    if hasattr(response, "tool_calls") and response.tool_calls:
        tool_names = [tc["name"] for tc in response.tool_calls]
        thought = f"為了精準回答，啟動 {', '.join(tool_names)} 來獲取股市資訊。"
    else:
        thought = response.content if response.content else "資料已備齊，準備撰寫專業報告。"

    # 紀錄 Router 階段資訊 (使用更加詳細且平坦的結構)
    trace = state.get("trace", {})
    if "steps" not in trace:
        trace["steps"] = []
    
    formatted_tool_calls = []
    if hasattr(response, "tool_calls") and response.tool_calls:
        for tc in response.tool_calls:
            args = tc.get("args", {})
            formatted_tool_calls.append({
                "name": tc["name"],
                "query": args.get("query"),
                "start_date": args.get("start_date"),
                "end_date": args.get("end_date"),
                "raw_args": args # 保留原始參數供參考
            })

    trace["steps"].append({
        "node": "router",
        "execution_time": round(execution_time, 3),
        "tool_calls": formatted_tool_calls,
        "thought": thought
    })
    
    return {"messages": [response], "trace": trace}

async def call_analyst(state: AgentState):
    """Analyst 節點：負責整合資料與產出研究報告"""
    start_time = time.time()
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # 使用最強的模型和最詳盡的指令來撰寫最後的回覆
    analyst_prompt = f"""你是一位資深首席股市分析師。現在是 {current_now}。
    請根據對話歷史中搜尋工具 (Tool Messages) 回傳的所有原始資料，撰寫一份極具深度且專業的投資研究報告。

    你的報告**必須**嚴格遵守以下格式規範：
    1. **[關鍵標的清單]**：如果你在工具回傳中看到了「推薦股票」或「推薦產業」，請務必在報告的一開始或結尾，使用「無序列表 (Bullet Points)」將它們**完整**列出，不可省略任何一個。
    - 格式範例：
        * 股票：台積電(2330)、世芯-KY(3661)、...
        * 產業：AI 伺服器、先進封裝...
    2. **[深度分析內容]**：基於新聞與分析報告，解讀市場脈動。
    3. **[數據引用]**：必須引用具體的日期、股價或損益預估數據。
    4. **[繁體中文]**：一律使用專業的繁體中文投研風格。

    請注意：不要為了美觀而進行「過度摘要」。使用者需要看到完整的檢索資訊，加上你的專業點評。
    """
    full_messages = [SystemMessage(content=analyst_prompt)] + messages
    response = await analyst_model.ainvoke(full_messages)
    
    execution_time = time.time() - start_time
    trace = state.get("trace", {})
    if "steps" not in trace:
        trace["steps"] = []
        
    trace["steps"].append({
        "node": "analyst",
        "execution_time": round(execution_time, 3),
        "content": response.content
    })
    
    # 同時保留一個最後的摘要紀錄方便 API 讀取
    trace["final_analyst"] = {
        "execution_time": round(execution_time, 3),
        "content": response.content
    }
    
    return {"messages": [response], "trace": trace}

async def call_tools(state: AgentState):
    """執行工具呼叫節點"""
    last_message = state["messages"][-1]
    tool_messages = []
    new_retrieved_data = []
    
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        args = tool_call["args"]
        
        # 執行工具並取得原始結構化結果
        if tool_name == "search_stock_news":
            raw_result = await search_news(
                query=args.get("query"),
                query_embedding=(await OpenAIEmbeddings().aembed_query(args.get("query"))),
                chat_id="api_call",
                start_date=args.get("start_date"),
                end_date=args.get("end_date")
            )
            # 給 AI 看的簡化內容
            ai_content = "\n\n".join([f"[{c.get('title')}]: {c.get('content')}" for c in raw_result.get("context", [])])
            # 給 API 看的完整 Metadata
            for c in raw_result.get("context", []):
                new_retrieved_data.append({**c, "source_tool": "news"})
                
        elif tool_name == "search_market_ai_analysis":
            raw_result = await search_ai_analysis(
                query=args.get("query"),
                query_embedding=(await OpenAIEmbeddings().aembed_query(args.get("query"))),
                chat_id="api_call",
                start_date=args.get("start_date"),
                end_date=args.get("end_date")
            )
            ai_content = "\n\n".join([f"[{c.get('title')}]: {c.get('content')}" for c in raw_result.get("context", [])])
            for c in raw_result.get("context", []):
                new_retrieved_data.append({**c, "source_tool": "ai_analysis"})
                
        elif tool_name == "get_market_recommendations":
            raw_result = await search_recommendations(
                query_embedding=(await OpenAIEmbeddings().aembed_query("推薦股票與強勢產業")),
                start_date=args.get("start_date"),
                end_date=args.get("end_date")
            )
            ai_content = f"推薦股票清單: {raw_result.get('stocks')}\n推薦產業清單: {raw_result.get('industries')}"
            for s in raw_result.get("sources", []):
                new_retrieved_data.append({**s, "source_tool": "recommendations"})
        else:
            ai_content = f"錯誤：找不到工具 {tool_name}"
            
        tool_messages.append(ToolMessage(content=ai_content, tool_call_id=tool_call["id"], name=tool_name))
        
    return {"messages": tool_messages, "retrieved_data": new_retrieved_data}

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
        inputs = {"messages": [HumanMessage(content="最近台積電表現如何，也告訴我一些近期推薦的股票跟要關注的產業，還有一些股市分析結果。")]}
        
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
