"""Insight 排程器：在 backend 行程內跑的背景迴圈（lifespan 啟動、關閉時停）。

## 準時

每一輪結束時查「最近的 next_run_at」，**睡到那個時間點**（最長 INSIGHT_TICK_SECONDS，
預設 30 秒，用來接住剛被使用者改過的排程與手動觸發）。所以觸發誤差約 1 秒，不是 30 秒。

## 不重複、不遺漏

狀態全部在 DB，不在記憶體：

1. **認領到期排程**（一個交易）：
   `SELECT … WHERE enabled AND next_run_at <= now() FOR UPDATE SKIP LOCKED`
   → 為每一列 INSERT 一個 SCHEDULED job（`scheduled_for = next_run_at`）
   → 把 next_run_at 推到「現在之後」的下一個時間點。
   `uq_insight_jobs_scheduled(user_id, scheduled_for)` 保證同一個時間點只會有一個 job；
   `SKIP LOCKED` 讓多個 backend 實例同時跑也不會搶同一列。
   服務停機錯過的時間點：醒來後**只補跑一次**，然後往後排（schedule.next_run_after）。
2. **認領 QUEUED job**（排程建的、或使用者手動按的）→ RUNNING，交給 runner。
3. **收屍**：RUNNING 但 heartbeat 超過 INSIGHT_STALE_MINUTES（預設 30）沒動的 job →
   FAILED（多半是 backend 在執行中被重啟）。
4. **保留期**：每小時一次，把超過 INSIGHT_CHART_RETENTION_DAYS（預設 30）的 K 線圖清掉、
   超過 INSIGHT_RETENTION_DAYS（預設 180）的 job 整筆刪掉。

同一位使用者同時最多一個 QUEUED/RUNNING job（`uq_insight_jobs_active`）：上一輪還沒跑完時
下一個排程到了，這一次會被略過並記一筆 log，排程照樣往後推。

`INSIGHT_SCHEDULER_ENABLED=0` 可關閉（例如只想跑 API 的實例）。
沒設定 Zentra（ZENTRA_MCP_URL / ZENTRA_MCP_KEY）時排程器不啟動。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.backend.database.postgresql import get_pool
from app.backend.insight import runner
from app.backend.insight.schedule import ScheduleRule, next_run_after
from app.backend.insight.zentra import configured


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def _log(message: str) -> None:
    print(f"[INSIGHT] {message}", flush=True)


def rule_of(row: asyncpg.Record) -> ScheduleRule:
    return ScheduleRule(frequency=row["frequency"], run_time=row["run_time"],
                        weekday=row["weekday"], month_day=row["month_day"])


class InsightScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._jobs: set[asyncio.Task] = set()
        self._last_cleanup: datetime | None = None
        self.tick_seconds = _env_int("INSIGHT_TICK_SECONDS", 30, 5, 300)
        self.stale_minutes = _env_int("INSIGHT_STALE_MINUTES", 30, 5, 24 * 60)
        self.max_running = _env_int("INSIGHT_MAX_RUNNING_JOBS", 2, 1, 20)

    # ── 生命週期 ────────────────────────────────────────────────────

    def start(self) -> None:
        if os.getenv("INSIGHT_SCHEDULER_ENABLED", "1").lower() in ("0", "false", "no"):
            _log("scheduler disabled by INSIGHT_SCHEDULER_ENABLED")
            return
        if not configured():
            _log("scheduler not started: ZENTRA_MCP_URL / ZENTRA_MCP_KEY not set")
            return
        self._task = asyncio.create_task(self._loop(), name="insight-scheduler")
        _log(f"scheduler started (tick ≤ {self.tick_seconds}s)")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        for job in list(self._jobs):
            job.cancel()
        await asyncio.gather(self._task, *self._jobs, return_exceptions=True)
        self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def poke(self) -> None:
        """API 建了手動 job 或改了排程時呼叫：立刻醒來，不用等這一輪睡完。"""
        self._wake.set()

    # ── 迴圈 ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
                sleep_for = await self._seconds_until_next_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:              # noqa: BLE001 - DB 暫時不通等，下一輪再試
                _log(f"tick failed: {type(exc).__name__}: {exc}")
                sleep_for = self.tick_seconds
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

    async def _seconds_until_next_due(self) -> float:
        async with get_pool().acquire() as db:
            nearest = await db.fetchval(
                "SELECT min(next_run_at) FROM insight_schedules WHERE enabled AND next_run_at IS NOT NULL")
        if nearest is None:
            return float(self.tick_seconds)
        delta = (nearest - datetime.now(timezone.utc)).total_seconds()
        # 稍微晚一點醒（+0.2s），確保 next_run_at <= now() 成立
        return max(0.2, min(float(self.tick_seconds), delta + 0.2))

    async def _tick(self) -> None:
        await self._claim_due_schedules()
        await self._reap_stale_jobs()
        await self._start_queued_jobs()
        await self._cleanup_if_due()

    async def _claim_due_schedules(self) -> None:
        now = datetime.now(timezone.utc)
        async with get_pool().acquire() as db:
            async with db.transaction():
                rows = await db.fetch(
                    """
                    SELECT user_id, frequency, run_time, weekday, month_day, next_run_at
                      FROM insight_schedules
                     WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= now()
                     ORDER BY next_run_at
                     LIMIT 50
                       FOR UPDATE SKIP LOCKED
                    """)
                for row in rows:
                    created = None
                    try:
                        async with db.transaction():   # savepoint：撞到 active job 只影響這一列
                            created = await db.fetchval(
                                """
                                INSERT INTO insight_jobs (user_id, trigger, scheduled_for)
                                VALUES ($1, 'SCHEDULED', $2)
                                ON CONFLICT (user_id, scheduled_for) WHERE trigger = 'SCHEDULED' DO NOTHING
                                RETURNING id
                                """, row["user_id"], row["next_run_at"])
                    except asyncpg.UniqueViolationError:
                        _log(f"user {row['user_id']} still has a job running; skipped {row['next_run_at']:%Y-%m-%d %H:%M}Z")
                    await db.execute(
                        "UPDATE insight_schedules SET next_run_at = $2, last_run_at = $3, updated_at = now() "
                        "WHERE user_id = $1",
                        row["user_id"], next_run_after(rule_of(row), now), row["next_run_at"])
                    if created:
                        _log(f"queued scheduled job {created} for {row['next_run_at']:%Y-%m-%d %H:%M}Z")

    async def _reap_stale_jobs(self) -> None:
        async with get_pool().acquire() as db:
            reaped = await db.fetch(
                """
                UPDATE insight_jobs
                   SET status = 'FAILED', finished_at = now(),
                       error = '執行中斷（backend 可能在執行中重啟），請重新執行'
                 WHERE status = 'RUNNING'
                   AND COALESCE(heartbeat_at, started_at) < now() - make_interval(mins => $1)
                RETURNING id
                """, self.stale_minutes)
        for row in reaped:
            _log(f"reaped stale job {row['id']}")

    async def _start_queued_jobs(self) -> None:
        self._jobs = {t for t in self._jobs if not t.done()}
        free = self.max_running - len(self._jobs)
        if free <= 0:
            return
        async with get_pool().acquire() as db:
            claimed = await db.fetch(
                """
                UPDATE insight_jobs SET status = 'RUNNING', started_at = now(), heartbeat_at = now()
                 WHERE id IN (SELECT id FROM insight_jobs WHERE status = 'QUEUED'
                               ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED)
                RETURNING id
                """, free)
        for row in claimed:
            job_id: UUID = row["id"]
            _log(f"starting job {job_id}")
            task = asyncio.create_task(runner.run_job(job_id), name=f"insight-job-{job_id}")
            self._jobs.add(task)
            task.add_done_callback(lambda _t: self._wake.set())   # 跑完立刻看還有沒有排隊的

    async def _cleanup_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_cleanup and (now - self._last_cleanup).total_seconds() < 3600:
            return
        self._last_cleanup = now
        chart_days = _env_int("INSIGHT_CHART_RETENTION_DAYS", 30, 1, 3650)
        job_days = _env_int("INSIGHT_RETENTION_DAYS", 180, 1, 3650)
        async with get_pool().acquire() as db:
            await db.execute(
                "UPDATE insight_results SET chart_png = NULL "
                "WHERE chart_png IS NOT NULL AND finished_at < now() - make_interval(days => $1)", chart_days)
            await db.execute(
                "DELETE FROM insight_jobs WHERE status NOT IN ('QUEUED', 'RUNNING') "
                "AND created_at < now() - make_interval(days => $1)", job_days)


scheduler = InsightScheduler()
