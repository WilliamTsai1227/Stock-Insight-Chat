# 外部 MCP Client 串接

本階段讓 Stock Insight Chat 作為 MCP Client，透過 **Streamable HTTP** 連接由
系統管理者設定的外部 MCP Server。它不會把本專案公開成 MCP Server。

## 設定

在後端 `.env` 加入：

```dotenv
MCP_SERVERS_JSON='{"market-data":{"url":"https://mcp.example.com/mcp","header_env":{"Authorization":"MARKET_MCP_AUTHORIZATION"},"allowed_tools":["get_quote","company_profile"]}}'
MARKET_MCP_AUTHORIZATION=Bearer replace-with-real-token
```

`MCP_SERVERS_JSON` 可使用物件或陣列格式。每個 server 支援：

| 欄位 | 必填 | 說明 |
| --- | --- | --- |
| `name` | 陣列格式必填 | Server 識別名稱；物件格式預設使用 key |
| `url` | 是 | Streamable HTTP endpoint；正式環境必須為 HTTPS |
| `header_env` | 否 | HTTP header 到環境變數名稱的映射，建議用於憑證 |
| `headers` | 否 | 固定 header；可在值內使用 `${ENV_NAME}` |
| `allowed_tools` | 否 | 可匯入的遠端工具白名單，正式環境強烈建議設定 |
| `enabled` | 否 | 預設 `true` |
| `timeout_seconds` | 否 | 1–120 秒，預設 20 秒 |

多個 Server 範例：

```dotenv
MCP_SERVERS_JSON='{"market":{"url":"https://market.example.com/mcp","allowed_tools":["quote"]},"research":{"url":"https://research.example.com/mcp","header_env":{"Authorization":"RESEARCH_MCP_AUTH"},"allowed_tools":["search"]}}'
RESEARCH_MCP_AUTH=Bearer replace-with-real-token
```

本機測試 HTTP endpoint 時，才加入：

```dotenv
MCP_ALLOW_INSECURE_HTTP=1
```

## 行為

- 後端啟動後按需連線並取得 `tools/list`。
- 工具會轉成 `mcp__<server>__<tool>`，避免與內建工具撞名。
- 前端「股市 Agent → 思考模式 → 工具權限」會自動列出已連線的 MCP 工具。
- Smart Mode 會將已發現且通過 server 白名單的 MCP 工具提供給 Router。
- 每次工具呼叫建立獨立 session，適合多使用者並行請求。
- MCP 結果會進入 Analyst 的完整參考資料；圖片與音訊不注入文字 context。
- 工具清單預設快取 300 秒，可呼叫 `POST /api/mcp/servers/refresh` 強制更新。

## 管理 API

兩個端點都需要登入：

- `GET /api/mcp/servers`：取得連線狀態及安全的工具清單。
- `POST /api/mcp/servers/refresh`：重新讀取環境設定並測試所有連線。

API 不會回傳 MCP URL、header 或 token。連線錯誤中的 URL 也會被遮蔽。

## 限制與安全

- 第一階段僅支援 Streamable HTTP，不啟動本機 `stdio` 子程序。
- Server URL 只能由部署者在後端環境設定，使用者不能透過 API 任意新增 URL。
- 正式環境預設拒絕 HTTP 與 literal private/loopback IP。
- 未設定 `allowed_tools` 時，只匯入 MCP 明確標註為唯讀的工具；未標註或可能修改資料的工具必須由部署者明確加入白名單。
- 請透過 `allowed_tools` 僅開放需要的工具；會寫入或刪除資料的工具不應加入 Smart Mode。
- 單一 Server 預設最多載入 24 個工具，單次結果最多注入 12,000 字元。

## 本機 docker compose 測試

`deploy/docker-compose.yml`（dev）內建一個 `mcp-test` 服務：用 mcp 2.x 寫的
Streamable HTTP Server，提供 `get_quote` / `company_profile` 兩個唯讀假工具，
不連任何外部服務。正式環境（`docker-compose.prod.yml`）沒有這個服務。

```bash
cd deploy
docker compose up -d --build
```

backend 的 `environment` 已經預設指向它：

```yaml
- MCP_ALLOW_INSECURE_HTTP=1
- MCP_SERVERS_JSON={"market-data":{"url":"http://mcp-test:8080/mcp","allowed_tools":[...]}}
```

設定寫在 `environment` 而非 `.env`，是因為 compose 各版本對 `env_file` 中含引號的
JSON 解析行為不一致；寫在 `environment` 才能保證值原封不動傳進容器。

### 驗證

```bash
# 1. 連線狀態（需登入後帶 AT）
curl -s localhost:8000/api/mcp/servers -H "Authorization: Bearer <AT>" | jq

# 2. 直接戳測試 server（host 上已把 8080 映射到 8090）
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8090/mcp
```

前端開 `http://localhost` → 股市 Agent → 思考模式 → 輸入框打 `@`，
應該看到 `get_quote` 與 `company_profile`（見 [`frontend_spec.md` §9](./frontend_spec.md)）。

### 三個容易卡住的點

1. **工具清單快取 300 秒。** 改了 `MCP_SERVERS_JSON` 或重啟 `mcp-test` 之後，
   舊的（可能是失敗的）快照還會留著。打 `POST /api/mcp/servers/refresh` 強制更新，
   或直接 `docker compose restart backend`。
2. **`http://` 一定要配 `MCP_ALLOW_INSECURE_HTTP=1`。** 私有 IP 檢查只擋**字面 IP**，
   docker 服務名（`mcp-test`）不受影響；但 http scheme 的檢查擋所有人。
3. **自架 MCP Server 的 DNS rebinding 保護。** mcp 2.x 的
   `TransportSecuritySettings` 預設 `enable_dns_rebinding_protection=True` 且
   `allowed_hosts=[]`，等於**拒絕所有 Host header**。`deploy/mcp-test-server/server.py`
   為此明確關閉；正式的 MCP Server 應改為列出 `allowed_hosts`。

### 換成真正的外部 MCP Server

把 compose 裡那兩行 `MCP_*` 註解掉，改在 `.env` 設定（HTTPS 不需要
`MCP_ALLOW_INSECURE_HTTP`）。`.env` 單行 JSON 用單引號包住即可。

### ⚠️ 本機測試會連到正式資料庫

`.env` 的 `DATABASE_URL` 目前指向 AWS RDS。`docker compose up` 起的 backend
會**直接讀寫正式資料庫** —— 你在本機測試建立的對話、訊息、token 用量都會寫進去。
要隔離的話，把 `docker-compose.yml` 裡註解掉的 `db` 服務打開，並把 `.env` 的
`DATABASE_URL` 改指向 `postgresql+asyncpg://postgres:password123@db:5432/Insight`。
