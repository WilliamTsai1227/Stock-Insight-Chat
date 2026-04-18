# Stock Insight Agent 規格說明 (Agent Specification)

本文件定義了「股市洞察系統」的核心對話大腦 (Agent) 實作邏輯與架構設計。

## 1. 核心開發框架：LangGraph (ReAct)

系統採用 **LangGraph** 的 **Stateful Graph** 架構實作 **ReAct (Reasoning and Acting)** 循環，確保 Agent 具備嚴謹的思考流程。

### 核心節點與流轉：
1.  **Router (導航)**: 使用 `gpt-4o-mini` 進行意圖辨識。它決定接下來要調用哪個工具，或是否已經具備充足資訊。
2.  **Tools (工具箱)**: 執行搜尋操作。執行完畢後**強制回到 Router** 讓 AI 檢視搜尋結果 (ReAct)。
3.  **Analyst (分析)**: 使用 `gpt-4o` 進行深度報告撰寫。這保證了最後的分析文本具備極高的專業度與文采。

---

## 2. 狀態定義 (Agent State)

```python
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages] # 完全對話歷史
    trace: Dict[str, Any]           # 紀錄各節點執行時間與思考 (Thoughts)
    retrieved_data: List[Dict]      # 儲存 Qdrant/Mongo 的原始結構化數據
    enabled_tools: List[str]        # 前端動態指定的工具權限
```

---

## 3. 動態工具綁定 (Dynamic Tool Binding)

為了防止 AI 的幻覺與資源浪費，系統實作了**硬性工具過濾**：
*   **權限檢查**: 在 `call_router` 階段，系統會讀取 `enabled_tools`。
*   **動態繫結**: 僅將「被允許」的工具物件 (Tool objects) 透過 `.bind_tools()` 提供給 Router。
*   **結果**: AI 絕對無法調用未被授權的工具，確保檢索範圍完全受控。

---

## 4. 數據優先政策 (Data-First Policy)

系統透過強化的 **System Prompt** 強制執行以下原則，解決 AI 「腦補」供應鏈名單的問題：
1.  **禁令**: 嚴禁僅憑內部記憶回答具體標的或公司清單。
2.  **檢證**: 即便 AI 知曉答案，也必須調用工具獲取具備「數據來源報告」的證明文件。
3.  **時間規範**: 針對「最新」、「近期」等詞彙，統一換算為伺服器時間往前回推 14 天。

---

## 5. 執行追蹤 (Transparency & Tracing)

系統為前端提供了全透明的執行指標：
*   **執行步驟 (Steps)**: 詳細列出每一輪 Router 的 `thought` 與 `tool_calls`。這讓使用者能看見 AI 是如何逐步收縮範圍並找到答案的。
*   **精確來源 (retrieval_sources)**: 每個回答均附帶原始資料的 `mongo_id`、`publishAt` 與 `url`。

---

## 6. 指令規範 (Analyst Instructions)

最後的回覆必須遵循以下格式：
1.  **[關鍵標的清單]**: 必須以 Bullet Points 列出所有提及的股票名稱與代碼。
2.  **[深度分析內容]**: 將多來源資訊統合，進行優缺點或風險分析。
3.  **[數據引用]**: 標記原始報導的重點數據或發生日期。

---

## 7. 驗證腳本

*   可以直接執行 `python app/backend/agent/chat.py` 進行循環邏輯測試。
*   推薦測試案例：*「請幫我找出台積電的主要化學供應商，並查詢該公司最近一週的風險。」*（此案例可觸發 2 次以上的 Router 循環）。
