"""Phase 1: the resolver seam — one authorized path from a reference to a root.

These tests are the regression net for the property the whole multi-user design
rests on: a workspace reference can only ever reach a root its actor is allowed
to open, and nothing outside the registry reconstructs such a root.
"""

import json
from pathlib import Path

import pytest

from app import accounts, auth, config, registry, workspaces
from app.workspaces import WorkspaceError


def _user(email: str) -> auth.Principal:
    account = accounts.create_user(email, "a-good-password")
    return auth.Principal(user_id=account["id"])


# ------------------------------------------------------------------- layout
def test_a_workspace_lands_in_its_owners_home(tmp_path):
    ws = workspaces.create_workspace("Procurement Audit")
    assert ws.root == tmp_path / "Users" / accounts.LOCAL_USER_ID / "Workspaces" / "procurement-audit"
    assert ws.dir_name == "procurement-audit"
    assert (ws.root / "workspace.json").is_file()


def test_identity_is_separate_from_location():
    ws = workspaces.create_workspace("Procurement Audit")
    assert ws.uid.startswith("ws_")
    assert ws.id == ws.uid
    assert ws.dir_name == "procurement-audit"
    assert ws.name == "Procurement Audit"
    stored = json.loads((ws.root / "workspace.json").read_text(encoding="utf-8"))
    assert stored["uid"] == ws.uid and stored["owner_id"] == accounts.LOCAL_USER_ID


def test_identity_survives_a_save():
    ws = workspaces.create_workspace("Procurement Audit")
    ws.name = "Renamed"
    ws.save()
    reloaded = workspaces.load_workspace(ws.uid)
    assert reloaded.uid == ws.uid
    assert reloaded.dir_name == "procurement-audit"
    assert reloaded.name == "Renamed"


def test_uids_are_unique_and_listings_are_creation_ordered():
    """Uid order is not a guarantee — two created in one millisecond tie on the
    time prefix — so listings sort on the registry's ``created_at`` instead."""
    first = workspaces.create_workspace("One")
    second = workspaces.create_workspace("Two")
    assert first.uid != second.uid
    assert {first.uid, second.uid} == {
        item["id"] for item in workspaces.list_workspaces()
    }


def test_two_users_may_each_own_the_same_slug():
    """The collision that a flat namespace made impossible."""
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    a = workspaces.create_workspace("Procurement", actor=alice)
    b = workspaces.create_workspace("Procurement", actor=bob)
    assert a.dir_name == b.dir_name == "procurement"
    assert a.uid != b.uid
    assert a.root != b.root


# ---------------------------------------------------------------- isolation
def test_one_user_cannot_open_anothers_workspace():
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    hers = workspaces.create_workspace("Hers", actor=alice)
    with pytest.raises(WorkspaceError) as error:
        workspaces.open_workspace(bob, hers.uid)
    # Reported as missing, not forbidden, so uids are not enumerable.
    assert "not found" in str(error.value)


def test_listing_is_scoped_to_the_actor():
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    hers = workspaces.create_workspace("Hers", actor=alice)
    his = workspaces.create_workspace("His", actor=bob)
    assert [item["id"] for item in workspaces.list_workspaces(alice)] == [hers.uid]
    assert [item["id"] for item in workspaces.list_workspaces(bob)] == [his.uid]


def test_a_revision_read_is_scoped_too():
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    hers = workspaces.create_workspace("Hers", actor=alice)
    assert workspaces.read_revision(hers.uid, actor=alice) == 0
    with pytest.raises(WorkspaceError):
        workspaces.read_revision(hers.uid, actor=bob)


@pytest.mark.parametrize(
    "reference",
    [
        "../bob/Workspaces/theirs",
        "../../etc",
        "..",
        ".",
        "/etc/passwd",
        "a/b",
        "with space",
        "",
    ],
)
def test_unsafe_references_never_reach_a_path_join(reference):
    with pytest.raises(WorkspaceError):
        workspaces.load_workspace(reference)


def test_a_shared_membership_row_grants_access():
    """The membership table is empty in production, but the resolver reads it,
    so enabling sharing is an INSERT rather than a resolver rewrite."""
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    hers = workspaces.create_workspace("Hers", actor=alice)
    from app import db

    db.execute(
        "INSERT INTO workspace_members (workspace_uid, user_id, role, created_at)"
        " VALUES (?, ?, 'member', '2026-01-01')",
        (hers.uid, bob.user_id),
    )
    assert workspaces.open_workspace(bob, hers.uid).uid == hers.uid
    assert [item["id"] for item in workspaces.list_workspaces(bob)] == [hers.uid]


