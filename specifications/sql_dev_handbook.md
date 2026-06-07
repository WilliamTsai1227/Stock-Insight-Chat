# SQL 開發調整手冊 (SQL Development Handbook)

本文件說明：**開發時若需要調整 PostgreSQL schema 或資料**，應如何進入 Docker、執行 SQL、以及如何把變更同步回程式庫。  
日常查詢與除錯語法請另見 [`maintenance_queries_spec.md`](./maintenance_queries_spec.md)；表結構與 ERD 請見 [`database_spec.md`](./database_spec.md)。

---

## 1. 先搞懂：改 SQL 會影響誰？

| 情境 | `init_db.sql` 會自動生效嗎？ | 你該做什麼 |
|------|------------------------------|------------|
| **全新** Postgres volume（第一次 `docker compose up`） | ✅ 會。映像掛載的 `init_db.sql` 只在 **volume 空白時** 由 `docker-entrypoint-initdb.d` 執行一次 | 把 DDL 寫進 `app/backend/database/init_db.sql` |
| **已有** `db_data` volume（本機開發最常見） | ❌ 不會。重啟 `db` 容器 **不會** 重跑 init | 在 **`db` 容器內用 `psql` 手動執行**，並新增 `migrations/V00x__*.sql` 留存 |
| **只想改幾筆測試資料** | — | 直接 `psql` 執行 `UPDATE` / `DELETE`（開發環境即可） |

> **重要**：後端程式碼（FastAPI）**不會**在啟動時自動跑 migration 檔；migration 目錄是給人類與維運對照用的「版本化 SQL」，需自行 `psql` 或管線執行。

---

## 2. Docker 服務對照

`deploy/docker-compose.yml` 與本手冊相關的服務：

| 服務名稱 | 映像／用途 | 你要進去改 SQL 嗎？ |
|----------|------------|---------------------|
| **`db`** | `postgres:16-alpine` | ✅ **是**。所有 schema / 資料變更都在這裡做 |
| **`backend`** | FastAPI + Agent | ❌ 一般不裝 `psql`。除錯 API、看 log 用 |
| **`frontend`** | Nginx | ❌ |
| **`qdrant`** | 向量庫 | ❌（非 PostgreSQL） |

連線資訊（與 compose 一致）：

- **Host（容器內）**：`db`
- **Host（本機）**：`localhost`（port `5432` 已映射）
- **User**：`postgres`
- **Password**：`password123`
- **Database**：`Insight`

---

## 3. 進入容器與連線 PostgreSQL

以下指令預設在**專案根目錄**（`Stock-Insight-Chat/`，內含 `deploy/`）執行。  
Compose V2 可用 `docker compose`；舊版二進制請改為 `docker-compose`。

### 3.1 確認容器已啟動

```bash
docker compose -f ./deploy/docker-compose.yml ps
```

### 3.2 進入 `db` 並開互動式 `psql`（最常用）

```bash
docker compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Insight
```

成功後提示字元為：`Insight=#`  
離開：`\q`

### 3.3 不進互動模式，單次執行一條 SQL

```bash
docker compose -f ./deploy/docker-compose.yml exec db \
  psql -U postgres -d Insight -c "SELECT version();"
```

### 3.4 從主機檔案執行整份 migration

```bash
docker compose -f ./deploy/docker-compose.yml exec -T db \
  psql -U postgres -d Insight \
  -f - < app/backend/database/migrations/V004__token_usage_logs_add_caller.sql
```

或先 `docker compose cp` 再 `psql -f /path/in/container`（路徑依環境調整）。

### 3.5 進入 `backend` 容器（僅除錯應用，不改 schema）

```bash
docker compose -f ./deploy/docker-compose.yml exec backend sh
```

在內可檢查環境變數、`python` 等。**Schema 變更請一律用 §3.2 的 `db` + `psql`。**

### 3.6 本機已安裝 `psql` 時（可選）

若 `5432` 已映射，也可不進容器：

```bash
PGPASSWORD=password123 psql -h localhost -U postgres -d Insight
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
5. 在本機「已有 volume」的 DB 上 exec db psql 執行 migration
      ↓
6. 修改後端 Python（查詢欄位、ORDER BY、寫入 updated_at 等）
      ↓
7. 重啟 backend（必要時 --build）
      ↓
8. 在 psql 驗證：\d 表名、SELECT 一筆、跑相關 API
```

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

### 4.3 在本機既有資料庫執行 migration

```bash
docker compose -f ./deploy/docker-compose.yml exec db \
  psql -U postgres -d Insight
```

貼上 migration 內容，或 §3.4 從檔案餵入。

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

重啟後端：

```bash
docker compose -f ./deploy/docker-compose.yml restart backend
```

（完整 Docker 維運指令見 [`docker_ops_handbook.md`](./docker_ops_handbook.md)。）

程式或依賴有變更時：

```bash
docker compose -f ./deploy/docker-compose.yml up -d --build backend
```

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

僅限本機開發、確認可清空時：

```bash
docker compose -f ./deploy/docker-compose.yml down
docker volume rm deploy_db_data
# 若 volume 名稱不同，先用：docker volume ls | grep db
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
- [ ] 本機 `db_data` 上已手動跑過 migration 並驗證 `\d`
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
| 改了 `init_db.sql` 重啟後表沒變 | volume 已存在，init 只跑一次 | 手動跑 migration 或 §5.3 清 volume |
| `exec db psql` 失敗 | `db` 未啟動 | `docker compose ... up -d db` |
| `ALTER TABLE ... ADD CONSTRAINT` 失敗 | 舊資料不符合新約束 | 先 `SELECT` 找出異常列並修補或刪除 |
| API 仍照 `created_at` 排序 | 只改了 DB，沒改 Python | 檢查 `app/backend/api/*.py` 的 SQL |
| 後端啟動報 `column "updated_at" does not exist` | migration 未套用到目前連線的 DB | 對 **同一個** `Insight` 執行 §4.3 |
