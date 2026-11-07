# 投資組合排程分析（Portfolio Insight）

使用者在「投資組合」頁維護**庫存**與**觀察清單**，設定排程後，backend 定期呼叫外部 Agent 平台
**Zentra** 的 stock-suite tools，產生每檔股票的新聞、K 線、技術面、財報分析，以及庫存的整體調整建議。

```
前端「投資組合」頁 ──REST──▶ backend
                             ├─ portfolio_items / insight_schedules（使用者設定）
                             └─ InsightScheduler（lifespan 背景迴圈）
                                  │ 到期 → 建 job → runner
                                  ▼
                             Zentra /mcp（tools/list 驗證 → tools/call _async）
                             Zentra /v1/runs/{id}（輪詢結果）
                                  ▼
                             insight_jobs / insight_results（報告）
```

| 程式 | 做什麼 |
| :--- | :--- |
| [`api/portfolio.py`](../app/backend/api/portfolio.py) | 清單 CRUD、排程設定、手動執行、報告查詢 |
| [`insight/scheduler.py`](../app/backend/insight/scheduler.py) | 排程器：認領到期排程、啟動 job、收屍、保留期清理 |
| [`insight/runner.py`](../app/backend/insight/runner.py) | 一個 job 的完整流程 |
| [`insight/zentra.py`](../app/backend/insight/zentra.py) | Zentra 專用 client（契約驗證、非同步呼叫、核對輸出） |
| [`insight/schedule.py`](../app/backend/insight/schedule.py) | 下一次執行時間的計算（純函式） |
| [`migrations/V011__portfolio_insight.sql`](../app/backend/database/migrations/V011__portfolio_insight.sql) | 四張表 |
| [`frontend/js/portfolio.js`](../app/frontend/js/portfolio.js) | 前端頁面 |

Zentra 那一側的契約（輸入、輸出、錯誤、部署）以 Zentra repo 的 `examples/stock-suite/README.md` 為準。

---

## 1. 每次分析做什麼

| 對象 | 呼叫的 tool | 參數 |
| :--- | :--- | :--- |
| 庫存每一檔 | `stock-news`、`stock-chart`、`stock-technical`、`stock-fundamental` | `use_llm=true`；後三個帶 `cost`、`shares` |
| 觀察清單每一檔 | 同上四個 | `use_llm=true`；**不帶** `cost`、`shares` |
| 庫存整體（有庫存才做） | `portfolio-advisor` | `use_llm=true`，`precomputed` = 上面庫存那幾檔成功的輸出（K 線圖的 base64 已移除），`investor_profile` = 排程設定的風險偏好與現金 |

- 觀察清單**不進** `portfolio-advisor`。
- 個股呼叫同時最多 `INSIGHT_MAX_CONCURRENCY` 個（預設 8）。每完成一個就寫一列 `insight_results`，前端可以邊跑邊看。
- 實測（2026-11-07，本機）：2 檔庫存 + 1 檔觀察清單，12 個個股呼叫加上 1 次整體建議，總共約 1 分 50 秒。

### 「不能 call 錯」的保證

1. **tool 名稱寫死**在 `insight/zentra.py` 的 `TOOLS`，不經過任何 LLM 判斷。
2. **每個 job 開始前**呼叫 `tools/list`，逐一比對五個 tool：
   必須存在，輸入欄位與必填欄位**完全一致**，`additionalProperties: false`，輸出的 `tool` 常數也要對上。
   任何一項不符，整個 job 就 `FAILED`，**一個 tool 都不呼叫**，`error` 會列出差異。
3. **送出前檢查參數**：不在契約內的欄位不會送出去，Zentra 那一側也會再擋一次。
4. **核對輸出**：回來的 `tool` 必須是呼叫的那個；個股 tool 的 `security.market/symbol` 必須是呼叫的那一檔。
   不符就算失敗，不會寫進報告當成那檔的結果。

### job 狀態

