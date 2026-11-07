# Stock Insight Chat 核心技術

這份文件整理這個專案裡**值得拿出來講的技術決策**：遇到什麼問題、怎麼解、為什麼這樣選、
代價是什麼，以及面試時常被追問的點。細節規格放在各自的 spec，這裡只講重點與取捨。

> 面試講法建議：每一題都用「**問題 → 做法 → 取捨 → 怎麼驗證**」四段講，
> 比列功能清單有說服力得多。

---

## 0. 30 秒介紹

> Stock Insight Chat 是一個股市分析對話平台。後端是 FastAPI 全非同步，Agent 用 LangGraph
> 拆成便宜模型做規劃、強模型寫報告；檢索是 Qdrant 的 dense + BM25 混合搜尋。
> 我花比較多心力的是**可靠性**：使用者關掉頁面，回答照樣跑完並存檔；Refresh Token 會輪替，
> 而且偵測得到重放攻擊；Token 配額用原子更新扣點；投資組合的排程分析把 PostgreSQL 當工作佇列，
> 開多台 backend 也不會重複執行。部署在 AWS：EC2 跑 Docker Compose，RDS 放關聯式資料，
> 前端在 S3 + Cloudflare，Lambda 每天增量同步向量。

| # | 主題 | 一句話 | 程式位置 |
|---|------|--------|----------|
| 1 | SSE 生產者–消費者 | 斷線不中斷生成，回答一定存進 DB | `api/chat.py` |
| 2 | Router / Analyst 雙模型 + Context 工程 | 便宜模型做決策、貴模型寫報告；Router 只看精簡上下文 | `agent/chat.py` |
| 3 | Hybrid RAG | dense + BM25 用 RRF 融合，依文章分組去重 | `tools/qdrant_hybrid.py` |
| 4 | Serverless 增量向量同步 | Lambda + 確定性 UUID，重跑不會重複寫入 | `data/lambda_function.py` |
| 5 | JWT RT Rotation | 原子消費 RT、偵測重放、前端共用一次 refresh | `api/auth.py`、`js/auth.js` |
| 6 | Token 配額 | pre-flight 攔截 + 原子累加，刻意允許單輪略超 | `module/usage_quota.py` |
| 7 | Parent DAG 對話樹 | `parent_id` + Recursive CTE 只載入目前分支 | `module/chat_context.py` |
| 8 | **DB 當工作佇列（排程分析）** | `FOR UPDATE SKIP LOCKED` + 部分唯一索引，多台不重複 | `insight/scheduler.py` |
| 9 | 外部 MCP 契約檢查 | 呼叫前比對 tools/list，參數組錯就不呼叫 | `insight/zentra.py` |
| 10 | 深度研究產檔 | 模型只填結構化 JSON，排版由程式決定 | `deep_research/` |
| 11 | MCP Client 安全 | 白名單、預設只開唯讀工具、擋私有 IP | `tools/mcp_client.py` |

---

## 1. SSE 串流：生產者–消費者解耦

**問題**：一次 Agent 回答要 10–30 秒。如果 LLM 直接在 SSE generator 裡跑，使用者關掉分頁，
Starlette 會 cancel generator，`CancelledError` 把整個回合打斷：錢花了，DB 卻沒有紀錄。

**做法**：每個請求建一個 `asyncio.Queue`。

```
asyncio.create_task(producer)   ← 跑 LangGraph，事件 put 進 queue，最後寫 DB，finally put(None)
event_generator (consumer)      ← 只做 queue.get() → yield；斷線只會 cancel 這一層
```

**重點**：
- 把「回答要不要繼續產生」和「使用者有沒有在看」拆成兩件事。
- 結束標記 `None` 放在 `finally`，例外時消費者也不會永遠卡在 `get()`。
- 前端也有對應設計：切換對話時，進行中的串流 DOM 會 park 到隱藏區繼續更新，上限 8 個，超過就 FIFO 中止最舊的（見 `frontend_spec.md` §10）。

**追問**
- *斷線後還在跑，不怕浪費錢嗎？* 怕，所以有配額 pre-flight（§6）；而且使用者重新整理後，可以從 DB 看到完整回答。這是刻意的取捨：體驗優先。
- *多台 backend 時會怎樣？* producer 綁在接到請求的那台行程裡。要做到「重連後接續串流」得把事件放到 Redis Stream 之類的外部儲存，目前不需要。
- *背景 task 被 GC 掉？* `create_task` 的回傳值要保留引用，否則可能被回收。排程器的 job 都放在 `self._jobs` set 裡，就是這個原因。

