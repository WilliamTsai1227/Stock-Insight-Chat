# 深度研究（Deep Research）

以 OpenAI Agents SDK 的 hosted tools（`WebSearchTool` / `FileSearchTool`）做一次性研究：
上傳 → 研究 → 產出報告／簡報 → 下載。session 只在記憶體，不寫 DB。

> 早期規劃的 NotebookLM 式持久化知識庫（Qdrant + S3 + `research_workspaces`）沒有實作，規劃文件已移除；
> 需要時可從 git 歷史的 `specifications/deep_search.md` 取回。

> 定位是**功能驗證**：先把「上傳 → 研究 → 產出報告／簡報 → 下載」整條路走通，
> 確認體驗與成本可接受之後，再決定要不要做持久化的知識庫版。

## 1. 與早期知識庫規劃的差異

| 面向 | 知識庫規劃（未實作） | 現況（已實作） |
|------|---------------------|-------------------|
| 檢索 | 自建 Qdrant + BM25 + RRF | OpenAI 托管的 `FileSearchTool` |
| 網路 | 未涵蓋 | OpenAI 托管的 `WebSearchTool` |
| 原始檔 | S3 永久保存 | 只在研究期間存在於 OpenAI 臨時 vector store，結束即刪 |
| 中繼資料 | PostgreSQL `research_workspaces` 等表 | **完全不寫 DB**，記憶體 session |
| 生命週期 | 使用者手動刪除 | TTL 120 分鐘 / 後端重啟 / 前端重新整理即消失 |
| 產出 | Phase 2 才做 | 已有「研究報告」（docx + html）與「簡報」（pptx + html）兩個 skill |

## 2. 模組結構

```
app/backend/deep_research/
├── config.py            # 模型清單、DEEP_SEARCH_* 上限、supports_reasoning_effort()
├── session.py           # 記憶體 SessionStore（TTL + 每人上限）
├── sources.py           # 上傳檔案前處理：vector store / 試算表轉文字 / 圖片轉 data URL
├── researcher.py        # Agents SDK 研究流程，把 SDK 事件轉成 SSE 事件
├── skills.py            # 報告 / 簡報 skill：prompt + 結構化 schema + renderer 註冊表
└── templates/           # 決定性的樣板；同一份物件渲染成多種格式
    ├── themes.py        # 五組視覺主題（色票 + 字體配對 + 排版取向）
    ├── common.py        # HTML 共用：轉義、Markdown→HTML、scrub
    ├── report.py        # 報告 → HTML        deck.py       # 簡報 → HTML
    ├── report_docx.py   # 報告 → .docx       deck_pptx.py  # 簡報 → .pptx
    └── office.py        # Office 共用：字型對應、紙張色票、Markdown→區塊
app/backend/api/deep_research.py   # 路由（SSE）
app/frontend/js/deep-research.js   # 前端
app/frontend/css/deep-research.css
```

## 3. 流程

```
前端                          backend                         OpenAI
 │ multipart(query,model,files)  │                               │
 ├─────────────────────────────►│ read_uploads() 驗證格式/大小    │
 │                              ├── 文件 ─────────────────────►  vector store（臨時）
 │                              ├── 試算表 → Markdown → prompt   │
 │                              ├── 圖片 → base64 → input_image  │
 │◄── SSE: session/status ──────┤                               │
 │                              ├── Runner.run_streamed() ────►  Agent（web_search + file_search）
 │◄── SSE: tool_start/done ─────┤◄──────────────────────────────┤
 │◄── SSE: delta ───────────────┤                               │
 │◄── SSE: done(markdown) ──────┤ cleanup(): 刪 vector store + files
 │                              │
 │ POST artifacts {kind}         │
 ├─────────────────────────────►│ Runner.run(output_type=ReportDoc/DeckDoc) ─► 結構化 JSON
 │◄── SSE: done(formats) ───────┤ templates/ 用同一份 JSON 組出 docx/pptx + HTML
 │ GET .../artifacts/{kind}/{fmt}│ 存進記憶體 session
 ├─────────────────────────────►│ Response(attachment)
```

## 4. 幾個設計決定

**排版不交給模型。** 兩個 skill 的模型輸出都是 `output_type` 綁定的結構化 JSON
（`ReportDoc` / `DeckDoc`），HTML 由 `templates/` 決定性地組出來。理由很實際：
模型只要漏一個結束標籤，整份下載檔就毀了；改成填 JSON 之後，最差情況只是內容平庸而非版面破碎。

