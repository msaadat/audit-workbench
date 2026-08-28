"""Per-user model-usage accounting.

Provider credentials are administrator-owned and shared, so the provider's own
dashboard reports one bill for the whole server.  Attribution has to happen
here or nowhere: without it there is no way to answer which auditor consumed
the budget, and no basis for a quota later.

Recording is best-effort by design.  Accounting must never be the reason a
model call that already succeeded is reported as failed.
"""

from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone

from . import db

# Resolving a workspace's owner is a primary-key read, but a run makes hundreds
# of model calls against the same workspace, so the answer is memoised.
_owners: dict[str, str] = {}
_owners_guard = threading.Lock()


def owner_of(workspace_uid: str) -> str | None:
    if not workspace_uid:
        return None
    with _owners_guard:
        cached = _owners.get(workspace_uid)
    if cached:
        return cached
    from . import registry

    row = registry.locate(workspace_uid)
    if row is None:
        return None
    with _owners_guard:
        _owners[workspace_uid] = row["owner_id"]
    return row["owner_id"]


def forget_owners() -> None:
    """Drop the owner memo (tests repoint the data root)."""
    with _owners_guard:
        _owners.clear()


def record(
    *,
    workspace_uid: str,
    run_id: str | None = None,
    provider: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    turns: int = 1,
) -> None:
    try:
        owner_id = owner_of(workspace_uid)
        if owner_id is None:
            return
        db.execute(
            "INSERT INTO llm_usage (id, user_id, workspace_uid, run_id, provider,"
            " model, turns, prompt_tokens, completion_tokens, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                secrets.token_hex(12), owner_id, str(workspace_uid),
                str(run_id) if run_id else None, str(provider or ""), str(model or ""),
                int(turns), int(prompt_tokens), int(completion_tokens),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
    except Exception:
        # Never let accounting fail a call the provider already answered.
        pass


def totals_for(user_id: str, since: str | None = None) -> dict:
    clause = " AND at >= ?" if since else ""
    params: tuple = (str(user_id), since) if since else (str(user_id),)
    row = db.query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(turns), 0) AS turns,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens"
        f" FROM llm_usage WHERE user_id = ?{clause}",
        params,
    )
    return {
        "calls": int(row["calls"] or 0),
        "turns": int(row["turns"] or 0),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
    }


def totals_by_user(since: str | None = None) -> list[dict]:
    clause = " WHERE at >= ?" if since else ""
    params: tuple = (since,) if since else ()
    return [
        {
            "user_id": row["user_id"],
            "calls": int(row["calls"] or 0),
            "turns": int(row["turns"] or 0),
            "prompt_tokens": int(row["prompt_tokens"] or 0),
            "completion_tokens": int(row["completion_tokens"] or 0),
        }
        for row in db.query(
            "SELECT user_id, COUNT(*) AS calls, COALESCE(SUM(turns), 0) AS turns,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens"
            f" FROM llm_usage{clause} GROUP BY user_id ORDER BY prompt_tokens DESC",
            params,
        )
    ]
