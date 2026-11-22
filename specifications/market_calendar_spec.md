# 投資行事曆與每日簡報（Market Calendar & Daily Brief）

> 狀態：**P1 已實作**（Lambda A、`market_events`、`GET /api/calendar/events`、行事曆頁）；P2–P4 規劃中。
> 實作與原設計不同的地方已直接改在下文，並標示「（P1 實作）」。

| 程式 | 做什麼 |
| :--- | :--- |
| [`data/calendar_macro/sources.py`](../data/calendar_macro/sources.py) | 各資料來源的抓取與解析（解析是純函式） |
| [`data/calendar_macro/lambda_function.py`](../data/calendar_macro/lambda_function.py) | Lambda A：合併、upsert、改期紀錄、`calendar_meta` |
| [`data/calendar_macro/README.md`](../data/calendar_macro/README.md) | 部署、排程、測試方式 |
| [`api/calendar.py`](../app/backend/api/calendar.py) | `GET /api/calendar/events` |
| [`frontend/js/calendar.js`](../app/frontend/js/calendar.js)、[`css/calendar.css`](../app/frontend/css/calendar.css) | 行事曆頁 |
| `test/data/test_calendar_macro.py`、`test/backend/test_calendar.py` | 單元測試 |

打開「行事曆」就能看到**現在要注意的時間點**：總經數據公布、巨頭財報，以及**自己庫存股與觀察清單**
的財報日、法說會、除權息與相關新聞。每天早上由「每日簡報」整理成幾句話提醒使用者，隨時可以在
懸浮 AI 助理裡追問。

## 0. 設計原則

**資料蒐集與個人化分開。**

| 層 | 負責 | 放哪裡 | 跟使用者有關？ |
| :--- | :--- | :--- | :--- |
| 蒐集 | 世界上**發生什麼**（事件、日期、預期值、實際值） | AWS Lambda（EventBridge 排程）→ MongoDB | 無關 |
| 個人化 | 這些事**對你代表什麼**（篩選、分頁、每日簡報、AI 問答） | backend（FastAPI）+ PostgreSQL | 有關 |

- 爬資料的失敗（逾時、來源改版）不影響聊天 API；Lambda 失敗由 CloudWatch 告警、重跑即可。
- **按股票跑，不按使用者跑**：個股事件只抓「所有使用者持股＋觀察清單的聯集」，每檔一次。
  成本跟「不重複股票數」成正比，跟使用者數無關 → 自用與公開版是同一套架構。
- **不存每個人的行事曆**。個人行事曆在查詢時組合：PostgreSQL 取「我的 symbols」＋ MongoDB 取「這些 symbol 的事件」。
  股票從觀察清單移到庫存時，事件自動換分頁。

```txt
┌─ Lambda A：calendar-macro（每天）─────────────────────────┐
│  美國：非農、CPI、PCE、FOMC、GDP…                          │
│  台灣：CPI、GDP、出口、央行理監事會…                        │
│  巨頭：GOOGL META NVDA AMD AVGO INTC TSM / 2330            │──┐
└──────────────────────────────────────────────────────────┘  │
┌─ Lambda B：calendar-symbols（每天）───────────────────────┐  ├─▶ MongoDB market_events
│  讀 MongoDB tracked_symbols（active=true）                 │──┘   （以 dedupe_key 冪等 upsert）
│  每檔一次：財報日、法說會、除權息、股東會                    │
└──────────────────────────────────────────────────────────┘
                 ▲ tracked_symbols
                 │（新增清單時 upsert；每日對帳）
backend ─────────┘
  GET /api/calendar/events ── PostgreSQL portfolio_items（我的 symbols + list_type）
                           └─ MongoDB market_events（事件）
  BriefScheduler ── 每天產生 assistant_briefs（LLM，計入 token 額度）
  懸浮 AI 助理 ── page_context 帶入行事曆事件
```

---

## 1. MongoDB（DB：`stock_insight`）

### 1.1 `market_events`

一個事件一份文件。

