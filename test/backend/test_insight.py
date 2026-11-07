"""投資組合排程分析：排程時間、Zentra 契約驗證、參數組裝、client 呼叫。"""

import base64
import json
from datetime import datetime, time, timezone

import httpx
import pytest

from app.backend.insight import zentra as zmod
from app.backend.insight.runner import precomputed_for, split_chart_image, tool_arguments
from app.backend.insight.schedule import TAIPEI, ScheduleRule, next_run_after
from app.backend.insight.zentra import (
    TOOLS, ZentraClient, ZentraConfig, ZentraConfigError, ZentraContractError, ZentraError, check_contract,
)


def tpe(*args) -> datetime:
    return datetime(*args, tzinfo=TAIPEI)


# ── 排程時間 ─────────────────────────────────────────────────────────

def test_daily_later_today_and_tomorrow():
    rule = ScheduleRule("DAILY", time(14, 30))
    assert next_run_after(rule, tpe(2026, 11, 9, 9, 0)) == tpe(2026, 11, 9, 14, 30)
    # 剛好在時間點上：要「嚴格晚於」，所以是明天
    assert next_run_after(rule, tpe(2026, 11, 9, 14, 30)) == tpe(2026, 11, 10, 14, 30)


def test_result_is_utc():
    out = next_run_after(ScheduleRule("DAILY", time(6, 0)), tpe(2026, 11, 9, 1, 0))
    assert out.tzinfo == timezone.utc
    assert out == datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc)


def test_weekdays_skip_weekend():
    rule = ScheduleRule("WEEKDAYS", time(14, 30))
    # 2026-11-13 是週五，15:00 已過 → 下週一 11-16
    assert next_run_after(rule, tpe(2026, 11, 13, 15, 0)) == tpe(2026, 11, 16, 14, 30)


def test_weekly():
    rule = ScheduleRule("WEEKLY", time(8, 0), weekday=6)   # 週六
    assert next_run_after(rule, tpe(2026, 11, 9, 0, 0)) == tpe(2026, 11, 14, 8, 0)


def test_monthly_rolls_to_next_month_and_year():
    rule = ScheduleRule("MONTHLY", time(9, 0), month_day=5)
    assert next_run_after(rule, tpe(2026, 12, 5, 9, 0)) == tpe(2027, 1, 5, 9, 0)


def test_missed_slots_run_once_then_move_forward():
    """停機三天後醒來：用「現在」算下一次，不會一口氣補三次。"""
    rule = ScheduleRule("DAILY", time(14, 30))
    now = tpe(2026, 11, 12, 10, 0)
    assert next_run_after(rule, now) == tpe(2026, 11, 12, 14, 30)


@pytest.mark.parametrize("rule", [
    ScheduleRule("HOURLY", time(1, 0)),
    ScheduleRule("WEEKLY", time(1, 0)),
    ScheduleRule("MONTHLY", time(1, 0), month_day=31),
])
def test_invalid_rules(rule):
    with pytest.raises(ValueError):
        next_run_after(rule, tpe(2026, 1, 1, 0, 0))


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        next_run_after(ScheduleRule("DAILY", time(1, 0)), datetime(2026, 1, 1))


# ── 設定 ─────────────────────────────────────────────────────────────

def test_config_allows_http_private_address_and_derives_api_url():
    config = ZentraConfig.from_env({"ZENTRA_MCP_URL": "http://10.0.3.7:8000/mcp", "ZENTRA_MCP_KEY": "zmcp_live_x"})
    assert config.api_url == "http://10.0.3.7:8000"


@pytest.mark.parametrize("env", [
    {},
    {"ZENTRA_MCP_URL": "http://z/mcp"},
    {"ZENTRA_MCP_URL": "http://z/mcp", "ZENTRA_MCP_KEY": "zk_live_wrong_kind"},
    {"ZENTRA_MCP_URL": "ftp://z/mcp", "ZENTRA_MCP_KEY": "zmcp_live_x"},
])
def test_config_errors(env):
    with pytest.raises(ZentraConfigError):
        ZentraConfig.from_env(env)


# ── 契約驗證 ─────────────────────────────────────────────────────────

def good_tools() -> list[dict]:
    tools = []
    for local, spec in TOOLS.items():
        tools.append({
            "name": spec["mcp"],
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "required": sorted(spec["required"]),
                            "properties": {p: {} for p in spec["properties"]}},
            "outputSchema": {"type": "object", "properties": {"tool": {"const": local}}},
        })
    return tools


def test_contract_ok():
    assert check_contract(good_tools()) == []


def test_contract_detects_missing_tool_and_changed_fields():
    tools = good_tools()
    tools = [t for t in tools if t["name"] != "stock-news__default"]
    tools[0]["inputSchema"]["properties"]["period"] = {}
    tools[1]["inputSchema"]["additionalProperties"] = True
    problems = check_contract(tools)
    assert any("stock-news__default" in p for p in problems)
    assert any("輸入欄位" in p for p in problems)
    assert any("additionalProperties" in p for p in problems)


# ── 參數組裝 ─────────────────────────────────────────────────────────

HOLDING = {"list_type": "HOLDING", "market": "TW", "symbol": "2330", "name": "台積電", "cost": 900, "shares": 1000}
WATCH = {"list_type": "WATCHLIST", "market": "US", "symbol": "NVDA", "name": "NVIDIA", "cost": None, "shares": None}


