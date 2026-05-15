# 維運查詢語法 (Maintenance Queries)

這份文件記錄了系統在日常營運與除錯時，常用的資料庫進入方式與查詢語法大全。

## 1. 進入資料庫 (PostgreSQL)

本專案使用 Docker Compose 部署，資料庫預設在 `db` container 中執行。以下方式為標準登入做法：

### 1-1 使用 Docker Compose（推薦）

如果你目前位於專案根目錄，請先切換至 `deploy` 資料夾，再進入資料庫：

```bash
cd deploy
docker-compose exec db psql -U postgres -d Stock_Insight_Chat
```

### 1-2 直接使用 Docker Exec

若知道 container 的明確名稱（可透過 `docker ps` 確認，如 `deploy-db-1`），可使用：

```bash
docker exec -it <db_container_name> psql -U postgres -d Stock_Insight_Chat
```

成功登入後，終端機將出現 `Stock_Insight_Chat=#` 提示字元。
*(若要離開請輸入 `\q` 並按下 Enter 即可)*

---

## 2. 常用資料查詢語法

以下查詢皆需要在進入 PostgreSQL (`Stock_Insight_Chat=#`) 介面後執行。

### 2-1 登入驗證 (JWT Refresh Tokens)

用於確認使用者登入後，系統是否正確產生與寫入 Refresh Token：

```sql
SELECT * FROM refresh_tokens ORDER BY created_at DESC LIMIT 5;
```

### 2-2 AI Token 使用量 (Token Usage Logs)

用於確認每次對話結束後，系統是否有正確記錄 LLM Token 的消耗量：

```sql
SELECT * FROM token_usage_logs ORDER BY created_at DESC LIMIT 5;
```

### 2-3 使用者資訊 (Users)

快速確認最新註冊或特定使用者的登入資訊與狀態：

```sql
SELECT id, email, username, status, created_at FROM users ORDER BY created_at DESC LIMIT 5;
```

### 2-4 專案與對話室 (Projects & Chats)

查詢最近建立的專案與對話群組：

```sql
-- 查詢最近 5 個建立的專案
SELECT * FROM projects ORDER BY created_at DESC LIMIT 5;

-- 查詢最近 5 筆建立的對話空間
SELECT * FROM chats ORDER BY created_at DESC LIMIT 5;
```

---

## 3. 檢查資料表結構與索引

以下指令皆在已登入 `Stock_Insight_Chat=#` 的 **psql** 中執行。用途為確認**欄位／型別／索引／約束**是否與 `app/backend/database/init_db.sql`（及 migration）一致，並順便觀察**是否有資料寫入**。

### 3-1 單表結構（欄位、型別、預設值）

以下為 **psql 元指令**（不是純 SQL），提示字元須為 `Stock_Insight_Chat=#`：

```text
\d token_usage_logs
\d user_usage_quotas
\d messages
```

（可依需要改成其他表名；若要在一般 SQL 客戶端查同等資訊，可改查 `information_schema.columns`。）

### 3-2 索引：內建指令或查系統目錄

以 **psql 元指令** 快速看名稱符合前綴的索引：

```text
\di token_usage_logs*
\di user_usage_quotas*
```

以 **SQL** 列出某表的索引定義（便於對照 migration）：

```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'token_usage_logs'
ORDER BY indexname;
```

將 `tablename` 換成 `user_usage_quotas`、`messages` 等即可。

### 3-3 外鍵與其他約束

```sql
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'public.token_usage_logs'::regclass;
```

`contype` 常見值：`p` 主鍵、`f` 外鍵、`u` 唯一、`c` check。查其他表時請把 `token_usage_logs` 改成對應表名。

### 3-4 各表約略列數（寫入與否）

`n_live_tup` 為統計估計值，非精確 count，但適合快速看「有沒有資料、哪張表是空的」：

```sql
SELECT relname, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY relname;
```

### 3-5 解讀與維護注意

* **`token_usage_logs` 為 0 筆**：代表尚未成功 `INSERT`，不一定是 schema 錯誤；請搭配應用程式 log（例如含 `[TOKEN]`、`record_token_usage failed`）與端到端再打一次對話除錯。
* **日常不必**對開發庫做離線毀損修復；若僅懷疑統計過舊，可在維護視窗對單表執行 `ANALYZE tablename;`。僅在出現明確 catalog／儲存錯誤訊息時，再依 PostgreSQL 官方文件評估 `VACUUM`、`REINDEX` 等（須評估鎖與時間）。

---

*(維運人員可隨時依需求將常用的 SQL 查詢或除錯語法補充至本文件中。結構與索引檢查以 **§3** 為準。)*
