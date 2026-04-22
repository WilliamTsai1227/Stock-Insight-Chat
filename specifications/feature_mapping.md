# 功能與實作對照表 (Feature Mapping)

本文件追蹤各項核心功能的實作狀態及其在各層級的對應關係。

| 功能模組 | 前端元件 (Frontend) | 後端接口 (API/Agent) | 資料存儲 (Database) | 狀態 |
| :--- | :--- | :--- | :--- | :--- |
| **會員系統** | Login/Register (UI Only) | `/api/auth/*` (Planned) | `users`, `roles` | 🟡 實作中 (Model已就緒) |
| **訂閱等級** | Tier Badge (Static) | `/api/user/profile` (Planned) | `subscription_tiers` | 🟢 已實作 (Model/DB) |
| **Token 計量** | Usage Bar (Static) | `/api/user/usage` (Planned) | `user_usage_quotas`, `token_logs` | 🟢 已實作 (Model/DB) |
| **即時對話** | Chat Window, Glassmorphism | `/api/getAIResponse` | `chats`, `messages` | 🟢 已實作 |
| **RAG 檢索** | Citation List, Sources | `Agent (Hybrid Search)` | `Qdrant`, `MongoDB` | 🟢 已實作 |
| **專案管理** | Project Sidebar | `/api/projects` | `projects` | 🟢 已實作 |
| **檔案檢索** | Upload UI | `/api/files/upload` | `PostgreSQL`, `S3` | 🟡 實作中 (API已就緒) |
| **執行追蹤** | ReAct Trace UI (Steps) | `Agent (trace state)` | `messages.metadata` | 🟢 已實作 |

## 技術亮點
1.  **ReAct 循環 (LangGraph)**: 採用 `gpt-5-mini` 決策與 `gpt-5` 分析的雙模型架構。
2.  **確定性數據遷移**: 透過 UUID v5 確保 Qdrant 資料不重複且具備溯源能力。
3.  **自動重試機制**: Agent 具備 5 次重試空間，會自動調整搜尋策略以應對空結果。
4.  **混合檢索與聚合**: 結合向量搜尋與標籤過濾，並透過 `search_groups` 避免重複內容。
