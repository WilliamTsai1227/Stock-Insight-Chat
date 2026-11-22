"""Desktop authorization redirect boundaries, PKCE and one-use handoff behavior."""
import asyncio
import base64
import hashlib
import uuid
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException, Response

from app.backend.module import desktop_auth
from app.backend.api import auth

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
STATE = "s" * 43
CALLBACK = "http://127.0.0.1:43210/oauth/callback"


@pytest.mark.parametrize("redirect", [
    "https://evil.example/oauth/callback", "http://localhost:43210/oauth/callback",
    "http://127.0.0.1.evil.example:43210/oauth/callback", "http://127.0.0.1:80/oauth/callback",
    "http://user@127.0.0.1:43210/oauth/callback", "http://127.0.0.1:43210/other",
    "http://127.0.0.1:43210/oauth/callback?url=evil", "http://127.0.0.1:43210/oauth/callback#fragment",
    "http://127.0.0.1:99999/oauth/callback", "http://[::1]:43210/oauth/callback",
])
def test_only_explicit_ipv4_loopback_callback_allowed(redirect):
    with pytest.raises(HTTPException):
        desktop_auth.validate_desktop_request(redirect, CHALLENGE, STATE)


def test_signed_context_bound_to_oauth_state_and_tamper_resistant():
    desktop_auth.validate_desktop_request(CALLBACK, CHALLENGE, STATE)
    token = desktop_auth.seal_context(CALLBACK, CHALLENGE, STATE, "oauth-state")
    context = desktop_auth.read_context(token, "oauth-state")
    assert context["challenge"] == CHALLENGE
    assert desktop_auth.read_context(token, "another-state") is None
    assert desktop_auth.read_context(token + "x", "oauth-state") is None
    assert parse_qs(urlparse(desktop_auth.callback_url(context, code="c")).query) == {
        "state": [STATE], "code": ["c"],
    }


class FakeDB:
    def __init__(self):
        self.codes = {}
        self.user_id = uuid.uuid4()
        self.status = "active"

    async def execute(self, sql, *args):
        if "INSERT INTO desktop_login_codes" in sql:
            self.codes[args[0]] = {"user_id": args[1], "challenge": args[2], "expires_at": args[3]}

    async def fetchrow(self, sql, *args):
        if "DELETE FROM desktop_login_codes" in sql:
            row = self.codes.get(args[0])
            if row and row["challenge"] == args[1] and row["expires_at"] > desktop_auth.datetime.now(desktop_auth.timezone.utc):
                return self.codes.pop(args[0])
            return None
        return {"id": self.user_id, "email": "test@example.com", "status": self.status}

    @asynccontextmanager
    async def transaction(self):
        yield


def test_pkce_rejects_wrong_verifier_without_consuming_and_rejects_replay():
    async def run():
        db = FakeDB()
        code = await desktop_auth.issue_code(db, db.user_id, {"challenge": CHALLENGE})
        assert code not in db.codes  # no plaintext authorization codes stored
        with pytest.raises(HTTPException) as exc:
            await desktop_auth.consume_code(db, code, "w" * 64)
        assert exc.value.status_code == 401
        assert await desktop_auth.consume_code(db, code, VERIFIER) == db.user_id
        with pytest.raises(HTTPException):
            await desktop_auth.consume_code(db, code, VERIFIER)
    asyncio.run(run())


def test_expired_code_rejected():
    async def run():
        from datetime import timedelta
        db = FakeDB()
        code = await desktop_auth.issue_code(db, db.user_id, {"challenge": CHALLENGE})
        next(iter(db.codes.values()))["expires_at"] -= timedelta(minutes=2)
        with pytest.raises(HTTPException):
            await desktop_auth.consume_code(db, code, VERIFIER)
    asyncio.run(run())


def test_desktop_login_requires_server_opt_in(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test")
    monkeypatch.delenv("DESKTOP_AUTH_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.google_start(desktop_redirect_uri=CALLBACK,
            desktop_code_challenge=CHALLENGE, desktop_state=STATE))
    assert exc.value.status_code == 503


