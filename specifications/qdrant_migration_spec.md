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

> **索引欄位的權威來源是 [`app/backend/scripts/setup_qdrant.py`](../app/backend/scripts/setup_qdrant.py) 的 `COLLECTION_DEFINITIONS`。** 下表「索引類型」欄已與其對齊；標示 **(無)** 代表 payload 裡有這個欄位、但**沒有建 payload index**（仍可讀取與回傳，只是不能高效過濾）。
>
> 兩個 collection 的向量結構皆為 `vectors_config["dense"]`（1536 維 Cosine）+ `sparse_vectors_config["text"]`（BM25，`modifier=IDF`）。

### A. Collection: `news` (股市新聞)
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

### B. Collection: `ai_analysis` (AI 產業分析)
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

## 3. 資料轉換邏輯 (ETL Pipeline v2)

### 第一步：確定性 UUID (Idempotency)
為了防止重複入庫，Point ID 採用 `uuid5(NAMESPACE_DNS, mongo_id + chunk_type + chunk_idx)`。

### 第二步：文本向量化拼接
- **News**: `"[標題] [內容片段]"`
- **AI Analysis**: 根據 `chunk_type` 加上對應前綴 (如 `[分析摘要]`)。

### 第三步：情緒轉化 (Sentiment Refinement)
遷移程序會掃描原始長文本，將其歸一化為 `positive`, `negative`, `neutral` 三種標籤，供 Qdrant `Keyword Match` 過濾使用。

---

## 4. 檢索策略：分組去重

為了避免同一篇文章的多個 Chunks 充斥搜尋結果，系統依 `mongo_id` 分組：

1.  **聚合**: 依 payload `mongo_id` 分組（`group_by="mongo_id"`）。
2.  **合併**: 每組保留分數最高的 2 個 chunks（`group_size=2`），並在工具層合併內容。

> 目前檢索為 **Hybrid + RRF**（dense + BM25 sparse 融合），分組去重等價於先前的 `search_groups` 效果。詳見 [`tools_spec.md`](./tools_spec.md) §1、§2。

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
*版本：v2.1 (2026-08-16)*
*更新記錄：*
- *v2.1 — §2 索引類型與 `setup_qdrant.py` 的 `COLLECTION_DEFINITIONS` 逐欄對齊；補上先前漏列的 `collection_type` / `chunk_type` / `chunk_idx` / `total_chunks` / `category` / `is_summary` / `analysis_batch`，並標示未建索引的欄位。*
- *v2.0 (2026-04-23) — 同步遷移腳本 v2 邏輯，細化 Payload 欄位與確定性 UUID 機制。*