| 狀態 | 意思 |
| :--- | :--- |
| `QUEUED` | 已建立，等排程器啟動 |
| `RUNNING` | 執行中，`heartbeat_at` 每完成一個 tool 更新一次 |
| `SUCCEEDED` | 全部成功 |
| `PARTIAL` | 有部分失敗。失敗那幾列有 `error`，以及可以拿去 Zentra Console 追查的 `run_id` |
| `FAILED` | 契約驗證失敗、Zentra 設定錯誤、清單是空的、執行中斷，或全部都失敗 |

`portfolio_status`：`SUCCEEDED` / `FAILED` / `SKIPPED`（沒有庫存）。

---

## 1.5 庫存總覽（甜甜圈圖）

「庫存」分頁上方顯示總現值、今日漲跌、總成本、未實現損益、台股／美股比例，以及各檔的現值比例甜甜圈圖。
資料來自 `GET /api/portfolio/summary`：backend 直接向 Yahoo 抓即時報價，台美股與 USD/TWD 匯率都來自同一個來源，快取 60 秒
（[`insight/quotes.py`](../app/backend/insight/quotes.py)），**跟排程分析無關**，不必等排程跑完就能看到。

- **顏色跟著個股走，不跟著排名走**：依加入順序分配 8 個類別色，市值變動不會換色。
  超過 8 檔時，市值最小的併成「其他」（灰）。配色已用 dataviz 的 `validate_palette.js` 對深淺兩種底色驗證過。
- 圖旁的清單列出名稱、代碼、比例、市值，不只靠顏色辨識（淺色模式有三個顏色對白底的對比低於 3:1）。
- 漲跌依台股慣例**漲紅跌綠**，並且一律帶正負號。
- 抓不到報價的那檔不計入現值與比例，會在圖下方列出來。

---

## 2. 排程

### 規則（一律以台北時間 UTC+8 解讀）

| `frequency` | 何時執行 | 額外欄位 |
| :--- | :--- | :--- |
| `WEEKDAYS` | 週一到週五的 `run_time` | — |
| `DAILY` | 每天的 `run_time` | — |
| `WEEKLY` | 每週 `weekday`（1=週一 … 7=週日）的 `run_time` | `weekday` |
| `MONTHLY` | 每月 `month_day`（1–28）的 `run_time` | `month_day` |

台灣沒有日光節約時間，所以程式用固定的 +08:00 計算，不依賴容器裡的 tzdata。

### 準時

排程器每一輪結束時會查最近的 `next_run_at`，**睡到那一刻**，最長睡 `INSIGHT_TICK_SECONDS`（預設 30 秒）。
使用者改排程或按「立即分析」時會直接叫醒它。實測觸發延遲約 0.2 秒。

### 不重複、不遺漏

狀態全部存在 DB，不放記憶體：

1. **認領到期排程**在同一個交易裡完成：
   `SELECT … WHERE enabled AND next_run_at <= now() FOR UPDATE SKIP LOCKED`
   → 建一個 `SCHEDULED` job（`scheduled_for = next_run_at`）→ 把 `next_run_at` 推到「現在之後」的下一個時間點。
2. `uq_insight_jobs_scheduled(user_id, scheduled_for)` 保證**同一個時間點只會有一個 job**。
   就算多個 backend 實例同時跑，`SKIP LOCKED` 也不會讓它們搶同一列。
3. **停機錯過的時間點**：醒來後**只補跑一次**，然後往後排，不會連跑好幾輪一樣的分析。
4. `uq_insight_jobs_active(user_id)`：同一位使用者同時最多一個 `QUEUED`/`RUNNING` job。
   上一輪還沒跑完時下一個排程到了，這次會跳過並記 log，排程照樣往後推。手動按「立即分析」會回 `409`。
5. **收屍**：`RUNNING` 但 `heartbeat_at` 超過 `INSIGHT_STALE_MINUTES`（預設 30）沒更新的 job 會標成 `FAILED`，通常是因為 backend 在執行中被重啟。

### 排隊（很多人同時到點時）

**排隊在 PostgreSQL，不在 backend 記憶體。** 到點時排程器只做一件事：為每位使用者 INSERT 一列
`insight_jobs`（`status = QUEUED`）。真正開始跑的時候才從 DB 認領：

