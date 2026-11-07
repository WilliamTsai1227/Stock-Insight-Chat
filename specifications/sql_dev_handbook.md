# SQL 開發調整手冊 (SQL Development Handbook)

本文件說明：**開發時若需要調整 PostgreSQL schema 或資料**，應如何進入 Docker、執行 SQL、以及如何把變更同步回程式庫。  
日常查詢與除錯語法見本文件 §11；表結構與 ERD 請見 [`database_spec.md`](./database_spec.md)。

---

## 1. 先搞懂：改 SQL 會影響誰？

| 情境 | `init_db.sql` 會自動生效嗎？ | 你該做什麼 |
|------|------------------------------|------------|
| **AWS RDS**（本機開發與生產目前都是這個） | ❌ 不會。RDS 沒有 `docker-entrypoint-initdb.d` 機制 | 用 `psql` 手動執行（§3），並新增 `migrations/V00x__*.sql` 留存 |
| **全新** Postgres volume（需啟用 db 服務，第一次 `up`） | ✅ 會。`init_db.sql` 只在 **volume 空白時** 由 `docker-entrypoint-initdb.d` 執行一次 | 把 DDL 寫進 `app/backend/database/init_db.sql` |
| **已有** `db_data` volume | ❌ 不會。重啟 `db` 容器 **不會** 重跑 init | 手動執行 migration，同上 |
| **只想改幾筆測試資料** | — | 直接 `psql` 執行 `UPDATE` / `DELETE`（開發環境即可） |

> **重要**：後端程式碼（FastAPI）**不會**在啟動時自動跑 migration 檔；migration 目錄是給人類與維運對照用的「版本化 SQL」，需自行 `psql` 或管線執行。

---

## 2. 你的 PostgreSQL 在哪？

> ⚠️ **`db` 服務目前在 [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) 中是整段註解掉的。** 本機開發與生產都直接連 **AWS RDS**，由 `.env` 的 `DATABASE_URL` 指定。
>
> 也就是說 `docker compose exec db psql ...` 會回 `no such service: db`。本手冊預設走 **§3.1 本機 psql 直連**；凡標示「（需啟用 db 服務）」的指令，都要先把 compose 內 `db:` 與 `volumes:` 的 `db_data:` 取消註解才能用。

| 情境 | 連線方式 |
|------|----------|
| **本機開發（目前預設）** | 本機 `psql` 直連 `.env` 的 `DATABASE_URL`（RDS） |
| **生產維運** | 同上，但 `DATABASE_URL` 指向正式 RDS —— **動手前先確認你連的是哪一個** |
| **要跑一顆本機 db 容器** | 取消註解 compose 的 `db:` 段，之後可用 `exec db psql` |

`deploy/docker-compose.yml` 其他服務：

| 服務名稱 | 映像／用途 | 你要進去改 SQL 嗎？ |
|----------|------------|---------------------|
| **`backend`** | FastAPI + Agent | ❌ 容器內未安裝 `psql`。除錯 API、看 log 用 |
| **`frontend`** | Nginx | ❌ |
| **`qdrant`** | 向量庫 | ❌（非 PostgreSQL） |
| ~~`db`~~ | `postgres:16-alpine` | 預設停用，見上方說明 |

**啟用 db 服務時**的連線資訊（與 compose 內註解一致）：

- **Host（容器內）**：`db`／**Host（本機）**：`localhost:5432`
- **User**：`postgres`／**Password**：`password123`／**Database**：`Insight`

---

## 3. 連線 PostgreSQL

以下指令預設在**專案根目錄**（`Stock-Insight-Chat/`，內含 `deploy/`）執行。

### 3.1 本機 `psql` 直連（目前預設做法）

先確認你要連的是哪一個 database：

```bash
grep '^DATABASE_URL=' .env
```

開互動式 session：

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
```

離開：`\q`。若本機沒有 `psql`：`brew install libpq && brew link --force libpq`（macOS）。

> RDS 通常要求 TLS。連不上且錯誤訊息提到 SSL 時，在連線字串尾端加 `?sslmode=require`
> （這是給 `psql` 用的；後端程式走的是 `.env` 的 `DATABASE_SSL=require`，見 [`env.md`](./env.md)）。

### 3.2 單次執行一條 SQL

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -c "SELECT version();"
```

