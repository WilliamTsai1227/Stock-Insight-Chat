# EC2 部署指南（Docker Hub Pull）

> **適用情境**：單台 EC2 快速上線；**不在 EC2 上 git clone / docker build**，改由本機或 CI build 後 push 至 **Docker Hub**，EC2 只負責 `pull` + `docker compose up`。
>
> 完整 AWS 架構（VPC、ALB、WAF、RDS、Route 53）見 [`aws_production_deploy.md`](./aws_production_deploy.md)。  
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
7. [Phase 3：ALB 與 HTTPS（建議）](#7-phase-3alb-與-https建議)
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
                    │   Route 53      │  （可選，正式域名）
                    │ app.example.com │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  WAF + ALB      │  HTTPS 終止；/api/* → backend
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │  EC2（docker compose prod）  │
              │  ┌─────────┐  ┌──────────┐  │
              │  │frontend │  │ backend  │  │
              │  │ :80     │  │ :8000    │  │  ← 僅 docker network / ALB 可達
              │  └─────────┘  └────┬─────┘  │
              │                  │         │
              │            ┌─────▼─────┐   │
              │            │  qdrant   │   │  ← 不對公網暴露
              │            └───────────┘   │
              └──────────────┬─────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  RDS PostgreSQL 16          │  ← 獨立託管，不在 EC2 compose 內
              └─────────────────────────────┘

本機 / CI ──build──► Docker Hub ──pull──► EC2
```

**Compose 內服務**

| 服務 | 映像來源 | 說明 |
|------|----------|------|
| `backend` | Docker Hub `${DOCKERHUB_USER}/stock-insight-backend:${IMAGE_TAG}` | FastAPI + Agent |
| `frontend` | Docker Hub `${DOCKERHUB_USER}/stock-insight-frontend:${IMAGE_TAG}` | Nginx 靜態檔 |
| `qdrant` | `qdrant/qdrant:latest` | 向量 DB，資料存 named volume |
| ~~`db`~~ | — | **不包含**；生產環境使用 **AWS RDS** |

---

## 2. 本機 vs EC2 差異

| 項目 | 本機 `deploy/docker-compose.yml` | EC2 `deploy/docker-compose.prod.yml` |
|------|----------------------------------|--------------------------------------|
| backend / frontend | `build:` 從原始碼建置 | `image:` 從 Docker Hub pull |
| 需要 git repo | ✅ | ❌ |
| 需要 Dockerfile | ✅（本機 build 時） | ❌ |
| `env_file` | `../.env`（專案根目錄） | 同目錄 `.env` |
| PostgreSQL | 可本機 container 或 RDS | **RDS** |
| `restart` | 未設定 | `always` |
| backend / qdrant port | 對外開放（開發用） | 預設**不** bind 到 host |

**為何要用獨立的 prod compose？**

本機 compose 含 `build:` 區塊，在 EC2 上若無完整原始碼會 build 失敗。prod compose 只指定 `image:`，EC2 只需 Docker Engine 與兩個設定檔（compose + `.env`）。

---

## 3. 相關檔案

| 檔案 | 用途 |
|------|------|
| [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml) | EC2 生產 compose（pull 映像） |
| [`deploy/.env.prod.example`](../deploy/.env.prod.example) | EC2 `.env` 範本（複製後改名，勿 commit） |
| [`deploy/docker-compose.yml`](../deploy/docker-compose.yml) | 本機開發 compose（build 映像） |
| [`deploy/backend.Dockerfile`](../deploy/backend.Dockerfile) | 本機 / CI build 後端 |
| [`deploy/frontend.Dockerfile`](../deploy/frontend.Dockerfile) | 本機 / CI build 前端 |

**EC2 建議目錄結構**（無需 clone 整個 repo）：

```
/opt/stock-insight/
├── docker-compose.prod.yml   ← 從 repo deploy/ 複製
└── .env                      ← 從 .env.prod.example 複製後填入真實值
```

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

### 4.3 Build & Push

```bash
# 後端
docker build -f deploy/backend.Dockerfile \
  -t ${DOCKERHUB_USER}/stock-insight-backend:${IMAGE_TAG} .
docker push ${DOCKERHUB_USER}/stock-insight-backend:${IMAGE_TAG}

# 前端
docker build -f deploy/frontend.Dockerfile \
  -t ${DOCKERHUB_USER}/stock-insight-frontend:${IMAGE_TAG} .
docker push ${DOCKERHUB_USER}/stock-insight-frontend:${IMAGE_TAG}
```

### 4.4 版本策略建議

- **不要用 `latest` 作為唯一 tag**：rollback 困難。
- 建議：`1.0.0`、`1.0.1`，或 `abc1234`（git short SHA）。
- EC2 的 `.env` 內 `IMAGE_TAG` 與 push 的 tag 必須一致。

### 4.5 改用 AWS ECR（可選）

若日後改用 ECR，只需把 `image:` 改成 ECR URL，流程相同：

```
123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/stock-insight-backend:1.0.0
```

EC2 需先 `aws ecr get-login-password | docker login ...`。詳見 [`aws_production_deploy.md`](./aws_production_deploy.md) §9。

---

## 5. Phase 1：EC2 前置準備

### 5.1 建議規格

| 項目 | 建議 |
|------|------|
| 實例類型 | `t3.large` 或以上（Qdrant mem_limit 3G + backend） |
| OS | Amazon Linux 2023 或 Ubuntu 22.04 |
| 磁碟 | root EBS ≥ 30 GB（Docker 映像 + qdrant volume） |
| 子網 | Private subnet + ALB 轉發（或 Public + Security Group 限流） |

### 5.2 安裝 Docker Engine

**Amazon Linux 2023 範例：**

```bash
sudo dnf update -y
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# 重新登入 SSH 後生效
```

**安裝 Docker Compose V2 外掛**（新版 Docker 通常已內建）：

```bash
docker compose version
```

> EC2 上建議使用 **`docker compose`**（空格），而非 legacy 的 `docker-compose`。  
> 本機若習慣 `docker-compose` 仍可並用，兩者讀同一份 YAML。

### 5.3 Security Group

| 方向 | Port | 來源 | 說明 |
|------|------|------|------|
| Inbound | 80 | ALB Security Group | 前端（HTTP，ALB 終止 HTTPS 時） |
| Inbound | 22 | 你的 IP | SSH 維運 |
| Outbound | 443 | 0.0.0.0/0 | Docker Hub pull、外部 API |
| Outbound | 5432 | RDS Security Group | PostgreSQL |

**不要**對 `0.0.0.0/0` 開放 8000（backend）或 6333（qdrant）。

### 5.4 RDS

1. 建立 RDS PostgreSQL 16（Private subnet）。
2. 執行 schema：[`init_db.sql`](../app/backend/database/init_db.sql) 及後續 migration（例如 `V007__user_feedback.sql`）。
3. 將 RDS endpoint 寫入 EC2 的 `.env` → `DATABASE_URL`。

詳見 [`aws_production_deploy.md`](./aws_production_deploy.md) §8、[`sql_dev_handbook.md`](./sql_dev_handbook.md)。

### 5.5 Qdrant 初始化

EC2 首次啟動 qdrant 後，需建立 collections（與本機相同）：

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python3 app/backend/scripts/setup_qdrant.py
```

（路徑為容器內 `/src/app/backend/scripts/setup_qdrant.py`；詳見 README Qdrant 初始化章節。）

---

## 6. Phase 2：上傳設定並啟動

### 6.1 準備 `.env`

在 EC2 的 `/opt/stock-insight/`：

```bash
sudo mkdir -p /opt/stock-insight
sudo chown ec2-user:ec2-user /opt/stock-insight
cd /opt/stock-insight
```

從本機 scp 上傳（範例）：

```bash
scp deploy/docker-compose.prod.yml ec2-user@<EC2_IP>:/opt/stock-insight/
scp deploy/.env.prod.example ec2-user@<EC2_IP>:/opt/stock-insight/.env
```

SSH 進 EC2 後編輯 `.env`，至少設定：

```env
DOCKERHUB_USER=your-dockerhub-user
IMAGE_TAG=1.0.0

DATABASE_URL=postgresql://user:password@your-rds.xxxx.ap-northeast-1.rds.amazonaws.com:5432/Insight
OPENAI_API_KEY=sk-...
SECRET_KEY=<openssl rand -hex 32>

GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=https://app.example.com/api/user/auth/google/callback

FRONTEND_URL=https://app.example.com
CORS_ALLOWED_ORIGINS=https://app.example.com
COOKIE_SECURE=true
DEBUG=false
```

`QDRANT_HOST` 由 compose 的 `environment` 設為 `qdrant`，**不需**在 `.env` 重複設定（除非覆寫）。

完整變數說明見 [`env.md`](./env.md)。

### 6.2 Pull 並啟動

```bash
cd /opt/stock-insight

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

### 6.3 確認狀態

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f backend
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:80/
```

---

## 7. Phase 3：ALB 與 HTTPS（建議）

前端 [`api-config.js`](../app/frontend/js/api-config.js) 在 **HTTPS** 下會自動使用同源 `/api`：

```
https://app.example.com/api/...  →  ALB 轉發  →  backend:8000
https://app.example.com/         →  ALB 轉發  →  frontend:80
```

**ALB Listener Rules 範例**

| 優先序 | 條件 | Target |
|--------|------|--------|
| 1 | Path `/api/*` | backend Target Group（port 8000） |
| 2 | Default | frontend Target Group（port 80） |

**ALB 設定提醒**

- Idle timeout ≥ **300 秒**（SSE 聊天串流）
- ACM 憑證綁在 ALB；Nginx **不必**再掛 TLS
- 有 ALB 時，`docker-compose.prod.yml` 中 backend 的 `ports: "8000:8000"` **維持註解**即可（ALB 透過 Target Group 連 EC2:8000 時，需在 compose 取消註解 backend ports，或改由 ALB 連 docker bridge——實務上通常 **取消註解 backend 8000，但 SG 只允許 ALB 來源**）

若暫無 ALB、僅 HTTP 直連 EC2 IP 測試：

- 前端會走 `http://<EC2_IP>:8000/api`（見 `api-config.js` HTTP 分支）
- 需取消註解 `backend` 的 `ports: "8000:8000"`

詳細網路架構見 [`aws_production_deploy.md`](./aws_production_deploy.md) §6–§7。

---

## 8. 版本更新與 Rollback

### 8.1 發布新版本

**本機 / CI：**

```bash
export IMAGE_TAG=1.0.1
# build & push（見 §4.3）
```

**EC2：**

```bash
cd /opt/stock-insight
# 編輯 .env：IMAGE_TAG=1.0.1
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Compose 會依新 tag 重建 container；qdrant volume 不受影響。

### 8.2 Rollback

```bash
# .env 改回 IMAGE_TAG=1.0.0
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

### 8.3 只更新單一服務

```bash
docker compose -f docker-compose.prod.yml pull backend
docker compose -f docker-compose.prod.yml up -d backend
```

---

## 9. 日常維運指令

以下均在 `/opt/stock-insight` 執行。Compose V2 使用 `docker compose`；若本機習慣 `docker-compose` 可替換。

| 操作 | 指令 |
|------|------|
| 查看狀態 | `docker compose -f docker-compose.prod.yml ps` |
| 查看 log | `docker compose -f docker-compose.prod.yml logs -f backend` |
| 重啟 backend | `docker compose -f docker-compose.prod.yml restart backend` |
| 停止全部 | `docker compose -f docker-compose.prod.yml stop` |
| 移除 container（保留 volume） | `docker compose -f docker-compose.prod.yml down` |
| 進 backend shell | `docker compose -f docker-compose.prod.yml exec backend sh` |

修改 `.env` 後需重啟 backend 才生效：

```bash
docker compose -f docker-compose.prod.yml up -d backend
```

---

## 10. 故障排除

| 現象 | 可能原因 | 處理 |
|------|----------|------|
| `pull` 失敗 401 | 未 `docker login` 或私有 repo 無權限 | `docker login`；確認 EC2 IAM/帳密 |
| `IMAGE_TAG` 找不到 | tag 未 push 或 `.env` 打錯 | 本機 `docker pull` 驗證；修正 `.env` |
| backend 起不來 | `DATABASE_URL` 錯、RDS SG 未允許 EC2 | 查 `logs backend`；確認 RDS 連線 |
| 前端 200 但 API 失敗 | ALB 規則未設 `/api/*`；或 HTTP 下 8000 未開 | 查 ALB rules；或取消註解 backend port |
| Qdrant 檢索失敗 | collections 未初始化 | 執行 setup_qdrant（§5.5） |
| Google SSO 失敗 | redirect URI 與 Google Console 不一致 | 確認 `GOOGLE_OAUTH_REDIRECT_URI` 為正式 HTTPS URL |

---

## 11. 上線前檢查清單

- [ ] 本機已 build 並 push `backend`、`frontend` 至 Docker Hub（tag 固定，非僅 latest）
- [ ] EC2 已安裝 Docker，`docker compose version` 正常
- [ ] `/opt/stock-insight/` 有 `docker-compose.prod.yml` 與 `.env`（**未** commit 至 Git）
- [ ] RDS 已建表（`init_db.sql` + migrations）
- [ ] `.env` 中 `SECRET_KEY`、`COOKIE_SECURE=true`、`DEBUG=false` 已設定
- [ ] Google OAuth redirect URI 已改為正式域名
- [ ] Security Group：80 僅 ALB；8000/6333 不對公網
- [ ] ALB idle timeout ≥ 300s；HTTPS 憑證已綁定
- [ ] `https://app.example.com` 可開前端；登入與 SSE 聊天正常
- [ ] Qdrant collections 已初始化

---

## 附錄：與 ECS 方案的關係

本指南為 **方案 A（單 EC2 + compose pull）**，適合快速 MVP。  
EC2 單點故障、需自行 patch OS；穩定後建議遷移至 **ECS Fargate + ECR**，見 [`aws_production_deploy.md`](./aws_production_deploy.md) §4、§12 Phase 2。