---

## 2. LangGraph 雙模型 Agent + Context 工程

**角色拆分**（成本驅動）：
- **Router（gpt-5-mini）**：只判斷要叫哪個工具、資料夠不夠。ReAct 最多 3 輪；空結果時會放寬時間、換關鍵字。
- **Analyst（gpt-5）**：拿到完整資料後一次寫完報告。
- Router 的 prompt 明令「不准寫總結」，只能回一句固定短句，避免便宜模型浪費 token 寫長文、拖慢收尾。

**Context 爆量的兩層根因**：
1. 每次進 Router 都帶完整 DB 歷史。
2. 同一輪 ReAct 重試時，每批 ToolMessage 會疊加，第 3 次進 Router 已經帶著前兩批全部的結果。

**雙軌保護**：

```
ToolMessage（截斷 500 字）→ state["messages"]       → Router 判斷用
原始完整資料              → state["retrieved_data"] → Analyst 分析用（品質不受影響）
```

| 手段 | 影響誰 |
|------|--------|
| Router 只看最近 2 輪歷史（`_slim_messages_for_router`） | 只有 Router |
| 同輪只留最新一批 ToolMessage，AIMessage（決策紀錄）全保留，避免重複叫同一工具 | 只有 Router |
| DB 歷史中舊的 Analyst 報告截斷到 800 字 | 兩者 |
| `RETRIEVAL_TOP_K` 15 → 5 | 注入量 |

**怎麼驗證**：每輪 Router／Analyst 的 prompt 與 completion tokens 都寫進 `token_usage_logs`，
可以拿同類對話比對優化前後各階段的 token 用量，用數字說話，不憑感覺。

**追問**
- *為什麼不全部用強模型？* 規劃步驟可能跑好幾輪，用便宜模型最划算；只有最後產出需要推理品質。
- *AIMessage 為什麼要保留？* Router 要知道自己已經試過哪些工具和參數，否則會重複呼叫。
- *快捷模式？* 直接跳過 Router，用原文檢索；可選「問句改寫」和原文並行雙軌搜尋，結果合併去重（見 `agent_spec.md` §9）。

---

## 3. Hybrid RAG（Qdrant dense + BM25）

**切塊策略因資料而異**：
- **新聞**：`RecursiveCharacterTextSplitter`（800 字、overlap 150，優先在段落或句號斷開）；每段前綴 `[標題]`，脫離上下文也檢索得到。
- **AI 產業分析**：依欄位語意拆成 `summary`、`key_news`、`stock_insight` 三種 chunk，讓不同意圖的查詢命中不同角色；推薦工具只搜 `stock_insight`。

**檢索**：
- **dense**（語意）+ **BM25 sparse**（精確詞，例如股票代碼「2330」）。兩者分數尺度不同，所以用 **RRF**（按名次融合）而不是加權相加。
- `group_by="mongo_id"`：同一篇長文的多個 chunk 不會洗版 top-k。
- payload index 支援時間區間、股票代碼、情緒、產業過濾；需要全文時再用 `mongo_id` 回 MongoDB 取。
- 三個工具共用同一組 `start_date`／`end_date`，結論在時間上一致。

**追問**
- *為什麼 RRF 後不設 cosine 門檻？* RRF 分數不是 cosine，原本的 0.3 門檻在融合後沒有意義，所以 hybrid 路徑的門檻預設是 `None`。
- *為什麼 Qdrant 不用 pgvector？* 需要原生 sparse 向量 + 伺服器端 RRF + group_by，Qdrant 一次給齊。

---

## 4. Serverless 增量向量同步

新聞每天寫進 MongoDB，**EventBridge 排程觸發 Lambda**，把新文件向量化後寫進 Qdrant。

- **不需要 checkpoint**：ObjectId 前 4 bytes 就是建立時間，直接用「最近 N 小時」範圍過濾。
- **冪等**：point ID = `uuid5(mongo_id + chunk_type + chunk_idx)`，重跑等於 upsert，不會重複。
- **Fail loud**：Embedding 重試用完就 raise，讓 Lambda 失敗並觸發告警；不填零向量假裝成功。修好後重跑就補齊（冪等的紅利）。
- BM25 模型在 build image 時就預載進映像，避免冷啟動時下載。

