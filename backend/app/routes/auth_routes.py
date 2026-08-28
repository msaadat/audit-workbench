"""Sign-in, sign-out, and the current account.

  POST /api/auth/login          exchange credentials for a session cookie
  POST /api/auth/logout         revoke this session
  GET  /api/auth/me             who am I, and what mode is this
  GET  /api/auth/invite/{token} the email an invitation is for
  POST /api/auth/invite/{token} accept an invitation by choosing a password

These are the only API endpoints reachable without a session, which is what
lets the login screen load at all.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Body, Request, Response

from .. import accounts, auth, sessions

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _secure_cookies(request: Request) -> bool:
    """HTTPS-only unless the deployment says otherwise or this is plain HTTP.

    An in-house server reached over plain HTTP would silently drop a ``Secure``
    cookie and leave sign-in mysteriously broken, so the flag follows the scheme
    the request actually arrived on.  ``SESSION_COOKIE_SECURE`` overrides it for
    a deployment behind a TLS-terminating proxy.
    """
    configured = str(os.environ.get("SESSION_COOKIE_SECURE") or "").strip().lower()
    if configured in {"1", "true", "yes"}:
        return True
    if configured in {"0", "false", "no"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return (forwarded or request.url.scheme) == "https"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        httponly=True,
        secure=_secure_cookies(request),
        # Lax rather than Strict: a bookmarked deep link into a workspace is a
        # top-level GET navigation and must arrive already signed in.  Lax still
        # withholds the cookie from cross-site POSTs, which with the Origin
        # check in ``main`` is what covers CSRF.
        samesite="lax",
        max_age=int(sessions.ttl().total_seconds()),
        path="/",
    )


@router.post("/login")
async def login(request: Request, response: Response, payload: dict = Body(...)):
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    user = accounts.authenticate(email, password)
    if user is None:
        accounts.record_auth_event("login.failed", email=email)
        # One message for every failure: a distinct "no such account" reply
        # would turn this endpoint into a directory of who has one.
        return Response(
            content='{"detail":"That email and password do not match an account."}',
            status_code=401,
            media_type="application/json",
        )
    token = sessions.create(user["id"])
    _set_session_cookie(request, response, token)
    accounts.record_auth_event("login", user_id=user["id"], email=user["email"])
    return {"user": user, "auth_mode": auth.auth_mode()}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token:
        user = sessions.resolve(token)
        sessions.revoke(token)
        if user is not None:
            accounts.record_auth_event("logout", user_id=user["id"], email=user["email"])
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """The session bootstrap the SPA calls before routing.

    Answers for an anonymous caller too — ``user: null`` is how the frontend
    learns it must show the login screen, so this must not 401.
    """
    principal = getattr(request.state, "principal", None)
    user = accounts.find(principal.user_id) if principal is not None else None
    return {
        "user": user,
        "auth_mode": auth.auth_mode(),
        "single_user": auth.single_user_mode(),
    }


@router.get("/invite/{token}")
async def read_invite(token: str):
    invite = accounts.peek_invite(token)
    if invite is None:
        return Response(
            content='{"detail":"This invitation is not valid. Ask for a new one."}',
            status_code=404,
            media_type="application/json",
        )
    return invite


@router.post("/invite/{token}")
async def accept_invite(token: str, request: Request, response: Response,
                        payload: dict = Body(...)):
    user = accounts.accept_invite(
        token,
        str(payload.get("password") or ""),
        display_name=str(payload.get("display_name") or ""),
    )
    session_token = sessions.create(user["id"])
    _set_session_cookie(request, response, session_token)
    accounts.record_auth_event("invite.accepted", user_id=user["id"], email=user["email"])
    return {"user": user}