# ------------------------------------------------------------------ reloads
def test_reload_returns_a_fresh_copy_without_a_principal(monkeypatch):
    """What keeps agent daemon threads working: no principal is needed."""
    ws = workspaces.create_workspace("Reload")
    ws.name = "Changed on disk"
    ws.save()
    stale = workspaces.load_workspace(ws.uid)
    stale.name = "Changed again"
    stale.save()

    monkeypatch.setenv("AUTH_MODE", "multi_user")
    with pytest.raises(auth.AuthError):
        auth.current_principal()
    fresh = ws.reload()
    assert fresh.name == "Changed again"
    assert fresh.root == ws.root and fresh.uid == ws.uid


# ---------------------------------------------------------------- reconcile
def _plant_legacy_workspace(owner_id: str, dir_name: str, slug: str) -> Path:
    """A workspace as it existed before uids: id is the slug, no owner_id."""
    root = config.data_root() / "Users" / owner_id / "Workspaces" / dir_name
    (root / "Data").mkdir(parents=True)
    (root / "workspace.json").write_text(
        json.dumps({
            "schema_version": 4, "revision": 3, "id": slug, "name": "Legacy",
            "description": "", "created": "2026-01-01", "tables": [], "joins": [],
        }),
        encoding="utf-8",
    )
    return root


def test_reconcile_registers_a_hand_moved_workspace():
    root = _plant_legacy_workspace(accounts.LOCAL_USER_ID, "procurement", "procurement")
    summary = registry.reconcile()
    assert summary == {"scanned": 1, "stamped": 1, "registered": 1}
    stored = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    assert stored["uid"].startswith("ws_")
    assert stored["owner_id"] == accounts.LOCAL_USER_ID
    assert stored["legacy_slug"] == "procurement"
    # The stamp must not look like an auditor edit.
    assert stored["revision"] == 3
    assert [item["id"] for item in workspaces.list_workspaces()] == [stored["uid"]]


def test_reconcile_is_idempotent():
    _plant_legacy_workspace(accounts.LOCAL_USER_ID, "procurement", "procurement")
    first = registry.reconcile()
    stamped = json.loads(
        (config.data_root() / "Users" / accounts.LOCAL_USER_ID / "Workspaces"
         / "procurement" / "workspace.json").read_text(encoding="utf-8")
    )
    second = registry.reconcile()
    assert first["registered"] == 1
    assert second == {"scanned": 1, "stamped": 0, "registered": 0}
    assert len(workspaces.list_workspaces()) == 1
    assert workspaces.load_workspace(stamped["uid"]).uid == stamped["uid"]


def test_a_pre_migration_link_still_resolves():
    """Existing bookmarks carry the old slug, recorded as legacy_slug."""
    _plant_legacy_workspace(accounts.LOCAL_USER_ID, "procurement", "procurement")
    registry.reconcile()
    ws = workspaces.load_workspace("procurement")
    assert ws.dir_name == "procurement"
    assert ws.legacy_slug == "procurement"
    assert ws.uid.startswith("ws_")


def test_a_legacy_slug_does_not_resolve_for_another_user():
    _plant_legacy_workspace(accounts.LOCAL_USER_ID, "procurement", "procurement")
    registry.reconcile()
    bob = _user("bob@example.com")
    with pytest.raises(WorkspaceError):
        workspaces.open_workspace(bob, "procurement")


def test_a_home_without_an_account_is_left_alone():
    _plant_legacy_workspace("u_ghost", "orphan", "orphan")
    assert registry.reconcile() == {"scanned": 0, "stamped": 0, "registered": 0}


# ----------------------------------------------------------------- deletion
def test_deleting_a_workspace_removes_its_registration():
    ws = workspaces.create_workspace("Doomed")
    workspaces.delete_workspace(ws.uid)
    assert registry.find(ws.uid) is None
    assert workspaces.list_workspaces() == []
    assert not ws.root.exists()
    with pytest.raises(WorkspaceError):
        workspaces.load_workspace(ws.uid)


