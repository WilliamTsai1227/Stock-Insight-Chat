# 📈 Stock Insight Chat

[![Dynamic Design](https://img.shields.io/badge/Design-Premium-FF69B4?style=for-the-badge)](https://github.com/WilliamTsai1227/Stock-Insight-Chat)
[![Technology Stack](https://img.shields.io/badge/Stack-AI--Native-007ACC?style=for-the-badge)](https://github.com/WilliamTsai1227/Stock-Insight-Chat)

> **股市洞察生成式聊天系統** —— 結合即時新聞、AI 產業分析與企業財報的智慧對話助手。

## 🌟 系統概覽

Stock Insight Chat 是一套專為投資者設計的 AI 智能對話系統。它不僅能理解使用者的提問，更能主動調用專業工具，從海量的新聞數據與 AI 分析報告中檢索關鍵片段（RAG），並結合企業歷史財報，提供具備深度見解的投資分析。

---

## 🚀 快速開始 (Quick Start)

### 1. 啟動基礎設施
透過 Docker Compose 啟動 Qdrant 向量資料庫與 PostgreSQL：
```bash
cd deploy
docker-compose up -d
```

### 2. 環境設定
在專案根目錄建立或編輯 `.env` 檔案，確保包含以下必要的配置：
```bash
# AI Provider
OPENAI_API_KEY=sk-your-key-here

# MongoDB (資料來源)
MONGO_URI=mongodb://localhost:27017
MONGO_DB=stock_insight

# Qdrant (向量目標)
QDRANT_HOST=localhost
QDRANT_PORT=6333
```

### 3. Python 環境安裝
建議使用 **Python 3.11** 版本（Python 3.13 仍有套件相容性問題）：
```bash
# 建立虛擬環境
python3.11 -m venv venv
source venv/bin/activate

# 安裝依賴
pip install -r app/backend/requirements.txt
```

### 4. 執行資料遷移 (Migration)
將資料從 MongoDB 遷移至 Qdrant：

```bash
# Step A: 初始化 Collection 與索引
python app/backend/scripts/setup_qdrant.py

# Step B: 執行遷移 (建議先 limit 10 進行測試)
python app/backend/scripts/migrate_to_qdrant.py --limit 10
```

### 5. 驗證資料 (Qdrant Dashboard)
遷移完成後，你可以透過瀏覽器存取 Qdrant 內建的控制台來檢查資料：
*   **Dashboard 地址**: [http://localhost:6333/dashboard](http://localhost:6333/dashboard)
*   可在界面中直接查看 `news` 與 `ai_analysis` 的 Points、Payload 與向量數值。

---

## 🧪 測試工具 (Testing)
本專案提供後端工具函式的自動化測試，確保檢索邏輯正常：
```bash
# 執行所有工具測試
pytest test/backend/tools/ -s
```
*   **news**: 測試新聞檢索與全文抓取流程。
*   **ai_analysis**: 測試 AI 報告搜尋與摘要抓取流程。

---

## 🧠 向量儲存結構 (Qdrant Schema)

系統採用 **Qdrant** 作為核心向量資料庫，支援高效的語義搜尋與動態過濾。以下是目前規劃的 Collection 結構設計：

### 1. 通用規格
*   **向量模型 (Embedding)**: OpenAI `text-embedding-3-small` (1536 維)
*   **距離計算法 (Distance Metric)**: `Cosine Similarity`
*   **時區規範**: `Asia/Taipei (UTC+8)`

### 2. Collection: `news` (股市新聞)
收錄每日爬蟲抓取的最新股市動態，並進行精細分段。

| 欄位 (Payload Key) | 資料型態 | 索引類型 | 說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | 對應 MongoDB 原始新聞 ID |
| `title` | String | Text | 新聞標題 |
| `publishAt` | String (ISO) | **Datetime** | 發布時間 (支援時間區間過濾) |
| `source` | String | Keyword | 來源 (如: anue) |
| `category` | String | Keyword | 文章分類 (如: headline) |
| `stock_list` | Array[String] | Keyword | 提及之股票代碼 |
| `content` | String | - | 文字片段內容 (含標題前綴) |
| `url` | String | - | 原始新聞連結 |

### 3. Collection: `ai_analysis` (AI 產業分析)
收錄由 LLM 產出的深度統整與產業趨勢分析。

| 欄位 (Payload Key) | 資料型態 | 索引類型 | 說明 |
| :--- | :--- | :--- | :--- |
| `mongo_id` | String | Keyword | 對應 MongoDB 原始分析報告 ID |
| `title` | String | Text | 報告標題 |
| `publishAt` | String (ISO) | **Datetime** | 生成時間 |
| `sentiment` | String | Keyword | 情緒標籤 (`positive`, `negative`, `neutral`) |
| `industry_list` | Array[String] | Keyword | 涉及產業 (如: 半導體、能源) |
| `stock_list` | Array[String] | Keyword | 推薦或提及之股票代碼 |
| `content` | String | - | 分析摘要或重要新聞片段 |

---

## 🛠️ 技術架構 (System Stack)

*   **後端系統**: Python FastAPI (非同步架構)
*   **向量檢索**: Qdrant (Rust-based Vector Database)
*   **大數據儲存**: MongoDB (用於原始文章全文儲存)
*   **關係型數據**: PostgreSQL (用於會員系統、專案管理與對話歷史)
*   **人工智慧**: OpenAI GPT-4o / GPT-4 Turbo & Embedding API
*   **工作流程**: LangGraph (Agent 邏輯編排與工具調用)

---

## 🚀 資料遷移與維護

系統內建完善的數據 ETL 工具，可確保 Qdrant 與 MongoDB 資料同步：

*   `setup_qdrant.py`: 自動初始化 Collection 與建立高性能索引（含 Datetime 索引）。
*   `migrate_to_qdrant.py`: 具備**防重複機制**的遷移腳本。
    *   利用 `uuid5` 產生確定性 ID，確保資料變動時僅執行 `upsert`。
    *   支援語義化情緒標籤轉換與時間時區校正。

---

## 🧩 資料切分與儲存策略 (Chunking & Storage Strategy)

為了確保 RAG (檢索增強生成) 的品質與系統的強健性，本專案採用以下策略：

### 1. 文本切分 (Chunking Strategy)
*   **固定長度切分**: 每 1000 個字元切分為一個片段 (Chunk)。
*   **上下文注入 (Context Injection)**: 每個片段開頭均強制加上 `[標題]:` 前綴方案。這能確保向量检索到的任何片段都具備明確的主題背景，大幅提升 LLM 回答的準確度。
*   **組合內容**: AI 分析報告會將「摘要」與「重要新聞」合併後再行切分，確保關鍵資訊不遺漏。

### 2. 資料一致性與防重複 (Idempotency)
*   **確定性 ID 生成**: 系統使用 `uuid5` 演算法，根據 `mongo_id` 與 `chunk_idx` 產生固定 UUID。
*   **覆蓋更新 (Upsert)**: Qdrant 偵測到相同 ID 時會自動執行更新，這讓遷移腳本可以多次重複執行而不會造成資料庫重複寫入。

### 3. 資料精煉與同步 (Data Refinement & Sync)
*   **最新優先 (Newest First)**: 遷移腳本預設採用 `.sort("_id", -1)` 排序，確保優先搬移最新的新聞與分析資料。
*   **情緒標準化**: 使用關鍵字比對技術 (Heuristic logic) 將原始情緒文本歸類為 `positive`, `negative`, 或 `neutral`。
*   **時間格式統一**: 將所有時間轉換為 `Asia/Taipei` 時區的 ISO 8601 格式，以支援精確的時間區間檢索。
*   **對應關係**:
    *   MongoDB `news` -> Qdrant `news`
    *   MongoDB `AI_news_analysis` -> Qdrant `ai_analysis`

---

## 🔍 RAG 檢索邏輯 (Retrieval Architecture)

系統採用兩階段檢索架構，平衡搜尋速度與資料完整性：

### 1. 第一階段：向量檢索 (Qdrant)
*   **目標**: 快速定位最相關的資料片段。
*   **搜尋方式**: 透過 `text-embeddings-3-small` 產生的 `query_vector` 進行 **Cosmic Similarity (餘弦相似度)** 搜尋。
*   **廣義混合搜尋 (Hybrid)**: 雖然目前未配置 Sparse Vector，但系統結合了 **向量搜尋 + 標籤過濾 (Payload Filtering)**。可同時針對 `stock_list`、`publishAt` 等 metadata 進行精確篩選。
*   **輸出**: 回傳 Top-K 個 **Chunks (片段)**，每個片段皆附帶 `[標題]` 前綴以增強語義脈絡。

### 2. 第二階段：全文提領 (MongoDB)
*   **目標**: 提供深度分析所需的完整上下文。
*   **觸發場景**:
    *   **場景 A (節省 Token)**: AI 僅需回答事實性問題（如「營收是多少？」），此時僅使用 Qdrant 片段。
    *   **場景 B (深度分析)**: 當使用者要求「總結長篇報告」或「對比全文細節」時，Agent 會根據 `mongo_id` 回到 **MongoDB** 提領完整原始文件。
*   **邏輯**: 確保 AI 不會因片段切分而遺失長文本中的關鍵連結。

---

## 📊 專案進度
- [x] 資料庫 Schema 設計 (PostgreSQL+MongoDB)
- [x] Qdrant 向量結構規劃與初始化
- [x] 資料遷移腳本 (含排序、防重覆機制)
- [x] 資料切分與檢索策略設計
- [ ] LangGraph Agent 核心邏輯實現
- [ ] 前端對話介面開發

---
*Last Update: 2026-04-17*
