# EC2 部署指南（Docker Hub Pull）

> **適用情境**：單台 EC2 首次建站；**不在 EC2 上 git clone / docker build**，改由本機或 CI build 後 push 至 **Docker Hub**，EC2 只負責 `pull` + `docker compose up`。
>
> 已建好站、只是要發新版 → 見 [`release_handbook.md`](./release_handbook.md)。  
> 完整 AWS 架構參考（VPC、RDS、未來遷移 ECS）見 [`aws_production_deploy.md`](./aws_production_deploy.md)。  
> 本機 Docker 維運見 [`docker_ops_handbook.md`](./docker_ops_handbook.md)。  
> 環境變數詳細說明見 [`env.md`](./env.md)。

---

## 目錄

1. [架構概覽](#1-架構概覽)
2. [本機 vs EC2 差異](#2-本機-vs-ec2-差異)
3. [相關檔案](#3-相關檔案)
4. [Phase 0：本機 Build 並 Push 至 Docker Hub](#4-phase-0本機-build-並-push-至-docker-hub)
5. [Phase 1：EC2 前置準備](#5-phase-1ec2-前置準備)
6. [Phase 2：上傳設定並啟動](#6-phase-2上傳設定並啟動)
7. [Phase 3：前端與 Cloudflare](#7-phase-3前端與-cloudflare)
8. [版本更新與 Rollback](#8-版本更新與-rollback)
9. [日常維運指令](#9-日常維運指令)
10. [故障排除](#10-故障排除)
11. [上線前檢查清單](#11-上線前檢查清單)

---

## 1. 架構概覽

```
                         Internet
                             │
                    ┌────────▼────────┐
                    │   Cloudflare    │  DNS + CDN + HTTPS 終止
                    │  (proxy 橘雲)   │
                    └───┬─────────┬───┘
                        │         │
          靜態檔 ───────┘         └─────── API / SSE
              │                              │
      ┌───────▼────────┐            ┌────────▼─────────┐
      │  AWS S3        │            │  EC2  :8000      │
      │  前端靜態檔     │            │  （backend 容器） │
      └────────────────┘            └────────┬─────────┘
                                             │
                    ┌────────────────────────▼────────────────────────┐
                    │  EC2 t4g.small（docker compose prod）            │
                    │  ┌──────────┐   ┌──────────┐   ┌──────────┐     │
                    │  │ backend  │──▶│ kinetic  │   │  qdrant  │     │
                    │  │  :8000   │   │  :8000   │◀──┤          │     │
                    │  │  對外    │   │ 僅內網   │   │ 僅內網   │     │
                    │  └────┬─────┘   └──────────┘   └──────────┘     │
                    └───────┼─────────────────────────────────────────┘
                            │
              ┌─────────────▼───────────────┐
              │  RDS PostgreSQL 16          │  ← 獨立託管，不在 EC2 compose 內
              │  database: Insight / kinetic │
              └─────────────────────────────┘

本機 / CI ──build──► Docker Hub ──pull──► EC2
```

**沒有 nginx、沒有 ALB。** backend 直接對外開 host port 8000，`/explore/*` 由 backend 內建的反向代理（[`app/backend/api/explore.py`](../app/backend/api/explore.py)）轉發給 kinetic 容器。

**Compose 內服務**（見 [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml)）

| 服務 | 映像來源 | 對外 | mem_limit | 說明 |
|------|----------|------|-----------|------|
| `backend` | Docker Hub `${DOCKERHUB_USER}/insight-chat-backend:${IMAGE_TAG}` | `8000:8000` | 640m | FastAPI + Agent |
| `kinetic` | Docker Hub `${DOCKERHUB_USER}/kinetic-charts:${KINETIC_TAG}` | 否（僅 docker network） | 448m | 探索功能，來自 **Stock-Analysis** 專案 |
| `qdrant` | `qdrant/qdrant:latest` | 否 | 448m | 向量 DB，資料存 named volume |
| ~~`frontend`~~ | — | — | — | **不在 compose**；靜態檔上傳 S3 + Cloudflare |
| ~~`db`~~ | — | — | — | **不包含**；生產環境使用 **AWS RDS** |

> mem_limit 三者合計約 1.5G，是 t4g.small 2G 記憶體扣掉 OS 後的分配。上限是天花板不是保留量，調整前先用 `docker stats` 看實際用量。
>
> **kinetic 必須維持單一容器、單一 Uvicorn worker**：背景掃描器與 Discord 告警跑在行程內，多副本會加倍打 Yahoo 上游並重複發告警（image 內已強制 `--workers 1`）。

---

## 2. 本機 vs EC2 差異

| 項目 | 本機 [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | EC2 [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml) |
|------|----------------------------------|--------------------------------------|
| backend | `build:` 從原始碼建置 | `image:` 從 Docker Hub pull |
| frontend | compose 內的 `frontend` 容器（nginx） | **不在 compose**，S3 + Cloudflare |
| kinetic | 無（本機不跑探索） | 有，`image:` pull |
| 需要 git repo / Dockerfile | ✅ | ❌ |
| `env_file` | `../.env`（專案根目錄） | 同目錄 `.env`（+ kinetic 用 `.env.kinetic`） |
| PostgreSQL | 可本機 container 或 RDS | **RDS** |
| `restart` | 未設定 | `always` |
| `mem_limit` | 僅 qdrant 1g | 三個服務都有（t4g.small 記憶體吃緊） |
| qdrant port | 6333 對外（開發用） | 不 bind 到 host |

**為何要用獨立的 prod compose？**

本機 compose 含 `build:` 區塊，在 EC2 上若無完整原始碼會 build 失敗。prod compose 只指定 `image:`，EC2 只需 Docker Engine 與三個設定檔（compose + `.env` + `.env.kinetic`）。

---

## 3. 相關檔案

| 檔案 | 用途 |
|------|------|
| [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml) | EC2 生產 compose（pull 映像）**— image 名稱以此為準** |
| [`deploy/.env.prod.example`](../deploy/.env.prod.example) | EC2 `.env` 範本（複製後改名，勿 commit） |
| [`deploy/.env.kinetic.example`](../deploy/.env.kinetic.example) | EC2 `.env.kinetic` 範本（kinetic 容器專用） |
| [`deploy/backend.Dockerfile`](../deploy/backend.Dockerfile) | 本機 / CI build 後端 |
| [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | 本機開發 compose（build 映像） |
| [`deploy/frontend.Dockerfile`](../deploy/frontend.Dockerfile) | **僅本機開發用**；生產前端走 S3，不打包 image |
| [`deploy/nginx/`](../deploy/nginx/) | **僅本機開發用**；生產無 nginx |

**EC2 目錄結構**（無需 clone 整個 repo）：

```
/opt/stock-insight/
├── docker-compose.prod.yml   ← 從 repo deploy/ 複製
├── .env                      ← 從 .env.prod.example 複製後填入真實值
└── .env.kinetic              ← 從 .env.kinetic.example 複製後填入真實值
```

> 目錄路徑非強制，本文以 `/opt/stock-insight/` 為例；只要 `.env` / `.env.kinetic` 與 compose 檔同目錄即可（`env_file` 是相對 compose 檔解析的）。

---

## 4. Phase 0：本機 Build 並 Push 至 Docker Hub

在**有完整原始碼的機器**（本機 Mac 或 CI）執行。請在**專案根目錄**（與 `deploy/` 同層）操作。

### 4.1 登入 Docker Hub

```bash
docker login
```

### 4.2 設定映像名稱

```bash
export DOCKERHUB_USER=your-dockerhub-user
export IMAGE_TAG=1.0.0    # 建議用語意化版本或 git commit SHA，避免只用 latest
```

### 4.3 Build & Push（後端）

```bash
docker build --platform linux/arm64 -f deploy/backend.Dockerfile \
  -t ${DOCKERHUB_USER}/insight-chat-backend:${IMAGE_TAG} .
docker push ${DOCKERHUB_USER}/insight-chat-backend:${IMAGE_TAG}
```

> ⚠️ **image 名稱必須是 `insight-chat-backend`**，與 compose 的 `image:` 完全一致。推到別的 repo 名（例如 `stock-insight-backend`）時，EC2 `pull` 會報 `not found`。
>
> ⚠️ **`--platform linux/arm64`**：t4g 系列是 Graviton（ARM）。Apple Silicon 原生產出 arm64，加著無害；Intel Mac / x86 CI 則非加不可，否則 EC2 起不來（`exec format error`）。

**前端不在這裡 build。** 靜態檔上傳 S3，見 [§7](#7-phase-3前端與-cloudflare)。  
**探索（kinetic）在 Stock-Analysis 專案 build**：

```bash
# 於 Stock-Analysis 專案根目錄
docker build --platform linux/arm64 -f deploy/Dockerfile \
  -t ${DOCKERHUB_USER}/kinetic-charts:${KINETIC_TAG} .
docker push ${DOCKERHUB_USER}/kinetic-charts:${KINETIC_TAG}
```

詳見 `Stock-Analysis/spec/insight-chat-deploy.md`。

### 4.4 版本策略建議

- **不要用 `latest` 作為唯一 tag**：rollback 困難，且 EC2 端難以辨識新舊。
- 建議：`1.0.0`、`1.0.1`，或 `abc1234`（git short SHA）。
- EC2 的 `.env` 內 `IMAGE_TAG` / `KINETIC_TAG` 與 push 的 tag 必須一致。

### 4.5 改用 AWS ECR（可選）

若日後改用 ECR，只需把 `image:` 改成 ECR URL，流程相同：

```
123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/insight-chat-backend:1.0.0
```

EC2 需先 `aws ecr get-login-password | docker login ...`。詳見 [`aws_production_deploy.md`](./aws_production_deploy.md) §9。

---

## 5. Phase 1：EC2 前置準備

### 5.1 規格

| 項目 | 目前 / 建議 |
|------|------|
| 實例類型 | `t4g.small`（2 vCPU / 2 GiB，**ARM Graviton**）—— 記憶體吃緊，三容器已設 mem_limit |
| OS | Ubuntu 22.04（Amazon Linux 2023 亦可） |
| 磁碟 | root EBS ≥ 30 GB（Docker 映像 + qdrant volume） |
| Swap | **建議開 1–2 GB**：compose 的 `memswap_limit` 靠 swap 吸收短暫尖峰 |
| 子網 | Public subnet + Security Group 限 Cloudflare 來源 |

> 若之後流量成長、backend 常被 OOM-kill（`docker logs` 見 exit 137），升級到 `t4g.medium`（4 GiB）比調 mem_limit 有效。

### 5.2 安裝 Docker Engine

**Ubuntu 22.04 範例：**

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
# 重新登入 SSH 後生效
```

**Amazon Linux 2023 範例：**

```bash
sudo dnf update -y
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
```

確認 Compose V2：

```bash
docker compose version
```

> EC2 上請使用 **`docker compose`**（空格），而非 legacy 的 `docker-compose`。

### 5.3 Security Group

| 方向 | Port | 來源 | 說明 |
|------|------|------|------|
| Inbound | 8000 | **Cloudflare IP 範圍** | backend API / SSE（Cloudflare 回源） |
| Inbound | 22 | 你的 IP | SSH 維運 |
| Outbound | 443 | 0.0.0.0/0 | Docker Hub pull、OpenAI / Tavily 等外部 API |
| Outbound | 5432 | RDS Security Group | PostgreSQL |

**不要**對 `0.0.0.0/0` 開放 8000（會繞過 Cloudflare 直打 origin）或 6333（qdrant）。Cloudflare IP 清單見 https://www.cloudflare.com/ips/。

### 5.4 RDS

1. 建立 RDS PostgreSQL 16（與 EC2 同 VPC）。
2. 建立**兩個 database**：`Insight`（chat 主體）與 `kinetic`（探索）。
   ```sql
   CREATE DATABASE kinetic;
   ```
   kinetic 的兩張表由應用啟動時自動建立；`Insight` 需執行 schema：
   [`init_db.sql`](../app/backend/database/init_db.sql) 及後續 migration（例如 `V007__user_feedback.sql`）。
3. 將 RDS endpoint 分別寫入 `.env` 的 `DATABASE_URL`（→ `Insight`）與 `.env.kinetic` 的 `DATABASE_URL`（→ `kinetic`）。

> ⚠️ 兩份 env 的 `DATABASE_URL` **必須指向不同 database**，混用會讓 kinetic 在 chat 的 schema 上建表。

詳見 [`aws_production_deploy.md`](./aws_production_deploy.md) §8、[`sql_dev_handbook.md`](./sql_dev_handbook.md)。

### 5.5 Qdrant 初始化

EC2 首次啟動 qdrant 後，需建立 collections（與本機相同）：

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python3 app/backend/scripts/setup_qdrant.py
```

（容器 WORKDIR 為 `/src`，實際路徑 `/src/app/backend/scripts/setup_qdrant.py`；詳見 README Qdrant 初始化章節。）

---

## 6. Phase 2：上傳設定並啟動

### 6.1 準備設定檔

在 EC2 建立部署目錄：

```bash
sudo mkdir -p /opt/stock-insight
sudo chown $USER:$USER /opt/stock-insight
cd /opt/stock-insight
```

從本機 scp 上傳：

```bash
scp deploy/docker-compose.prod.yml ubuntu@<EC2_IP>:/opt/stock-insight/
scp deploy/.env.prod.example      ubuntu@<EC2_IP>:/opt/stock-insight/.env
scp deploy/.env.kinetic.example   ubuntu@<EC2_IP>:/opt/stock-insight/.env.kinetic
```

### 6.2 編輯 `.env`

```env
DOCKERHUB_USER=your-dockerhub-user
IMAGE_TAG=1.0.0
KINETIC_TAG=1.0.0

DATABASE_URL=postgresql://user:password@your-rds.xxxx.ap-northeast-1.rds.amazonaws.com:5432/Insight
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...
SECRET_KEY=<openssl rand -hex 32>

GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=https://api.example.com/api/user/auth/google/callback

FRONTEND_URL=https://app.example.com
CORS_ALLOWED_ORIGINS=https://app.example.com
COOKIE_SECURE=true

# 延遲調校（可選，不設則用程式預設）
ROUTER_REASONING_EFFORT=minimal
ANALYST_REASONING_EFFORT=low
```

> `.env.prod.example` 裡的 `DEBUG=false` 是歷史遺留 —— **程式從未讀取 `DEBUG`**，設或不設都沒有作用，可以直接刪掉。完整變數清單見 [`env.md`](./env.md)。

> ⚠️ **不要在 `.env` 用行內註解**（`KEY=value  # 說明`）。本機 python-dotenv 會幫你剝掉，但 compose 的 `env_file` parser 行為不同，可能把 `# 說明` 當成值的一部分送進容器。註解請獨立一行。

`QDRANT_HOST` 與 `KINETIC_UPSTREAM` 由 compose 的 `environment:` 直接設定，**不需**在 `.env` 重複（除非要覆寫）。

`.env.kinetic` 依 [`deploy/.env.kinetic.example`](../deploy/.env.kinetic.example) 填寫，其 `DATABASE_URL` 指向 `kinetic` database。

完整變數說明見 [`env.md`](./env.md)。

### 6.3 Pull 並啟動

```bash
cd /opt/stock-insight

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

### 6.4 確認狀態

```bash
docker compose -f docker-compose.prod.yml ps            # 三個容器 running
docker compose -f docker-compose.prod.yml logs -f backend
docker stats --no-stream                                 # 確認未貼著 mem_limit

curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/          # backend health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/explore/  # kinetic 代理
```

---

## 7. Phase 3：前端與 Cloudflare

### 7.1 前端上傳 S3

前端是純靜態檔（[`app/frontend/`](../app/frontend/)），不打包 image：

```bash
aws s3 sync app/frontend/ s3://<你的-bucket>/ --delete
```

**必須設定 API base。** 前端與 API 不同源（S3/Cloudflare 的 `app.example.com` vs EC2 的 `api.example.com`），[`api-config.js`](../app/frontend/js/api-config.js) 在 HTTPS 下預設會取同源 `/api`，那對這個架構是錯的。請在載入 `api-config.js` **之前**設定覆寫：

```html
<script>window.STOCK_INSIGHT_API_BASE = 'https://api.example.com/api';</script>
```

### 7.2 Cloudflare 設定

| 主機名 | 指向 | 說明 |
|--------|------|------|
| `app.example.com` | S3 bucket / static hosting | 前端 |
| `api.example.com` | EC2 Elastic IP | 後端，proxy 開啟（橘雲） |

**關鍵：Origin Rule 改寫 port。** Cloudflare proxy 原生不支援 8000，需加規則：

```
If  Hostname equals api.example.com
Then Rewrite → Destination port → 8000
```

> ⚠️ 規則條件要涵蓋**整個 hostname**，勿只 match `/api` 路徑 —— 否則 `/explore/*` 與根路徑 health check 會回 522。

**其他 Cloudflare 設定**

- SSL/TLS 模式：`Full`（origin 為 HTTP:8000 時）
- **關閉 `/api/chat` 這類 SSE 路徑的快取**，並確認 Cloudflare 的 proxy read timeout 足夠（免費方案 100 秒；長回應需改用 `Cache Rules` + 分段輸出，或考慮升級）
- CORS：`CORS_ALLOWED_ORIGINS` 需含前端網域
- Cookie：跨子網域登入需 `COOKIE_SECURE=true`

### 7.3 探索（`/explore/`）

前端的探索 iframe 指向 `https://api.example.com/explore/`，由 backend 的代理轉給 kinetic 容器。kinetic 本身**沒有認證**，登入閘門完全由 backend 的 `/explore` 代理把關 —— 所以 kinetic 絕不可對外開 port。

要完全關閉探索功能：拿掉 compose 中 backend 的 `KINETIC_UPSTREAM` 環境變數即可。

---

## 8. 版本更新與 Rollback

> 日常發版的完整 SOP（含前端、migration、驗證清單）見 [`release_handbook.md`](./release_handbook.md)，本節只列最小步驟。

### 8.1 發布新版本

**本機 / CI：**

```bash
export IMAGE_TAG=1.0.1
# build & push（見 §4.3，記得 --platform linux/arm64）
```

**EC2：**

```bash
cd /opt/stock-insight
# 編輯 .env：IMAGE_TAG=1.0.1
docker compose -f docker-compose.prod.yml pull backend
docker compose -f docker-compose.prod.yml up -d backend
```

Compose 會依新 tag 重建 container；qdrant volume 不受影響。

**驗證新程式碼真的進去了**（改 tag 是為了讓這步有意義）：

```bash
docker compose -f docker-compose.prod.yml exec backend \
  grep -n "<你這次改的關鍵字>" app/backend/<改到的檔案>.py
```

### 8.2 Rollback

```bash
# .env 改回 IMAGE_TAG=1.0.0
docker compose -f docker-compose.prod.yml pull backend
docker compose -f docker-compose.prod.yml up -d backend
```

### 8.3 只改 `.env`（不換 image）

調 `ROUTER_REASONING_EFFORT`、`ANALYST_TARGET_MAX_WORDS` 這類環境變數不需要重 build：

```bash
# 編輯 .env
docker compose -f docker-compose.prod.yml up -d backend   # 重建容器才會重讀 env_file
docker compose -f docker-compose.prod.yml exec backend env | grep <變數名>
```

> `restart` **不會**重讀 `env_file`，必須 `up -d`（重建容器）。

---

## 9. 日常維運指令

以下均在 `/opt/stock-insight` 執行。

| 操作 | 指令 |
|------|------|
| 查看狀態 | `docker compose -f docker-compose.prod.yml ps` |
| 查看 log | `docker compose -f docker-compose.prod.yml logs -f backend` |
| 記憶體用量 | `docker stats --no-stream` |
| 重啟 backend | `docker compose -f docker-compose.prod.yml up -d backend` |
| 停止全部 | `docker compose -f docker-compose.prod.yml stop` |
| 移除 container（保留 volume） | `docker compose -f docker-compose.prod.yml down` |
| 進 backend shell | `docker compose -f docker-compose.prod.yml exec backend sh` |
| 清掉舊 image 釋放磁碟 | `docker image prune -a` |

---

## 10. 故障排除

| 現象 | 可能原因 | 處理 |
|------|----------|------|
| `pull` 報 `not found` | **push 的 repo 名與 compose 不符**（`stock-insight-backend` vs `insight-chat-backend`），或 tag 沒 push 成功 | `docker manifest inspect <帳號>/insight-chat-backend:<tag>` 驗證；必要時 `docker tag` 改名重推 |
| `pull` 失敗 401 | 未 `docker login` 或私有 repo 無權限 | `docker login` |
| **改了程式碼但行為沒變** | 只做了 `down`/`up`，沒重 build+push+pull；程式碼是烤進 image 的 | `exec backend grep <關鍵字> <檔案>` 驗證；重 build 並換新 tag |
| 容器 `exec format error` | build 平台錯（x86 image 跑在 Graviton） | 加 `--platform linux/arm64` 重 build |
| 容器 exit 137、反覆重啟 | 超過 `mem_limit` 被 OOM-kill | `docker stats` 看用量；調高該服務上限（三者總和勿超過 2G）或升級機型 |
| 環境變數值含 `# ...` | `.env` 用了行內註解 | 註解移到獨立一行，`up -d` 重建容器 |
| 改 `.env` 沒生效 | 用了 `restart`（不重讀 env_file） | 改用 `up -d backend` |
| backend 起不來 | `DATABASE_URL` 錯、RDS SG 未允許 EC2 | 查 `logs backend`；確認 RDS 連線 |
| API 回 522 | Cloudflare Origin Rule 沒涵蓋整個 hostname；或 SG 未開 8000 | 檢查 Origin Rule 條件；確認 SG inbound |
| 探索頁空白 / 502 | kinetic 容器未起，或 `KINETIC_UPSTREAM` 未設 | `ps` 看 kinetic；`logs kinetic` |
| Qdrant 檢索失敗 | collections 未初始化 | 執行 setup_qdrant（§5.5） |
| Google SSO 失敗 | redirect URI 與 Google Console 不一致 | 確認 `GOOGLE_OAUTH_REDIRECT_URI` 為正式 HTTPS URL |

---

## 11. 上線前檢查清單

- [ ] 本機已 build 並 push `insight-chat-backend`（**名稱與 compose 一致**、`--platform linux/arm64`、tag 固定非 latest）
- [ ] `kinetic-charts` image 已於 Stock-Analysis 專案 build & push
- [ ] EC2 已安裝 Docker，`docker compose version` 正常，swap 已開
- [ ] `/opt/stock-insight/` 有 `docker-compose.prod.yml`、`.env`、`.env.kinetic`（**未** commit 至 Git，且無行內註解）
- [ ] RDS 有 `Insight` 與 `kinetic` 兩個 database，`Insight` 已建表（`init_db.sql` + migrations）
- [ ] `.env` 中 `SECRET_KEY`、`COOKIE_SECURE=true`、`DATABASE_SSL=require` 已設定
- [ ] Google OAuth redirect URI 已改為正式 HTTPS URL
- [ ] Security Group：8000 僅 Cloudflare 來源；6333 不對公網；kinetic 未 bind host port
- [ ] Cloudflare Origin Rule 已改寫 port → 8000，條件涵蓋整個 hostname
- [ ] 前端已 sync 至 S3，且 `window.STOCK_INSIGHT_API_BASE` 指向 API 網域
- [ ] `docker stats` 三容器記憶體未貼上限
- [ ] Qdrant collections 已初始化
- [ ] 正式站跑一輪：登入 → 發訊息（SSE 有串流）→ 側欄探索打得開

---

## 附錄：與 ECS 方案的關係

本指南為 **方案 A（單 EC2 + compose pull）**，適合快速 MVP。  
EC2 單點故障、需自行 patch OS；穩定後建議遷移至 **ECS Fargate + ECR**，見 [`aws_production_deploy.md`](./aws_production_deploy.md) §4、§12 Phase 2。
