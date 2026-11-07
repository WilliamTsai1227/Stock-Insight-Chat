"""排程時間計算（純函式，不碰 DB）。

時間一律以**台北時間（UTC+8）**解讀：台灣沒有日光節約時間，用固定 offset
就是精確的，不依賴容器裡有沒有 tzdata。DB 存的 next_run_at 是 TIMESTAMPTZ（UTC）。

    DAILY     每天 run_time
    WEEKDAYS  週一到週五 run_time（台股/美股週末休市，週末跑只會拿到一樣的資料）
    WEEKLY    每週 weekday（ISO：1=週一 … 7=週日）run_time
    MONTHLY   每月 month_day（1–28）run_time
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")

FREQUENCIES = ("DAILY", "WEEKDAYS", "WEEKLY", "MONTHLY")


@dataclass(frozen=True)
class ScheduleRule:
    frequency: str
    run_time: time
    weekday: int | None = None
    month_day: int | None = None

    def validate(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency 必須是 {', '.join(FREQUENCIES)} 之一")
        if self.frequency == "WEEKLY" and not (self.weekday and 1 <= self.weekday <= 7):
            raise ValueError("每週排程需要 weekday（1=週一 … 7=週日）")
        if self.frequency == "MONTHLY" and not (self.month_day and 1 <= self.month_day <= 28):
            raise ValueError("每月排程需要 month_day（1–28）")


def _matches(rule: ScheduleRule, day: date) -> bool:
    if rule.frequency == "DAILY":
        return True
    if rule.frequency == "WEEKDAYS":
        return day.isoweekday() <= 5
    if rule.frequency == "WEEKLY":
        return day.isoweekday() == rule.weekday
    return day.day == rule.month_day


def next_run_after(rule: ScheduleRule, after: datetime) -> datetime:
    """嚴格晚於 `after` 的下一個執行時間（回傳 UTC）。

    `after` 必須帶時區。排程器在認領一個到期的排程後，會用「現在」呼叫這個函式：
    如果服務停機錯過了好幾個時間點，**只補跑一次**（就是認領的那次），然後往後排，
    不會醒來後連跑好幾輪一樣的分析。
    """
    if after.tzinfo is None:
        raise ValueError("after 必須帶時區")
    rule.validate()
    local = after.astimezone(TAIPEI)
    # 每月最多 31 天就一定會遇到 month_day；多看幾天當保險
    for offset in range(0, 40):
        day = local.date() + timedelta(days=offset)
        if not _matches(rule, day):
            continue
        candidate = datetime.combine(day, rule.run_time.replace(tzinfo=None), tzinfo=TAIPEI)
        if candidate > local:
            return candidate.astimezone(timezone.utc)
    raise RuntimeError("找不到下一個執行時間（不應該發生）")
