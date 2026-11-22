"""通用行事曆的資料來源（Lambda A：calendar-macro）。

每個來源分成兩層：
  - parse_*：純函式，輸入原始回應（HTML / ICS / JSON rows），輸出 Event。單元測試只測這一層。
  - fetch_*：負責 HTTP，呼叫 parse_*。一個來源失敗不影響其他來源（由 lambda_function 收集錯誤）。

| 來源 | 內容 | 可看多遠 |
| :--- | :--- | :--- |
| Fed 官網 FOMC 日程 | FOMC 利率決策（官方） | 一年以上 |
| BEA 發布日程 ICS | GDP、個人所得與支出（PCE）（官方） | 數個月 |
| Nasdaq 經濟日曆 | CPI、非農、PPI、零售…的預期值／前值／實際值 | 約 3 週 |
| Nasdaq 財報日曆 | 巨頭財報日、盤前／盤後、EPS 預估 | 約 7 週 |
| 主計總處近期統計資料發布 | 台灣 CPI、GDP、出口、外銷訂單、景氣燈號 | 逐日查詢 |

每天跑一次，越接近的事件越完整：遠期只有官方日程，接近時 Nasdaq 補上預期值，公布後補上實際值。

### Nasdaq 經濟日曆的兩個怪癖（2026-11 實測，用 2025 年已知的 FOMC 日期驗證）

1. `?date=D` 回傳的是 **D 的前一天** 的事件（2025-12-10 的 FOMC 出現在 `date=2025-12-11`）。
2. 時間欄（標題叫 `gmt`）實際是**固定 UTC-5**，不隨美國日光節約時間變動
   （9 月 FOMC 14:00 EDT 顯示為 13:00，12 月 14:00 EST 顯示為 14:00）。

財報日曆沒有這個偏移。偏移若被 Nasdaq 修正，`check_fomc_alignment` 會拿 Fed 官網日期比對並產生警告。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import httpx

TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
NASDAQ_ECON_TZ = timezone(timedelta(hours=-5))
NASDAQ_ECON_DATE_OFFSET = timedelta(days=-1)

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
}
NASDAQ_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BEA_ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
NASDAQ_ECON_URL = "https://api.nasdaq.com/api/calendar/economicevents"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
DGBAS_URL = "https://www.stat.gov.tw/News_NoticeCalendar_Future.aspx"

# 官方來源的日期／標題優先；Nasdaq 只補數值與時間
SOURCE_PRIORITY = {"fed": 0, "bea": 0, "dgbas": 0, "nasdaq_econ": 1, "nasdaq_earnings": 1}


@dataclass
class Event:
    key: str                         # dedupe_key
    scope: str                       # MACRO | SYMBOL
    type: str                        # ECON_DATA | CENTRAL_BANK | EARNINGS | ...
    title: str
    importance: int                  # 1–3
    market_date: date                # 事件所在市場的當地日期
    source: str
    event_at: Optional[datetime] = None   # UTC；有確切時間才有
    country: Optional[str] = None
    market: Optional[str] = None
    symbol: Optional[str] = None
    name: Optional[str] = None
    indicator: Optional[str] = None
    detail: Optional[str] = None
    time_hint: Optional[str] = None  # BMO | AMC | None
    status: str = "CONFIRMED"        # ESTIMATED | CONFIRMED | RELEASED | CANCELLED
    period: Optional[str] = None
    readings: list = field(default_factory=list)   # [{label, actual, forecast, previous}]
    eps: Optional[dict] = None       # {estimate, actual}
    big_tech: bool = False
    source_url: Optional[str] = None
    sources: list = field(default_factory=list)

    @property
    def event_date(self) -> date:
        """台北日期（前端以此分日）。美股盤後財報在台北是隔天早上。"""
        if self.event_at is not None:
            return self.event_at.astimezone(TAIPEI).date()
        if self.market == "US" and self.time_hint == "AMC":
            return self.market_date + timedelta(days=1)
        return self.market_date


# ── 共用小工具 ─────────────────────────────────────────────────────

def clean_value(raw: Any) -> Optional[str]:
    """Nasdaq 的空值是 '&nbsp;'、' '、'N/A'。"""
    if raw is None:
        return None
    text = html.unescape(str(raw)).replace("\xa0", " ").strip()
    return text if text and text.upper() not in ("N/A", "NA", "-") else None


def month_key(day: date) -> str:
    return f"{day:%Y-%m}"


def daterange(start: date, end: date, weekdays_only: bool = False) -> list[date]:
    days = []
    day = start
    while day <= end:
        if not weekdays_only or day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def fomc_title(sep: bool) -> str:
    return "FOMC 利率決策（含經濟預測 SEP）" if sep else "FOMC 利率決策"


# ── Fed：FOMC 日程 ─────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_FED_YEAR = re.compile(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>')
_FED_MONTH = re.compile(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>')
_FED_DATE = re.compile(r'fomc-meeting__date[^>]*>([^<]+)<')


def parse_fed_calendar(page: str) -> list[Event]:
    """每場會議取「第二天」（14:00 ET 公布決策）。notation vote 等非例會略過。"""
    events: list[Event] = []
    marks = list(_FED_YEAR.finditer(page))
    for i, mark in enumerate(marks):
        year = int(mark.group(1))
        section = page[mark.end(): marks[i + 1].start() if i + 1 < len(marks) else len(page)]
        for month_raw, date_raw in zip(_FED_MONTH.findall(section), _FED_DATE.findall(section)):
            text = date_raw.strip()
            if "notation" in text.lower() or "unscheduled" in text.lower():
                continue
            sep = "*" in text
            days = re.findall(r"\d+", text)
            months = [_MONTHS.get(m.strip()[:3].lower()) for m in month_raw.split("/")]
            if not days or not months or None in months:
                continue
            first, last = int(days[0]), int(days[-1])
            month = months[-1] if (len(months) > 1 and last < first) else months[0]
            try:
                day = date(year, month, last)
            except ValueError:
                continue
            event_at = datetime.combine(day, time(14, 0), NEW_YORK).astimezone(timezone.utc)
            events.append(Event(
                key=f"CENTRAL_BANK:US:FOMC:{month_key(day)}", scope="MACRO", type="CENTRAL_BANK",
                title=fomc_title(sep), importance=3, market_date=day, event_at=event_at,
                country="US", indicator="FOMC", source="fed", source_url=FED_URL,
            ))
    return events


# ── BEA：GDP、個人所得與支出（PCE） ────────────────────────────────

_BEA_MAP = (
    # (summary 開頭, indicator, 標題, 重要度)
    ("Personal Income and Outlays", "US_PCE", "美國 PCE 物價指數（個人所得與支出）", 3),
    # 只要全國 GDP 估算（"GDP (Advance Estimate)…"），不要 "GDP by County…"、"GDP by State…"
    ("GDP (", "US_GDP", "美國 GDP", 2),
)


def parse_bea_ics(ics: str) -> list[Event]:
    unfolded = re.sub(r"\r?\n[ \t]", "", ics)
    events: list[Event] = []
    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        summary_m = re.search(r"^SUMMARY[^:\n]*:(.*)$", block, re.M)
        start_m = re.search(r"^DTSTART[^:\n]*:(\d{8})(?:T(\d{6})(Z?))?", block, re.M)
        if not summary_m or not start_m:
            continue
        summary = summary_m.group(1).strip().replace("\\,", ",").replace("\\;", ";")
        match = next((m for m in _BEA_MAP if summary.startswith(m[0])), None)
        if not match:
            continue
        _, indicator, title, importance = match
        day = datetime.strptime(start_m.group(1), "%Y%m%d").date()
        event_at = None
        if start_m.group(2):
            clock = datetime.strptime(start_m.group(2), "%H%M%S").time()
            tz = timezone.utc if start_m.group(3) == "Z" else NEW_YORK
            event_at = datetime.combine(day, clock, tz).astimezone(timezone.utc)
            day = event_at.astimezone(NEW_YORK).date()
        events.append(Event(
            key=f"ECON_DATA:US:{indicator}:{month_key(day)}", scope="MACRO", type="ECON_DATA",
            title=title, importance=importance, market_date=day, event_at=event_at,
            country="US", indicator=indicator, detail=summary, source="bea",
            source_url="https://www.bea.gov/news/schedule",
        ))
    return events


# ── Nasdaq 經濟日曆（美國指標） ────────────────────────────────────

US_INDICATORS = (
    # (indicator, type, 標題, 重要度, Nasdaq eventName 完全比對)
    ("US_JOBS", "ECON_DATA", "美國非農就業報告", 3,
     ("Nonfarm Payrolls", "Unemployment Rate", "Average Hourly Earnings")),
    ("US_CPI", "ECON_DATA", "美國 CPI 消費者物價指數", 3, ("CPI", "Core CPI")),
    ("US_PCE", "ECON_DATA", "美國 PCE 物價指數（個人所得與支出）", 3, ("PCE Price Index", "Core PCE Price Index")),
    ("FOMC", "CENTRAL_BANK", "FOMC 利率決策", 3, ("Fed Interest Rate Decision",)),
    ("US_GDP", "ECON_DATA", "美國 GDP", 2, ("GDP",)),
    ("US_PPI", "ECON_DATA", "美國 PPI 生產者物價指數", 2, ("PPI", "Core PPI")),
    ("US_RETAIL", "ECON_DATA", "美國零售銷售", 2, ("Retail Sales", "Core Retail Sales")),
    ("US_ISM_MFG", "ECON_DATA", "美國 ISM 製造業 PMI", 2, ("ISM Manufacturing PMI",)),
    ("US_ISM_SVC", "ECON_DATA", "美國 ISM 服務業 PMI", 1, ("ISM Non-Manufacturing PMI", "ISM Services PMI")),
    ("US_CONF", "ECON_DATA", "美國諮商會消費者信心指數", 1, ("CB Consumer Confidence",)),
    ("US_UMICH", "ECON_DATA", "美國密大消費者信心指數", 1, ("Michigan Consumer Sentiment",)),
)
_US_BY_NAME = {name: spec for spec in US_INDICATORS for name in spec[4]}


def parse_nasdaq_econ(rows: Iterable[dict], query_date: date) -> list[Event]:
    """同一指標同一天的多列（例如 CPI 月增、年增）合併成一個事件，各列放進 readings。"""
    market_date = query_date + NASDAQ_ECON_DATE_OFFSET
    grouped: dict[str, Event] = {}
    for row in rows:
        if row.get("country") != "United States":
            continue
        name = clean_value(row.get("eventName"))
        spec = _US_BY_NAME.get(name or "")
        if not spec:
            continue
        indicator, etype, title, importance, _ = spec
        event = grouped.get(indicator)
        if event is None:
            event_at = None
            clock = re.fullmatch(r"(\d{1,2}):(\d{2})", (row.get("gmt") or "").strip())
            if clock:
                local = datetime.combine(market_date, time(int(clock[1]), int(clock[2])), NASDAQ_ECON_TZ)
                event_at = local.astimezone(timezone.utc)
                market_date_local = event_at.astimezone(NEW_YORK).date()
            else:
                market_date_local = market_date
            event = Event(
                key=f"{etype}:US:{indicator}:{month_key(market_date_local)}", scope="MACRO", type=etype,
                title=title, importance=importance, market_date=market_date_local, event_at=event_at,
                country="US", indicator=indicator, source="nasdaq_econ",
            )
            grouped[indicator] = event
        reading = {
            "label": name,
            "actual": clean_value(row.get("actual")),
            "forecast": clean_value(row.get("consensus")),
            "previous": clean_value(row.get("previous")),
        }
        if reading not in event.readings:
            event.readings.append(reading)
    for event in grouped.values():
        label_mom_yoy(event.readings)
        if any(r["actual"] for r in event.readings):
            event.status = "RELEASED"
    return list(grouped.values())


def _pct(text: Optional[str]) -> Optional[float]:
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", (text or "").strip())
    return float(m[1]) if m else None


def label_mom_yoy(readings: list[dict]) -> None:
    """Nasdaq 的月增率與年增率同名（例如兩列都叫 'CPI'），沒有欄位可以區分。

    同名剛好兩列、都是百分比時，數值（前值優先，其次預期、實際）絕對值較小的是月增、較大的是年增。
    只在兩者相差至少 2 倍時標註；分不清楚就維持原名，不猜。
    """
    by_label: dict[str, list[dict]] = {}
    for r in readings:
        by_label.setdefault(r["label"], []).append(r)
    for label, pair in by_label.items():
        if len(pair) != 2:
            continue
        values = []
        for r in pair:
            v = next((x for x in (_pct(r["previous"]), _pct(r["forecast"]), _pct(r["actual"])) if x is not None), None)
            values.append(v)
        if None in values:
            continue
        small, large = sorted(range(2), key=lambda i: abs(values[i]))
        if abs(values[large]) < 2 * abs(values[small]) or values[large] == 0:
            continue
        pair[small]["label"] = f"{label} MoM"
        pair[large]["label"] = f"{label} YoY"


def check_fomc_alignment(nasdaq_events: Iterable[Event], fed_events: Iterable[Event]) -> list[str]:
    """Nasdaq 的 FOMC 日期對不上 Fed 官網 → 日期偏移規則可能變了。"""
    fed_days = {e.market_date for e in fed_events}
    if not fed_days:
        return []
    return [
        f"Nasdaq FOMC 日期 {e.market_date} 不在 Fed 官網日程內，Nasdaq 經濟日曆的日期偏移可能已改變"
        for e in nasdaq_events if e.indicator == "FOMC" and e.market_date not in fed_days
    ]


# ── Nasdaq 財報日曆（巨頭） ────────────────────────────────────────

DEFAULT_BIG_TECH = "US:GOOGL,US:META,US:NVDA,US:AMD,US:AVGO,US:INTC,TW:2330"
BIG_TECH_NAMES = {
    ("US", "GOOGL"): "Alphabet (Google)", ("US", "META"): "Meta", ("US", "NVDA"): "NVIDIA",
    ("US", "AMD"): "AMD", ("US", "AVGO"): "Broadcom", ("US", "INTC"): "Intel", ("TW", "2330"): "台積電",
}
# 台股巨頭在美國的 ADR：財報與法說會同一天，用 ADR 在 Nasdaq 財報日曆上的日期
TW_ADR = {"2330": "TSM", "2303": "UMC"}


def parse_big_tech(spec: str) -> dict[str, tuple[str, str, str]]:
    """'US:NVDA,TW:2330' → {Nasdaq 代碼: (market, symbol, 顯示名稱)}；沒有 ADR 的台股略過。"""
    out: dict[str, tuple[str, str, str]] = {}
    for part in (spec or "").split(","):
        if ":" not in part:
            continue
        market, symbol = (x.strip().upper() for x in part.split(":", 1))
        nasdaq_symbol = symbol if market == "US" else TW_ADR.get(symbol)
        if market not in ("US", "TW") or not symbol or not nasdaq_symbol:
            continue
        out[nasdaq_symbol] = (market, symbol, BIG_TECH_NAMES.get((market, symbol), symbol))
        if nasdaq_symbol == "GOOGL":
            out["GOOG"] = out["GOOGL"]
    return out


_TIME_HINT = {"time-pre-market": "BMO", "time-after-hours": "AMC"}


def fiscal_period(raw: Optional[str]) -> Optional[str]:
    """'Oct/2026' → '2026-10'"""
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*/(\d{4})", (raw or "").strip())
    if not m or m[1].lower() not in _MONTHS:
        return None
    return f"{m[2]}-{_MONTHS[m[1].lower()]:02d}"


def parse_nasdaq_earnings(rows: Iterable[dict], query_date: date,
                          big_tech: dict[str, tuple[str, str, str]]) -> list[Event]:
    events: list[Event] = []
    seen: set[str] = set()
    for row in rows:
        target = big_tech.get((row.get("symbol") or "").upper())
        if not target:
            continue
        market, symbol, name = target
        period = fiscal_period(row.get("fiscalQuarterEnding"))
        key = f"EARNINGS:{market}:{symbol}:{period or month_key(query_date)}"
        if key in seen:
            continue
        seen.add(key)
        estimate = clean_value(row.get("epsForecast"))
        actual = clean_value(row.get("eps"))
        if market == "TW":
            title, hint = f"{name} 法說會／季度財報", None
        else:
            title, hint = f"{name} 財報", _TIME_HINT.get(row.get("time") or "")
        events.append(Event(
            key=key, scope="SYMBOL", type="EARNINGS", title=title, importance=3, market_date=query_date,
            market=market, symbol=symbol, name=name, time_hint=hint, period=period,
            status="RELEASED" if actual else "ESTIMATED",
            eps={"estimate": estimate, "actual": actual} if (estimate or actual) else None,
            detail=f"財季截至 {row.get('fiscalQuarterEnding')}" if row.get("fiscalQuarterEnding") else None,
            big_tech=True, source="nasdaq_earnings",
            source_url=f"https://www.nasdaq.com/market-activity/stocks/{(row.get('symbol') or '').lower()}/earnings",
        ))
    return events


# ── 主計總處：台灣統計資料發布 ─────────────────────────────────────

TW_INDICATORS = (
    # (indicator, 標題, 重要度, 比對函式(發布機關, 資料項目))
    ("TW_CPI", "台灣 CPI 消費者物價指數", 3, lambda dept, name: name == "消費者物價指數"),
    ("TW_GDP", "台灣 GDP／經濟成長率", 3, lambda dept, name: name.startswith("國內生產毛額")),
    ("TW_TRADE", "台灣進出口貿易統計", 3, lambda dept, name: "海關進出口貿易" in name),
    ("TW_FORECAST", "主計總處經濟預測", 2, lambda dept, name: name == "經濟預測"),
    ("TW_EXPORT_ORDERS", "台灣外銷訂單", 2, lambda dept, name: "外銷訂單" in name),
    ("TW_SIGNAL", "台灣景氣對策信號", 2, lambda dept, name: name.endswith("景氣對策信號")),
    ("TW_JOBS", "台灣失業率", 1, lambda dept, name: "失業率" in name and dept == "行政院主計總處"),
    ("TW_IP", "台灣工業生產統計", 1, lambda dept, name: name == "工業生產統計"),
)


def roc_period(notice: Optional[str]) -> Optional[str]:
    """'(11510)' → '2026-10'；'(113~115Q3)' → '2026-Q3'"""
    text = notice or ""
    m = re.search(r"(\d{3})Q([1-4])\)", text)
    if m:
        return f"{int(m[1]) + 1911}-Q{m[2]}"
    m = re.search(r"\((\d{3})(\d{2})\)", text)
    if m and 1 <= int(m[2]) <= 12:
        return f"{int(m[1]) + 1911}-{m[2]}"
    return None


def parse_dgbas_day(page: str, query_date: date) -> list[Event]:
    start = page.find("var VueData = ")
    if start < 0:
        raise ValueError("主計總處頁面結構改變：找不到 VueData")
    data, _ = json.JSONDecoder().raw_decode(page[start + len("var VueData = "):])
    events: list[Event] = []
    seen: set[str] = set()
    for item in data.get("list") or []:
        dept, name = item.get("DeptName") or "", (item.get("name") or "").strip()
        timedata = item.get("timedatas") or {}
        if timedata.get("date") != f"{query_date:%m/%d}":
            continue
        spec = next((s for s in TW_INDICATORS if s[3](dept, name)), None)
        if not spec:
            continue
        indicator, title, importance, _ = spec
        key = f"ECON_DATA:TW:{indicator}:{month_key(query_date)}"
        if key in seen:
            continue
        seen.add(key)
        event_at = None
        clock = re.fullmatch(r"(\d{1,2}):(\d{2})", (timedata.get("time") or "").strip())
        if clock:
            event_at = datetime.combine(query_date, time(int(clock[1]), int(clock[2])), TAIPEI).astimezone(timezone.utc)
        events.append(Event(
            key=key, scope="MACRO", type="ECON_DATA", title=title, importance=importance,
            market_date=query_date, event_at=event_at, country="TW", indicator=indicator,
            period=roc_period(timedata.get("notice")), detail=f"{dept}：{name}",
            source="dgbas", source_url=item.get("ContentUrl") or DGBAS_URL,
        ))
    return events


# ── 合併 ───────────────────────────────────────────────────────────

def merge_events(events: Iterable[Event]) -> list[Event]:
    """同一個 dedupe_key 來自多個來源時：官方來源決定日期與標題，其他來源補空缺的欄位。"""
    groups: dict[str, list[Event]] = {}
    for event in events:
        groups.setdefault(event.key, []).append(event)
    merged: list[Event] = []
    for items in groups.values():
        items.sort(key=lambda e: SOURCE_PRIORITY.get(e.source, 9))
        base = items[0]
        base.sources = sorted({e.source for e in items})
        for other in items[1:]:
            base.event_at = base.event_at or other.event_at
            base.time_hint = base.time_hint or other.time_hint
            base.period = base.period or other.period
            base.detail = base.detail or other.detail
            base.eps = base.eps or other.eps
            if not base.readings and other.readings:
                base.readings = other.readings
            if other.status == "RELEASED":
                base.status = "RELEASED"
            if base.indicator == "FOMC" and "SEP" in other.title:
                base.title = other.title
        merged.append(base)
    return merged


# ── HTTP ───────────────────────────────────────────────────────────

async def _get(client: httpx.AsyncClient, url: str, *, params: Optional[dict] = None,
               headers: Optional[dict] = None, attempts: int = 3) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            last = httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
        except (httpx.TransportError, httpx.TimeoutException) as err:
            last = err
        await asyncio.sleep(1.5 * (attempt + 1))
    raise last or RuntimeError(f"GET {url} 失敗")


async def _per_day(days: list[date], worker, concurrency: int) -> tuple[list[Event], list[str]]:
    """逐日查詢；個別日期失敗只記錯誤，超過一半失敗才算整個來源失敗。"""
    sem = asyncio.Semaphore(concurrency)
    errors: list[str] = []

    async def run(day: date) -> list[Event]:
        async with sem:
            try:
                return await worker(day)
            except Exception as err:   # noqa: BLE001 — 單日失敗不中斷
                errors.append(f"{day}: {type(err).__name__}: {err}")
                return []

    results = await asyncio.gather(*(run(d) for d in days))
    if days and len(errors) > len(days) / 2:
        raise RuntimeError(f"{len(errors)}/{len(days)} 天查詢失敗，例如 {errors[0]}")
    return [e for chunk in results for e in chunk], errors[:5]


async def fetch_fed(client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    response = await _get(client, FED_URL, headers=BROWSER_HEADERS)
    events = parse_fed_calendar(response.text)
    if not events:
        raise ValueError("Fed 頁面結構改變：解析不到任何 FOMC 會議")
    return events, []


async def fetch_bea(client: httpx.AsyncClient) -> tuple[list[Event], list[str]]:
    response = await _get(client, BEA_ICS_URL, headers=BROWSER_HEADERS)
    events = parse_bea_ics(response.text)
    if not events:
        raise ValueError("BEA ICS 解析不到任何 GDP / PCE 發布")
    return events, []


async def fetch_nasdaq_econ(client: httpx.AsyncClient, start: date, end: date) -> tuple[list[Event], list[str]]:
    async def worker(day: date) -> list[Event]:
        query = day - NASDAQ_ECON_DATE_OFFSET
        response = await _get(client, NASDAQ_ECON_URL, params={"date": query.isoformat()}, headers=NASDAQ_HEADERS)
        rows = ((response.json() or {}).get("data") or {}).get("rows") or []
        return parse_nasdaq_econ(rows, query)
    return await _per_day(daterange(start, end, weekdays_only=True), worker, concurrency=4)


async def fetch_nasdaq_earnings(client: httpx.AsyncClient, start: date, end: date,
                                big_tech: dict[str, tuple[str, str, str]]) -> tuple[list[Event], list[str]]:
    async def worker(day: date) -> list[Event]:
        response = await _get(client, NASDAQ_EARNINGS_URL, params={"date": day.isoformat()}, headers=NASDAQ_HEADERS)
        rows = ((response.json() or {}).get("data") or {}).get("rows") or []
        return parse_nasdaq_earnings(rows, day, big_tech)
    return await _per_day(daterange(start, end, weekdays_only=True), worker, concurrency=4)


async def fetch_dgbas(client: httpx.AsyncClient, start: date, end: date) -> tuple[list[Event], list[str]]:
    async def worker(day: date) -> list[Event]:
        response = await _get(client, DGBAS_URL, params={"n": "3907", "date": day.isoformat()},
                              headers=BROWSER_HEADERS)
        return parse_dgbas_day(response.text, day)
    return await _per_day(daterange(start, end, weekdays_only=True), worker, concurrency=3)
