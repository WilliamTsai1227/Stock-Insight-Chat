-- V010：筆記頁可以對應到一則對話
--
-- 原本從對話整理出的筆記全部塞進「預設頁」，混在一起。改成一則對話對應一個
-- 筆記頁：同一則對話再整理，會回到同一頁累加，而不是每次都長出新頁。
-- 手動建立的頁 source_chat_id 為 NULL。

ALTER TABLE note_pages
    ADD COLUMN IF NOT EXISTS source_chat_id UUID REFERENCES chats(id) ON DELETE SET NULL;

-- 一則對話最多對應一頁（部分索引：手動建立的頁不受限制）
CREATE UNIQUE INDEX IF NOT EXISTS ux_note_pages_user_chat
    ON note_pages(user_id, source_chat_id)
    WHERE source_chat_id IS NOT NULL;
