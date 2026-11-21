---
name: notion-debug
description: 把剛完成的除錯排查／測試過程記錄到 Notion「系統測試」（API／ETL／資安）
argument-hint: [問題描述或補充說明]
disable-model-invocation: true
---

把這次對話中的除錯排查或測試記錄到 Notion「📍 系統測試」。

使用者補充說明：$ARGUMENTS

## Notion 結構（三層，只能寫在這裡）
系統測試：https://app.notion.com/p/31d2dbee508080ddb8acc297e5c6fd9b

| 第一層 inline 資料庫 | 第二層：每一列是一個頁面 | 第三層：頁面內的 inline 資料庫「測試項目」 |
|---|---|---|
| **API**（`collection://31d2dbee-5080-801d-a912-000b01ea4dd8`，標題欄位 `Name`） | 一組 API（依路徑前綴分組） | 每一列是一次測試／一個 bug |
| **ETL**（`collection://4182dbee-5080-81db-b446-000b8a339932`，標題欄位 `Name`） | 一條 ETL 流程或腳本，目前只有 `ETL` | 同上 |
| **資安**（`collection://3702dbee-5080-80b3-9cd9-000bd3fda0a3`，標題欄位 `檢測類別`） | 一種資安檢測類別（頁面有 emoji icon） | 每一列是一個具體檢測項目 |

頁面最下方的「RAGAS」、「Postman」子頁面不要動。

### API 第二層：路徑 → 頁面
新頁面命名慣例是 `路徑前綴（中文說明）`，例：`/api/chat（對話管理）`。現有頁面：

| 被測的路徑 | 頁面 |
|---|---|
| `/api/chat/messages`（一般對話／股市 Agent 串流） | `api/chat/message`（舊頁，標題是粗體） |
| `/api/chat/deep-research`、`/api/deep-research/*`（含浮動 AI 小助手） | `deep search` |
| 其他 `/api/chat*`（建立、列表、標題、刪除…） | `/api/chat（對話管理）` |
| `/api/feed/*` | `/api/feed（新聞・AI 分析）` |
| `/api/portfolio/*`、`/api/insights/*` | `/api/portfolio・/api/insights` |
| `/api/notes/*` | `/api/notes` |
| `/api/project/*` | `/api/project` |
| `/api/files/*` | `/api/files` |
| `/api/skills` | `/api/skills` |
| `/api/mcp/*` | `/api/mcp` |
| `/api/user/auth/*` | `/api/user/auth（Google SSO・refresh・logout）` |
| 其他 `/api/user/*` | `/api/user（個人資料・用量・回饋・刪除帳號）` |
| `/explore/*` | `/explore` |
| `/` | `/ 健康檢查` |
| `/chatbot/api/vectorSearch` | `/chatbot/api/vectorSearch`（舊版 API） |

這張表可能過時：寫入前先用 `query-data-sources`（rows 模式）列出第一層資料庫的實際列；fetch 資料庫只會回 schema，看不到列。

### 資安第二層：現有檢測類別
輸入驗證、XSS／前端安全、Prompt Injection／AI 安全、身分驗證與登入、流量防護、CORS／HTTP 安全設定、存取控制（IDOR）

### 第三層「測試項目」的欄位（各頁不同）
| 範例頁 | 標題欄位 | 其他欄位 |
|---|---|---|
| `api/chat/message`（舊） | `Request` | `簡述`、`BUG程度` |
| `/api/chat（對話管理）` 等新 API 頁 | `測試項目` | `簡述`、`BUG程度` |
| 資安各類別 | `測試項目` | 無 |

`BUG程度` 選項：緊急／中等／輕微／純測試。

## 步驟
1. 判斷屬於 API、ETL 還是資安；再依上面的對照判斷是哪一個第二層頁面。
   - 找不到合適的 → 在第一層資料庫新增一列（API 照 `路徑前綴（中文說明）` 命名；資安用檢測類別名並加 emoji icon），
     並在該頁面裡建立 inline 資料庫「測試項目」，欄位照同類型最新的頁面複製（API 照 `/api/chat（對話管理）`，資安照 `存取控制（IDOR）`）。
   - 判斷不了放哪裡時，先問使用者。
2. fetch 該頁面拿到「測試項目」的 data source，再 fetch 它的 schema，**照現有欄位填**，不要自己新增欄位。
3. 新增一列：
   - 標題：`✅ YYYY-MM-DD 簡短問題名稱`（還沒修好用 `🔧`）；如果標題欄位是 `Request`，就放觸發問題的 request／輸入。
   - 有 `簡述`、`BUG程度` 欄位就填。
   - 內文：
     - **症狀**：看到什麼錯誤、在哪個環境（本機／EC2／RDS…）
     - **排查過程**：試過哪些方向、哪些被排除
     - **根本原因**
     - **修正方式**：改了哪些檔案、commit（用 `git log --oneline -10` 找）
     - **預防／後續**：要補的測試、監控或文件
4. 從這次對話整理，使用者補充說明優先；對話裡沒有的細節不要猜，標註「未確認」。
5. 完成後回報：寫在哪個路徑（例：系統測試 / API / deep search / 測試項目）、紀錄標題、Notion 連結。