### 3.3 執行整份 migration

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  -f app/backend/database/migrations/V004__token_usage_logs_add_caller.sql
```

> 對**正式** RDS 執行前，先確認 `DATABASE_URL` 指向的是你要改的那一個 database
> （chat 用 `Insight`、探索用 `kinetic`，兩者在同一台 RDS 上）。

### 3.4 進入 `backend` 容器（僅除錯應用，不改 schema）

```bash
docker compose -f ./deploy/docker-compose.yml exec backend sh
```

在內可檢查環境變數、`python` 等；容器內**未安裝 `psql`**。

### 3.5 走 db 容器（需啟用 db 服務，見 §2）

```bash
docker compose -f ./deploy/docker-compose.yml ps
docker compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Insight

# 單次執行
docker compose -f ./deploy/docker-compose.yml exec db \
  psql -U postgres -d Insight -c "SELECT version();"

# 從主機檔案執行 migration
docker compose -f ./deploy/docker-compose.yml exec -T db \
  psql -U postgres -d Insight \
  -f - < app/backend/database/migrations/V004__token_usage_logs_add_caller.sql
```

---

## 4. 開發時調整 SQL 的標準流程

建議每次 schema 變更都走完下列步驟，避免「本機好了、同事或 CI 庫還是舊的」。

```
1. 設計變更（欄位、索引、約束）
      ↓
2. 更新 app/backend/database/init_db.sql（新環境用）
      ↓
3. 在 init_db.sql 末尾或表定義後加冪等 ALTER（可選，方便複製到既有庫）
      ↓
4. 新增 app/backend/database/migrations/V00x__描述.sql
      ↓
5. 對目標 DB 執行 migration（§3.3）
      ↓
6. 修改後端 Python（查詢欄位、ORDER BY、寫入 updated_at 等）
      ↓
7. up -d --build backend（改程式）／up -d backend（只改 .env）
      ↓
8. 在 psql 驗證：\d 表名、SELECT 一筆、跑相關 API
```

> 部署到生產時**先跑 migration、再部署新後端**（新程式碼可能依賴新欄位）；順序見 [`release_handbook.md`](./release_handbook.md) §3.3。

### 4.1 修改 `init_db.sql`

- 路徑：`app/backend/database/init_db.sql`
- **新表**：用 `CREATE TABLE IF NOT EXISTS ...`
- **新欄位（建議冪等）**：在表定義後加：

```sql
ALTER TABLE projects ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
```

- **新索引**：`CREATE INDEX IF NOT EXISTS ...`

> 僅改 `init_db.sql` **不會**更新你已經跑過的 `db_data`；§4.2 仍必做。

### 4.2 新增 migration 檔

- 目錄：`app/backend/database/migrations/`
- 命名：`V{三位數}__簡短英文描述.sql`（例：`V006__projects_chats_updated_at.sql`）
- 內容建議包在 `BEGIN;` / `COMMIT;` 內，並盡量使用 `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`

範本：

```sql
-- ============================================================
-- Migration V006: projects / chats 新增 updated_at 與索引
-- ============================================================

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chats_updated_at ON chats(updated_at DESC);

COMMIT;
```

既有 migration 列表（執行前請對照是否已套用過）：

| 檔案 | 用途 |
|------|------|
| `V002__add_chat_id_to_token_usage_logs.sql` | `token_usage_logs.chat_id` |
| `V003__token_usage_logs_composite_indexes.sql` | 用量表複合索引 |
| `V004__token_usage_logs_add_caller.sql` | `token_usage_logs.caller` |
| `V005__seed_subscription_tiers.sql` | 訂閱等級種子資料 |
| `V006__quota_reset_logs.sql` | 新增 `quota_reset_logs` 表 |
| `V007__user_feedback.sql` | 新增 `user_feedback` 表 |
| `V008__user_feedback_tokens_granted.sql` | `user_feedback.tokens_granted` |
| `V009__notes.sql` | 新增 `note_pages`、`notes`（筆記功能）。2026-11-07 已補套用正式 RDS |
| `V010__note_pages_source_chat.sql` | `note_pages.source_chat_id` + 唯一索引（須在 V009 之後）。2026-11-07 已補套用正式 RDS |
| `V011__portfolio_insight.sql` | 投資組合排程分析四張表（2026-11-07 已套用正式 RDS） |

### 4.3 對既有資料庫執行 migration

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  -f app/backend/database/migrations/V008__user_feedback_tokens_granted.sql
```