def test_tool_arguments_match_contract():
    for tool in ("stock-news", "stock-chart", "stock-technical", "stock-fundamental"):
        for item in (HOLDING, WATCH):
            args = tool_arguments(tool, item)
            spec = TOOLS[tool]
            assert set(args) <= spec["properties"], (tool, item["list_type"])
            assert spec["required"] <= set(args)
            assert args["use_llm"] is True
    assert "cost" not in tool_arguments("stock-news", HOLDING)        # news 不收成本
    assert "cost" not in tool_arguments("stock-chart", WATCH)         # 觀察清單沒有成本
    assert tool_arguments("stock-chart", HOLDING)["shares"] == 1000.0


def test_split_chart_image_moves_png_out():
    png = b"\x89PNG fake"
    output = {"tool": "stock-chart", "data": {"image": {"mime_type": "image/png",
                                                         "base64": base64.b64encode(png).decode()}}}
    slim, raw = split_chart_image(output)
    assert raw == png
    assert "base64" not in slim["data"]["image"] and slim["data"]["image"]["stored"] is True
    assert "base64" in output["data"]["image"]      # 不改到原物件


def test_precomputed_only_holdings_with_results():
    outputs = {("TW", "2330", "stock-technical"): {"tool": "stock-technical"}}
    entries = precomputed_for([HOLDING, {**HOLDING, "symbol": "2317"}], outputs)
    assert entries == [{"symbol": "2330", "market": "TW", "technical": {"tool": "stock-technical"}}]


# ── client（MockTransport 模擬 Zentra）────────────────────────────────

def fake_zentra(run_output: dict, *, status: str = "SUCCEEDED", call_error: str | None = None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp":
            assert request.headers["authorization"] == "Bearer zmcp_live_test"
            body = json.loads(request.content)
            calls.append(body)
            if body["method"] == "tools/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": good_tools()}})
            assert body["params"]["arguments"]["_async"] is True
            if call_error:
                result = {"isError": True, "content": [{"type": "text", "text": call_error}]}
            else:
                result = {"structuredContent": {"run_id": "run-1", "status": "QUEUED"}}
            # 用 SSE 回，確認兩種格式都吃
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result})}\n\n")
        if request.url.path == "/v1/runs/run-1":
            return httpx.Response(200, json={"run_id": "run-1", "status": status, "output": run_output,
                                             "error": {"code": "USER_CODE_ERROR", "message": "boom"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


async def make_client(transport) -> ZentraClient:
    config = ZentraConfig.from_env({"ZENTRA_MCP_URL": "http://zentra/mcp", "ZENTRA_MCP_KEY": "zmcp_live_test",
                                    "INSIGHT_POLL_SECONDS": "1"})
    client = ZentraClient(config)
    client._http = httpx.AsyncClient(transport=transport)
    return client


ARGS = {"symbol": "2330", "name": "台積電", "market": "TW", "use_llm": True}


async def test_client_happy_path():
    out = {"tool": "stock-technical", "security": {"symbol": "2330", "market": "TW"}, "data": {}}
    transport, calls = fake_zentra(out)
    client = await make_client(transport)
    await client.verify()
    run_id, output = await client.call("stock-technical", ARGS)
    assert (run_id, output) == ("run-1", out)
    assert calls[-1]["params"]["name"] == "stock-technical__default"


async def test_client_rejects_output_from_another_tool_or_symbol():
    transport, _ = fake_zentra({"tool": "stock-news", "security": {"symbol": "2330", "market": "TW"}})
    client = await make_client(transport)
    with pytest.raises(ZentraError, match="輸出不是"):
        await client.call("stock-technical", ARGS)

    transport, _ = fake_zentra({"tool": "stock-technical", "security": {"symbol": "2317", "market": "TW"}})
    client = await make_client(transport)
    with pytest.raises(ZentraError, match="不是我們要的"):
        await client.call("stock-technical", ARGS)


async def test_client_reports_failed_run_with_run_id():
    transport, _ = fake_zentra({}, status="FAILED")
    client = await make_client(transport)
    with pytest.raises(ZentraError) as info:
        await client.call("stock-technical", ARGS)
    assert info.value.run_id == "run-1" and "USER_CODE_ERROR" in str(info.value)


async def test_client_refuses_arguments_outside_contract():
    transport, calls = fake_zentra({})
    client = await make_client(transport)
    with pytest.raises(ZentraError, match="參數不對"):
        await client.call("stock-news", {**ARGS, "cost": 1})
    assert calls == []          # 根本沒送出去


async def test_verify_raises_contract_error(monkeypatch):
    monkeypatch.setattr(zmod, "check_contract", lambda tools: ["缺少 tool x"])
    transport, _ = fake_zentra({})
    client = await make_client(transport)
    with pytest.raises(ZentraContractError):
        await client.verify()


# ── 即時報價（庫存總覽）──────────────────────────────────────────────

def test_quote_parse_uses_previous_daily_close():
    from app.backend.insight.quotes import _parse
    payload = {"chart": {"result": [{
        "meta": {"regularMarketPrice": 2135.0, "currency": "TWD"},
        "timestamp": [1, 2, 3],
        "indicators": {"quote": [{"close": [2200.0, None, 2240.0, 2135.0][1:]}]},
    }]}}
    quote = _parse(payload, "2330.TW")
    assert quote.price == 2135.0 and quote.previous_close == 2240.0 and quote.currency == "TWD"


def test_quote_parse_empty_result():
    from app.backend.insight.quotes import _parse
    assert _parse({"chart": {"result": None}}, "X") is None
