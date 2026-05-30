# Google SSO 與 `users` Schema（作法 A）

本文件說明：**為何以 `google_sub` 為準**、資料庫作法 A 的欄位意義、OAuth **重新導向 URI** 端點設計要點，以及 **Docker 內**如何連進容器執行 PostgreSQL 指令（含既有庫的 migration）。

> **實作狀態**：`init_db.sql` 已預留欄位給未來 `GET /api/user/auth/google/start` / `callback` 等端點。**後端路由與前端按鈕**需另開任務實作；本文以規格與 DB 為主。

---

## 1. `google_sub` 是什麼？為什麼「以 sub 為主、email 僅顯示」？

Google 使用 **OpenID Connect**。使用者在 Google 登入成功後，身分權杖（**ID Token**）或 **UserInfo** 裡會有一段穩定識別碼：

| 欄位（概念） | 在 Google / OIDC 裡常見名稱 | 意義 |
|-------------|---------------------------|------|
| **主體識別碼** | **`sub`（Subject）** | Google 帳號在全世界 OIDC 底下的**唯一、穩定 ID**（字串）。換暱稱、改顯示名稱通常**不會變**。我們存入 DB 的 **`google_sub`** 就是這個值。 |
| 聯絡用／顯示用 | **`email`** | 可能被使用者**更改**或因隱私設定而**不可用**（視你的 OAuth scope 與帳號類型）；**不能**單獨當作主鍵身分。 |

**「以 google_sub 為主」**：登入或「找既有使用者」時，優先用 **`google_sub` 是否已存在於 `users`** 來對應帳號，而不是只用 email。

**「email 僅顯示用」**：仍可存 **`users.email`** 給 UI、通報錯誤文案用，但不要假設「email 永不變」或「一定能從 Google 拿到」來做唯一對應（需搭配驗證與業務規則）。

---

## 2. 作法 A：同一張 `users`（與本次 `init_db.sql` 一致）

對 **全新初始化**（`init_db.sql`）的約定：

| 變更 | 說明 |
|------|------|
| **`password_hash`** | 改為**可為 NULL**。純 Google 帳號沒有本地密碼。 |
| **`google_sub`** | `TEXT`、`UNIQUE`、可為 **NULL**。有值代表已與某 Google `sub` 綁定；NULL 代表尚未綁定 Google。PostgreSQL 的 **UNIQUE 允許多筆 NULL**（互不衝突）。 |
| **`email_verified`** | `BOOLEAN`，預設 `FALSE`。之後若以 Google email 填入，可依 Google claims 設為 true（實作時再對齊）。 |
| **`last_login_provider`** | 可為 NULL；登入流程可寫入 `'password'` 或 `'google'` 方便稽核／除錯。 |
| **`CONSTRAINT users_has_password_or_google`** | 任一筆資料須滿足 **`password_hash IS NOT NULL` 或 `google_sub IS NOT NULL`**（至少要有一種可驗的身分來源）。 |

**密碼註冊使用者**：`password_hash` 有值、`google_sub` NULL。

**Google 首登自動建帳**（將來由 callback 實作）：`google_sub` 有值、`password_hash` NULL，`email`/`username` 由後端規則產生（例如自 email 截取或來自 Google 名稱 + 確保 username 唯一）。

**帳號合併**（將來議題）：同一 `email` 已存在本地密碼帳號、又用 Google 登入時，要明確決定禁止／需驗證後手動綁定／自動綁定，避免被搶信箱。

---

## 3. 重新導向 URI（Callback）對應的 API 怎麼設計（提要）

這與先前架構文件一致；此處只列摘要，方便對齊 Google Cloud Console：

1. **`GET`**（例如 **`/api/user/auth/google/start`**）  
   產生 **`state`**（與選用的 PKCE verifier）存入 **HttpOnly Cookie** 或 server session → **302** 到 Google 授權網址。

2. **`GET`**（你在 Console 登记的 **Authorized redirect URIs**，例如 **`http://localhost:8000/api/user/auth/google/callback`**）  
   Query：`code`、`state`。驗 **`state`** → 用 **`code`** 換 token → 讀 **`sub`、`email`** 等 → **Upsert `users`**（以 **`google_sub`** 查詢）→ **沿用現有**「簽發 AT + 寫入 `refresh_tokens` + `Set-Cookie`」邏輯 → **302** 回前端（例如 `/` 或 `/index.html`），讓 `auth.js` 用 RT 換 AT。

