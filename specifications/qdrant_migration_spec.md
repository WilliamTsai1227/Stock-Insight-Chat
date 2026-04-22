# Qdrant 向量資料庫規格與遷移計畫 (Qdrant Migration & Schema Specification)

本文件定義了股市生成式聊天應用中，Qdrant 向量資料庫的 Collection 結構、資料遷移邏輯 (ETL) 以及測試方案。

---

## 1. 核心技術規格 (System Infrastructure)

* **向量化模型 (Embedding)**: 使用 OpenAI `text-embedding-3-small` (1536 維)。
* **距離計算法 (Distance Metric)**: `Cosine Similarity` (餘弦相似度)。
* **分段策略 (Chunking)**: 
    * **News**: 長文使用 `RecursiveCharacterTextSplitter`，每段約 800 字。
    * **AI Analysis**: 按欄位角色拆分 (summary / key_news / stock_insight)。

---

## 2. Collection 結構設計 (Schema)

### A. Collection: `news` (股市新聞)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | MongoDB 的 `_id` 字串 (用於聚合) |
| `title` | String | Text | 新聞標題 |
| `publishAt` | String (ISO) | **Datetime** | 帶時區的 ISO 範式 (Asia/Taipei) |
| `source` | String | Keyword | 新聞來源 (如: anue) |
| `type` | String | Keyword | 新聞類型 (如: 台股新聞、國際新聞) |
| `keywords` | Array[String] | Keyword | 新聞關鍵字標籤 |
| `stock_codes` | Array[String] | Keyword | 涉及股票代號 (如: ["2330"]) |
| `stock_names` | Array[String] | Keyword | 涉及股票名稱 (如: ["台積電"]) |
| `content` | String | (無) | 該片段完整內容 |

### B. Collection: `ai_analysis` (AI 產業分析)
| 欄位 (Payload Key) | 資料型態 | 索引類型 | 來源說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | MongoDB 的 `_id` 字串 |
| `title` | String | Text | 分析報告標題 |
| `publishAt` | String (ISO) | **Datetime** | 帶時區的 ISO 範式 |
| `chunk_type` | String | Keyword | 片段角色 (summary / key_news / stock_insight) |
| `sentiment_label` | String | Keyword | 統一情緒標籤 (positive/negative/neutral) |
| `industry_list` | Array[String] | Keyword | 涉及產業標籤 |
| `stock_list` | Array | Keyword | 涉及股票資訊 |
| `source_news_titles` | Array[String] | (無) | 參考的新聞標題清單 |

---

## 3. 資料轉換邏輯 (ETL Pipeline v2)

### 第一步：確定性 UUID (Idempotency)
為了防止重複入庫，Point ID 採用 `uuid5(NAMESPACE_DNS, mongo_id + chunk_type + chunk_idx)`。

### 第二步：文本向量化拼接
- **News**: `"[標題] [內容片段]"`
- **AI Analysis**: 根據 `chunk_type` 加上對應前綴 (如 `[分析摘要]`)。

### 第三步：情緒轉化 (Sentiment Refinement)
遷移程序會掃描原始長文本，將其歸一化為 `positive`, `negative`, `neutral` 三種標籤，供 Qdrant `Keyword Match` 過濾使用。

---

## 4. 檢索策略：`search_groups`
為了避免同一篇文章的多個 Chunks 充斥搜尋結果，系統實施以下策略：
1.  **聚合**: 使用 `search_groups` 並指定 `group_by="mongo_id"`。
2.  **合併**: 設定 `group_size=2`，取每篇文章最相關的前 2 個 chunks 並在工具層進行內容合併。

---

## 5. 數據範例 (Qdrant Output)

### 5.1 News Point
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

### 5.2 AI Analysis Point
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
*版本：v2.0 (2026-04-23)*
*更新記錄：同步遷移腳本 v2 邏輯，細化 Payload 欄位與確定性 UUID 機制。*
