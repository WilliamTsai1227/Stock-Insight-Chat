# 維運查詢語法 (Maintenance Queries)

這份文件記錄了系統在日常營運與除錯時，常用的資料庫進入方式與查詢語法大全。

## 0. 查看 Docker 後端日誌（backend）

Compose 檔為 `deploy/docker-compose.yml`，**後端服務名稱為 `backend`**（FastAPI + Agent，對外埠為 `8000`）。

**以下預設均在「專案根目錄」**（例如 `Stock-Insight-Chat/`，內含 `deploy/`、`app/`）執行，與 README 的 Docker 指令一致：

```bash
docker-compose -f ./deploy/docker-compose.yml <子命令>
```

若你**已 `cd deploy`**，可改為：`docker-compose -f docker-compose.yml ...`（路徑視當前目錄調整）。

### 0-0 啟動／重建容器（對照程式與依賴是否進映像）

程式或 `app/backend/requirements.txt` 變更後，建議在根目錄：

```bash
docker-compose -f ./deploy/docker-compose.yml up --build -d
```

僅重啟並重建後端（較快）：

```bash
docker-compose -f ./deploy/docker-compose.yml up -d --build backend
```

### 0-0-A 語法對照：`--no-cache`、`up`、`build`（避免錯指令）

下列寫法是**錯誤**的，`docker-compose`/`docker compose` 不會這樣解析：

```bash
# ❌ 錯誤：up 子命令不能把 build、--no-cache 這樣接在後面
docker-compose -f ./deploy/docker-compose.yml up -d build --no-cache
```

- **`--no-cache`** 只用在 **`build`**：`build --no-cache` 會禁止沿用 Docker build cache（較適合對照「依賴／COPY 是否真的重裝進映像」）。
- **`up --build`**：`up … --build [服務]` 會在啟動前**觸發建置**，但預設**仍會使用 build cache**，**不等同**於 `build --no-cache`。

**所有服務**都不吃快取、重建映像並在背景啟動（含 `frontend`、`backend`、`db`/`qdrant` 等在 compose 裡定義的服務）：**分兩步**為正解：

```bash
docker-compose -f ./deploy/docker-compose.yml build --no-cache
docker-compose -f ./deploy/docker-compose.yml up -d
```

若只想**不吃快取重建某一服務**，再啟動（例：後端）：

```bash
docker-compose -f ./deploy/docker-compose.yml build --no-cache backend
docker-compose -f ./deploy/docker-compose.yml up -d backend
```

（細節與對照容器內套件版本：**§0-5-1**。）

### 0-1 使用 Docker Compose（`docker compose` 或 `docker-compose` 擇一）

在專案根目錄：

```bash
# 持續追蹤輸出（Ctrl+C 結束）
docker-compose -f ./deploy/docker-compose.yml logs -f backend

# 只看最近約 300 行再進入追蹤
docker-compose -f ./deploy/docker-compose.yml logs --tail 300 -f backend

# 附上容器時間戳（除錯方便對時間）
docker-compose -f ./deploy/docker-compose.yml logs -f --timestamps backend
```

若本機已安裝 **Compose V2**，可將 `docker-compose` 換成 `docker compose`（`-f ./deploy/docker-compose.yml` 用法相同）。

### 0-2 僅列出最近紀錄、不進入追蹤

```bash
docker-compose -f ./deploy/docker-compose.yml logs --tail 200 backend
```

### 0-3 直接使用 `docker logs`（已知 container 名稱）

先找出執行中的容器名（常類似 `{專案目錄名}-backend-1`，例如資料夾名為 `deploy` 時可能是 `deploy-backend-1`，實際以本機為準）：

```bash
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i backend
```

對查到的 `<container_name>` 執行：

```bash
docker logs -f --tail 300 <container_name>
```

可加時間戳：`docker logs -f -t --tail 300 <container_name>`。

### 0-4 與後端除錯相關常見 log 關鍵字

在終端機用 **grep**（管線）過濾，或將 `--tail` 放大後再以編輯器搜尋：