**注意**：Callback 必須是 **GET**；URL 必須與 Google Console **完全相同**（含 port、path、http/https）。

---

## 4. 全新安裝 vs 既有資料庫

- **全新 volume / 第一次跑 Postgres init**：`init_db.sql` 會建立新表結構，**不用再手動 ALTER**。
- **已存在 `db_data` volume**（資料庫早就在跑）：修改 `init_db.sql` **不會**自動重跑。必須在 DB 上手動執行 **§5 的 Migration SQL**。

---

## 5. 進入 Docker 並對 PostgreSQL 下指令（Migration）

專案 `deploy/docker-compose.yml` 內：

| 服務名稱 | 用途 |
|----------|------|
| **`db`** | PostgreSQL 16（資料在 volume `db_data`） |
| **`backend`** | FastAPI |

在專案**根目錄**（`Stock-Insight-Chat`，內含 `deploy/docker-compose.yml`）執行。

### 5.1 確認容器已啟動

```bash
docker compose -f ./deploy/docker-compose.yml ps
```

若你安裝的是舊版二進制，可把 `docker compose` 改成 `docker-compose`。

### 5.2 進入 **`db` 容器**並開互動式 `psql`

```bash
docker compose -f ./deploy/docker-compose.yml exec db psql -U postgres -d Stock_Insight_Chat
```

在 `psql` 裡可貼上 §5.4 的 SQL；離開：`\q`。

### 5.3 不必進交互式、`psql -c` 單次執行

```bash
docker compose -f ./deploy/docker-compose.yml exec db \
  psql -U postgres -d Stock_Insight_Chat -c "SELECT 1;"
```

### 5.4 **既有資料庫**適用的 Migration（作法 A）

在 **`db`** 容器內用 `psql` 執行（或由主機 `-c` 傳入整段指令）。請在**備份後**於維護窗口操作。

```sql
-- 1) 本地密碼改為可空（OAuth 專用帳號將不存密碼）
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- 2) Google OIDC Subject（可与 NULL 並存多筆；非 NULL 值唯一）
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_users_google_sub
  ON users (google_sub)
  WHERE google_sub IS NOT NULL;

-- 3) 選用欄位
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_provider VARCHAR(32);

-- 4) 資料完整性：至少要有密碼或 Google sub（若表中已有異常資料，此步會失敗，需先手動清理）
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_has_password_or_google;
ALTER TABLE users ADD CONSTRAINT users_has_password_or_google CHECK (
  password_hash IS NOT NULL OR google_sub IS NOT NULL
);
```

> **備註**：若未來你希望 `email_verified` / `last_login_provider` 的預設與程式寫入行為完全一致，請在實作 Google callback 後再跑一次資料修補或預設值調整。

### 5.5 進入 **`backend`** 容器（除錯用）

```bash
docker compose -f ./deploy/docker-compose.yml exec backend sh
```

在內可看環境變數、手動跑 Python 等；**資料庫 schema 仍建議一律用 **`db` + `psql`**。

---

## 6. Google Cloud Console 對照（本專案 README 假設）

- **前端登入頁**：`http://localhost/login.html` → **Authorized JavaScript origins** 常填：**`http://localhost`**（不含 path）。
- **Callback（後端對外埠）**：常填：**`http://localhost:8000/api/user/auth/google/callback`**（路徑以實作為準；需與 `GOOGLE_OAUTH_REDIRECT_URI` 與程式一致）。

---

## 7. 相關檔案

| 檔案 | 說明 |
|------|------|
| `app/backend/database/init_db.sql` | **新環境**的 `users` 表已定義 **`password_hash` 可 NULL、`google_sub`、`email_verified`、`last_login_provider`** 與 **CHECK**。 |
| `deploy/docker-compose.yml` | **`db`** 服務：`exec db psql ...`。**`backend`** 埠 `8000:8000**。 |
| `specifications/auth_system_spec.md` | 既有 JWT / RT Cookie 規格 |
