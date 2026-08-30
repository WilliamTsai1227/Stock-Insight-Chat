# 環境變數設定說明 (Environment Variables)

本文件列出**程式實際會讀取的所有環境變數**、預設值與注意事項。

> **權威來源**：以原始碼中的 `os.getenv(...)` 為準。本文件與其對齊；新增變數時請一併更新此表。  
> 深入的 Flash／Router 調參說明見 [`readme_full_details.md`](./readme_full_details.md)；Agent 行為見 [`agent_spec.md`](./agent_spec.md)。

---

## 一、設定方式

| 環境 | 檔案 | 讀取方式 |
| :--- | :--- | :--- |
| **本機開發** | 專案根目錄 `.env` | compose 的 `env_file: ../.env` 注入 backend；`load_dotenv()` 亦會讀 |
| **生產（EC2）** | 部署目錄 `.env` | compose 的 `env_file: .env`（與 `docker-compose.prod.yml` 同目錄） |
| **生產（探索）** | 部署目錄 `.env.kinetic` | kinetic 容器專用，見 [`deploy/.env.kinetic.example`](../deploy/.env.kinetic.example) |

範本：[`.env.example`](../.env.example)（本機）、[`deploy/.env.prod.example`](../deploy/.env.prod.example)（生產）。`.env` 已列入 `.gitignore`，**勿提交至版本控制**。

### ⚠️ 兩個常見陷阱

**1. 不要用行內註解**

```env
ROUTER_REASONING_EFFORT=minimal    # 合法值: minimal / low / ...   ← 危險
```

本機 python-dotenv 會幫你把 `# ...` 剝掉，但 Docker Compose 的 `env_file` parser 行為不同，可能把整串當成值送進容器，導致 API 回 400。**註解請獨立一行。**

**2. 改了 `.env` 要用 `up -d`，不是 `restart`**

環境變數是容器**建立當下**就固定的，`restart` 只是把同一個容器停掉再啟動，不會重讀 `env_file`：

```bash
docker compose -f ./deploy/docker-compose.yml up -d backend      # 本機
docker compose -f docker-compose.prod.yml up -d backend          # EC2
docker compose ... exec backend env | grep <變數名>               # 驗證讀到了
```

詳見 [`docker_ops_handbook.md`](./docker_ops_handbook.md) §3.1。

---

## 二、安全性（Security）

| 變數 | 預設 | 正式必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `SECRET_KEY` | `super-secret-key-for-development` | ✅ | AT / RT 的 HS256 簽名密鑰 |
| `COOKIE_SECURE` | `false` | ✅ | RT Cookie 的 `Secure` 屬性 |
| `CORS_ALLOWED_ORIGINS` | `http://localhost,http://localhost:80` | ✅ | 允許的前端來源（逗號分隔） |

- `SECRET_KEY` 正式環境請用 `openssl rand -hex 32` 產生；**預設值絕不可用於正式環境**。
- `COOKIE_SECURE`：Secure Cookie 只在 HTTPS 下送出。本機 HTTP 開發設 `false`，否則 RT 永遠不會被帶到 `/api/user/refresh`，每次都 401；正式環境（HTTPS）必須 `true`。
- `CORS_ALLOWED_ORIGINS`：`allow_credentials=True` 下**不可用萬用字元 `*`**。程式另有 `allow_origin_regex` 放行 `192.168.0.*` 網段供區網開發。

> **AT / RT 效期不是環境變數**：`ACCESS_TOKEN_EXPIRE_MINUTES = 15`、`REFRESH_TOKEN_EXPIRE_DAYS = 7` 是 [`module/jwt.py`](../app/backend/module/jwt.py) 的硬編碼常數，寫進 `.env` 不會生效。

---

## 三、Google SSO（登入必需）

| 變數 | 預設 | 正式必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `GOOGLE_CLIENT_ID` | `""` | ✅ | Google OAuth Client ID |
| `GOOGLE_CLIENT_SECRET` | `""` | ✅ | Google OAuth Client Secret |
| `GOOGLE_OAUTH_REDIRECT_URI` | 無預設（必填） | ✅ | Callback URI，須與 Google Console **完全一致**（含 protocol / port / path） |
| `FRONTEND_URL` | `http://localhost` | ✅ | 前端根網址；登入後重導目標，同時作為 `/explore/*` 的 CSP `frame-ancestors` 白名單 |

少了這組就無法登入。設定步驟與 Google Console 對應見 [`google_sso.md`](./google_sso.md)。

---

## 四、資料庫

