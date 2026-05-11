"""
對話上下文載入（PostgreSQL + 遞迴 CTE）

從「本輪剛寫入的 user 訊息」的 parent_id 起點，沿 parent_id 往上走，
只取同一 chat_id 下的單一主線（不含被放棄的分支），避免 ORDER BY created_at
整包撈取時混進無關分支。

注意：parent_id 必須能串成鏈（建議：每則 user 的 parent_id 指向前一則訊息，
assistant 的 parent_id 指向對應的 user）。僅 assistant→user 而 user 皆為 NULL 時，
遞迴在舊資料上只會回到上一輪，無法再往上。
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence
from uuid import UUID

import asyncpg
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# 沿 parent_id 往上回溯（傳進 SQL 的 max_hops，遞迴條件為 c.hop < max_hops）。
#
# 「數字」代表的是：從「本則 user 的上一則」（種子，通常是 assistant）起，還能再沿
# parent **往上走幾步**。若鏈夠長，最多會拿到 **max_hops + 1 則**（含種子），
# 每一步就是多帶一則 user 或 assistant，不是「整數輪」的單位。
#
# 在鏈為 user ↔ assistant 交替、且種子是 assistant 的典型情況下，**成組與否與奇偶**：
# - max_hops 為奇數（1,3,5,…）→ 總則數為偶數 → 由舊到新通常從 **user** 開始、以 **assistant**
#   結束，邊界上是**整組整組**問答（不包含最上層落單的 assistant）。
# - max_hops 為正偶數（2,4,…）→ 總則數為奇數 → **最舊一則多半是 assistant**，對應的那一則
#   **user 在視窗外**，最上面是**半組**，不完整一輪。
# - max_hops = 0 → 僅 1 則（種子），多半是單則 assistant，不是一輪。
#
# 約略：約 5「輪」完整問答可將 max_hops 設成 9（10 則 = 5×2），或依 token 再縮。
DEFAULT_CONTEXT_CHAIN_MAX_HOPS: int = int(
    os.getenv("CONTEXT_CHAIN_MAX_HOPS", "10")
)


async def fetch_ancestor_chain_rows(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    start_message_id: UUID,
    max_hops: int = DEFAULT_CONTEXT_CHAIN_MAX_HOPS,
) -> List[asyncpg.Record]:
    """
    從 start_message_id 沿 parent_id 往「更舊」的訊息遞迴，僅限同一 chat_id。

    回傳列順序：由舊到新（適合直接餵給 LLM）。
    """
    if max_hops < 0:
        max_hops = 0

    rows = await conn.fetch(
        """
        WITH RECURSIVE chain AS (
            SELECT
                id,
                parent_id,
                chat_id,
                role,
                content,
                context_refs,
                0 AS hop
            FROM messages
            WHERE id = $1::uuid AND chat_id = $2::uuid

            UNION ALL

            SELECT
                m.id,
                m.parent_id,
                m.chat_id,
                m.role,
                m.content,
                m.context_refs,
                c.hop + 1
            FROM messages AS m
            INNER JOIN chain AS c ON m.id = c.parent_id
            WHERE m.chat_id = $2::uuid
              AND c.hop < $3
        )
        SELECT id, parent_id, chat_id, role, content, context_refs, hop
        FROM chain
        ORDER BY hop DESC;
        """,
        start_message_id,
        chat_id,
        max_hops,
    )
    return list(rows)


async def fetch_prior_context_for_agent(
    conn: asyncpg.Connection,
    *,
    chat_id: UUID,
    user_message_id: UUID,
    max_hops: int = DEFAULT_CONTEXT_CHAIN_MAX_HOPS,
) -> List[asyncpg.Record]:
    """
    供 Agent 使用的「本輪 user 訊息之前」的脈絡。

    本輪內容由呼叫端另用 HumanMessage(query) 傳入，因此從
    messages.parent_id（本輪 user 的上一則）開始往上遞迴；首則訊息則回傳空列表。

    僅前一則：max_hops=0（或 CONTEXT_CHAIN_MAX_HOPS=0）。

    僅前一「輪」（一組問答，通常 2 則：上一則 user + 上一則 assistant）：
    max_hops=1（或 CONTEXT_CHAIN_MAX_HOPS=1）。前提：本則 user 的 parent 為上一則 assistant，
    且該 assistant 的 parent 為上一則 user（與 api 寫入方式一致時成立）。
    """
    parent_id: Optional[Any] = await conn.fetchval(
        """
        SELECT parent_id
        FROM messages
        WHERE id = $1::uuid AND chat_id = $2::uuid
        """,
        user_message_id,
        chat_id,
    )
    if parent_id is None:
        return []

    return await fetch_ancestor_chain_rows(
        conn,
        chat_id=chat_id,
        start_message_id=parent_id,
        max_hops=max_hops,
    )


def rows_to_langchain_messages(rows: Sequence[Any]) -> List[BaseMessage]:
    """將 DB 列轉成 LangChain 訊息（僅 user / assistant）。"""
    out: List[BaseMessage] = []
    for r in rows:
        role = r["role"]
        content = r["content"] or ""
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
    return out
