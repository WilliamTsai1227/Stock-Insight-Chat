# AWS 生產環境上線執行方案

> **⚠️ 此為計畫開發 / 上線執行指南**
>
> 本文件為 **Stock-Insight-Chat**（含深度研究 Deep Search）在 AWS 上的完整雲端架設與上線步驟。  
> 相關功能規格見 [`deep_search.md`](./deep_search.md)；本機 Docker 維運見 [`docker_ops_handbook.md`](./docker_ops_handbook.md)。

---

## 目錄

1. [上線目標與原則](#1-上線目標與原則)
2. [服務元件對照](#2-服務元件對照)
3. [推薦架構總覽](#3-推薦架構總覽)
4. [Compute 選型：EC2 vs 替代方案](#4-compute-選型ec2-vs-替代方案)
5. [Qdrant 部署方案](#5-qdrant-部署方案)
6. [網路與安全（VPC / WAF / ALB）](#6-網路與安全vpc--waf--alb)
7. [Route 53 與 HTTPS](#7-route-53-與-https)
8. [RDS PostgreSQL](#8-rds-postgresql)
9. [S3 與 ECR](#9-s3-與-ecr)
10. [Secrets 與 IAM](#10-secrets-與-iam)
11. [應用程式生產設定調整](#11-應用程式生產設定調整)
12. [上線執行步驟（Runbook）](#12-上線執行步驟runbook)
13. [監控、備份與維運](#13-監控備份與維運)
14. [成本估算（月）](#14-成本估算月)
15. [擴展路徑](#15-擴展路徑)
16. [上線前檢查清單](#16-上線前檢查清單)

---

## 1. 上線目標與原則

### 1.1 目標

- 將現有 Docker Compose 四服務（backend / frontend / db / qdrant）遷移至 AWS 生產環境
- 支援 HTTPS、Google SSO、SSE 長連線聊天
- 預留深度研究功能：S3 原始 PDF、背景 ingest worker
- **先上線、後優化**：第一階段以可運維、可備份、可 HTTPS 為優先

### 1.2 設計原則

| 原則 | 說明 |
|------|------|
| **託管優先** | RDS、ALB、WAF 用 AWS 託管；能少碰 EC2 就少碰 |
| **與 dev 接近** | 第一階段盡量沿用 Docker 映像，降低遷移風險 |
| **私有子網** | DB、Qdrant、Backend 不對公網開 port |
| **單一入口** | 使用者只打 `https://app.example.com`，由 ALB 分流 |
| **秘密不入 Git** | API Key、DB 密碼放 Secrets Manager |

### 1.3 建議 Region

**`ap-northeast-1`（東京）** — 距離台灣延遲低、服務完整。  
若主要使用者在中國大陸，可再評估 `ap-southeast-1`（新加坡）。

---

## 2. 服務元件對照

| 本機 Compose 服務 | AWS 生產對應 | 備註 |
|-------------------|--------------|------|
| `frontend` (Nginx 靜態) | **ALB** → Frontend Target Group；或 **S3 + CloudFront** | 見 §4 |
| `backend` (FastAPI) | **ECS Fargate** 或 **EC2** Docker | 含 ingest worker |
| `db` (PostgreSQL 16) | **RDS PostgreSQL 16** | Multi-AZ 建議 |
| `qdrant` | **EC2 + EBS** 或 **Qdrant Cloud** | 見 §5 |
| （新增）研究原始 PDF | **S3** private bucket | 見 [`deep_search.md`](./deep_search.md) §8 |
| MongoDB（外部） | **MongoDB Atlas**（維持現狀） | 已在雲端 |
| OpenAI / Tavily | 外部 API | 出網 via NAT Gateway |

---

## 3. 推薦架構總覽

### 3.1 Phase 1 上線架構（推薦）

```
                         Internet
                             │
                    ┌────────▼────────┐
                    │   Route 53      │
                    │ app.example.com │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   AWS WAF       │
                    │ (ALB 關聯)       │
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │  Application Load Balancer   │
              │  :443 HTTPS (ACM 憑證)        │
              │  :80  → 301 redirect HTTPS    │
              └──┬──────────────────────┬───┘
                 │                      │
        /api/*   │                      │  /*
                 ▼                      ▼
    ┌────────────────────┐   ┌────────────────────┐
    │ ECS Fargate        │   │ ECS Fargate        │
    │ Service: backend   │   │ Service: frontend  │
    │ (FastAPI :8000)    │   │ (Nginx :80)        │
    │ + ingest worker    │   │                    │
    └─────────┬──────────┘   └────────────────────┘
              │
    ┌─────────┼──────────────┬──────────────┐
    │         │              │              │
    ▼         ▼              ▼              ▼
┌───────┐ ┌────────┐   ┌──────────┐   ┌──────────┐
│  RDS  │ │ Qdrant │   │ S3       │   │ Secrets  │
│ Postgres│ │ EC2/   │   │ research │   │ Manager  │
│       │ │ Cloud  │   │ -docs    │   │          │
└───────┘ └────────┘   └──────────┘   └──────────┘
              ▲
              │ 6333 (僅 VPC 內)
         Private Subnet
```

### 3.2 VPC 子網配置

```
VPC 10.0.0.0/16
├── Public Subnet A  (10.0.1.0/24)  ap-northeast-1a  → ALB、NAT Gateway
├── Public Subnet B  (10.0.2.0/24)  ap-northeast-1c  → ALB
├── Private Subnet A (10.0.11.0/24) ap-northeast-1a  → ECS、RDS、Qdrant EC2
└── Private Subnet B (10.0.12.0/24) ap-northeast-1c  → RDS standby、ECS
```

| 元件 | 子網 | 公網 IP |
|------|------|---------|
| ALB | Public | 是（AWS 管理） |
| NAT Gateway | Public | 是 |
| ECS Fargate tasks | Private | 否（出網走 NAT） |
| RDS | Private | 否 |
| Qdrant EC2 | Private | 否 |

---

## 4. Compute 選型：EC2 vs 替代方案

### 4.1 方案比較

| 方案 | 優點 | 缺點 | 適合 |
|------|------|------|------|
| **A. 單台 EC2 + docker-compose** | 與本機最像、遷移最快、成本最低 | 單點故障、手動 scale、你要自己 patch OS | 搶先上線 1–2 週 MVP |
| **B. ECS Fargate（推薦）** | 免管 EC2、可獨立 scale backend/frontend、與 ALB 整合佳 | 需寫 Task Definition、比 A 多 2–3 天設定 | **正式上線首選** |
| **C. EC2 + ECS EC2 Launch Type** | 比 Fargate 便宜一點 | 仍要管 EC2 叢集 | 成本敏感且熟悉 ECS |
| **D. App Runner** | 部署最簡 | 不適合 SSE 長連線微調、難跑 sidecar worker | ❌ 不建議 |
| **E. EKS (Kubernetes)** | 最彈性 | 維運成本最高 | 團隊已有 K8s 經驗時 |

### 4.2 建議決策

```
搶時間（< 1 週）     → 方案 A：1× EC2 跑 compose + RDS + Qdrant EC2
穩定上線（推薦）     → 方案 B：ECS Fargate + RDS + Qdrant EC2 或 Qdrant Cloud
```

**本文件以下 Runbook 以方案 B（ECS Fargate）為主**，並在 §12 附方案 A 快速路徑。

### 4.3 ECS Fargate 服務拆分

| ECS Service | 映像 | CPU / Memory | 數量 | 說明 |
|-------------|------|--------------|------|------|
| `sic-backend` | ECR `backend:latest` | 1 vCPU / 2 GB | 2 | FastAPI + uvicorn；SSE 需 ALB idle timeout ≥ 300s |
| `sic-frontend` | ECR `frontend:latest` | 0.25 vCPU / 512 MB | 2 | Nginx 靜態檔 |
| `sic-worker`（可選） | 同 backend 映像 | 1 vCPU / 2 GB | 1 | ingest worker 獨立 scale；MVP 可與 backend 同容器 |

> **MVP 簡化**：ingest worker 與 backend 同一 container，在 `lifespan` 啟動（見 `deep_search.md` §9）。流量成長後再拆 `sic-worker`。

---

## 5. Qdrant 部署方案

Qdrant **沒有 AWS 原生託管服務**，需自架或使用 Qdrant Cloud。

### 5.1 方案比較

| 方案 | 說明 | 月成本粗估 | 建議 |
|------|------|------------|------|
| **Qdrant Cloud** | 全託管，含 backup、監控 | ~$50–150+ | ✅ **想少維運首選** |
| **EC2 自架** | Docker 跑 `qdrant/qdrant` + EBS | ~$35–60 | ✅ **成本可控、資料在自家 VPC** |
| **ECS Fargate + EFS** | 可行但 I/O 較差 | ~$40+ | ⚠️ 不建議高寫入場景 |
| **與 backend 同 EC2** | compose 一起跑 | $0 增量 | ⚠️ 僅方案 A 過渡用 |

### 5.2 推薦：EC2 自架 Qdrant（方案 B 搭配）

| 項目 | 設定 |
|------|------|
| Instance | `t3.medium`（2 vCPU / 4 GB） |
| AMI | Amazon Linux 2023 或 Ubuntu 22.04 |
| 磁碟 | **gp3 100 GB**（向量會成長，可擴） |
| 位置 | Private Subnet |
| 安裝 | Docker：`qdrant/qdrant:latest` |
| Port | 6333、6334 僅 SG 允許 backend SG 入站 |
| 備份 | EBS Snapshot 每日；或 Qdrant snapshot API → S3 |

**Security Group（Qdrant）：**

```
Inbound: 6333/tcp from sg-backend only
Outbound: all (或限制至 S3 snapshot)
```

**Backend 環境變數：**

```env
QDRANT_HOST=qdrant.internal.example.com   # 或 EC2 private IP
QDRANT_PORT=6333
```

建議用 **Route 53 Private Hosted Zone** 或 **Cloud Map** 註冊 `qdrant.internal` → EC2 private IP，避免 IP 變動。

### 5.3 Qdrant Cloud（替代）

若不想維護 EC2：

1. 在 [cloud.qdrant.io](https://cloud.qdrant.io) 開 cluster（選 Tokyo region）
2. 取得 URL + API Key
3. Backend 改連 cloud endpoint（需確認 qdrant-client 支援 TLS）

```env
QDRANT_HOST=xxx.aws.cloud.qdrant.io
QDRANT_PORT=6333
QDRANT_API_KEY=...
```

---

## 6. 網路與安全（VPC / WAF / ALB）

### 6.1 Security Groups 摘要

| SG 名稱 | Inbound | 說明 |
|---------|---------|------|
| `sg-alb` | 443 from 0.0.0.0/0, 80 from 0.0.0.0/0 | ALB |
| `sg-backend` | 8000 from sg-alb | FastAPI |
| `sg-frontend` | 80 from sg-alb | Nginx |
| `sg-rds` | 5432 from sg-backend | PostgreSQL |
| `sg-qdrant` | 6333 from sg-backend | Qdrant |

### 6.2 Application Load Balancer

| 設定 | 值 | 原因 |
|------|-----|------|
| Scheme | Internet-facing | 公開服務 |
| Listener 443 | HTTPS + ACM 憑證 | 正式 TLS |
| Listener 80 | Redirect → 443 | 強制 HTTPS |
| Idle timeout | **300 秒**（預設 60） | SSE 聊天長連線 |
| Target type | IP（Fargate） | |
| Stickiness | 可選啟用（SSE 同連線） | 若多 backend task |

**Listener Rules：**

| Priority | 條件 | 轉發至 |
|----------|------|--------|
| 1 | Path `/api/*` | Target Group: backend:8000 |
| 2 | Path `/*` | Target Group: frontend:80 |

Health check：

- Backend：`GET /` → 200（或新增 `/health`）
- Frontend：`GET /index.html` → 200

### 6.3 AWS WAF

WAF Web ACL **關聯到 ALB**（不是 CloudFront，除非前端改 CloudFront）。

**Phase 1 建議啟用的 Managed Rules：**

| Rule Group | 用途 |
|------------|------|
| `AWSManagedRulesCommonRuleSet` | SQLi、XSS 等通用防護 |
| `AWSManagedRulesKnownBadInputsRuleSet` | 已知惡意 payload |
| `AWSManagedRulesAmazonIpReputationList` | 惡意 IP |
| `AWSManagedRulesBotControl`（可選） | Bot 管理（有額外費用） |

**自訂規則（建議）：**

```
Rate limit: /api/chat/messages → 100 requests / 5 min / IP
Rate limit: /api/research/*/upload → 20 requests / 5 min / IP
Size restriction: body > 55MB → block（配合 PDF 上傳上限）
```

**Google OAuth 注意：** WAF 不要擋 `/api/user/auth/google/*` 的 redirect；若 Bot Control 誤殺，加 whitelist。

### 6.4 NAT Gateway

Private subnet 的 ECS 需出網（OpenAI、Google OAuth、MongoDB Atlas、Tavily）：

- 每 AZ 1 個 NAT Gateway（高可用）或 MVP 單 AZ 1 個（省成本 ~$32/月）

---

## 7. Route 53 與 HTTPS

### 7.1 DNS 配置

假設網域 `example.com` 已在 Route 53（或將 NS 指向 Route 53）：

| 記錄 | 類型 | 目標 |
|------|------|------|
| `app.example.com` | A (Alias) | ALB dualstack |
| `qdrant.internal.example.com` | A | Qdrant EC2 private IP（Private Hosted Zone） |

### 7.2 ACM 憑證

1. 在 **ap-northeast-1** 申請 ACM 憑證：`app.example.com`
2. DNS validation（Route 53 一鍵建立 CNAME）
3. 綁定到 ALB Listener 443

> Google OAuth `GOOGLE_OAUTH_REDIRECT_URI` 必須改為：  
> `https://app.example.com/api/user/auth/google/callback`

### 7.3 前端 API 位址（重要）

目前 `api-config.js` 預設打 `hostname:8000`。**生產環境必須改為同域不帶 port**：

```javascript
// 生產： https://app.example.com/api/...
function resolveStockInsightApiBase() {
    if (window.STOCK_INSIGHT_API_BASE) {
        return window.STOCK_INSIGHT_API_BASE;
    }
    return `${window.location.origin}/api`;
}
```

部署時在 `index.html` / `login.html` 注入：

```html
<script>window.STOCK_INSIGHT_API_BASE = '/api';</script>
<script src="/js/api-config.js"></script>
```

ALB 將 `/api/*` 轉到 backend 後，前端與 API 同源，Cookie / CORS 最單純。

---

## 8. RDS PostgreSQL

### 8.1 規格建議

| 項目 | MVP 上線 | 成長期 |
|------|----------|--------|
| Engine | PostgreSQL **16** | 同左 |
| Instance | `db.t4g.medium`（2 vCPU / 4 GB） | `db.r6g.large` |
| Storage | gp3 50 GB | 100 GB+ |
| Multi-AZ | ✅ 建議開啟 | ✅ |
| Backup retention | 7 天 | 14–30 天 |
| Encryption | ✅ at rest | ✅ |
| Public access | ❌ 關閉 | ❌ |

### 8.2 連線

Backend 環境變數（Secrets Manager 注入）：

```env
POSTGRES_HOST=sic-prod.xxxx.ap-northeast-1.rds.amazonaws.com
POSTGRES_PORT=5432
POSTGRES_USER=sic_app
POSTGRES_PASSWORD=<from-secrets>
POSTGRES_DB=Insight
DATABASE_URL=postgresql+asyncpg://sic_app:xxx@.../Insight
```

### 8.3 初始化

1. RDS 建立後，從 bastion 或 ECS one-off task 連線
2. 執行 `init_db.sql`（新庫）或 `migrations/V00x__*.sql`（見 [`sql_dev_handbook.md`](./sql_dev_handbook.md)）
3. 確認 `research_workspaces` migration 已套用（見 [`deep_search.md`](./deep_search.md) §18）

### 8.4 Bastion（可選）

若不想開 RDS public，用 **SSM Session Manager** 連一台小 EC2 bastion，或直接用 **ECS Exec** 進 backend task 跑 `psql`。

---

## 9. S3 與 ECR

### 9.1 S3 Buckets

| Bucket | 用途 | 設定 |
|--------|------|------|
| `sic-research-docs-prod` | 深度研究原始 PDF | Private、SSE-S3、版本控制可選 |
| `sic-terraform-state`（可選） | IaC state | 若用 Terraform |
| `sic-alb-logs`（可選） | ALB access log | |

**Research bucket policy 要點：**

- 禁止 public access
- 僅 backend task IAM role 可 `PutObject` / `GetObject` / `DeleteObject`
- CORS 不需要（前端不直連 S3；走 presigned URL）

**Object key 格式**（見 `deep_search.md`）：

```
research/{workspace_id}/{file_id}/{filename}.pdf
```

### 9.2 ECR

| Repository | 映像 |
|--------------|------|
| `stock-insight-chat/backend` | `deploy/backend.Dockerfile` |
| `stock-insight-chat/frontend` | `deploy/frontend.Dockerfile` |

CI/CD 流程（GitHub Actions 範例）：

```
push main → build docker → push ECR → update ECS service → wait stable
```

---

## 10. Secrets 與 IAM

### 10.1 Secrets Manager

Secret 名稱：`sic/prod/app-env`

JSON 內容（範例）：

```json
{
  "OPENAI_API_KEY": "...",
  "TAVILY_API_KEY": "...",
  "SECRET_KEY": "...",
  "GOOGLE_CLIENT_ID": "...",
  "GOOGLE_CLIENT_SECRET": "...",
  "POSTGRES_PASSWORD": "...",
  "MONGO_URI": "...",
  "QDRANT_API_KEY": ""
}
```

ECS Task Definition 用 `secrets` 欄位注入為環境變數。

### 10.2 Backend Task IAM Role

```json
{
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::sic-research-docs-prod/*"
    },
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:ap-northeast-1:*:secret:sic/prod/*"
    }
  ]
}
```

---

## 11. 應用程式生產設定調整

### 11.1 `.env` 生產值

```env
DEBUG=False
COOKIE_SECURE=true
FRONTEND_URL=https://app.example.com
GOOGLE_OAUTH_REDIRECT_URI=https://app.example.com/api/user/auth/google/callback
CORS_ALLOWED_ORIGINS=https://app.example.com
QDRANT_HOST=<qdrant private host>
POSTGRES_HOST=<rds endpoint>
S3_BUCKET_NAME=sic-research-docs-prod
AWS_REGION=ap-northeast-1
# ECS task role 提供 credentials 時可不設 AK/SK
```

### 11.2 ALB / Nginx

- ALB 已處理 HTTPS，Nginx **不必**再掛憑證
- 若需上傳大 PDF，ALB、backend `client_max_body_size`（Nginx）/ FastAPI 限制一致為 **50MB**

### 11.3 SSE 與 WebSocket

- ALB 支援 SSE（HTTP/1.1 chunked）
- 確認 `idle timeout = 300`
- 不要讓 CloudFront 擋在中間（除非特別設定 streaming）

---

## 12. 上線執行步驟（Runbook）

### Phase 0：前置（Day 0–1）

- [ ] 註冊 / 轉移網域至 Route 53
- [ ] 建立 AWS Organization / 生產帳號（與 dev 分離）
- [ ] 建立 VPC、Public/Private Subnet、IGW、NAT Gateway
- [ ] 建立 Security Groups（§6.1）
- [ ] 建立 ECR repositories
- [ ] 建立 S3 bucket `sic-research-docs-prod`
- [ ] 建立 Secrets Manager secret

### Phase 1：資料層（Day 2–3）

- [ ] 建立 RDS PostgreSQL 16（Private subnet）
- [ ] 執行 DB migration / init
- [ ] 部署 Qdrant（EC2 Docker 或 Qdrant Cloud）
- [ ] 執行 `setup_qdrant.py` 建立 `news`、`ai_analysis`、`research_documents` collections
- [ ] 執行 MongoDB → Qdrant 遷移（若需要股市新聞資料）

### Phase 2：應用層（Day 3–5）

- [ ] 修改 `api-config.js` 支援生產同源 `/api`
- [ ] Build & push backend / frontend 映像至 ECR
- [ ] 建立 ECS Cluster（Fargate）
- [ ] 建立 Task Definitions（backend、frontend）
- [ ] 建立 ALB + Target Groups + Listener Rules
- [ ] 建立 ACM 憑證並綁定 ALB
- [ ] Route 53 `app.example.com` → ALB
- [ ] 建立 ECS Services（backend ×2、frontend ×2）
- [ ] 設定 WAF Web ACL 並關聯 ALB

### Phase 3：驗證（Day 5–6）

- [ ] `https://app.example.com` 可開前端
- [ ] Google SSO 登入成功
- [ ] 股市聊天 SSE 正常、斷線後 DB 有寫入
- [ ] Qdrant 檢索正常
- [ ] （Deep Search）PDF 上傳 → S3 → ingest → Qdrant
- [ ] CloudWatch Logs 有 backend log
- [ ] RDS backup 確認

### Phase 4：上線（Day 7）

- [ ] WAF rate limit 調至正式值
- [ ] 監控告警設定（§13）
- [ ] 文件更新：[`env.md`](./env.md)、README 部署章節

---

### 附錄：方案 A 快速上線（單 EC2 + docker-compose）

若 **7 天內** 必須上線且人力有限：

```
Route 53 → WAF → ALB → EC2 (docker-compose 全服務 except db)
                         ├── backend:8000
                         ├── frontend:80
                         └── qdrant:6333 (僅 localhost/docker network)
RDS (PostgreSQL) — 獨立
S3 — research docs
```

**步驟：**

1. 開 `t3.large` EC2（Private subnet + ALB 轉發）
2. 安裝 Docker，clone repo，改 compose：
   - 移除 `db` service，改連 RDS endpoint
   - `qdrant` 保留，volume 掛 EBS
3. ALB 規則同 §6.2
4. 其餘 WAF / Route 53 / ACM 相同

**缺點：** EC2 掛了就全掛；後續務必遷移到 ECS Fargate。

---

## 13. 監控、備份與維運

### 13.1 CloudWatch

| 項目 | 設定 |
|------|------|
| ECS task logs | `/ecs/sic-backend`、`/ecs/sic-frontend` |
| RDS | CPU、FreeStorageSpace、DatabaseConnections 告警 |
| ALB | 5xx count、TargetResponseTime |
| WAF | BlockedRequests（異常尖峰告警） |

**告警範例：**

- RDS CPU > 80% 持續 5 分鐘 → SNS → Email / Slack
- ALB 5xx > 10 / 5 min → SNS
- ECS running task count < desired → SNS

### 13.2 備份

| 資料 | 方式 | 頻率 |
|------|------|------|
| RDS | Automated backup | 每日（retention 7 天） |
| Qdrant | EBS Snapshot 或 Qdrant snapshot → S3 | 每日 |
| S3 research | 版本控制 + 可選 Cross-Region Replication | 持續 |
| Secrets | Secrets Manager 自動 rotation（DB 密碼可開） | 可選 |

### 13.3 部署更新

```
1. git push → CI build image → ECR
2. ecs update-service --force-new-deployment
3. Rolling update（min healthy 100%, max 200%）
4. 驗證 /health + smoke test
```

DB migration：**先跑 migration，再 deploy 新 code**（見 sql_dev_handbook）。

---

## 14. 成本估算（月）

以 **ap-northeast-1**、方案 B、小流量（< 500 DAU）粗估：

| 項目 | 規格 | USD/月 |
|------|------|--------|
| ECS Fargate backend | 2 × (1vCPU/2GB) | ~$60 |
| ECS Fargate frontend | 2 × (0.25vCPU/512MB) | ~$15 |
| RDS PostgreSQL | db.t4g.medium Multi-AZ | ~$85 |
| Qdrant EC2 | t3.medium + 100GB gp3 | ~$45 |
| ALB | 1 個 + LCU | ~$25 |
| NAT Gateway | 1 AZ | ~$35 |
| WAF | Web ACL + 基本 rules | ~$10–30 |
| S3 | 50 GB research + request | ~$2 |
| Route 53 | 1 hosted zone + queries | ~$1 |
| Secrets Manager | 1 secret | ~$0.50 |
| CloudWatch Logs | 5 GB | ~$3 |
| **合計** | | **~$280–300/月** |

**省錢選項：**

- Qdrant 改 Qdrant Cloud 免 EC2（但 cloud 費可能更高）
- NAT 改单 AZ、RDS 關 Multi-AZ（不建議正式環境）
- 方案 A 单 EC2 可壓到 **~$150–200/月**

外部 API（OpenAI、Tavily）依用量另計，通常高於 infra。

---

## 15. 擴展路徑

| 階段 | 觸發條件 | 動作 |
|------|----------|------|
| Scale backend | CPU > 70% 或 SSE 延遲上升 | ECS backend task 2 → 4 |
| 拆 worker | ingest 塞車 | 獨立 `sic-worker` ECS service |
| Qdrant 升級 | 向量 > 500 萬 points | EC2 升 t3.large / 磁碟擴容 |
| 前端 CDN | 全球使用者 | S3 + CloudFront 取代 Nginx ECS |
| 多 Region | 災難復原 | RDS cross-region read replica |
| IaC | 環境 reproducible | Terraform / CDK 管理全套 |

---

## 16. 上線前檢查清單

### 安全

- [ ] 所有 DB / Qdrant 在 Private subnet
- [ ] S3 bucket block public access
- [ ] WAF 已關聯 ALB
- [ ] `COOKIE_SECURE=true`
- [ ] Secrets 不在 image / Git 內
- [ ] IAM role 最小權限

### 功能

- [ ] Google OAuth redirect URI 已更新為生產域名
- [ ] CORS 僅允許 `https://app.example.com`
- [ ] 前端 API 改同源 `/api`（非 :8000）
- [ ] ALB idle timeout ≥ 300s
- [ ] SSE 聊天 smoke test 通過

### 資料

- [ ] RDS migration 完成
- [ ] Qdrant collections 已建立
- [ ] S3 research bucket IAM 測試通過

### 維運

- [ ] CloudWatch 告警已設
- [ ] RDS backup retention 確認
- [ ] 部署 Runbook 團隊可執行
- [ ] Rollback 流程：ECS 回前一 task definition revision

---

## 附錄 A：Terraform / CDK

本 repo 目前無 IaC。若團隊要 infra as code，建議模組：

```
modules/vpc
modules/rds
modules/ecs
modules/alb-waf
modules/qdrant-ec2
modules/s3
```

可另開 `infra/` 目錄，不在本文件範圍內。

---

## 附錄 B：與 deep_search.md 的對應

| deep_search 需求 | AWS 元件 |
|------------------|----------|
| 原始 PDF | S3 `sic-research-docs-prod` |
| Ingest worker | ECS backend task（同 `lifespan` worker） |
| 向量 | Qdrant EC2 或 Qdrant Cloud |
| File metadata | RDS `files` + `research_workspaces` |

---

## 修訂紀錄

| 日期 | 說明 |
|------|------|
| 2026-06-05 | 初版：AWS 上線執行方案（Route 53 / WAF / ALB / ECS Fargate / RDS / Qdrant / S3） |
