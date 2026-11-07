"""執行一個 insight job：對清單裡每檔股票呼叫 1–4，再對庫存呼叫 portfolio-advisor。

    驗證 Zentra 契約（tools/list）── 不符 → job FAILED，什麼都不呼叫
      │
      ├─ 庫存 + 觀察清單的每一檔 × {news, chart, technical, fundamental}，use_llm=true
      │    （同時最多 INSIGHT_MAX_CONCURRENCY 個；每完成一個就寫一列 insight_results）
      │
      └─ 有庫存 → portfolio-advisor(use_llm=true, precomputed=上面成功的結果)
                  觀察清單不進這一步

job 的最終狀態：
    SUCCEEDED  全部成功
    PARTIAL    有部分失敗（失敗的那幾列有 error 與 Zentra 的 run_id）
    FAILED     契約驗證失敗、設定錯誤、清單是空的、或全部都失敗
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from app.backend.database.postgresql import get_pool
from app.backend.insight.zentra import SECURITY_TOOLS, ZentraClient, ZentraError

#: 觀察清單沒有成本與股數，所以 chart / technical / fundamental 不帶這兩欄。
_POSITION_TOOLS = {"stock-chart", "stock-technical", "stock-fundamental"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    return float(value) if isinstance(value, Decimal) else float(value)


def tool_arguments(tool: str, item: dict[str, Any]) -> dict[str, Any]:
    """組出一個 tool 的參數。只會產生 Zentra 契約裡有的欄位（多帶會被 Zentra 拒絕）。"""
    args: dict[str, Any] = {"symbol": item["symbol"], "name": item["name"], "market": item["market"],
                            "use_llm": True}
    if tool in _POSITION_TOOLS and item["list_type"] == "HOLDING":
        args["cost"] = _num(item["cost"])
        args["shares"] = _num(item["shares"])
    return args


def split_chart_image(output: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    """把 stock-chart 輸出裡的 base64 圖拿出來（存 BYTEA），輸出裡留一個標記。"""
    image = ((output.get("data") or {}).get("image")) if isinstance(output.get("data"), dict) else None
    if not isinstance(image, dict) or not image.get("base64"):
        return output, None
    try:
        png = base64.b64decode(image["base64"], validate=True)
    except (ValueError, TypeError):
        png = None
    slim = {**output, "data": {**output["data"],
                               "image": {k: v for k, v in image.items() if k != "base64"} | {"stored": png is not None}}}
    return slim, png


def precomputed_for(holdings: list[dict[str, Any]], outputs: dict[tuple[str, str, str], dict[str, Any]]) -> list[dict]:
    """把成功的 1–4 輸出整理成 portfolio-advisor 的 precomputed（圖已經拿掉了）。"""
    part_of = {"stock-news": "news", "stock-chart": "chart",
               "stock-technical": "technical", "stock-fundamental": "fundamental"}
    entries = []
    for item in holdings:
        entry: dict[str, Any] = {"symbol": item["symbol"], "market": item["market"]}
        for tool, part in part_of.items():
            output = outputs.get((item["market"], item["symbol"], tool))
            if output is not None:
                entry[part] = output
        if len(entry) > 2:
            entries.append(entry)
    return entries


async def _heartbeat(db: asyncpg.Connection, job_id: UUID) -> None:
    await db.execute("UPDATE insight_jobs SET heartbeat_at = now() WHERE id = $1", job_id)


async def _finish(job_id: UUID, *, status: str, error: str | None = None, counts: dict | None = None) -> None:
    async with get_pool().acquire() as db:
        await db.execute(
            """
            UPDATE insight_jobs
               SET status = $2, error = $3, counts = COALESCE($4::jsonb, counts),
                   finished_at = now(), heartbeat_at = now()
             WHERE id = $1
            """,
            job_id, status, error, json.dumps(counts) if counts is not None else None,
        )


async def run_job(job_id: UUID) -> None:
    """跑一個已經被認領成 RUNNING 的 job。所有例外都收在這裡，最後一定寫入終態。"""
    try:
        await _run(job_id)
    except Exception as exc:                     # noqa: BLE001 - 不讓排程器的 task 死掉
        print(f"[INSIGHT] job {job_id} crashed: {type(exc).__name__}: {exc}", flush=True)
        await _finish(job_id, status="FAILED", error=f"內部錯誤：{type(exc).__name__}: {exc}"[:1000])


async def _run(job_id: UUID) -> None:
    pool = get_pool()
    async with pool.acquire() as db:
        job = await db.fetchrow("SELECT id, user_id FROM insight_jobs WHERE id = $1", job_id)
        if job is None:
            return
        user_id = job["user_id"]
        items = [dict(r) for r in await db.fetch(
            """
            SELECT list_type, market, symbol, name, cost, shares
              FROM portfolio_items WHERE user_id = $1
             ORDER BY list_type, created_at
            """, user_id)]
        schedule = await db.fetchrow(
            "SELECT risk_tolerance, cash_twd FROM insight_schedules WHERE user_id = $1", user_id)

    if not items:
        await _finish(job_id, status="FAILED", error="庫存與觀察清單都是空的", counts={"total": 0, "succeeded": 0, "failed": 0})
        return

    concurrency = max(1, min(int(os.getenv("INSIGHT_MAX_CONCURRENCY", "8") or 8), 16))
    semaphore = asyncio.Semaphore(concurrency)
    outputs: dict[tuple[str, str, str], dict[str, Any]] = {}
    tally = {"total": 0, "succeeded": 0, "failed": 0}

    try:
        async with ZentraClient() as zentra:
            await zentra.verify()

            async def one(item: dict[str, Any], tool: str) -> None:
                async with semaphore:
                    started = _now()
                    run_id, output, error, png = None, None, None, None
                    try:
                        run_id, full = await zentra.call(tool, tool_arguments(tool, item))
                        output, png = split_chart_image(full) if tool == "stock-chart" else (full, None)
                        outputs[(item["market"], item["symbol"], tool)] = output
                    except ZentraError as exc:
                        error, run_id = str(exc), exc.run_id
                    async with pool.acquire() as db:
                        await db.execute(
                            """
                            INSERT INTO insight_results
                                (job_id, user_id, list_type, market, symbol, name, cost, shares, tool,
                                 status, run_id, output, chart_png, error, started_at, finished_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13,$14,$15,now())
                            ON CONFLICT (job_id, market, symbol, tool) DO NOTHING
                            """,
                            job_id, user_id, item["list_type"], item["market"], item["symbol"], item["name"],
                            item["cost"], item["shares"], tool, "FAILED" if error else "SUCCEEDED",
                            run_id, json.dumps(output, ensure_ascii=False) if output is not None else None,
                            png, error, started,
                        )
                        await _heartbeat(db, job_id)
                    tally["total"] += 1
                    tally["failed" if error else "succeeded"] += 1

            await asyncio.gather(*(one(item, tool) for item in items for tool in SECURITY_TOOLS))

            holdings = [i for i in items if i["list_type"] == "HOLDING"]
            portfolio_status, portfolio_error = "SKIPPED", None
            if holdings:
                arguments: dict[str, Any] = {
                    "holdings": [{"symbol": h["symbol"], "name": h["name"], "market": h["market"],
                                  "cost": _num(h["cost"]), "shares": _num(h["shares"])} for h in holdings],
                    "use_llm": True,
                    "precomputed": precomputed_for(holdings, outputs),
                }
                profile = {}
                if schedule and schedule["risk_tolerance"]:
                    profile["risk_tolerance"] = schedule["risk_tolerance"]
                if schedule and schedule["cash_twd"] is not None:
                    profile["cash_twd"] = _num(schedule["cash_twd"])
                if profile:
                    arguments["investor_profile"] = profile
                run_id, output = None, None
                try:
                    run_id, output = await zentra.call("portfolio-advisor", arguments)
                    portfolio_status = "SUCCEEDED"
                except ZentraError as exc:
                    portfolio_status, portfolio_error, run_id = "FAILED", str(exc), exc.run_id
                async with pool.acquire() as db:
                    await db.execute(
                        """
                        UPDATE insight_jobs
                           SET portfolio_status = $2, portfolio_run_id = $3,
                               portfolio_output = $4::jsonb, portfolio_error = $5, heartbeat_at = now()
                         WHERE id = $1
                        """,
                        job_id, portfolio_status, run_id,
                        json.dumps(output, ensure_ascii=False) if output is not None else None, portfolio_error,
                    )
            else:
                async with pool.acquire() as db:
                    await db.execute("UPDATE insight_jobs SET portfolio_status = 'SKIPPED' WHERE id = $1", job_id)
    except ZentraError as exc:                   # 設定或契約錯誤：一個 tool 都沒呼叫
        await _finish(job_id, status="FAILED", error=str(exc), counts=tally)
        return

    any_ok = tally["succeeded"] > 0 or portfolio_status == "SUCCEEDED"
    any_bad = tally["failed"] > 0 or portfolio_status == "FAILED"
    status = "FAILED" if not any_ok else ("PARTIAL" if any_bad else "SUCCEEDED")
    await _finish(job_id, status=status, counts=tally,
                  error=portfolio_error if status != "SUCCEEDED" and portfolio_error else None)
