# 功能與實作對照表 (Feature Mapping)

本文件追蹤各項核心功能的實作狀態及其在各層級的對應關係。

| 功能模組 | 前端元件 (Frontend) | 後端接口 (API/Agent) | 資料存儲 (Database) | 狀態 |
| :--- | :--- | :--- | :--- | :--- |
| **會員系統** | Google 登入按鈕 | `/api/user/auth/google/*` | `users` | 🟢 已實作（Google SSO） |
| **訂閱等級** | Tier Badge | `/api/user`（含 `tier_id`） | `subscription_tiers` | 🟢 已實作 |
| **Token 計量** | Usage Bar | `usage_quota` 模組（後端強制） | `user_usage_quotas`, `token_usage_logs` | 🟢 已實作（後端計量） |
| **即時對話** | Chat Window | `/api/chat/messages`（SSE） | `chats`, `messages` | 🟢 已實作 |
| **RAG 檢索** | Citation List, Sources | Agent（Hybrid Search） | `Qdrant`, `MongoDB` | 🟢 已實作 |
| **專案管理** | Project Sidebar | `/api/project` | `projects` | 🟢 已實作 |
| **建議回饋** | 回饋表單 | `/api/user/feedback` | `user_feedback` | 🟢 已實作 |
| **檔案檢索** | Upload UI | `/api/files/upload`（stub） | `PostgreSQL`, `S3` | 🟡 規劃中 |
| **執行追蹤** | ReAct Trace UI (Steps) | Agent（trace state） | `messages.metadata` | 🟢 已實作 |

## 實作重點
1.  **ReAct 循環 (LangGraph)**: `gpt-5-mini` 決策 + `gpt-5` 分析的雙模型架構。
2.  **確定性數據遷移**: 以 UUID v5 產生確定性 point ID，確保 Qdrant 資料不重複且可溯源。
3.  **空結果重試**: Router 最多 `ROUTER_MAX_CYCLES`（預設 3）輪，空結果時調整搜尋策略（擴大時間範圍、切換關鍵字、放寬過濾）。
4.  **混合檢索與聚合**: dense + BM25 混合檢索，並依 `mongo_id` 分組去重避免同篇重複。
