"""Phase 6 gates for incremental Cycle-vouch assertion authoring."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app import cycle_vouching, doc_tests
from app.agent import action_tools, actions
from app.routes import doc_test_routes
from app.workspace_transactions import ParentConflict
from app.workspaces import WorkspaceConflict, WorkspaceError
from test_cycle_vouching_phase2 import _manifest
from test_cycle_vouching_phase3 import _workspace


FIXTURE = Path(__file__).parent / "fixtures" / "procurement_cycle_phase0.json"


@pytest.fixture
def contract() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _signed_cycle(contract: dict, monkeypatch):
    workspace, test, current = _workspace(contract, monkeypatch)
    workspace.update_rcm(
        test["rcm_id"], {"control_attributes": contract["control_attributes"]}
    )
    manifest = _manifest(contract)
    monkeypatch.setattr(
        cycle_vouching,
        "transaction_evidence_manifest",
        lambda *_args, **_kwargs: copy.deepcopy(manifest),
    )
    evaluated = cycle_vouching.evaluate_cycle_test(workspace, test)
    doc_tests.save_test(workspace, evaluated)
    loaded = doc_tests.load_test(workspace, test["id"])
    signed = doc_tests.update_item(
        workspace, test["id"], loaded["items"][0]["id"], {"state": "confirmed"}
    )
    return workspace, signed, current, manifest


def _new_assertion(contract: dict, key: str = "payment_amount_second_check") -> dict:
    assertion = copy.deepcopy(
        next(
            value
            for value in contract["cycle_test"]["definition"]["assertions"]
            if value["key"] == "invoice_amount_to_payment"
        )
    )
    assertion.update(key=key, label="Payment amount independently agrees")
    return assertion


def test_append_preserves_unaffected_results_and_stales_signed_disposition(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    item_before = copy.deepcopy(signed["items"][0])
    assertion = _new_assertion(contract)

    outcome = doc_tests.append_cycle_assertions(
        workspace,
        signed["id"],
        expected_test_sha1=signed["sha1"],
        assertions=[assertion],
        placement={"after_key": "invoice_amount_to_payment"},
    )
    mutated = outcome["test"]
    item = mutated["items"][0]

    keys = [value["key"] for value in mutated["definition"]["assertions"]]
    assert keys.index(assertion["key"]) == keys.index("invoice_amount_to_payment") + 1
    for key, result in item_before["result_by_assertion"].items():
        assert item["result_by_assertion"][key] == result
    pending = item["result_by_assertion"][assertion["key"]]
    assert pending["verdict"] == "not_run"
    assert pending["stale"] is True
    assert item["evaluation"]["state"] == "stale"
    assert item["evaluation"]["definition_sha1"] == outcome["mutation"][
        "after_definition_sha1"
    ]
    assert item["disposition"] == {
        **item_before["disposition"],
        "stale": True,
    }
    assert item["disposition_history"][-1]["state"] == "confirmed"
    assert item["disposition_history"][-1]["reason"] == (
        "cycle_assertion_definition_changed"
    )
    assert mutated["status"] == "review_required"
    assert outcome["mutation"]["retained_result_count"] == len(
        item_before["result_by_assertion"]
    )
    assert outcome["mutation"]["pending_result_count"] == 1
    assert not doc_tests.item_disposition_current(mutated, item)


def test_changed_key_stales_only_that_result_and_selective_execution_runs_it(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    before = copy.deepcopy(signed["items"][0]["result_by_assertion"])
    changed = copy.deepcopy(
        next(
            value
            for value in signed["definition"]["assertions"]
            if value["key"] == "invoice_amount_to_payment"
        )
    )
    changed["label"] = "Invoice total still agrees to payment"
    outcome = doc_tests.append_cycle_assertions(
        workspace,
        signed["id"],
        expected_test_sha1=signed["sha1"],
        assertions=[changed],
    )
    mutated = outcome["test"]
    pending = mutated["items"][0]["result_by_assertion"]
    assert pending[changed["key"]]["verdict"] == "not_run"
    assert pending[changed["key"]]["display"] == ""
    assert pending[changed["key"]]["comparisons"] == []
    assert pending[changed["key"]]["evidence_refs"] == []
    assert pending[changed["key"]]["result_sha1"] is None
    assert outcome["mutation"]["changed_assertion_keys"] == [changed["key"]]
    for key in set(before) - {changed["key"]}:
        assert pending[key] == before[key]

    calls = 0
    original = cycle_vouching._comparison

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cycle_vouching, "_comparison", counted)
    executed = cycle_vouching.evaluate_cycle_test(workspace, mutated)

    assert calls == 1
    assert executed["items"][0]["result_by_assertion"][changed["key"]][
        "verdict"
    ] == before[changed["key"]]["verdict"]
    for key in set(before) - {changed["key"]}:
        assert executed["items"][0]["result_by_assertion"][key] == before[key]
    assert executed["items"][0]["disposition"]["stale"] is True
    doc_tests.save_test(workspace, executed)
    resigned = doc_tests.update_item(
        workspace,
        executed["id"],
        executed["items"][0]["id"],
        {"state": "confirmed"},
    )
    assert resigned["items"][0]["disposition"]["stale"] is False
    assert resigned["items"][0]["disposition_history"][-1]["state"] == "confirmed"


def test_assertion_mutation_enforces_test_sha_and_workspace_revision_boundary(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    with pytest.raises(ParentConflict) as raised:
        doc_tests.append_cycle_assertions(
            workspace,
            signed["id"],
            expected_test_sha1="stale-test-sha1",
            assertions=[_new_assertion(contract)],
        )
    assert raised.value.parent_ref == f"doctest:{signed['id']}"
    assert raised.value.current_sha1 == signed["sha1"]

    stale_workspace = type(workspace)(workspace.root)
    workspace.update_planning({"context": {"objective": "Revision advanced"}})
    with pytest.raises(WorkspaceConflict) as revision_error:
        doc_tests.append_cycle_assertions(
            stale_workspace,
            signed["id"],
            expected_test_sha1=signed["sha1"],
            assertions=[_new_assertion(contract)],
        )
    assert "revision" in str(revision_error.value).lower()


def test_route_and_registered_agent_action_share_the_typed_service(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    assertion = _new_assertion(contract)

    async def immediate(function, *args, **kwargs):
        # Exercise the handler contract without the repository's separately
        # known pytest/asyncio default-executor shutdown stall.
        return function(*args, **kwargs)

    monkeypatch.setattr(doc_test_routes.asyncio, "to_thread", immediate)
    response = asyncio.run(
        doc_test_routes.append_cycle_assertion(
            workspace.id,
            signed["id"],
            {
                "expected_test_sha1": signed["sha1"],
                "assertion": assertion,
                "placement": {"before_key": "receipt_before_payment"},
            },
        )
    )
    assert response["mutation"]["new_assertion_keys"] == [assertion["key"]]
    assert set(response) == {
        "test_id", "test_sha1", "definition_sha1", "assertion_keys", "mutation"
    }
    assert any(
        route.path.endswith("/doc-tests/{test_id}/assertions")
        and "POST" in (route.methods or set())
        for route in doc_test_routes.router.routes
    )

    workspace = type(workspace)(workspace.root)
    reloaded = doc_tests.load_test(workspace, signed["id"])
    agent_assertion = _new_assertion(contract, "agent_payment_check")
    action = {
        "id": "append-cycle",
        "idempotency_key": "append-cycle-key",
        "type": "append_cycle_assertions",
        "definition_version": 1,
        "target": {"kind": "doctest", "resolved_id": signed["id"]},
        "args": {
            "expected_test_sha1": reloaded["sha1"],
            "assertions": [agent_assertion],
        },
    }
    definition = actions.validate_action(action)
    action["postcondition"] = actions.expected_postcondition(action)
    receipt = definition.executor(workspace, action, {"id": "RUN-6", "mode": "auto"})

    assert receipt["result"]["new_assertion_keys"] == [agent_assertion["key"]]
    assert receipt["result"]["pending_result_count"] == 2
    assert receipt["result_refs"] == [f"doctest:{signed['id']}"]
    assert definition.reconciler(workspace, action) == "already_applied"


def test_agent_authoring_context_is_exact_descriptor_backed_and_row_free(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    result = action_tools.ActionToolSession(workspace, []).dispatch(
        "get_artifact", {"ref": f"doctest:{signed['id']}"}
    )
    assert result["record_truncated"] is False
    record = result["record"]
    assert isinstance(record, dict)
    assert "items" not in record
    assert "coverage" not in record
    assert record["sha1"] == signed["sha1"]
    assert record["definition"]["assertions"] == signed["definition"][
        "assertions"
    ]
    descriptor = record["assertion_authoring"]["pack"]
    assert descriptor["id"] == signed["registry"]["pack_id"]
    assert descriptor["version"] == signed["registry"]["pack_version"]
    assert descriptor["definition_hash"] == signed["registry"][
        "definition_hash"
    ]
    role_kinds = {
        role["record_kind"] for role in signed["definition"]["roles"]
    }
    assert {value["id"] for value in descriptor["record_kinds"]} == role_kinds
    assert record["assertion_authoring"]["operators"]


def test_mutation_rejects_non_cycle_and_broad_or_invalid_payloads(
    contract, monkeypatch
):
    workspace, signed, _current, _manifest_value = _signed_cycle(
        contract, monkeypatch
    )
    with pytest.raises(WorkspaceError, match="Unknown assertion-mutation field"):
        asyncio.run(
            doc_test_routes.append_cycle_assertion(
                workspace.id,
                signed["id"],
                {
                    "expected_test_sha1": signed["sha1"],
                    "assertion": _new_assertion(contract),
                    "definition": {},
                },
            )
        )
    with pytest.raises(WorkspaceError, match="placement applies only"):
        doc_tests.append_cycle_assertions(
            workspace,
            signed["id"],
            expected_test_sha1=signed["sha1"],
            assertions=[
                copy.deepcopy(signed["definition"]["assertions"][0])
            ],
            placement={"before_key": "receipt_before_payment"},
        )
    broad_assertion = _new_assertion(contract)
    broad_assertion["items"] = []
    with pytest.raises(WorkspaceError, match="unsupported field 'items'"):
        doc_tests.append_cycle_assertions(
            workspace,
            signed["id"],
            expected_test_sha1=signed["sha1"],
            assertions=[broad_assertion],
        )

    invalid_action = {
        "type": "append_cycle_assertions",
        "definition_version": 1,
        "target": {"kind": "doctest", "resolved_id": signed["id"]},
        "args": {
            "expected_test_sha1": signed["sha1"],
            "assertions": [_new_assertion(contract)],
            "placement": {
                "before_key": "invoice_amount_to_payment",
                "after_key": "receipt_before_payment",
            },
        },
    }
    with pytest.raises(WorkspaceError, match="exactly one"):
        actions.validate_action(invalid_action)

    qa = doc_tests.create_test(
        workspace,
        {"kind": "qa", "title": "Question", "items": [{"question": "Why?"}]},
    )
    workspace = type(workspace)(workspace.root)
    qa = doc_tests.load_test(workspace, qa["id"])
    with pytest.raises(WorkspaceError, match="only on cycle_vouch"):
        doc_tests.append_cycle_assertions(
            workspace,
            qa["id"],
            expected_test_sha1=qa["sha1"],
            assertions=[_new_assertion(contract)],
        )
