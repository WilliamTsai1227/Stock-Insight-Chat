# 功能與實作對照表 (Feature Mapping)

本文件追蹤各項核心功能的實作狀態及其在各層級的對應關係。

| 功能模組 | 前端元件 (Frontend) | 後端接口 (API/Agent) | 資料存儲 (Database) | 狀態 |
| :--- | :--- | :--- | :--- | :--- |
| **會員系統** | Google 登入按鈕（`login.html`） | `/api/user/auth/google/*` | `users` | 🟢 已實作（Google SSO Only） |
| **訂閱等級** | Tier Badge | `/api/user`（含 `tier_id`） | `subscription_tiers` | 🟢 已實作 |
| **Token 計量** | Usage Bar | `/api/user/usage`、`usage_quota` 模組 | `user_usage_quotas`, `token_usage_logs` | 🟢 已實作（後端強制） |
| **即時對話（SSE）** | Chat Window | `/api/chat/messages` | `chats`, `messages` | 🟢 已實作 |
| **股市 Agent（思考模式）** | 工具控制 Popover | `agent/chat.py`（LangGraph ReAct） | `Qdrant`, `MongoDB` | 🟢 已實作 |
| **快捷模式（Flash）** | 模式切換 | `agent/flash_pipeline.py` | `Qdrant`, `MongoDB` | 🟢 已實作 |
| **一般對話** | 模式切換 | `agent/general_chat.py` | — | 🟢 已實作 |
| **RAG 檢索** | Citation List, Sources | Hybrid Search（dense + BM25 + RRF） | `Qdrant`, `MongoDB` | 🟢 已實作 |
| **即時網路搜尋** | 工具控制 Popover | `tavily_global_search`（Tavily API） | — | 🟢 已實作 |
| **探索（Kinetic Charts）** | 側欄「探索」iframe | `/explore/*` 反向代理 → `kinetic` 容器 | 獨立 `kinetic` database | 🟢 已實作（獨立專案 Stock-Analysis） |
| **專案管理** | Project Sidebar | `/api/project`（POST/GET/DELETE） | `projects` | 🟢 已實作（**無改名功能**） |
| **建議回饋** | 回饋表單 + Turnstile | `/api/user/feedback`、`/api/public/feedback-config` | `user_feedback` | 🟢 已實作（含 Token 獎勵） |
| **執行追蹤** | ReAct Trace UI (Steps) | Agent（trace state） | `messages.metadata` | 🟢 已實作 |
| **檔案檢索** | Upload UI | `/api/files/upload`、`/api/files/{id}`（stub） | `files`, `S3` | 🟡 規劃中 |
| **深度研究（Deep Research）** | 側欄「深度研究」（`deep-research.js`） | `/api/deep-research/*`（OpenAI Agents SDK） | **無**（記憶體 session，TTL 120 分鐘） | 🟢 MVP 已實作（功能驗證版） |
| **深度研究：知識庫版** | — | — | `research_workspaces`, `Qdrant`, `S3` | ⚪ 未實作，見 [`deep_search.md`](./deep_search.md) §1–20 |

## 實作重點

1.  **三條對話路徑**: `chat_mode` × `response_mode` 決定走 LangGraph ReAct（思考）、線性管線（Flash）或純聊天（general），見 [`agent_spec.md`](./agent_spec.md) §0。
2.  **ReAct 循環 (LangGraph)**: `gpt-5-mini` 決策 + `gpt-5` 分析的雙模型架構；兩者的 `reasoning_effort` 可經環境變數調整（延遲最敏感的旋鈕）。
3.  **確定性數據遷移**: 以 UUID v5 產生確定性 point ID，確保 Qdrant 資料不重複且可溯源。
4.  **空結果重試**: Router 最多 `ROUTER_MAX_CYCLES`（程式常數，3）輪，空結果時調整搜尋策略（擴大時間範圍、切換關鍵字、放寬過濾）。
5.  **混合檢索與聚合**: dense + BM25 混合檢索經 RRF 融合，並依 `mongo_id` 分組去重避免同篇重複。
6.  **深度研究不落地**: MVP 走 Agents SDK 的 hosted tools（`WebSearchTool` / `FileSearchTool`），session 只放記憶體、上傳文件研究完即從 OpenAI 刪除；產出的報告與簡報由後端樣板決定性地組成 HTML，模型只負責填結構化 JSON。與 [`deep_search.md`](./deep_search.md) 規劃的 NotebookLM 式知識庫是兩件事。
7.  **探索是獨立專案**: `kinetic` 容器來自 **Stock-Analysis** 專案，本專案只負責 `/explore/*` 的登入閘門與反向代理；kinetic 本身無認證，故不可對外開 port。
