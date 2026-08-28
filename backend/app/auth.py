"""Who the current request is acting as.

Phase 1 ships the *seam*, not the login screen.  ``AUTH_MODE`` defaults to
``single_user``, where every request resolves to the local account and the
application behaves exactly as it did before — which is what lets the whole
tenancy refactor land and be regression-tested before any user exists.

``multi_user`` resolves the principal from the request's session instead.  The
session lookup arrives in Phase 3; until then the mode fails closed rather than
falling back to an ambient account.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from . import accounts

SINGLE_USER = "single_user"
MULTI_USER = "multi_user"

_principal: ContextVar["Principal | None"] = ContextVar("workbench_principal", default=None)


class AuthError(RuntimeError):
    """No principal could be established for this request."""


@dataclass(frozen=True)
class Principal:
    """The actor a request is performed on behalf of."""

    user_id: str
    is_admin: bool = False

    @property
    def id(self) -> str:
        return self.user_id


def auth_mode() -> str:
    """Read per call so a deployment can flip modes without a code change."""
    value = str(os.environ.get("AUTH_MODE") or "").strip().lower()
    return value if value in {SINGLE_USER, MULTI_USER} else SINGLE_USER


def single_user_mode() -> bool:
    return auth_mode() == SINGLE_USER


def local_principal() -> Principal:
    """The account a single-user installation acts as.

    Defaults to the built-in ``local`` account, which is what makes a fresh
    install and the test suite work with no setup.  ``WORKBENCH_LOCAL_USER``
    (an id or an email) points it at a real account instead — the escape hatch
    for an installation that bootstrapped an admin before auth landed and wants
    that admin to own its workspaces.
    """
    named = str(os.environ.get("WORKBENCH_LOCAL_USER") or "").strip()
    if named:
        user = accounts.find(named) or accounts.find_by_email(named)
        if user is None:
            raise AuthError(
                f"WORKBENCH_LOCAL_USER names '{named}', which is not an account."
            )
        return Principal(user_id=user["id"], is_admin=bool(user["is_admin"]))
    user = accounts.ensure_local_user()
    return Principal(user_id=user["id"], is_admin=True)


def current_principal() -> Principal:
    """The actor for the current request, or the local account offline.

    Background work must not rely on this.  Agent runs execute on daemon
    threads that no request context reaches, which is exactly why every
    internal reload goes through ``Workspace.reload()`` — a ``Workspace``
    already carries its own root, so holding one proves the resolver
    authorized it and no principal needs to cross the thread boundary.
    """
    established = _principal.get()
    if established is not None:
        return established
    if single_user_mode():
        return local_principal()
    raise AuthError("This request has no authenticated user.")


@contextmanager
def principal_scope(principal: Principal | None):
    token = _principal.set(principal)
    try:
        yield principal
    finally:
        _principal.reset(token)


def resolve_request_principal(request) -> Principal | None:
    """The principal a request carries, or ``None`` when it is anonymous."""
    if single_user_mode():
        return local_principal()
    from . import sessions

    user = sessions.resolve(request.cookies.get(sessions.COOKIE_NAME) or "")
    if user is None:
        return None
    return Principal(user_id=user["id"], is_admin=bool(user["is_admin"]))


class PermissionError_(RuntimeError):
    """The actor is authenticated but not allowed to do this."""


def require_admin(principal: Principal) -> Principal:
    """Administrative surfaces are admin-only in every mode.

    Single-user runs as an administrator, so this is transparent locally and
    load-bearing only once real accounts exist.
    """
    if not principal.is_admin:
        raise PermissionError_("This action requires an administrator.")
    return principal


# ------------------------------------------------------------- FastAPI wiring
def current_actor(request) -> Principal:
    """Route dependency: the authenticated actor, or a 401-shaped failure."""
    principal = getattr(request.state, "principal", None) or _principal.get()
    if principal is None:
        raise AuthError("This request has no authenticated user.")
    return principal
