"""通用行事曆 Lambda（data/calendar_macro）：各來源解析、合併、upsert 文件組裝。"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "data" / "calendar_macro"))

import lambda_function as lf  # noqa: E402
import sources as src  # noqa: E402


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── Fed ────────────────────────────────────────────────────────────

FED_HTML = """
<h4><a id="42828">2026 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-xs-5"><strong>October</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div>
<div class="fomc-meeting__month col-xs-5"><strong>December</strong></div>
<div class="fomc-meeting__date col-xs-4">8-9*</div>
<h4><a id="45694">2027 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col-xs-5"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-xs-4">30-1</div>
<div class="fomc-meeting__month col-xs-5"><strong>August</strong></div>
<div class="fomc-meeting__date col-xs-4 col-lg-2">22 (notation vote)</div>
"""


def test_fed_takes_second_day_and_sep_flag():
    events = {e.key: e for e in src.parse_fed_calendar(FED_HTML)}
    dec = events["CENTRAL_BANK:US:FOMC:2026-12"]
    assert dec.market_date == date(2026, 12, 9)
    assert dec.event_at == utc(2026, 12, 9, 19, 0)          # 14:00 EST
    assert dec.event_date == date(2026, 12, 10)             # 台北凌晨 03:00
    assert "SEP" in dec.title
    oct_ = events["CENTRAL_BANK:US:FOMC:2026-10"]
    assert oct_.event_at == utc(2026, 10, 28, 18, 0)        # 14:00 EDT
    assert "SEP" not in oct_.title


def test_fed_month_wrap_and_notation_vote_skipped():
    events = {e.key: e for e in src.parse_fed_calendar(FED_HTML)}
    assert events["CENTRAL_BANK:US:FOMC:2027-05"].market_date == date(2027, 5, 1)
    assert not any(k.endswith("2027-08") for k in events)


# ── BEA ────────────────────────────────────────────────────────────

BEA_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:GDP (Second Estimate) and Corporate Profits\\, 3rd Quarter 2026\r\n"
    "DTSTART;VALUE=DATE-TIME:20261125T133000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:Personal Income and Outlays\\, October\r\n  2026\r\n"   # RFC 5545 折行：去掉 CRLF + 一個空白
    "DTSTART;VALUE=DATE-TIME:20261125T133000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:GDP by County and Personal Income by County\\, 2025\r\n"
    "DTSTART;VALUE=DATE-TIME:20261202T133000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nSUMMARY:U.S. International Trade in Goods and Services\\, October 2026\r\n"
    "DTSTART;VALUE=DATE-TIME:20261208T133000Z\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_bea_keeps_national_gdp_and_pce_only():
    events = {e.key: e for e in src.parse_bea_ics(BEA_ICS)}
    assert set(events) == {"ECON_DATA:US:US_GDP:2026-11", "ECON_DATA:US:US_PCE:2026-11"}
    pce = events["ECON_DATA:US:US_PCE:2026-11"]
    assert pce.detail == "Personal Income and Outlays, October 2026"   # 折行已還原、\, 已還原
    assert pce.event_at == utc(2026, 11, 25, 13, 30)
    assert pce.event_date == date(2026, 11, 25)                        # 台北 21:30


# ── Nasdaq 經濟日曆 ────────────────────────────────────────────────

def test_nasdaq_econ_date_offset_and_fixed_utc_minus_5():
    rows = [
        {"gmt": "13:00", "country": "United States", "eventName": "Fed Interest Rate Decision",
         "actual": "4.25%", "consensus": " ", "previous": "4.50%"},
        {"gmt": "08:30", "country": "Canada", "eventName": "CPI", "actual": "&nbsp;"},
    ]
    events = src.parse_nasdaq_econ(rows, date(2025, 9, 18))   # 查 9/18 → 9/17 的事件
    assert len(events) == 1
    fomc = events[0]
    assert fomc.market_date == date(2025, 9, 17)
    assert fomc.event_at == utc(2025, 9, 17, 18, 0)            # 13:00 UTC-5 = 14:00 EDT
    assert fomc.key == "CENTRAL_BANK:US:FOMC:2025-09"
    assert fomc.status == "RELEASED"
    assert fomc.readings == [{"label": "Fed Interest Rate Decision", "actual": "4.25%",
                              "forecast": None, "previous": "4.50%"}]


def test_nasdaq_econ_groups_rows_and_dedupes():
    row = {"gmt": "08:30", "country": "United States", "actual": "&nbsp;", "consensus": "0.3%", "previous": "0.2%"}
    rows = [dict(row, eventName="CPI"), dict(row, eventName="CPI"), dict(row, eventName="Core CPI"),
            dict(row, eventName="CPI Index")]
    events = src.parse_nasdaq_econ(rows, date(2026, 12, 11))
    assert len(events) == 1
    assert [r["label"] for r in events[0].readings] == ["CPI", "Core CPI"]
    assert events[0].status == "CONFIRMED"
    assert events[0].event_date == date(2026, 12, 10)          # 美東 12/10 08:30 = 台北 12/10 21:30


def test_mom_yoy_labels_only_when_clear():
    def rows(prev_a, prev_b):
        base = {"gmt": "08:30", "country": "United States", "eventName": "CPI", "actual": "&nbsp;", "consensus": " "}
        return [dict(base, previous=prev_a), dict(base, previous=prev_b)]
    [e] = src.parse_nasdaq_econ(rows("3.7%", "0.4%"), date(2026, 12, 11))
    assert [(r["label"], r["previous"]) for r in e.readings] == [("CPI YoY", "3.7%"), ("CPI MoM", "0.4%")]
    [e] = src.parse_nasdaq_econ(rows("0.3%", "0.2%"), date(2026, 12, 11))   # 分不清楚 → 不標
    assert [r["label"] for r in e.readings] == ["CPI", "CPI"]


def test_fomc_alignment_warning():
    fed = src.parse_fed_calendar(FED_HTML)
    rows = [{"gmt": "14:00", "country": "United States", "eventName": "Fed Interest Rate Decision"}]
    aligned = src.parse_nasdaq_econ(rows, date(2026, 12, 10))
    shifted = src.parse_nasdaq_econ(rows, date(2026, 12, 9))
    assert src.check_fomc_alignment(aligned, fed) == []
    assert len(src.check_fomc_alignment(shifted, fed)) == 1


# ── Nasdaq 財報 ────────────────────────────────────────────────────

def test_big_tech_spec():
    big = src.parse_big_tech("US:NVDA, TW:2330, TW:9999, XX:AAA, US:GOOGL")
    assert big["NVDA"] == ("US", "NVDA", "NVIDIA")
    assert big["TSM"] == ("TW", "2330", "台積電")
    assert big["GOOG"] == big["GOOGL"]
    assert "9999" not in str(big) and "AAA" not in big


def test_earnings_after_hours_moves_to_next_taipei_day():
    big = src.parse_big_tech(src.DEFAULT_BIG_TECH)
    rows = [
        {"symbol": "AVGO", "time": "time-after-hours", "epsForecast": "$3.45", "fiscalQuarterEnding": "Oct/2026"},
        {"symbol": "TSM", "time": "time-pre-market", "epsForecast": "$3.10", "fiscalQuarterEnding": "Dec/2026"},
        {"symbol": "DELL", "time": "time-after-hours"},
    ]
    events = {e.key: e for e in src.parse_nasdaq_earnings(rows, date(2026, 12, 9), big)}
    assert set(events) == {"EARNINGS:US:AVGO:2026-10", "EARNINGS:TW:2330:2026-12"}
    avgo = events["EARNINGS:US:AVGO:2026-10"]
    assert avgo.time_hint == "AMC" and avgo.event_date == date(2026, 12, 10)
    assert avgo.status == "ESTIMATED" and avgo.eps == {"estimate": "$3.45", "actual": None}
    assert avgo.big_tech and avgo.scope == "SYMBOL"
    tsmc = events["EARNINGS:TW:2330:2026-12"]
    assert tsmc.title == "台積電 法說會／季度財報" and tsmc.event_date == date(2026, 12, 9)


def test_fiscal_period():
    assert src.fiscal_period("Oct/2026") == "2026-10"
    assert src.fiscal_period("bad") is None


# ── 主計總處 ───────────────────────────────────────────────────────

def dgbas_page(items):
    return f"<script>var VueData = {json.dumps({'list': items, 'Weeks': []}, ensure_ascii=False)};</script>"


def test_dgbas_maps_indicators_and_period():
    items = [
        {"DeptName": "行政院主計總處", "name": "消費者物價指數", "ContentUrl": "https://x/cpi",
         "timedatas": {"date": "12/08", "time": "16:00", "notice": "(11511)"}},
        {"DeptName": "行政院主計總處", "name": "國內生產毛額、國民所得、經濟成長率及平減指數",
         "timedatas": {"date": "12/08", "time": "16:00", "notice": "(113~115Q3)"}},
        {"DeptName": "交通部", "name": "高鐵客運統計", "timedatas": {"date": "12/08", "time": "16:20"}},
        {"DeptName": "行政院主計總處", "name": "消費者物價指數", "timedatas": {"date": "12/09", "time": "16:00"}},
    ]
    events = {e.key: e for e in src.parse_dgbas_day(dgbas_page(items), date(2026, 12, 8))}
    assert set(events) == {"ECON_DATA:TW:TW_CPI:2026-12", "ECON_DATA:TW:TW_GDP:2026-12"}
    cpi = events["ECON_DATA:TW:TW_CPI:2026-12"]
    assert cpi.event_at == utc(2026, 12, 8, 8, 0) and cpi.period == "2026-11"
    assert cpi.source_url == "https://x/cpi"
    assert events["ECON_DATA:TW:TW_GDP:2026-12"].period == "2026-Q3"


def test_dgbas_layout_change_raises():
    with pytest.raises(ValueError):
        src.parse_dgbas_day("<html>改版了</html>", date(2026, 12, 8))


# ── 合併與寫入 ─────────────────────────────────────────────────────

def test_merge_official_date_wins_and_values_filled():
    fed = [e for e in src.parse_fed_calendar(FED_HTML) if e.key.endswith("2026-12")][0]
    rows = [{"gmt": "14:00", "country": "United States", "eventName": "Fed Interest Rate Decision",
             "actual": "&nbsp;", "previous": "4.00%"}]
    nasdaq = src.parse_nasdaq_econ(rows, date(2026, 12, 10))[0]
    nasdaq.title = "FOMC 利率決策"
    [merged] = src.merge_events([nasdaq, fed])
    assert merged.source == "fed" and merged.sources == ["fed", "nasdaq_econ"]
    assert "SEP" in merged.title
    assert merged.readings[0]["previous"] == "4.00%"


def make_event(**kw):
    base = dict(key="ECON_DATA:US:US_CPI:2026-12", scope="MACRO", type="ECON_DATA", title="CPI",
                importance=3, market_date=date(2026, 12, 10), source="nasdaq_econ",
                event_at=utc(2026, 12, 10, 13, 30), country="US")
    base.update(kw)
    return src.Event(**base)


NOW = utc(2026, 11, 22, 0, 0)


def test_build_update_new_event():
    update = lf.build_update(make_event(), None, NOW)
    assert update["$set"]["event_date"] == "2026-12-10"
    assert update["$set"]["updated_at"] == NOW
    assert update["$setOnInsert"]["readings"] == [] and "$push" not in update


def test_build_update_reschedule_pushes_history():
    old = {"event_date": "2026-12-09", "status": "CONFIRMED", "readings": []}
    update = lf.build_update(make_event(), old, NOW)
    assert update["$push"]["history"]["$each"][0] == {
        "at": NOW, "field": "event_date", "from": "2026-12-09", "to": "2026-12-10"}


def test_build_update_released_never_reverts():
    old = {"event_date": "2026-12-10", "status": "RELEASED",
           "readings": [{"label": "CPI", "actual": "0.3%", "forecast": None, "previous": None}]}
    update = lf.build_update(make_event(status="CONFIRMED"), old, NOW)
    assert update["$set"]["status"] == "RELEASED"
    assert "readings" not in update["$set"]           # 沒有新數值就不覆寫既有的實際值
    assert "updated_at" not in update["$set"]
