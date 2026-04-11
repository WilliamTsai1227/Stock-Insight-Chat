# Qdrant 向量資料庫規格與遷移計畫 (Qdrant Migration & Schema Specification)

本文件定義了股市生成式聊天應用中，Qdrant 向量資料庫的 Collection 結構、資料遷移邏輯 (ETL) 以及測試方案。

---

## 1. 核心技術規格 (System Infrastructure)

* **向量化模型 (Embedding)**: 使用 OpenAI `text-embedding-3-small` (1536 維)。
* **距離計算法 (Distance Metric)**: `Cosine Similarity` (餘弦相似度)。
* **系統時區 (Timezone)**: `Asia/Taipei (UTC+8)`。
* **分段策略 (Chunking)**: 採用 `[Title]: [Content Chunk]` 拼接法，確保檢索背景完整。

---

## 2. Collection 結構設計 (Schema)

### A. Collection: `news` (股市新聞)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | MongoDB 的 `_id` 字串 (用於聚合) |
| `title` | String | Text | 新聞標題 |
| `publishAt` | String (ISO) | **Datetime** | 將 Unix Timestamp 轉換為帶時區的 ISO 範式 |
| `chunk_idx` | Integer | (無) | 該新聞的片段序號 (0, 1, 2...) |
| `source` | String | Keyword | 新聞來源 (如: anue) |
| `category` | String | Keyword | 新聞分類 (如: headline) |
| `stock_list` | Array[String] | Keyword | 涉及股票代號 (由 MongoDB 原始資料映射) |
| `content` | String | (無) | 該片段完整內容 (含 Title 前綴) |

### B. Collection: `ai_analysis` (AI 產業分析)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | MongoDB 的 `_id` 字串 |
| `title` | String | Text | 分析報告標題 |
| `publishAt` | String (ISO) | **Datetime** | 帶時區的 ISO 範式 |
| `sentiment` | String | Keyword | 情緒標籤 (positive/negative/neutral) |
| `industry_list` | Array[String] | Keyword | 涉及產業標籤 (如: 石油、軍工) |
| `stock_list` | Array[Array[String]] | Keyword | 涉及股票代號 (如: [["us", "XOM", "埃克森美孚"]]) |

---

## 3. 資料轉換邏輯 (ETL Pipeline)

### 第一步：時間轉換規則 (Logic)
MongoDB 原始數據中的 `publishAt` 為 Unix Timestamp (例如 `1775455204`)，需轉換為符合 ISO 8601 格式的字串，並顯式包含 `+08:00` 偏移量。
- **轉換範式**: `1775455204` -> `2026-04-04T14:00:04+08:00`

### 第二步：文本向量化拼接 (Context-Aware Embedding)
為了提升搜尋相關性，每個 Chunk 均須採用以下拼接規則進行 Embedding 生成：
- **News**: `"[新聞標題]: [內容段落(約1000 tokens)]"`
- **AI Analysis**: `"[報告標題]: [摘要內容 (summary)]"`

### 第三步：情緒標籤轉化規則 (Sentiment Refinement)
由於 MongoDB 原始數據中的 `sentiment` 欄位是長短不一的文字描述，遷移程序需執行以下邏輯轉化為精確標籤：
- **Negative (負面)**: 原始文本包含 "負面", "惡化", "風險", "下行" 或經 LLM 判定為負面。
- **Positive (正面)**: 原始文本包含 "正面", "成長", "亮眼", "樂觀" 或經 LLM 判定為正面。
- **Neutral (中性)**: 若無法判定則預設為中性。
- **最終儲存**: Qdrant Payload 的 `sentiment` 欄位僅儲存 `positive`, `negative`, `neutral` 其中之一，以便進行 `Keyword Match` 過濾。

### 第四步：聚合與檢索 (Group-By Strategy)
- **避免重複**: 執行搜尋時，應調用 `search_groups` 並指定 `group_by="mongo_id"`。
- **效果**: 當一篇長文章有多個 Chunk 被匹配到時，搜尋結果僅會保留分數最高的一個 Chunk，避免相同文章充斥搜尋列表。

---

## 4. 具體執行步驟 (Implementation Blueprint)

### Phase 1: 環境配置
1.  確保 `requirements.txt` 包含 `pytz` 與 `openai`。
2.  在 `.env` 中設定 `OPENAI_API_KEY` 以便調用 Embedding 介面。

### Phase 2: 初始化 Collection
執行 `app/backend/scripts/setup_qdrant.py`：
- 建立具備 1536 維度的 Collection。
- 手動針對 `publishAt` 欄位建立 **Datetime Index** 以支援時間區間過濾。
- 建立對應的 Keyword Index。

### Phase 3: 正式資料遷移
執行 `app/backend/scripts/migrate_to_qdrant.py`：
- 從 MongoDB `news` 與 `ai_analysis` 集合批次讀取數據。
- 執行分段、時區轉換與向量化。
- 將封裝好的 Point (Vector + Payload) `upsert` 至 Qdrant。

### Phase 4: 壓力與功能測試
執行 `app/backend/scripts/test_qdrant_filter.py`：
- 驗證 `DatetimeRange` 過濾是否正確（例如查找上週一至今的資料）。
- 驗證複合過濾條件（例如搜尋：產業為 "能源" 且情緒為 "負面" 的資料）。

---

---

## 5. 數據源規格 (Source MongoDB Schema)

