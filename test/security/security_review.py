"""Opt-in security review. Uses fake credentials/DBs; never connects to production.

Run: PYTHONPATH=. venv/bin/python test/security/security_review.py --output /tmp/review.json
Findings are observations, not assertions that known vulnerabilities are fixed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Set before imports: load_dotenv must not load real service credentials or targets.
os.environ.update({
    "SECRET_KEY": "isolated-security-review-key-not-used-in-production",
    "OPENAI_API_KEY": "audit-placeholder-no-paid-calls",
    "MONGO_URI": "mongodb://127.0.0.1:27017",
    "MONGODB_URL": "mongodb://127.0.0.1:27017",
    "FEED_MONGO_URI": "mongodb://127.0.0.1:27017",
    "DATABASE_URL": "postgresql://audit:audit@127.0.0.1/audit_only",
    "DATABASE_SSL": "require", "MCP_SERVERS_JSON": "", "ZENTRA_MCP_URL": "",
    "KINETIC_UPSTREAM": "http://audit-upstream.invalid",
    "FRONTEND_URL": "https://insight-chat.xyz",
    "CORS_ALLOWED_ORIGINS": "https://insight-chat.xyz",
    "COOKIE_SECURE": "true", "DESKTOP_AUTH_ENABLED": "true",
})

import asyncpg
import httpx
from fastapi import HTTPException, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

from app.backend.app import app
from app.backend.api import auth, chat_share, explore, notes, portfolio, project
from app.backend.database.postgresql import get_db, _parse_dsn
from app.backend.deep_research.session import SessionStore
from app.backend.deep_research.skills import ReportDoc, DeckDoc
from app.backend.deep_research.templates import render_report, render_deck, resolve_theme
from app.backend.deep_research.templates.common import markdown_to_html
from app.backend.module import desktop_auth, jwt
from app.backend.module.upload_limit import UploadSizeLimitMiddleware

UID, OTHER, OBJECT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
checks = []


def record(name, expected, observed, *, severity="info", evidence=None):
    checks.append({"id": name, "status": "pass" if expected == observed else "fail",
                   "severity": severity if expected != observed else "info",
                   "expected": expected, "observed": observed, "evidence": evidence})


def status_of(fn):
    try:
        fn()
        return 200
    except HTTPException as exc:
        return exc.status_code


class FakeDB:
    def __init__(self, disabled=False):
        self.disabled = disabled
        self.calls = []
        self.rt_exists = True

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "DELETE FROM refresh_tokens" in sql:
            if not self.rt_exists:
                return None
            self.rt_exists = False
            return {"user_id": UID}
        if "FROM refresh_tokens r" in sql:
            return {"id": UID, "status": "disabled" if self.disabled else "active"} if self.rt_exists else None
        if "FROM users" in sql:
            return {"id": UID, "email": "audit@example.invalid", "username": "audit",
                    "status": "disabled" if self.disabled else "active", "tier_id": None,
                    "last_login_at": None, "created_at": None, "updated_at": None}
        return None

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "DELETE 0" if sql.strip().startswith("DELETE") else "INSERT 0 1"

    @asynccontextmanager
    async def transaction(self):
        yield


def jwt_checks():
    rt = jwt.create_refresh_token({"sub": str(UID), "email": "audit@example.invalid"})
    at = jwt.create_access_token({"sub": str(UID)})
    record("AUTH-01-refresh-token-rejected-as-access", 401,
           status_of(lambda: jwt._decode_user_id(rt)), severity="high",
           evidence="module/jwt.py:_decode_user_id does not check token type or refresh-token store")
    record("AUTH-02-valid-access-accepted", 200, status_of(lambda: jwt._decode_user_id(at)))
    invalid = {
        "expired": jwt.create_access_token({"sub": str(UID)}, timedelta(seconds=-5)),
        "wrong-signature": jose_jwt.encode({"sub": str(UID)}, "another-audit-key", algorithm="HS256"),
        "garbage": "bad.token", "missing-sub": jwt.create_access_token({}),
        "invalid-uuid": jwt.create_access_token({"sub": "not-uuid"}),
        "unsigned": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJub25lIn0.",
    }
    for label, token in invalid.items():
        record("AUTH-invalid-" + label, 401, status_of(lambda: jwt._decode_user_id(token)))
    no_exp = jose_jwt.encode({"sub": str(UID)}, jwt.SECRET_KEY, algorithm=jwt.ALGORITHM)
    record("AUTH-03-missing-exp-rejected", 401, status_of(lambda: jwt._decode_user_id(no_exp)),
           severity="low", evidence="Only server-signed fixtures; no signing-key compromise demonstrated")
    return at, rt


async def async_checks(at, rt):
    disabled = FakeDB(disabled=True)
    try:
        await jwt.get_current_user(at, disabled)
        status = 200
    except HTTPException as exc:
        status = exc.status_code
    record("AUTH-disabled-full-dependency", 403, status)
    try:
        await jwt.get_current_user_id(at, disabled)
        status = 200
    except HTTPException as exc:
        status = exc.status_code
    record("AUTH-04-disabled-light-dependency", 403, status, severity="high")
    response = Response()
    try:
        await auth.refresh_access_token(response, rt, disabled)
        status = 200
    except HTTPException as exc:
        status = exc.status_code
    record("AUTH-05-disabled-refresh-rejected", 403, status, severity="high")
    active = FakeDB()
    result = await auth.refresh_access_token(response, rt, active)
    record("AUTH-active-refresh-accepted", True, bool(result.get("access_token")))
    record("AUTH-refresh-httponly-cookie", True, "HttpOnly" in response.headers.get("set-cookie", ""))
    record("AUTH-refresh-secure-cookie", True, "Secure" in response.headers.get("set-cookie", ""))
    record("AUTH-refresh-samesite-cookie", True, "SameSite=lax" in response.headers.get("set-cookie", ""))
    record("AUTH-06-refresh-no-store", "no-store", response.headers.get("cache-control"), severity="low")
    try:
        await auth.refresh_access_token(Response(), rt, active)
        replay = 200
    except HTTPException as exc:
        replay = exc.status_code
    record("AUTH-refresh-replay-rejected", 401, replay)
    revoked = FakeDB()
    revoked.rt_exists = False
    try:
        await explore._require_session(rt, revoked)
        status = 200
    except HTTPException as exc:
        status = exc.status_code
    record("AUTH-07-revoked-explore-session-rejected", 401, status, severity="high")
    methods = []
    class Upstream:
        async def request(self, method, *args, **kwargs):
            methods.append(method)
            return httpx.Response(200, json={"audit": True})
    with patch.object(explore, "_get_client", return_value=Upstream()):
        from starlette.requests import Request
        for method in ("GET", "POST", "DELETE"):
            request = Request({"type": "http", "method": method, "path": "/explore/audit",
                               "query_string": b"", "headers": []},
                              receive=lambda: None)
            async def empty_body(): return b""
            request.body = empty_body
            try:
                result = await explore.explore_proxy("audit", request, rt, revoked)
                status = result.status_code
            except HTTPException as exc:
                status = exc.status_code
            record("AUTH-explore-revoked-" + method, 401, status, severity="high")
        record("AUTH-explore-revoked-never-forwarded", [], methods)
    record("PROXY-relative-path-preserves-upstream", "kinetic.invalid",
           httpx.AsyncClient(base_url="http://kinetic.invalid:8000").build_request("GET", "//evil.invalid/").url.host)

    db = FakeDB()
    owners = [
        ("share", lambda: chat_share._assert_owner(db, OBJECT, UID)),
        ("notes", lambda: notes._assert_page_owned(db, OBJECT, UID)),
        ("project-delete", lambda: project.delete_project(OBJECT, db, {"id": UID})),
        ("portfolio-delete", lambda: portfolio.delete_item(OBJECT, UID, db)),
    ]
    for name, call in owners:
        try: await call(); status = 200
        except HTTPException as exc: status = exc.status_code
        record("IDOR-" + name + "-foreign-object", 404, status)
    store = SessionStore()
    session = await store.create(user_id=str(UID), model="audit-model", query="audit")
    record("IDOR-research-session-owner", None, await store.get(session.id, str(OTHER)))
    record("IDOR-research-session-self", True, await store.get(session.id, str(UID)) is session)

    class BrokenDB(FakeDB):
        async def execute(self, *args):
            raise asyncpg.PostgresError("AUDIT_INTERNAL_DATABASE_DETAIL")
    try: await project.delete_project(OBJECT, BrokenDB(), {"id": UID}); detail = ""
    except HTTPException as exc: detail = exc.detail
    record("INFO-01-database-error-redacted", False,
           "AUDIT_INTERNAL_DATABASE_DETAIL" in detail, severity="medium")

    # Google userinfo with email_verified=False must not link an existing email account.
    class GoogleClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={"access_token": "fake-google-token"}, request=httpx.Request("POST", "https://audit.invalid"))
        async def get(self, *args, **kwargs):
            return httpx.Response(200, json={"sub": "audit-google-sub", "email": "audit@example.invalid", "email_verified": False}, request=httpx.Request("GET", "https://audit.invalid"))
    class LinkDB(FakeDB):
        async def fetchrow(self, sql, *args):
            if "google_sub =" in sql: return None
            return await super().fetchrow(sql, *args)
    linking = LinkDB()
    with patch.object(auth.httpx, "AsyncClient", GoogleClient):
        await auth.google_callback(db=linking, code="fake-code", state="audit-state", error=None,
                                   oauth_state="audit-state", oauth_return_url=None, oauth_desktop=None)
    record("AUTH-08-unverified-email-not-linked", False,
           any("SET google_sub" in sql for sql, _ in linking.calls), severity="high",
           evidence="Userinfo stub only; real Google account takeover not performed")

    # Middleware receives a bounded synthetic body; no large allocation/network traffic.
    async def downstream(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    middleware = UploadSizeLimitMiddleware(downstream, paths=["/api/deep-research/runs", "/api/chat/deep-research", "/api/files/upload"], max_bytes=32)
    for route in ("/api/deep-research/runs", "/api/chat/deep-research", "/api/files/upload"):
        for declared in (False, True):
            messages = []
            async def receive(): return {"type": "http.request", "body": b"x" * 64, "more_body": False}
            async def send(message): messages.append(message)
            scope = {"type": "http", "path": route, "headers": [(b"content-length", b"64")] if declared else []}
            await middleware(scope, receive, send)
            observed = next(m["status"] for m in messages if m["type"]=="http.response.start")
            record("LIMIT-body-" + route + ("-declared" if declared else "-chunked"), 413, observed,
                   severity="medium" if route=="/api/files/upload" else "info")


def dependency_names(dep):
    result = {getattr(dep.call, "__name__", "")}
    for sub in dep.dependencies: result.update(dependency_names(sub))
    return result


def http_checks(at, rt):
    db = FakeDB()
    async def fake_db(): yield db
    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app)  # no context manager: production lifespan is never run
    protected = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or "GET" not in route.methods: continue
        if not {"get_current_user", "get_current_user_id"}.intersection(dependency_names(route.dependant)): continue
        path = re.sub(r"\{[^}]+\}", str(OBJECT), route.path)
        response = client.get(path)
        record("HTTP-anonymous-" + route.path, 401, response.status_code)
        protected.append(route.path)
    for token_name, token in [("refresh", rt), ("revoked-refresh", rt)]:
        db.rt_exists = token_name != "revoked-refresh"
        response = client.get("/api/portfolio/items", headers={"Authorization": "Bearer " + token})
        record("HTTP-" + token_name + "-as-bearer", 401, response.status_code, severity="high")
    db.disabled = True
    response = client.get("/api/portfolio/items", headers={"Authorization": "Bearer " + at})
    record("HTTP-disabled-portfolio", 403, response.status_code, severity="high")
    response = client.get("/api/project/all", headers={"Authorization": "Bearer " + at})
    record("HTTP-disabled-project", 403, response.status_code)
    db.disabled = False
    # Invalid PDF header and unrelated IDs: endpoint currently returns a placeholder success.
    response = client.post("/api/files/upload", headers={"Authorization": "Bearer " + at},
                           data={"project_id": str(OTHER)}, files={"file": ("fake.pdf", b"not-a-pdf", "application/pdf")})
    record("UPLOAD-legacy-unimplemented-fails-closed", 501, response.status_code, severity="medium",
           evidence="Legacy endpoint is a non-persistent TODO stub; no real stored-file exploit claimed")
    for origin, expected in [("https://insight-chat.xyz", 200), ("https://evil.invalid", 400),
                             ("https://evil.insight-chat.xyz", 400), ("http://192.168.0.42:9000", 400)]:
        response = client.options("/api/portfolio/items", headers={"Origin": origin,
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
        record("CORS-" + origin, expected, response.status_code, severity="medium")
    app.dependency_overrides.clear()
    client.close()
    return protected


def static_and_payload_checks(output):
    _, ssl_context = _parse_dsn()
    record("TLS-01-db-certificate-verified", True,
           bool(ssl_context and ssl_context.check_hostname and ssl_context.verify_mode != 0), severity="medium")
    no_secret = subprocess.run([sys.executable, "-c", "import app.backend.module.jwt"],
                               cwd=ROOT, env={**os.environ, "SECRET_KEY": ""}, capture_output=True)
    record("CONFIG-01-jwt-fails-closed-without-secret", True,
           no_secret.returncode != 0 and b"SECRET_KEY must be explicitly set" in no_secret.stderr,
           severity="medium")
    index = (ROOT / "app/frontend/index.html").read_text()
    record("XSS-01-artifact-frame-sandboxed", True,
           bool(re.search(r'<iframe(?=[^>]*id="dr-preview-frame")(?=[^>]*\bsandbox\b)', index)), severity="high")
    cases = {
        "plain-event": '<svg onload="parent.__auditMarker=1"></svg>',
        "slash-event": '<svg/onload=parent.__auditMarker=2></svg>',
        "entity-url": '<a id="audit-link" href="java&#x73;cript:parent.__auditMarker=3">audit</a>',
        "newline-url": '<a id="audit-link" href="java\nscript:parent.__auditMarker=4">audit</a>',
        "plain-script": '<script>parent.__auditMarker=5</script>',
    }
    artifacts = []
    for name, payload in cases.items():
        doc = ReportDoc(title="Security review fixture", subtitle="No network, credentials or user data",
                        executive_summary=[], key_figures=[], sections=[{"heading": "Fixture", "body_markdown": payload, "callout": ""}],
                        open_questions=[], references=[])
        html = render_report(doc, model="audit", query="audit", theme=resolve_theme(None))
        artifacts.append({"name": name, "html": html, "payload": payload})
    (output.parent / "xss-cases.json").write_text(json.dumps(artifacts))
    deck = DeckDoc(title="Local deck fixture", subtitle="", slides=[{
        "layout": "bullets", "kicker": "Audit", "title": str(i), "lead": "",
        "bullets": ["Fixture"], "stats": [], "columns": [], "note": "Local notes",
    } for i in (1, 2)])
    (output.parent / "deck.html").write_text(render_deck(deck, model="audit", query="audit", theme=resolve_theme(None)))
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append({"path": route.path, "methods": sorted(route.methods),
                           "dependencies": sorted(dependency_names(route.dependant))})
    (output.parent / "route-inventory.json").write_text(json.dumps(routes, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    at, rt = jwt_checks()
    asyncio.run(async_checks(at, rt))
    protected = http_checks(at, rt)
    static_and_payload_checks(args.output)
    result = {"date": "2026-11-22", "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "scope": "Isolated local ASGI/FakeDB; no production database or paid AI calls",
              "protected_get_routes": len(protected), "checks": checks}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({"checks": len(checks), "pass": sum(c["status"]=="pass" for c in checks),
                      "fail": sum(c["status"]=="fail" for c in checks), "report": str(args.output)}, ensure_ascii=False))
    for c in checks:
        if c["status"]=="fail": print(c["id"], c["severity"], "observed=", c["observed"])
    if any(c["status"] == "fail" for c in checks):
        raise SystemExit(1)


if __name__ == "__main__": main()
