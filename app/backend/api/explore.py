"""
探索（Kinetic Charts）反向代理
================================
正式環境 backend 容器直接對外開 port、沒有 nginx/ALB 做 path 分流，
因此由 backend 自己把 /explore/* 轉發給 kinetic 容器（Stock-Analysis 專案）：

  瀏覽器 iframe ── /explore/... ──► backend（本模組）──► http://kinetic:8000/...

設計要點：
- 前綴剝除在這裡完成（/explore/api/quote → 上游 /api/quote），
  kinetic 後端維持原路由、零改動。
- 登入閘門：kinetic 本身無認證，這裡在轉發前先驗 refresh_token cookie
  （stateless：簽章 + exp + type，不查 DB —— 每個靜態資源請求都會經過，必須夠快）。
  已撤銷但未過期的 RT 仍會通過，對一個以讀為主的嵌入頁可接受。
- 同源：iframe 與 chat API 同主機，samesite=lax 的 cookie 會自動帶上。
- 未設定 KINETIC_UPSTREAM 時整組路由回 404（功能關閉，本地 dev 不受影響）。

所需環境變數：
  KINETIC_UPSTREAM   kinetic 上游位址，例如 http://kinetic:8000（未設定 = 功能關閉）
  FRONTEND_URL       前端根網址（CSP frame-ancestors 白名單，沿用既有變數）
"""

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from app.backend.module.jwt import decode_token

KINETIC_UPSTREAM = os.getenv("KINETIC_UPSTREAM", "").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost").rstrip("/")

router = APIRouter(tags=["Explore (Kinetic Charts)"])

# 連線池共用（lazy 建立：模組載入時 event loop 尚未啟動）
_client: Optional[httpx.AsyncClient] = None

# 上游回應中不該原樣轉發的 headers（hop-by-hop / 由本服務重算或覆寫）
_EXCLUDED_RESPONSE_HEADERS = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "keep-alive", "server", "date", "cache-control",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=KINETIC_UPSTREAM,
            timeout=httpx.Timeout(10.0, read=60.0),  # quant-scan 即時回測可達數十秒
        )
    return _client


def _require_session(refresh_token: Optional[str]) -> None:
    """驗證 refresh_token cookie（stateless），失敗一律 401。"""
    payload = decode_token(refresh_token) if refresh_token else None
    if not payload or payload.get("type") != "refresh" or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


@router.get("/explore", include_in_schema=False)
async def explore_redirect():
    # 無結尾斜線必須 301，否則 kinetic 頁面內的相對資源路徑會解析到錯誤層級
    return RedirectResponse(url="/explore/", status_code=301)


@router.api_route(
    "/explore/{path:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def explore_proxy(
    path: str,
    request: Request,
    refresh_token: Optional[str] = Cookie(None),
):
    if not KINETIC_UPSTREAM:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    _require_session(refresh_token)

    # 只轉發必要的 request headers；cookie 不外洩給上游
    upstream_headers = {}
    if request.headers.get("content-type"):
        upstream_headers["Content-Type"] = request.headers["content-type"]

    try:
        upstream = await _get_client().request(
            request.method,
            f"/{path}",
            params=str(request.url.query) or None,
            content=await request.body() or None,
            headers=upstream_headers,
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Explore service unavailable",
        )

    headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _EXCLUDED_RESPONSE_HEADERS
    }
    # cookie 保護的內容：只允許 chat 前端以 iframe 嵌入，且不進共用快取
    headers["Cache-Control"] = "private, no-store"
    if upstream.headers.get("content-type", "").startswith("text/html"):
        headers["Content-Security-Policy"] = f"frame-ancestors {FRONTEND_URL}"

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
    )


@router.on_event("shutdown")
async def _close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