或開互動式 session（§3.1）後貼上 migration 內容。**需啟用 db 服務**時見 §3.5。

### 4.4 驗證

在 `psql` 內：

```sql
-- 看表結構
\d projects
\d chats

-- 確認欄位存在
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'chats' AND column_name = 'updated_at';

-- 確認排序用的索引
\di idx_chats_updated_at
```

套用後端變更：

```bash
# 改了 Python 程式或依賴
docker compose -f ./deploy/docker-compose.yml up -d --build backend

# 只改了 .env（注意：restart 不會重讀 env_file）
docker compose -f ./deploy/docker-compose.yml up -d backend
```

（完整 Docker 維運指令見 [`docker_ops_handbook.md`](./docker_ops_handbook.md) §3、§4。）

---

## 5. 常見操作速查

### 5.1 只看表有哪些

```sql
\dt
```

### 5.2 看某張表欄位與索引

```sql
\d+ messages
```

### 5.3 開發環境重置整個資料庫（會刪光資料）

**連 RDS 時（目前預設）**：沒有「清 volume」這個捷徑。`init_db.sql` 本身是冪等的（全部 `IF NOT EXISTS`），重跑不會清資料；真要清空請用 SQL，**且務必先確認 `DATABASE_URL` 不是正式庫**：

```bash
grep '^DATABASE_URL=' .env      # 先看清楚你要清的是哪一個
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" \
  -f app/backend/database/init_db.sql
```

**需啟用 db 服務時**，可用清 volume 的方式：

```bash
docker compose -f ./deploy/docker-compose.yml down
docker volume ls | grep db
docker volume rm <實際_volume_名稱>
docker compose -f ./deploy/docker-compose.yml up -d db
```

新 volume 啟動時會**自動**再執行一次 `init_db.sql`。

### 5.4 只改測試資料（不動 schema）

```sql
UPDATE chats SET title = '測試標題' WHERE id = '...';
```

### 5.5 大表加索引（進階）

開發用小表可直接 `CREATE INDEX`。  
若未來線上資料量大，可改 `CREATE INDEX CONCURRENTLY`（須在 transaction 外單獨執行），詳見 PostgreSQL 官方文件。

---

## 6. 與應用程式的分工

| 層級 | 負責內容 | 範例 |
|------|----------|------|
| **SQL / migration** | 表、欄位、索引、約束 | `chats.updated_at` 欄位存在 |
| **後端 API** | 何時 `UPDATE`、查詢 `ORDER BY` | 使用者送訊息後 `UPDATE chats SET updated_at = NOW()`；`GET /api/chat/all` 用 `ORDER BY updated_at DESC` |
| **前端** | 顯示與快取 | 呼叫 `/api/chat/all`，依後端回傳順序渲染 sidebar |

只改 SQL 而不改 API，新欄位可能永遠不會被寫入或查詢。

---

## 7. 檢查清單（提交前）

- [ ] `init_db.sql` 已更新（新環境可一鍵建庫）
- [ ] `migrations/V00x__*.sql` 已新增（既有環境可重現）
- [ ] 已對開發用 DB 跑過 migration 並用 `\d` 驗證
- [ ] 後端查詢／寫入已對齊新欄位
- [ ] `database_spec.md` 若表結構有變，是否需同步更新 ERD／欄位表（可另開 PR）

---

## 8. 相關檔案與文件

| 路徑 | 說明 |
|------|------|
| `deploy/docker-compose.yml` | `db` 服務、volume、`init_db.sql` 掛載 |
| `app/backend/database/init_db.sql` | 新資料庫初始化腳本 |
| `app/backend/database/migrations/` | 版本化增量 SQL |
| `specifications/database_spec.md` | 資料表規格與 ERD |
| `specifications/docker_ops_handbook.md` | Docker 啟停、log、進容器 |

---

## 9. 故障排除

| 現象 | 可能原因 | 處理 |
|------|----------|------|
| 改了 `init_db.sql` 重啟後表沒變 | 連的是 RDS，或 volume 已存在（init 只跑一次） | 手動跑 migration（§3.3）或 §5.3 |
| `exec db psql` 回 `no such service: db` | **`db` 服務預設是註解掉的**（見 §2） | 改用 §3.1 本機 psql 直連，或取消註解 compose 的 `db:` 段 |
| `psql` 連 RDS 報 SSL 錯 | RDS 強制 TLS | 連線字串加 `?sslmode=require`；後端則設 `DATABASE_SSL=require` |
| `ALTER TABLE ... ADD CONSTRAINT` 失敗 | 舊資料不符合新約束 | 先 `SELECT` 找出異常列並修補或刪除 |
| API 仍照 `created_at` 排序 | 只改了 DB，沒改 Python | 檢查 `app/backend/api/*.py` 的 SQL |
| 後端啟動報 `column "updated_at" does not exist` | migration 未套用到目前連線的 DB | 對 **同一個** `Insight` 執行 §4.3 |