```js
{
  _id: ObjectId,
  dedupe_key: "EARNINGS:US:NVDA:2026-Q4",   // 唯一索引；冪等 upsert 的依據
  scope: "MACRO" | "SYMBOL",                // MACRO：總經；SYMBOL：個股（含巨頭）
  type: "ECON_DATA" | "CENTRAL_BANK" | "EARNINGS" | "INVESTOR_CONF" | "DIVIDEND" | "SHAREHOLDER_MTG" | "OTHER",
  country: "US" | "TW",                     // MACRO 用
  market: "US" | "TW" | null,               // SYMBOL 用，對齊 portfolio_items.market
  symbol: "NVDA" | null,                    // SYMBOL 用，對齊 portfolio_items.symbol
  name: "NVIDIA",
  title: "NVIDIA FY2027 Q3 財報",
  importance: 1 | 2 | 3,                    // 3 = 高（非農、CPI、FOMC、巨頭財報）
  event_date: "2026-11-25",                 // 台北日期（前端以此分日）
  event_at: ISODate | null,                 // 有確切時間才填（UTC）
  time_hint: "BMO" | "AMC" | "TBD" | null,  // 盤前 / 盤後 / 未定（財報常見）
  status: "ESTIMATED" | "CONFIRMED" | "RELEASED" | "CANCELLED",
  period: "2026-Q3" | "2026-10" | null,     // 數據或財報所屬期間
  values: {                                 // ECON_DATA / EARNINGS 用，未公布時為 null
    unit: "%",
    previous: 2.9, forecast: 2.8, actual: null,
    eps_estimate: null, eps_actual: null, revenue_estimate: null, revenue_actual: null
  },
  big_tech: true,                           // Lambda A 的巨頭清單 → 進「通用」分頁
  source: "finnhub" | "twse_mops" | "dgbas" | "manual" | ...,
  source_url: "https://...",
  fetched_at: ISODate,
  updated_at: ISODate,
  history: [ { at: ISODate, field: "event_date", from: "2026-11-20", to: "2026-11-25" } ]  // 改期紀錄，最多保留 10 筆
}
```

索引：

| 索引 | 用途 |
| :--- | :--- |
| `{ dedupe_key: 1 }` unique | 冪等 upsert |
| `{ scope: 1, event_date: 1 }` | 通用分頁的日期區間查詢 |
| `{ market: 1, symbol: 1, event_date: 1 }` | 個股分頁：`$in` 我的 symbols + 日期區間 |

| `{ big_tech: 1, event_date: 1 }` | 通用分頁裡的巨頭事件（P1 實作新增） |

`dedupe_key` 規則：`{type}:{country|market}:{symbol|指標代碼}:{period}`。同一期財報改期時 **key 不變**，
只更新 `event_date` 並寫入 `history` → 前端可以標示「已改期」。

（P1 實作）實際的 period：
- **財報**：Nasdaq 的財季（`Oct/2026` → `2026-10`），例：`EARNINGS:US:NVDA:2026-10`。
- **總經數據與央行**：用**發布月份**（各指標一個月最多發布一次），例：`ECON_DATA:US:US_CPI:2026-12`、
  `CENTRAL_BANK:US:FOMC:2026-12`。這樣官方日程（Fed、BEA）與 Nasdaq 對同一場發布會產生同一個 key，可以合併。
- 合併規則：官方來源（Fed、BEA、主計總處）決定日期與標題，Nasdaq 補預期值／前值／實際值與時間；
  `sources` 欄位記錄有哪些來源提供這筆事件。

（P1 實作）額外欄位：`market_date`（事件所在市場的當地日期；美股盤後財報在台北是隔天，`event_date` 會 +1）、
`indicator`（指標代碼，例 `US_CPI`、`TW_TRADE`）、`detail`（原始發布名稱）、`sources`、`readings`
（`[{label, actual, forecast, previous}]`，取代原設計的 `values`；同一場發布常有多個數字，例如 CPI 月增與年增）、
`eps`（`{estimate, actual}`）。

`readings.label` 的 MoM／YoY 後綴：Nasdaq 的月增率與年增率同名，沒有欄位可分。同名兩列都是百分比、
且數值相差至少 2 倍時，較小的標 `MoM`、較大的標 `YoY`；分不清楚就維持原名。

