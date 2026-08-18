# Docker 維運指令手冊 (Docker Operations Handbook)

本文件集中整理 **Stock-Insight-Chat** 本機 Docker Compose 的常用維運指令：啟停、重啟、重建、看 log、進容器。

| 主題 | 另見 |
|------|------|
| 後端 log 除錯、Token、`build --no-cache` 細節 | [`maintenance_queries_spec.md`](./maintenance_queries_spec.md) §0 |
| 進 PostgreSQL、schema migration | [`sql_dev_handbook.md`](./sql_dev_handbook.md) |
| `.env` 變數說明 | [`env.md`](./env.md) |

---

## 1. 使用前必讀

### 1.1 工作目錄

以下指令預設在**專案根目錄**執行（路徑內含 `deploy/`、`app/`）：

```text
Stock-Insight-Chat/
├── deploy/docker-compose.yml
├── .env
└── app/
```

### 1.2 Compose 前綴（全文通用）

```bash
# Compose V2（建議）
docker compose -f ./deploy/docker-compose.yml <子命令>

# 舊版二進制
docker-compose -f ./deploy/docker-compose.yml <子命令>
```

下文以 **`docker compose`** 為例；若本機只有 `docker-compose`，請自行替換。

若已 **`cd deploy`**，可改為：

```bash
docker compose -f docker-compose.yml <子命令>
```

### 1.3 服務一覽

| 服務名 | 用途 | 本機埠 |
|--------|------|--------|
| **`backend`** | FastAPI + Agent | `8000` |
| **`frontend`** | Nginx + 靜態前端 | `80` |
| **`qdrant`** | 向量資料庫 | `6333` |
| ~~`db`~~ | PostgreSQL 16 | **預設停用** |

> ⚠️ **`db` 服務目前在 [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) 中是整段註解掉的**，本機開發直接連 AWS RDS（由 `.env` 的 `DATABASE_URL` 指定）。因此 `exec db`、`restart db`、`logs db` 這類指令會回 `no such service: db`。
>
> 若你要在本機跑一顆 PostgreSQL 容器，需先把 compose 內 `db:` 與 `volumes:` 的 `db_data:` 兩段取消註解，再 `up -d db`。本手冊凡標示「（需啟用 db 服務）」的段落都以此為前提。

`.env` 由 compose 的 `env_file: ../.env` 注入 **backend**；改 `.env` 後需**重建容器**才會載入新值（見 §3.1）。

---

## 2. 啟動與停止

### 2.1 第一次或全站啟動（背景）

```bash
docker compose -f ./deploy/docker-compose.yml up -d
```

### 2.2 啟動並重建映像（程式或 requirements 有改）

```bash
docker compose -f ./deploy/docker-compose.yml up -d --build
```

只重建並啟動 **backend**（較快）：

```bash
docker compose -f ./deploy/docker-compose.yml up -d --build backend
```

### 2.3 停止所有服務（保留 volume 資料）

```bash
docker compose -f ./deploy/docker-compose.yml stop
```

### 2.4 停止並移除容器（仍保留 volume）

```bash
docker compose -f ./deploy/docker-compose.yml down
```

### 2.5 查看服務狀態

```bash
docker compose -f ./deploy/docker-compose.yml ps
```

---

## 3. 重啟（Restart）

### 3.1 改了 `.env` 之後（最常用）

例如調整 `GENERAL_CHAT_MODEL`、`OPENAI_API_KEY`、`ROUTER_REASONING_EFFORT`、`FLASH_*` 等，**不必重建映像**，但**必須重建容器**：

```bash
docker compose -f ./deploy/docker-compose.yml up -d backend
```

> ⚠️ **不要用 `restart`。** `docker compose restart` 只是把**同一個容器**停掉再啟動，環境變數是容器**建立當下**就固定的，`restart` 不會重讀 `env_file`，改了 `.env` 也不會生效。
>
> `up -d` 會偵測到設定變更並**重新建立**容器（沿用同一個映像，很快），新的 `.env` 才會進去。
>
> 驗證有沒有讀到：
> ```bash
> docker compose -f ./deploy/docker-compose.yml exec backend env | grep <變數名>
> ```

`restart` 仍然有用 —— 當你只是想讓行程重跑一次（例如清掉記憶體狀態、重連外部服務），而**沒有**改任何設定時。

### 3.2 重啟其他單一服務

```bash
docker compose -f ./deploy/docker-compose.yml restart frontend
docker compose -f ./deploy/docker-compose.yml restart qdrant
# docker compose -f ./deploy/docker-compose.yml restart db   # 需啟用 db 服務，見 §1.3
```

### 3.3 重啟全部服務

```bash
docker compose -f ./deploy/docker-compose.yml restart
```

---

## 4. 何時用 `restart` vs `up --build`？

| 你改了什麼 | 建議指令 |
|------------|----------|
| 只改 **`.env`**（模型名、API key、開關） | **`up -d backend`**（重建容器；`restart` 不會重讀 env） |
| 改 **`app/backend/`** Python 程式 | `up -d --build backend` |
| 改 **`app/backend/requirements.txt`** | `build --no-cache backend` 再 `up -d backend`（見 §5） |
| 改 **前端** `app/frontend/` | `up -d --build frontend` |
| 改 **`deploy/docker-compose.yml`** 本身 | `up -d`（會重建受影響的服務） |
| 什麼都沒改，只想讓行程重跑 | `restart <服務>` |
| 改 **`init_db.sql`** 且 DB **已存在** | 不會自動套用；請用 [`sql_dev_handbook.md`](./sql_dev_handbook.md) 手動 migration |

> 生產環境（EC2）同樣適用「改 `.env` 要用 `up -d`」這條規則，見 [`ec2_deploy.md`](./ec2_deploy.md) §8.3。

