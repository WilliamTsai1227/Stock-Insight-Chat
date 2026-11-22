# Stock Insight Desktop v0.1.0

Windows x64 桌面客戶端：Electron + 現有 HTML/CSS/JavaScript 前端。
不會啟動 Python、Docker 或本機資料庫，使用者需要網路連線。

## 架構與金鑰

```text
Windows 安裝檔
  └─ Electron + 打包的 app/frontend
       ├─ HTTPS REST / SSE → 雲端 FastAPI → AI、資料庫、Qdrant、Kinetic
       └─ 系統瀏覽器 → Google → 雲端 callback → 一次性授權碼 → 桌面 Session
```

- OpenAI、Google Client Secret、JWT SECRET_KEY、資料庫密碼、MCP 金鑰，仍放雲端的環境變數或 Secret Manager。
- `desktop.config.json` 只有公開的前端 origin 和 API URL，不可放秘密；安裝檔內容可被使用者解開。
- 桌面只保存現有的使用者設定／profile 和 Chromium 管理的 HttpOnly Refresh Token Cookie。Access Token 延續現有前端機制，保存在記憶體。
- 桌面有獨立的持久 Session，和系統瀏覽器的 Cookie 分開；OAuth 不會把瀏覽器的 Refresh Token 複製給桌面。
- 本版固定載入打包的前端資產；前端更新須重新發安裝檔，後端更新仍在雲端部署。
- 字型、Lucide、Markdown 等沿用網頁版的 CDN 來源，仍需要網路；本版沒有離線 AI 或自動更新。

Electron 在專用 Session 攔截前端 HTTPS origin，將 UI 路徑回應為安裝檔內的資產。
API、`/explore/` 和 CDN 請求照常走網路，保留 TLS、CORS、SameSite 和 HttpOnly 的行為。
這讓現有的 `/login`、分享網址、相同站點 Cookie 與探索 iframe 可繼續使用，不必停用瀏覽器安全檢查。

## 先部署雲端登入支援

1. 先在既有的 Insight PostgreSQL database 執行：

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f app/backend/database/migrations/V014__desktop_login_codes.sql
   ```

2. 雲端 backend 的環境變數加入：

   ```dotenv
   DESKTOP_AUTH_ENABLED=true
   COOKIE_SECURE=true
   FRONTEND_URL=https://insight-chat.xyz
   CORS_ALLOWED_ORIGINS=https://insight-chat.xyz
   ```

   原有的 Google Client ID／Secret、`GOOGLE_OAUTH_REDIRECT_URI`、其他 API keys 均留在雲端。
   CORS 若已有其他 origin，保留它們並以逗號加入桌面設定的 frontend origin。

3. 部署新版 backend。確認 `GET https://api.insight-chat.xyz/api/public/desktop-config` 回傳 `{"enabled":true}`。

**Google Console 仍使用現有雲端 callback URL**，不必新增每台電腦的 localhost port 或把 Client Secret 放進安裝檔。
Google 登入使用系統預設瀏覽器；後端驗證成功後才把自有的一次性 code 導回 `127.0.0.1` 的隨機 port。
桌面以 S256 PKCE verifier 換成自己的 HttpOnly Session；code 有效 60 秒，原子消費一次，桌面等待上限 5 分鐘。

未設定 `DESKTOP_AUTH_ENABLED=true` 時，既有網頁登入保持原本行為，桌面登入會提示尚未啟用。

### 安全修補的部署要求（2026-11-22）

- 必須重新部署 backend / frontend 並重建桌面安裝檔，工作區修補才會生效。
- `SECRET_KEY` 必須明確設定，至少 32 bytes；空值或舊開發預設金鑰會阻止啟動。
- PostgreSQL TLS 現在驗證 CA 與 hostname。RDS / 私有 CA 若不在系統信任庫，需掛載 PEM bundle，並設定容器內的 `DATABASE_SSL_CA_FILE`；不再跳過憑證驗證。
- 正式環境維持 `ALLOW_DEV_ORIGINS=false`，以 `CORS_ALLOWED_ORIGINS` 列出實際前端網址。區網開發才明確開啟。
- 舊 Access Token 沒有 `type=access`，更新後會被拒絕；有效 Refresh Token 可正常換發。
- 報告 iframe 使用不含 `allow-same-origin` 的 sandbox；桌面新視窗另外使用暫存 session 與 opaque origin。簡報播放器仍保留翻頁及講稿功能。
- 舊 `/api/files/upload` 與 DELETE stub 未實作儲存與權限驗證，改回 501；對話附件流程不受影響。
- 既有後端相依套件仍有待升級項目，詳見 `test/security/README.md`；本次修補不代表整個相依樹已無安全公告。

## 本機執行與 Windows 打包

使用 Node.js 24 LTS。在 Windows PowerShell：

```powershell
cd app/desktop
npm ci
npm test
npm start
# 產生 Windows x64 安裝檔
npm run dist:win
npm run verify:package
```

輸出：`app/desktop/dist/Stock-Insight-0.1.0-Windows-x64-Setup.exe`。
使用者安裝後直接登入，不需要安裝 Node、Python 或 Docker。

公開網址預設沿用專案現有的 `insight-chat.xyz`／`api.insight-chat.xyz`。若正式前端其實使用 `www` 或不同網域，**打包前要改成與後端 CORS／FRONTEND_URL 一致的實際 origin**：

```powershell
$env:DESKTOP_FRONTEND_ORIGIN = "https://app.example.com"
$env:DESKTOP_API_BASE = "https://api.example.com/api"
npm run dist:win
```

也可以直接編輯 `desktop.config.json`；prepare 會把正規化後的公開設定寫回這個檔案。
前端和 API 請使用同一站點的 HTTPS 網域，例如 `app.example.com` 與 `api.example.com`，以配合現有 `SameSite=Lax` Cookie。
此版本只接受 HTTPS；本機開發用的 HTTP backend 不是桌面預設目標。

Mac 可以用 `npm start` 和 `npm run pack` 驗證客戶端，但 Windows 安裝檔建議在 Windows 上建置。
專案已附 `.github/workflows/desktop-windows.yml`：推送程式碼後在 Actions 手動執行 **Build Windows desktop**，
完成後下載 `Stock-Insight-Windows-x64` artifact。此工作流程只建置和提供下載，不發布 Release。

## 驗證與發佈

- `npm test`：資產路徑隔離、公開設定驗證、桌面／網頁 API 選擇、loopback state、取消及逾時。
- `npm run smoke`：實際啟動 Electron，驗證打包介面、隔離的 preload、頁面路由和 Session Cookie。
- `npm run verify:package`：檢查實際 `app.asar`，確認前端存在，沒有混入 `.env`、後端、SQL、Python 或私鑰檔。
- 後端測試：`test/backend/test_desktop_auth.py`，涵蓋 redirect 白名單、簽章 context、PKCE、過期／重放、雲端開關、HttpOnly Cookie 與停用帳號。

Windows 實機還需要驗收：Google 成功／取消登入、關閉重開後續用 Session、登出、SSE 對話、上傳／下載和探索 iframe。
沒有 Windows 簽章憑證的安裝檔可能出現 SmartScreen「未知發行者」提示；正式對外發佈前請加上 Windows code signing。

官方參考：[Electron Protocol](https://www.electronjs.org/docs/latest/api/protocol/)、
[Electron Security](https://www.electronjs.org/docs/latest/tutorial/security/)、
[Google OAuth Policies](https://developers.google.com/identity/protocols/oauth2/policies)、
[electron-builder 多平台打包](https://www.electron.build/docs/multi-platform-build/)。