---

## 10. 三方 schema 比對（正式 RDS／本機 compose／規格）

`init_db.sql` + `migrations/` 是規格的唯一來源。三邊應該**完全一致**，懷疑不一致時這樣比：

```bash
# A. 規格基準：用 init_db.sql 建一個全新的資料庫
docker run -d --name schema-ref -e POSTGRES_PASSWORD=x -e POSTGRES_DB=Insight postgres:16-alpine
docker exec -i schema-ref psql -U postgres -d Insight -q < app/backend/database/init_db.sql

# 三邊各輸出一份「排序過、與建立順序無關」的結構清單
SNAP=app/backend/database/schema_snapshot.sql
docker exec -i schema-ref  psql -U postgres -d Insight -At < $SNAP > /tmp/ref.txt
docker exec -i deploy-db-1 psql -U postgres -d Insight -At < $SNAP > /tmp/local.txt
docker run --rm -i postgres:18-alpine psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#+asyncpg##')?sslmode=require" -At < $SNAP > /tmp/prod.txt

diff /tmp/ref.txt /tmp/prod.txt; diff /tmp/ref.txt /tmp/local.txt
docker rm -f schema-ref
```

`schema_snapshot.sql` 輸出表、欄位（型別、NOT NULL、預設值）、約束、索引、函式、觸發器、extension，
每項一行並排序。不直接 diff `pg_dump` 是因為後補的欄位（`ALTER TABLE ADD COLUMN`）在 dump 裡排在最後，
會和 `init_db.sql` 的欄位順序不同，產生大量假差異。

**已知、可忽略的差異**：正式 RDS 是 PostgreSQL 18，pgcrypto 多了 `fips_mode()` 函式；本機是 16，沒有這個函式。

### 2026-11-07 比對紀錄

| 對象 | 結果 | 處理 |
| :--- | :--- | :--- |
| 正式 RDS | 缺 V009、V010（筆記） | 已套用 → 與規格一致 |
| 本機 compose | 缺 V006、V007、V008（`quota_reset_logs`、`user_feedback`）；`users.google_sub` 停在早期 Google SSO 升級的過渡態（nullable + 部分唯一索引 `ux_users_google_sub`） | 已套用 V006–V008；本機測試帳號 `mcp-test@local.dev` 補上佔位 `google_sub = 'local-dev:mcp-test'` 後，改回規格的 `NOT NULL` + `users_google_sub_key` → 與規格一致 |
| `database_spec.md` | §2 欄位名稱、型別、NOT NULL 與實際一致；§3 漏列 4 個 `uq_*` 唯一索引 | 已補 |

比對後三邊共 359 項結構完全相同（`fips_mode()` 除外）。

---

## 11. 常用維運查詢

（原 `maintenance_queries_spec.md`；Docker log 與重建指令見 [`docker_ops_handbook.md`](./docker_ops_handbook.md)）

### 1. 進入資料庫 (PostgreSQL)

> **Schema 變更、migration 流程、init_db 與既有 volume 的差異**：請見 [`sql_dev_handbook.md`](./sql_dev_handbook.md)。

> ⚠️ **`db` 服務目前在 [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) 中是整段註解掉的**，本機開發與生產都直接連 **AWS RDS**（由 `.env` 的 `DATABASE_URL` 指定）。`exec db psql` 會回 `no such service: db`。

#### 1-1 本機 `psql` 直連（目前預設做法）

在**專案根目錄**執行：

```bash
grep '^DATABASE_URL=' .env      # 先確認你要連哪一個 database
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
```

本機若沒有 `psql`：macOS 可 `brew install libpq && brew link --force libpq`。  
RDS 強制 TLS 時，連線字串尾端加 `?sslmode=require`。

> 對**正式** RDS 下 SQL 前務必確認連的是哪一台、哪一個 database（chat 用 `Insight`、探索用 `kinetic`）。本文件多數查詢是唯讀的，但 §1-3 之後有 DDL。

