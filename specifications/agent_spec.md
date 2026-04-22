# Stock Insight Agent 規格說明 (Agent Specification)

本文件定義了「股市洞察系統」的核心對話大腦 (Agent) 實作邏輯與架構設計。

## 1. 核心開發框架：LangGraph (ReAct)

系統採用 **LangGraph** 的 **Stateful Graph** 架構實作 **ReAct (Reasoning and Acting)** 循環，確保 Agent 具備嚴謹的思考流程。

### 核心節點與流轉：
1.  **Router (導航)**: 使用 `gpt-5-mini` 進行意圖辨識。它決定接下來要調用哪些工具，或是否已經具備充足資訊。支援動態綁定前端指定的工具集。
2.  **Retry Check (重試檢查)**: 一個安全閥節點。若工具回傳空結果且未達重試上限 (**MAX_CYCLES = 5**)，則注入提示引導 Router 調整策略（如擴大時間範圍或更換關鍵字）。
3.  **Tools (工具箱)**: 並行執行 (`asyncio.gather`) 所有被觸發的工具。執行完畢後**回到 Router** 進行下一輪思考。
4.  **Analyst (分析)**: 使用 `gpt-5` 進行深度報告撰寫。這保證了最後的分析文本具備極高的專業度與 Gemini 風格的高層次洞察。

---

## 2. 狀態定義 (Agent State)

```python
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages] # 完全對話歷史
    trace: Dict[str, Any]           # 紀錄各節點執行時間、Tool Calls 與 Thought
    retrieved_data: List[Dict]      # 儲存 Qdrant 的原始結構化數據，供 API 溯源
    enabled_tools: List[str]        # 前端動態指定的可用工具列表
```

---

## 3. 動態工具綁定 (Dynamic Tool Binding)

為了防止 AI 的幻覺與資源浪費，系統實作了**硬性工具過濾**：
*   **權限檢查**: 在 `call_router` 階段，系統會讀取 `enabled_tools`。
*   **動態繫結**: 僅將「被允許且系統已實作」的工具物件 (Tool objects) 透過 `.bind_tools()` 提供給 Router。
*   **目前支援工具**: `search_stock_news`, `search_market_ai_analysis`, `get_market_recommendations`。

---

## 4. 數據優先與空結果策略 (Data-First & Retry Policy)

系統透過強化的 **System Prompt** 與 **Retry Check** 確保答案的真實性：
1.  **禁令**: 嚴禁僅憑內部記憶回答具體標的或公司清單。
2.  **檢證**: 必須透過工具獲取具備來源證明的資料。
3.  **自動重試**: 當工具回傳「找不到」時，Agent 會自動嘗試以下策略：
    *   擴大時間範圍 (例如推至 90 天)。
    *   切換關鍵字 (如中英文互換)。
    *   放寬過濾條件 (移除 stock_code 或 sentiment 限制)。
4.  **時間規範**: 針對「最新」、「近期」等詞彙，統一換算為伺服器時間往前回推 14 天。

---

## 5. 執行追蹤 (Transparency & Tracing)

系統為前端提供了全透明的執行指標：
*   **執行步驟 (Steps)**: 詳細列出每一輪 Router 的 `thought` 與 `tool_calls`（含參數）。
*   **性能監測**: 記錄每個節點的 `execution_time`。
*   **精確來源 (retrieval_sources)**: 每個回答均附帶原始資料的 `mongo_id`、`publishAt`、`url` 與 `score`。

---

## 6. 指令規範 (Analyst Instructions)

最後的回覆必須遵循 **Gemini 風格**：
1.  **語意化結構**: 使用多級標題，避免死板的條列式。
2.  **數據高亮**: 重要的「日期、股價、股票代碼 (XXXX)」必須使用 **粗體**。
3.  **深度合成**: 交叉驗證多來源資訊，解釋對投資者的實質意義。
4.  **標的總結**: 在報告末尾列出提到的股票，並附上入選理由。

---

## 7. Token 管理與計量 (Token Management)

Agent 產出的 `trace` 包含完整的執行軌跡。後端 API 層會負責：
1.  **統計消耗**: 加總所有 LLM 呼叫的 Token。
2.  **非同步寫入**: 將數據寫入 PostgreSQL 的 `token_usage_logs`。
3.  **配額扣除**: 原子化更新 `user_usage_quotas`。
