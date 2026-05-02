# 📋 專案開發待辦清單 (Project TODO List)

## 🔐 身份驗證系統 (Authentication)
- [x] **資料庫優化**: 為 `init_db.sql` 加入關鍵欄位索引 (Index)。
- [x] **系統架構設計**: 定義 JWT (AT/RT) 雙權杖認證流程。
- [x] **安全性工具**: 實作 `security.py` (Argon2 雜湊、JWT 簽發與驗證)。
- [x] **ORM 模型**: 建立 `User` 與 `RefreshToken` 的 SQLAlchemy 模型。
- [x] **API 實作**:
    - [x] 使用者註冊 (`/api/auth/register`) - 含配額初始化。
    - [x] 使用者登入 (`/api/auth/login`) - 含 Cookie 寫入。
    - [x] 使用者登出 (`/api/auth/logout`) - 含資料庫 Token 撤銷。
- [x] **自動化測試**: 建立 `test_auth_api.py` 整合測試腳本。
- [ ] **環境修復**: 解決主機 Port 5432 衝突問題 (Local Postgres vs Docker)。
- [ ] **權杖刷新**: 實作接口，使用 RT 換取新 AT。
- [ ] **權限保護**: 將現有的 API 加上 JWT 驗證裝飾器。

## 📈 核心功能擴充
- [ ] **會員資料**: 實作 `/api/user/profile` 與 `/api/user/usage` 接口。
- [ ] **歷史紀錄**: 實作對話分頁查詢功能。
- [ ] **Google SSO**: 串接 Google OAuth2 登入。

## 🎨 前端開發 (Frontend)
- [ ] **權杖管理**: 實作前端自動刷新 Token 的攔截器 (Interceptor)。
- [ ] **配額顯示**: 顯示剩餘 Token 用量頁面。
- [x] **Parked staging 防呆**: 並行對話離開視圖時封存 DOM 上限 `MAX_PARKED_STAGING_CHATS`（依 Map 插入序 FIFO 剔除最舊、並 `abort` 對應 fetch）；刪專案成功後對該專案底下 chat id 呼叫 `evictParkedPane`，避免 CASCADE 後殘留 parked。

## 多對話 SSE／Parked 行為備忘（前端）

### CASCADE 後清 parked｜重新載入是否足够？

- **會。** 整頁重新載入後，記憶體中的 `Map`、進行中的 `fetch`、`#stream-staging` 底下的 DOM 都會重置，不存在「孤兒 parked」殘留在上一輪 SPA 生命的問題。
- **不必 F5：** `confirmDeleteProject` 在呼叫 DELETE **前**快取 `(state.chats[project.id] || []).map(...)` 的 chat id；**成功後**對每個 id 呼叫 `evictParkedPane`（`abort` 對應串流、`streamingChatIds` 與 parked wrapper 清除）。若 `currentChatId` 落在被刪專案的 chat 清單内，並清空並回到空白聊天區，避免 CASCADE 後仍留 staged DOM。

### Staging 數量上限（產品防呆）

- **`MAX_PARKED_STAGING_CHATS = 8`（可調）。** 每多 park 一段即檢查；超過時對 **`parkedPaneByChatId.keys().next()`**（Map **插入順序**裡最先進來的）反覆剔除，並 **`abort` 該對話的 SSE**、呼叫 `updateSendButtonForStreamingState()`。
- 此為 **FIFO**（最先被停進 staging 的最先 eviction），較贴切「封存時間最久的先釋放」；若需要嚴格 **LRU**（每次回到／unpark 某對話就視為「最近使用」，應延後剔除），需在「再接回該對話」時對 Map 做一次 **delete + set** 讓鍵順序排到最後；**目前程式未做 LRU**，僅 FIFO。

### 多對話切換／並行串流／畫面是否各自獨立？

- **設計上：** 每則對話的活躍區塊是同一棵 DOM 子上 **.closure 鎖住的 `msgDiv`／`bubble`／`toolsContainer`**，SSE loop 永遠寫同一批節點；離開視圖時 **`parkViewportFor` 將整個 `#chat-messages` 內容移到 `#stream-staging`**（CSS 隱藏仍掛在 document），故背景串流仍會更新 **該對話自己的** 子樹，不會寫到別人的節點。
- **`navigateToChat`：** 先對「上一則」`maybeParkViewportForLeavingChat`（正由 **含 await fetch 與 reader** 的 `streamingChatIds` 驅動），再切 `currentChatId`；若目標在 parked 有封存則 **unpark** 接回，否則 **GET 歷史**。
- **已補競態（實作於 `index.js`）：**
  1. **在 `POST /chat/messages` 的 `await authFetch` 之前就** 把 `streamTargetChatId` 加入 `streamingChatIds`，確保「fetch 尚未回來就切換」也會 park，不被 `loadChatHistoryIntoView` 清掉半截 UI。
  2. **`chat_id` POST body** 改用 **`streamTargetChatId`**，避免 await 間使用者換對話而把訊息掛錯後端對話。
  3. **`loadChatHistoryIntoView`** 若回應晚到且 `state.currentChatId` 已不再等於請求的 `chatId`，**不寫入主視窗**，避免歷史畫面互蓋。