**追問**：*同步區間重疊會怎樣？* 刻意讓區間重疊（例如每天跑、抓 24+ 小時）。確定性 ID 讓重疊無害，換來不會漏資料。

---

## 5. 認證：Google SSO + JWT Refresh Token Rotation

| Token | 放哪 | 為什麼 |
|-------|------|--------|
| Access Token（15 分鐘） | JS 記憶體變數 | 不落 localStorage，降低 XSS 竊取風險；驗證時不查 DB |
| Refresh Token（7 天） | HttpOnly + `SameSite=Lax` Cookie | JS 讀不到；SameSite 防 CSRF |

**原子消費 + 重放偵測**：

```sql
DELETE FROM refresh_tokens WHERE token = $1 AND expires_at > NOW() RETURNING user_id
```

- 併發下只有一個請求刪得到那一列，所以每個 RT 只能用一次。
- 簽章有效、DB 卻刪不到 → 代表這個 RT 已經被用過 → 判定為重放攻擊，**撤銷該使用者所有 session**。無法分辨誰是本人，全部撤銷最安全。

**前端三道機制**：到期前 60 秒先 refresh；每次請求前檢查效期、401 自動重試；
**共用同一個 refresh Promise**。沒有最後這道，兩個同時 401 的請求會各 refresh 一次，第二次拿到的是已經消費過的舊 RT，會被誤判成攻擊，結果全裝置登出。

**追問**
- *為什麼以 `google_sub` 當身分？* email 可以改，`sub` 是 Google 帳號永久不變的 ID。
- *OAuth state 做什麼？* 防止登入 CSRF：callback 時比對 state，避免把攻擊者的帳號登進受害者的瀏覽器。

---

## 6. Token 配額：pre-flight + 原子累加

1. **Pre-flight**：進 LangGraph 前檢查 `used_tokens >= monthly_limit`，超過就直接回 429，不開串流。
2. **結算**：每輪 LLM 結束，在**同一個交易**裡做兩件事：`UPDATE … SET used_tokens = used_tokens + $delta` 原子累加計數器，再 INSERT 一筆 `token_usage_logs` 流水。計數器和流水分開存放：計數器負責高頻更新，流水只追加，用來對帳。

**刻意的取捨**：進行中的回合不中斷，允許單輪略超，由下一輪的 pre-flight 擋下（最終一致）。
理由是使用者明明還有額度，卻看到回答被砍一半，體驗太差。
如果改成成本優先，可以用條件式更新 `WHERE used_tokens + delta <= limit`，代價是可能中斷串流。

**深度研究的延伸**：前端斷線時 producer 會被 cancel，這時 `finally` 裡的 `await` 會馬上再丟出 `CancelledError`，記帳就不會發生。所以記帳改丟成獨立 task（`_spawn_cleanup`），不然「中途斷線」就成了免費研究的後門。

**追問**：*為什麼 `used_tokens = used_tokens + $1`，而不是先 SELECT 再 UPDATE？* 先讀後寫會有 lost update：兩個請求讀到同一個舊值，各自加完寫回，少算一次。

---

## 7. 對話歷史：Parent DAG + Recursive CTE

`messages.parent_id` 自參照，取代「一則對話一個陣列」：
- **重新生成**：多個 assistant 回答共用同一個 parent，前端可以做 `< 1/2 >` 版本切換。
- **載入上下文**：用 `WITH RECURSIVE` 從最新訊息沿著 `parent_id` 往上找，最多 `CONTEXT_CHAIN_MAX_HOPS` 層，只取目前分支，不會混進被放棄的分支。
- 分頁靠 `(chat_id, created_at DESC)` 複合索引做 cursor-based 分頁。

---

## 8. 投資組合排程分析：把 PostgreSQL 當工作佇列 ⭐

使用者設定每日／每週／每月的分析時間，時間一到，backend 就去呼叫外部 Agent 平台 Zentra，
對每檔股票跑新聞、K 線、技術面、財報四個工具，最後再對整個庫存跑投資組合建議。
**要求：準時、不重複、不遺漏，開多台 backend 也一樣。**

### 8.1 為什麼不用 Redis／Celery？

