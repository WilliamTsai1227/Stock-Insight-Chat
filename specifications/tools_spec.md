# 搜尋工具規格說明 (Search Tools Specification)

本文件定義了 Agent 調用的核心搜尋工具技術規範，旨在確保向量檢索與結構化資料提取的一致性。

## 1. 核心元件
*   **向量庫 (Vector Store)**: Qdrant（**Qdrant 伺服器需 ≥ v1.10** 以支援 Query API / RRF 融合）
*   **文件庫 (Document Store)**: MongoDB (僅用於獲取全文)
*   **外部搜尋 (External Search)**: Tavily Search API（`tavily_global_search`，見 §2-D；不經 Qdrant/MongoDB，本節其餘規格不適用）
*   **Dense Embeddings**: OpenAI `text-embedding-3-small` (1536 dims)，向量名稱 **`dense`**
*   **Sparse (BM25)**: FastEmbed `Qdrant/bm25`，索引與查詢分別使用 `passage_embed` / `query_embed`；Qdrant 端 sparse 向量名稱 **`text`**，`SparseVectorParams.modifier = IDF`（與 FastEmbed 慣例一致）
*   **融合策略**: **RRF (Reciprocal Rank Fusion)**：對同一 filter 下 dense 與 sparse 兩路 prefetch 結果做排名融合（非加權分數相加）
*   **分數門檻**: `score_threshold` 預設為 **`None`**（不裁切）。RRF 產生的分數尺度與單獨 cosine 相似度不同；若需裁切請自行實測後傳入閾值
*   **連線方式**: 全面採用 **Async (非同步)** 客戶端以支援高併發與並行檢索

### 1.1 Collection 與資料要求
* 新 collection 須由 `setup_qdrant.py` 建立：`vectors_config["dense"]` + `sparse_vectors_config["text"]`（IDF）。
* 既有僅含「單一未命名向量」的 collection **不相容**；須重建 collection 並**重新遷移**（寫入 dense + sparse）。

**建議作業順序**（於**專案根目錄**執行；`--reset` 會刪除 Qdrant 內既有 collection 資料）：

```bash
pip install -r app/backend/requirements.txt
python3 app/backend/scripts/setup_qdrant.py --reset
python3 app/backend/scripts/migrate_to_qdrant.py
```

`migrate_to_qdrant.py` 可依環境加上 `--limit`、`--collection`、`--dry-run` 等（與 README「資料 migration」一節相同）。

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
    1.  以 **Hybrid + RRF**：`query` → BM25 sparse；`query_embedding` → dense；兩路 `Prefetch` 共用同一 `Filter`。
    2.  **過濾**：`must` 時間／類型等；`stock_code` / `keyword` / `stock_name` 為 **`should`（OR）**。
    3.  **聚合**：對 RRF 結果依 payload **`mongo_id`** 分組（等價於先前 `search_groups` 效果），每組保留分數最高的 **2** 個 chunk 並合併正文。
*   **Output**: 返回合併後的內容片段與溯源資訊（`score` 為 RRF 融合分數）。

### B. AI 深度分析搜尋 (`search_market_ai_analysis`)
*   **目標**: 檢索專業報告與法人觀點摘要。
*   **Input**: 
    *   `query`, `start_date`, `end_date` (同上)。
    *   `sentiment` (str, Optional): "positive" | "negative" | "neutral"。
    *   `industry` (str, Optional): 產業標籤。
*   **檢索邏輯**: 與新聞相同之 **Hybrid + RRF + `mongo_id` 分組**（每組 2 chunks），集合為 **`ai_analysis`**。底層函式 `search_ai_analysis` 另可依 **`chunk_type` / `sentiment_label` / `industry_list`** 做 **must** 過濾；目前 LangChain 工具包裝暴露 **`sentiment` / `industry` / 日期**。
*   **Output**: 返回報告摘要與詳細元數據。

### C. 結構化推薦提取 (`get_market_recommendations`)
*   **目標**: **專項工具**，從報告中直接提取推薦股票與產業標籤。
*   **Input**: `start_date`, `end_date`。
*   **解析邏輯**:
    1.  Agent 端以固定中文句產生 dense embedding；工具內以 **相同詞組**（「推薦股票、強勢產業、潛力標的、看好板塊」）產生 **BM25 查詢向量**，與 dense 語意對齊。
    2.  **Hybrid + RRF** 檢索，`chunk_type="stock_insight"` 等條件以 **must** filter 保留。
    3.  解析 Payload 中的 `stock_list` (自動格式化為 `名稱(代碼)`)。
*   **Output**: 返回結構化的 `stocks` (list) 與 `industries` (list) 以及資料來源清單。

### D. 即時網路搜尋 (`tavily_global_search`)

*   **目標**: 向量庫覆蓋不到的問題 —— 最新時事、一般知識、產品說明、政策法規、非股市專業問題。
*   **資料來源**: **Tavily Search API（外部服務）**，不經 Qdrant 或 MongoDB。
*   **Input**: `query` (str) —— 搜尋關鍵字，中英文皆可。**僅此一個參數**，無日期或標的過濾。
*   **執行邏輯**:
    1.  呼叫 [`tools/global_search.py`](../app/backend/tools/global_search.py) 的 `tavily_search()`。
    2.  依 `TAVILY_INCLUDE_ANSWER` 決定是否附帶 Tavily 的即時摘要。
    3.  每則結果的正文同樣套用 `_clip_for_llm(..., MAX_TOOL_ITEM_CHARS)` 截斷。
    4.  失敗時**不拋例外**，回傳 `網路搜尋失敗：<原因>` 字串交給 Router 判斷。
*   **Output**: 標題、URL、發布日期與內容片段的文字區塊。

**優先順序**：Router 的 system prompt 明確指示 —— 若問題能由 `search_stock_news` 或 `search_market_ai_analysis` 回答，**優先用那兩個**；Tavily 是覆蓋不到時的補位。

**相關環境變數**（完整說明見 [`env.md`](./env.md) §八）：

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `TAVILY_API_KEY` | `""` | **未設定時本工具不可用** |
| `TAVILY_MAX_RESULTS` | `5` | 每次回傳筆數 |
| `TAVILY_SEARCH_DEPTH` | `basic` | `basic` / `advanced`（後者深但慢且貴） |
| `TAVILY_INCLUDE_ANSWER` | `1` | 是否附帶即時摘要 |
| `TAVILY_TIMEOUT_SEC` | `15` | 單次逾時秒數 |
| `TAVILY_MAX_CALLS_PER_TURN` | `2` | 單輪呼叫次數上限，防止反覆搜尋拖慢回應 |

---

## 3. 並行執行與快取機制

1.  **並行檢索**: Agent 在 `call_tools` 節點會同時啟動多個工具呼叫，縮短等待時間。
2.  **Embedding 快取**: 相同對話內的重複查詢詞會進行快取，避免重複請求 OpenAI API（**BM25 查詢向量**目前於各工具呼叫內即算，採執行緒池執行 FastEmbed 以避免阻塞事件迴圈）。
3.  **時區對齊**: 全系統統一使用 `Asia/Taipei` (UTC+8) 進行時間計算與 Qdrant 過濾。
