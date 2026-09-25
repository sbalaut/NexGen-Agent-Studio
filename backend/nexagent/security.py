"""Passwords, sessions, CSRF, login throttling and API-key identities.

Throttling (review point 5): no site-wide bucket. Failures are counted per
account and per client address; after a small allowance the lock time grows
exponentially (30 s, 60 s, 120 s ... capped at 15 min). A successful sign-in
clears the account counter. Client address comes from X-Forwarded-For only when
NEXAGENT_TRUSTED_PROXY=1 (e.g. behind the JupyterHub proxy).
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings
from .policy import Actor, load_actor

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_DUMMY = hasher.hash("nexagent-dummy-password-not-an-account")

ACCOUNT_ALLOWANCE = 5
ADDRESS_ALLOWANCE = 30
BASE_LOCK_S = 30
MAX_LOCK_S = 15 * 60
MIN_PASSWORD = 12


def hash_password(value: str) -> str:
    return hasher.hash(value)


def check_password(password: str, stored: str | None) -> bool:
    try:
        return hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hmac.new(settings().secret().encode(), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


def key_verifier(secret: str) -> str:
    return hmac.new(settings().secret().encode(), ("apikey:" + secret).encode(), hashlib.sha256).hexdigest()


def client_address(request: Request) -> str:
    if settings().trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def lock_seconds(failures: int, allowance: int) -> float:
    if failures <= allowance:
        return 0.0
    return float(min(MAX_LOCK_S, BASE_LOCK_S * math.pow(2, failures - allowance - 1)))


def throttle_state(conn: sqlite3.Connection, keys: list[str]) -> float:
    """Returns seconds remaining on the longest active lock (0 when free)."""
    t = db.now()
    remaining = 0.0
    for key in keys:
        row = conn.execute("SELECT locked_until FROM login_failures WHERE key=?", (token_digest(key),)).fetchone()
        if row and row[0] > t:
            remaining = max(remaining, row[0] - t)
    return remaining


def record_failure(conn: sqlite3.Connection, key: str, allowance: int) -> None:
    k, t = token_digest(key), db.now()
    row = conn.execute("SELECT failures,last_failure FROM login_failures WHERE key=?", (k,)).fetchone()
    failures = 1 if not row or t - row[1] > 24 * 3600 else row[0] + 1
    conn.execute("""INSERT INTO login_failures(key,failures,locked_until,last_failure) VALUES(?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET failures=excluded.failures, locked_until=excluded.locked_until,
        last_failure=excluded.last_failure""", (k, failures, t + lock_seconds(failures, allowance), t))


def clear_failures(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM login_failures WHERE key=?", (token_digest(key),))


def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        # Same-origin fetches from older browsers may omit Origin; fall back to Referer.
        ref = request.headers.get("referer", "")
        origin = "/".join(ref.split("/")[:3]) if ref else ""
    if origin.rstrip("/") != settings().origin:
        raise HTTPException(403, "Request origin is not allowed")


def new_session(conn: sqlite3.Connection, user_id: str) -> str:
    raw = secrets.token_urlsafe(48)
    t = db.now()
    conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                 (token_digest(raw), user_id, t, t + settings().session_hours * 3600))
    conn.execute("DELETE FROM sessions WHERE expires_at<?", (t,))
    return raw


def session_actor(raw: str) -> Actor | None:
    if not raw or not (32 <= len(raw) <= 256):
        return None
    with db.read() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
                           (token_digest(raw), db.now())).fetchone()
        return load_actor(conn, row[0]) if row else None


def current_actor(request: Request) -> Actor:
    raw = request.cookies.get(settings().cookie_name, "")
    actor = session_actor(raw)
    if actor is None:
        raise HTTPException(401, "Your session has expired. Sign in again.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_origin(request)
        sent = request.headers.get("x-csrf-token") or ""
        if not sent or not hmac.compare_digest(sent, csrf_for(raw)):
            raise HTTPException(403, "Your security token is missing or invalid. Reload and try again.")
    return actor


CurrentActor = Depends(current_actor)


def api_key_identity(request: Request) -> dict:
    """Service identity for published assistant API calls: 'Authorization: Bearer nxk_<prefix>_<secret>'."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer nxk_"):
        raise HTTPException(401, "An assistant API key is required")
    try:
        _, prefix, secret = auth.split(" ", 1)[1].split("_", 2)
    except ValueError:
        raise HTTPException(401, "Malformed API key") from None
    with db.read() as conn:
        row = db.one(conn, """SELECT k.*, a.slug, a.project_id FROM api_keys k JOIN assistants a ON a.id=k.assistant_id
            WHERE k.prefix=? AND k.revoked_at IS NULL""", (prefix,))
    if not row or not hmac.compare_digest(row["verifier"], key_verifier(secret)):
        raise HTTPException(401, "API key is invalid or revoked")
    return row


def api_key_active(conn: sqlite3.Connection, key_id: str) -> bool:
    return conn.execute("SELECT 1 FROM api_keys WHERE id=? AND revoked_at IS NULL", (key_id,)).fetchone() is not None
