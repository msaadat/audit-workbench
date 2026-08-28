"""Phase 3: sessions, the auth gate, and multi-user isolation over HTTP.

The single-user tests assert the local-first product is unchanged; the
multi-user tests assert that two accounts genuinely cannot reach each other.
"""

import pytest
from fastapi.testclient import TestClient

from app import accounts, sessions, workspaces
from app.main import create_app


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")


@pytest.fixture
def client():
    return TestClient(create_app())


def _account(email="auditor@example.com", password="a-good-password", admin=False):
    return accounts.create_user(email, password, is_admin=admin)


def _sign_in(client, email, password="a-good-password"):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


# ------------------------------------------------------------------ sessions
def test_a_session_resolves_to_its_account():
    user = _account()
    token = sessions.create(user["id"])
    assert sessions.resolve(token)["id"] == user["id"]


def test_only_the_hash_of_a_token_is_stored():
    from app import db

    user = _account()
    token = sessions.create(user["id"])
    stored = db.query("SELECT token_hash FROM sessions")[0]["token_hash"]
    assert token not in stored
    assert stored == sessions.hash_token(token)


def test_a_revoked_session_stops_resolving():
    user = _account()
    token = sessions.create(user["id"])
    sessions.revoke(token)
    assert sessions.resolve(token) is None


def test_an_expired_session_is_refused_and_swept(monkeypatch):
    user = _account()
    token = sessions.create(user["id"])
    from app import db

    db.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00+00:00'")
    assert sessions.resolve(token) is None
    assert db.query("SELECT * FROM sessions") == []


def test_disabling_an_account_invalidates_its_live_session():
    user = _account()
    token = sessions.create(user["id"])
    accounts.set_status(user["id"], "disabled")
    assert sessions.resolve(token) is None


def test_an_unknown_token_resolves_to_nothing():
    assert sessions.resolve("not-a-real-token") is None
    assert sessions.resolve("") is None


# --------------------------------------------------------------- single user
def test_single_user_needs_no_sign_in(client):
    """The local-first product must be unchanged: no login, no gate."""
    identity = client.get("/api/auth/me").json()
    assert identity["single_user"] is True
    assert identity["user"]["id"] == accounts.LOCAL_USER_ID
    assert client.get("/api/workspaces").status_code == 200


# ---------------------------------------------------------------- multi user
def test_the_api_is_closed_without_a_session(multi_user, client):
    response = client.get("/api/workspaces")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_the_login_surface_is_reachable_anonymously(multi_user, client):
    identity = client.get("/api/auth/me").json()
    assert identity["user"] is None
    assert identity["single_user"] is False


def test_sign_in_then_use_the_api(multi_user, client):
    _account()
    _sign_in(client, "auditor@example.com")
    assert client.get("/api/workspaces").status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == "auditor@example.com"


def test_a_wrong_password_is_refused_without_saying_why(multi_user, client):
    _account()
    wrong = client.post("/api/auth/login",
                        json={"email": "auditor@example.com", "password": "nope"})
    missing = client.post("/api/auth/login",
                          json={"email": "ghost@example.com", "password": "nope"})
    assert wrong.status_code == missing.status_code == 401
    # Identical replies: otherwise this endpoint enumerates who has an account.
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_a_disabled_account_cannot_sign_in(multi_user, client):
    user = _account()
    accounts.set_status(user["id"], "disabled")
    assert client.post("/api/auth/login",
                       json={"email": "auditor@example.com",
                             "password": "a-good-password"}).status_code == 401


def test_signing_out_closes_the_session(multi_user, client):
    _account()
    _sign_in(client, "auditor@example.com")
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/workspaces").status_code == 401


def test_two_users_cannot_see_each_others_workspaces(multi_user, client):
    alice = _account("alice@example.com")
    bob = _account("bob@example.com")
    from app import auth

    hers = workspaces.create_workspace("Hers", actor=auth.Principal(user_id=alice["id"]))
    his = workspaces.create_workspace("His", actor=auth.Principal(user_id=bob["id"]))

    _sign_in(client, "alice@example.com")
    assert [w["id"] for w in client.get("/api/workspaces").json()] == [hers.uid]
    assert client.get(f"/api/workspaces/{hers.uid}").status_code == 200
    # Bob's workspace is reported missing, not forbidden.
    assert client.get(f"/api/workspaces/{his.uid}").status_code == 400

    client.post("/api/auth/logout")
    _sign_in(client, "bob@example.com")
    assert [w["id"] for w in client.get("/api/workspaces").json()] == [his.uid]


