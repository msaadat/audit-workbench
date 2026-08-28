"""Phase 4: the failures that only exist because the box is shared.

Noisy neighbours, unbounded resources, and code paths whose safety argument was
"it runs on your own laptop".
"""

import os

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, loader, sandbox, uploads, usage, workspaces
from app.agent import runner
from app.main import create_app
from app.workspaces import WorkspaceError


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")


def _user(email: str) -> auth.Principal:
    account = accounts.create_user(email, "a-good-password")
    return auth.Principal(user_id=account["id"])


# ------------------------------------------------------------------- sandbox
#
# Phase 4 closed this surface outright; Phase 5 reopened it behind real
# isolation. What survives from Phase 4 is the rule that the decision is made
# at the choke point, and that a server with no way to isolate still refuses.


def test_python_runs_freely_on_a_single_user_install():
    """The local-first product is unchanged: this is the auditor's own machine."""
    assert sandbox.isolation_mode() == sandbox.MODE_INPROCESS
    result, _ = sandbox.run("result = df", {"df": pl.DataFrame({"a": [1]})})
    assert result.height == 1


def test_a_shared_server_without_isolation_still_refuses(multi_user, monkeypatch):
    """A bare subprocess is not a boundary: it runs as the same OS user and can
    read the ``.env`` the parent's environment was scrubbed of."""
    monkeypatch.setenv("WORKBENCH_BWRAP", "/nonexistent/bwrap")
    assert sandbox.isolation_mode() == sandbox.MODE_SUBPROCESS
    assert sandbox.execution_allowed() is False
    with pytest.raises(sandbox.SandboxError, match="disabled on this server"):
        sandbox.run("result = df", {"df": pl.DataFrame({"a": [1]})})


def test_the_gate_is_at_the_choke_point_not_the_route(multi_user, monkeypatch):
    """Data tests, dashboard tiles and agent analyses all execute through
    ``sandbox.run``; gating only the HTTP route would leave those open."""
    import inspect

    from app.routes import assistant_routes

    source = inspect.getsource(assistant_routes.run_python)
    assert "single_user_mode" not in source
    monkeypatch.setenv("WORKBENCH_BWRAP", "/nonexistent/bwrap")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run("result = df", {"df": pl.DataFrame({"a": [1]})})


def test_an_operator_can_opt_back_in_deliberately(multi_user, monkeypatch):
    monkeypatch.setenv("WORKBENCH_BWRAP", "/nonexistent/bwrap")
    monkeypatch.setenv(sandbox.ALLOW_ENV_VAR, "1")
    monkeypatch.setenv(sandbox.MODE_ENV_VAR, sandbox.MODE_INPROCESS)
    assert sandbox.execution_allowed() is True
    result, _ = sandbox.run("result = df", {"df": pl.DataFrame({"a": [1]})})
    assert result.height == 1


# --------------------------------------------------------------- concurrency
class _FakeHandle:
    def __init__(self, workspace_id, owner_id):
        self.workspace_id = workspace_id
        self.owner_id = owner_id


