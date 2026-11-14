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
| **API**（`collection://31d2dbee-5080-801d-a912-000b01ea4dd8`，標題欄位 `Name`） | 一支被測試的 API，例：`api/chat/message`、`deep search`、`/chatbot/api/vectorSearch` | 每一列是一次測試／一個 bug |
| **ETL**（`collection://4182dbee-5080-81db-b446-000b8a339932`，標題欄位 `Name`） | 一條 ETL 流程或腳本，例：`ETL` | 同上 |
| **資安**（`collection://3702dbee-5080-80b3-9cd9-000bd3fda0a3`，標題欄位 `檢測類別`） | 一種資安檢測類別：輸入驗證、XSS／前端安全、Prompt Injection／AI 安全、身分驗證與登入、流量防護 | 每一列是一個具體檢測項目 |

頁面最下方的「RAGAS」、「Postman」子頁面不要動。

## 步驟
1. 判斷屬於 API、ETL 還是資安；再判斷是哪一個第二層頁面。
   - fetch 該第一層資料庫，找對應的頁面。
   - 找不到 → 在第一層資料庫新增一列（API 用路徑或功能名、ETL 用流程名、資安用檢測類別名），並在該頁面裡建立 inline 資料庫「測試項目」。
   - 判斷不了放哪裡時，先問使用者。
2. fetch 該頁面內的「測試項目」資料庫，**照它現有的欄位填**。各資料庫欄位不一樣，例如 `api/chat/message` 的有 `Request`（標題）、`簡述`、`BUG程度`（緊急／中等／輕微／純測試）；其他多半只有標題欄位。不要自己新增欄位。
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
