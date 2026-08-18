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
├── css/index.css, css/login.css
└── js/
    ├── api-config.js       ← API base URL 解析（見 §5）
    ├── auth.js             ← AT 記憶體保存、自動 refresh
    ├── index.js            ← 主應用邏輯（SSE、對話、專案、回饋）
    ├── login.js
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