**版面不交給模型，主題交給使用者。** 產出的視覺風格收斂成五個具名主題
（色票 + 字體配對 + 排版取向），由前端在產生前選擇，報告與簡報各自獨立。
作法參考 [Anthropic theme-factory](https://github.com/anthropics/skills/tree/main/skills/theme-factory)：
與其讓模型每次自由發揮（結果是每份檔案長得都不一樣，而且通常都不好看），
不如提供少數經過設計的預設。

**篇幅交給使用者，但只寫進 prompt。** 簡報頁數（5–20，預設 12）與報告小節數
（3–10，預設 6）由前端的數字欄位指定，經 `config.resolve_length()` clamp 後
接在 skill instructions 後面（`skills._length_rule()`）。上下限存在的理由是成本：
這個值直接來自瀏覽器且等同模型的輸出量，沒 clamp 一個 `length: 500` 就能燒掉整包額度。
因此上限做成三層：前端 `<input>` 的 `min`/`max`（體感，可繞過）、API 的絕對上限
`LENGTH_HARD_MAX = 20`（越界回 400，正常前端送不出這種值）、以及 `resolve_length()`
把區間內的偏差 clamp 掉。`LENGTH_SPECS` 的 `max` 與環境變數給的 `default` 在啟動時
一併校正 —— `default` 是唯一繞得過 `resolve_length()` 的路徑（`length` 沒送時直接回傳），
沒校正的話 `DEEP_SEARCH_DECK_SLIDES=999` 會讓每次產出都跑 999 頁且從 API 看不出異常。
**後端不對產出做裁切** —— 硬砍會刪掉有內容的頁，而結構化輸出對「剛好 N 個」的
命中率夠好，偶爾偏一頁遠比破壞內容划算。頁數同時會影響簡報的節奏規則：
八頁以上才要求 section 分隔頁與 stats／quote 頁，更短時以論點優先。

**Office 檔不是從 HTML 轉出來的。** 報告額外產出 `.docx`、簡報額外產出 `.pptx`，
兩者都是**吃同一份結構化 JSON 的另一個 renderer**（`report_docx.py` / `deck_pptx.py`），
不是把 HTML 丟給轉檔器。這是「排版不交給模型」這個決定的直接紅利：
物件已經是結構化的，多一種格式的成本只有幾十毫秒的渲染，不用再燒一次 token，
也不會有 HTML→Office 轉換那種版面走樣。反過來走（pandoc / LibreOffice headless）
還要在 image 裡塞一整套辦公軟體，動輒 1GB+ 記憶體 —— backend 的 `mem_limit`
只有 640m，裝不下。`python-docx` / `python-pptx` 一律在 renderer 函式內才 import，
免得 lxml 與 Pillow 常駐佔記憶體。

兩種格式的分工是刻意的：Office 檔是使用者要帶走、要編輯、要在會議室打開的那一份
（簡報的講稿進 PowerPoint 的備忘稿）；HTML 檔負責瀏覽器內預覽、離線放映與列印成 PDF。
視覺上 Office 版比 HTML 版樸素 —— CSS 漸層與翻頁動畫換不過來，也不該換。
主題（色票、字體配對）兩邊共用，但 Word 一律用淺色紙張：文件會被列印，
而 Word 預設不印背景色，暗色主題照搬過去就是白紙配一片看不見的淺色字
（`office.paper_palette()` 負責換算，強調色會壓暗到在白底上可讀）。

**用內容預算取代 render QA 迴圈。**
[OpenAI slides skill](https://github.com/openai/skills) 的作法是
產生 → 用 LibreOffice rasterize 成 PNG → 程式化偵測溢位／重疊 → 修正。
那套是為 PptxGenJS 而生的：盲畫座標，不跑一次根本不知道有沒有爆版。
我們輸出 HTML，排版由瀏覽器決定且樣板完全自控，唯一變因是內容長度，
因此改在 `deck._density()` 依字數與條目數換算 d1/d2/d3 三級，
字級與間距整組跟著降。省下在後端塞 Chromium（+400MB、每次多十幾秒）的代價。
真要做視覺 QA 迴圈，之後再加不遲。

**試算表不進向量庫。** OpenAI File Search 的支援格式**不含 `.xlsx` / `.csv`**，
而且切塊語意檢索本來就不適合表格。改成本地用 openpyxl 轉成 Markdown 表格直接放進 prompt，
以 `DEEP_SEARCH_SPREADSHEET_*` 控制字數預算。

**上傳檔案不留在 OpenAI。** 研究結束（含失敗與前端斷線）一律 `cleanup()` 刪掉 vector store
與底層 file 物件；另外掛 `expires_after` 當保險絲，避免後端沒清乾淨時檔案留在帳號裡。

**模型鎖定 `gpt-5.6-luna`（白名單，不是預設值）。** 前端下拉只會有這一個，後端
`config.MODEL_CATALOG` 是唯一的來源，環境變數只能縮減不能擴充。理由是計費：
換模型等於換一組費率，而 `TOKEN_COST_TABLE` 的對照與配額扣點都綁在 model id 上，
開放自選模型等於開放使用者自選單價（Luna $0.20/$1.20、Sol $4/$20 差二十倍）。
要放寬時先補費率再加 id，兩邊要一起改。已用 Agents SDK 實測：`WebSearchTool` 與 `FileSearchTool` 都正常
（附一份只有檔案內才有的代號，模型答得出來即證明 file search 生效），`reasoning.effort=medium`
與 structured outputs 亦可用。注意 `reasoning_effort=minimal` **不被** 5.4 之後的模型接受，
但深度研究只用 `medium` / `low`，不受影響；聊天那條路的 `ROUTER_REASONING_EFFORT=minimal` 則要留意。

**非推理模型不帶 `reasoning.effort`。** Responses API 對 `gpt-4.1` 系列帶 reasoning 會直接回 400，
判斷在 `config.supports_reasoning_effort()`。

**依賴限制：`openai-agents==0.3.3`。** 這是最後一個相容 `openai` 1.x 的版本；
更新的版本要求 `openai>=2`，會與本專案的 `langchain-openai==0.1.1`（`openai<2`）衝突。
一併把 `openai` 升到 `1.109.1`、`pydantic` 升到 `2.13.4`（openai-agents 的下限），
`langchain` 系列版本不動。

## 5. 計費與配額

深度研究與聊天吃**同一份月配額**，共用 [`usage_quota`](../app/backend/module/usage_quota.py)
與 [`token_usage`](../app/backend/module/token_usage.py)，串接點集中在
[`deep_research/usage.py`](../app/backend/deep_research/usage.py)。

| 時機 | 做什麼 |
|------|--------|
| `POST /runs` 進來 | `assert_preflight_llm_quota()`：已達上限直接 429，連檔案都不讀 |
| `POST /runs/{sid}/artifacts` 進來 | 同上；產檔是另一次模型呼叫，獨立擋一次 |
| 研究結束（含失敗、斷線） | 把 `result.context_wrapper.usage` 記進配額與 `token_usage_logs` |
| 產檔結束 | 同上，另記一列 |

三個實作上的取捨：

- **用量從 `context_wrapper.usage` 取**，那是 Agents SDK 跨多輪（web search 會用掉數輪）
  累加後的總和，所以一次研究只結算一列，而不是每輪一列。
- **中斷也要記帳**。`stream_research()` 在 `finally` 回填用量，API 層則用 `_spawn_cleanup()`
  把記帳丟成獨立 task —— 前端斷線時 producer 已被 cancel，`finally` 裡直接 `await`
  會立刻再拋 `CancelledError`，記帳根本不會發生。不記帳的話，中斷就是免費研究的後門。
- **`chat_id` 留 NULL**。研究不隸屬任何一則對話（欄位本身可為 NULL），改用 `caller` 分辨來源：
  `deep_research`、`deep_research_report`、`deep_research_deck`，對帳時可直接依此分群。

pre-flight 只擋「開始之前」，不會在跑到一半時中止（與聊天一致）：允許最後一次請求略超上限，
下一次再擋。單次研究動輒數十萬 token，因此另有「每位使用者同時只能跑一個研究」的併發節流。

## 6. 已知限制（要進 Phase 2 前必須處理）

- **單 process**：session 在記憶體，多 worker 或多台機器會拿不到彼此的 session，得換 Redis。
- **前端斷線即中止**：SSE generator 被 cancel 時會連帶取消研究任務（沒有持久化，續跑沒有意義）。
- **無歷史**：重新整理就沒了，這是刻意的取捨。