---

## 5. 強制重建映像（不吃 build cache）

下列寫法是**錯誤**的：

```bash
# ❌ up 不能把 build --no-cache 這樣接在一起
docker compose -f ./deploy/docker-compose.yml up -d build --no-cache
```

**正確：分兩步**

```bash
# 全部服務
docker compose -f ./deploy/docker-compose.yml build --no-cache
docker compose -f ./deploy/docker-compose.yml up -d

# 僅 backend
docker compose -f ./deploy/docker-compose.yml build --no-cache backend
docker compose -f ./deploy/docker-compose.yml up -d backend
```

細節與 OpenAI 套件對照見 [`maintenance_queries_spec.md`](./maintenance_queries_spec.md) §0-0、§0-5。

---

## 6. 查看日誌（Logs）

### 6.1 backend（最常用）

```bash
# 持續追蹤（Ctrl+C 結束）
docker compose -f ./deploy/docker-compose.yml logs -f backend

# 最近 300 行再追蹤
docker compose -f ./deploy/docker-compose.yml logs --tail 300 -f backend

# 含時間戳
docker compose -f ./deploy/docker-compose.yml logs -f --timestamps backend
```

### 6.2 其他服務

```bash
docker compose -f ./deploy/docker-compose.yml logs -f frontend
docker compose -f ./deploy/docker-compose.yml logs -f qdrant
# docker compose -f ./deploy/docker-compose.yml logs -f db   # 需啟用 db 服務，見 §1.3
```

### 6.3 全部服務一起

```bash
docker compose -f ./deploy/docker-compose.yml logs -f
```

### 6.4 已知容器名稱時（可選）

```bash
docker ps --format '{{.Names}}\t{{.Image}}'
docker logs -f --tail 300 <container_name>
```

後端除錯關鍵字（`[TOKEN]`、`Agent 執行失敗` 等）見 [`maintenance_queries_spec.md`](./maintenance_queries_spec.md) §0-4。

---

## 7. 進入容器（exec）

### 7.1 backend（除錯 Python、看套件版本）

```bash
docker compose -f ./deploy/docker-compose.yml exec backend sh
```

容器內程式路徑：`/src/app/backend`（`PYTHONPATH=/src`）。

範例（不進 shell，單次執行）：

```bash
docker compose -f ./deploy/docker-compose.yml exec backend pip show openai
```

### 7.2 PostgreSQL / psql

**目前預設連 AWS RDS**，本機沒有 db 容器可 exec。請用本機的 `psql` 直接連（連線字串取自 `.env` 的 `DATABASE_URL`）：

```bash
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
```

（需啟用 db 服務時）改為進容器：

```bash
docker compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Insight
```

Schema 變更流程見 [`sql_dev_handbook.md`](./sql_dev_handbook.md)。

### 7.3 frontend

```bash
docker compose -f ./deploy/docker-compose.yml exec frontend sh
```

---

## 8. 常見情境速查

### 8.1 改了 `.env` 的模型或 API key

```bash
docker compose -f ./deploy/docker-compose.yml up -d backend        # 重建容器，非 restart
docker compose -f ./deploy/docker-compose.yml exec backend env | grep <變數名>   # 確認讀到
docker compose -f ./deploy/docker-compose.yml logs --tail 50 backend
```

### 8.2 backend 啟動失敗（IndentationError、import 錯誤）

```bash
docker compose -f ./deploy/docker-compose.yml logs --tail 100 backend
# 修程式後
docker compose -f ./deploy/docker-compose.yml up -d --build backend
```

### 8.3 前端畫面沒更新

```bash
docker compose -f ./deploy/docker-compose.yml up -d --build frontend
# 瀏覽器強制重新整理（Ctrl+Shift+R）
```

### 8.4 確認 API 是否活著

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/docs
# 預期 200（Swagger 頁）
```

### 8.5 本機連線位址

| 用途 | URL |
|------|-----|
| 前端 | http://localhost （port 80） |
| 後端 API / Swagger | http://localhost:8000 （`/docs`） |
| PostgreSQL | AWS RDS（見 `.env` 的 `DATABASE_URL`）；啟用 db 服務時為 `localhost:5432` |
| Qdrant | http://localhost:6333 |

---

## 9. 資料 volume（謹慎）

| Volume | 用途 |
|--------|------|
| `deploy_qdrant_storage` 或 `qdrant_storage` | Qdrant 向量資料（實際名稱以 `docker volume ls` 為準） |
| `deploy_db_data` 或 `db_data` | PostgreSQL 資料 —— **僅在啟用 db 服務時存在**；舊的 volume 可能仍佔磁碟，可手動 `docker volume rm` |

**清空本機 DB 並重新跑 `init_db.sql`**（會刪光資料，**需啟用 db 服務**）：

```bash
docker compose -f ./deploy/docker-compose.yml down
docker volume ls | grep db
docker volume rm <實際_volume_名稱>
docker compose -f ./deploy/docker-compose.yml up -d db
```

> 連 RDS 時**沒有**這個捷徑：`init_db.sql` 是冪等的，直接對目標 database 執行即可；要清資料請用 SQL（`TRUNCATE` / `DROP TABLE`），並確認你連的不是正式庫。

詳見 [`sql_dev_handbook.md`](./sql_dev_handbook.md) §5.3。

---

## 10. 相關檔案

| 路徑 | 說明 |
|------|------|
| `deploy/docker-compose.yml` | 服務定義、埠映射、`env_file` |
| `deploy/backend.Dockerfile` | 後端映像建置 |
| `deploy/frontend.Dockerfile` | 前端映像建置 |
| `.env` | 本機機密與模型設定（勿提交 git） |
