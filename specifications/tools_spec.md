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

`migrate_to_qdrant.py` 可依環境加上 `--limit`、`--collection`、`--dry-run` 等（見 §5）。

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

---

## 4. Qdrant Collection 結構與 ETL

（原 `qdrant_migration_spec.md`）

### 1. 核心技術規格 (System Infrastructure)

* **向量化模型 (Embedding)**: 使用 OpenAI `text-embedding-3-small` (1536 維)。
* **距離計算法 (Distance Metric)**: `Cosine Similarity` (餘弦相似度)。
* **分段策略 (Chunking)**: 
    * **News**: 長文使用 `RecursiveCharacterTextSplitter`，每段約 800 字。
    * **AI Analysis**: 按欄位角色拆分 (summary / key_news / stock_insight)。

---

### 2. Collection 結構設計 (Schema)

> **索引欄位的權威來源是 [`app/backend/scripts/setup_qdrant.py`](../app/backend/scripts/setup_qdrant.py) 的 `COLLECTION_DEFINITIONS`。** 下表「索引類型」欄已與其對齊；標示 **(無)** 代表 payload 裡有這個欄位、但**沒有建 payload index**（仍可讀取與回傳，只是不能高效過濾）。
>
> 兩個 collection 的向量結構皆為 `vectors_config["dense"]`（1536 維 Cosine）+ `sparse_vectors_config["text"]`（BM25，`modifier=IDF`）。

#### A. Collection: `news` (股市新聞)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `publishAt` | String (ISO) | **Datetime** | 帶時區的 ISO 範式 (Asia/Taipei) |
| `source` | String | Keyword | 新聞來源 (如: anue, cnyes) |
| `category` | String | Keyword | 分類 (headline, tw_stock, etc.) |
| `type` | String | Keyword | 新聞類型 (台股新聞 / 國際新聞) |
| `stock_codes` | Array[String] | Keyword | 涉及股票代號 (如: ["2330"]) |
| `stock_names` | Array[String] | Keyword | 涉及股票名稱 (如: ["台積電"]) |
| `keywords` | Array[String] | Keyword | 新聞關鍵字標籤 |
| `collection_type` | String | Keyword | 固定為 `"news"` |
| `chunk_type` | String | Keyword | `"full"` / `"partial"` |
| `chunk_idx` | Integer | Integer | 片段序號 |
| `total_chunks` | Integer | Integer | 該篇總片段數 |
| `mongo_id` | String | **(無)** | MongoDB `_id` 字串；用於**結果分組**，非過濾條件，故未建索引 |
| `title` | String | **(無)** | 新聞標題 |
| `content` | String | **(無)** | 該片段完整內容 |

#### B. Collection: `ai_analysis` (AI 產業分析)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `publishAt` | String (ISO) | **Datetime** | 帶時區的 ISO 範式 |
| `sentiment_label` | String | Keyword | 統一情緒標籤 (positive / negative / neutral) |
| `industry_list` | Array[String] | Keyword | 涉及產業標籤 |
| `category` | String | Keyword | 分類 (headline, etc.) |
| `chunk_type` | String | Keyword | 片段角色 (summary / key_news / stock_insight) |
| `collection_type` | String | Keyword | 固定為 `"ai_analysis"` |
| `is_summary` | Boolean | **Bool** | 是否為彙總報告 |
| `analysis_batch` | Integer | Integer | 分析批次 |
| `chunk_idx` | Integer | Integer | 片段序號 |
| `mongo_id` | String | **Text** | MongoDB `_id` 字串；此 collection 有建 text index 供 `group_by` 聚合 |
| `title` | String | **(無)** | 分析報告標題 |
| `stock_list` | Array | **(無)** | 涉及股票資訊；工具層自行解析為 `名稱(代碼)` |
| `source_news_titles` | Array[String] | **(無)** | 參考的新聞標題清單 |

> 注意兩個 collection 的 `mongo_id` 處理不同：`ai_analysis` 建了 text index，`news` 沒有。若日後要對 `news.mongo_id` 做 filter，需先在 `setup_qdrant.py` 補上索引定義並重跑。

---

### 3. 資料轉換邏輯 (ETL Pipeline v2)

#### 第一步：確定性 UUID (Idempotency)
為了防止重複入庫，Point ID 採用 `uuid5(NAMESPACE_DNS, mongo_id + chunk_type + chunk_idx)`。

