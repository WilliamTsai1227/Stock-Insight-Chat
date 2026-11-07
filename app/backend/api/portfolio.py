"""Portfolio Insight API（投資組合排程分析）
==========================================
使用者維護「庫存」與「觀察清單」，設定排程後由 backend 的排程器定期呼叫
Zentra 的 stock-suite tools，產生個股分析與整體庫存調整建議。

資料模型見 database/migrations/V011__portfolio_insight.sql；
流程見 specifications/portfolio_insight_spec.md。

安全設計（與 notes.py 一致）：
- 所有端點需登入；user_id 一律取自 JWT
- 每個查詢都帶 user_id 當條件，找不到一律 404
- 使用者只能決定「分析哪些股票、什麼時候」，不能指定 Zentra 網址或 tool
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timezone
from typing import Any, Literal, Optional
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.backend.database.postgresql import get_db
from app.backend.insight.quotes import get_quotes
from app.backend.insight.schedule import ScheduleRule, next_run_after
from app.backend.insight.scheduler import scheduler
from app.backend.insight.zentra import configured as zentra_configured
from app.backend.module.jwt import get_current_user_id

router = APIRouter(tags=["Portfolio Insight"])

#: 每個清單的上限。庫存 30 = Zentra portfolio-advisor 的 holdings 上限。
MAX_ITEMS_PER_LIST = 30

_US_SYMBOL = re.compile(r"^[A-Z]{1,6}(\.[A-Z])?$")      # AAPL、BRK.B
_TW_SYMBOL = re.compile(r"^[0-9]{4}[0-9A-Z]{0,2}$")     # 2330、00878、00632R
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f<>]")


def normalize_symbol(market: str, raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if market == "TW":
        symbol = re.sub(r"\.(TW|TWO)$", "", symbol)
        if not _TW_SYMBOL.match(symbol):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "台股代碼格式不對（例如 2330、00878）。")
    elif not _US_SYMBOL.match(symbol):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "美股代碼格式不對（例如 AAPL、BRK.B）。")
    return symbol


def clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name or len(name) > 60 or _CONTROL_CHARS.search(name):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "股票名稱需為 1–60 字，且不可含 < > 或控制字元。")
    return name


def _float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _json(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


# ════════════════════════════════════════════════════════════════
#  清單
# ════════════════════════════════════════════════════════════════

ListType = Literal["HOLDING", "WATCHLIST"]
Market = Literal["TW", "US"]


class ItemCreate(BaseModel):
    list_type: ListType
    market: Market
    symbol: str = Field(min_length=1, max_length=15)
    name: str = Field(min_length=1, max_length=60)
    cost: Optional[float] = Field(default=None, ge=0, le=1e9)
    shares: Optional[float] = Field(default=None, gt=0, le=1e12)


class ItemUpdate(BaseModel):
    list_type: Optional[ListType] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    cost: Optional[float] = Field(default=None, ge=0, le=1e9)
    shares: Optional[float] = Field(default=None, gt=0, le=1e12)


def _item_row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "list_type": row["list_type"], "market": row["market"],
        "symbol": row["symbol"], "name": row["name"],
        "cost": _float(row["cost"]), "shares": _float(row["shares"]),
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]),
    }


def _check_holding(list_type: str, cost: Optional[float], shares: Optional[float]) -> None:
    if list_type == "HOLDING" and (cost is None or shares is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "庫存需要填成本價與股數。")


async def _check_capacity(db: asyncpg.Connection, user_id: UUID, list_type: str) -> None:
    count = await db.fetchval(
        "SELECT count(*) FROM portfolio_items WHERE user_id = $1 AND list_type = $2", user_id, list_type)
    if count >= MAX_ITEMS_PER_LIST:
        label = "庫存" if list_type == "HOLDING" else "觀察清單"
        raise HTTPException(status.HTTP_409_CONFLICT, f"{label}最多 {MAX_ITEMS_PER_LIST} 檔。")


_ITEM_COLUMNS = "id, list_type, market, symbol, name, cost, shares, created_at, updated_at"


@router.get("/api/portfolio/items")
async def list_items(user_id: UUID = Depends(get_current_user_id), db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch(
        f"SELECT {_ITEM_COLUMNS} FROM portfolio_items WHERE user_id = $1 ORDER BY created_at", user_id)
    items = [_item_row(r) for r in rows]
    return {"status": "success", "data": {
        "holdings": [i for i in items if i["list_type"] == "HOLDING"],
        "watchlist": [i for i in items if i["list_type"] == "WATCHLIST"],
        "limit_per_list": MAX_ITEMS_PER_LIST,
    }}


@router.post("/api/portfolio/items", status_code=status.HTTP_201_CREATED)
async def create_item(body: ItemCreate, user_id: UUID = Depends(get_current_user_id),
                      db: asyncpg.Connection = Depends(get_db)):
    symbol = normalize_symbol(body.market, body.symbol)
    name = clean_name(body.name)
    _check_holding(body.list_type, body.cost, body.shares)
    await _check_capacity(db, user_id, body.list_type)
    try:
        row = await db.fetchrow(
            f"""
            INSERT INTO portfolio_items (user_id, list_type, market, symbol, name, cost, shares)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING {_ITEM_COLUMNS}
            """, user_id, body.list_type, body.market, symbol, name, body.cost, body.shares)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{body.market} {symbol} 已經在清單裡了（同一檔只能在庫存或觀察清單其中一個）。")
    return {"status": "success", "data": _item_row(row)}


@router.patch("/api/portfolio/items/{item_id}")
async def update_item(item_id: UUID, body: ItemUpdate, user_id: UUID = Depends(get_current_user_id),
                      db: asyncpg.Connection = Depends(get_db)):
    current = await db.fetchrow(
        f"SELECT {_ITEM_COLUMNS} FROM portfolio_items WHERE id = $1 AND user_id = $2", item_id, user_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到這檔股票。")
    fields = body.model_dump(exclude_unset=True)
    list_type = fields.get("list_type", current["list_type"])
    cost = fields["cost"] if "cost" in fields else _float(current["cost"])
    shares = fields["shares"] if "shares" in fields else _float(current["shares"])
    name = clean_name(fields["name"]) if "name" in fields else current["name"]
    _check_holding(list_type, cost, shares)
    if list_type != current["list_type"]:
        await _check_capacity(db, user_id, list_type)
    row = await db.fetchrow(
        f"""
        UPDATE portfolio_items SET list_type = $3, name = $4, cost = $5, shares = $6, updated_at = now()
         WHERE id = $1 AND user_id = $2
        RETURNING {_ITEM_COLUMNS}
        """, item_id, user_id, list_type, name, cost, shares)
    return {"status": "success", "data": _item_row(row)}


@router.delete("/api/portfolio/items/{item_id}")
async def delete_item(item_id: UUID, user_id: UUID = Depends(get_current_user_id),
                      db: asyncpg.Connection = Depends(get_db)):
    deleted = await db.fetchval(
        "DELETE FROM portfolio_items WHERE id = $1 AND user_id = $2 RETURNING id", item_id, user_id)
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到這檔股票。")
    return {"status": "success", "data": {"id": str(deleted)}}


@router.get("/api/portfolio/summary")
async def portfolio_summary(user_id: UUID = Depends(get_current_user_id),
                            db: asyncpg.Connection = Depends(get_db)):
    """庫存總覽：即時報價算出的現值、成本、損益、今日漲跌與各檔權重（台幣）。

    報價來自 insight/quotes.py（Yahoo，快取 60 秒）。抓不到報價的那檔 `price` 為 null、
    不計入現值與權重，並列在 `unpriced`；美股抓不到匯率時同樣不計入。
    """
    rows = await db.fetch(
        "SELECT id, market, symbol, name, cost, shares, created_at FROM portfolio_items "
        "WHERE user_id = $1 AND list_type = 'HOLDING' ORDER BY created_at, id", user_id)
    holdings = [dict(r) for r in rows]
    needs_fx = any(h["market"] == "US" for h in holdings)
    quotes, fx = await get_quotes([(h["market"], h["symbol"]) for h in holdings], with_fx=needs_fx) \
        if holdings else ({}, None)
    usd_twd = fx.price if fx else None

    items, unpriced = [], []
    total_value = total_cost = total_prev = 0.0
    by_market = {"TW": 0.0, "US": 0.0}
    for h in holdings:
        q = quotes.get((h["market"], h["symbol"]))
        rate = 1.0 if h["market"] == "TW" else usd_twd
        shares, cost = float(h["shares"]), float(h["cost"])
        item = {
            "id": str(h["id"]), "market": h["market"], "symbol": h["symbol"], "name": h["name"],
            "shares": shares, "cost": cost, "currency": "TWD" if h["market"] == "TW" else "USD",
            "price": q.price if q else None, "previous_close": q.previous_close if q else None,
            "value_twd": None, "cost_twd": None, "pnl_twd": None, "pnl_pct": None,
            "day_change_pct": None, "weight_pct": None,
        }
        if q is None or rate is None:
            unpriced.append(h["symbol"])
        else:
            value, basis = q.price * shares * rate, cost * shares * rate
            item.update({
                "value_twd": round(value, 2), "cost_twd": round(basis, 2),
                "pnl_twd": round(value - basis, 2),
                "pnl_pct": round((q.price / cost - 1) * 100, 2) if cost else None,
                "day_change_pct": round((q.price / q.previous_close - 1) * 100, 2) if q.previous_close else None,
            })
            total_value += value
            total_cost += basis
            total_prev += (q.previous_close or q.price) * shares * rate
            by_market[h["market"]] += value
        items.append(item)

    for item in items:
        if item["value_twd"] is not None and total_value:
            item["weight_pct"] = round(item["value_twd"] / total_value * 100, 2)

    return {"status": "success", "data": {
        "currency": "TWD",
        "usd_twd": usd_twd,
        "total_value_twd": round(total_value, 2),
        "total_cost_twd": round(total_cost, 2),
        "unrealized_pnl_twd": round(total_value - total_cost, 2),
        "unrealized_pnl_pct": round((total_value / total_cost - 1) * 100, 2) if total_cost else None,
        "day_change_pct": round((total_value / total_prev - 1) * 100, 2) if total_prev else None,
        "market_split_pct": {m: (round(v / total_value * 100, 2) if total_value else None)
                             for m, v in by_market.items()},
        "holdings": items,
        "unpriced": unpriced,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }}


# ════════════════════════════════════════════════════════════════
#  排程
# ════════════════════════════════════════════════════════════════

class ScheduleBody(BaseModel):
    enabled: bool = True
    frequency: Literal["DAILY", "WEEKDAYS", "WEEKLY", "MONTHLY"]
    run_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="台北時間 HH:MM")
    weekday: Optional[int] = Field(default=None, ge=1, le=7)
    month_day: Optional[int] = Field(default=None, ge=1, le=28)
    risk_tolerance: Optional[Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]] = None
    cash_twd: Optional[float] = Field(default=None, ge=0, le=1e12)


def _schedule_row(row: Optional[asyncpg.Record]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "enabled": row["enabled"], "frequency": row["frequency"],
        "run_time": row["run_time"].strftime("%H:%M"), "weekday": row["weekday"], "month_day": row["month_day"],
        "risk_tolerance": row["risk_tolerance"], "cash_twd": _float(row["cash_twd"]),
        "timezone": "Asia/Taipei",
        "next_run_at": _iso(row["next_run_at"]), "last_run_at": _iso(row["last_run_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _service_state() -> dict[str, Any]:
    return {"zentra_configured": zentra_configured(), "scheduler_running": scheduler.available}


@router.get("/api/insights/schedule")
async def get_schedule(user_id: UUID = Depends(get_current_user_id), db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM insight_schedules WHERE user_id = $1", user_id)
    return {"status": "success", "data": {"schedule": _schedule_row(row), **_service_state()}}


@router.put("/api/insights/schedule")
async def put_schedule(body: ScheduleBody, user_id: UUID = Depends(get_current_user_id),
                       db: asyncpg.Connection = Depends(get_db)):
    hour, minute = (int(x) for x in body.run_time.split(":"))
    rule = ScheduleRule(frequency=body.frequency, run_time=time(hour, minute),
                        weekday=body.weekday if body.frequency == "WEEKLY" else None,
                        month_day=body.month_day if body.frequency == "MONTHLY" else None)
    try:
        rule.validate()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    next_run = next_run_after(rule, datetime.now(timezone.utc)) if body.enabled else None
    row = await db.fetchrow(
        """
        INSERT INTO insight_schedules
            (user_id, enabled, frequency, run_time, weekday, month_day, risk_tolerance, cash_twd, next_run_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (user_id) DO UPDATE SET
            enabled = EXCLUDED.enabled, frequency = EXCLUDED.frequency, run_time = EXCLUDED.run_time,
            weekday = EXCLUDED.weekday, month_day = EXCLUDED.month_day,
            risk_tolerance = EXCLUDED.risk_tolerance, cash_twd = EXCLUDED.cash_twd,
            next_run_at = EXCLUDED.next_run_at, updated_at = now()
        RETURNING *
        """, user_id, body.enabled, rule.frequency, rule.run_time, rule.weekday, rule.month_day,
        body.risk_tolerance, body.cash_twd, next_run)
    scheduler.poke()
    return {"status": "success", "data": {"schedule": _schedule_row(row), **_service_state()}}


# ════════════════════════════════════════════════════════════════
#  執行紀錄與報告
# ════════════════════════════════════════════════════════════════

_JOB_COLUMNS = ("id, trigger, scheduled_for, status, started_at, finished_at, error, counts, "
                "portfolio_status, portfolio_error, created_at")


def _job_row(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "trigger": row["trigger"], "status": row["status"],
        "scheduled_for": _iso(row["scheduled_for"]), "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]), "created_at": _iso(row["created_at"]),
        "error": row["error"], "counts": _json(row["counts"]),
        "portfolio_status": row["portfolio_status"], "portfolio_error": row["portfolio_error"],
    }


@router.post("/api/insights/jobs", status_code=status.HTTP_202_ACCEPTED)
async def run_now(user_id: UUID = Depends(get_current_user_id), db: asyncpg.Connection = Depends(get_db)):
    if not zentra_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "分析服務（Zentra）尚未設定。")
    if not scheduler.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "排程器沒有在執行，暫時無法分析。")
    has_items = await db.fetchval("SELECT EXISTS (SELECT 1 FROM portfolio_items WHERE user_id = $1)", user_id)
    if not has_items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "請先在庫存或觀察清單加入股票。")
    try:
        row = await db.fetchrow(
            f"""
            INSERT INTO insight_jobs (user_id, trigger, scheduled_for) VALUES ($1, 'MANUAL', now())
            RETURNING {_JOB_COLUMNS}
            """, user_id)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status.HTTP_409_CONFLICT, "已經有一次分析在進行中，請等它完成。")
    scheduler.poke()
    return {"status": "success", "data": _job_row(row)}


@router.get("/api/insights/jobs")
async def list_jobs(limit: int = Query(20, ge=1, le=100), user_id: UUID = Depends(get_current_user_id),
                    db: asyncpg.Connection = Depends(get_db)):
    rows = await db.fetch(
        f"SELECT {_JOB_COLUMNS} FROM insight_jobs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
        user_id, limit)
    return {"status": "success", "data": {"jobs": [_job_row(r) for r in rows]}}


@router.get("/api/insights/jobs/{job_id}")
async def get_job(job_id: UUID, user_id: UUID = Depends(get_current_user_id),
                  db: asyncpg.Connection = Depends(get_db)):
    row = await db.fetchrow(
        f"SELECT {_JOB_COLUMNS}, portfolio_output FROM insight_jobs WHERE id = $1 AND user_id = $2",
        job_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "找不到這次分析。")
    results = await db.fetch(
        """
        SELECT id, list_type, market, symbol, name, cost, shares, tool, status, run_id, output, error,
               chart_png IS NOT NULL AS has_chart, finished_at
          FROM insight_results WHERE job_id = $1 AND user_id = $2
         ORDER BY list_type, market, symbol, tool
        """, job_id, user_id)
    job = _job_row(row)
    job["portfolio_output"] = _json(row["portfolio_output"])
    job["results"] = [{
        "id": str(r["id"]), "list_type": r["list_type"], "market": r["market"], "symbol": r["symbol"],
        "name": r["name"], "cost": _float(r["cost"]), "shares": _float(r["shares"]), "tool": r["tool"],
        "status": r["status"], "run_id": r["run_id"], "output": _json(r["output"]), "error": r["error"],
        "has_chart": r["has_chart"], "finished_at": _iso(r["finished_at"]),
    } for r in results]
    return {"status": "success", "data": job}


@router.get("/api/insights/results/{result_id}/chart")
async def get_chart(result_id: UUID, user_id: UUID = Depends(get_current_user_id),
                    db: asyncpg.Connection = Depends(get_db)):
    png = await db.fetchval(
        "SELECT chart_png FROM insight_results WHERE id = $1 AND user_id = $2 AND tool = 'stock-chart'",
        result_id, user_id)
    if not png:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "沒有這張圖（可能已超過保留期）。")
    return Response(content=bytes(png), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=86400"})

