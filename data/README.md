# MongoDB → Qdrant 向量同步（AWS Lambda 自動化）

改寫自 [migrate_to_qdrant.py](../app/backend/scripts/migrate_to_qdrant.py) 的 Lambda 版本，
由 EventBridge 排程觸發，定期把 MongoDB 新增的 `news` / `AI_news_analysis` 文件
向量化後寫入 EC2 上的 Qdrant。

## 檔案

| 檔案 | 用途 |
|---|---|
| `lambda_function.py` | Lambda handler（增量同步、冪等 upsert） |
| `requirements.txt` | Python 依賴 |
| `Dockerfile` | Lambda container image（fastembed 太大，zip 部署放不下） |

## 與原腳本的差異

- **增量同步**：預設只抓「最近 `SYNC_SINCE_HOURS` 小時」內新增的文件（用 ObjectId
  時間戳過濾，無需記錄同步狀態）。point id 是確定性 UUID，重跑不會產生重複向量。
- Embedding 重試耗盡會直接讓 Lambda 失敗（方便接 CloudWatch Alarm / SNS 告警）。
- BM25 模型在 build image 時預先下載進映像，冷啟動不需連 HuggingFace。

## 前置：讓 Lambda 連得到 EC2 上的 Qdrant

目前 `deploy/docker-compose.prod.yml` 的 qdrant **沒有對外開 port**（只在 docker
network 內給 backend 用）。Lambda 要寫入，必須擇一：

### 方案 A（建議，最省成本）：開放 6333 + 強制 API key

Lambda **不放 VPC**（可直連 OpenAI 與 MongoDB Atlas，不需 NAT Gateway），
EC2 的 Qdrant 開放對外 port，但用 API key 保護：

1. 修改 EC2 上的 compose，qdrant service 加上：

   ```yaml
   qdrant:
     ports:
       - "6333:6333"
     environment:
       - QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY}   # 寫在 EC2 的 .env
   ```

   > 注意：backend 連 qdrant 也會開始要求 API key，backend 的環境變數需同步加上
   > `QDRANT_API_KEY`（若 backend 的 qdrant client 尚未支援 api_key，需一併補上）。

2. EC2 Security Group 開 inbound TCP 6333（來源 0.0.0.0/0，靠 API key 擋；
   Lambda 不在 VPC 時沒有固定來源 IP，無法用 SG 白名單）。
3. 流量是明文 HTTP，API key 會在網路上傳輸。介意的話用 nginx 反代 6333 加 TLS，
   或改用方案 B。

### 方案 B：Lambda 放進 EC2 同一個 VPC

SG 白名單最乾淨（6333 只對 Lambda 的 SG 開），但 Lambda 進 VPC 後**沒有對外網路**，
連 OpenAI API / MongoDB Atlas 需要 NAT Gateway（約 $32/月起）。已有 NAT 的話選這個。

## 部署步驟（container image）

```bash
AWS_REGION=ap-northeast-1          # 換成你的 region
AWS_ACCOUNT=<你的 account id>
REPO=stock-insight-qdrant-sync
ECR=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

# 1. 建立 ECR repo（一次性）
aws ecr create-repository --repository-name $REPO --region $AWS_REGION

# 2. build & push（在本資料夾內；Lambda 是 x86_64 記得指定 platform）
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR
docker build --platform linux/amd64 -t $REPO .
docker tag $REPO:latest $ECR/$REPO:latest
docker push $ECR/$REPO:latest

# 3. 建立 Lambda（一次性；execution role 只需基本的 AWSLambdaBasicExecutionRole）
aws lambda create-function \
  --function-name stock-insight-qdrant-sync \
  --package-type Image \
  --code ImageUri=$ECR/$REPO:latest \
  --role arn:aws:iam::$AWS_ACCOUNT:role/<lambda-execution-role> \
  --timeout 900 \
  --memory-size 1024 \
  --region $AWS_REGION

# 4. 設定環境變數
aws lambda update-function-configuration \
  --function-name stock-insight-qdrant-sync \
  --environment "Variables={
    MONGO_URI=<mongodb 連線字串>,
    MONGO_DB=stock_insight,
    QDRANT_URL=http://<EC2 IP 或域名>:6333,
    QDRANT_API_KEY=<與 EC2 .env 相同的 key>,
    OPENAI_API_KEY=<openai key>,
    SYNC_SINCE_HOURS=24,
    SYNC_LIMIT=500
  }" --region $AWS_REGION

# 5. EventBridge 排程（例：每 6 小時）
aws scheduler create-schedule \
  --name qdrant-sync-every-6h \
  --schedule-expression "rate(6 hours)" \
  --flexible-time-window Mode=OFF \
  --target "Arn=arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNT:function:stock-insight-qdrant-sync,RoleArn=arn:aws:iam::$AWS_ACCOUNT:role/<scheduler-invoke-role>" \
  --region $AWS_REGION
```

> `SYNC_SINCE_HOURS` 建議設成排程間隔的 2 倍以上（例：每 6 小時跑一次就設 24），
> 有重疊也沒關係——upsert 冪等，只會覆寫相同的 point。

更新程式碼時：重跑步驟 2 的 build/push，然後
`aws lambda update-function-code --function-name stock-insight-qdrant-sync --image-uri $ECR/$REPO:latest`。

## 測試

```bash
# 手動觸發（dry run：只切分預覽，不呼叫 Embedding、不寫入 Qdrant）
aws lambda invoke --function-name stock-insight-qdrant-sync \
  --payload '{"dry_run": true, "since_hours": 48, "limit": 10}' \
  --cli-binary-format raw-in-base64-out /dev/stdout

# 正式小量測試
aws lambda invoke --function-name stock-insight-qdrant-sync \
  --payload '{"since_hours": 48, "limit": 20}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

本地測試（不經 Lambda）：

```bash
cd data
pip install -r requirements.txt
MONGO_URI=... QDRANT_URL=http://localhost:6333 OPENAI_API_KEY=... python lambda_function.py
```

Event 參數（都可省略）：`collection`（`all`/`news`/`ai_analysis`）、`since_hours`
（`0` = 不過濾時間，全量抓到 `limit` 為止）、`limit`、`dry_run`。

## 注意事項

- Qdrant collection 必須先存在（在 EC2 上跑過
  [setup_qdrant.py](../app/backend/scripts/setup_qdrant.py)）；不存在時 Lambda 會直接報錯，
  不會自動建立。
- Lambda timeout 上限 15 分鐘；預設 `SYNC_LIMIT=500` 綽綽有餘，若要做大批量歷史回填，
  請分多次以 `limit` + `since_hours=0` 呼叫，或直接在本機跑原版 migrate 腳本。
- 建議對 Lambda 的 Errors metric 建 CloudWatch Alarm，同步失敗時發 SNS 通知。
