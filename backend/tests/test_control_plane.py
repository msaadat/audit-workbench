"""Phase 0: the SQLite control plane — schema, accounts, and the principal.

The control plane owns identity and authorization only.  Audit content stays on
the filesystem, and these tests are deliberately blind to it.
"""

import sqlite3

import pytest

from app import accounts, auth, db


# ------------------------------------------------------------------- schema
def test_schema_is_migrated_on_first_connection():
    connection = db.connect()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.SCHEMA_VERSION
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "users", "sessions", "workspaces", "workspace_members",
        "invites", "llm_usage", "auth_events",
    } <= tables


def test_migrations_are_idempotent():
    connection = db.connect()
    assert db.migrate(connection) == db.SCHEMA_VERSION
    assert db.migrate(connection) == db.SCHEMA_VERSION
    assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_foreign_keys_are_enforced():
    """Off by default in SQLite; the registry's owner reference depends on it."""
    connection = db.connect()
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO workspaces (uid, owner_id, dir_name, name, created_at)"
            " VALUES ('ws_x', 'nobody', 'x', 'x', '2026-01-01')"
        )


def test_the_database_follows_the_data_root(tmp_path):
    assert db.database_path().parent == tmp_path


# ----------------------------------------------------------------- accounts
def test_passwords_round_trip_and_reject_the_wrong_one():
    stored = accounts.hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert "correct horse battery" not in stored
    assert accounts.verify_password("correct horse battery", stored)
    assert not accounts.verify_password("wrong password", stored)


def test_a_short_password_is_refused():
    with pytest.raises(accounts.AccountError):
        accounts.hash_password("short")


def test_password_verification_survives_a_corrupt_hash():
    assert not accounts.verify_password("anything", "not-a-hash")
    assert not accounts.verify_password("anything", "")


def test_creating_a_user_and_finding_it_again():
    user = accounts.create_user("auditor@example.com", "a-good-password", display_name="Auditor")
    assert user["email"] == "auditor@example.com"
    assert user["is_admin"] is False
    assert accounts.find(user["id"]) == user
    assert accounts.find_by_email("AUDITOR@example.com") == user


def test_duplicate_email_is_refused():
    accounts.create_user("dup@example.com", "a-good-password")
    with pytest.raises(accounts.AccountError):
        accounts.create_user("dup@example.com", "another-password")


def test_an_invalid_email_is_refused():
    with pytest.raises(accounts.AccountError):
        accounts.create_user("not-an-email", "a-good-password")


def test_authentication_accepts_only_the_right_password():
    accounts.create_user("who@example.com", "a-good-password")
    assert accounts.authenticate("who@example.com", "a-good-password") is not None
    assert accounts.authenticate("who@example.com", "nope") is None
    assert accounts.authenticate("missing@example.com", "a-good-password") is None


def test_disabling_a_user_blocks_login_and_revokes_sessions():
    user = accounts.create_user("gone@example.com", "a-good-password")
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at)"
        " VALUES ('t', ?, '2026-01-01', '2099-01-01', '2026-01-01')",
        (user["id"],),
    )
    accounts.set_status(user["id"], "disabled")
    assert accounts.authenticate("gone@example.com", "a-good-password") is None
    assert db.query("SELECT * FROM sessions WHERE user_id = ?", (user["id"],)) == []


def test_the_local_account_is_created_once():
    first = accounts.ensure_local_user()
    second = accounts.ensure_local_user()
    assert first["id"] == second["id"] == accounts.LOCAL_USER_ID
    assert first["is_admin"] is True
    assert len([u for u in accounts.list_users() if u["id"] == accounts.LOCAL_USER_ID]) == 1


# ---------------------------------------------------------------- principal
def test_single_user_is_the_default_mode():
    assert auth.auth_mode() == auth.SINGLE_USER
    assert auth.single_user_mode() is True
    assert auth.current_principal().user_id == accounts.LOCAL_USER_ID


def test_multi_user_fails_closed_without_a_principal(monkeypatch):
    """The mode must never silently fall back to an ambient account."""
    monkeypatch.setenv("AUTH_MODE", "multi_user")
    assert auth.single_user_mode() is False
    with pytest.raises(auth.AuthError):
        auth.current_principal()


def test_an_established_principal_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")
    other = auth.Principal(user_id="u_someone")
    with auth.principal_scope(other):
        assert auth.current_principal() is other
    with pytest.raises(auth.AuthError):
        auth.current_principal()


def test_an_unrecognised_auth_mode_falls_back_to_single_user(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "nonsense")
    assert auth.auth_mode() == auth.SINGLE_USER
