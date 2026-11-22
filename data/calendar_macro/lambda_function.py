"""
通用行事曆 Lambda（calendar-macro）
====================================
EventBridge 每天觸發：抓美國／台灣總經數據公布日、FOMC、巨頭財報日，寫入 MongoDB `market_events`。
規格見 specifications/market_calendar_spec.md；各資料來源見 sources.py。

- 冪等：以 `dedupe_key` upsert，重跑不會產生重複事件。同一期改期時 key 不變，只更新日期並記入 `history`。
- 一個來源失敗不影響其他來源；**全部來源都失敗**才讓 Lambda 失敗（觸發 CloudWatch 告警）。
- 已公布（RELEASED）的事件不會因為之後的抓取少了實際值而退回未公布。
- `source = "manual"` 的文件（人工補登）不會被覆寫或清除。
- 執行結果寫入 `calendar_meta`（_id = "macro"），backend 用它顯示資料新鮮度。

Event 參數（皆可省略）：
  {
    "dry_run": false,          # true：只抓取與合併，不寫入，回傳前 20 筆預覽
    "days_back": 7,            # 往回抓幾天（補實際值）
    "days_ahead": 60,          # Nasdaq 財報與主計總處往後查幾天
    "sources": ["fed", "bea", "nasdaq_econ", "nasdaq_earnings", "dgbas"]
  }

必要環境變數：MONGO_URI
選用環境變數：MONGO_DB（預設 stock_insight）、CALENDAR_BIG_TECH（預設見 sources.DEFAULT_BIG_TECH）
"""

from __future__ import annotations

import asyncio
import os
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from pymongo import ASCENDING, MongoClient, UpdateOne

import sources as src

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "stock_insight")
EVENTS = "market_events"
META = "calendar_meta"

ALL_SOURCES = ("fed", "bea", "nasdaq_econ", "nasdaq_earnings", "dgbas")
HORIZON_DAYS = 400        # 官方日程（Fed / BEA）最遠保留到一年多以後
RETENTION_DAYS = 400      # 早於此的事件刪除（保留一年做去年同期比較）
HISTORY_LIMIT = 10
NASDAQ_ECON_AHEAD = 35    # Nasdaq 經濟日曆實測約只排到 3 週後，多查幾天無妨


def today_taipei() -> date:
    return datetime.now(src.TAIPEI).date()


# ── 抓取 ───────────────────────────────────────────────────────────

async def collect(selected: tuple[str, ...], today: date, days_back: int, days_ahead: int,
                  big_tech: dict) -> tuple[list[src.Event], dict[str, dict], list[str]]:
    start = today - timedelta(days=days_back)
    jobs = {
        "fed": lambda c: src.fetch_fed(c),
        "bea": lambda c: src.fetch_bea(c),
        "nasdaq_econ": lambda c: src.fetch_nasdaq_econ(c, start, today + timedelta(days=NASDAQ_ECON_AHEAD)),
        "nasdaq_earnings": lambda c: src.fetch_nasdaq_earnings(c, start, today + timedelta(days=days_ahead), big_tech),
        "dgbas": lambda c: src.fetch_dgbas(c, start, today + timedelta(days=days_ahead)),
    }
    report: dict[str, dict] = {}
    by_source: dict[str, list[src.Event]] = {}

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        async def run(name: str) -> None:
            began = _time.monotonic()
            try:
                events, errors = await jobs[name](client)
                by_source[name] = events
                report[name] = {"ok": True, "count": len(events), "partial_errors": errors}
            except Exception as err:   # noqa: BLE001 — 單一來源失敗不中斷
                report[name] = {"ok": False, "count": 0, "error": f"{type(err).__name__}: {err}"[:500]}
            report[name]["seconds"] = round(_time.monotonic() - began, 1)

        await asyncio.gather(*(run(n) for n in selected))

    warnings = src.check_fomc_alignment(by_source.get("nasdaq_econ", []), by_source.get("fed", []))
    events = [e for name in selected for e in by_source.get(name, [])]
    low, high = start, today + timedelta(days=HORIZON_DAYS)
    merged = [e for e in src.merge_events(events) if low <= e.event_date <= high]
    return merged, report, warnings


# ── 寫入 ───────────────────────────────────────────────────────────

def build_update(event: src.Event, existing: Optional[dict], now: datetime) -> dict:
    """組出一筆 upsert 的 update 文件（純函式，方便測試）。"""
    new_date = event.event_date.isoformat()
    status = event.status
    if existing and existing.get("status") == "RELEASED" and status != "RELEASED":
        status = "RELEASED"

    fields: dict[str, Any] = {
        "dedupe_key": event.key,
        "scope": event.scope,
        "type": event.type,
        "title": event.title,
        "importance": event.importance,
        "event_date": new_date,
        "market_date": event.market_date.isoformat(),
        "event_at": event.event_at,
        "time_hint": event.time_hint,
        "status": status,
        "country": event.country,
        "market": event.market,
        "symbol": event.symbol,
        "name": event.name,
        "indicator": event.indicator,
        "detail": event.detail,
        "period": event.period,
        "big_tech": event.big_tech,
        "source": event.source,
        "sources": event.sources or [event.source],
        "source_url": event.source_url,
        "fetched_at": now,
    }
    if event.readings:
        fields["readings"] = event.readings
    if event.eps:
        fields["eps"] = event.eps

    changed = (
        existing is None
        or existing.get("event_date") != new_date
        or existing.get("status") != status
        or (event.readings and existing.get("readings") != event.readings)
        or (event.eps and existing.get("eps") != event.eps)
    )
    if changed:
        fields["updated_at"] = now

    update: dict[str, Any] = {"$set": fields, "$setOnInsert": {"created_at": now}}
    if not event.readings:
        update["$setOnInsert"]["readings"] = []
    if not event.eps:
        update["$setOnInsert"]["eps"] = None
    if existing and existing.get("event_date") and existing["event_date"] != new_date:
        update["$push"] = {"history": {
            "$each": [{"at": now, "field": "event_date", "from": existing["event_date"], "to": new_date}],
            "$slice": -HISTORY_LIMIT,
        }}
    return update


