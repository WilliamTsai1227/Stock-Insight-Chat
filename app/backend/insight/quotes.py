"""即時報價（庫存總覽 / 圓餅圖用）。

來源：Yahoo Finance chart API（不需 key；台股 .TW/.TWO、美股、USD/TWD 匯率同一個端點）。
這跟排程分析無關 —— 排程分析的報價由 Zentra 那邊抓；這裡只是讓「庫存」頁打開時
能算出現值與今日漲跌，不必等下一次排程。

- 取 5 天日線：最後一根是最新價，前一根是昨收，算今日漲跌。
- 每個代碼快取 QUOTE_CACHE_SECONDS（預設 60 秒），同一使用者連續重整不會一直打 Yahoo。
- 台股先試 `.TW`（上市）再試 `.TWO`（上櫃），試到的那個會記住。
- 抓不到的回 None，由呼叫端標示「無報價」，不讓整個總覽失敗。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote as urlquote

import httpx

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"


@dataclass(frozen=True)
class Quote:
    yahoo_symbol: str
    price: float
    previous_close: Optional[float]
    currency: Optional[str]
    as_of: Optional[int]          # epoch 秒（最後一根 K 棒）


_cache: dict[str, tuple[float, Optional[Quote]]] = {}
_tw_suffix: dict[str, str] = {}   # "2330" → ".TW" / "6488" → ".TWO"


def _ttl() -> float:
    try:
        return max(5.0, float(os.getenv("QUOTE_CACHE_SECONDS", "60")))
    except ValueError:
        return 60.0


def _parse(payload: dict, yahoo_symbol: str) -> Optional[Quote]:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    closes = [c for c in (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
              if c is not None]
    stamps = result.get("timestamp") or []
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if price is None:
        return None
    previous = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose")
    return Quote(yahoo_symbol=yahoo_symbol, price=float(price),
                 previous_close=float(previous) if previous else None,
                 currency=meta.get("currency"), as_of=stamps[-1] if stamps else None)


async def _fetch(client: httpx.AsyncClient, yahoo_symbol: str) -> Optional[Quote]:
    try:
        response = await client.get(CHART_URL.format(symbol=urlquote(yahoo_symbol, safe="")))
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return _parse(response.json(), yahoo_symbol)
    except ValueError:
        return None


async def _quote(client: httpx.AsyncClient, market: str, symbol: str) -> Optional[Quote]:
    key = f"{market}:{symbol}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _ttl():
        return hit[1]
    if market == "TW":
        suffixes = [_tw_suffix[symbol]] if symbol in _tw_suffix else [".TW", ".TWO"]
        quote = None
        for suffix in suffixes:
            quote = await _fetch(client, symbol + suffix)
            if quote:
                _tw_suffix[symbol] = suffix
                break
    elif market == "FX":
        quote = await _fetch(client, symbol)
    else:
        quote = await _fetch(client, symbol.replace(".", "-"))    # BRK.B → BRK-B
    _cache[key] = (time.monotonic(), quote)
    return quote


async def get_quotes(items: list[tuple[str, str]], *, with_fx: bool) -> tuple[dict[tuple[str, str], Optional[Quote]], Optional[Quote]]:
    """回傳 ({(market, symbol): Quote|None}, USD/TWD Quote|None)。"""
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=8.0, headers={"Accept": "application/json",
                                                        "User-Agent": "StockInsightChat/1"}) as client:
        async def one(market: str, symbol: str) -> Optional[Quote]:
            async with semaphore:
                return await _quote(client, market, symbol)

        tasks = [one(m, s) for m, s in items]
        if with_fx:
            tasks.append(one("FX", "TWD=X"))
        results = await asyncio.gather(*tasks)
    fx = results.pop() if with_fx else None
    return dict(zip(items, results)), fx
