"""Server-side sessions for cookie authentication.

The session is a row, not a signed token.  That is what makes sign-out and
account suspension actually revoke access rather than merely advise the client
to forget something: the next request finds no row and is refused.

Cookies rather than an ``Authorization`` header is forced rather than preferred.
The frontend streams agent and debug events over ``EventSource``, which cannot
set request headers — a bearer scheme would leave those two surfaces
unauthenticated or force them onto a query-string token, which then lands in
access logs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from . import accounts, db

COOKIE_NAME = "aw_session"
DEFAULT_TTL_HOURS = 12
# A sliding session would otherwise write to the database on every request.
# Extending only once the session is this far along keeps active use free while
# still meaning "12 hours of inactivity", not "12 hours from sign-in".
REFRESH_AFTER_SECONDS = 300


def ttl() -> timedelta:
    try:
        hours = float(os.environ.get("SESSION_TTL_HOURS") or DEFAULT_TTL_HOURS)
    except ValueError:
        hours = DEFAULT_TTL_HOURS
    return timedelta(hours=max(0.25, hours))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hash_token(token: str) -> str:
    """Only the hash is stored, so a database read cannot mint a session."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create(user_id: str) -> str:
    """Open a session and return the raw token; it is never stored."""
    accounts.require(user_id)
    token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (hash_token(token), str(user_id), _stamp(now), _stamp(now + ttl()), _stamp(now)),
    )
    return token


def resolve(token: str) -> dict | None:
    """The active account for a token, or ``None``.

    Expired rows are deleted on sight, and a session whose account has since
    been disabled resolves to nothing.
    """
    if not token:
        return None
    token_hash = hash_token(token)
    row = db.query_one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
    if row is None:
        return None
    expires = _parse(row["expires_at"])
    now = _now()
    if expires is None or expires <= now:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None
    user = accounts.find(row["user_id"])
    if user is None or user["status"] != "active":
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None
    last_seen = _parse(row["last_seen_at"])
    if last_seen is None or (now - last_seen).total_seconds() >= REFRESH_AFTER_SECONDS:
        db.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (_stamp(now), _stamp(now + ttl()), token_hash),
        )
    return user


def revoke(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))


def revoke_all(user_id: str) -> None:
    db.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))


def sweep_expired() -> int:
    cursor = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (_stamp(_now()),))
    return cursor.rowcount or 0


def active_for(user_id: str) -> list[dict]:
    return [
        {"created_at": row["created_at"], "last_seen_at": row["last_seen_at"],
         "expires_at": row["expires_at"]}
        for row in db.query(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
            (str(user_id),),
        )
    ]
