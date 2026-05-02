import os
import time
import asyncio
from typing import TypedDict, Annotated, List, Tuple, Union, Dict, Any
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
async def search_stock_news(
    query: str,
    start_date: str = None,
    end_date: str = None,
    stock_code: str = None,
    news_type: str = None,
    keyword: str = None,
    stock_name: str = None,
):
    """
    搜尋股市相關新聞。
    注意：若使用者詢問「最近」、「最新」或「這幾天」，請根據當前提問時間計算出具體日期範圍。
    - 最近：通常指過去 14 天。
    範例：若今天是 2026-04-18，最近 14 天則 start_date="2026-04-04T00:00:00Z"。
    參數:
        query: 搜尋關鍵字。
        start_date: 開始時間 (ISO 格式，如 2026-04-01T00:00:00Z)。
        end_date: 結束時間 (ISO 格式)。
        stock_code: 股票代碼過濾 (如 "2330" 代表台積電)。若使用者提及特定個股，請填入其代碼。
        news_type: 新聞類型過濾 (如 "台股新聞" 或 "國際新聞")。
        keyword: 關鍵字標籤過濾 (如 "國巨"、"台積電"、"AI")。
            用於匹配新聞的 keywords 標籤。
        stock_name: 股票名稱過濾 (如 "勤誠"、"台積電")。
            用於匹配新聞的 stock_names 標籤。
        keyword、stock_code、stock_name 三者以 OR 邏輯匹配，只要其中任一命中即可。
        建議查詢個股時同時填入三者以最大化搜尋命中率。
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    query_vector = await embeddings.aembed_query(query)

    result = await search_news(
        query=query,
        query_embedding=query_vector,
        chat_id="agent_session",
        start_date=start_date,
        end_date=end_date,
        stock_code=stock_code,
        news_type=news_type,
        keyword=keyword,
        stock_name=stock_name,
    )

    # 格式化回傳給 LLM 讀取的字串
    if not result["context"]:
        return "找不到相關新聞。"

    output = "找到以下新聞片段：\n"
    for item in result["context"]:
        stocks_info = ""
        if item.get("stock_names"):
            stocks_info = f" [相關個股: {', '.join(item['stock_names'])}]"
        output += f"- 【{item['title']}】{stocks_info}: {item['content']}\n"
    return output

@tool
async def search_market_ai_analysis(
    query: str,
    start_date: str = None,
    end_date: str = None,
    sentiment: str = None,
    industry: str = None,
):
    """
    搜尋投研機構產出的 AI 市場分析報告。這比一般新聞更具備專業深度。
    注意：若問題包含「最近」、「最近一月」或「近況」，必須填入具體的 start_date。
    - 最近一月：指過去 30 天。
    參數:
        query: 搜尋關鍵字。
        start_date: 開始時間 (ISO 格式)。
        end_date: 結束時間 (ISO 格式)。
        sentiment: 情緒過濾 ("positive" / "negative" / "neutral")。若使用者問「利空」用 negative，「利多」用 positive。
        industry: 產業標籤過濾 (如 "半導體測試"、"能源")。
    """
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    query_vector = await embeddings.aembed_query(query)

    result = await search_ai_analysis(
        query=query,
        query_embedding=query_vector,
        chat_id="agent_session",
        start_date=start_date,
        end_date=end_date,
        sentiment=sentiment,
        industry=industry,
    )

    if not result["context"]:
        return "找不到相關的 AI 分析報告。"

    output = "找到以下 AI 分析報告：\n"
    for item in result["context"]:
        meta_parts = []
        if item.get("sentiment_label"):
            meta_parts.append(f"情緒: {item['sentiment_label']}")
        if item.get("industry_list"):
            meta_parts.append(f"產業: {', '.join(item['industry_list'])}")
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""

        output += f"\n--- 【{item['title']}】{meta} ---\n"
        output += f"{item['content']}\n"
        if item.get("source_news_titles"):
            output += f"  參考新聞: {'; '.join(item['source_news_titles'][:3])}\n"

    return output

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
    for src in result["sources"][:5]:
        sentiment_tag = f" [{src.get('sentiment_label', '')}]" if src.get("sentiment_label") else ""
        publish_date = src.get("publishAt", "")[:10] if src.get("publishAt") else "未知日期"
        output += f"- {src['title']} ({publish_date}){sentiment_tag}\n"
        if src.get("source_news_titles"):
            output += f"  參考新聞: {'; '.join(src['source_news_titles'][:2])}\n"

    return output

tools = [search_stock_news, search_market_ai_analysis, get_market_recommendations]

# --- 模型配置 ---
# 1. 導航模型 (Router): 速度快、工具調用準確
router_model_base = ChatOpenAI(model="gpt-5-mini", temperature=1)
# 2. 分析模型 (Analyst): 深度思考、文筆詳盡
analyst_model = ChatOpenAI(model="gpt-5", temperature=1)

# Router / 檢索反覆上限（與 trace 裡 node=="router" 筆數對齊）
# 前 N 次進入 router 可綁定工具；自第 N+1 次起卸除工具強制收斂（N = 此常數）。
ROUTER_MAX_CYCLES = 3

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

[精準過濾指引 - 善用進階參數]
- 若使用者提及**特定股票或公司名稱**（如「國巨」、「勤誠」、「台積電」），使用 search_stock_news 時請**同時**填入 stock_code 和 keyword 參數。
  例如查詢國巨：stock_code="2327", keyword="國巨", stock_name="國巨"。
  這三個參數以 OR 邏輯匹配，只要其中一個命中即可，能最大化搜尋命中率。
  常見代碼：台積電="2330"、鴻海="2317"、聯發科="2454"、台達電="2308"、國巨="2327"。
- 若使用者僅關心「台股」相關，使用 search_stock_news 時可設定 news_type="台股新聞"；若關心國際局勢則填 "國際新聞"。
- 若使用者詢問「利空消息」或「負面新聞」，使用 search_market_ai_analysis 時請設定 sentiment="negative"；反之「利多」設定 sentiment="positive"。
- 若使用者指定產業（如「半導體」、「能源」、「房地產」），使用 search_market_ai_analysis 時請填入 industry 參數，如 industry="半導體"。
- 上述進階參數皆為可選，只在使用者提問明確對應時才填入，不要強制猜測。

[空結果重試策略 - 極重要]
當你看到工具回傳的 Tool Message 包含「找不到」、「找不到相關新聞」、「找不到相關的 AI 分析報告」或「找不到推薦資訊」等空結果時，你「不得」直接回覆使用者或提供選項。你必須按照以下策略自動重試：
1. **擴大時間範圍**：將 start_date 往前推至 90 天或更久。
2. **切換新聞類型**：若 news_type 為「台股新聞」，試改為「國際新聞」，反之亦然。也可嘗試不帶 news_type。
3. **更換關鍵字**：嘗試用英文名稱（如「Yageo」代替「國巨」）、公司全稱、或更廣泛的產業關鍵字。
4. **放寬過濾條件**：移除 stock_code、sentiment、industry 等限制條件重試。
5. **嘗試其他工具**：若某個工具無結果，改用另一個工具搜尋。

每次重試至少嘗試 2-3 個不同策略（可同時並行呼叫多個工具）。只有在所有合理策略都已嘗試過但仍無結果時，才可以停止搜尋並告知使用者目前資料庫中確實沒有相關資料。
"""
    
    # 核心修正：動態挑選工具物件實體，進行硬性綁定
    # 從 tools 全域變數中，找出名稱符合 target_tools 的物件
    current_tools_to_bind = [t for t in tools if t.name in target_tools]
    
    # 萃取目前的 trace 紀錄，計算已經走過幾次 router 節點
    trace = state.get("trace", {})
    router_cycles = sum(1 for step in trace.get("steps", []) if step.get("node") == "router")

    if router_cycles >= ROUTER_MAX_CYCLES:
        # 達到上限：清空可用工具，並在 prompt 加上強制終止指令
        current_tools_to_bind = []
        system_prompt += f"\n\n[系統通知 - 極重要] 檢索次數已達上限 ({ROUTER_MAX_CYCLES}次)。請立刻根據你目前手邊已獲取的所有資料進行總結與回覆。"
        # 不綁定工具，強迫產出純文字
        dynamic_router = router_model_base
    else:
        # 這裡我們將 Router 模型動態綁定選後的工具
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
    """Analyst 節點：負責整合資料與產出研究報告（支援 astream_events token 串流）"""
    start_time = time.time()
    messages = state["messages"]
    current_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    analyst_prompt = f"""你是一位具備頂尖洞察力的資深分析師，擅長從碎片化的數據中提取最有價值的投資核心資訊。現在是 {current_now}。

    請將對話歷史中搜尋工具 (Tool Messages) 提供的一切資料，轉化為一封「清晰、優雅且具備專業點評」的分析報告。
    
    ### 撰寫規範：
    * **語意化結構**：請使用多級標題（如：## 市場核心觀察、### 關鍵標的追蹤），避免死板的 [1][2] 格式。
    * **數據強調**：對於重要的「日期、股價、成長率、股票代碼」，請務必使用 **粗體** 標註。
    * **深度合成**：將多來源資訊進行交叉驗證與點評，解釋其對投資者的實質意義與潛在風險。
    * **標的清單**：請在報告末尾優雅地列出提到的股票或產業，並附上入選理由。
    * **語氣風格**：流暢、專業且友善的繁體中文。
    * **細節**：如有數據，像是股價、漲跌幅、成交量等，任何數據請務必列出。
    """
    full_messages = [SystemMessage(content=analyst_prompt)] + messages

    # 使用 astream 逐 chunk 累積，讓 astream_events 能在 analyst 節點期間
    # 發出 on_chat_model_stream 事件供 API 層轉發 token
    full_content = ""
    async for chunk in analyst_model.astream(full_messages):
        full_content += chunk.content or ""

    from langchain_core.messages import AIMessage
    response = AIMessage(content=full_content)
    
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
    """執行工具呼叫節點 (v3: 並行執行 + Embedding 快取)"""
    last_message = state["messages"][-1]

    # 🆕 P4: 統一建立 Embedding 實例 + 快取，避免重複 API 呼叫
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    embedding_cache: Dict[str, List[float]] = {}

    async def get_cached_embedding(text: str) -> List[float]:
        if text not in embedding_cache:
            embedding_cache[text] = await embeddings.aembed_query(text)
        return embedding_cache[text]

    # 🆕 P3: 定義單一工具的執行邏輯，供並行調用
    async def execute_single_tool(tool_call) -> Tuple[ToolMessage, List[Dict[str, Any]]]:
        tool_name = tool_call["name"]
        args = tool_call["args"]
        retrieved = []

        if tool_name == "search_stock_news":
            query_text = args.get("query", "")
            raw_result = await search_news(
                query=query_text,
                query_embedding=(await get_cached_embedding(query_text)),
                chat_id="api_call",
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                stock_code=args.get("stock_code"),
                news_type=args.get("news_type"),
                keyword=args.get("keyword"),
                stock_name=args.get("stock_name"),
            )
            parts = []
            for c in raw_result.get("context", []):
                stock_info = ""
                if c.get("stock_names"):
                    stock_info = f" [相關: {', '.join(c['stock_names'])}]"
                parts.append(f"[{c.get('title')}]{stock_info}: {c.get('content')}")
            ai_content = "\n\n".join(parts) if parts else "找不到相關新聞。"
            for c in raw_result.get("context", []):
                retrieved.append({**c, "source_tool": "news"})

        elif tool_name == "search_market_ai_analysis":
            query_text = args.get("query", "")
            raw_result = await search_ai_analysis(
                query=query_text,
                query_embedding=(await get_cached_embedding(query_text)),
                chat_id="api_call",
                start_date=args.get("start_date"),
                end_date=args.get("end_date"),
                sentiment=args.get("sentiment"),
                industry=args.get("industry"),
            )
            parts = []
            for c in raw_result.get("context", []):
                meta = ""
                meta_parts = []
                if c.get("sentiment_label"):
                    meta_parts.append(f"情緒:{c['sentiment_label']}")
                if c.get("industry_list"):
                    meta_parts.append(f"產業:{','.join(c['industry_list'])}")
                if meta_parts:
                    meta = f" ({', '.join(meta_parts)})"
                entry = f"[{c.get('title')}]{meta}: {c.get('content')}"
                if c.get("source_news_titles"):
                    entry += f"\n  參考新聞: {'; '.join(c['source_news_titles'][:3])}"
                parts.append(entry)
            ai_content = "\n\n".join(parts) if parts else "找不到相關的 AI 分析報告。"
            for c in raw_result.get("context", []):
                retrieved.append({**c, "source_tool": "ai_analysis"})

        elif tool_name == "get_market_recommendations":
            raw_result = await search_recommendations(
                query_embedding=(await get_cached_embedding("推薦股票與強勢產業")),
                start_date=args.get("start_date"),
                end_date=args.get("end_date")
            )
            lines = []
            if raw_result.get("stocks"):
                lines.append(f"推薦股票清單: {', '.join(raw_result['stocks'])}")
            if raw_result.get("industries"):
                lines.append(f"推薦產業清單: {', '.join(raw_result['industries'])}")
            for src in raw_result.get("sources", [])[:5]:
                sentiment_tag = f" [{src.get('sentiment_label', '')}]" if src.get("sentiment_label") else ""
                lines.append(f"- {src.get('title')} ({src.get('publishAt', '')[:10]}){sentiment_tag}: {src.get('content', '')[:200]}")
            ai_content = "\n".join(lines) if lines else "找不到推薦資訊。"
            for s in raw_result.get("sources", []):
                retrieved.append({**s, "source_tool": "recommendations"})
        else:
            ai_content = f"錯誤：找不到工具 {tool_name}"

        msg = ToolMessage(content=ai_content, tool_call_id=tool_call["id"], name=tool_name)
        return msg, retrieved

    # 🆕 P3: 並行執行所有工具 (asyncio.gather)
    results = await asyncio.gather(*[
        execute_single_tool(tc) for tc in last_message.tool_calls
    ])

    tool_messages = [r[0] for r in results]
    new_retrieved_data = []
    for r in results:
        new_retrieved_data.extend(r[1])

    return {"messages": tool_messages, "retrieved_data": new_retrieved_data}

