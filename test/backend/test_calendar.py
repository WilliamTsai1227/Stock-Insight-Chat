"""投資行事曆 API：查詢組裝（通用／庫存／觀察清單）、標籤、改期、資料新鮮度、端點流程。"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.backend.api import calendar as cal

H = {("US", "NVDA"), ("TW", "2330")}
W = {("US", "AMD")}
D1, D2 = date(2026, 11, 22), date(2026, 12, 31)


def test_parse_range_limits():
    assert cal.parse_range("2026-11-22", "2026-12-31") == (D1, D2)
    for bad in [("2026-11-22", "2026-11-01"), ("2026-11-22", "2027-03-01"), ("x", "2026-12-01")]:
        with pytest.raises(HTTPException) as exc:
            cal.parse_range(*bad)
        assert exc.value.status_code == 422


def test_query_macro_ignores_portfolio():
    q = cal.build_query("MACRO", D1, D2, 1, H, W)
    assert q["$or"] == [{"scope": "MACRO"}, {"big_tech": True}]
    assert q["event_date"] == {"$gte": "2026-11-22", "$lte": "2026-12-31"}
    assert "importance" not in q


def test_query_holding_and_watchlist_use_own_symbols():
    q = cal.build_query("HOLDING", D1, D2, 2, H, W)
    assert q["$or"] == [{"market": "TW", "symbol": "2330"}, {"market": "US", "symbol": "NVDA"}]
    assert q["importance"] == {"$gte": 2}
    assert cal.build_query("WATCHLIST", D1, D2, 1, H, W)["$or"] == [{"market": "US", "symbol": "AMD"}]


def test_query_empty_list_returns_none():
    assert cal.build_query("WATCHLIST", D1, D2, 1, H, set()) is None


def test_query_all_combines_macro_and_mine():
    q = cal.build_query("ALL", D1, D2, 1, set(), W)
    assert {"scope": "MACRO"} in q["$or"] and {"market": "US", "symbol": "AMD"} in q["$or"]
    assert cal.build_query("ALL", D1, D2, 1, set(), set())["$or"] == cal.MACRO_COND["$or"]


def test_tags_big_tech_in_holdings_is_both():
    doc = {"scope": "SYMBOL", "big_tech": True, "market": "US", "symbol": "NVDA"}
    assert cal.tags_of(doc, H, W) == ["MACRO", "HOLDING"]
    assert cal.tags_of({"scope": "MACRO"}, H, W) == ["MACRO"]


def test_serialize_reschedule_and_types():
    at = datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc)
    doc = {"dedupe_key": "EARNINGS:US:AMD:2026-12", "scope": "SYMBOL", "market": "US", "symbol": "AMD",
           "event_date": "2026-12-11", "event_at": at, "readings": None,
           "history": [{"field": "event_date", "from": "2026-12-09", "to": "2026-12-11"}]}
    out = cal.serialize(doc, H, W)
    assert out["key"] == "EARNINGS:US:AMD:2026-12"
    assert out["rescheduled"] is True and out["reschedule_from"] == "2026-12-09"
    assert out["event_at"] == at.isoformat() and out["readings"] == []
    assert out["tags"] == ["WATCHLIST"] and "history" not in out


def test_serialize_naive_mongo_datetime_is_utc():
    """pymongo 讀回的 datetime 不帶時區；輸出一定要有 +00:00，否則前端會把 UTC 當台北時間（差 8 小時）。"""
    out = cal.serialize({"dedupe_key": "k", "event_at": datetime(2026, 12, 10, 13, 30)}, set(), set())
    assert out["event_at"] == "2026-12-10T13:30:00+00:00"


def test_sort_by_date_then_time_untimed_last():
    t = lambda h: datetime(2026, 12, 1, h, tzinfo=timezone.utc)  # noqa: E731
    docs = [{"event_date": "2026-12-01", "event_at": None, "importance": 3},
            {"event_date": "2026-12-01", "event_at": t(15), "importance": 1},
            {"event_date": "2026-11-30", "event_at": None, "importance": 1},
            {"event_date": "2026-12-01", "event_at": t(13), "importance": 2}]
    docs.sort(key=cal.sort_key)
    assert [(d["event_date"], d["importance"]) for d in docs] == [
        ("2026-11-30", 1), ("2026-12-01", 2), ("2026-12-01", 1), ("2026-12-01", 3)]


def test_freshness():
    now = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
    assert cal.freshness(None, now)["stale"] is True
    fresh = cal.freshness({"last_success_at": now - timedelta(hours=5), "warnings": ["x"]}, now)
    assert fresh["stale"] is False and fresh["warnings"] == ["x"]
    assert cal.freshness({"last_success_at": (now - timedelta(hours=40)).replace(tzinfo=None)}, now)["stale"]


# ── 端點流程（假的 PostgreSQL 與 Mongo） ────────────────────────────

class _PG:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        assert "portfolio_items" in sql
        return self.rows


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, n):
        return self.docs[:n]


class _Coll:
    def __init__(self, docs=None, one=None):
        self.docs, self.one, self.queries = docs or [], one, []

    def find(self, query, projection):
        self.queries.append(query)
        return _Cursor(self.docs)

    async def find_one(self, query):
        return self.one


class _Mongo(dict):
    pass


async def test_endpoint_watchlist_empty_skips_mongo_query(monkeypatch):
    events = _Coll(docs=[{"dedupe_key": "x"}])
    mongo = _Mongo({cal.EVENTS: events, cal.META: _Coll(one=None)})
    monkeypatch.setattr(cal, "_get_db", lambda: mongo)
    pg = _PG([{"list_type": "HOLDING", "market": "US", "symbol": "NVDA"}])
    out = await cal.list_events(from_="2026-11-22", to="2026-12-31", tab="WATCHLIST", min_importance=1,
                                user_id=uuid.uuid4(), db=pg)
    assert out["data"]["events"] == [] and events.queries == []
    assert out["data"]["counts"] == {"holding": 1, "watchlist": 0}
    assert out["data"]["data_freshness"]["stale"] is True


async def test_endpoint_all_tags_and_sorts(monkeypatch):
    docs = [
        {"dedupe_key": "b", "scope": "MACRO", "event_date": "2026-12-11", "importance": 3},
        {"dedupe_key": "a", "scope": "SYMBOL", "big_tech": True, "market": "US", "symbol": "NVDA",
         "event_date": "2026-12-01", "importance": 3},
    ]
    mongo = _Mongo({cal.EVENTS: _Coll(docs=docs),
                    cal.META: _Coll(one={"last_success_at": datetime.now(timezone.utc)})})
    monkeypatch.setattr(cal, "_get_db", lambda: mongo)
    pg = _PG([{"list_type": "HOLDING", "market": "US", "symbol": "NVDA"}])
    out = await cal.list_events(from_="2026-11-22", to="2026-12-31", tab="ALL", min_importance=1,
                                user_id=uuid.uuid4(), db=pg)
    events = out["data"]["events"]
    assert [e["key"] for e in events] == ["a", "b"]
    assert events[0]["tags"] == ["MACRO", "HOLDING"]
    assert out["data"]["data_freshness"]["stale"] is False