保留期：`event_date` 早於 400 天的文件由 Lambda A 順手刪除（保留一年做「去年同期」比較）。
`source = "manual"` 的人工補登文件不會被覆寫或刪除。已公布（`RELEASED`）的事件不會因為之後的抓取少了實際值而退回。

### 1.1.1 `calendar_meta`（P1 實作新增）

一份文件 `_id: "macro"`：`last_run_at`、`last_success_at`、`event_count`、`sources`（各來源 `ok`／`count`／`error`／`seconds`）、
`warnings`（例：Nasdaq FOMC 日期對不上 Fed 官網）。backend 用 `last_success_at` 算 `data_freshness`。
P2 的 Lambda B 用 `_id: "symbols"`。

### 1.2 `tracked_symbols`

Lambda B 要追蹤的股票清單。**Lambda 不連 PostgreSQL**（Lambda 不放 VPC，見 `data/README.md` 方案 A），
所以由 backend 把 symbol 聯集同步到這裡。

```js
{
  _id: "TW:2330",              // {market}:{symbol}
  market: "TW", symbol: "2330", name: "台積電",
  active: true,                // 還有任何使用者的清單裡有它
  first_seen_at: ISODate,
  last_synced_at: ISODate,     // backend 最近一次對帳時間
  last_fetched_at: ISODate | null,   // Lambda B 最近一次成功抓取
  last_error: null | "..."
}
```

同步方式：

1. `POST /api/portfolio/items` 成功後，backend upsert `{active: true}`（新股票當天就能在下一輪被抓）。
2. backend 的排程器每天一次對帳：`SELECT DISTINCT market, symbol, name FROM portfolio_items`，
   聯集內的設為 `active: true`，不在聯集內的設為 `active: false`（不刪除，事件資料保留）。
3. 刪除清單項目時**不即時**改 `active`（其他使用者可能還有），交給每日對帳。

---

## 2. Lambda

沿用 `data/` 既有做法：container image、EventBridge 排程、不放 VPC、直連 MongoDB Atlas。

| | Lambda A：`calendar-macro` | Lambda B：`calendar-symbols` |
| :--- | :--- | :--- |
| 排程 | 每天 06:00 台北（完整） | 每天 06:30 台北（台股） / 每天 20:00 台北（美股盤前） |
| 範圍 | 往回 7 天 ＋ 往後 60 天；官方日程（Fed、BEA）保留到一年後 | `tracked_symbols.active = true` 的每一檔，未來 120 天 |
| 補值 | 週一至週五 22:00、23:30 台北只跑 `nasdaq_econ`，補美東 08:30／10:00 公布的實際值（P1 實作） | 財報公布日隔天補 EPS / 營收實際值 |
| 逾時 | 5 分鐘（實測一次完整執行約 25 秒、約 150 個 HTTP 請求） | 15 分鐘；超過 `CALENDAR_SYMBOL_LIMIT` 檔時分批（event 帶 `offset`） |

台灣數據沒有「實際值」可補：主計總處的發布看板只有日程，數值要另外抓（P3 簡報需要時再加）。

使用者原本提議通用事件**每 3 天**跑一次；改為每天，因為財報日常從「預估」變「確認」或臨時改期，而每天跑的成本幾乎為零。

### 2.1 巨頭清單（Lambda A 固定追蹤）

| 市場 | 代碼 |
| :--- | :--- |
| US | GOOGL、META、NVDA、AMD、AVGO、INTC |
| TW | 2330 台積電 |

清單放在環境變數 `CALENDAR_BIG_TECH`（例：`US:GOOGL,US:META,…,TW:2330`），改清單不必重新部署 image。
巨頭事件寫入時 `scope = "SYMBOL"`、`big_tech = true`：同時出現在「通用」分頁，以及持有該股的人的「庫存」分頁。

### 2.2 資料來源（P1 實作，2026-11 實測）

