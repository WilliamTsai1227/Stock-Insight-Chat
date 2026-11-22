"""
投資行事曆 API
==============
讀 Lambda（data/calendar_macro）寫入 MongoDB 的 `market_events`，依使用者的庫存／觀察清單
（PostgreSQL `portfolio_items`）在查詢時組合出個人行事曆。不另外存每個人的行事曆。

- GET /api/calendar/events     行事曆事件（tab：ALL / MACRO / HOLDING / WATCHLIST）

規格見 specifications/market_calendar_spec.md。
P1 只有通用事件（總經＋巨頭）；個股事件由 P2 的 Lambda B 補上，本端點不用改。

Mongo 連線沿用 market_feed 的 client（MONGO_URI / FEED_MONGO_URI），沒設定時回 503。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.errors import PyMongoError

from app.backend.api.market_feed import _db_error, _get_db, _jsonable
from app.backend.database.postgresql import get_db
from app.backend.module.jwt import get_current_user_id

router = APIRouter(tags=["Calendar"])

EVENTS = "market_events"
META = "calendar_meta"
MAX_RANGE_DAYS = 93
MAX_EVENTS = 1500
STALE_HOURS = 36

Tab = Literal["ALL", "MACRO", "HOLDING", "WATCHLIST"]

_PROJECTION = {
    "_id": 0, "dedupe_key": 1, "scope": 1, "type": 1, "title": 1, "detail": 1, "country": 1,
    "market": 1, "symbol": 1, "name": 1, "indicator": 1, "importance": 1, "event_date": 1,
    "market_date": 1, "event_at": 1, "time_hint": 1, "status": 1, "period": 1, "readings": 1,
    "eps": 1, "big_tech": 1, "history": 1, "source": 1, "sources": 1, "source_url": 1,
}

MACRO_COND: dict[str, Any] = {"$or": [{"scope": "MACRO"}, {"big_tech": True}]}


def parse_range(from_: str, to: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from／to 需為 YYYY-MM-DD")
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "to 不可早於 from")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"查詢區間最長 {MAX_RANGE_DAYS} 天")
    return start, end


def symbol_cond(pairs: set[tuple[str, str]]) -> Optional[dict[str, Any]]:
    if not pairs:
        return None
    return {"$or": [{"market": m, "symbol": s} for m, s in sorted(pairs)]}


def build_query(tab: str, start: date, end: date, min_importance: int,
                holdings: set[tuple[str, str]], watchlist: set[tuple[str, str]]) -> Optional[dict[str, Any]]:
    """組 Mongo 查詢；該分頁一定沒有結果時回 None（例如觀察清單是空的）。"""
    if tab == "MACRO":
        scope = MACRO_COND
    elif tab == "HOLDING":
        scope = symbol_cond(holdings)
    elif tab == "WATCHLIST":
        scope = symbol_cond(watchlist)
    else:
        mine = symbol_cond(holdings | watchlist)
        scope = {"$or": MACRO_COND["$or"] + mine["$or"]} if mine else MACRO_COND
    if scope is None:
        return None
    query: dict[str, Any] = {"event_date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, **scope}
    if min_importance > 1:
        query["importance"] = {"$gte": min_importance}
    return query


def tags_of(doc: dict, holdings: set[tuple[str, str]], watchlist: set[tuple[str, str]]) -> list[str]:
    tags = []
    if doc.get("scope") == "MACRO" or doc.get("big_tech"):
        tags.append("MACRO")
    pair = (doc.get("market"), doc.get("symbol"))
    if pair in holdings:
        tags.append("HOLDING")
    if pair in watchlist:
        tags.append("WATCHLIST")
    return tags


def as_utc(value: Any) -> Any:
    """pymongo 預設讀回不帶時區的 datetime（實際是 UTC）；不補時區的話 isoformat 沒有 +00:00，前端會當成本地時間。"""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def serialize(doc: dict, holdings: set[tuple[str, str]], watchlist: set[tuple[str, str]]) -> dict:
    history = [h for h in (doc.get("history") or []) if h.get("field") == "event_date"]
    out = {k: doc.get(k) for k in _PROJECTION if k not in ("_id", "dedupe_key", "history")}
    out["event_at"] = as_utc(out.get("event_at"))
    out["key"] = doc.get("dedupe_key")
    out["readings"] = doc.get("readings") or []
    out["tags"] = tags_of(doc, holdings, watchlist)
    out["rescheduled"] = bool(history)
    out["reschedule_from"] = history[-1].get("from") if history else None
    return _jsonable(out)


def sort_key(doc: dict) -> tuple:
    at = as_utc(doc.get("event_at"))
    return (doc.get("event_date") or "", at is None, at or datetime.min.replace(tzinfo=timezone.utc),
            -(doc.get("importance") or 0))


def freshness(meta: Optional[dict], now: datetime) -> dict:
    last = as_utc((meta or {}).get("last_success_at"))
    return {
        "macro_fetched_at": last.isoformat() if last else None,
        "stale": last is None or now - last > timedelta(hours=STALE_HOURS),
        "warnings": (meta or {}).get("warnings") or [],
    }


async def user_symbols(db: asyncpg.Connection, user_id: UUID) -> tuple[set, set]:
    rows = await db.fetch("SELECT list_type, market, symbol FROM portfolio_items WHERE user_id = $1", user_id)
    holdings = {(r["market"], r["symbol"]) for r in rows if r["list_type"] == "HOLDING"}
    watchlist = {(r["market"], r["symbol"]) for r in rows if r["list_type"] == "WATCHLIST"}
    return holdings, watchlist


@router.get("/api/calendar/events")
async def list_events(
    from_: str = Query(..., alias="from", description="YYYY-MM-DD（台北日期）"),
    to: str = Query(..., description="YYYY-MM-DD（台北日期），區間最長 93 天"),
    tab: Tab = Query("ALL"),
    min_importance: int = Query(1, ge=1, le=3),
    user_id: UUID = Depends(get_current_user_id),
    db: asyncpg.Connection = Depends(get_db),
):
    start, end = parse_range(from_, to)
    holdings, watchlist = await user_symbols(db, user_id)
    query = build_query(tab, start, end, min_importance, holdings, watchlist)
    mongo = _get_db()
    try:
        docs = [] if query is None else await mongo[EVENTS].find(query, _PROJECTION).to_list(MAX_EVENTS)
        meta = await mongo[META].find_one({"_id": "macro"})
    except PyMongoError as err:
        raise _db_error(err)

    docs.sort(key=sort_key)
    return {"status": "success", "data": {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "tab": tab,
        "events": [serialize(d, holdings, watchlist) for d in docs],
        "counts": {"holding": len(holdings), "watchlist": len(watchlist)},
        "data_freshness": freshness(meta, datetime.now(timezone.utc)),
    }}
