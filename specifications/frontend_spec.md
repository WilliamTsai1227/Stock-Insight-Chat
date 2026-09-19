# 前端規格說明書 (Frontend Specification)

前端採用 **玻璃擬態 (Glassmorphism)** 深色主題 UI，為**純靜態檔**（無打包步驟、無框架）。

> **部署**：生產環境不打包 image，靜態檔直接上傳 **S3 + Cloudflare CDN**；`frontend.Dockerfile` 與 `deploy/nginx/` 僅供本機開發。流程見 [`release_handbook.md`](./release_handbook.md) §2。

## 1. 核心設計語彙

*   **視覺風格**: 半透明背景 (Backdrop Filter)、細緻描邊、深色主題、Outlined Icons。
*   **字體**: `Outfit` (標題) 與 `Inter` (內文)。
*   **互動**: 真 SSE 串流逐字渲染、Loading 思考動畫、Marked.js Markdown 渲染。

## 2. 檔案結構

```
app/frontend/
├── index.html          ← 主應用（對話、專案、探索、回饋）
├── login.html          ← 登入頁（Google SSO + 法遵條款）
├── manifest.json       ← PWA manifest（見 §8）── 必須位於站台根目錄
├── sw.js               ← Service Worker（scope `/`）── 必須位於站台根目錄
├── icons/              ← PWA / apple-touch-icon / favicon（PNG）
├── css/index.css, css/login.css, css/deep-research.css
└── js/
    ├── api-config.js       ← API base URL 解析（見 §5）
    ├── pwa.js              ← SW 註冊、theme-color 同步、iOS 加入主畫面提示（見 §8）
    ├── auth.js             ← AT 記憶體保存、自動 refresh
    ├── index.js            ← 主應用邏輯（SSE、對話、專案、回饋）
    ├── login.js
    ├── deep-research.js    ← 深度研究模組
    └── legal-content.js    ← 服務條款／隱私權內容
```

## 3. 主要頁面結構

### 3.1 登入頁 (`login.html`)

*   **Google SSO 按鈕**：導向 `GET /api/user/auth/google/start`。**無本地密碼註冊／登入**。
*   **法遵區塊**：服務條款與隱私權內容由 `legal-content.js` 注入。

### 3.2 主應用 (`index.html`)

*   **Sidebar**:
    *   **PROJECTS**: 專案列表與切換（可建立、刪除；**目前無改名功能**）。
    *   **CHATS**: 專案下的對話列表，以及不屬於任何專案的「最近」對話。
    *   **探索 (Explore)**: 以 iframe 嵌入 `/explore/`，由後端代理至 kinetic 容器；需登入 Cookie 才會通過。
    *   **User Profile**: 目前使用者、等級與 Token 用量。
*   **Main Chat**:
    *   **Welcome Hero**: 初次進入顯示的歡迎畫面。
    *   **Message Bubbles**: 區分 User 與 AI 氣泡。
    *   **ReAct Trace Card**: 摺疊式面板，顯示 Agent 每一輪的 `Thought` 與 `Tool Calls`（僅思考模式）。
    *   **Sources Card**: 顯示該回答引用的原始新聞或報告來源，點擊可跳轉至原文網址。
*   **Input Area**:
    *   **Auto-expanding Textarea**: 根據輸入內容自動調整高度。
    *   **模式切換**: `chat_mode`（股市 Agent / 一般對話）與 `response_mode`（思考 / 快捷 Flash）。
    *   **Tool Control Popover**: 手動切換「自動模式」或指定 `enabled_tools`（4 個工具，見 [`tools_spec.md`](./tools_spec.md)）。
*   **建議回饋表單**: 類型 + 內容，自動附帶 `page_url` / `user_agent` / `context`；可選 Cloudflare Turnstile CAPTCHA（依 `GET /api/public/feedback-config` 決定是否渲染）。

## 4. 技術棧

*   **核心**: HTML5, Vanilla JavaScript（無框架、無打包）。
*   **樣式**: Vanilla CSS (使用 Flex/Grid 與 CSS Variables)。
*   **第三方庫**:
    *   `lucide-icons`: 向量圖示。
    *   `marked.js`: Markdown 解析。
    *   `fetch API`: 與後端 FastAPI 通訊。
    *   Cloudflare Turnstile（回饋表單，可選）。

## 5. API base URL 解析 (`api-config.js`)

```js
window.STOCK_INSIGHT_API_BASE   // 最優先：完整 URL 或 '/api'
window.API_BACKEND_PORT         // 本機 dev 的 backend port（預設 8000）
```

解析順序：

1.  有 `window.STOCK_INSIGHT_API_BASE` → 直接用（去尾斜線）。
2.  **HTTPS** → 同源 `${origin}/api`。
3.  **HTTP**（本機／區網 dev） → `${protocol}//${hostname}:${backendPort}/api`。