| 變數 | 預設 | 正式必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `DATABASE_URL` | `postgresql://postgres:password123@db:5432/Insight` | ✅ | asyncpg 連線字串 |
| `DATABASE_SSL` | `""` | RDS 建議 | 設為 `require` / `true` / `1` 時以 TLS 連線 |

- 程式會自動把 `postgresql+asyncpg://` 正規化為 `postgresql://`。
- asyncpg **不支援** URL query 上的 `?ssl=require`（會觸發 `CantChangeRuntimeParamError`），因此需改用 `DATABASE_SSL` 環境變數；若連線字串裡寫了 `?ssl=`，程式會剝離後改以 `create_pool(ssl=...)` 傳入。
- **`db` 容器目前預設停用**，本機開發也連 RDS，見 [`sql_dev_handbook.md`](./sql_dev_handbook.md) §2。

---

## 五、向量庫與文件庫

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `QDRANT_HOST` | `localhost` | Qdrant 主機（compose 內設為 `qdrant`） |
| `QDRANT_PORT` | `6333` | Qdrant REST API 埠 |
| `MONGO_URI` | 無預設 | MongoDB 連線字串 —— **`news.py` 使用** |
| `MONGODB_URL` | `mongodb://localhost:27017` | 同上，**`ai_analysis.py` 使用**（歷史遺留的別名） |
| `MONGO_DB` | `stock_insight` | MongoDB 資料庫名稱 |

> ⚠️ `MONGO_URI` 與 `MONGODB_URL` 是**兩個不同的變數名指向同一個 MongoDB**（[`news.py`](../app/backend/tools/news.py) 讀前者、[`ai_analysis.py`](../app/backend/tools/ai_analysis.py) 讀後者）。正式環境**兩個都要設**，且值要相同，否則其中一個工具會連到 `localhost` 而失敗。

---

## 六、Agent 模型與推理強度

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `ROUTER_REASONING_EFFORT` | `minimal` | Router（`gpt-5-mini`）的思考強度。合法值 `minimal` / `low` / `medium` / `high`；**留空＝不傳此參數，走模型預設** |
| `ANALYST_REASONING_EFFORT` | `low` | Analyst（`gpt-5`）的思考強度，同上 |
| `ANALYST_MAX_COMPLETION_TOKENS` | `""`（不設上限） | Analyst 的 `max_completion_tokens`。⚠️ reasoning 模型會**連思考 token 一起算**，設太低可能從中間截斷甚至無輸出 |
| `TITLE_MODEL` | `gpt-4o-mini` | 產生對話標題（15 字內）的小模型 |
| `ANALYST_MODEL` | `gpt-4o` | ⚠️ **僅作為 token 計帳的模型標籤**，見下方警告 |

> ⚠️ **`ANALYST_MODEL` 不會換掉 Analyst 模型。** 真正的 Analyst 是 [`agent/chat.py`](../app/backend/agent/chat.py) 內硬編碼的 `gpt-5`；此變數只在 [`api/chat.py`](../app/backend/api/chat.py) 當作寫入 `token_usage_logs.model_name` 的預設標籤，且預設值 `gpt-4o` 與實際模型不符。要換 Analyst 模型請改程式碼。

**延遲最敏感的兩個旋鈕**是 `ROUTER_REASONING_EFFORT` 與 `ANALYST_REASONING_EFFORT`。Router 只需選工具 → `minimal` 足夠；Analyst 需論述 → `low`。要恢復深度思考設 `medium` / `high` 或留空。

驗證有沒有生效：OpenAI Platform → Logs → 展開該筆的 Tokens，看 **reasoning tokens**（`minimal` 應接近 0）。

### 6.1 Analyst 報告篇幅

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `ANALYST_TARGET_MIN_WORDS` | `1200` | 報告目標字數下限（軟性，由 prompt 約束） |
| `ANALYST_TARGET_MAX_WORDS` | `1800` | 報告目標字數上限 |
| `ANALYST_HARD_CAP_WORDS` | `2600` | 硬上限 |

三者任一設為空或 `<= 0` → **完全不加篇幅約束**（恢復不限長度的舊行為）。控制的是「最終報告的可見字數」，與 `reasoning_effort`（思考深度）無關。

### 6.2 一般對話（非股市 Agent）

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `GENERAL_CHAT_MODEL` | `gpt-4o-mini` | `chat_mode=general` 使用的模型 |
| `GENERAL_CHAT_ENABLE_WEB_SEARCH` | `1` | 一般對話是否允許網路搜尋 |
| `GENERAL_REWRITE_LLM` | `gpt-4o-mini` | 一般對話檢索前的問句改寫模型 |