- `[TOKEN]`、`[TOKEN-DBG]`：Token 統計／寫入 `token_usage_logs` 流程  
- `[TOKEN-PARSE-DEBUG]`：需在執行環境設定 **`TOKEN_PARSE_DEBUG`** 才會輸出——`1`／`true`／`all` 表示每次 `on_chat_model_end` 都印 **LangChain `output` 結構摘要**（型別、`generations` 形狀、`response_metadata`/`token_usage` 鍵與數字預覽，**不含助理長文**）；`zero` 僅在該輪解析出之 batch 為 0 時印。Docker 可在 `deploy/docker-compose.yml` 的 `backend.environment` 加 `TOKEN_PARSE_DEBUG: "1"`（或在本機 `export`）後重建 backend。  
- `[MSG]`：`messages` 寫入失敗  
- `Agent 執行失敗`：SSE 錯誤訊息或例外堆疊（若發生請一併看完整 traceback）

### 0-5 對照 backend 映像：OpenAI 版本與 `StreamUsageChatOpenAI`（日誌長顯 `batch_p=0` 時）

後端 log 若持續為 `[TOKEN-DBG] on_chat_model_end: batch_p=0 batch_c=0` 且 `[TOKEN] skip record`，先排除**容器仍跑舊映像**（未含新版 `openai` 或 `stream_usage_chat_openai.py`）。**以下均在專案根目錄**執行，Compose 前綴統一為：

```bash
docker-compose -f ./deploy/docker-compose.yml
```

（Compose V2 請改為 `docker compose -f ./deploy/docker-compose.yml`。）

#### 0-5-1 先確認曾「重建映像」，不是只沿用舊 layer

改過 `app/backend` 或 `app/backend/requirements.txt` 後，至少應做一次 **`build` 再 `up`**；`--no-cache` 可避免 `COPY`／`pip install` 仍用舊快取：

```bash
docker-compose -f ./deploy/docker-compose.yml build --no-cache backend
docker-compose -f ./deploy/docker-compose.yml up -d backend
```

若可以接受沿用 **build cache**：全專案背景啟動並觸發建置可用 **`up --build -d`**（見 **§0-0**）；此方式**並非** `build --no-cache`，語法與差異見 **§0-0-A**。

#### 0-5-2 進容器看 OpenAI 套件版本

```bash
docker-compose -f ./deploy/docker-compose.yml exec backend pip show openai
```

看 **Version**：

- 需支援串流 `stream_options` 的版本大致 **≥ 1.26**（若 `requirements.txt` 為 **1.68.2**，這裡應顯示對應版本）。
- 若仍為 **1.14.3** 等舊版：代表映像裡 `pip install` 層尚未吃到新 `requirements.txt`，或映像**未重建**。

#### 0-5-3 進容器檢查檔案是否存在、`chat.py` 是否接上 `StreamUsageChatOpenAI`

後端 Dockerfile 將程式放於容器內 **`/src/app/backend`**（`PYTHONPATH=/src`）：

```bash
docker-compose -f ./deploy/docker-compose.yml exec backend \
  test -f /src/app/backend/agent/stream_usage_chat_openai.py && echo "stream_usage_chat_openai: OK"

docker-compose -f ./deploy/docker-compose.yml exec backend \
  grep -n "StreamUsageChatOpenAI" /src/app/backend/agent/chat.py
```

第二條 **若無任何輸出**：容器內的 `chat.py` **尚未** import／使用 `StreamUsageChatOpenAI`。

#### 0-5-4 看目前正在跑的容器／映像建立時間（粗判是否舊 build）

```bash
docker-compose -f ./deploy/docker-compose.yml ps backend
docker inspect "$(docker-compose -f ./deploy/docker-compose.yml ps -q backend)" \
  --format '{{.Image}} {{.Created}}'
```

或瀏覽本機映像列表（前幾筆）：

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | head
```

若 **Created** 很早、但你才剛改程式卻沒有 **§0-5-1** 的 rebuild，很可能仍是舊映像在跑。

#### 0-5-5（可選）與本機檔案 SHA256 對照

容器內通常有 `sha256sum`；macOS 本機可用 `shasum -a 256`。**在專案根目錄**：

```bash
docker-compose -f ./deploy/docker-compose.yml exec backend \
  sha256sum /src/app/backend/agent/stream_usage_chat_openai.py