狀態本來就在 PostgreSQL，量也不大（每人每天幾次）。用 DB 當佇列，不必多維護一套元件，
而且「建 job」和「推進排程」可以放在**同一個交易**裡，這是外部佇列做不到的原子性。
代價是吞吐量比不上專門的 MQ，但這個場景遠遠用不到那麼多。

### 8.2 核心：`FOR UPDATE SKIP LOCKED`

- `FOR UPDATE`：把選到的列鎖到交易結束。
- `SKIP LOCKED`：遇到別人鎖住的列直接跳過，不等待。
- 兩者結合起來，就是一個不需要協調者的佇列：誰先鎖到就是誰的，其他台自動拿下一筆。

**認領到期排程**（一個交易）：

```sql
SELECT … FROM insight_schedules
 WHERE enabled AND next_run_at <= now()
 ORDER BY next_run_at LIMIT 50
   FOR UPDATE SKIP LOCKED;
-- 每列：savepoint 內 INSERT job（ON CONFLICT DO NOTHING）
-- 每列：UPDATE next_run_at = 下一個時間點
COMMIT;
```

`next_run_at` 跟著同一個交易推進，所以 commit 之後，別台再查，這列已經不算到期，不會被認領兩次。

**認領排隊中的 job**（一條語句）：

```sql
UPDATE insight_jobs SET status = 'RUNNING', started_at = now()
 WHERE id IN (SELECT id FROM insight_jobs WHERE status = 'QUEUED'
               ORDER BY created_at LIMIT $free FOR UPDATE SKIP LOCKED)
RETURNING id;
```

選取和改狀態在同一條語句裡完成，`RETURNING` 回來的 id 只屬於這一台。

### 8.3 第二道防線：部分唯一索引

| 索引 | 保證 |
|------|------|
| `uq_insight_jobs_scheduled (user_id, scheduled_for) WHERE trigger='SCHEDULED'` | 同一個時間點只會有一個排程 job（冪等） |
| `uq_insight_jobs_active (user_id) WHERE status IN ('QUEUED','RUNNING')` | 每人同時只有一個進行中的 job |

應用層邏輯就算有 bug，資料庫也會拒絕重複寫入。INSERT 放在 **savepoint** 裡：某一列撞到唯一索引，只會回滾這一列，不會連累同批其他使用者的排程。

### 8.4 準時：不是固定輪詢

每輪結束時查 `min(next_run_at)`，**睡到那個時間點 + 0.2 秒**（最長 30 秒，用來接住剛改過的排程）。
「立即分析」或修改排程時會呼叫 `poke()`，馬上叫醒。實測觸發誤差約 0.2 秒。
時區固定用 UTC+8（台灣沒有夏令時間），資料庫一律存 UTC。

### 8.5 失敗處理

| 情境 | 處理 |
|------|------|
| 停機錯過好幾個時間點 | 醒來後**只補跑一次**，然後從「現在」往後排，不會一次狂補 |
| backend 在 job 執行中重啟 | 每寫一筆結果就更新 heartbeat；超過 30 分鐘沒動靜，收屍改成 FAILED |
| 上一輪還沒跑完，下一輪就到了 | 撞到 `uq_insight_jobs_active`，這次略過並記 log，排程照樣往後推 |
| 部分工具失敗 | job 狀態為 PARTIAL，失敗那幾列記下 error 與 Zentra 的 run_id，方便追查 |

### 8.6 多台 backend

- 實測：3 個排程器同時跑、60 人同時到期 → 剛好 60 個 job；重複認領不會多出來；60 個 job 每個只執行一次。
- 只跑 API 的實例設 `INSIGHT_SCHEDULER_ENABLED=0`：收到「立即分析」只負責寫入 QUEUED，交給跑排程器的那台。（測試時抓到一個 bug：這種實例原本會回 503，已修正。）
- 並發上限是每台各自計算：總並發 = 台數 × `INSIGHT_MAX_RUNNING_JOBS`。

**追問**
- *`SKIP LOCKED` 會不會讓某些列一直被跳過？* 只有在被鎖的時候才會跳過；鎖會在交易結束時釋放，下一輪就拿得到。交易都很短，不會餓死。
- *為什麼不用 advisory lock 選一台當 leader？* leader 掛掉時還要處理接手；SKIP LOCKED 讓每台都是對等的 worker，天生容錯。
- *時鐘不同步？* 到期判斷用 DB 的 `now()`；下一個時間點用 backend 的時鐘計算。伺服器都有 NTP，誤差在毫秒級。
- *為什麼 job 中斷不自動重試？* 中斷前可能已經呼叫過部分付費工具，自動重試會重複計費；目前讓使用者自己決定要不要重跑。