---

## 七、檢索與上下文長度

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `MAX_TOOL_ITEM_CHARS` | `500` | 餵給模型的 ToolMessage 每則正文截斷長度。Analyst 另有未截斷的【完整參考資料】，見 [`agent_spec.md`](./agent_spec.md) §8.5 |
| `ROUTER_HISTORY_TURNS` | `2` | Router 保留幾輪對話歷史（每輪＝1 問 + 1 答） |
| `CONTEXT_CHAIN_MAX_HOPS` | `6` | 從 DB 沿 `parent_id` 往上遞迴載入幾步歷史訊息 |
| `HISTORY_HUMAN_MAX_CHARS` | `3000` | DB 歷史中使用者發話的截斷上限 |
| `HISTORY_ASSISTANT_MAX_CHARS` | `800` | DB 歷史中 Analyst 回答的截斷上限（股市 Agent） |
| `HISTORY_GENERAL_ASSISTANT_MAX_CHARS` | `4000` | 同上，一般對話模式用（較寬鬆） |
| `QUERY_MAX_CHARS` | `2000` | `POST /api/chat/messages` 的 `query` 長度上限 |

> 思考模式的 `RETRIEVAL_TOP_K = 5` 是 [`agent/chat.py`](../app/backend/agent/chat.py) 的程式常數，**不是**環境變數；Flash 模式才有可調的 `FLASH_RETRIEVAL_TOP_K`。

---

## 八、Tavily 網路搜尋（`tavily_global_search` 工具）

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `TAVILY_API_KEY` | `""` | Tavily API 金鑰；**未設定時該工具不可用** |
| `TAVILY_MAX_RESULTS` | `5` | 每次搜尋回傳筆數 |
| `TAVILY_SEARCH_DEPTH` | `basic` | `basic`（快）或 `advanced`（深、較慢較貴） |
| `TAVILY_INCLUDE_ANSWER` | `1` | 是否請 Tavily 附帶即時摘要 |
| `TAVILY_TIMEOUT_SEC` | `15` | 單次搜尋逾時秒數 |
| `TAVILY_MAX_CALLS_PER_TURN` | `2` | 單輪最多呼叫幾次，避免 Agent 反覆搜尋拖慢回應 |

---

## 九、Flash（快捷）模式

僅在 `response_mode=flash` 時生效；思考模式不受這些變數影響。完整說明見 [`readme_full_details.md`](./readme_full_details.md)。

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `FLASH_ANALYST_MODEL` | `gpt-5-mini` | Flash 模式的 Analyst 模型 |
| `FLASH_ANALYST_MAX_TOKENS` | `5000` | 對應 `max_completion_tokens`。設太小會被推理 token 吃光，只剩標題半句；**設為空字串＝不傳上限** |
| `FLASH_SKIP_ROUTER` | `1` | `1`＝略過規劃用 Router LLM（較快）；`0`＝呼叫 Router 細調檢索參數 |
| `FLASH_RETRIEVAL_TOP_K` | `10` | Flash 每輪向量取回上限 |
| `FLASH_REF_MAX_BODY_CHARS` | `2200` | 注入 Analyst【完整參考資料】時每段正文上限（下限 400） |
| `FLASH_DATE_RANGE_DAYS` | `80` | 預設 `start_date` / `end_date` 的回溯天數 |
| `FLASH_ENABLE_WEB_SEARCH` | `0` | Flash 模式是否啟用網路搜尋 |
| `FLASH_LLM_QUERY_REWRITE` | `0` | `1`＝檢索前用小模型把問句收成檢索用 query |
| `FLASH_REWRITE_DUAL_SEARCH` | `1` | 與上者併用：`1`＝原文與收成後 query 各搜一次再合併去重；`0`＝只搜收成結果 |
| `FLASH_REWRITE_MODEL` | `gpt-4o-mini` | 問句收成用模型 |
| `FLASH_REWRITE_MAX_COMPLETION_TOKENS` | `256` | 收成請求的 token 上限（宜小以降低延遲） |
| `FLASH_REWRITE_TIMEOUT_SEC` | `12` | 收成 LLM 逾時秒數（下限 0.8） |
| `FLASH_MERGED_RETRIEVE_CAP` | `max(TOP_K×2, 16)` | 雙軌並搜合併後最多保留幾段參考 |

---

## 十、探索（Kinetic Charts 代理）

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `KINETIC_UPSTREAM` | `""` | `/explore/*` 的代理上游。compose 內設為 `http://kinetic:8000` |

