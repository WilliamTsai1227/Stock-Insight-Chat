-- V013：對話公開分享連結（唯讀快照）
--
-- 分享的是「建立／更新當下」的訊息快照，之後的新訊息不會外流；
-- 一則對話最多一個分享（chat_id UNIQUE），停止分享 = 刪除該列，連結立即失效。
-- token 由後端 secrets.token_urlsafe(32) 產生（43 字元），不可猜測、不可列舉。
-- 須在部署新後端「之前」套用。

CREATE TABLE IF NOT EXISTS chat_shares (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token         VARCHAR(64)  NOT NULL UNIQUE,
    chat_id       UUID         NOT NULL UNIQUE REFERENCES chats(id) ON DELETE CASCADE,
    user_id       UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         VARCHAR(255) NOT NULL,
    snapshot      JSONB        NOT NULL,          -- [{role, content, created_at}]，已排除失敗的回覆
    message_count INTEGER      NOT NULL CHECK (message_count >= 0),
    truncated     BOOLEAN      NOT NULL DEFAULT FALSE,  -- 超過上限時只保留最新的訊息
    view_count    INTEGER      NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_shares_user ON chat_shares(user_id);
