# 搜尋工具規格說明 (Search Tools Specification)

本文件定義了 Agent 調用的核心搜尋工具技術規範，旨在確保向量檢索與結構化資料提取的一致性。

## 1. 核心元件
*   **向量庫 (Vector Store)**: Qdrant
*   **文件庫 (Document Store)**: MongoDB
*   **Embeddings**: OpenAI `text-embedding-3-small` (1536 dims)
*   **連線方式**: 全面採用 **Async (非同步)** 客戶端以支援高併發。

---

## 2. 工具清單 (Tool Registry)

### A. 市場新聞搜尋 (`search_stock_news`)
*   **目標**: 檢索最新的市場新聞片段。
*   **Input**:
    *   `query` (str): 搜尋關鍵字。
    *   `start_date` (ISO Date, Optional): 開始日期。
    *   `end_date` (ISO Date, Optional): 結束日期。
*   **邏輯**: 執行向量搜尋並套用 `publishAt` 時間過濾器。
*   **Output**: 返回 `[標題]: 內容` 格式的新聞片段。

### B. AI 深度分析搜尋 (`search_market_ai_analysis`)
*   **目標**: 檢索專業報告與法人觀點摘要。
*   **Input**: 同上。
*   **邏輯**: 從 `ai_analysis` 集合進行語義搜尋，定位專業見解。
*   **Output**: 返回報告摘要與 `mongo_id`（供後續提領全文使用）。

### C. 結構化推薦提取 (`get_market_recommendations`)
*   **目標**: **專項工具**，從報告中直接提取推薦股票與產業標籤。
*   **Input**: `start_date`, `end_date`。
*   **解析邏輯**:
    1.  使用「推薦、潛力、看好」等關鍵字向量觸發檢索。
    2.  解析 Payload 中的 `stock_list` (格式：`[["tw", "代碼", "名稱"], ...]`)。
    3.  解析 Payload 中的 `industry_list` (格式：`["產業名", ...]`)。
    4.  **去重與格式化**: 統一格式為 `名稱(代碼)` 並過濾重複項。
*   **Output**: 返回結構化的股票清單與產業清單。

---

## 3. 時間同步機制 (Temporal Filtering)

為了避免時區偏移導致的資料漏失（例如 4/17 下午的資料被 4/17 00:00 的過濾器擋住），工具層遵循以下規範：
1.  **全日覆蓋**: Agent 產出 `end_date` 時必須補齊至當天深夜（`23:59:59Z`）。
2.  **UTC 統一**: Qdrant 內部的 `DatetimeIndex` 一律儲存為帶時區資訊的 ISO 字串，搜尋時進行精確範圍比對。
