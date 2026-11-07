-- V011：投資組合排程分析（庫存 / 觀察清單 → 定期呼叫 Zentra 的 stock-suite tools）
--
-- portfolio_items  ：使用者的庫存（HOLDING）與觀察清單（WATCHLIST）
-- insight_schedules：每位使用者一筆排程設定；next_run_at 是排程器認領的依據
-- insight_jobs     ：每次執行（排程或手動）一列
-- insight_results  ：每個 job × 每檔股票 × 每個 tool 一列
--
-- 詳見 specifications/portfolio_insight_spec.md。全部冪等，可重跑。

CREATE TABLE IF NOT EXISTS portfolio_items (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_type  VARCHAR(10) NOT NULL CHECK (list_type IN ('HOLDING', 'WATCHLIST')),
    market     VARCHAR(2)  NOT NULL CHECK (market IN ('TW', 'US')),
    symbol     VARCHAR(15) NOT NULL,
    name       VARCHAR(60) NOT NULL,
    cost       NUMERIC(18, 4),
    shares     NUMERIC(20, 4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 同一檔股票只能在一個清單裡（買進後從觀察清單「移到」庫存）
    CONSTRAINT uq_portfolio_items_symbol UNIQUE (user_id, market, symbol),
    -- 庫存一定要有成本與股數；觀察清單可以沒有
    CONSTRAINT ck_portfolio_items_holding CHECK (
        list_type = 'WATCHLIST' OR (cost IS NOT NULL AND cost >= 0 AND shares IS NOT NULL AND shares > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_portfolio_items_user ON portfolio_items(user_id, list_type, created_at);

CREATE TABLE IF NOT EXISTS insight_schedules (
    user_id        UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    -- DAILY 每天、WEEKDAYS 週一到週五、WEEKLY 每週 weekday、MONTHLY 每月 month_day
    frequency      VARCHAR(10) NOT NULL CHECK (frequency IN ('DAILY', 'WEEKDAYS', 'WEEKLY', 'MONTHLY')),
    run_time       TIME NOT NULL,                       -- 台北時間（UTC+8）
    weekday        SMALLINT CHECK (weekday BETWEEN 1 AND 7),      -- ISO：1=週一 … 7=週日
    month_day      SMALLINT CHECK (month_day BETWEEN 1 AND 28),   -- 只到 28，避免二月沒有那天
    risk_tolerance VARCHAR(12) CHECK (risk_tolerance IN ('CONSERVATIVE', 'BALANCED', 'AGGRESSIVE')),
    cash_twd       NUMERIC(18, 2) CHECK (cash_twd >= 0),
    next_run_at    TIMESTAMPTZ,                         -- NULL = 停用中
    last_run_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_insight_schedules_weekly  CHECK (frequency <> 'WEEKLY'  OR weekday IS NOT NULL),
    CONSTRAINT ck_insight_schedules_monthly CHECK (frequency <> 'MONTHLY' OR month_day IS NOT NULL)
);

-- 排程器每輪只掃「啟用且已到期」的列
CREATE INDEX IF NOT EXISTS idx_insight_schedules_due
    ON insight_schedules(next_run_at) WHERE enabled AND next_run_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS insight_jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger        VARCHAR(10) NOT NULL CHECK (trigger IN ('SCHEDULED', 'MANUAL')),
    scheduled_for  TIMESTAMPTZ NOT NULL,                -- 排程的目標時間；手動 = 按下的時間
    status         VARCHAR(10) NOT NULL DEFAULT 'QUEUED'
                   CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')),
    started_at     TIMESTAMPTZ,
    heartbeat_at   TIMESTAMPTZ,                         -- 執行中每完成一個 tool 更新；判斷是否卡死
    finished_at    TIMESTAMPTZ,
    error          TEXT,
    counts         JSONB,                               -- {"total", "succeeded", "failed"}
    portfolio_status VARCHAR(10) CHECK (portfolio_status IN ('SUCCEEDED', 'FAILED', 'SKIPPED')),
    portfolio_run_id TEXT,
    portfolio_output JSONB,                             -- portfolio-advisor 的完整輸出
    portfolio_error  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 同一個排程時間點只會有一個 job：排程器就算重複認領，INSERT 也會撞這裡而不會跑兩次
CREATE UNIQUE INDEX IF NOT EXISTS uq_insight_jobs_scheduled
    ON insight_jobs(user_id, scheduled_for) WHERE trigger = 'SCHEDULED';
-- 同一位使用者同時最多一個進行中的 job（手動連按、或排程撞上手動）
CREATE UNIQUE INDEX IF NOT EXISTS uq_insight_jobs_active
    ON insight_jobs(user_id) WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX IF NOT EXISTS idx_insight_jobs_user ON insight_jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_insight_jobs_queued ON insight_jobs(created_at) WHERE status = 'QUEUED';

CREATE TABLE IF NOT EXISTS insight_results (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       UUID NOT NULL REFERENCES insight_jobs(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_type    VARCHAR(10) NOT NULL CHECK (list_type IN ('HOLDING', 'WATCHLIST')),
    market       VARCHAR(2)  NOT NULL CHECK (market IN ('TW', 'US')),
    symbol       VARCHAR(15) NOT NULL,
    name         VARCHAR(60) NOT NULL,
    cost         NUMERIC(18, 4),                        -- 執行當下的快照（之後改清單不影響舊報告）
    shares       NUMERIC(20, 4),
    tool         VARCHAR(40) NOT NULL CHECK (tool IN ('stock-news', 'stock-chart', 'stock-technical', 'stock-fundamental')),
    status       VARCHAR(10) NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    run_id       TEXT,                                  -- Zentra 的 run_id，追查用
    output       JSONB,                                 -- tool 的完整輸出（K 線圖的 base64 已移到 chart_png）
    chart_png    BYTEA,                                 -- 只有 stock-chart；保留期過後清成 NULL
    error        TEXT,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    CONSTRAINT uq_insight_results_cell UNIQUE (job_id, market, symbol, tool)
);

CREATE INDEX IF NOT EXISTS idx_insight_results_job ON insight_results(job_id);