| 來源（`sources` 名稱） | 內容 | 可看多遠 | 授權 |
| :--- | :--- | :--- | :--- |
| Fed 官網 FOMC 日程（`fed`） | FOMC 利率決策、是否含 SEP | 一年以上 | 官方公開 |
| BEA 發布日程 ICS（`bea`） | 美國 GDP（只取全國估算）、個人所得與支出（PCE） | 數個月 | 官方公開 |
| Nasdaq 經濟日曆（`nasdaq_econ`） | CPI、非農、PCE、PPI、零售、ISM、消費者信心的預期值／前值／實際值；FOMC 利率 | 約 3 週 | **非公開 API** |
| Nasdaq 財報日曆（`nasdaq_earnings`） | 巨頭財報日、盤前／盤後、EPS 預估；台積電用 ADR（TSM） | 約 7 週 | **非公開 API** |
| 主計總處近期統計資料發布（`dgbas`） | 台灣 CPI、GDP、進出口、經濟預測、外銷訂單、景氣燈號、失業率、工業生產 | 逐日查 60 天 | 官方公開 |

- **BLS**（CPI、非農的官方日程）擋自動化請求（實測 403），所以 CPI／非農只能靠 Nasdaq，約 3 週前才會出現。
- **Nasdaq 經濟日曆的兩個怪癖**：`?date=D` 回傳 D 前一天的事件；時間欄是固定 UTC-5。
  用 2025 年已知的 FOMC 日期驗證過，程式已修正；每次執行會拿 Fed 官網日期比對，偏移規則變了會寫入 `calendar_meta.warnings`。
- **開放給大眾前**要把兩個 Nasdaq adapter 換成有授權的來源（Finnhub、FMP 付費方案）。
- 台灣央行理監事會、台股法說會（除了台積電）、除權息：P1 沒有，P2 從公開資訊觀測站補。

每個來源是獨立的 adapter，一個來源失敗不影響其他來源；逐日查詢的來源超過一半天數失敗才算該來源失敗；
**全部來源都失敗**才讓 Lambda 失敗（觸發告警）。

### 2.3 環境變數

| 變數 | 說明 |
| :--- | :--- |
| `MONGO_URI` / `MONGO_DB` | 同 `data/lambda_function.py` |
| `CALENDAR_BIG_TECH` | 巨頭清單 |
| `FINNHUB_API_KEY` 等 | 改用有授權的來源時才需要（P1 不需要任何 API key） |
| `CALENDAR_SYMBOL_LIMIT` | Lambda B 單次最多處理幾檔，預設 300 |

---

## 3. PostgreSQL

### 3.1 `assistant_briefs`（新表，migration `V014__assistant_briefs.sql`）

每位使用者每天一份簡報。

```sql
CREATE TABLE IF NOT EXISTS assistant_briefs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    brief_date  DATE NOT NULL,                     -- 台北日期
    status      VARCHAR(10) NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    content     JSONB,                             -- { headline, items: [{event_key, symbol, why, action_hint}], generated_by }
    event_keys  TEXT[] NOT NULL DEFAULT '{}',      -- 這份簡報引用的 market_events.dedupe_key
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    read_at     TIMESTAMPTZ,                       -- 使用者打開過
    CONSTRAINT uq_assistant_briefs_day UNIQUE (user_id, brief_date)
);
```

### 3.2 `user_settings` 增加欄位

| 欄位 | 預設 | 說明 |
| :--- | :--- | :--- |
| `brief_enabled` | `true` | 是否產生每日簡報 |
| `brief_time` | `07:30` | 台北時間 |

---

## 4. 每日簡報（BriefScheduler）

放在 backend，沿用 [`insight/scheduler.py`](../app/backend/insight/scheduler.py) 的模式（狀態存在 DB、`FOR UPDATE SKIP LOCKED`、停機只補跑一次）。

1. 到 `brief_time` → INSERT `assistant_briefs`（`QUEUED`），`uq_assistant_briefs_day` 保證一天只有一份。
2. 取資料：
   - 未來 7 天的 MACRO 事件（`importance >= 2`）＋巨頭事件
   - 我的庫存與觀察清單在未來 14 天的事件
   - 過去 24 小時 `RELEASED` 的事件（`actual` vs `forecast`）
   - 我的 symbols 在過去 24 小時的新聞（MongoDB `news`，已有）