def test_a_workspace_created_over_http_belongs_to_the_caller(multi_user, client):
    _account("alice@example.com")
    _sign_in(client, "alice@example.com")
    created = client.post("/api/workspaces", json={"name": "Mine"}).json()
    assert client.get(f"/api/workspaces/{created['id']}").status_code == 200

    _account("bob@example.com")
    client.post("/api/auth/logout")
    _sign_in(client, "bob@example.com")
    assert client.get("/api/workspaces").json() == []
    assert client.get(f"/api/workspaces/{created['id']}").status_code == 400


# --------------------------------------------------------------------- CSRF
def test_a_cross_site_mutation_is_refused(multi_user, client):
    _account()
    _sign_in(client, "auditor@example.com")
    response = client.post("/api/workspaces", json={"name": "Evil"},
                           headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert client.get("/api/workspaces").json() == []


def test_a_same_origin_mutation_is_allowed(multi_user, client):
    _account()
    _sign_in(client, "auditor@example.com")
    response = client.post("/api/workspaces", json={"name": "Fine"},
                           headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_a_cross_site_read_is_untouched(multi_user, client):
    """Only state-changing methods are gated; GETs carry no CSRF risk."""
    _account()
    _sign_in(client, "auditor@example.com")
    assert client.get("/api/workspaces",
                      headers={"Origin": "https://attacker.example"}).status_code == 200


# -------------------------------------------------------------------- admin
def test_admin_surfaces_require_an_administrator(multi_user, client):
    _account("plain@example.com")
    _sign_in(client, "plain@example.com")
    assert client.get("/api/admin/users").status_code == 403
    assert client.post("/api/admin/invites",
                       json={"email": "x@example.com"}).status_code == 403


def test_an_administrator_can_provision_an_account(multi_user, client):
    _account("boss@example.com", admin=True)
    _sign_in(client, "boss@example.com")
    created = client.post("/api/admin/users",
                          json={"email": "new@example.com",
                                "password": "a-good-password"})
    assert created.status_code == 200
    assert created.json()["workspace_count"] == 0
    emails = [user["email"] for user in client.get("/api/admin/users").json()]
    assert "new@example.com" in emails


def test_an_administrator_cannot_disable_themselves(multi_user, client):
    boss = _account("boss@example.com", admin=True)
    _sign_in(client, "boss@example.com")
    response = client.post(f"/api/admin/users/{boss['id']}/status",
                           json={"status": "disabled"})
    assert response.status_code == 400


def test_a_password_reset_ends_the_old_sessions(multi_user, client):
    boss = _account("boss@example.com", admin=True)
    victim = _account("victim@example.com")
    stolen = sessions.create(victim["id"])
    _sign_in(client, "boss@example.com")
    assert client.post(f"/api/admin/users/{victim['id']}/password",
                       json={"password": "brand-new-password"}).status_code == 200
    assert sessions.resolve(stolen) is None


# ------------------------------------------------------------------ invites
def test_an_invitation_is_redeemed_once(multi_user, client):
    boss = _account("boss@example.com", admin=True)
    _sign_in(client, "boss@example.com")
    invite = client.post("/api/admin/invites", json={"email": "new@example.com"}).json()
    client.post("/api/auth/logout")

    assert client.get(f"/api/auth/invite/{invite['token']}").json()["email"] == "new@example.com"
    accepted = client.post(f"/api/auth/invite/{invite['token']}",
                           json={"password": "a-good-password"})
    assert accepted.status_code == 200
    # Signed in immediately, and the invitation is spent.
    assert client.get("/api/auth/me").json()["user"]["email"] == "new@example.com"
    assert client.get(f"/api/auth/invite/{invite['token']}").status_code == 404


def test_an_invitation_token_is_not_stored_in_the_clear(multi_user, client):
    from app import db

    boss = _account("boss@example.com", admin=True)
    _sign_in(client, "boss@example.com")
    invite = client.post("/api/admin/invites", json={"email": "new@example.com"}).json()
    stored = db.query("SELECT token_hash FROM invites")[0]["token_hash"]
    assert invite["token"] not in stored


def test_an_unknown_invitation_is_refused(multi_user, client):
    assert client.get("/api/auth/invite/made-up").status_code == 404
    assert client.post("/api/auth/invite/made-up",
                       json={"password": "a-good-password"}).status_code == 400