**留空＝整組 `/explore/*` 回 404**，等同關閉探索功能。kinetic 容器本身的環境變數走獨立的 `.env.kinetic`，見 [`deploy/.env.kinetic.example`](../deploy/.env.kinetic.example)。

---

## 十一、深度研究（Deep Research）

以 OpenAI Agents SDK 執行的獨立功能，與聊天／Flash 完全分開。只有 `DEEP_SEARCH_MODEL` 是必填。

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `DEEP_SEARCH_MODEL` | `gpt-5.6-luna` | **前端未選模型時的預設模型**（本功能唯一必填）。設什麼就送什麼，即使不在下面的清單裡也會自動併入 |
| `DEEP_SEARCH_MODELS` | 程式內清單 | 逗號分隔，覆寫前端可選的模型；**前端送來**的清單外 id 會被退回預設值 |
| `DEEP_SEARCH_MAX_FILES` | `10` | 單次研究可上傳的檔案數 |
| `DEEP_SEARCH_MAX_FILE_MB` | `20` | 單一檔案大小上限 |
| `DEEP_SEARCH_MAX_IMAGES` | `4` | 單次研究可附的圖片數 |
| `DEEP_SEARCH_QUERY_MAX_CHARS` | `4000` | 研究題目字數上限 |
| `DEEP_SEARCH_MAX_TURNS` | `24` | Agent 迴圈上限（一次 web search 會用掉數輪） |
| `DEEP_SEARCH_DECK_SLIDES` | `12` | 簡報頁數的**預設值**；使用者可在前端改成 5–20。設在區間外會被壓回區間內，不會放大上限 |
| `DEEP_SEARCH_REPORT_SECTIONS` | `6` | 報告小節數的**預設值**；使用者可在前端改成 3–10。同樣會被壓回區間內 |
| `DEEP_SEARCH_SPREADSHEET_MAX_CHARS` | `12000` | 單張試算表轉 Markdown 後的字數上限 |
| `DEEP_SEARCH_SPREADSHEET_TOTAL_MAX_CHARS` | `30000` | 所有試算表合計進 prompt 的字數上限 |
| `DEEP_SEARCH_SESSION_TTL_MINUTES` | `120` | 記憶體 session 存活時間 |
| `DEEP_SEARCH_MAX_SESSIONS_PER_USER` | `5` | 每位使用者保留的 session 數，超過淘汰最舊的 |
| `DEEP_SEARCH_VECTOR_STORE_EXPIRES_DAYS` | `1` | OpenAI vector store 的 `expires_after` 保險絲 |

> `DEEP_SEARCH_MODELS` 內只該放 **Responses API 支援 hosted tools**（`web_search` / `file_search`）的模型。
> `gpt-5.6-luna` / `sol` / `terra` 已實測通過（web search、file search、`reasoning.effort=medium`、structured outputs 皆可）。
> 換新模型前建議先跑一次驗證：附一份只有檔案裡才有的事實，看模型答不答得出來，即可確認 file search 真的生效。
> 非推理模型（`gpt-4.1` 系列）不會被帶上 `reasoning.effort` —— 帶了 Responses API 會直接回 400，
> 判斷邏輯在 [`config.py`](../app/backend/deep_research/config.py) 的 `supports_reasoning_effort()`。
>
> 本功能沿用既有的 `OPENAI_API_KEY`；未設定時 `/api/deep-research/runs` 回 **503**。
> 目前**不計入 `token_usage_logs` 與月配額**（MVP 取捨），改以「每位使用者同時只能跑一個研究」節流。

---

## 十二、建議回饋 API（`POST /api/user/feedback`）

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `FEEDBACK_RATE_LIMIT_MAX` | `5` | 時間窗內最多提交次數 |
| `FEEDBACK_RATE_LIMIT_WINDOW_MINUTES` | `10` | rate limit 時間窗（分鐘） |
| `FEEDBACK_DUPLICATE_WINDOW_MINUTES` | `30` | 相同內容重複提交阻擋時間窗 |
| `FEEDBACK_MIN_SUBMIT_SECONDS` | `2` | 表單開啟後最少等待秒數（防 bot） |
| `FEEDBACK_DAILY_MAX` | `3` | 每位使用者每曆日提交上限 |
| `FEEDBACK_TOKEN_REWARD` | `2500` | 每次成功提交發放的 Token 獎勵（從 `used_tokens` 扣除） |
| `FEEDBACK_DAILY_TIMEZONE` | `Asia/Taipei` | 計算「每日」上限的時區 |
| `TURNSTILE_SITE_KEY` | `""` | Cloudflare Turnstile 站台 key |
| `TURNSTILE_SECRET_KEY` | `""` | Turnstile secret |
| `CF_TURNSTILE_SITE_KEY` | `""` | `TURNSTILE_SITE_KEY` 的**備用名稱**（前者未設時才讀） |
| `CF_TURNSTILE_SECRET_KEY` | `""` | 同上 |