#### 第二步：文本向量化拼接
- **News**: `"[標題] [內容片段]"`
- **AI Analysis**: 根據 `chunk_type` 加上對應前綴 (如 `[分析摘要]`)。

#### 第三步：情緒轉化 (Sentiment Refinement)
遷移程序會掃描原始長文本，將其歸一化為 `positive`, `negative`, `neutral` 三種標籤，供 Qdrant `Keyword Match` 過濾使用。

---

### 4. 檢索策略：分組去重

為了避免同一篇文章的多個 Chunks 充斥搜尋結果，系統依 `mongo_id` 分組：

1.  **聚合**: 依 payload `mongo_id` 分組（`group_by="mongo_id"`）。
2.  **合併**: 每組保留分數最高的 2 個 chunks（`group_size=2`），並在工具層合併內容。

> 目前檢索為 **Hybrid + RRF**（dense + BM25 sparse 融合），分組去重等價於先前的 `search_groups` 效果。詳見 [`tools_spec.md`](./tools_spec.md) §1、§2。

---

### 5. 數據範例 (Qdrant Output)

#### 5.1 News Point
```json
{
  "id": "determined-uuid-v5",
  "payload": {
    "mongo_id": "69d363...",
    "publishAt": "2026-04-04T14:00:04+08:00",
    "title": "油價高企...",
    "stock_codes": ["2330"],
    "type": "台股新聞",
    "content": "[油價高企...]: ..."
  }
}
```

#### 5.2 AI Analysis Point
```json
{
  "id": "determined-uuid-v5",
  "payload": {
    "mongo_id": "69d368...",
    "chunk_type": "stock_insight",
    "sentiment_label": "negative",
    "industry_list": ["石油", "天然氣"]
  }
}
```

---
*版本：v2.1 (2026-08-16)*
*更新記錄：*
- *v2.1 — §2 索引類型與 `setup_qdrant.py` 的 `COLLECTION_DEFINITIONS` 逐欄對齊；補上先前漏列的 `collection_type` / `chunk_type` / `chunk_idx` / `total_chunks` / `category` / `is_summary` / `analysis_batch`，並標示未建索引的欄位。*
- *v2.0 (2026-04-23) — 同步遷移腳本 v2 邏輯，細化 Payload 欄位與確定性 UUID 機制。*

---

## 5. 資料遷移操作（setup_qdrant / migrate_to_qdrant）

（原 `readme_full_details.md`）

將 MongoDB 內文稿轉為 Qdrant 向量點並建立索引。**依目的不同**，分成兩種流程（請擇一參照）：

---

#### 情境一：清空並重建 Qdrant（Hybrid 結構對了，但資料庫要換空／改 schema）

適用：**第一次建庫**、**升級過 `setup_qdrant.py` 定義後需整庫對齊**、**確認要刪掉既有向量再重灌**。注意：`--reset` 會**刪除** `news` 與 `ai_analysis` 底下**所有 points**。

```bash
# 1) 刪除既有 collection，依腳本重建（互動確認 y）
python3 app/backend/scripts/setup_qdrant.py --reset

# 2) 確認 Mongo 文稿切分與程式無誤（不呼叫 Embedding API、不寫入 Qdrant）
python3 app/backend/scripts/migrate_to_qdrant.py --dry-run --limit 10

# 3) 正式寫入向量（數量可自行調整：100 為各 collection 至多處理的「最新 N 篇」，依 mongo sort）
python3 app/backend/scripts/migrate_to_qdrant.py --limit 100

# 4) 可選：跑過濾／聚合檢查
python3 app/backend/scripts/test_qdrant_filter.py
```

完成情境一後，Qdrant 即為**結構與資料皆由腳本當時版本決定的一套乾淨庫**。若之後只新增 Mongo 文檔、未改動切分程式，通常改走**情境二**即可補資料。

---

#### 情境二：日常維運（collection 已在、不重灌整庫）

**什麼時候執行 `setup_qdrant.py`（不加 `--reset`）？**

| 時機 | 說明 |
| :--- | :--- |
| **新環境／剛架起 Qdrant、尚無 collection** | 只做「建 bucket + payload 索引」，不刪資料。 |
| **從 repo 拉了新版 `setup_qdrant.py`、多了欄位要建索引** | 再跑一次可補 `create_payload_index`；collection 若已存在則跳過重建。 |
| **想確認 Hybrid 是否正常** | 可看腳本輸出的**結構驗證**區塊。 |

