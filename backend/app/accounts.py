"""User accounts in the control plane.

Accounts are admin-provisioned: there is no self-service registration path.
The first administrator is created by the ``backend/manage.py`` bootstrap
command, not by a route.

Password hashing uses the standard library's scrypt.  It keeps the dependency
count at zero, which matches how the rest of this codebase treats dependencies
(``config`` hand-rolls dotenv parsing for the same reason).  ``argon2-cffi`` is
the upgrade if a dependency ever becomes acceptable; ``verify_password`` reads
the parameters back out of the stored hash, so old hashes keep verifying if the
parameters below are ever raised.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import db

# The principal every single-user (local) installation runs as.  Multi-user
# deployments never resolve to this account.
LOCAL_USER_ID = "local"
LOCAL_USER_EMAIL = "local@workbench.local"

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(ValueError):
    """A user-facing account problem (bad email, duplicate, unknown user)."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_user_id() -> str:
    return f"u_{uuid.uuid4().hex[:24]}"


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    if len(str(password)) < 8:
        raise AccountError("A password must be at least 8 characters.")
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        str(password).encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, expected_hex = str(stored).split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            str(password).encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(expected_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), expected_hex)


# ------------------------------------------------------------------- accounts
def _clean_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if not _EMAIL.match(email):
        raise AccountError(f"'{email}' is not a valid email address.")
    return email


def create_user(
    email: str,
    password: str = "",
    *,
    display_name: str = "",
    is_admin: bool = False,
    user_id: str | None = None,
) -> dict:
    address = _clean_email(email)
    if find_by_email(address) is not None:
        raise AccountError(f"An account for '{address}' already exists.")
    record = {
        "id": user_id or new_user_id(),
        "email": address,
        "display_name": str(display_name or "").strip() or address.split("@")[0],
        "password_hash": hash_password(password) if password else "",
        "is_admin": 1 if is_admin else 0,
        "status": "active",
        "created_at": utcnow(),
    }
    db.execute(
        "INSERT INTO users (id, email, display_name, password_hash, is_admin, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        tuple(record[key] for key in
              ("id", "email", "display_name", "password_hash", "is_admin", "status", "created_at")),
    )
    return _project(record)


def _project(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "is_admin": bool(row["is_admin"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def find_by_email(email: str) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE email = ?", (str(email).strip().lower(),))
    return _project(row) if row is not None else None


def find(user_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE id = ?", (str(user_id),))
    return _project(row) if row is not None else None


def require(user_id: str) -> dict:
    user = find(user_id)
    if user is None:
        raise AccountError(f"Unknown user '{user_id}'.")
    return user


def list_users() -> list[dict]:
    return [_project(row) for row in db.query("SELECT * FROM users ORDER BY created_at, email")]


def authenticate(email: str, password: str) -> dict | None:
    row = db.query_one("SELECT * FROM users WHERE email = ?", (str(email).strip().lower(),))
    if row is None or row["status"] != "active" or not row["password_hash"]:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return _project(row)


def set_password(user_id: str, password: str) -> None:
    require(user_id)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
               (hash_password(password), str(user_id)))


def set_status(user_id: str, status: str) -> dict:
    if status not in {"active", "disabled"}:
        raise AccountError("A user's status must be 'active' or 'disabled'.")
    require(user_id)
    db.execute("UPDATE users SET status = ? WHERE id = ?", (status, str(user_id)))
    if status == "disabled":
        db.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))
    return require(user_id)


def ensure_local_user() -> dict:
    """The account a single-user installation runs as, created on demand.

    Single-user mode still writes real registry rows, so the owner reference
    they carry has to exist.  Creating it lazily keeps local use and the test
    suite free of any setup step.
    """
    existing = find(LOCAL_USER_ID)
    if existing is not None:
        return existing
    return create_user(
        LOCAL_USER_EMAIL, display_name="Local", is_admin=True, user_id=LOCAL_USER_ID,
    )


def adopt_local_account(email: str, password: str, display_name: str = "") -> dict:
    """Turn the built-in local account into a real administrator, in place.

    A single-user installation already owns its workspaces as ``local``: the
    directories sit in that home and the registry rows point at it.  Promoting
    the row rather than creating a second account means enabling multi-user
    moves no files and rewrites no registry rows — the id simply stops being
    anonymous.
    """
    address = _clean_email(email)
    existing = find_by_email(address)
    if existing is not None and existing["id"] != LOCAL_USER_ID:
        raise AccountError(f"An account for '{address}' already exists.")
    ensure_local_user()
    db.execute(
        "UPDATE users SET email = ?, display_name = ?, password_hash = ?, is_admin = 1"
        " WHERE id = ?",
        (address, str(display_name or "").strip() or address.split("@")[0],
         hash_password(password), LOCAL_USER_ID),
    )
    return require(LOCAL_USER_ID)


# --------------------------------------------------------------------- invites
# Accounts are admin-provisioned, so an invite is how a new auditor gets to
# choose their own password without an administrator ever handling it.
INVITE_TTL_DAYS = 7


def _invite_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_invite(email: str, created_by: str) -> tuple[str, dict]:
    """Issue an invite and return the raw token; only its hash is stored."""
    address = _clean_email(email)
    require(created_by)
    if find_by_email(address) is not None:
        raise AccountError(f"An account for '{address}' already exists.")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)
    db.execute(
        "INSERT INTO invites (token_hash, email, created_by, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_invite_hash(token), address, str(created_by), utcnow(),
         expires.isoformat(timespec="seconds")),
    )
    return token, {"email": address, "expires_at": expires.isoformat(timespec="seconds")}


def peek_invite(token: str) -> dict | None:
    """The email an unexpired, unaccepted invite is for, without consuming it."""
    row = db.query_one("SELECT * FROM invites WHERE token_hash = ?", (_invite_hash(token),))
    if row is None or row["accepted_at"]:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return None
    if not expires.tzinfo:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        return None
    return {"email": row["email"]}


def accept_invite(token: str, password: str, display_name: str = "") -> dict:
    invite = peek_invite(token)
    if invite is None:
        raise AccountError("This invitation is not valid. Ask for a new one.")
    user = create_user(invite["email"], password, display_name=display_name)
    db.execute(
        "UPDATE invites SET accepted_at = ? WHERE token_hash = ?",
        (utcnow(), _invite_hash(token)),
    )
    return user


def list_invites() -> list[dict]:
    return [
        {"email": row["email"], "created_at": row["created_at"],
         "expires_at": row["expires_at"], "accepted_at": row["accepted_at"]}
        for row in db.query("SELECT * FROM invites ORDER BY created_at DESC")
    ]


def record_auth_event(event: str, *, user_id: str | None = None,
                      email: str = "", detail: str = "") -> None:
    db.execute(
        "INSERT INTO auth_events (id, user_id, email, event, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
        (secrets.token_hex(12), user_id, str(email or ""), str(event), str(detail or ""), utcnow()),
    )