### 5.1 原始新聞 (news collection)
| 欄位 | 說明 | 範例 |
| :--- | :--- | :--- |
| `_id` | MongoDB 唯一識別碼 | `ObjectId("...")` |
| `title` | 新聞標題 | `"油價高企衝擊物價..."` |
| `content` | 新聞全文內容 | `"中東局勢緊張推高..."` |
| `publishAt` | Unix Timestamp | `1775455204` |
| `source` | 來源名稱 | `"anue"` |
| `category` | 文章分類 | `"headline"` |

### 5.2 原始分析報告 (ai_analysis collection)
| 欄位 | 說明 | 範例 |
| :--- | :--- | :--- |
| `_id` | MongoDB 唯一識別碼 | `ObjectId("...")` |
| `article_title` | 報告總標題 | `"中東地緣政治危機..."` |
| `summary` | 內容摘要 | `"中東地緣政治危機嚴重影響..."` |
| `important_news` | 重點條列新聞 | `"1. 中東地緣政治危機..."` |
| `sentiment` | 長串情緒分析文本 | `"整體情勢呈現負面走向..."` |
| `industry_list` | 產業列表 | `["石油", "天然氣"]` |
| `stock_list` | 相關股票代號 | `[["us", "XOM", "埃克森美孚"]]` |

---

## 6. 資料一致性與執行規格 (Operational Specs)

### 6.1 防重複機制 (Idempotency)
為了確保多次執行遷移腳本不會產生重複數據，系統採用 **確定性 UUID (Deterministic UUID)**：
- **演算法**: `uuid5(NAMESPACE_DNS, mongo_id + chunk_idx)`。
- **效果**: 只要 MongoDB ID 與片段序號不變，生成的 Qdrant Point ID 永遠相同。
- **衝突處理**: Qdrant 偵測到 ID 重複時會自動執行 **Overwrite (覆蓋)**，確保資料最新且唯一。

### 6.2 命令行參數 (CLI Args)
腳本 `migrate_to_qdrant.py` 支援以下參數以利分段測試：
- `--limit <int>`: 設定每個集合讀取的文檔上限。預設為 100 筆，防止永無止盡執行。
- `--batch_size <int>`: 設定單次與資料庫交互的批次量。

---

## 7. 數據範例對比 (Raw vs Qdrant)

### 7.1 MongoDB 原始範例 (News)
```json
{
  "_id": { "$oid": "69d3638d94d3edb8ab6894b5" },
  "news_id": 6407933,
  "title": "油價高企衝擊物價！華爾街投行紛上調韓通膨預期 最高恐飆破3%",
  "publishAt": 1775455204,
  "url": "https://news.cnyes.com/news/id/6407933",
  "source": "anue",
  "category": "headline",
  "content": "中東局勢緊張推高國際油價，引發海外主要投行...分析指出，國際油價上漲對物價的傳導通常有..."
}
```

### 7.2 MongoDB 原始範例 (AI Analysis)
```json
{
  "_id": { "$oid": "69d36899f938a893cabee588" },
  "article_title": "中東地緣政治危機引發全球供應鏈中斷與通膨壓力",
  "publishAt": 1775462553,
  "summary": "中東地緣政治危機嚴重影響全球液化天然氣 (LNG)...",
  "important_news": "1. 中東地緣政治危機造成全球 LNG 供應鏈嚴重中斷...",
  "sentiment": "整體情勢呈現負面走向。中東地緣政治局勢持續惡化...",
  "industry_list": ["石油", "天然氣", "軍工", "食品", "生活必需品"],
  "stock_list": [["us", "XOM", "埃克森美孚"], ["us", "CVX", "雪佛龍"]]
}
```

### 7.3 Qdrant 最終儲存範例 - News (Output)
這是在執行 `news` 集合遷移後的典型 Point 結構：
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000", (由 uuid5 產生)
  "vector": [0.12, -0.05, ...], (1536 維)
  "payload": {
    "mongo_id": "69d3638d94d3edb8ab6894b5",
    "publishAt": "2026-04-04T14:00:04+08:00",
    "title": "油價高企衝擊物價！華爾街投行...",
    "content": "[油價高企衝擊物價！...]: 中東局勢緊張推高國際油價，引發...",
    "chunk_idx": 0,
    "source": "anue",
    "category": "headline",
    "url": "https://news.cnyes.com/news/id/6407933"
  }
}
```

### 7.4 Qdrant 最終儲存範例 - AI Analysis (Output)
這是在執行 `ai_analysis` 集合遷移後的典型 Point 結構：
```json
{
  "id": "bba998c0-8123-4567-89ab-cdef01234567", (由 uuid5 產生)
  "vector": [0.08, 0.22, ...], (1536 維)
  "payload": {
    "mongo_id": "69d36899f938a893cabee588",
    "publishAt": "2026-04-04T16:02:33+08:00",
    "title": "中東地緣政治危機引發全球供應鏈中斷與通膨壓力",
    "content": "[中東地緣政治危機引發...]: 中東地緣政治危機嚴重影響全球液化天然氣...",
    "chunk_idx": 0,
    "sentiment": "negative",
    "industry_list": ["石油", "天然氣", "軍工", "食品", "生活必需品"],
    "stock_list": [["us", "XOM", "埃克森美孚"], ["us", "CVX", "雪佛龍"]]
  }
}
```

---
*版本：v1.3 (2026-04-06)*
*更新記錄：細化展示 news 與 ai_analysis 兩大集合的獨立 Qdrant 輸出結構。*