---

## 9. 呼叫外部 MCP 工具：契約檢查

Zentra 的 tool 定義在另一個專案。只要那邊改了參數，這邊就可能**呼叫錯**，甚至白白花掉 LLM 費用。

- 每個 job 開始前先抓 `tools/list`，逐一比對每個 tool 的 `properties` 與 `required`，必須和本地定義**完全一致**；不一致就整個 job FAILED，一個工具都不呼叫。
- 呼叫前再檢查一次組出來的參數（不多不少）；回來後確認輸出的 `tool` 和 `security.symbol` 確實是這次問的那一檔。
- 長任務走 `_async: true` 拿到 run_id，再輪詢 `GET /v1/runs/{id}`，避免 HTTP 長連線逾時。
- 1–4 號工具的結果用 `precomputed` 傳給投資組合建議工具，不重抓資料、不重複花錢。

---

## 10. 深度研究：模型只填 JSON，排版交給程式

- 報告和簡報都用 `output_type` 綁定結構化 schema（`ReportDoc` / `DeckDoc`），HTML／docx／pptx 由**決定性的樣板**渲染。
  模型漏一個結束標籤，整份檔案就毀了；改成填 JSON 之後，最差的情況只是內容平庸，版面不會壞掉。
- 同一份 JSON 渲染成多種格式：多出一種格式的成本只有幾十毫秒，不必再燒 token，也不必在 640MB 的容器裡塞 LibreOffice。
- 頁數由使用者決定，但有三層上限（前端 min/max、API 絕對上限、clamp），因為頁數直接決定輸出 token 數，也就是成本。
- 上傳的檔案研究完就從 OpenAI 刪掉，另外掛 `expires_after` 當保險絲。
- 模型用白名單鎖定：換模型等於換費率，而費率表和配額扣點都綁在 model id 上。

---

## 11. MCP Client 安全

本專案也可以當 MCP Client，接外部工具給 Agent 用。外部工具等於不受信任的輸入，所以：
- Server URL 只能由部署者在環境變數設定，使用者不能透過 API 新增（防 SSRF）。
- 正式環境預設拒絕 HTTP 與私有／loopback IP（明確指定的內網例外除外，例如同 VPC 的 Zentra）。
- 沒設白名單時，**只匯入標示為唯讀的工具**；可能寫入資料的工具，必須由部署者明確加入白名單。
- 工具名稱一律加上前綴 `mcp__<server>__<tool>`，避免和內建工具撞名；單次結果最多注入 12,000 字。
- API 回應中不包含 URL、header 或 token；錯誤訊息裡的 URL 也會遮蔽。

---

## 12. 其他可以順帶一提的

| 主題 | 一句話 |
|------|--------|
| Schema 一致性 | 用 catalog 查詢產生排序過的結構清單，三方比對正式 RDS、本機與規格，不依賴 pg_dump 的欄位順序 |
| Migration | 只做加法的版本化 SQL（`V0xx__*.sql`），同步回 `init_db.sql`；先跑 migration 再部署新程式 |
| 前端安全 | 使用者資料一律用 `textContent`；Markdown 先經 DOMPurify 再寫進 DOM |
| PWA | Service Worker 對導覽用 network-first、靜態資源用 stale-while-revalidate；API／SSE 不攔截 |
| 圖表 | 甜甜圈圖的顏色跟著持股（依建立順序），不跟排名，篩選後顏色不會亂跳；調色盤通過色盲辨識驗證 |
| 測試 | httpx `MockTransport` 模擬 Zentra，不打真實服務；Prompt Injection 測試腳本 |

---

## 13. 已知限制（被問到「還能怎麼改進」時用）

- 聊天的 producer 綁在單一行程：重新整理後看得到 DB 裡的結果，但接不回即時串流。改進方式是用 Redis Stream 存事件。
- 深度研究的 session 在記憶體：多 worker 時要換成 Redis。
- 中斷的排程 job 不會自動重試（避免重複計費）。可以改成只重跑失敗或缺少的工具。
- 配額允許單輪略超（§6 的取捨）。
- 單台 EC2 是單點故障；要提高可用性，可以遷移到 ECS Fargate 並開多台（排程器已經支援多實例）。
