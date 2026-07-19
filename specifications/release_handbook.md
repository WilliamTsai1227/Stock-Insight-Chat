# 發版部署手冊（每次前後端更新 SOP）

> **適用情境**：程式碼改完、測完，要把變更部署到正式環境。
> 首次建站見 [`ec2_deploy.md`](./ec2_deploy.md)；容器維運指令見 [`docker_ops_handbook.md`](./docker_ops_handbook.md)；
> 探索（Kinetic Charts）的更新見 `Stock-Analysis/spec/insight-chat-deploy.md`。

## 0. 架構速覽（誰部署在哪）

| 元件 | 部署位置 | 更新方式 |
|------|----------|----------|
| 前端（`app/frontend/`） | S3 + Cloudflare CDN | 上傳靜態檔（不打包 image） |
| 後端（`app/backend/`） | EC2 docker compose：`backend` 容器 | build image → Docker Hub → EC2 pull |
| 探索（Stock-Analysis 專案） | EC2 docker compose：`kinetic` 容器 | 於 Stock-Analysis 專案 build/push，見其手冊 |
| Qdrant | EC2 docker compose：`qdrant` 容器 | 幾乎不動 |
| PostgreSQL | AWS RDS（`Insight` + `kinetic` 兩個 database） | migration 腳本，見 §3.3 |

對外流量：Cloudflare → backend :8000（無 nginx）；`/explore/*` 由 backend 內建代理轉給 kinetic。

## 1. 後端更新流程

### 1.1 本機 build & push（專案根目錄執行）

```bash
docker login    # 第一次或憑證過期時

export DOCKERHUB_USER=<你的dockerhub帳號>
export IMAGE_TAG=latest         

docker build -f deploy/backend.Dockerfile \
  -t ${DOCKERHUB_USER}/insight-chat-backend:${IMAGE_TAG} .
docker push ${DOCKERHUB_USER}/insight-chat-backend:${IMAGE_TAG}
```

- image 名稱以 `deploy/docker-compose.prod.yml` 為準：`insight-chat-backend`。
- 在 Apple Silicon 上 build 給 x86 EC2 用時，`docker build` 要加 `--platform linux/amd64`；
  EC2 若是 Graviton（ARM）則不用。

### 1.2 EC2 套用（SSH 進 EC2 的部署目錄，例 `/opt/stock-insight/`）

```bash
# 1) 更新 .env 的 IMAGE_TAG 成剛 push 的版本
# 手動編輯

# 2) 只拉、只重啟 backend（不動 kinetic / qdrant）
docker compose -f docker-compose.prod.yml pull backend
docker compose -f docker-compose.prod.yml up -d backend
```

### 1.3 驗證

```bash
docker compose -f docker-compose.prod.yml ps                  # backend running
docker compose -f docker-compose.prod.yml logs backend --tail 30   # 無 traceback、pool created
curl -s https://<API網域>/            # health check 回應正常
```

再開正式站實際跑一輪核心流程：登入 → 發一則訊息（SSE 有串流回來）→ 側欄探索打得開。

### 1.4 回滾

```bash
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=<上一版tag>/' .env
docker compose -f docker-compose.prod.yml pull backend
docker compose -f docker-compose.prod.yml up -d backend
```

（這就是「勿只用 latest」的原因：舊 tag 還在 Docker Hub 上，回滾只是改一行。）

## 2. 前端更新流程

前端正式環境**不是 image**（`frontend.Dockerfile` 只給本地 dev 用），是靜態檔上傳 S3。

### 2.1 上傳 S3（本機專案根目錄執行）

```bash
aws s3 sync app/frontend/ s3://<你的前端bucket>/ \
  --exclude ".DS_Store" \
  --delete        # 刪除 bucket 上已不存在於本地的舊檔；首次使用先拿掉 --delete 觀察
```

只改了個別檔案時也可以單傳：

```bash
aws s3 cp app/frontend/js/index.js s3://<你的前端bucket>/js/index.js
```

### 2.2 清 Cloudflare 快取（必做，否則使用者拿到舊檔）

Cloudflare Dashboard → 該網域 → Caching → **Purge Everything**（或針對改動的檔案 Purge by URL）。

### 2.3 驗證

- 無痕視窗開正式站，DevTools → Network 確認 js/css 是新版（比對檔案內容或 Response 大小）。
- 跑一輪登入 → 對話 → 探索。

### 2.4 回滾

前端沒有 tag 機制，回滾 = 用 git 切回上一版重新上傳：

```bash
git stash        # 或 checkout 上一個 commit 的 app/frontend/
aws s3 sync app/frontend/ s3://<你的前端bucket>/ --delete
# Cloudflare 再 purge 一次
```

## 3. 常見情境的完整順序

### 3.1 前後端一起改（例如新 API + 新 UI）

**順序：先後端、後前端**（新後端要相容舊前端，反之通常不行）：

1. §1 後端 build → push → EC2 pull/up → 驗證舊功能正常
2. §2 前端上傳 S3 → purge 快取 → 驗證新功能

### 3.2 只改 `.env`（不改程式）

```bash
# EC2 部署目錄；編輯 .env 後：
docker compose -f docker-compose.prod.yml up -d backend   # 重建容器讓新 env 生效
```

注意 `restart` **不會**重讀 env_file，必須用 `up -d`（詳見 docker_ops_handbook.md §4）。

### 3.3 有 DB migration 時

**順序：先跑 migration、再部署新後端**（新程式碼可能依賴新欄位）：

1. 把 `app/backend/database/migrations/V0XX__*.sql` 依編號在 RDS 的 `Insight` database 執行
2. 驗證：`\d <表名>` 確認欄位/索引存在
3. 再走 §1 後端更新流程

### 3.4 探索（kinetic）更新

chat 這邊完全不用動，流程在 Stock-Analysis 專案：build/push `kinetic-charts` image →
EC2 改 `.env` 的 `KINETIC_TAG` → `pull kinetic` + `up -d kinetic`。
詳見 `Stock-Analysis/spec/insight-chat-deploy.md` §五。

## 4. 發版檢查清單（照抄可用）

```
□ 本地測試通過（pytest / 手動流程）
□ IMAGE_TAG 已遞增（不是 latest、跟上次不同）
□ （有 migration）已先在 RDS 跑完並驗證
□ backend：build → push → EC2 改 tag → pull → up -d
□ backend 驗證：ps / logs / health check / 登入發訊息
□ 前端：s3 sync → Cloudflare purge
□ 前端驗證：無痕視窗確認新版 js/css、核心流程跑一輪
□ 探索頁打得開（backend 更新會重啟代理，順手確認）
□ 記下本次部署的 tag（回滾用）
```

## 5. 疑難排解

| 症狀 | 處理 |
|------|------|
| EC2 pull 拉不到新 image | tag 打錯或沒 push 成功：`docker manifest inspect <帳號>/insight-chat-backend:<tag>` 確認存在 |
| backend 起不來 | `logs backend` 看 traceback；最常見是 `.env` 缺新變數（對照 `.env.prod.example`） |
| image 在 EC2 跑不起來（exec format error） | build 時少了 `--platform linux/amd64`（Apple Silicon → x86 EC2） |
| 前端改動沒生效 | Cloudflare 快取沒清、或瀏覽器快取：purge + 無痕視窗重試 |
| 探索頁 502 | kinetic 容器沒起來，或 backend 環境變數 `KINETIC_UPSTREAM` 遺失 |
| 部署後磁碟變滿 | 舊 image 堆積：`docker image prune -a --filter "until=168h"` |
