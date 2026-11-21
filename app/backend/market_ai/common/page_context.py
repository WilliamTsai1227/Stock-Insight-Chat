from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from uuid import UUID

import asyncpg
from bson import ObjectId


def _get_db():
    # 延遲 import：api 套件的 __init__ 會載入 chat.py，而 chat.py 又引用本模組，頂層 import 會循環
    from app.backend.api.market_feed import _get_db as get_feed_db

    return get_feed_db()


async def load_selected_news_context(news_id: str) -> Dict[str, Any]:
    if not ObjectId.is_valid(news_id):
        return {"source_type": "selected_news", "error": "新聞 ID 格式錯誤"}
    db = _get_db()
    doc = await db["news"].find_one({"_id": ObjectId(news_id)})
    if not doc:
        return {"source_type": "selected_news", "error": "資料庫中找不到這篇新聞"}
    return {
        "source_type": "selected_news",
        "news_id": str(doc["_id"]),
        "title": doc.get("title") or "",
        "content": doc.get("content") or "",
        "summary": doc.get("summary") or "",
        "url": doc.get("url") or "",
        "source": doc.get("source") or "",
        "category": doc.get("category") or "",
        "publishAt": doc.get("publishAt"),
        "stock": doc.get("stock") or [],
        "keyword": doc.get("keyword") or [],
    }


async def load_portfolio_context(db: asyncpg.Connection, user_id: UUID) -> Dict[str, Any]:
    items = await db.fetch(
        "SELECT list_type, market, symbol, name, cost, shares "
        "FROM portfolio_items WHERE user_id=$1 ORDER BY created_at, id",
        user_id,
    )
    holdings: List[Dict[str, Any]] = []
    watchlist: List[Dict[str, Any]] = []
    for row in items:
        item = {
            "market": row["market"], "symbol": row["symbol"], "name": row["name"],
            "cost": float(row["cost"]) if row["cost"] is not None else None,
            "shares": float(row["shares"]) if row["shares"] is not None else None,
        }
        (holdings if row["list_type"] == "HOLDING" else watchlist).append(item)

    schedule = await db.fetchrow(
        "SELECT risk_tolerance, cash_twd FROM insight_schedules WHERE user_id=$1",
        user_id,
    )
    mongodb = _get_db()
    since = int(time.time()) - 90 * 24 * 60 * 60
    symbols = [item["symbol"] for item in holdings + watchlist]
    query: Dict[str, Any] = {"publishAt": {"$gte": since}}
    if symbols:
        import re
        escaped = "|".join(re.escape(str(symbol)) for symbol in symbols)
        pattern = re.compile(escaped, re.IGNORECASE)
        query["$or"] = [{"title": pattern}, {"content": pattern}]

    cursor = mongodb["news"].find(query, {"title": 1, "content": 1, "publishAt": 1, "source": 1, "url": 1}).sort("publishAt", -1).limit(80)
    news = await cursor.to_list(length=80)

    return {
        "source_type": "portfolio",
        "holdings": holdings,
        "watchlist": watchlist,
        "risk_tolerance": schedule["risk_tolerance"] if schedule else None,
        "cash_twd": float(schedule["cash_twd"]) if schedule and schedule["cash_twd"] is not None else None,
        "recent_news_days": 90,
        "recent_news": [
            {
                "title": n.get("title") or "",
                "content": str(n.get("content") or "")[:800],
                "publishAt": n.get("publishAt"),
                "source": n.get("source") or "",
                "url": n.get("url") or "",
            }
            for n in news
        ],
    }


def format_assistant_context(context: Dict[str, Any]) -> str:
    if context.get("source_type") == "selected_news":
        if context.get("error"):
            return (
                f"頁面上下文載入失敗：{context['error']}。"
                "請在回答開頭明確告知使用者無法讀取所選新聞原文，再以一般研究方式回答。"
            )
        return (
            "【頁面上下文｜使用者選取的完整新聞】\n"
            f"新聞 ID：{context.get('news_id')}\n"
            f"標題：{context.get('title')}\n"
            f"來源：{context.get('source')}\n"
            f"類別：{context.get('category')}\n"
            f"發布時間：{context.get('publishAt')}\n"
            f"相關標的：{context.get('stock')}\n"
            f"完整內容：\n{context.get('content')}\n"
            "回答時優先以此完整新聞為事實來源，再查證基本面、新聞面與技術面。"
        )

    if context.get("source_type") == "portfolio":
        if context.get("error"):
            return (
                f"頁面上下文載入失敗：{context['error']}。"
                "請在回答開頭明確告知使用者本輪無法讀取其資產配置，並避免臆測持股內容。"
            )
        lines = [
            "【頁面上下文｜個人資產配置與近 90 天新聞】",
            f"持股：{context.get('holdings')}",
            f"觀察清單：{context.get('watchlist')}",
            f"風險偏好：{context.get('risk_tolerance')}",
            f"現金（TWD）：{context.get('cash_twd')}",
        ]
        lines.append("近期新聞：")
        for i, news in enumerate(context.get("recent_news", [])[:40], 1):
            lines.append(f"{i}. {news.get('title')}｜{news.get('publishAt')}｜{news.get('content')}")
        lines.append("回答時需結合持股成本、股數、集中度、風險承受度、近期新聞、基本面與技術面；區分短期調整、中期再平衡與長期規劃。")
        return "\n".join(lines)
    return ""
