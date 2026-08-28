"""Administrative surfaces: accounts and invitations.

Every endpoint here requires an administrator.  There is deliberately no
self-service registration route anywhere in the API — an account exists because
an administrator created it or invited its holder.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .. import accounts, auth, registry, sessions, usage

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin(request: Request):
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise auth.AuthError("This request has no authenticated user.")
    return auth.require_admin(principal)


def _with_counts(user: dict) -> dict:
    return {**user, "workspace_count": len(registry.list_for_owner(user["id"]))}


@router.get("/users")
async def list_users(request: Request):
    _admin(request)
    return [_with_counts(user) for user in accounts.list_users()]


@router.post("/users")
async def create_user(request: Request, payload: dict = Body(...)):
    _admin(request)
    user = accounts.create_user(
        str(payload.get("email") or ""),
        str(payload.get("password") or ""),
        display_name=str(payload.get("display_name") or ""),
        is_admin=bool(payload.get("is_admin")),
    )
    registry.user_workspaces_dir(user["id"]).mkdir(parents=True, exist_ok=True)
    accounts.record_auth_event("user.created", user_id=user["id"], email=user["email"])
    return _with_counts(user)


@router.post("/users/{user_id}/status")
async def set_status(user_id: str, request: Request, payload: dict = Body(...)):
    actor = _admin(request)
    status = str(payload.get("status") or "")
    if user_id == actor.user_id and status == "disabled":
        # Locking the last administrator out of their own installation is not a
        # recoverable mistake from inside the app.
        raise accounts.AccountError("You cannot disable your own account.")
    user = accounts.set_status(user_id, status)
    accounts.record_auth_event(f"user.{status}", user_id=user["id"], email=user["email"])
    return _with_counts(user)


@router.post("/users/{user_id}/password")
async def reset_password(user_id: str, request: Request, payload: dict = Body(...)):
    _admin(request)
    accounts.set_password(user_id, str(payload.get("password") or ""))
    # A reset is only meaningful if it ends the sessions opened with the old one.
    sessions.revoke_all(user_id)
    user = accounts.require(user_id)
    accounts.record_auth_event("user.password_reset", user_id=user_id, email=user["email"])
    return {"ok": True}


@router.get("/usage")
async def usage_totals(request: Request):
    """Who consumed the shared provider budget.

    The provider bills one key for the whole server, so this is the only place
    the question can be answered.
    """
    _admin(request)
    by_user = {row["user_id"]: row for row in usage.totals_by_user()}
    return [
        {
            "user_id": user["id"],
            "email": user["email"],
            **{key: value for key, value in (by_user.get(user["id"]) or {}).items()
               if key != "user_id"},
        }
        for user in accounts.list_users()
    ]


@router.get("/invites")
async def list_invites(request: Request):
    _admin(request)
    return accounts.list_invites()


@router.post("/invites")
async def create_invite(request: Request, payload: dict = Body(...)):
    actor = _admin(request)
    token, invite = accounts.create_invite(str(payload.get("email") or ""), actor.user_id)
    accounts.record_auth_event("invite.created", user_id=actor.user_id,
                               email=invite["email"])
    # Returned once and never recoverable: only the hash is stored.
    return {**invite, "token": token, "path": f"/invite/{token}"}
