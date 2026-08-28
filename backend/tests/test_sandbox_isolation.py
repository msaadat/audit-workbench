"""Phase 5: the sandbox as a boundary rather than a convention.

``sandbox.py`` used to say outright that it was a guard-rail and not a security
boundary, on the grounds that it ran on the auditor's own machine. These tests
are about what replaced that argument: a snippet that escapes the AST guard
still cannot reach another auditor's data, the provider credentials, or the
network — and cannot take the server down by asking for too much.

The escape is simulated directly rather than pursued through the guard: the
question is what the *child* can do, not whether the guard can be beaten.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import polars as pl
import pytest

from app import config, sandbox

requires_bwrap = pytest.mark.skipif(
    sandbox.bubblewrap_path() is None,
    reason="bubblewrap is not installed on this machine",
)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")
    monkeypatch.setenv(sandbox.MODE_ENV_VAR, sandbox.MODE_CONTAINER)


def _run_in_jail(script: str) -> dict:
    """Run arbitrary Python inside the jail, bypassing the AST guard entirely.

    This is the escaped-snippet scenario: the guard has already failed, and what
    is left is whatever the jail itself denies.
    """
    exchange = Path(tempfile.mkdtemp(prefix="jail-probe-"))
    try:
        (exchange / "probe.py").write_text(script, encoding="utf-8")
        command = sandbox._bwrap_command(exchange, sandbox._interpreter())
        command = command[:-3] + [sandbox._interpreter(), str(exchange / "probe.py")]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
        assert completed.stdout.strip(), completed.stderr[-500:]
        return json.loads(completed.stdout)
    finally:
        shutil.rmtree(exchange, ignore_errors=True)


# ------------------------------------------------------------------ the jail
@requires_bwrap
def test_an_escaped_snippet_cannot_read_the_provider_credentials(isolated):
    """The keys live in this process's environment and in a dotenv file. The
    child is given a new environment, and the file is not mounted at all."""
    report = _run_in_jail(
        "import json, os\n"
        "print(json.dumps({'env': sorted(os.environ), "
        f"'dotenv': os.path.exists({str(config.PROJECT_ROOT / '.env')!r})}}))"
    )
    assert report["dotenv"] is False
    assert not [key for key in report["env"] if "KEY" in key or "TOKEN" in key]
    assert set(report["env"]) <= {
        "HOME", "LANG", "PATH", "PWD", "POLARS_MAX_THREADS",
        "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    }


@requires_bwrap
def test_an_escaped_snippet_cannot_reach_another_auditors_workspaces(isolated, tmp_path):
    other = tmp_path / "Users" / "u_someone" / "Workspaces" / "theirs"
    other.mkdir(parents=True)
    (other / "workspace.json").write_text('{"secret": "theirs"}', encoding="utf-8")

    report = _run_in_jail(
        "import json, os\n"
        f"print(json.dumps({{'data_root': os.path.exists({str(tmp_path)!r}), "
        f"'their_file': os.path.exists({str(other / 'workspace.json')!r})}}))"
    )
    assert report == {"data_root": False, "their_file": False}


@requires_bwrap
def test_the_control_plane_database_is_not_mounted(isolated):
    """Password hashes and live sessions live here."""
    report = _run_in_jail(
        "import json, os\n"
        f"print(json.dumps({{'db': os.path.exists({str(config.data_root() / 'workbench.db')!r})}}))"
    )
    assert report["db"] is False


@requires_bwrap
def test_an_escaped_snippet_has_no_network(isolated):
    report = _run_in_jail(
        "import json, socket\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 53))\n"
        "    out = 'reachable'\n"
        "except Exception as error:\n"
        "    out = type(error).__name__\n"
        "print(json.dumps({'network': out}))"
    )
    assert report["network"] != "reachable"


# -------------------------------------------------------------- correctness
@requires_bwrap
def test_a_snippet_returns_its_result_and_output_through_the_jail(isolated):
    frames = {"transactions": pl.DataFrame({"amount": [1, 2, 3]})}
    result, stdout = sandbox.run(
        "print('working')\nresult = transactions.select(pl.col('amount').sum())", frames
    )
    assert result.item() == 6
    assert "working" in stdout


@requires_bwrap
def test_a_runtime_error_still_reads_like_one(isolated):
    with pytest.raises(sandbox.SandboxError, match="ColumnNotFound|nope"):
        sandbox.run("result = df.select(pl.col('nope'))", {"df": pl.DataFrame({"a": [1]})})


@requires_bwrap
def test_the_static_guard_still_runs_before_anything_is_copied(isolated):
    """Rejecting an unsafe snippet must not cost a process spawn."""
    with pytest.raises(sandbox.SandboxError, match="Imports are not allowed"):
        sandbox.run("import os\nresult = df", {"df": pl.DataFrame({"a": [1]})})


@requires_bwrap
def test_frames_survive_their_types_across_the_boundary(isolated):
    frames = {
        "t": pl.DataFrame({
            "n": [1, 2], "f": [1.5, 2.5], "s": ["a", "b"], "b": [True, False],
        })
    }
    result, _ = sandbox.run("result = t", frames)
    assert result.schema == frames["t"].schema
    assert result.rows() == frames["t"].rows()


# ------------------------------------------------------------------ limits
@requires_bwrap
def test_a_runaway_allocation_is_stopped_rather_than_taking_the_server_with_it(
    isolated, monkeypatch
):
    monkeypatch.setenv("WORKBENCH_SANDBOX_MEMORY_MB", "256")
    with pytest.raises(sandbox.SandboxError, match="memory|stopped|failed"):
        sandbox.run(
            "result = pl.DataFrame({'x': list(range(200_000_000))})",
            {"df": pl.DataFrame({"a": [1]})},
        )


@requires_bwrap
def test_an_endless_snippet_is_stopped(isolated, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SANDBOX_TIMEOUT_SECONDS", "5")
    with pytest.raises(sandbox.SandboxError, match="longer than|stopped"):
        sandbox.run("while True:\n    pass\nresult = df", {"df": pl.DataFrame({"a": [1]})})


# --------------------------------------------------------- frame narrowing
def test_only_the_frames_a_snippet_names_are_copied():
    """Every frame handed over is serialised, so sending a whole workspace when
    the snippet names one table turns a cheap call into a slow one."""
    available = {
        "transactions": pl.DataFrame({"a": [1]}),
        "customers": pl.DataFrame({"b": [2]}),
        "ledger": pl.DataFrame({"c": [3]}),
    }
    selected = sandbox.referenced_frames("result = transactions.head()", available)
    assert list(selected) == ["transactions"]


def test_a_literal_tables_lookup_is_resolved():
    available = {"a": pl.DataFrame({"x": [1]}), "b": pl.DataFrame({"y": [2]})}
    selected = sandbox.referenced_frames("result = tables['b']", available)
    assert list(selected) == ["b"]


def test_a_dynamic_tables_access_forfeits_the_narrowing():
    """Better a slow call than a confusing KeyError."""
    available = {"a": pl.DataFrame({"x": [1]}), "b": pl.DataFrame({"y": [2]})}
    for code in [
        "result = tables[name]",
        "result = list(tables.values())[0]",
        "for key in tables:\n    result = tables[key]",
    ]:
        assert list(sandbox.referenced_frames(code, available)) == ["a", "b"], code


def test_df_keeps_binding_to_the_first_frame():
    """``df`` is the first frame handed over, so narrowing must not change which
    frame that is."""
    available = {
        "first": pl.DataFrame({"x": [1]}),
        "second": pl.DataFrame({"y": [2]}),
        "third": pl.DataFrame({"z": [3]}),
    }
    selected = sandbox.referenced_frames("result = df.join(third, how='cross')", available)
    assert list(selected) == ["first", "third"]
    assert next(iter(selected)) == "first"


@requires_bwrap
def test_df_binds_to_the_same_frame_isolated_as_it_does_in_process(isolated):
    frames = {
        "alpha": pl.DataFrame({"which": ["alpha"]}),
        "beta": pl.DataFrame({"which": ["beta"]}),
    }
    isolated_result, _ = sandbox.run("result = df", frames)
    local_result, _ = sandbox.execute_locally("result = df", frames)
    assert isolated_result.rows() == local_result.rows() == [("alpha",)]


# ------------------------------------------------------------------- policy
def test_a_single_user_install_stays_in_process(monkeypatch):
    monkeypatch.delenv(sandbox.MODE_ENV_VAR, raising=False)
    assert sandbox.isolation_mode() == sandbox.MODE_INPROCESS


@requires_bwrap
def test_a_shared_server_isolates_by_default(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")
    monkeypatch.delenv(sandbox.MODE_ENV_VAR, raising=False)
    assert sandbox.isolation_mode() == sandbox.MODE_CONTAINER
    assert sandbox.execution_allowed() is True


def test_asking_for_isolation_that_is_unavailable_is_an_error(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "multi_user")
    monkeypatch.setenv(sandbox.MODE_ENV_VAR, sandbox.MODE_CONTAINER)
    monkeypatch.setenv("WORKBENCH_BWRAP", "/nonexistent/bwrap")
    with pytest.raises(sandbox.SandboxError, match="bubblewrap"):
        sandbox.isolation_mode()
    # And the failure closes the surface rather than silently downgrading it.
    assert sandbox.execution_allowed() is False
