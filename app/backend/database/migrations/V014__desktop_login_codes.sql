-- Apply before enabling DESKTOP_AUTH_ENABLED and deploying the desktop client.
CREATE TABLE IF NOT EXISTS desktop_login_codes (
    code_hash      CHAR(64) PRIMARY KEY,
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_challenge VARCHAR(43) NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_desktop_login_codes_expiry ON desktop_login_codes(expires_at);