```sql
UPDATE insight_jobs SET status = 'RUNNING' …
 WHERE id IN (SELECT id FROM insight_jobs WHERE status = 'QUEUED'
               ORDER BY created_at LIMIT <空位數> FOR UPDATE SKIP LOCKED)
```

- **順序**：先建立的先跑（FIFO）。同一時間點到期的排程在同一輪建立，所以先後差距只有幾毫秒。
- **同時跑幾個**：`INSIGHT_MAX_RUNNING_JOBS`（預設 4）。有一個跑完就立刻叫醒排程器，補下一個，不必等下一輪。
- **記憶體**：只有**正在跑**的 job 佔記憶體。每個 job 把 1–4 的輸出留到最後交給 portfolio-advisor，
  一檔大約 30–100 KB（K 線圖已經轉存 DB，不在記憶體），30 檔約 3 MB；4 個 job 同時跑約 12 MB，
  遠低於 backend 的 640 MB 上限。排隊中的 job 不佔任何記憶體。
- **backend 重啟**：排隊中的 job 留在 DB，重啟後照樣會跑。執行到一半的 job 超過 `INSIGHT_STALE_MINUTES`
  沒有心跳就標成 `FAILED`，已經寫入的個股結果會保留，但**不會自動重跑**，要等下一次排程或手動重跑。

**人很多時，「準時」指的是準時開始排隊，不保證準時跑完。** 粗估（2026-11-07 實測：3 檔股票一次約 2 分鐘）：

| 同時到點的人數（每人約 5 檔） | 預設設定下全部跑完 |
| :--- | :--- |
| 4 | 約 3 分鐘 |
| 40 | 約 30 分鐘 |
| 400 | 約 5 小時 |

要撐更多人，依序可以：

1. **調高 `INSIGHT_MAX_RUNNING_JOBS` / `INSIGHT_MAX_CONCURRENCY`**：實際上限是 LLM 供應商的速率限制
   （每檔新聞要 3 次網路搜尋），不是 backend 或 Zentra。
2. **多開 backend 實例**：認領用 `FOR UPDATE SKIP LOCKED`，多個實例同時跑不會搶到同一個 job，也不會重複執行。
   不想讓 API 實例跑分析的話，設 `INSIGHT_SCHEDULER_ENABLED=0`，另外開專門跑排程的實例。
   這種實例收到「立即分析」時照樣接受，只把 job 寫進 DB（`QUEUED`），由跑排程器的實例認領
   （`scheduler.available`）。**至少要有一台跑排程器**，否則 job 會一直停在 `QUEUED`。
   多實例認領的正確性已實測：3 個排程器實例同時對 60 個到期排程認領，建立剛好 60 個 job、
   每個只被執行一次（2026-11-07）。
3. **錯開時間**：前端預設 14:30；使用者多時可以把預設時間分散，避免全部擠在同一分鐘。

### 保留期

每小時清理一次：

- 超過 `INSIGHT_CHART_RETENTION_DAYS`（預設 30）的 K 線圖會清成 `NULL`，報告文字保留。
- 超過 `INSIGHT_RETENTION_DAYS`（預設 180）的 job 整筆刪除。

K 線圖以 BYTEA 存在 `insight_results.chart_png`，每張約 100 KB，不放進 JSONB。

---

