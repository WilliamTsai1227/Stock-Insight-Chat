# 環境變數設定說明 (Environment Variables)

本文件列出系統所有環境變數、預設值、各環境建議設定值，以及注意事項。

---

## 一、設定方式

後端透過 Docker Compose 的 `env_file` 讀取專案根目錄的 `.env` 檔案：

```yaml
# deploy/docker-compose.yml
env_file:
  - ../.env
```

修改 `.env` 後需重啟 backend 才會生效，指令見 [`docker_ops_handbook.md`](./docker_ops_handbook.md) §3.1。

請在專案根目錄建立 `.env`（已列入 `.gitignore`，**勿提交至版本控制**）：

```bash
cp .env.example .env   # 若有範本
# 或手動建立 .env 並填入以下變數
```

---

## 二、變數總覽

### 🔐 安全性（Security）

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `SECRET_KEY` | `super-secret-key-for-development` | ✅ | JWT 簽名密鑰，正式環境必須換成高強度亂數字串 |
| `COOKIE_SECURE` | `false` | ✅ | RT Cookie 的 `Secure` 屬性；正式環境（HTTPS）必須設為 `true` |

#### `SECRET_KEY`

- 用於 AT 與 RT 的 HS256 簽名與驗證
- **本機預設值 `super-secret-key-for-development` 絕對不能用於正式環境**
- 正式環境請用以下指令產生：
  ```bash
  openssl rand -hex 32
  # 範例輸出：a3f8c2d1e4b7a9f0c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5
  ```

#### `COOKIE_SECURE`

- 控制 RT Cookie 的 `Secure` 屬性
- `Secure` Cookie 只在 **HTTPS** 連線下才會被瀏覽器送出
- 本機 HTTP 開發時設 `false`，否則 RT Cookie 永遠不會被帶到 `/refresh`，導致每次都 401
- 正式環境（HTTPS）必須設 `true`，防止 Cookie 在 HTTP 明文傳輸中被竊取

| 環境 | 設定值 | 原因 |
| :--- | :--- | :--- |
| 本機開發（HTTP） | `COOKIE_SECURE=false` | 瀏覽器 HTTP 下不送 Secure Cookie |
| 正式環境（HTTPS） | `COOKIE_SECURE=true` | 強制只在加密連線傳輸 Cookie |

---

### 🤖 AI 服務（OpenAI）

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `OPENAI_API_KEY` | 無（必填） | ✅ | OpenAI API 金鑰，用於 Embedding 與 GPT 對話 |