# --- 4. 定義邊界邏輯 (Conditional Edges) ---

async def retry_check(state: AgentState):
    """Safety net：若工具全部返回空結果且未達上限，注入重試提示強制 Router 重新搜尋"""
    last_message = state["messages"][-1]

    # 如果 Router 已經發起新的工具呼叫，直接通過
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return {"messages": [], "trace": state.get("trace", {})}

    trace = state.get("trace", {})
    router_cycles = sum(1 for s in trace.get("steps", []) if s.get("node") == "router")

    if router_cycles < ROUTER_MAX_CYCLES:
        # 檢查最近的 Tool Messages 是否全為空結果
        tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
        if tool_msgs:
            empty_keywords = ["找不到", "找不到相關", "找不到推薦"]
            recent_tools = tool_msgs[-3:]  # 檢查最近幾則
            all_empty = all(
                any(kw in m.content for kw in empty_keywords)
                for m in recent_tools
            )
            if all_empty:
                hint = SystemMessage(content=(
                    "[系統提示] 前一次搜尋所有工具均回傳空結果。"
                    "請參考你上一段回覆中提出的備選方案，選擇最合適的策略重試搜尋。"
                    "優先嘗試：擴大時間範圍、更換關鍵字（中/英文）、移除過濾條件。"
                    "不要詢問使用者，直接執行搜尋。"
                ))
                return {"messages": [hint], "trace": trace}

    return {"messages": [], "trace": state.get("trace", {})}

def should_continue_after_check(state: AgentState):
    """retry_check 之後的路由判斷"""
    last_message = state["messages"][-1]

    # 如果最後一條是 SystemMessage（重試提示），回到 router
    if isinstance(last_message, SystemMessage):
        return "router"

    # 如果有 tool_calls，去執行工具
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"

    # 否則進入分析
    return "analyst"

# --- 5. 建立 Graph ---
# 流程: Router → retry_check → (tools / router 重試 / analyst)

def create_chat_agent():
    workflow = StateGraph(AgentState)

    # 添加節點
    workflow.add_node("router", call_router)
    workflow.add_node("retry_check", retry_check)
    workflow.add_node("tools", call_tools)
    workflow.add_node("analyst", call_analyst)

    # 設置起點
    workflow.set_entry_point("router")

    # Router 產出後先經過 retry_check
    workflow.add_edge("router", "retry_check")

    # retry_check 判斷是否重試、執行工具、或進入分析
    workflow.add_conditional_edges("retry_check", should_continue_after_check)

    # 工具執行完回到 Router
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
