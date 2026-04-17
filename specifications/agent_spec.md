# Stock Insight Agent 規格說明 (Agent Specification)

本文件定義了「股市洞察系統」的核心對話大腦 (Agent) 實作邏輯與架構設計。

## 1. 核心開發框架：LangGraph (ReAct)

為了確保 Agent 具備嚴謹的思考流程與工具調用能力，系統採用 **LangGraph** 的 **Stateful Graph** 架構實作 **ReAct (Reasoning and Acting)** 循環。

### 設計優點：
*   **狀態化記憶 (Stateful Memory)**：透過 `TypedDict` 管理 `messages` 列表，自動維護對話上下文。
*   **精準控制 (Explicit Control)**：比起傳統 LangChain Agent，LangGraph 能更明確地定義「思考 (Agent)」與「執行 (Tools)」之間的邊界與循環條件。
*   **非同步支援**：完整支援 `async/await`，適合高併發的 Web 服務場景。

---

## 2. 狀態定義 (Agent State)

```python
class AgentState(TypedDict):
    # 紀錄完整的對話歷史，包含 Human, AI, 與 Tool 的回傳訊息
    messages: Annotated[List[BaseMessage], add_messages]
```

---

## 3. 工具整合 (Tooling Strategy)

Agent 並不直接處理向量搜尋的細節，而是將語義轉化工作委託給工具內部：

1.  **search_stock_news**: 
    *   **Agent 責任**: 提取使用者問題中的「關鍵字」或「事件描述」。
    *   **工具責任**: 將文字轉為 1536 維向量 -> Qdrant 相似度檢索 -> 返回帶有 [標題] 的新聞片段。
2.  **search_market_ai_analysis**:
    *   **Agent 責任**: 判斷是否需要宏觀產業分析或法人觀點。
    *   **工具責任**: 檢索 AI 分析報告資料庫 -> 返回深度分析摘要。
3.  **get_market_recommendations**:
    *   **Agent 責任**: 當使用者詢問推薦、潛力比對或產業關注時啟動。
    *   **工具責任**: 從 `ai_analysis` 集合中提取結構化的 `stock_list` 與 `industry_list` 並進行去重。

---

## 4. 時間同步邏輯 (Temporal Consistency)

為了確保分析的時效性，Agent 在單次對話任務中會遵循以下邏輯：
*   **動態日期鎖定**: 每輪對話開始時，AI 會獲取當前系統時間（預設為 2026 年）。
*   **跨工具同步**: 當 AI 確定了時間區間（如：2026-04-10 至 2026-04-17）後，會將 **「完全相同的 start_date 與 end_date」** 同時傳遞給所有的工具（News, Analysis, Recommendations），確保得出的結論是基於同一個時間窗口的數據。

---

## 5. 工作流程規劃 (The Graph)

系統運行路徑如下：

1.  **START** -> `agent` 節點 (LLM 思考是否需要工具)
2.  **Conditional Edge**:
    *   若 LLM 決定調用工具 -> 跳轉至 `tools` 節點。
    *   若 LLM 決定直接回答 -> 跳轉至 `END`。
3.  **Loop**: `tools` 執行完畢後會跳回 `agent`，讓 LLM 根據工具回傳的資料進行下一步判斷（可能繼續搜，也可能直接回答）。

---

## 6. 模型與配置 (Model Configuration)

*   **LLM 模型**: `gpt-4o` (最新旗艦模型，具備強大的 Tool Calling 與多語言處理能力)。
*   **嵌入模型**: `text-embedding-3-small` (高效能且低成本的向量轉換模型)。
*   **連線配置**: 使用 `.env` 載入 `OPENAI_API_KEY`，確保系統安全性。

---

## 7. 測試與驗證

*   可以使用 `test/backend/agent/` 下的腳本（待開發）進行端到端測試。
*   本機開發可直接執行 `python app/backend/agent/chat.py` 進行節點流轉驗證。
