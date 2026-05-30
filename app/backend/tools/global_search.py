"""
global_search.py — 網路即時搜尋工具
─────────────────────────────────────────────
目前整合 Tavily Search API，透過已有的 httpx（非同步）打 REST 端點，
不需要額外 SDK 套件。

環境變數：
  TAVILY_API_KEY          必填；至 https://tavily.com 取得
  TAVILY_MAX_RESULTS      每次搜尋回傳最多幾筆（預設 5）
  TAVILY_SEARCH_DEPTH     "basic"（快，預設）或 "advanced"（更完整但稍慢）
  TAVILY_INCLUDE_ANSWER   是否附帶 Tavily 自動摘要（"1" 開，預設）
  TAVILY_TIMEOUT_SEC      HTTP 逾時秒數（預設 15）

對外函式：
  tavily_search(query, max_results=None) → dict
    回傳 { query, answer, results: [{title, url, content, score, published_date}], num_results }
    失敗時 raise TavilySearchError
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
TAVILY_MAX_RESULTS: int = int(os.getenv("TAVILY_MAX_RESULTS", "5"))
TAVILY_SEARCH_DEPTH: str = os.getenv("TAVILY_SEARCH_DEPTH", "basic")
TAVILY_INCLUDE_ANSWER: bool = os.getenv("TAVILY_INCLUDE_ANSWER", "1").strip() in ("1", "true", "yes")
TAVILY_TIMEOUT_SEC: float = float(os.getenv("TAVILY_TIMEOUT_SEC", "15"))

_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class TavilySearchError(RuntimeError):
    """Tavily API 呼叫失敗時拋出。"""


async def tavily_search(
    query: str,
    max_results: Optional[int] = None,
) -> Dict[str, Any]:
    """
    呼叫 Tavily Search API，回傳結構化搜尋結果。

    Parameters
    ----------
    query:
        搜尋關鍵字（中英文皆可）。
    max_results:
        回傳筆數，None 時使用 TAVILY_MAX_RESULTS 環境變數。

    Returns
    -------
    dict
        {
          "query": str,
          "answer": str | None,           # Tavily 自動摘要（TAVILY_INCLUDE_ANSWER=1 時有值）
          "results": [                     # 搜尋結果列表
            {
              "title": str,
              "url": str,
              "content": str,             # 頁面正文（約數百字）
              "score": float,
              "published_date": str | None,
            }
          ],
          "num_results": int,
          "execution_time": float,
        }
    """
    if not TAVILY_API_KEY:
        raise TavilySearchError(
            "TAVILY_API_KEY 未設定，請在 .env 加入 TAVILY_API_KEY=tvly-xxxx"
        )

    k = max_results if max_results is not None else TAVILY_MAX_RESULTS
    payload: Dict[str, Any] = {
        "api_key": TAVILY_API_KEY,
        "query": (query or "").strip(),
        "search_depth": TAVILY_SEARCH_DEPTH,
        "max_results": k,
        "include_answer": TAVILY_INCLUDE_ANSWER,
        "include_raw_content": False,
    }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT_SEC) as client:
            resp = await client.post(_TAVILY_ENDPOINT, json=payload)
            resp.raise_for_status()
            data: Dict[str, Any] = resp.json()
    except httpx.HTTPStatusError as e:
        raise TavilySearchError(
            f"Tavily API 回傳 HTTP {e.response.status_code}：{e.response.text[:300]}"
        ) from e
    except httpx.TimeoutException as e:
        raise TavilySearchError(f"Tavily API 逾時（{TAVILY_TIMEOUT_SEC}s）") from e
    except Exception as e:
        raise TavilySearchError(f"Tavily 呼叫失敗：{e}") from e

    elapsed = time.time() - t0

    results: List[Dict[str, Any]] = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "score": float(r.get("score", 0)),
            "published_date": r.get("published_date"),
        }
        for r in data.get("results", [])
    ]

    return {
        "query": query,
        "answer": data.get("answer"),
        "results": results,
        "num_results": len(results),
        "execution_time": round(elapsed, 3),
    }
