-- ============================================================
-- Migration V005: 種子 subscription_tiers（free / pro / ultra）
-- 與 app/backend/module/usage_quota.py 預設 free 上限一致。
-- ============================================================

BEGIN;

INSERT INTO subscription_tiers (name, monthly_token_limit, max_projects)
VALUES
    ('free', 200000, 10),
    ('pro', 1000000, 20),
    ('ultra', 5000000, 999999)
ON CONFLICT (name) DO UPDATE SET
    monthly_token_limit = EXCLUDED.monthly_token_limit,
    max_projects = EXCLUDED.max_projects;

COMMIT;