## 3. 設定

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `ZENTRA_MCP_URL` | — | **必填**。Zentra 的 MCP 端點，例如 `http://10.0.3.7:8000/mcp`。**允許 http 與私有 IP**（同 VPC 內網） |
| `ZENTRA_MCP_KEY` | — | **必填**。Zentra Console 發的 `zmcp_live_…`，**只勾選 stock-suite 那五個 Agent** |
| `ZENTRA_API_URL` | MCP 網址去掉 `/mcp` | 輪詢 `GET /v1/runs/{id}` 用的 REST 根網址 |
| `ZENTRA_REQUEST_TIMEOUT_SECONDS` | 30 | 單次 HTTP 請求的逾時 |
| `INSIGHT_TOOL_TIMEOUT_SECONDS` | 1200 | 單一 tool 從送出到結束最多等多久 |
| `INSIGHT_POLL_SECONDS` | 3 | 輪詢起始間隔（會漸增到最多 15 秒） |
| `INSIGHT_MAX_CONCURRENCY` | 8 | 一個 job 內同時進行的個股呼叫數 |
| `INSIGHT_MAX_RUNNING_JOBS` | 4 | 一個 backend 同時跑幾個 job（多使用者時排隊，見 §2「排隊」） |
| `INSIGHT_SCHEDULER_ENABLED` | 1 | `0` = 這個實例不跑排程器 |
| `INSIGHT_TICK_SECONDS` | 30 | 排程器最長睡多久 |
| `INSIGHT_STALE_MINUTES` | 30 | 多久沒有心跳就當作中斷 |
| `INSIGHT_CHART_RETENTION_DAYS` | 30 | K 線圖保留天數 |
| `INSIGHT_RETENTION_DAYS` | 180 | job 保留天數 |

`ZENTRA_MCP_URL` 或 `ZENTRA_MCP_KEY` 沒設定時，排程器不會啟動；清單和排程仍然可以編輯，但「立即分析」會回 `503`。

### 為什麼不走 `MCP_SERVERS_JSON`

那是給聊天 Router 用的通用 MCP client：讓 LLM 自己挑工具、結果截到 12,000 字、丟掉圖片、正式環境拒絕 http 與私有 IP。
排程需要的剛好相反，所以另外寫了一個專用 client，兩者互不影響。
Zentra 的 key **不要**加進 `MCP_SERVERS_JSON`，否則聊天的 LLM 也能呼叫這五個 tool。

### 正式環境網路（AWS）

- backend 與 Zentra 放在同一個 VPC（或 VPC peering），`ZENTRA_MCP_URL` 用私有 IP 或內部 DNS。
- **Zentra 的 REST 沒有認證**，`/v1/*` 連得到就能管理平台。Zentra 的 security group **只能放行 backend 那台**的來源，不可對外開放。
- 內網之間是 http，MCP key 會以明文在 VPC 內傳輸。要加密的話，前面加一個 internal ALB + ACM，`ZENTRA_MCP_URL` 改成 https 即可，程式不用改。

### 本機開發

本機的 Zentra 和 backend 都想用 8000 port，所以 Zentra 改開在 8100（compose override 把 app 的 `ports` 改成 `8100:8000`）。
backend 在容器裡，要透過 `host.docker.internal` 連主機：

```dotenv
ZENTRA_MCP_URL=http://host.docker.internal:8100/mcp
ZENTRA_MCP_KEY=zmcp_live_...
```

---

## 4. 資料表

見 [`V011__portfolio_insight.sql`](../app/backend/database/migrations/V011__portfolio_insight.sql) 的註解。重點：

- `portfolio_items`：`UNIQUE(user_id, market, symbol)`，**同一檔只能在一個清單**（買進時從觀察清單「移到」庫存）。
  `HOLDING` 必須有成本與股數（CHECK）。每個清單最多 30 檔，30 也是 Zentra `portfolio-advisor` 的上限。
- `insight_schedules`：每位使用者一列。`next_run_at IS NULL` 表示停用。
- `insight_results`：保存**執行當下**的成本與股數快照，之後改清單不會影響舊報告。
- 刪除使用者會 CASCADE 刪掉以上全部資料。

---

## 5. 費用與限制

- 每次分析的 LLM 費用記在 **Zentra** 那一側（`llm_calls`），**不計入**本專案的 token 配額。
  一檔股票每次約 1 次網路搜尋統整 + 3 次網路搜尋 + 4 次解讀 + 1 次看圖。持股多、頻率高時要留意費用。
- 報告內容是程式和模型整理公開資訊的結果，每個輸出都附 `disclaimer`，**不構成投資建議**。
- 新聞品質取決於 Zentra 那邊模型的網路搜尋能力，偶爾會有日期錯置的情況。