**不一定要每天跑**；Qdrant 若已存在且無 schema 異動可略過。

**什麼時候執行 `migrate_to_qdrant.py`？**

| 指令 | 時機 |
| :--- | :--- |
| `--dry-run --limit 10` | 改過切分、`chunk_news_document`／payload 映射後想**先看切長相**；或大額 migrate 前先抽查。Dry run **不embedding、不 upsert**。 |
| `--limit N`（或進階的 `--collection`） | Mongo 有新的新聞／分析要進向量庫時，**例行補資料**；同一 `(mongo_id, chunk_type, chunk_idx)` 的 point id **固定**，重跑等同 **upsert 覆寫**，可當增量或修正後重灌部分篇數。 |

**日常若不想清空重來**，較務實的順序為：

```bash
# （可選）僅在未建過 collection／要補新索引時
python3 app/backend/scripts/setup_qdrant.py

# （推薦在改過遷移邏輯後）先看切分
python3 app/backend/scripts/migrate_to_qdrant.py --dry-run --limit 10

# 實際寫入或更新向量（調整 limit 視資料量而定）
python3 app/backend/scripts/migrate_to_qdrant.py --limit 100

# （可選）檢索自測
python3 app/backend/scripts/test_qdrant_filter.py
```

**Hybrid 檢索**之**首次建庫**，或自**舊版「僅單一向量」**升級時，請優先遵循 **情境一**。其他細節見 `specifications/tools_spec.md` §1.1 與下方「遷移進階用法」。執行前請先完成 **§3**（`pip install -r app/backend/requirements.txt`）與 Qdrant 服務可連線。

#### 遷移進階用法
```bash
# 只遷移特定 collection
python3 app/backend/scripts/migrate_to_qdrant.py --collection news --limit 500
python3 app/backend/scripts/migrate_to_qdrant.py --collection ai_analysis --limit 200

# 全量遷移
python3 app/backend/scripts/migrate_to_qdrant.py --limit 99999

# 重建 Collection (⚠️ 清除所有現有資料)
python3 app/backend/scripts/setup_qdrant.py --reset
```

#### `setup_qdrant.py`：重複執行、提示訊息與結構驗證

- **可多次執行**：若 Qdrant 裡 **`news` / `ai_analysis` 已存在**，腳本會**略過 `create_collection`**（不會再多建同名 collection）；仍會依序呼叫 `create_payload_index`。已存在的索引若建立失敗，會印出「可能已存在」類訊息並繼續。
- **剛做完 `--reset` 再跑一次一般初始化**：接下來再執行 `python3 app/backend/scripts/setup_qdrant.py` 時會顯示 **collection「已存在、略過建立」**，這代表上一輪已建好，**不是** Hybrid 設定失敗；若需確認實際 schema，請以腳本尾段的 **結構驗證** 區塊為準（或見 Dashboard）。
- **`--reset`**：互動確認 `y` 後會**刪除**上述兩個 collection（**內含所有 points**），再依腳本定義重建 **dense（`dense`，1536 維）+ sparse BM25（`text`）** 與 payload 索引。**刪檔後須重新執行 `migrate_to_qdrant.py` 才有向量資料。**
- **結構驗證輸出**（每次跑完每個 collection 的索引步驟後）：會列印 **`status`、`points_count`**、**`dense` 維度是否為 1536**、**是否存在 `text` sparse**、 **`payload_schema` 是否涵蓋腳本預期的索引欄位**（若有額外欄位會以 ℹ️ 標示）。若 `payload_schema` 為空（少數情境），腳本會說明可能原因，並仍以向量區塊為主判斷。
- **資料持久化**：若 Docker Compose **未使用 `down -v` 刪除 Qdrant volume**，即使重建容器，`news`／`ai_analysis` 仍可能在 volume 裡存活，這與腳本的「已存在」行為一致。

### 5. 驗證資料 (Qdrant Dashboard)
遷移完成後，你可以透過瀏覽器存取 Qdrant 內建的控制台來檢查資料：
*   **Dashboard 地址**: [http://localhost:6333/dashboard](http://localhost:6333/dashboard)
*   可在界面中直接查看 `news` 與 `ai_analysis` 的 Points、Payload 與向量數值。

---