#### 1-2 走 db 容器（需啟用 db 服務）

先把 compose 內 `db:` 與 `volumes:` 的 `db_data:` 取消註解並 `up -d db`，之後：

```bash
docker compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Insight

# 或已知 container 名稱時（docker ps 可查，如 deploy-db-1）
docker exec -it <db_container_name> psql -U postgres -d Insight
```

成功登入後，終端機將出現 `Insight=#` 提示字元。
*(若要離開請輸入 `\q` 並按下 Enter 即可)*

#### 1-3 變更資料庫結構／新增或刪除索引（`token_usage_logs`）

在已進入 **`Insight=#`** 後，若既有資料庫仍為舊版單欄索引（`idx_token_usage_logs_user_id`、`idx_token_usage_logs_chat_id`），可手動執行下列交易：**新增複合索引**並**移除已冗餘的單欄索引**。  
（與程式庫中 migration **`app/backend/database/migrations/V003__token_usage_logs_composite_indexes.sql`** 等價；新環境若以 **`init_db.sql`** 建庫則通常已含這些索引，無須重複執行。）

**說明：** 未在此 DROP **`idx_token_usage_logs_created_at`**；若僅依時間掃描（無 `user_id`／`chat_id`）仍需該單欄索引，請見 **`init_db.sql`** 中的 `created_at DESC` 定義。

```sql
BEGIN;

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_chat
    ON token_usage_logs(user_id, chat_id);

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_created_at
    ON token_usage_logs(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_chat_created_at
    ON token_usage_logs(chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_created_at ON token_usage_logs(created_at DESC);

DROP INDEX IF EXISTS idx_token_usage_logs_user_id;
DROP INDEX IF EXISTS idx_token_usage_logs_chat_id;

COMMIT;
```

