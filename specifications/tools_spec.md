# 搜尋工具規格說明 (Search Tools Specification)

本文件定義了 Agent 調用的核心搜尋工具技術規範，旨在確保向量檢索與結構化資料提取的一致性。

## 1. 核心元件
*   **向量庫 (Vector Store)**: Qdrant
*   **文件庫 (Document Store)**: MongoDB (僅用於獲取全文)
*   **Embeddings**: OpenAI `text-embedding-3-small` (1536 dims)
*   **最低相似度門檻**: `score_threshold = 0.3` (過濾低品質結果)
*   **連線方式**: 全面採用 **Async (非同步)** 客戶端以支援高併發與並行檢索。

---

## 2. 工具清單 (Tool Registry)

### A. 市場新聞搜尋 (`search_stock_news`)
*   **目標**: 檢索最新的市場新聞片段。
*   **Input**:
    *   `query` (str): 搜尋關鍵字。
    *   `start_date` / `end_date` (ISO Date, Optional): 時間區間。
    *   `stock_code` (str, Optional): 股票代碼 (如 "2330")。
    *   `news_type` (str, Optional): 新聞類型 (如 "台股新聞", "國際新聞")。
    *   `keyword` (str, Optional): 新聞標籤關鍵字。
    *   `stock_name` (str, Optional): 股票名稱。
*   **檢索邏輯**: 
    1.  向量搜尋 + 複合過濾 (`must` 時間/類型, `should` 代碼/關鍵字/名稱)。
    2.  採用 **`search_groups`** 按 `mongo_id` 聚合。
    3.  取 `group_size=2` 並合併同一篇新聞的前 2 個最相關 chunks。
*   **Output**: 返回合併後的內容片段與溯源資訊。

### B. AI 深度分析搜尋 (`search_market_ai_analysis`)
*   **目標**: 檢索專業報告與法人觀點摘要。
*   **Input**: 
    *   `query`, `start_date`, `end_date` (同上)。
    *   `sentiment` (str, Optional): "positive" | "negative" | "neutral"。
    *   `industry` (str, Optional): 產業標籤。
*   **檢索邏輯**: 同新聞搜尋，但鎖定 `ai_analysis` 集合，支援情緒與產業過濾。
*   **Output**: 返回報告摘要與詳細元數據。

### C. 結構化推薦提取 (`get_market_recommendations`)
*   **目標**: **專項工具**，從報告中直接提取推薦股票與產業標籤。
*   **Input**: `start_date`, `end_date`。
*   **解析邏輯**:
    1.  固定使用「推薦股票、強勢產業」等關鍵字向量觸發。
    2.  **精準命中**: 僅搜尋 `chunk_type="stock_insight"` 的向量。
    3.  解析 Payload 中的 `stock_list` (自動格式化為 `名稱(代碼)`)。
*   **Output**: 返回結構化的 `stocks` (list) 與 `industries` (list) 以及資料來源清單。

---

## 3. 並行執行與快取機制

1.  **並行檢索**: Agent 在 `call_tools` 節點會同時啟動多個工具呼叫，縮短等待時間。
2.  **Embedding 快取**: 相同對話內的重複查詢詞會進行快取，避免重複請求 OpenAI API。
3.  **時區對齊**: 全系統統一使用 `Asia/Taipei` (UTC+8) 進行時間計算與 Qdrant 過濾。
