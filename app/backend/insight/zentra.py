"""Zentra stock-suite 的專用 client（給排程用，不給聊天的 LLM 用）。

## 為什麼不用 tools/mcp_client.py

那支是給聊天 Router 用的：讓 LLM 自己挑工具、結果截到 12,000 字、丟掉圖片、
正式環境拒絕 http 與私有 IP。排程要的剛好相反：

- **tool 寫死**，不經過任何 LLM 判斷 —— 只會呼叫 `TOOLS` 裡那五個名字。
- **先驗證契約**：每個 job 開始前跑一次 `tools/list`，確認五個 tool 都在，而且
  輸入欄位、`additionalProperties: false`、輸出的 `tool` 常數都跟這裡預期的一樣。
  對不上就整個 job 失敗，**不做部分執行**（Zentra 那邊改版了卻沒跟著改這裡時，
  與其送錯參數，不如明確停下來）。
- **非同步呼叫 + 輪詢**：MCP 同步呼叫最多等 60 秒，新聞與整體建議常常超過。
  一律帶 `_async: true` 拿 `run_id`，再輪詢 Zentra 的 `GET /v1/runs/{run_id}`。
- **核對輸出**：輸出的 `tool` 與 `security` 必須是我們叫的那個、那一檔。
- **允許 http 與私有 IP**：Zentra 跟 backend 在同一個 VPC 內網，由部署者在 env 指定，
  使用者無法改。內網之外的安全靠 security group 只放行 backend。

規格見 specifications/portfolio_insight_spec.md；Zentra 端的契約見
Zentra repo 的 examples/stock-suite/README.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

# 我們會呼叫的 tool：本地名稱 → (MCP tool 名稱, 輸入欄位集合, 必填欄位)
_SECURITY_REQUIRED = {"symbol", "name", "market", "use_llm"}
TOOLS: dict[str, dict[str, Any]] = {
    "stock-news": {
        "mcp": "stock-news__default",
        "properties": {"symbol", "name", "market", "use_llm"},
        "required": _SECURITY_REQUIRED,
    },
    "stock-chart": {
        "mcp": "stock-chart__default",
        "properties": {"symbol", "name", "market", "cost", "shares", "use_llm"},
        "required": _SECURITY_REQUIRED,
    },
    "stock-technical": {
        "mcp": "stock-technical__default",
        "properties": {"symbol", "name", "market", "cost", "shares", "use_llm"},
        "required": _SECURITY_REQUIRED,
    },
    "stock-fundamental": {
        "mcp": "stock-fundamental__default",
        "properties": {"symbol", "name", "market", "cost", "shares", "use_llm"},
        "required": _SECURITY_REQUIRED,
    },
    "portfolio-advisor": {
        "mcp": "portfolio-advisor__default",
        "properties": {"holdings", "use_llm", "investor_profile", "precomputed"},
        "required": {"holdings", "use_llm"},
    },
}
SECURITY_TOOLS = ("stock-news", "stock-chart", "stock-technical", "stock-fundamental")

_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class ZentraError(RuntimeError):
    """呼叫 Zentra 失敗。`run_id` 有值時可以到 Zentra Console 追。"""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ZentraConfigError(ZentraError):
    """沒設定或設定錯誤。"""


class ZentraContractError(ZentraError):
    """Zentra 上的 tool 跟這裡預期的不一樣（缺 tool、欄位不同）。"""


@dataclass(frozen=True)
class ZentraConfig:
    mcp_url: str
    api_url: str
    key: str
    request_timeout: float
    tool_timeout: float
    poll_interval: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ZentraConfig":
        env = os.environ if env is None else env
        mcp_url = (env.get("ZENTRA_MCP_URL") or "").strip()
        key = (env.get("ZENTRA_MCP_KEY") or "").strip()
        if not mcp_url or not key:
            raise ZentraConfigError("ZENTRA_MCP_URL 與 ZENTRA_MCP_KEY 都要設定")
        if not key.startswith("zmcp_live_"):
            raise ZentraConfigError("ZENTRA_MCP_KEY 應該是 zmcp_live_ 開頭的 MCP key")
        for name, url in (("ZENTRA_MCP_URL", mcp_url), ("ZENTRA_API_URL", env.get("ZENTRA_API_URL") or "")):
            if url:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ZentraConfigError(f"{name} 必須是 http(s)://host[:port]/… 的網址")
        api_url = (env.get("ZENTRA_API_URL") or "").strip()
        if not api_url:
            # 預設：MCP 網址去掉結尾的 /mcp 就是 REST 的根
            api_url = mcp_url.rstrip("/")
            if api_url.endswith("/mcp"):
                api_url = api_url[: -len("/mcp")]

        def number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(env.get(name, default))
            except (TypeError, ValueError):
                raise ZentraConfigError(f"{name} 必須是數字") from None
            return min(max(value, low), high)

        return cls(
            mcp_url=mcp_url, api_url=api_url.rstrip("/"), key=key,
            request_timeout=number("ZENTRA_REQUEST_TIMEOUT_SECONDS", 30, 5, 120),
            tool_timeout=number("INSIGHT_TOOL_TIMEOUT_SECONDS", 1200, 60, 3600),
            poll_interval=number("INSIGHT_POLL_SECONDS", 3, 1, 30),
        )


def configured(env: Mapping[str, str] | None = None) -> bool:
    try:
        ZentraConfig.from_env(env)
    except ZentraConfigError:
        return False
    return True


def _parse_rpc(response: httpx.Response) -> dict[str, Any]:
    """MCP Streamable HTTP 可能回 JSON，也可能回 SSE（data: 行）。"""
    if response.status_code == 401:
        raise ZentraConfigError("Zentra 拒絕這把 MCP key（401）：key 錯了或已撤銷")
    if response.status_code >= 400:
        raise ZentraError(f"Zentra /mcp 回 HTTP {response.status_code}")
    text = response.text.strip()
    if not text.startswith("{"):
        data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not data_lines:
            raise ZentraError("Zentra /mcp 的回應看不懂（既不是 JSON 也不是 SSE）")
        text = data_lines[-1]
    payload = json.loads(text)
    if "error" in payload:
        raise ZentraError(f"Zentra /mcp 錯誤：{payload['error']}")
    return payload.get("result") or {}


def check_contract(tools: list[dict[str, Any]]) -> list[str]:
    """比對 tools/list 與 TOOLS。回傳問題清單（空 = 沒問題）。"""
    by_name = {tool.get("name"): tool for tool in tools}
    problems = []
    for local, spec in TOOLS.items():
        tool = by_name.get(spec["mcp"])
        if tool is None:
            problems.append(f"缺少 tool {spec['mcp']}（MCP key 沒勾選、或 Agent 沒有已發布版本）")
            continue
        schema = tool.get("inputSchema") or {}
        props = set((schema.get("properties") or {}).keys())
        required = set(schema.get("required") or [])
        if props != spec["properties"]:
            problems.append(f"{spec['mcp']} 的輸入欄位是 {sorted(props)}，預期 {sorted(spec['properties'])}")
        if required != spec["required"]:
            problems.append(f"{spec['mcp']} 的必填欄位是 {sorted(required)}，預期 {sorted(spec['required'])}")
        if schema.get("additionalProperties") is not False:
            problems.append(f"{spec['mcp']} 沒有 additionalProperties: false")
        out_tool = (((tool.get("outputSchema") or {}).get("properties") or {}).get("tool") or {}).get("const")
        if out_tool != local:
            problems.append(f"{spec['mcp']} 的輸出 tool 是 {out_tool!r}，預期 {local!r}")
    return problems


class ZentraClient:
    """一個 job 用一個（`async with`），結束就關連線。"""

    def __init__(self, config: ZentraConfig | None = None) -> None:
        self.config = config or ZentraConfig.from_env()
        self._http: httpx.AsyncClient | None = None
        self._rpc_id = 0

    async def __aenter__(self) -> "ZentraClient":
        self._http = httpx.AsyncClient(timeout=self.config.request_timeout, trust_env=False)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._http is not None, "請用 async with ZentraClient()"
        self._rpc_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            body["params"] = params
        try:
            response = await self._http.post(self.config.mcp_url, json=body, headers={
                "Authorization": f"Bearer {self.config.key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
        except httpx.HTTPError as exc:
            raise ZentraError(f"連不到 Zentra /mcp：{type(exc).__name__}") from exc
        return _parse_rpc(response)

    async def verify(self) -> None:
        """job 開始前呼叫：tools/list 與 TOOLS 對不上就拋 ZentraContractError。"""
        result = await self._rpc("tools/list")
        problems = check_contract(result.get("tools") or [])
        if problems:
            raise ZentraContractError("Zentra 的 tool 與預期不符：" + "；".join(problems))

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """呼叫一個 tool，等到結束。回傳 (run_id, output)；失敗拋 ZentraError。"""
        spec = TOOLS[tool]                       # KeyError = 程式寫錯，不是使用者的問題
        extra = set(arguments) - spec["properties"]
        missing = spec["required"] - set(arguments)
        if extra or missing:
            raise ZentraError(f"{tool} 參數不對：多了 {sorted(extra)}，缺了 {sorted(missing)}")

        result = await self._rpc("tools/call", {"name": spec["mcp"], "arguments": {**arguments, "_async": True}})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            raise ZentraError(f"{tool} 呼叫失敗：{text[:500]}")
        ack = result.get("structuredContent") or {}
        if not ack.get("run_id"):
            try:
                ack = json.loads((result.get("content") or [{}])[0].get("text") or "{}")
            except (ValueError, AttributeError, IndexError):
                ack = {}
        run_id = ack.get("run_id")
        if not run_id:
            raise ZentraError(f"{tool} 沒有回 run_id")

        run = await self._wait(run_id)
        if run.get("status") != "SUCCEEDED":
            error = run.get("error") or {}
            raise ZentraError(
                f"{tool} {run.get('status')}：{error.get('code', '')} {error.get('message', '')}".strip()[:800],
                run_id=run_id)
        output = run.get("output")
        if not isinstance(output, dict) or output.get("tool") != tool:
            raise ZentraError(f"{tool} 的輸出不是 {tool} 的格式（tool={output.get('tool') if isinstance(output, dict) else None!r}）",
                              run_id=run_id)
        if tool in SECURITY_TOOLS:
            security = output.get("security") or {}
            want = (arguments["market"], str(arguments["symbol"]).upper())
            got = (security.get("market"), str(security.get("symbol", "")).upper())
            if got != want:
                raise ZentraError(f"{tool} 回傳的是 {got}，不是我們要的 {want}", run_id=run_id)
        return run_id, output

    async def _wait(self, run_id: str) -> dict[str, Any]:
        assert self._http is not None
        deadline = time.monotonic() + self.config.tool_timeout
        interval = self.config.poll_interval
        failures = 0
        while True:
            try:
                response = await self._http.get(f"{self.config.api_url}/v1/runs/{run_id}")
                if response.status_code == 200:
                    run = response.json()
                    failures = 0
                    if run.get("status") in _TERMINAL:
                        return run
                else:
                    failures += 1
            except httpx.HTTPError:
                failures += 1
            if failures >= 10:
                raise ZentraError("連續 10 次查不到 Run 狀態", run_id=run_id)
            if time.monotonic() > deadline:
                raise ZentraError(f"等了 {int(self.config.tool_timeout)} 秒還沒結束", run_id=run_id)
            await asyncio.sleep(interval)
            interval = min(interval * 1.3, 15)