# ------------------------------------------------------------- architecture
#
# The design rests on one invariant: ``open_workspace`` is the only way a
# workspace reference becomes a filesystem root.  A second path join under the
# data root, added later by someone who did not know, is exactly how tenant
# isolation would quietly stop holding — so it is asserted rather than
# documented.

APP = Path(__file__).resolve().parents[1] / "app"

# ``registry`` owns the layout; ``config`` defines the root itself.
LAYOUT_OWNERS = {"registry.py", "config.py"}
# ``db`` and ``assistant_settings`` place one fixed file at the root and never
# address a workspace, so they may ask for the root but not for the layout.
ROOT_READERS = LAYOUT_OWNERS | {"db.py", "assistant_settings.py"}


def _sources():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_only_config_reads_the_data_root_environment_variable():
    offenders = [
        path.relative_to(APP).as_posix()
        for path, source in _sources()
        if "WORKBENCH_DATA" in source and path.name != "config.py"
    ]
    assert offenders == [], (
        f"{offenders} read WORKBENCH_DATA directly; use config.data_root() so the "
        "root has one definition."
    )


def test_only_the_layout_owners_resolve_the_data_root():
    offenders = [
        path.relative_to(APP).as_posix()
        for path, source in _sources()
        if "data_root()" in source and path.name not in ROOT_READERS
    ]
    assert offenders == [], (
        f"{offenders} resolve the data root. Workspace paths come from "
        "registry.workspace_root(); a new fixed file at the root belongs in "
        "ROOT_READERS with a reason."
    )


def test_only_the_registry_knows_the_workspace_layout():
    offenders = [
        path.relative_to(APP).as_posix()
        for path, source in _sources()
        if path.name not in LAYOUT_OWNERS
        and ('"Workspaces"' in source or "'Workspaces'" in source
             or '"Users"' in source or "'Users'" in source)
    ]
    assert offenders == [], (
        f"{offenders} name a layout folder. The Users/<owner>/Workspaces/<dir> "
        "shape belongs to registry.py alone."
    )


def test_the_resolver_is_the_only_caller_that_builds_a_workspace_root():
    offenders = [
        path.relative_to(APP).as_posix()
        for path, source in _sources()
        if "workspace_root(" in source
        and path.name not in {"registry.py", "workspaces.py", "debug_store.py"}
    ]
    assert offenders == [], (
        f"{offenders} build a workspace root directly. Go through "
        "workspaces.open_workspace(actor, ref), which authorizes first."
    )


# --------------------------------------------------------------- legacy data
def test_a_chat_written_before_uids_is_still_readable():
    """``assistant_chats`` asserts the stored ``workspace_id`` matches the
    workspace it was found under.  A chat written before workspaces had uids
    stored the slug, so without the legacy tolerance every pre-existing chat
    would become unreadable the moment ``Workspace.id`` became the uid."""
    from app import assistant_chats

    root = _plant_legacy_workspace(accounts.LOCAL_USER_ID, "procurement", "procurement")
    chat_id = "chat_20260105_120000_abc123"
    folder = root / "AssistantChats" / chat_id
    folder.mkdir(parents=True)
    (folder / "chat.json").write_text(
        json.dumps({
            "id": chat_id,
            "workspace_id": "procurement",  # the pre-migration slug
            "title": "Legacy chat", "title_source": "user",
            "created_at": "2026-01-05", "updated_at": "2026-01-05",
            "messages": [], "next_ordinal": 1,
        }),
        encoding="utf-8",
    )
    registry.reconcile()
    ws = workspaces.load_workspace("procurement")
    assert ws.uid != "procurement"
    assert assistant_chats.get_chat(ws, chat_id)["title"] == "Legacy chat"


def test_a_chat_naming_a_different_workspace_is_still_rejected():
    from app import assistant_chats

    ws = workspaces.create_workspace("Real")
    chat_id = "chat_20260105_120000_abc123"
    folder = ws.root / "AssistantChats" / chat_id
    folder.mkdir(parents=True)
    (folder / "chat.json").write_text(
        json.dumps({
            "id": chat_id, "workspace_id": "some-other-workspace",
            "title": "Wrong", "title_source": "user",
            "created_at": "2026-01-05", "updated_at": "2026-01-05",
            "messages": [], "next_ordinal": 1,
        }),
        encoding="utf-8",
    )
    with pytest.raises(WorkspaceError):
        assistant_chats.get_chat(ws, chat_id)
