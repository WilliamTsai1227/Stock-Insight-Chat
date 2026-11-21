---
name: notion-spec
description: 把最近完成的開發內容同步到 Notion「系統分析」底下的 API 規格／DB 規格／系統設計
argument-hint: [這次要記錄的功能或補充說明]
disable-model-invocation: true
---

把這次的開發內容同步到 Notion「⛽ 系統分析」。

使用者補充說明：$ARGUMENTS

## 範圍判斷
依使用者補充說明判斷；沒有說明時，用 `git log --oneline -15` 和 `git diff main...HEAD --stat` 判斷這個分支做了什麼。
以 repo 內容為準（`specifications/*.md`、實際程式碼、`init_db.sql` 等），不要自己編規格。一次改動可能要更新多處。

## Notion 結構（只能寫在這些位置）
系統分析：https://app.notion.com/p/31d2dbee5080807dabedf4a103b29084

「系統分析」頁本身只放四個 inline database，每個 database 裡一列就是一個入口頁：

```txt
⛽ 系統分析
├── 架構（inline DB）  → 架構圖
├── API（inline DB）   → API 規格 → 資料庫「聊天室相關端點」
├── DB（inline DB）    → 💾 DB 規格 → MongoDB 資料庫規格 / PostgreSQL資料庫規格
└── 系統設計（inline DB）→ ⚙️ 系統設計 → 各設計主題子頁面
```

外層四個 inline DB 只有 `Name` 欄位，**不要在外層新增列**；要寫的內容都在下面列出的入口頁或資料庫裡。

### 1. API 規格 → 資料庫「聊天室相關端點」
- 路徑：系統分析 › API › API 規格（https://app.notion.com/p/31d2dbee508080b9957efa59e756ce2e）
- data source：`collection://31d2dbee-5080-8035-8d28-000b75faff70`（雖然叫「聊天室相關端點」，所有 API 都放這裡）
- **一個 endpoint 一列**。屬性：
  - `API`（標題）：路徑，例如 `/api/chat/messages`；path 參數寫 `{item_id}`
  - `HTTP method`：GET / POST / PUT / PATCH / DELETE；同一路徑多個 method 共用一個 handler 時用 `GET/POST/DELETE`（目前只有 `/explore/{path}`）
  - `對應DB`：Mongodb / PostgreSQL；不碰資料庫的留空
  - `DB 名稱`：MongoDB 填 `stock_insight`，PostgreSQL 填 `Insight`
  - `Collection/table名稱`：**單選**，用到多張表時填主要那張，其餘寫在內文；沒有合適選項可新增。
    現有選項：PostgreSQL 的 `chats`、`messages`、`projects`、`files`、`users`、`refresh_tokens`、`user_usage_quotas`、`token_usage_logs`、`user_feedback`、`note_pages`、`notes`、`portfolio_items`、`insight_schedules`、`insight_jobs`、`insight_results`；MongoDB 的 `news`、`news_vector`、`AI_news_analysis`
  - `用途`：一句話說明
- 內文沿用既有格式（參考 `/api/chat/messages` 那列）：
  `- **Endpoint:**` 程式碼區塊 → `- **Request Body:**` JSON 區塊 → `## Response Body:` → 範例 → `## 結構摘要表`（欄位、HTTP 狀態碼表格）
- 先查詢有沒有同路徑＋同 method 的列：有就更新屬性與內文對應段落，沒有才新增。
- 範例值（預設值、列舉值、欄位名）要從程式碼或 `init_db.sql` 查，不要自己填。

### 2. DB 規格
- 路徑：系統分析 › DB › 💾 DB 規格（https://app.notion.com/p/31d2dbee50808045bba7da8c3d6882cc）
- **MongoDB** 資料庫（標題「MongoDB 資料庫規格 (DB: stock_insight)」）：`collection://31d2dbee-5080-8068-8af0-000be3e5a0c9`
  一個 collection 一列。屬性：`Collcetion`（標題，注意原本就拼成這樣，不要改）、`用途`。
- **PostgreSQL** 資料庫（標題「PostgreSQL資料庫規格」）：`collection://3392dbee-5080-80de-bc12-000be52aadfa`
  一張表一列，標題格式 `xxx 表`（例如 `chats 表`），另有一列「總覽」。
  內文是該表的 `CREATE TABLE` SQL 程式碼區塊（含註解），以 repo 的 schema 檔為準。
- 只更新這次改動到的 collection／表；不要整批回填，除非使用者要求。

### 3. 系統設計
- 路徑：系統分析 › 系統設計 › ⚙️ 系統設計（https://app.notion.com/p/35e2dbee5080804d8b7cf5ba7a8675c3）
- 這頁裡是一個**無標題的 inline 資料庫**：`collection://41c2dbee-5080-8069-a346-000bf5d3ed0c`（只有 `Name` 標題欄位）。
  **一個設計主題一列**，內文寫在該列的頁面裡。現有列：「存Token 機制」、「@ 金融分析 Skill」。
- 新主題 → 在這個資料庫新增一列（加 emoji icon），不要建成「⚙️ 系統設計」頁的子頁面，也不要另開資料庫。
- 主題已存在 → 先 query 這個資料庫找到該列，再更新那一頁的對應段落。
- 內文風格參考「存Token 機制」：`##` 標題分段、`---` 分隔、比較用表格、流程用 ```txt 區塊、長 SQL／程式碼放 toggle 裡、關鍵句加粗。

### 4. 架構圖（只在使用者要求時更新）
- 路徑：系統分析 › 架構 › 架構圖（https://app.notion.com/p/41c2dbee50808062bcd5e8a0a83069b7）
- 放系統架構圖（PNG，亮色在前、暗色收在 toggle）與簡短說明；圖用 `/stack-diagram` 產生。

## 規則
- 資料庫裡的列用 `query-data-sources`（rows 模式）列出；fetch 資料庫只會回 schema，看不到有哪些列。
- 先 fetch 目標資料庫／頁面，確認現有列與結構後才寫入。如果 fetch 到的結構和上面寫的不同，先告訴使用者，不要自己猜位置。
- 用 `update_content` 搜尋取代時，`old_str` 要照 fetch 回來的樣子寫：inline code 會變成 `<code>…</code>`、粗體變 `<b>…</b>`，用 Markdown 反引號會比對不到。
- 程式碼裡已不存在的 endpoint／表，不要自行刪除，回報給使用者決定。
- 不新建頂層頁面或新資料庫；不要寫到上述位置以外。
- 完成後回報：每個位置新增或更新了什麼，附上連結。