> ⚠️ **生產環境必須設定 `window.STOCK_INSIGHT_API_BASE`。** 前端在 S3/Cloudflare（例如 `app.example.com`）、API 在 EC2（例如 `api.example.com`），兩者**不同源**；第 2 條規則的同源 `/api` 在這個架構下是打不到的。請在載入 `api-config.js` **之前**設定覆寫：
>
> ```html
> <script>window.STOCK_INSIGHT_API_BASE = 'https://api.example.com/api';</script>
> ```

## 6. 認證與串流

*   **AT 存於 JS 記憶體變數**（不落 localStorage，降低 XSS 風險）；頁面刷新後由 `auth.js` 的 `tryRefreshToken()` 用 RT Cookie 重新換發。
*   **RT 為 HttpOnly Cookie**（`SameSite=Lax`），JS 讀不到。
*   **SSE 串流**：`POST /api/chat/messages` 以 `fetch` + `response.body.getReader()` 讀取，**不是 `EventSource`**（`EventSource` 只支援 GET，無法送 body）。事件型別見 [`api_spec.md`](./api_spec.md) §4。

## 7. 特色實作

*   **Stock Ticker Highlighting**: 自動偵測內容中的 4 位數字股票代碼，包裝成 `.stock-ticker` 高亮標籤。
*   **並行對話 / Parked staging**: 對話離開視圖時封存其 DOM，上限 `MAX_PARKED_STAGING_CHATS`（依 Map 插入序 FIFO 剔除最舊，並 `abort` 對應 fetch）；刪除專案成功後對其底下 chat id 呼叫 `evictParkedPane`，避免 CASCADE 後殘留。細節見 [`todo.md`](./todo.md)。

## 8. PWA（加到主畫面 / 獨立視窗）

網站可安裝成 App：iOS Safari「分享 → 加入主畫面」後，從圖示點開會是**獨立視窗**（沒有網址列與分頁列），Android / 桌面 Chrome 則可直接安裝。

### 8.1 組成

| 檔案 | 作用 |
| --- | --- |
| `manifest.json` | `display: standalone`、`start_url: /`、`scope: /`、名稱、主題色、4 個 icon |
| `sw.js` | Service Worker。導覽用 network-first、靜態資源用 stale-while-revalidate；跨網域（CDN、`api.*`）與 `/api/`、`/explore/`、SSE 一律不攔 |
| `js/pwa.js` | 註冊 SW、同步 `<meta name="theme-color">`、iOS「加入主畫面」提示 |
| `icons/*.png` | `apple-touch-icon`(180，不透明全出血)、192/512(`any`)、512(`maskable`)、favicon 16/32 |

圖示由 `deploy/generate_pwa_icons.py` 產生（已 commit，只有要改配色或造型時才需重跑）。

SW 的目的是**啟動變快 + 獨立視窗外殼**，不是離線可用：marked / DOMPurify / lucide / highlight.js 都在第三方 CDN（跨網域，SW 不攔），對話本身也一定要連後端 API。斷網時只會拿到快取的頁面外殼。

`manifest.json` 與 `sw.js` **必須放在站台根目錄**：SW 的控制範圍不能超出自己所在的路徑，放進子目錄就管不到 `/`。

### 8.2 iOS 相關細節

*   `apple-mobile-web-app-capable: yes` — 舊版 iOS 靠這個才會用獨立視窗開啟。
*   `apple-mobile-web-app-status-bar-style: default` — 內容排在狀態列**下方**，狀態列底色跟著 `theme-color`。刻意不用 `black-translucent`：那會強制白色狀態列文字，淺色主題會看不見。
*   `theme-color` 由 `pwa.js` 的 `syncPwaThemeColor()` 隨深／淺色切換更新，`index.js` 與 `login.js` 的 `applyUiTheme()` 都會呼叫它。
*   viewport 加了 `viewport-fit=cover`，`env(safe-area-inset-*)` 才會回傳非 0；CSS 一律用 `max(原值, env(...))`，在沒有瀏海的裝置上等於維持原樣。
*   從主畫面開啟時 `<html>` 會掛上 `.pwa-standalone`，需要針對「像 App」情境調整版面時可以用。
*   **儲存空間是獨立的**：主畫面 App 與 Safari 不共用 Cookie／localStorage，安裝後需要重新用 Google 登入一次。

### 8.3 「加入主畫面」提示

iOS 沒有安裝提示 API，只能自己講。`pwa.js` 會在**同時**滿足下列條件時，於登入頁底部顯示一則可關閉的提示：

*   `<body>` 帶 `data-pwa-install-hint="on"`（目前只有 `login.html` 有）
*   iOS Safari（非 Chrome/Firefox iOS，步驟不同）
*   尚未從主畫面開啟
*   使用者沒按過關閉（記在 `localStorage.insightA2hsHintDismissed`）

要讓主應用也顯示，把同一個 `data-pwa-install-hint="on"` 加到 `index.html` 的 `<body>` 即可。

### 8.4 改前端後必做

`sw.js` 的 `VERSION` 常數要加一，否則舊的 `insight-static-*` 快取不會淘汰，使用者可能拿到舊的 css/js。導覽（HTML）走 network-first，所以頁面本身不會卡舊版。
