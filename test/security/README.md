# 2026-11-22 桌面與前後端安全檢查

範圍以 `7ce6a31` 新增的 Electron / Google 登入交接為中心，延伸到共用認證與報告預覽。修補目前在工作區，尚未部署或發行 Windows 安裝檔。檢查採隔離的 ASGI / FakeDB / Electron profile，不使用正式資料庫或付費 AI。

## 已修補

| 問題 | 修補 |
| --- | --- |
| Refresh Token 可當作 Bearer Access Token，撤銷後仍可繞過 RT store | Access Token 加入並驗證 `type=access`，強制 `exp` / `sub`；錯誤憑證在取得 DB 連線前拒絕 |
| 部分 API 僅驗 JWT，停用帳號仍可使用；refresh 未檢查停用狀態 | UUID 認證 dependency 與 refresh 都查即時帳號狀態；Google 登入也拒絕停用帳號 |
| `/explore` 接受已撤銷的 RT | 查詢 RT store、有效期限、user ID 與帳號狀態；不轉送上游 Set-Cookie |
| Google email 未驗證仍可合併既有帳號 | 強制 `email_verified`；只對 Gmail / Workspace 身分自動綁定，拒絕不同 Google sub 覆寫 |
| 報告 regex 清理可被 malformed HTML / entity URL 繞過 | 改用 nh3 HTML parser 與標籤、屬性、協定白名單 |
| 報告 iframe 及 blob 新視窗繼承主程式來源，可接觸 app DOM / storage | Web iframe 移除同源權限；Web 新分頁使用 sandbox iframe；Electron 新視窗使用 opaque data / srcdoc 與獨立暫存 session |
| 桌面沒有 CSP | 靜態頁加入 CSP；既有 inline code 用 SHA-256 allowlist，拒絕新增 inline script；共用可信簡報播放器以 hash 允許 |
| JWT 缺省開發金鑰、缺少 expires claim 仍可驗過 | SECRET_KEY 缺少、過短或舊預設值時阻止啟動；所有登入憑證要求到期時間 |
| 資料庫 TLS 跳過 CA / hostname 驗證 | 恢復完整驗證，私有 CA 以 `DATABASE_SSL_CA_FILE` 提供 |
| 正式環境也允許整段 LAN CORS / OAuth return origin | 改為 `ALLOW_DEV_ORIGINS=true` 才開啟，正式環境預設關閉 |
| 舊上傳接口沒有 body 上限，且未儲存也回傳成功 | 加入含 chunked / trailing slash 的大小限制；未實作的上傳 / 刪除接口回 501，使用對話附件流程 |
| Project API 將資料庫錯誤內容直接回傳 | 伺服器記錄錯誤，對使用者回傳一般錯誤訊息 |
| 桌面打包工具帶入 minimatch 已知漏洞 | 僅覆寫受影響的兩個傳遞依賴至 11.0.1，重建鎖檔與封裝驗證 |

另對 refresh / OAuth 成功回應加入 no-store，桌面 exchange 欄位加上長度與格式限制。

## 驗證

- 原本後端安全檢查 74 項有 21 個失敗情境；修補後擴充至 76 項，全部通過。
- 106 個後端相關測試通過（desktop auth、chat artifacts、skills security、deep research uploads）。
- 7 個桌面單元測試通過，Electron smoke 通過登入交接、PKCE、Cookie、隔離報告預覽。
- 28 個 Electron 執行期安全檢查通過，包含 CSP 注入阻擋、報告來源 / session 隔離、簡報翻頁與講稿。
- macOS arm64 本機封裝與 archive 檢查通過；Windows 安裝與簽章未在本次執行驗證。
- 桌面 `npm audit`：0 個已知漏洞。

提交前另驗證全部 231 個 pytest 案例：230 個以隔離設定通過；需真實資料庫的 `test_mongodb_exists` 依使用者指示，單獨以 `.env` 的正式 MongoDB 設定執行唯讀查詢並通過。正式 MongoDB 憑證未提供給其他測試。

執行安全檢查（安全腳本在 import 前設定測試專用環境）：

```sh
PYTHONPATH=. venv/bin/python test/security/security_review.py --output /private/tmp/stock-insight-security/backend.json
node app/desktop/scripts/prepare.cjs
SECURITY_AUDIT_DIR=/private/tmp/stock-insight-security app/desktop/node_modules/.bin/electron test/security/desktop_review.cjs
```

後端測試也應先載入同一份隔離環境，避免 `.env` 中的真實來源在 import 時觸發連線：

```sh
venv/bin/python - <<'PY'
import runpy
runpy.run_path('test/security/security_review.py', run_name='security_fixture')
import pytest
raise SystemExit(pytest.main([
    'test/backend/test_desktop_auth.py', 'test/backend/test_chat_artifacts.py',
    'test/backend/test_skills_security.py', 'test/backend/test_deep_research_uploads.py', '-q',
]))
PY
```

## 尚未完成的相依套件升級

`pip-audit -r app/backend/requirements.txt --no-deps --disable-pip` 仍標記 **24 個套件版本**。這是公告與版本比對，不能直接視為 24 個在本應用可利用的漏洞；掃描也不涵蓋完整傳遞依賴。去除重複 ID 的清單保存在 [backend-dependency-findings.json](backend-dependency-findings.json)。

- 包含 Starlette 0.36.3、python-multipart 0.0.9、aiohttp、cryptography、pymongo 與舊版 LangChain / LangGraph / LangSmith 等。HTTP / multipart 的拒絕服務公告需優先安排升級；body 上限只能降低部分風險。
- LangChain 整組牽涉 OpenAI SDK / Agents SDK 的現有相容性限制。本次沒有直接進行未驗證的跨主版本遷移，這部分仍需升級、安裝解析及 AI 流程回歸。
- python-jose 公告涉及非對稱公鑰作為 HMAC key；本專案固定 HS256 與對稱 SECRET_KEY，這次沒有證實該利用路徑。ecdsa 的 signing timing 公告也不等同本專案使用了 ECDSA 簽名。
- CDN 腳本仍沿用現有來源；桌面 CSP 限制來源，尚未改為全部本機封裝 / SRI pinning。

因此本次結果是「已修補列出的應用程式缺口」，不是完整依賴樹安全認證。

## 部署要求與來源

重新部署前後端、重建桌面安裝檔後才會生效。須設定至少 32 bytes 的隨機 SECRET_KEY、確保 DB CA 信任鏈可用、維持正式環境 `ALLOW_DEV_ORIGINS=false`；桌面登入仍須先套用 V014 並設定 `DESKTOP_AUTH_ENABLED=true`。詳見 [desktop README](../../app/desktop/README.md)。

設計依據：[Electron security checklist](https://www.electronjs.org/docs/latest/tutorial/security)、[Google 帳號綁定與 email authority](https://developers.google.com/identity/sign-in/web/backend-auth)、[nh3 sanitizer](https://nh3.readthedocs.io/en/stable/)、[minimatch advisory](https://github.com/advisories/GHSA-g3hv-75wg-m249)。
