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

### 1. ⚙️ API 規格 → 資料庫「聊天室相關端點」
- data source：`collection://31d2dbee-5080-8035-8d28-000b75faff70`（雖然叫「聊天室相關端點」，所有 API 都放這裡）
- **一個 endpoint 一列**。屬性：
  - `API`（標題）：路徑，例如 `/api/chat/messages`
  - `HTTP method`：GET / POST / PUT / DELETE
  - `對應DB`：Mongodb / PostgreSQL
  - `DB 名稱`：例如 stock_insight
  - `Collection/table名稱`：用到的 collection 或 table，沒有合適選項可新增
  - `用途`：一句話說明
- 內文沿用既有格式（參考 `/api/chat/messages` 那列）：
  `- **Endpoint:**` 程式碼區塊 → `- **Request Body:**` JSON 區塊 → `## Response Body:` → 範例 → `## 結構摘要表`（表格）
- 先查詢有沒有同路徑＋同 method 的列：有就更新屬性與內文對應段落，沒有才新增。

### 2. 💾 DB 規格
- **MongoDB** 資料庫：`collection://31d2dbee-5080-8068-8af0-000be3e5a0c9`
  一個 collection 一列。屬性：`Collcetion`（標題，注意原本就拼成這樣，不要改）、`用途`。
- **PostgreSQL** 資料庫：`collection://3392dbee-5080-80de-bc12-000be52aadfa`
  一張表一列，標題格式 `xxx 表`（例如 `chats 表`），另有一列「總覽」。
  內文是該表的 `CREATE TABLE` SQL 程式碼區塊（含註解），以 repo 的 schema 檔為準。
- 只更新這次改動到的 collection／表；不要整批回填，除非使用者要求。

### 3. ⚙️ 系統設計
- 頁面：https://app.notion.com/p/35e2dbee5080804d8b7cf5ba7a8675c3
- **一個設計主題一個子頁面**（現有例：「⛺ 存Token 機制」），子頁面要有 emoji icon。
- 內文風格參考「存Token 機制」：`##` 標題分段、`---` 分隔、比較用表格、流程用 ```txt 區塊、長 SQL／程式碼放 toggle 裡、關鍵句加粗。
- 主題已存在 → 更新該子頁面的對應段落；新主題 → 新增子頁面。

## 規則
- 先 fetch 目標資料庫／頁面，確認現有列與結構後才寫入。
- 不新建頂層頁面或新資料庫；不要寫到上述位置以外。
- 完成後回報：每個位置新增或更新了什麼，附上連結。