def test_desktop_start_keeps_google_callback_on_cloud(monkeypatch):
    monkeypatch.setenv("DESKTOP_AUTH_ENABLED", "true")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test")
    response = asyncio.run(auth.google_start(desktop_redirect_uri=CALLBACK,
        desktop_code_challenge=CHALLENGE, desktop_state=STATE))
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == [auth.GOOGLE_REDIRECT_URI]
    assert "oauth_desktop=" in "\n".join(response.headers.getlist("set-cookie"))


def test_web_start_clears_previous_desktop_context(monkeypatch):
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test")
    response = asyncio.run(auth.google_start())
    assert 'oauth_desktop="";' in "\n".join(response.headers.getlist("set-cookie"))


def test_exchange_sets_httponly_cookie_without_returning_tokens(monkeypatch):
    monkeypatch.setenv("DESKTOP_AUTH_ENABLED", "true")
    monkeypatch.setattr(auth, "COOKIE_SECURE", True)

    async def issue(*args):
        return "server-refresh-token"
    monkeypatch.setattr(auth, "_issue_refresh_token", issue)

    async def run():
        db = FakeDB()
        code = await desktop_auth.issue_code(db, db.user_id, {"challenge": CHALLENGE})
        response = Response()
        result = await auth.desktop_exchange(auth.DesktopExchangeRequest(code=code, code_verifier=VERIFIER), response, db)
        assert result == {"status": "success"}
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert response.headers["cache-control"] == "no-store"
    asyncio.run(run())


def test_exchange_rejects_disabled_accounts(monkeypatch):
    monkeypatch.setenv("DESKTOP_AUTH_ENABLED", "true")

    async def run():
        db = FakeDB()
        db.status = "disabled"
        code = await desktop_auth.issue_code(db, db.user_id, {"challenge": CHALLENGE})
        with pytest.raises(HTTPException) as exc:
            await auth.desktop_exchange(auth.DesktopExchangeRequest(code=code, code_verifier=VERIFIER), Response(), db)
        assert exc.value.status_code == 403
    asyncio.run(run())


def test_google_callback_returns_only_one_use_code_to_loopback(monkeypatch):
    monkeypatch.setenv("DESKTOP_AUTH_ENABLED", "true")

    class GoogleResponse:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class GoogleClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return GoogleResponse({"access_token": "google-token"})

        async def get(self, *args, **kwargs):
            return GoogleResponse({"sub": "google-sub", "email": "test@example.com"})

    monkeypatch.setattr(auth.httpx, "AsyncClient", GoogleClient)

    async def run():
        db = FakeDB()
        context = desktop_auth.seal_context(CALLBACK, CHALLENGE, STATE, "oauth-state")
        response = await auth.google_callback(db=db, code="google-code", state="oauth-state", error=None,
            oauth_state="oauth-state", oauth_return_url=None, oauth_desktop=context)
        location = urlparse(response.headers["location"])
        assert location.hostname == "127.0.0.1"
        params = parse_qs(location.query)
        assert set(params) == {"state", "code"}
        assert params["state"] == [STATE]
        assert await desktop_auth.consume_code(db, params["code"][0], VERIFIER) == db.user_id
        assert "refresh_token=" not in "\n".join(response.headers.getlist("set-cookie"))
        assert response.headers["referrer-policy"] == "no-referrer"
    asyncio.run(run())


def test_google_cancellation_returns_desktop_error_without_db_access():
    async def run():
        token = desktop_auth.seal_context(CALLBACK, CHALLENGE, STATE, "oauth-state")
        response = await auth.google_callback(db=None, code=None, state="oauth-state", error="access_denied",
            oauth_state="oauth-state", oauth_return_url=None, oauth_desktop=token)
        params = parse_qs(urlparse(response.headers["location"]).query)
        assert params == {"state": [STATE], "error": ["oauth_cancelled"]}
    asyncio.run(run())
