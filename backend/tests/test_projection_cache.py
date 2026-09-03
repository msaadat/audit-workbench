"""A projection is drawn once per state of the workspace, by one caller."""

from __future__ import annotations

import os
import threading
import time

import pytest

from app import projection_cache


@pytest.fixture(autouse=True)
def _clean():
    projection_cache.clear()
    yield
    projection_cache.clear()


def _write(path, text):
    # A replace, the way ``write_json_atomic`` writes: a new file under the
    # old name, with its own size and time.
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def test_the_same_workspace_is_drawn_once(tmp_path):
    (tmp_path / "workspace.json").write_text('{"revision": 1}', encoding="utf-8")
    calls = {"n": 0}

    def draw():
        calls["n"] += 1
        return {"drawn": calls["n"]}

    first = projection_cache.cached(tmp_path, "record", draw)
    second = projection_cache.cached(tmp_path, "record", draw)

    assert first is second
    assert calls["n"] == 1


def test_a_changed_file_anywhere_under_the_root_redraws(tmp_path):
    (tmp_path / "DocTests").mkdir()
    _write(tmp_path / "DocTests" / "DT-1.json", '{"status": "draft"}')
    calls = {"n": 0}

    def draw():
        calls["n"] += 1
        return calls["n"]

    assert projection_cache.cached(tmp_path, "record", draw) == 1
    # The replaced file is a different size, which signs even when the clock
    # has not moved between the two writes.
    _write(tmp_path / "DocTests" / "DT-1.json", '{"status": "completed"}')

    assert projection_cache.cached(tmp_path, "record", draw) == 2


def test_a_new_file_and_a_removed_file_both_redraw(tmp_path):
    calls = {"n": 0}

    def draw():
        calls["n"] += 1
        return calls["n"]

    assert projection_cache.cached(tmp_path, "record", draw) == 1
    _write(tmp_path / "new.json", "{}")
    assert projection_cache.cached(tmp_path, "record", draw) == 2
    os.remove(tmp_path / "new.json")
    assert projection_cache.cached(tmp_path, "record", draw) == 3


def test_projections_are_keyed_by_name_as_well_as_root(tmp_path):
    record = projection_cache.cached(tmp_path, "record", lambda: "record")
    status = projection_cache.cached(tmp_path, "status", lambda: "status")

    assert (record, status) == ("record", "status")


def test_the_telemetry_database_does_not_count(tmp_path):
    calls = {"n": 0}

    def draw():
        calls["n"] += 1
        return calls["n"]

    assert projection_cache.cached(tmp_path, "record", draw) == 1
    _write(tmp_path / "telemetry.db-wal", "x" * 64)

    assert projection_cache.cached(tmp_path, "record", draw) == 1


def test_concurrent_misses_share_one_drawing(tmp_path):
    """Three requests at once are one computation, not three slow ones."""
    started = threading.Barrier(3)
    calls = {"n": 0}
    lock = threading.Lock()

    def draw():
        with lock:
            calls["n"] += 1
        time.sleep(0.05)
        return "drawn"

    results = []

    def request():
        started.wait()
        results.append(projection_cache.cached(tmp_path, "record", draw))

    threads = [threading.Thread(target=request) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["drawn"] * 3
    assert calls["n"] == 1


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(projection_cache, "MAX_ENTRIES", 2)
    for name in ("a", "b", "c"):
        projection_cache.cached(tmp_path, name, lambda name=name: name)
    calls = {"n": 0}

    def draw():
        calls["n"] += 1
        return "a"

    # "a" was evicted; "c" was not.
    projection_cache.cached(tmp_path, "a", draw)
    projection_cache.cached(tmp_path, "c", draw)

    assert calls["n"] == 1
