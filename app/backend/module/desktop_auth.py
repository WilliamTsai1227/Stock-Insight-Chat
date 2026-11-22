"""Desktop OAuth handoff: loopback-only redirects and PKCE-bound, one-use codes."""

import base64
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from fastapi import HTTPException
from jose import jwt

from app.backend.module.jwt import ALGORITHM, SECRET_KEY


def validate_desktop_request(redirect_uri: str, challenge: str, state: str) -> None:
    try:
        uri = urlparse(redirect_uri)
        port = uri.port
    except ValueError:
        raise HTTPException(400, "Invalid desktop redirect URI")
    if (
        uri.scheme != "http" or uri.hostname != "127.0.0.1"
        or not port or not 1024 <= port <= 65535
        or uri.netloc != f"127.0.0.1:{port}"
        or uri.path != "/oauth/callback" or uri.query or uri.fragment
    ):
        raise HTTPException(400, "Desktop redirect must use a loopback callback")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise HTTPException(400, "An S256 code challenge is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", state):
        raise HTTPException(400, "Invalid desktop state")


def seal_context(redirect_uri: str, challenge: str, state: str, oauth_state: str) -> str:
    return jwt.encode({
        "type": "desktop_oauth", "redirect_uri": redirect_uri,
        "challenge": challenge, "state": state, "oauth_state": oauth_state,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }, SECRET_KEY, algorithm=ALGORITHM)


def read_context(token: str | None, oauth_state: str | None) -> dict | None:
    if not token or not oauth_state:
        return None
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if data.get("type") != "desktop_oauth" or not secrets.compare_digest(
            data.get("oauth_state", ""), oauth_state
        ):
            return None
        validate_desktop_request(data["redirect_uri"], data["challenge"], data["state"])
        return data
    except Exception:
        return None


def callback_url(context: dict, **params: str) -> str:
    return context["redirect_uri"] + "?" + urlencode({"state": context["state"], **params})


async def issue_code(db, user_id, context: dict) -> str:
    code = secrets.token_urlsafe(32)
    # Only a hash is stored, and the verifier never leaves the desktop until exchange.
    await db.execute("DELETE FROM desktop_login_codes WHERE expires_at <= NOW()")
    await db.execute(
        """INSERT INTO desktop_login_codes (code_hash, user_id, code_challenge, expires_at)
           VALUES ($1, $2, $3, $4)""",
        hashlib.sha256(code.encode()).hexdigest(), user_id, context["challenge"],
        datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    return code


async def consume_code(db, code: str, verifier: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code) or not re.fullmatch(
        r"[A-Za-z0-9._~-]{43,128}", verifier
    ):
        raise HTTPException(400, "Invalid desktop code or verifier")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    # Check PKCE in the DELETE itself: a wrong verifier cannot consume a valid code.
    row = await db.fetchrow(
        """DELETE FROM desktop_login_codes
           WHERE code_hash = $1 AND code_challenge = $2 AND expires_at > NOW()
           RETURNING user_id""",
        hashlib.sha256(code.encode()).hexdigest(), challenge,
    )
    if not row:
        raise HTTPException(401, "Desktop login expired or already used")
    return row["user_id"]
