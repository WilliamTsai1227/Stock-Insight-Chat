# 筆記（Notes）

把一段對話交給 LLM 整理成筆記，存進使用者自己的筆記頁。

目前是**最小可用版本**：條列式筆記頁。白板（多節點、可拖曳、像 Figma）是下一階段，
資料表已預留座標欄位，長成白板時不需要再做一次 migration。

## 1. 資料模型

`database/migrations/V009__notes.sql`（已同步進 `init_db.sql`）

| 表 | 說明 |
| --- | --- |
| `note_pages` | 筆記頁。一個使用者可有多頁，每頁是一塊獨立空間 |
| `notes` | 單則筆記。`canvas_x/y/w/h` 保留給白板階段，本階段全為 NULL |

幾個刻意的設計：

- `notes.user_id` 對 `note_pages.user_id` 是冗餘的，但讓「我的所有筆記」不必 join，
  也讓每個查詢都能直接拿 `user_id` 當授權條件
- `source_chat_id` 用 `ON DELETE SET NULL`：對話被刪除時筆記要留著
- `source_message_ids` 存成 JSONB，記錄這則筆記是從哪幾則訊息整理來的

上限：每人 50 頁、每頁 500 則筆記、單次最多整理 50 則訊息、來源 48,000 字
（超過從最舊的開始截斷）。

## 2. API

全部需登入，`user_id` 一律取自 JWT。找不到資源一律回 404，不區分「不存在」與
「不屬於你」，避免洩漏他人的資源 id。

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| GET | `/api/notes/pages` | 列出筆記頁（含每頁筆記數） |
| POST | `/api/notes/pages` | 新增筆記頁 |
| PATCH | `/api/notes/pages/{id}` | 重新命名 |
| DELETE | `/api/notes/pages/{id}` | 刪除（底下筆記 CASCADE） |
| GET | `/api/notes?page_id=` | 列出筆記 |
| POST | `/api/notes/generate` | **把選取的訊息整理成筆記** |
| PATCH | `/api/notes/{id}` | 編輯標題／內容，或搬到另一頁 |
| DELETE | `/api/notes/{id}` | 刪除筆記 |

頁面標題套用與 `project.py` 相同的白名單正則；筆記**內容**不套
（它是 LLM 產生的 Markdown，由前端既有的消毒渲染路徑處理）。

### 2.1 `/api/notes/generate` 的安全要點

查詢來源訊息時同時限定 `chat_id` 與該 chat 的擁有者 —— 只比對 `message_id`
的話，帶別人的 id 就能把他人對話內容撈出來。

### 2.2 計費

與 `/chat/messages`、深度研究一致：發 LLM 前 `assert_preflight_llm_quota()`
（超額回 429），結束後 `record_token_usage(caller="notes_generate")`。

LLM 走 `astream()` 而非 `ainvoke()`：專案共用的 `StreamUsageChatOpenAI` 帶著
`stream_options`（OpenAI 只允許串流時使用），而 token 用量正是靠串流結尾那個
usage chunk 取得 —— 走串流才接得上既有計費。筆記不需即時顯示，收完再一次回傳。

## 3. 前端

| 檔案 | 說明 |
| --- | --- |
| `js/notes.js` | 視圖切換、筆記頁／筆記 CRUD、對話選取模式 |
| `css/notes.css` | 只用 `index.css` 的設計 token，深淺主題自動跟著切換 |

### 3.1 選取模式

對話標題列的 ✎ 按鈕進入選取模式：每則訊息右側出現勾選框，勾完按浮動動作列的
「整理成筆記」。`Esc` 或切換視圖都會退出。

浮動動作列掛在 `.input-container` 底下（它已是 `position: relative`），
而不是讓 `.main-content` 變成定位基準 —— 後者會影響其他既有的 absolute 元素。

### 3.2 訊息 id 怎麼來

後端的 SSE `done` 事件在 assistant 訊息寫進 DB **之前**就送出，所以剛對話完的
氣泡沒有 `data-message-id`。`syncMessageIds()` 在進入選取模式時向
`GET /api/chat?chat_id=` 取權威清單，依序補上（DOM 與 API 的訊息順序一致）；
數量對不起來就不猜，提示使用者重新整理。

歷史訊息方面，`addMessageToUI()` 已支援 `options.messageId`，載入歷史時直接帶入。

## 4. 下一階段：白板

資料面已就緒（`canvas_*`）。要補的是：

- 畫布互動：pan / zoom / 節點拖曳 / 連線 / 多選
- `note_edges` 表（若要節點連線）
- 位置的批次儲存 API

前端目前是 vanilla JS、無框架無打包；自己刻畫布約 1500～2500 行，
或改用 CDN UMD 的畫布庫（會是專案第一個第三方框架，需另行決定）。