3. **沒有任何事件也沒有新聞 → `SKIPPED`，不呼叫 LLM**（不浪費額度）。
4. 呼叫 LLM 產生 3–6 點提醒，**庫存優先於觀察清單、觀察清單優先於通用**；每一點必須引用一個 `dedupe_key` 或新聞 ID，
   不能憑空寫事件（引用的 key 不在輸入裡 → 丟掉那一點）。
5. token 用量記入 `token_usage_logs`（`caller = 'daily_brief'`），計入使用者額度；額度用完 → `SKIPPED`。

---

## 5. API

| Method | 路徑 | 說明 |
| :--- | :--- | :--- |
| GET | `/api/calendar/events` | 行事曆事件 |
| GET | `/api/calendar/events/{event_key}` | 單一事件詳情（含改期紀錄、相關新聞）— **P2**，P1 的詳情直接用列表資料 |
| GET | `/api/assistant/briefs/today` | 今天的簡報（沒有就回 `204`） |
| GET | `/api/assistant/briefs` | 過去簡報列表（分頁） |
| POST | `/api/assistant/briefs/{brief_id}/read` | 標記已讀 |
| PUT | `/api/assistant/brief-settings` | 開關與時間 |

### `GET /api/calendar/events`

Query：

| 參數 | 必填 | 說明 |
| :--- | :--- | :--- |
| `from` / `to` | 是 | `YYYY-MM-DD`（台北日期），區間最長 93 天 |
| `tab` | 否 | `ALL`（預設）/ `MACRO` / `HOLDING` / `WATCHLIST` |
| `min_importance` | 否 | 1–3，預設 1 |

組合邏輯：

- `MACRO`：`scope = MACRO` 或 `big_tech = true`
- `HOLDING` / `WATCHLIST`：先查 `portfolio_items`（該 `list_type`）取 `(market, symbol)`，再查 `market_events`
- `ALL`：通用條件 OR 我的 symbols
- 清單是空的分頁（例如觀察清單沒有股票）直接回空陣列，不查 Mongo
- 每個事件回傳時附 `tags: ["MACRO", "HOLDING", ...]`（一個巨頭事件可能同時是 MACRO 與 HOLDING）
- 排序：`event_date` → 有確切時間的在前、依 `event_at` → 重要度高的在前；最多 1500 筆
- 需要登入（JWT）；P4 公開版再開放未登入讀通用分頁

Response（P1 實作，沿用專案的 `{status, data}` 外層）：

```json
{
  "status": "success",
  "data": {
    "from": "2026-11-30", "to": "2027-01-03", "tab": "ALL",
    "events": [
      {
        "key": "EARNINGS:US:AVGO:2026-10",
        "scope": "SYMBOL", "type": "EARNINGS", "title": "Broadcom 財報", "detail": "財季截至 Oct/2026",
        "market": "US", "symbol": "AVGO", "name": "Broadcom", "country": null, "indicator": null,
        "importance": 3, "event_date": "2026-12-10", "market_date": "2026-12-09",
        "event_at": null, "time_hint": "AMC", "status": "ESTIMATED", "period": "2026-10",
        "readings": [], "eps": { "estimate": "$3.45", "actual": null },
        "big_tech": true, "source": "nasdaq_earnings", "sources": ["nasdaq_earnings"],
        "source_url": "https://www.nasdaq.com/market-activity/stocks/avgo/earnings",
        "tags": ["MACRO", "WATCHLIST"], "rescheduled": false, "reschedule_from": null
      }
    ],
    "counts": { "holding": 2, "watchlist": 1 },
    "data_freshness": { "macro_fetched_at": "2026-11-22T06:41:27.724000+00:00", "stale": false, "warnings": [] }
  }
}
```

- `event_at` 一律帶時區（`+00:00`）。pymongo 讀回的 datetime 不帶時區，後端會補上 UTC，
  否則前端會把 UTC 當成本地時間，差 8 小時。
- `data_freshness.stale`：`last_success_at` 超過 36 小時（或從未成功）時為 `true`，前端顯示「資料可能過期」。
  P2 會加上 `symbols_fetched_at`。

---

## 6. 前端

新頁面「行事曆」（側欄入口，與「投資組合」並列），檔案 `frontend/js/calendar.js`、`frontend/css/calendar.css`。