shasum -a 256 app/backend/agent/stream_usage_chat_openai.py
```

兩邊雜湊**相同**，且已做過成功 rebuild，通常表示該檔已照本機版本進映像。

#### 0-5-6 檢查項目小結

| 檢查項目 | 預期結果 |
|----------|----------|
| `pip show openai` | 版本 **≥ 1.26**（與 `requirements.txt` 一致） |
| `stream_usage_chat_openai.py` | 容器內檔案存在 |
| `grep StreamUsageChatOpenAI` `chat.py` | 有對應 import／使用 |
| 映像重建 | 強制不用 build cache：**§0-5-1** 或 **§0-0-A**；可接受 cache：**§0-0** 的 `up --build -d` |

若 **openai** 已是新版、**StreamUsageChatOpenAI** 也都在映像內，但 **`[TOKEN-DBG]` 仍全為 0**，則偏向 **LangGraph／`astream_events` 裡 `on_chat_model_end` 的 `output` 形狀**或**實際呼叫的模型／供應鏈**問題，已不是單純「映像過舊」。

---

## 1. 進入資料庫 (PostgreSQL)

本專案使用 Docker Compose 部署，資料庫預設在 `db` container 中執行。以下方式為標準登入做法：

### 1-1 使用 Docker Compose（推薦）

在**專案根目錄**執行：

```bash
docker-compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Stock_Insight_Chat
```

（Compose V2 使用者可將 `docker-compose` 改為 `docker compose`。）

### 1-2 直接使用 Docker Exec
若知道 container 的明確名稱（可透過 `docker ps` 確認，如 `deploy-db-1`），可使用：

```bash
docker exec -it <db_container_name> psql -U postgres -d Stock_Insight_Chat
```

成功登入後，終端機將出現 `Stock_Insight_Chat=#` 提示字元。
*(若要離開請輸入 `\q` 並按下 Enter 即可)*

### 1-3 變更資料庫結構／新增或刪除索引（`token_usage_logs`）

在已進入 **`Stock_Insight_Chat=#`** 後，若既有資料庫仍為舊版單欄索引（`idx_token_usage_logs_user_id`、`idx_token_usage_logs_chat_id`），可手動執行下列交易：**新增複合索引**並**移除已冗餘的單欄索引**。  
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

### 1-4 變更資料庫結構／新增 `caller` 欄位（`token_usage_logs`）

若資料庫為較早版本建立的 **`token_usage_logs`**（尚未含 **`caller`**），在 **`Stock_Insight_Chat=#`** 下可手動執行下列交易，與 **`init_db.sql`** 建表時第 45 行之 **`caller VARCHAR(50)`**（可 NULL，語意：router／analyst 等 LLM 輪次來源）對齊：  
（與程式庫中 migration **`app/backend/database/migrations/V004__token_usage_logs_add_caller.sql`** 等價；**以 `init_db.sql` 全新建庫者無須執行**。）

```sql
BEGIN;

ALTER TABLE token_usage_logs
    ADD COLUMN IF NOT EXISTS caller VARCHAR(50);

COMMENT ON COLUMN token_usage_logs.caller IS 'LLM 輪次來源：router、analyst 等（可 NULL）';

COMMIT;
```

---


以下查詢皆需要在進入 PostgreSQL (`Stock_Insight_Chat=#`) 介面後執行。

### 2-1 登入驗證 (JWT Refresh Tokens)


```sql
SELECT * FROM refresh_tokens ORDER BY created_at DESC LIMIT 5;
```


### 2-2 Token 用量流水帳 (`token_usage_logs`)

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

* **`token_usage_logs` 為 0 筆**：代表尚未成功 `INSERT`，不一定是 schema 錯誤；請搭配應用程式 log（例如含 `[TOKEN]`、`record_token_usage failed`）、**§0-5**（映像／`openai`／`StreamUsageChatOpenAI`）與端到端再打一次對話除錯。
* **日常不必**對開發庫做離線毀損修復；若僅懷疑統計過舊，可在維護視窗對單表執行 `ANALYZE tablename;`。僅在出現明確 catalog／儲存錯誤訊息時，再依 PostgreSQL 官方文件評估 `VACUUM`、`REINDEX` 等（須評估鎖與時間）。

---

*(維運人員可隨時依需求將常用的 SQL 查詢或除錯語法補充至本文件中。後端 Docker 日誌見 **§0**，映像與 Token／`StreamUsageChatOpenAI` 排查見 **§0-5**；資料庫表結構與索引檢查以 **§3** 為準。)*
