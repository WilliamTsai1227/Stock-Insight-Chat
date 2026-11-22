# 通用行事曆 Lambda（calendar-macro）

EventBridge 排程觸發，抓美國／台灣總經數據公布日、FOMC、巨頭財報日，寫入 MongoDB `market_events`。
backend 的 `GET /api/calendar/events` 從這裡讀。完整設計見
[specifications/market_calendar_spec.md](../../specifications/market_calendar_spec.md)。

## 檔案

| 檔案 | 用途 |
|---|---|
| `lambda_function.py` | handler：抓取 → 合併 → upsert、改期紀錄、執行狀態寫入 `calendar_meta` |
| `sources.py` | 各資料來源的抓取與解析（解析是純函式，測試在 `test/data/test_calendar_macro.py`） |
| `requirements.txt` / `Dockerfile` | container image（沿用 `data/` 的部署方式） |

## 資料來源

| 來源 | 內容 | 可看多遠 |
|---|---|---|
| Fed 官網 FOMC 日程 | FOMC 利率決策（官方） | 一年以上 |
| BEA 發布日程 ICS | 美國 GDP、個人所得與支出（PCE）（官方） | 數個月 |
| Nasdaq 經濟日曆 | CPI、非農、PPI、零售、ISM…的預期值／前值／實際值 | 約 3 週 |
| Nasdaq 財報日曆 | 巨頭財報日、盤前／盤後、EPS 預估；台積電用 ADR（TSM） | 約 7 週 |
| 主計總處「近期統計資料發布」 | 台灣 CPI、GDP、進出口、外銷訂單、景氣燈號、失業率 | 逐日查詢 60 天 |

> **授權**：Nasdaq 的 `api.nasdaq.com` 是網站前端用的非公開 API，沒有正式授權條款。自用沒問題；
> **開放給大眾前**要改用有授權的來源（例如 Finnhub / FMP 付費方案），只需要換掉 `sources.py` 的對應 adapter。
> BLS（CPI、非農的官方日程）會擋自動化請求（實測 403），所以 CPI／非農目前只靠 Nasdaq，大約只看得到 3 週後。

Nasdaq 經濟日曆有兩個怪癖（日期晚一天、時間固定 UTC-5），已在 `sources.py` 開頭說明並修正；
每次執行會拿 Fed 官網的 FOMC 日期比對，對不上時 `calendar_meta.warnings` 會出現警告。

## 寫進 MongoDB 的東西

- `market_events`：一個事件一份文件，`dedupe_key` 唯一。欄位見規格 §1.1。
  - 同一期改期時 key 不變，只更新 `event_date` 並在 `history` 加一筆（最多 10 筆）。
  - 已公布（`RELEASED`）不會退回未公布；`source = "manual"` 的人工補登文件不會被覆寫或刪除。
  - `event_date` 早於 400 天的事件會被刪除。
- `calendar_meta`（`_id: "macro"`）：`last_run_at`、`last_success_at`、各來源成功與否、警告。

索引在每次執行時以 `create_index` 建立（已存在就不動）。

## 部署（container image）

```bash
AWS_REGION=ap-northeast-1
AWS_ACCOUNT=<你的 account id>
REPO=stock-insight-calendar-macro
ECR=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

# 1. ECR repo（一次性）
aws ecr create-repository --repository-name $REPO --region $AWS_REGION

# 2. build & push（在本資料夾內）
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR
docker build --platform linux/amd64 -t $REPO .
docker tag $REPO:latest $ECR/$REPO:latest
docker push $ECR/$REPO:latest

# 3. 建立 Lambda（一次性；不放 VPC，直連 MongoDB Atlas 與外部網站）
aws lambda create-function \
  --function-name stock-insight-calendar-macro \
  --package-type Image \
  --code ImageUri=$ECR/$REPO:latest \
  --role arn:aws:iam::$AWS_ACCOUNT:role/<lambda-execution-role> \
  --timeout 300 \
  --memory-size 256 \
  --region $AWS_REGION

aws lambda update-function-configuration \
  --function-name stock-insight-calendar-macro \
  --environment "Variables={MONGO_URI=<mongodb 連線字串>,MONGO_DB=stock_insight}" \
  --region $AWS_REGION
```

### 排程（EventBridge Scheduler，台北時間）

| 排程 | 時間 | payload | 用途 |
|---|---|---|---|
| 每日完整更新 | 每天 06:00 | `{}` | 全部來源；也補上凌晨公布的 FOMC 決策 |
| 美股數據補實際值 | 週一至週五 22:00、23:30 | `{"sources": ["nasdaq_econ"], "days_back": 2}` | 美東 08:30／10:00 公布的數據 |

```bash
FN=arn:aws:lambda:$AWS_REGION:$AWS_ACCOUNT:function:stock-insight-calendar-macro
ROLE=arn:aws:iam::$AWS_ACCOUNT:role/<scheduler-invoke-role>

aws scheduler create-schedule --name calendar-macro-daily \
  --schedule-expression "cron(0 6 * * ? *)" --schedule-expression-timezone "Asia/Taipei" \
  --flexible-time-window Mode=OFF \
  --target "{\"Arn\":\"$FN\",\"RoleArn\":\"$ROLE\",\"Input\":\"{}\"}" --region $AWS_REGION

aws scheduler create-schedule --name calendar-macro-us-actuals \
  --schedule-expression "cron(0,30 22,23 ? * MON-FRI *)" --schedule-expression-timezone "Asia/Taipei" \
  --flexible-time-window Mode=OFF \
  --target "{\"Arn\":\"$FN\",\"RoleArn\":\"$ROLE\",\"Input\":\"{\\\"sources\\\":[\\\"nasdaq_econ\\\"],\\\"days_back\\\":2}\"}" \
  --region $AWS_REGION
```

**第一次部署後**手動跑一次 `{"days_back": 30}`，讓行事曆一開始就有過去一個月已公布的數據（之後每天只往回抓 7 天）：

```bash
aws lambda invoke --function-name stock-insight-calendar-macro \
  --payload '{"days_back": 30}' --cli-binary-format raw-in-base64-out /dev/stdout
```

告警：對這個 Lambda 的 `Errors` 指標建 CloudWatch Alarm（全部來源失敗時 Lambda 才會失敗）。
部分來源失敗不會讓 Lambda 失敗，要看 `calendar_meta.sources` 或 CloudWatch Logs 的 `[CALENDAR]` 行。

## 測試

```bash
# 只抓取與合併、不寫入（本機，不需要 MONGO_URI）
cd data/calendar_macro
pip install -r requirements.txt
python lambda_function.py '{"dry_run": true}'

# 正式寫入
MONGO_URI=... python lambda_function.py '{}'

# 部署後
aws lambda invoke --function-name stock-insight-calendar-macro \
  --payload '{"dry_run": true}' --cli-binary-format raw-in-base64-out /dev/stdout
```

Event 參數（都可省略）：`dry_run`、`days_back`（0–30，預設 7）、`days_ahead`（7–120，預設 60）、
`sources`（`fed` / `bea` / `nasdaq_econ` / `nasdaq_earnings` / `dgbas` 的子集合）。