def ensure_indexes(db) -> None:
    coll = db[EVENTS]
    coll.create_index([("dedupe_key", ASCENDING)], unique=True, name="uq_dedupe_key")
    coll.create_index([("scope", ASCENDING), ("event_date", ASCENDING)], name="scope_event_date")
    coll.create_index([("market", ASCENDING), ("symbol", ASCENDING), ("event_date", ASCENDING)],
                      name="market_symbol_event_date")
    coll.create_index([("big_tech", ASCENDING), ("event_date", ASCENDING)], name="big_tech_event_date")


def write_events(db, events: list[src.Event], now: datetime) -> dict[str, int]:
    coll = db[EVENTS]
    keys = [e.key for e in events]
    existing = {
        doc["dedupe_key"]: doc
        for doc in coll.find({"dedupe_key": {"$in": keys}},
                             {"dedupe_key": 1, "event_date": 1, "status": 1, "readings": 1, "eps": 1, "source": 1})
    }
    ops = []
    skipped_manual = 0
    rescheduled = 0
    for event in events:
        old = existing.get(event.key)
        if old and old.get("source") == "manual":
            skipped_manual += 1
            continue
        update = build_update(event, old, now)
        if "$push" in update:
            rescheduled += 1
        ops.append(UpdateOne({"dedupe_key": event.key}, update, upsert=True))
    result = coll.bulk_write(ops, ordered=False) if ops else None
    cutoff = (today_taipei() - timedelta(days=RETENTION_DAYS)).isoformat()
    deleted = coll.delete_many({"event_date": {"$lt": cutoff}, "source": {"$ne": "manual"}}).deleted_count
    return {
        "upserted": result.upserted_count if result else 0,
        "modified": result.modified_count if result else 0,
        "rescheduled": rescheduled,
        "skipped_manual": skipped_manual,
        "deleted_old": deleted,
    }


def preview(event: src.Event) -> dict:
    return {
        "key": event.key, "title": event.title, "event_date": event.event_date.isoformat(),
        "event_at": event.event_at.isoformat() if event.event_at else None,
        "status": event.status, "sources": event.sources, "readings": event.readings[:2], "eps": event.eps,
    }


# ── 進入點 ─────────────────────────────────────────────────────────

def run(event: dict) -> dict:
    event = event or {}
    selected = tuple(s for s in (event.get("sources") or ALL_SOURCES) if s in ALL_SOURCES)
    if not selected:
        raise ValueError(f"sources 必須是 {ALL_SOURCES} 的子集合")
    days_back = max(0, min(int(event.get("days_back", 7)), 30))
    days_ahead = max(7, min(int(event.get("days_ahead", 60)), 120))
    dry_run = bool(event.get("dry_run", False))
    big_tech = src.parse_big_tech(os.getenv("CALENDAR_BIG_TECH") or src.DEFAULT_BIG_TECH)

    began = _time.monotonic()
    today = today_taipei()
    events, report, warnings = asyncio.run(collect(selected, today, days_back, days_ahead, big_tech))
    ok_sources = [n for n, r in report.items() if r["ok"]]
    for name, r in report.items():
        line = f"[CALENDAR] {name}: ok={r['ok']} count={r['count']} {r.get('error') or ''}"
        print(line.strip(), flush=True)
    for w in warnings:
        print(f"[CALENDAR] WARNING {w}", flush=True)

    summary: dict[str, Any] = {
        "today": today.isoformat(), "events": len(events), "sources": report, "warnings": warnings,
        "dry_run": dry_run,
    }
    if dry_run:
        summary["preview"] = [preview(e) for e in sorted(events, key=lambda e: e.event_date)[:20]]
        summary["seconds"] = round(_time.monotonic() - began, 1)
        return summary

    if not MONGO_URI:
        raise RuntimeError("缺少 MONGO_URI")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    try:
        db = client[MONGO_DB]
        now = datetime.now(timezone.utc)
        if ok_sources:
            ensure_indexes(db)
            summary["write"] = write_events(db, events, now)
        meta_set: dict[str, Any] = {"last_run_at": now, "sources": report, "warnings": warnings,
                                    "event_count": len(events)}
        if ok_sources:
            meta_set["last_success_at"] = now
        db[META].update_one({"_id": "macro"}, {"$set": meta_set}, upsert=True)
    finally:
        client.close()

    summary["seconds"] = round(_time.monotonic() - began, 1)
    if not ok_sources:
        raise RuntimeError(f"所有資料來源都失敗：{ {n: r.get('error') for n, r in report.items()} }")
    return summary


def lambda_handler(event, context):  # noqa: ARG001 — Lambda 介面
    return run(event)


if __name__ == "__main__":
    import json
    import sys

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"dry_run": True}
    print(json.dumps(run(payload), ensure_ascii=False, indent=2, default=str))