def test_one_users_run_no_longer_blocks_everyone(monkeypatch):
    """The old process-wide cap of one meant one auditor stalled the server."""
    monkeypatch.delenv("AGENT_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("AGENT_MAX_CONCURRENT_PER_USER", raising=False)
    live = [_FakeHandle("ws_a", "u_alice")]

    assert runner._admission_error(live, "u_alice") is not None
    assert runner._admission_error(live, "u_bob") is None


def test_a_user_is_still_limited_to_one_run_at_a_time(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_CONCURRENT_PER_USER", raising=False)
    live = [_FakeHandle("ws_a", "u_alice")]
    refusal = runner._admission_error(live, "u_alice")
    assert refusal and "already have an agent run" in refusal


def test_the_global_ceiling_still_applies(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT", "2")
    live = [_FakeHandle("ws_a", "u_alice"), _FakeHandle("ws_b", "u_bob")]
    refusal = runner._admission_error(live, "u_carol")
    assert refusal and "as many agent runs" in refusal


def test_the_per_user_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_PER_USER", "2")
    live = [_FakeHandle("ws_a", "u_alice")]
    assert runner._admission_error(live, "u_alice") is None


# --------------------------------------------------------------- frame cache
def _frame(rows: int) -> pl.DataFrame:
    return pl.DataFrame({"value": list(range(rows)), "label": ["x" * 64] * rows})


def test_the_frame_cache_evicts_least_recently_used(tmp_path, monkeypatch):
    """Unbounded was survivable for one auditor's frames; several auditors
    holding 100MB+ populations at once is the dominant memory risk."""
    loader.clear_cache()
    paths = []
    for index in range(4):
        path = tmp_path / f"table{index}.csv"
        _frame(2000).write_csv(path)
        paths.append(path)

    one = loader.read_table(paths[0])
    monkeypatch.setenv("WORKBENCH_FRAME_CACHE_MB", "1")
    budget = int(one.estimated_size() * 2.5)
    for path in paths:
        loader.read_table(path)
    loader._evict_to_budget(budget)

    assert loader.cache_bytes() <= budget
    # The frame read most recently survives; the oldest is gone.
    assert loader._signature(paths[-1]) in loader._cache
    assert loader._signature(paths[0]) not in loader._cache


def test_a_single_oversized_frame_is_still_cached(tmp_path):
    """Refusing to cache it would not free memory the caller already holds,
    and would re-parse the file on every request."""
    loader.clear_cache()
    path = tmp_path / "big.csv"
    _frame(2000).write_csv(path)
    loader.read_table(path)
    loader._evict_to_budget(1)
    assert len(loader._cache) == 1


def test_a_reread_refreshes_recency(tmp_path):
    loader.clear_cache()
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    _frame(500).write_csv(first)
    _frame(500).write_csv(second)
    loader.read_table(first)
    loader.read_table(second)
    loader.read_table(first)  # touch
    loader._evict_to_budget(1)
    assert loader._signature(first) in loader._cache


# ------------------------------------------------------------------- uploads
class _Upload:
    def __init__(self, body: bytes, filename="data.csv"):
        self._body = body
        self.filename = filename
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body)
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


@pytest.mark.anyio
async def test_an_upload_within_the_limit_is_read_whole():
    body = b"a" * 4096
    assert await uploads.read_upload(_Upload(body)) == body


@pytest.mark.anyio
async def test_an_oversized_upload_is_refused(monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_UPLOAD_MB", "1")
    with pytest.raises(WorkspaceError, match="larger than"):
        await uploads.read_upload(_Upload(b"a" * (2 * 1024 * 1024)))


def test_the_quota_is_unlimited_unless_configured():
    assert uploads.quota_bytes() is None
    # No configured ceiling means no filesystem walk at all.
    uploads.check_quota("u_nobody", 10 ** 12)


def test_a_quota_refuses_the_write_that_would_exceed_it(monkeypatch):
    alice = _user("alice@example.com")
    ws = workspaces.create_workspace("Big", actor=alice)
    (ws.data_dir / "payload.bin").write_bytes(b"x" * (3 * 1024 * 1024))

    monkeypatch.setenv("WORKBENCH_USER_QUOTA_MB", "4")
    uploads.check_quota(alice.user_id, 512 * 1024)
    with pytest.raises(WorkspaceError, match="storage limit"):
        uploads.check_quota(alice.user_id, 4 * 1024 * 1024)


def test_a_quota_is_measured_per_user(monkeypatch):
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    ws = workspaces.create_workspace("Hers", actor=alice)
    (ws.data_dir / "payload.bin").write_bytes(b"x" * (3 * 1024 * 1024))

    monkeypatch.setenv("WORKBENCH_USER_QUOTA_MB", "4")
    with pytest.raises(WorkspaceError):
        uploads.check_quota(alice.user_id, 4 * 1024 * 1024)
    # Bob's allowance is untouched by Alice's data.
    uploads.check_quota(bob.user_id, 3 * 1024 * 1024)


# --------------------------------------------------------------------- usage
def test_model_usage_is_attributed_to_the_workspace_owner():
    alice = _user("alice@example.com")
    ws = workspaces.create_workspace("Hers", actor=alice)

    usage.record(workspace_uid=ws.uid, run_id="run_1", provider="mistral",
                 model="m", prompt_tokens=1200, completion_tokens=300)

    totals = usage.totals_for(alice.user_id)
    assert totals == {"calls": 1, "turns": 1, "prompt_tokens": 1200,
                      "completion_tokens": 300}


def test_usage_for_an_unknown_workspace_is_dropped_not_raised():
    """Accounting must never fail a call the provider already answered."""
    usage.record(workspace_uid="ws_does_not_exist", prompt_tokens=10)
    assert usage.totals_by_user() == []


def test_usage_separates_users():
    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    hers = workspaces.create_workspace("Hers", actor=alice)
    his = workspaces.create_workspace("His", actor=bob)

    usage.record(workspace_uid=hers.uid, prompt_tokens=100)
    usage.record(workspace_uid=his.uid, prompt_tokens=900)

    assert usage.totals_for(alice.user_id)["prompt_tokens"] == 100
    assert usage.totals_for(bob.user_id)["prompt_tokens"] == 900


# ------------------------------------------------------- cross-tenant reads
def test_the_cost_estimate_never_measures_another_users_engagements():
    from app import engagement

    alice, bob = _user("alice@example.com"), _user("bob@example.com")
    workspaces.create_workspace("Hers", actor=alice)
    workspaces.create_workspace("His", actor=bob)

    # Neither has history, but the scan itself must not reach across.
    assert engagement._comparable_runs(alice) == []
    assert engagement.cost_estimate(alice)["state"] == "insufficient_history"


# ------------------------------------------------------ admin-only settings
def test_assistant_settings_are_admin_only(multi_user):
    client = TestClient(create_app())
    accounts.create_user("plain@example.com", "a-good-password")
    client.post("/api/auth/login",
                json={"email": "plain@example.com", "password": "a-good-password"})

    refused = client.patch("/api/assistant/settings", json={"provider": "mistral"})
    assert refused.status_code == 403
    # Reading stays open: the UI must know whether a model is configured.
    assert client.get("/api/assistant/status").status_code == 200


def test_an_administrator_may_change_assistant_settings(multi_user, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "a-key")
    client = TestClient(create_app())
    accounts.create_user("boss@example.com", "a-good-password", is_admin=True)
    client.post("/api/auth/login",
                json={"email": "boss@example.com", "password": "a-good-password"})

    assert client.patch("/api/assistant/settings",
                        json={"provider": "mistral",
                              "model": "mistral-small-latest"}).status_code == 200
