# SQL 開發調整手冊 (SQL Development Handbook)

本文件說明：**開發時若需要調整 PostgreSQL schema 或資料**，應如何進入 Docker、執行 SQL、以及如何把變更同步回程式庫。  
日常查詢與除錯語法請另見 [`maintenance_queries_spec.md`](./maintenance_queries_spec.md)；表結構與 ERD 請見 [`database_spec.md`](./database_spec.md)。

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
| `specifications/maintenance_queries_spec.md` | 進入 DB、日誌、維運查詢 |
| `specifications/google_sso.md` | 既有庫 Google SSO 欄位 migration 範例 |

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