- Turnstile **site key 與 secret 同時設定**才會啟用 CAPTCHA。
- 未啟用 Turnstile 時仍套用 rate limit、honeypot、context 大小限制、重複內容檢查。
- 部署到既有 DB 需先執行 migration `V008__user_feedback_tokens_granted.sql`。

---

## 十三、除錯

| 變數 | 預設 | 說明 |
| :--- | :--- | :--- |
| `TOKEN_PARSE_DEBUG` | `""` | `1`＝印出所有 token 解析；`zero`＝僅在批次為 0 時印。輸出前綴 `[TOKEN-PARSE-DEBUG]` |

> **`DEBUG` 不是有效變數。** `.env.prod.example` 裡的 `DEBUG=false` 是歷史遺留，程式從未讀取，設或不設都沒有作用。

---

## 十四、正式環境 `.env` 範本

```env
# ── Docker Hub 映像（compose 變數替換用）────────────────
DOCKERHUB_USER=your-dockerhub-user
IMAGE_TAG=1.0.1
KINETIC_TAG=1.0.0

# ── 安全性 ────────────────────────────────────────────
SECRET_KEY=<openssl rand -hex 32>
COOKIE_SECURE=true
CORS_ALLOWED_ORIGINS=https://app.example.com

# ── Google SSO ────────────────────────────────────────
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
GOOGLE_OAUTH_REDIRECT_URI=https://api.example.com/api/user/auth/google/callback
FRONTEND_URL=https://app.example.com

# ── PostgreSQL（RDS）──────────────────────────────────
DATABASE_URL=postgresql://user:password@your-rds.ap-northeast-1.rds.amazonaws.com:5432/Insight
DATABASE_SSL=require

# ── Qdrant / MongoDB ──────────────────────────────────
# QDRANT_HOST 由 compose 的 environment 設定，不需在此重複
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
MONGODB_URL=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
MONGO_DB=stock_insight

# ── LLM ───────────────────────────────────────────────
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...

# ── 深度研究 ──────────────────────────────────────────
DEEP_SEARCH_MODEL=gpt-5.6-luna

# ── 延遲調校（可選；不設則用程式預設）────────────────
ROUTER_REASONING_EFFORT=minimal
ANALYST_REASONING_EFFORT=low
ANALYST_TARGET_MAX_WORDS=1800
```

`DOCKERHUB_USER` / `IMAGE_TAG` / `KINETIC_TAG` 只給 compose 做變數替換，不會進容器。

---

## 十五、本機開發最小設定

```env
OPENAI_API_KEY=sk-...
DATABASE_URL=postgresql://...        # 目前本機也連 RDS
MONGO_URI=mongodb+srv://.../stock_insight
MONGODB_URL=mongodb+srv://.../stock_insight
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/user/auth/google/callback
DEEP_SEARCH_MODEL=gpt-5.6-luna       # 深度研究預設模型
```

`QDRANT_HOST=qdrant` 已由 `docker-compose.yml` 的 `environment` 設定，不需重複填。其餘變數都有堪用的預設值。

---

## 十六、安全性核查清單（上線前）

- [ ] `SECRET_KEY` 已換成 `openssl rand -hex 32` 產生的隨機值
- [ ] `COOKIE_SECURE=true`（HTTPS 環境）
- [ ] `CORS_ALLOWED_ORIGINS` 只含正式網域，無 `localhost`
- [ ] `DATABASE_URL` 密碼已更換，不使用預設 `password123`；`DATABASE_SSL=require`
- [ ] `MONGO_URI` **與** `MONGODB_URL` 都已設定且值相同
- [ ] `GOOGLE_OAUTH_REDIRECT_URI` 與 Google Console 完全一致
- [ ] `.env` 無行內註解，且已在 `.gitignore` 中、未提交
- [ ] `OPENAI_API_KEY` 已在 OpenAI Platform 設定用量上限
- [ ] （可選）Turnstile site key + secret 都已設定
- [ ] 改完 `.env` 是用 `up -d` 而非 `restart`，並已 `exec backend env | grep` 驗證
