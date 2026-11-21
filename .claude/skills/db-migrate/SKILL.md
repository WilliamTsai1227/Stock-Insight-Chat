---
name: db-migrate
description: 對 AWS RDS（.env 的 DATABASE_URL）檢查、預演並套用 app/backend/database/migrations 的 migration，套用前一定先問使用者
argument-hint: [要跑的 migration，例如 V012 V013；不帶則自動找出尚未套用的]
disable-model-invocation: true
---

把 migration 套用到 AWS RDS 的 PostgreSQL。

使用者補充說明：$ARGUMENTS

## 背景（以 repo 為準）
- 本機開發與正式站都連 **AWS RDS**，連線字串是專案根目錄 `.env` 的 `DATABASE_URL`（database `Insight`）。**當作正式資料庫看待。**
- 沒有 migration 紀錄表；哪些已套用記在 `specifications/sql_dev_handbook.md` §4.2 的表格（「已套用正式 RDS」／「尚未套用正式 RDS」）。
- 本機通常沒有 `psql`，且 `DATABASE_URL` 是 `postgresql+asyncpg://` 開頭，psql 不吃。一律改用本 skill 附的
  `run_migration.py`（用後端同一套連線設定，會處理前綴與 SSL）：
  ```bash
  RUN="./venv/bin/python .claude/skills/db-migrate/run_migration.py"
  ```
- 部署順序：**先套用 migration，再部署新後端**（`specifications/release_handbook.md` §3.3）。

## 步驟

1. **確認目標**：`$RUN --target`（不連線）。把「user@host/database」告訴使用者。
   host 不是 `*.rds.amazonaws.com` 或 database 不是 `Insight` → 先問使用者是不是要連這一台再繼續。

2. **找出要跑的 migration**：
   - 使用者有指定（例如 `V012 V013`）→ 用指定的。
   - 沒指定 → `$RUN --status`（只讀 catalog，不改資料）＋對照 handbook 表格，挑出「未套用」「部分套用」的，依編號排序。
   - `--status` 顯示「部分套用」→ 先停下來說明哪些物件在、哪些不在，請使用者決定。
   - 「無法判斷」（例如種子資料 V005）→ 以 handbook 表格為準，不確定就問使用者。
   - 全部已套用 → 回報「沒有待套用的 migration」並結束。

3. **讀 SQL、標出風險**：逐一讀要跑的檔案，用一兩句話說明會改什麼。出現下列語句要特別警告：
   `DROP`、`TRUNCATE`、`DELETE`、沒有 `WHERE` 的 `UPDATE`、`ALTER COLUMN ... TYPE`、對既有大表 `SET NOT NULL`、
   不含 `IF NOT EXISTS`／`IF EXISTS` 的建立或刪除（重跑會失敗）。

4. **預演**：`$RUN --dry-run <檔案…>`。在交易內執行後全部 ROLLBACK，資料庫不會留下變更。
   - 預演會短暫取得表鎖（最多等 5 秒，拿不到就放棄），對正式站影響很小。
   - 預演失敗 → 不要套用，把錯誤回報給使用者。

5. **（建議）先做 RDS 快照**：問使用者要不要先備份。要的話：
   ```bash
   aws rds describe-db-instances --query "DBInstances[?Endpoint.Address=='<host>'].DBInstanceIdentifier" --output text
   aws rds create-db-snapshot --db-instance-identifier <id> --db-snapshot-identifier insight-before-<V編號>-<YYYYMMDD-HHMM>
   aws rds wait db-snapshot-available --db-snapshot-identifier <同上>
   ```
   `aws` 指令失敗（未登入、權限不足）→ 告訴使用者，由使用者決定要不要沒有快照就繼續。

6. **套用前一定要問**：列出目標（host／database）、要跑的檔案、預演結果、有無快照，請使用者明確同意。
   沒有得到「同意套用」就停在這裡。之前的對話同意過，不代表這一次也同意。

7. **套用**：`$RUN --apply --confirm-db Insight <檔案…>`（`--confirm-db` 必須等於目標資料庫名稱，否則腳本拒絕執行）。
   - 每個檔案各自一個交易，失敗會整份回滾，後面的檔案不會執行。
   - 失敗 → 停止，回報錯誤；不要修改已經套用到正式庫的 migration 檔，需要修正就新增下一號 migration。

8. **驗證與紀錄**：
   - 腳本套用後會印出驗證結果與相關表的欄位、索引，確認都是「已套用」。
   - 更新 `specifications/sql_dev_handbook.md` §4.2 表格：該列改成「YYYY-MM-DD 已套用正式 RDS」。
   - 提醒使用者：現在可以部署新後端；handbook 的變更可以用 `/commit-push` 提交。

9. **回報**：目標資料庫、套用了哪些 migration、預演與套用結果、快照名稱（有的話）、驗證結果、下一步。

## 規則
- 不對 RDS 執行 `init_db.sql`（那是給全新資料庫用的）。
- 不在沒有使用者明確同意的情況下執行 `--apply`。
- 連不上且錯誤提到 SSL → 加上環境變數重試：`DATABASE_SSL=require $RUN …`（不改 `.env`）。
- 不把 `DATABASE_URL` 的密碼印出來；腳本輸出已遮蔽，不要另外 `cat .env`。
