"""Phase 4 gates for the bounded Cycle vouch grid and engagement summary."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app import cycle_vouching, doc_tests, rcm_execution
from app.routes import doc_test_routes
from app.workspaces import WorkspaceError
from test_cycle_vouching_phase3 import _workspace


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _evaluated_cycle(contract: dict, monkeypatch):
    workspace, test, current = _workspace(contract, monkeypatch)
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    doc_tests.save_test(workspace, evaluated)
    return workspace, doc_tests.load_test(workspace, test["id"]), current


def _assert_grid_contains_no_detail_evidence(value: object) -> None:
    if isinstance(value, dict):
        assert "excerpt" not in value
        assert "raw_value" not in value
        assert "evidence_refs" not in value
        for child in value.values():
            _assert_grid_contains_no_detail_evidence(child)
    elif isinstance(value, list):
        for child in value:
            _assert_grid_contains_no_detail_evidence(child)


def test_grid_paginates_rows_but_counts_every_item(contract, monkeypatch):
    workspace, test, _current = _evaluated_cycle(contract, monkeypatch)
    base = test["items"][0]
    items = []
    for index, label in enumerate(("INV-003", "INV-001", "INV-002"), start=1):
        item = copy.deepcopy(base)
        item.update(id=f"ITEM-{index}", label=label)
        items.append(item)
    test["items"] = items
    test["coverage"] = {
        "population_rows": 20,
        "selected_rows": 3,
        "rows_with_evidence": 3,
        "complete_cycles": 3,
        "missing_role_counts": {},
        "selection_basis": "evidence_linked",
        "assurance_scope": "targeted_evidence_only",
    }

    first = cycle_vouching.grid_projection(test, offset=0, limit=2)
    second = cycle_vouching.grid_projection(test, offset=2, limit=2)
    rollup = doc_tests.result_rollup(test)

    assert [row["label"] for row in first["rows"]] == ["INV-001", "INV-002"]
    assert [row["label"] for row in second["rows"]] == ["INV-003"]
    assert first["page"] == {"offset": 0, "limit": 2, "total": 3}
    assert first["truncated"] is True
    assert second["truncated"] is False
    assert first["assurance_scope"] == "targeted_evidence_only"
    assert first["assurance_label"] == "Targeted evidence - not a sample"
    assert first["coverage"]["selected_rows"] == 3
    assert first["assertion_counts"] == rollup["assertion_counts"]
    for column in first["columns"]:
        expected = {
            verdict: sum(
                item["result_by_assertion"][column["key"]]["verdict"] == verdict
                for item in items
            )
            for verdict in cycle_vouching.ASSERTION_VERDICTS
        }
        assert column["counts"] == expected
    _assert_grid_contains_no_detail_evidence(first)


def test_grid_keeps_every_per_document_result_and_shared_relationship(contract, monkeypatch):
    workspace, test, current = _workspace(contract, monkeypatch)
    assertion = {
        "key": "recorded_total_agrees",
        "label": "Recorded total agrees across applicable documents",
        "left": {"source": "row", "column": "INVOICE_AMOUNT"},
        "right": {
            "source": "roles",
            "roles": ["vendor_invoice", "payment_voucher"],
            "field": {"group": "amounts", "kind": "total", "attribute": "value"},
            "entry_quantifier": "one",
        },
        "operator": "numeric_within",
        "tolerance": {"absolute": 0.01, "percent": 0},
        "role_quantifier": "all",
    }
    test["definition"]["assertions"] = [assertion]
    item = cycle_vouching.materialize_cycle_items(workspace, test)[0]
    item = cycle_vouching.evaluate_cycle_item(
        workspace, test, item, records=current["records"]
    )
    item["shared_record_facts"] = [{
        "role": "payment_voucher",
        "record_id": "REC-PAY",
        "related_item_ids": ["ITEM-OTHER"],
        "reuse_across_items": "allowed",
        "identifier_edge": {
            "identifier_kind": "procure_to_pay.payment_voucher_number",
            "normalized_value": "inv2024004",
        },
    }]
    test["items"] = [item]

    grid = cycle_vouching.grid_projection(test)
    cell = grid["rows"][0]["cells"][assertion["key"]]

    assert cell["comparison_count"] == 2
    assert [(value["role"], value["verdict"]) for value in cell["comparisons"]] == [
        ("vendor_invoice", "match"),
        ("payment_voucher", "match"),
    ]
    shared = grid["rows"][0]["shared_record_facts"][0]
    assert shared["role"] == "payment_voucher"
    assert shared["related_item_ids"] == ["ITEM-OTHER"]
    assert shared["related_item_count"] == 1
    assert shared["related_items_truncated"] is False


def test_grid_scope_is_derived_and_sampled_population_is_distinct(contract, monkeypatch):
    workspace, test, _current = _workspace(
        contract,
        monkeypatch,
        selection={
            "mode": "sample",
            "method": "random",
            "size": 1,
            "seed": 42,
            "assurance_scope": "sampled_population",
        },
    )
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    evaluated["coverage"] = {
        "selection_basis": "evidence_linked",
        "assurance_scope": "targeted_evidence_only",
    }

    grid = cycle_vouching.grid_projection(evaluated)
    rollup = doc_tests.result_rollup(evaluated)

    assert grid["selection_basis"] == "sample"
    assert grid["assurance_scope"] == "sampled_population"
    assert grid["assurance_label"] == "Sampled population"
    assert grid["coverage"]["selection_basis"] == "sample"
    assert grid["coverage"]["assurance_scope"] == "sampled_population"
    assert rollup["assurance_scope"] == "sampled_population"


def test_grid_api_is_read_only_matches_item_projection_and_rejects_stale_results(
    contract, monkeypatch
):
    workspace, test, _current = _evaluated_cycle(contract, monkeypatch)
    path = workspace.root / "DocTests" / f"{test['id']}.json"
    before = path.read_bytes()
    revision = workspace.revision
    detail = asyncio.run(
        doc_test_routes.get_document_test(workspace.id, test["id"])
    )
    grid = asyncio.run(
        doc_test_routes.get_cycle_vouch_grid(
            workspace.id, test["id"], offset=0, limit=1
        )
    )

    item = detail["items"][0]
    assert grid["test_sha1"] == detail["sha1"]
    assert grid["rows"][0]["item_id"] == item["id"]
    assert {
        key: cell["verdict"] for key, cell in grid["rows"][0]["cells"].items()
    } == {
        key: result["verdict"]
        for key, result in item["result_by_assertion"].items()
    }
    assert workspace.revision == revision
    assert path.read_bytes() == before
    assert any(
        route.path.endswith("/doc-tests/{test_id}/grid")
        and "GET" in (route.methods or set())
        for route in doc_test_routes.router.routes
    )
    with pytest.raises(WorkspaceError, match="between 1 and 200"):
        asyncio.run(
            doc_test_routes.get_cycle_vouch_grid(
                workspace.id, test["id"], offset=0, limit=201
            )
        )

    stale = copy.deepcopy(test)
    stale["items"][0]["evaluation"]["definition_sha1"] = "sha1:other-definition"
    monkeypatch.setattr(doc_tests, "load_test", lambda *_args, **_kwargs: stale)
    conflict = asyncio.run(
        doc_test_routes.get_cycle_vouch_grid(workspace.id, test["id"])
    )
    assert conflict.status_code == 409
    assert json.loads(conflict.body)["code"] == "stale_definition"


def test_summary_and_rollup_keep_tests_items_and_assertions_separate(
    contract, monkeypatch
):
    workspace, test, _current = _evaluated_cycle(contract, monkeypatch)
    cycle_item = test["items"][0]
    assertion_keys = list(cycle_item["result_by_assertion"])
    for key in assertion_keys[:2]:
        cycle_item["result_by_assertion"][key]["verdict"] = "mismatch"
    cycle_item["evaluation"]["state"] = "failed"
    cycle_item["disposition"] = {
        "state": "exception",
        "evaluated_definition_sha1": cycle_item["evaluation"]["definition_sha1"],
        "stale": False,
    }
    doc_tests.save_test(workspace, test)
    qa = doc_tests.create_test(workspace, {
        "kind": "qa",
        "title": "Contract question",
        "items": [{"label": "Clause 1", "question": "Is the clause approved?"}],
    })

    projected_cycle = doc_tests.load_test(workspace, test["id"])
    rollup = doc_tests.result_rollup(projected_cycle)
    summary = doc_tests.summary_payload(workspace)

    assert rollup["failed_items"] == 1
    assert rollup["assertion_counts"]["mismatch"] == 2
    assert rollup["exception_items"] == 1
    assert rollup["exceptions"] == 1
    assert summary["test_counts"]["total"] == 2
    assert summary["test_counts"]["cycle_vouch"] == 1
    assert summary["test_counts"]["item_first"] == 1
    assert summary["tested_item_counts"]["failed"] == 1
    assert summary["tested_item_counts"]["exceptions"] == 1
    assert summary["assertion_counts"]["mismatch"] == 2
    assert {
        (entry["entry_type"], entry["test_id"]) for entry in summary["entries"]
    } == {("cycle_test", test["id"]), ("item", qa["id"])}
    cycle_entry = next(
        entry for entry in summary["entries"] if entry["entry_type"] == "cycle_test"
    )
    assert cycle_entry["assurance_scope"] == "targeted_evidence_only"
    assert cycle_entry["assurance_label"] == "Targeted evidence - not a sample"
    assert cycle_entry["coverage"] == rollup["coverage"]
    assert cycle_entry["assertion_counts"] == rollup["assertion_counts"]

    monkeypatch.setattr(
        rcm_execution,
        "_observation",
        lambda *_args, **_kwargs: {"outcome": "exception"},
    )
    _status, exceptions, open_exceptions, executed, _anchors = (
        rcm_execution._rollup_doctest(
            workspace,
            {"id": test["rcm_id"]},
            projected_cycle,
        )
    )
    assert executed == 1
    assert exceptions == 1
    assert open_exceptions == 1
