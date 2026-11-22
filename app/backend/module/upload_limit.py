"""
上傳端點的請求大小限制（ASGI middleware）。

為什麼不能只靠 handler 裡的檢查：FastAPI 在進 handler 之前就會把整個 multipart
body 解析完（大檔案寫進暫存檔），handler 才看得到檔案大小。沒有這一層的話，
送一個 5GB 的請求會先把磁碟與頻寬耗完，最後才回 400。

這裡在讀 body 的當下就計數：
- 有 Content-Length 且超過上限 → 一個位元組都不讀，直接 413
- 沒有 Content-Length（chunked）→ 邊收邊算，超過就中止並回 413
"""

from __future__ import annotations

import json
from typing import Iterable


class _BodyTooLarge(Exception):
    pass


class UploadSizeLimitMiddleware:
    def __init__(self, app, *, paths: Iterable[str], max_bytes: int):
        self.app = app
        self.paths = frozenset(paths)
        self.max_bytes = max_bytes

    async def _reject(self, send) -> None:
        body = json.dumps(
            {"detail": f"上傳內容過大，單次請求上限約 {self.max_bytes // (1024 * 1024)} MB。"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").rstrip("/") not in self.paths:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.max_bytes:
                    await self._reject(send)
                    return
                break

        received = 0
        too_large = False
        response_started = False

        async def limited_receive():
            nonlocal received, too_large
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal response_started
            if too_large:
                # FastAPI 解析 multipart 時會把 receive 拋出的例外包成「400 解析失敗」；
                # 那不是使用者該看到的原因，一律改回 413 並丟掉原本的回應內容
                if message.get("type") == "http.response.start" and not response_started:
                    response_started = True
                    await self._reject(send)
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(send)
