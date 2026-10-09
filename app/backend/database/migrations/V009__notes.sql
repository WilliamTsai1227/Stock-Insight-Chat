-- V009：筆記功能（最小版本）
--
-- note_pages：筆記頁，一個使用者可有多頁，每頁是一塊獨立空間。
-- notes     ：單則筆記。canvas_* 是未來白板階段的座標，最小版本不寫入，
--             先留欄位避免日後再做一次 migration（NULL = 尚未擺放）。

CREATE TABLE IF NOT EXISTS note_pages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      VARCHAR(100) NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,          -- 側欄排序
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_note_pages_user
    ON note_pages(user_id, position, created_at);

CREATE TABLE IF NOT EXISTS notes (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id            UUID NOT NULL REFERENCES note_pages(id) ON DELETE CASCADE,
    -- 冗餘存 user_id：查「我的所有筆記」不必 join note_pages，
    -- 也讓每個查詢都能直接用 user_id 當授權條件
    user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title              VARCHAR(200) NOT NULL,
    content            TEXT NOT NULL,
    -- 來源對話：對話被刪時筆記要留著，所以是 SET NULL 而非 CASCADE
    source_chat_id     UUID REFERENCES chats(id) ON DELETE SET NULL,
    source_message_ids JSONB,
    -- 白板階段的座標與尺寸（最小版本全為 NULL）
    canvas_x           INTEGER,
    canvas_y           INTEGER,
    canvas_w           INTEGER,
    canvas_h           INTEGER,
    created_at         TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notes_page ON notes(page_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id, created_at DESC);