線上大表若要降低鎖表時間，可改用以 **`CREATE INDEX CONCURRENTLY`** 為主的流程（須拆成多個交易；細節見 [PostgreSQL 文件：CREATE INDEX CONCURRENTLY](https://www.postgresql.org/docs/current/sql-createindex.html)）。

#### 1-4 變更資料庫結構／新增 `caller` 欄位（`token_usage_logs`）

若資料庫為較早版本建立的 **`token_usage_logs`**（尚未含 **`caller`**），在 **`Insight=#`** 下可手動執行下列交易，與 **`init_db.sql`** 建表時第 45 行之 **`caller VARCHAR(50)`**（可 NULL，語意：router／analyst 等 LLM 輪次來源）對齊：  
（與程式庫中 migration **`app/backend/database/migrations/V004__token_usage_logs_add_caller.sql`** 等價；**以 `init_db.sql` 全新建庫者無須執行**。）

```sql
BEGIN;

ALTER TABLE token_usage_logs
    ADD COLUMN IF NOT EXISTS caller VARCHAR(50);

COMMENT ON COLUMN token_usage_logs.caller IS 'LLM 輪次來源：router、analyst 等（可 NULL）';

COMMIT;
```

---


以下查詢皆需要在進入 PostgreSQL (`Insight=#`) 介面後執行。

#### 2-1 登入驗證 (JWT Refresh Tokens)


```sql
SELECT * FROM refresh_tokens ORDER BY created_at DESC LIMIT 5;
```


#### 2-2 Token 用量流水帳 (`token_usage_logs`)

設計可為「**同一 `chat_id` 多列**」（每次 LLM `on_chat_model_end` 一筆）；總用量請對該對話 `SUM(cost_usd)`、`SUM(total_tokens)`。

索引對齊：`init_db.sql` 含 `(user_id, chat_id)`、`(user_id, created_at DESC)`、`(chat_id, created_at DESC)`、`(created_at DESC)`；既有資料庫請套用 migration **`V003__token_usage_logs_composite_indexes.sql`**。  
可選欄位 **`caller`**（例如 `router`／`analyst`，區分每一輪 LLM 來源）：既有庫請套用 migration **`V004__token_usage_logs_add_caller.sql`**，或於 psql 手動執行 **§1-4** 之 `ALTER TABLE`。

確認是否有寫入（最新幾筆）：

```sql
SELECT * FROM token_usage_logs ORDER BY created_at DESC LIMIT 5;
```

某一使用者、某一對話，依模型彙總（注意：**聚合後不能再選裸的 `created_at`**；時間請用 `MIN`/`MAX` 或拆另一段查詢）：

```sql
SELECT
    model_name,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(total_tokens) AS total_tokens,
    SUM(cost_usd) AS cost_usd
FROM token_usage_logs
WHERE user_id = 'YOUR_UUID'::uuid AND chat_id = 'YOUR_CHAT_UUID'::uuid
GROUP BY model_name;
```

同一對話按時間列出每一輪明細：

```sql
SELECT id, caller, model_name, prompt_tokens, completion_tokens, total_tokens, cost_usd, created_at
FROM token_usage_logs
WHERE chat_id = 'YOUR_CHAT_UUID'::uuid
ORDER BY created_at ASC;
```

某使用者在某時間區間的總用量（報表／對帳）：

```sql
SELECT SUM(total_tokens) AS total_tokens, SUM(cost_usd) AS cost_usd
FROM token_usage_logs
WHERE user_id = 'YOUR_UUID'::uuid
  AND created_at >= NOW() - INTERVAL '30 days';
```

其他常見情境：依 `user_id` + 時間 `GROUP BY date_trunc('day', created_at)` 做日報；全站最近 N 筆掃描用 `ORDER BY created_at DESC LIMIT N`（可走 `created_at` 索引）。已有 **`caller`** 時可在相同 `WHERE` 下 `GROUP BY caller` 區分 router／analyst 用量。

#### 2-3 使用者資訊 (Users)

快速確認最新註冊或特定使用者的登入資訊與狀態：

```sql
SELECT id, email, username, status, created_at FROM users ORDER BY created_at DESC LIMIT 5;
```

#### 2-4 專案與對話室 (Projects & Chats)

查詢最近建立的專案與對話群組：

```sql
-- 查詢最近 5 個建立的專案
SELECT * FROM projects ORDER BY created_at DESC LIMIT 5;

-- 查詢最近 5 筆建立的對話空間
SELECT * FROM chats ORDER BY created_at DESC LIMIT 5;
```

---

### 3. 檢查資料表結構與索引

以下指令皆在已登入 `Insight=#` 的 **psql** 中執行。用途為確認**欄位／型別／索引／約束**是否與 `app/backend/database/init_db.sql`（及 migration）一致，並順便觀察**是否有資料寫入**。

#### 3-1 單表結構（欄位、型別、預設值）

以下為 **psql 元指令**（不是純 SQL），提示字元須為 `Insight=#`：

```text
\d token_usage_logs
\d user_usage_quotas
\d messages
```

（可依需要改成其他表名；若要在一般 SQL 客戶端查同等資訊，可改查 `information_schema.columns`。）

#### 3-2 索引：內建指令或查系統目錄

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

#### 3-3 外鍵與其他約束

```sql
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'public.token_usage_logs'::regclass;
```

`contype` 常見值：`p` 主鍵、`f` 外鍵、`u` 唯一、`c` check。查其他表時請把 `token_usage_logs` 改成對應表名。

#### 3-4 各表約略列數（寫入與否）

`n_live_tup` 為統計估計值，非精確 count，但適合快速看「有沒有資料、哪張表是空的」：

```sql
SELECT relname, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY relname;
```

#### 3-5 解讀與維護注意

* **`token_usage_logs` 為 0 筆**：代表尚未成功 `INSERT`，不一定是 schema 錯誤；請搭配應用程式 log（例如含 `[TOKEN]`、`record_token_usage failed`）、**§0-5**（映像／`openai`／`StreamUsageChatOpenAI`）與端到端再打一次對話除錯。
* **日常不必**對開發庫做離線毀損修復；若僅懷疑統計過舊，可在維護視窗對單表執行 `ANALYZE tablename;`。僅在出現明確 catalog／儲存錯誤訊息時，再依 PostgreSQL 官方文件評估 `VACUUM`、`REINDEX` 等（須評估鎖與時間）。

---

*(維運人員可隨時依需求將常用的 SQL 查詢或除錯語法補充至本文件中。後端 Docker 日誌見 **§0**，映像與 Token／`StreamUsageChatOpenAI` 排查見 **§0-5**；資料庫表結構與索引檢查以 **§3** 為準。)*

---

## 12. 手動配額重置（quota_reset_logs）

（原 `readme_full_details.md`）

營運上若需**提前歸零某使用者的配額計數**（讓其可繼續發問），但**保留全部花費流水**，請使用姊妹專案 **Insight-Monitor**（獨立 repo，與本專案同層）的「配額重置」頁，或依下列 SQL 手動執行。

#### 三表分工

| 表 | 用途 | 重置時 |
|----|------|--------|
| `user_usage_quotas` | 當期配額計數（`used_tokens`） | **歸零**，`current_period_start` → NOW() |
| `token_usage_logs` | 永久 append-only 流水（Token、花費） | **不動** |
| `quota_reset_logs` | 每次重置前的區間摘要 | **INSERT 一筆** |

Stock-Insight-Chat **主程式不讀寫** `quota_reset_logs`；僅 Monitor 或維運 SQL 寫入。

#### Table Schema（`quota_reset_logs`）

見 `app/backend/database/init_db.sql` **§12-A**；既有庫請套用 migration **`V006__quota_reset_logs.sql`**：

```sql
CREATE TABLE IF NOT EXISTS quota_reset_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reset_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    previous_period_start TIMESTAMPTZ,
    previous_used_tokens BIGINT NOT NULL DEFAULT 0,
    period_total_tokens BIGINT,
    period_total_cost_usd NUMERIC(10, 6),
    note TEXT,
    reset_by VARCHAR(100) DEFAULT 'monitor'
);
CREATE INDEX IF NOT EXISTS idx_quota_reset_logs_user_reset_at
    ON quota_reset_logs(user_id, reset_at DESC);
```

#### SQL 建置（既有 RDS）

在 **Insight=#** 或任何連到本專案 DB 的客戶端（專案根目錄）：

```bash
# 本機 Docker db（若 compose 有啟 db 服務）
docker-compose -f ./deploy/docker-compose.yml exec db \
  psql -U postgres -d Insight \
  -f - < app/backend/database/migrations/V006__quota_reset_logs.sql

# 或 RDS／任意連線（推薦）
psql "$DATABASE_URL" -f app/backend/database/migrations/V006__quota_reset_logs.sql
```

亦可於 psql 內直接貼上上方 **Table Schema** 的 `CREATE TABLE`／`CREATE INDEX`（`IF NOT EXISTS`，可重複執行）。

#### 重置語法（手動 psql）

將 `YOUR_USER_UUID` 換成 `users.id`（Monitor「使用者」頁可複製 UUID）：

```sql
BEGIN;

INSERT INTO user_usage_quotas (user_id, current_period_start, used_tokens)
VALUES (
    'YOUR_USER_UUID'::uuid,
    date_trunc('month', NOW() AT TIME ZONE 'UTC'),
    0
)
ON CONFLICT (user_id) DO NOTHING;

SELECT used_tokens, current_period_start
FROM user_usage_quotas
WHERE user_id = 'YOUR_USER_UUID'::uuid
FOR UPDATE;

INSERT INTO quota_reset_logs (
    user_id,
    previous_period_start,
    previous_used_tokens,
    period_total_tokens,
    period_total_cost_usd,
    note,
    reset_by
)
SELECT
    q.user_id,
    q.current_period_start,
    q.used_tokens,
    COALESCE(SUM(t.total_tokens), 0),
    COALESCE(SUM(t.cost_usd), 0),
    'manual reset via psql',
    'psql'
FROM user_usage_quotas q
LEFT JOIN token_usage_logs t
    ON t.user_id = q.user_id
   AND t.created_at >= q.current_period_start
   AND t.created_at < NOW()
WHERE q.user_id = 'YOUR_USER_UUID'::uuid
GROUP BY q.user_id, q.current_period_start, q.used_tokens;

UPDATE user_usage_quotas
SET
    used_tokens = 0,
    current_period_start = NOW(),
    updated_at = NOW()
WHERE user_id = 'YOUR_USER_UUID'::uuid;

COMMIT;
```

**效果**：使用者可再次發問（配額計數從 0 起算）；`token_usage_logs` 歷史花費完整保留；`quota_reset_logs` 留存該區間 Token／花費摘要供對帳。

#### 查詢各重置區間

```sql
SELECT reset_at, previous_period_start, previous_used_tokens,
       period_total_tokens, period_total_cost_usd, note
FROM quota_reset_logs
WHERE user_id = 'YOUR_USER_UUID'::uuid
ORDER BY reset_at DESC;
```

Monitor 實作細節見 Insight-Monitor repo 的 `README.md`「配額重置（quota_reset_logs）」一節。