| 區塊 | 內容 |
| :--- | :--- |
| 頂部 | 今日簡報卡片（標題一句話＋展開的 3–6 點）；未讀顯示小圓點 |
| 分頁 | **通用重要時機點** / **庫存股** / **觀察清單** / 全部 |
| 檢視 | 月曆（桌機預設）/ 清單（手機預設，依日期分組，今天置頂） |
| 事件卡 | 類型 icon、重要度、`預估` / `已確認` / `已公布` 標籤、改期標示、公布後顯示實際值 vs 預期值（台股慣例漲紅跌綠） |
| 互動 | 點事件 → 側邊詳情（相關新聞、改期紀錄）＋「問 AI」按鈕 |

- 「問 AI」打開既有的懸浮 AI 助理（`frontend/js/ai-assistant.js`），並把事件當作頁面情境帶入
  （新增 `market_ai/common/page_context.py` 的 `load_calendar_event_context`）。— **P3**
- 在行事曆頁開啟 AI 助理時，預設帶入「目前檢視區間內的事件摘要」，可以直接問「這週我要注意什麼？」。— **P3**

（P1 實作）目前的行事曆頁：

- 頂部沒有簡報卡片（P3 才有）；有圖例（美國數據／台灣數據／央行／財報四色）與「時間皆為台北時間 · 資料更新於…」。
- 月曆：桌機右側是選取日的行程；預設選「今天或之後第一個有事件的日子」。手機的格子只顯示色點，點日期後行程出現在下方。
- 清單：只列當月有事件的日子，過去的日子淡化，自動捲到今天或下一個事件。
- 事件卡：台北時間（美股盤後財報顯示「美股盤後」並註明「美東 X/X 收盤後，台北清晨」）、重要度三點、
  `預估`／`已排定`／`已公布` 標籤、庫存／觀察標籤、改期標示、數值表（實際值高於預期 ▲ 紅、低於 ▼ 綠）、EPS、來源連結。
- 「問 AI」（簡化版）：打開懸浮 AI 助理，把事件名稱、時間與數值寫成問題填進輸入框，**不自動送出**，使用者可以改。
- 庫存／觀察清單分頁固定提示「目前只追蹤巨頭的財報日」（P2 前的限制）；清單是空的時提示去「投資組合」新增。
- 分頁、檢視方式、重要度篩選記在 localStorage（讀寫都包 try/catch，被封鎖時用預設值）。

---

## 7. 自用版與公開版

架構相同，只差在權限與額度：

| | 自用 / 付費 | 免費公開 |
| :--- | :--- | :--- |
| 通用行事曆 | ✓ | ✓（不需登入也可看，作為導流頁） |
| 庫存 / 觀察清單分頁 | ✓ | ✓（需登入，受 portfolio 筆數上限） |
| 每日簡報 | 每天 | 每週一次或關閉（依 `subscription_tiers`） |
| AI 追問 | 計入額度 | 計入額度 |

---

## 8. 分階段

| 階段 | 內容 | 完成標準 |
| :--- | :--- | :--- |
| P1 ✅ | Lambda A + `market_events` + `GET /api/calendar/events`（四個分頁都可查）+ 行事曆頁 | 打開頁面能看到非農、CPI、FOMC 與巨頭財報（CPI／非農約 3 週前出現，受資料來源限制） |
| P2 | `tracked_symbols` 同步 + Lambda B + 庫存 / 觀察清單分頁 | 新增一檔觀察清單，隔天出現它的財報日 |
| P3 | `assistant_briefs` + BriefScheduler + 簡報卡片 + AI 助理帶入行事曆情境 | 每天早上有一份只引用真實事件的簡報 |
| P4 | 公開版：未登入可看通用分頁、依方案限制簡報頻率；（可選）Email / Web Push 提醒 | |

## 9. 待決定

- 最終資料來源與付費方案（影響公開版成本）。
- 每日簡報是否也推播（Web Push / Email），還是只在打開網站時顯示。
- 法說會公告的解析準確度：先人工抽查 P2 第一週的結果再決定是否需要 LLM 輔助解析。