- 在 [OpenAI Platform](https://platform.openai.com/api-keys) 取得
- 格式：`sk-...`
- **勿提交至 Git**

---

### 🗄️ 資料庫（PostgreSQL）

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `DATABASE_URL` | `postgresql://postgres:password123@db:5432/Insight` | ✅ | asyncpg 連線字串 |

- Docker Compose 本機環境已在 `docker-compose.yml` 的 `environment` 直接設定，不需寫進 `.env`
- 正式環境請換成實際的 DB 主機、帳密、資料庫名稱
- 格式：`postgresql://USER:PASSWORD@HOST:PORT/DBNAME`

---

### 📦 向量資料庫（Qdrant）

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `QDRANT_HOST` | `localhost` | ✅ | Qdrant 主機位址 |
| `QDRANT_PORT` | `6333` | — | Qdrant REST API 埠號 |

- Docker Compose 本機環境已在 `docker-compose.yml` 設定 `QDRANT_HOST=qdrant`（容器名稱）
- 正式環境若使用 Qdrant Cloud，`QDRANT_HOST` 填入雲端 cluster URL

---

### 🍃 MongoDB

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `MONGO_URI` | `mongodb://localhost:27017` | ✅ | MongoDB 連線字串（含帳密） |
| `MONGODB_URL` | `mongodb://localhost:27017` | ✅ | 同上，另一處引用的別名（待統一） |
| `MONGO_DB` | `stock_insight` | — | 資料庫名稱 |

> ⚠️ 注意：`news.py` 使用 `MONGO_URI`，`ai_analysis.py` 使用 `MONGODB_URL`，兩者應指向同一個 MongoDB 實例。正式環境請確保兩個變數都設定。

---

### 🌐 CORS（跨域設定）

| 變數名稱 | 本機開發預設 | 正式環境必填 | 說明 |
| :--- | :--- | :---: | :--- |
| `CORS_ALLOWED_ORIGINS` | `http://localhost,http://localhost:80,http://127.0.0.1,http://127.0.0.1:80` | ✅ | 允許的前端來源（逗號分隔） |

- 正式環境填入實際網域，例如：`https://your-domain.com`
- `allow_credentials=True` 情況下**不可使用萬用字元 `*`**，瀏覽器安全規範不允許

---

## 三、正式環境 `.env` 範本

```env
# ─── 安全性 ───────────────────────────────────────────────────
SECRET_KEY=your-super-secure-random-hex-key-here   # openssl rand -hex 32
COOKIE_SECURE=true

# ─── OpenAI ──────────────────────────────────────────────────
OPENAI_API_KEY=sk-...

# ─── PostgreSQL ───────────────────────────────────────────────
DATABASE_URL=postgresql://your_user:your_password@your_db_host:5432/your_db_name

# ─── Qdrant ───────────────────────────────────────────────────
QDRANT_HOST=your-qdrant-host
QDRANT_PORT=6333

# ─── MongoDB ──────────────────────────────────────────────────
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
MONGODB_URL=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
MONGO_DB=stock_insight

# ─── CORS ─────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS=https://your-domain.com
```

---

## 四、本機開發 `.env` 最小設定

本機 Docker Compose 已內建大多數預設值，只需提供以下兩項：

```env
OPENAI_API_KEY=sk-...
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
MONGODB_URL=mongodb+srv://user:password@cluster.mongodb.net/stock_insight
```

其餘變數（`DATABASE_URL`、`QDRANT_HOST` 等）已在 `docker-compose.yml` 的 `environment` 區塊設定，不需重複填入 `.env`。

---

## 五、建議回饋 API 安全（`POST /api/user/feedback`）

| 變數 | 預設 | 說明 |
|------|------|------|
| `FEEDBACK_RATE_LIMIT_MAX` | `5` | 每使用者於時間窗內最多提交次數 |
| `FEEDBACK_RATE_LIMIT_WINDOW_MINUTES` | `10` | rate limit 時間窗（分鐘） |
| `FEEDBACK_DUPLICATE_WINDOW_MINUTES` | `30` | 相同內容重複提交阻擋時間窗 |
| `FEEDBACK_MIN_SUBMIT_SECONDS` | `2` | 表單開啟後最少等待秒數（防 bot） |
| `TURNSTILE_SITE_KEY` | — | Cloudflare Turnstile 站台 key（可選） |
| `TURNSTILE_SECRET_KEY` | — | Turnstile secret；與 site key 同時設定才啟用 CAPTCHA |
| `FEEDBACK_DAILY_MAX` | `3` | 每位使用者每曆日最多提交建議回饋次數 |
| `FEEDBACK_TOKEN_REWARD` | `2500` | 每次成功提交發放的 Token 獎勵（從 `used_tokens` 扣除） |
| `FEEDBACK_DAILY_TIMEZONE` | `Asia/Taipei` | 計算「每日」次數上限的時區 |

未設定 Turnstile 時仍會套用 rate limit、honeypot、context 大小限制、重複內容檢查。  
成功提交回饋後會自動發放 Token；每日次數見 `FEEDBACK_DAILY_MAX`。部署既有 DB 需執行 migration `V008__user_feedback_tokens_granted.sql`。

---

## 六、安全性核查清單（上線前確認）

- [ ] `SECRET_KEY` 已換成 `openssl rand -hex 32` 產生的隨機值
- [ ] `COOKIE_SECURE=true`（HTTPS 環境）
- [ ] `CORS_ALLOWED_ORIGINS` 只包含正式網域，無 `localhost`
- [ ] `DATABASE_URL` 密碼已更換，不使用預設 `password123`
- [ ] `.env` 已加入 `.gitignore`，未提交至版本控制
- [ ] `OPENAI_API_KEY` 已設定 API 使用限額（OpenAI Platform）
- [ ] （可選）`TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` 已設定，啟用回饋 CAPTCHA