- **仍請注意：** 同一則對話並未在 UI 強制禁止「換離後再對同一對話發第二則」（後端若限制併發需另議）；FIFO 超限會 **強制中止**最舊 parked 的請求／畫面。

### 已知邊際情況與後續優化（待辦）

- [ ] **scrollToBottom 與 parked 背景串流**：`scrollToBottom()` 目前只對 `#chat-messages`（前景主欄）捲動。對話 **parked** 後 SSE 若在背景繼續跑，程式仍會呼叫 `scrollToBottom()`，對 **隱藏中的 parked 樹**沒有助捲，卻可能讓 **前景**聊天區 **偶發輕微波動**。可選優化：`scrollToBottom` 帶「目標 chatId」或与 `currentChatId`/`streamTargetChatId` 比對後再捲，或對 parked wrapper 另行捲動（若仍需預渲染捲軸）。
- [ ] **`MAX_PARKED_STAGING_CHATS` 超限與 UX**：超限時 **FIFO 踢最舊並 `abort`** 為刻意取捨，不是無限並行。可選：**Toast／站內提示**「封存上限已回收最舊對話」，或讓使用者自行選擇要關閉哪一則 streamed 對話後再開始新的。
- [ ] **串流內 Markdown 組字**：`token`/`done` 路徑仍使用 **`innerHTML` + `renderMarkdown`**（沿用既有作法），與 **XSS** 及「前端避免 `innerHTML`」的工程規約有張力。**後續可改**：sanitize 後再寫 DOM，或改用安全 API 組 Markdown 輸出樹。
- [ ] **Parked 改為 LRU eviction**：目前是 Map **FIFO**。**LRU**：在 **`unparkViewportFor`**（或每次「接回」該 parked 對話）對 `parkedPaneByChatId` 做 **`delete(chatId)` 再 `set(chatId, wrapper)`**，讓鍵順序排到「最後」以延後被踢。**小改動**即可接在現有 eviction 後面。
- **架構備註（低優／資訊性）**：`index.html` 僅 **`#chat-messages`**，`#stream-staging` 由 **`index.js`** 動態插入；`index.css` 對 `.stream-staging` **off-screen** 隱藏。此結構 **不會**多重聊天區 **`id`** 混在一起，對「渲染錯區」並無結構性風險，通常 **不需**為此再改骨架。

## 方向 C：折衷設計備忘 — 長 SSE + 斷線後後端仍跑完並寫入

### 問題與目標

- **問題**：僅倚賴單一路徑 SSE 時，瀏覽器重整／關分頁／網路中斷後，前端拿不到串流結果；若尚未把 assistant 段落落檔，使用者會覺得「回答不見」。
- **目標**：HTTP／SSE **可斷**，但同一則對話上的 **後端推理仍會跑完並寫入**（至少是完整 assistant message，或可還原的 checkpoint）。使用者稍後再打開同一 chat，應能從 DB（或載入流程）看到 **已定稿或接近定稿** 的內容，而不必只靠「運氣接上串流」。

### 作法概念（與現有 SSE 並存）

1. **與請求生命週期解耦**：`POST /messages`（或對應端點）在驗證通過並建立 user／job 紀錄後，將「整段 agent 執行」丟進 **背景任務**（例如 `asyncio.create_task`、佇列 worker、或未來 Celery／RQ），不必與連線著的 SSE promise 鎖在同一條 await 鏈。
2. **SSE 的定位**：SSE 仍可作為 **最佳努力** 的即時進度／token 輸出；Client 斷線時後端不中斷（或可依策略中止以省成本），但以 **持久化結果** 為準。
3. **取得結果的兩種路徑**：  
   - **仍連著**：繼續收 SSE（與現在類似）；  
   - **已斷線**：重整後透過 **`GET`/分頁** 拉回已寫入的 messages（與前台 parked／上限策略互補：parked 管 DOM，方向 C 管「真相來源」在 DB）。
4. **進階變形（可選）**：首包回 **`job_id`**，事件改用 **polling** 或 SSE 再接回 subscription，降低對單連線的依賴。

### 優點與適用時機

- **優點**：大幅緩解「重整就沒 assistant」的痛點；與現有長連線相容，可分階段導入。
- **適用**：尚未要上完整分散式 Job／多 worker 時，可先 **在同一 process** 做出「斷線仍落 assistant」的行為。

### 風險與需補的護欄（與「高併發」強相關）

- **背壓與濫用**：若允許無限背景 task，惡意或失手大量送出會堆滿佇列。**需**：每使用者／每 chat **併發上限**、**逾時**、**取消語意**（與 Abort、client disconnect 對齊）。
- **資源上限**：單 process 下同時長任務過多會擠 CPU、LLM / token quota；應有可觀測計數器與 429／佇列滿的回應策略。
- **一致性**：SSE 半途斷線時 UI 進度可能與最終 DB 內容不同；產品行為要定義：**以 DB（或伺服器視窗）為最終準**。
- **與 parked 分流**：Parked staging 只管 **多端 SSE＋前端 DOM**；方向 C 保證 **後端仍可完賽並可查**，兩者不衝突，但實作時避免「只靠前端 unpark」當成品交付。

---
*最後更新日期: 2026-05-02*
