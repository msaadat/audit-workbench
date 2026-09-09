"""Findings consolidation, phase 2: the ``findings.consolidated`` capability.

The worker proposes groups over every draft and the validator holds it to the
overlap table the adapter computed locally; the capability's readiness is
keyed by the basis the suggestion was made against; nothing here changes a
finding.
"""

from __future__ import annotations

import json

import pytest

from app import data_tests, finding_consolidation, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.context import (
    ContextBundle,
    ContextBundleItem,
    ContextRepresentation,
    PRESETS,
    finding_consolidation_scope,
    supplied_size,
    total_supplied_size,
)
from app.agent.context.adapters import finding_overlaps
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.reporting import FindingExecutorTarget
from app.agent.workers import WORKERS, WorkerRequest, WorkerResponseValidationError
from app.agent.workers import reporting as reporting_worker
from app.agent.workers.reporting import validate_consolidation_proposal
from app.workspace_transactions import parent_hashes
from test_finding_coverage import NARRATIVE, _polars_test, _row, _selecting


class _Gateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, user, activity=None, *, attempt=1, **kwargs):
        self.calls.append({"system": system, "user": user, "activity": activity, "attempt": attempt})
        return self.responses.pop(0)


DRAFTS = [
    {"id": "F-A", "title": "Payments before receipt", "severity": "high", "process": "Payment", "tests": [{"id": "DAT-1", "title": "Paid before GRN"}], "entity_keys": ["INVOICE_ID"]},
    {"id": "F-B", "title": "Payment released ahead of goods receipt", "severity": "high", "process": "Payment", "tests": [{"id": "DAT-2", "title": "Payment date vs receipt"}], "entity_keys": ["INVOICE_ID"]},
    {"id": "F-C", "title": "Inactive vendor on PO", "severity": "medium", "process": "Purchase order", "tests": [{"id": "DAT-3", "title": "Vendor status"}], "entity_keys": ["VENDOR_ID"]},
    {"id": "F-D", "title": "Inactive vendor paid", "severity": "medium", "process": "Payment", "tests": [{"id": "DAT-4", "title": "Vendor status at payment"}], "entity_keys": ["VENDOR_ID"]},
]
KEYS = [
    {"finding_id": "F-A", "keys": {"INVOICE_ID": ["INV1", "INV2", "INV3"]}, "ids_supplied": 3, "ids_withheld": 0},
    {"finding_id": "F-B", "keys": {"INVOICE_ID": ["INV1", "INV2", "INV3"]}, "ids_supplied": 3, "ids_withheld": 0},
    {"finding_id": "F-C", "keys": {"VENDOR_ID": ["V9"]}, "ids_supplied": 1, "ids_withheld": 0},
    {"finding_id": "F-D", "keys": {"VENDOR_ID": ["V9"], "INVOICE_ID": ["INV7"]}, "ids_supplied": 2, "ids_withheld": 0},
]
OVERLAPS = {
    "pairs": [
        {"finding_ids": ["F-A", "F-B"], "basis": "entity", "key": "INVOICE_ID", "shared_count": 3, "jaccard": 1.0, "shared_ids": ["INV1", "INV2", "INV3"]},
        {"finding_ids": ["F-C", "F-D"], "basis": "entity", "key": "VENDOR_ID", "shared_count": 1, "jaccard": 1.0, "shared_ids": ["V9"]},
        {"finding_ids": ["F-A", "F-D"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []},
        {"finding_ids": ["F-B", "F-D"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []},
    ],
    "entity_pairs": 2,
    "process_pairs": 2,
}


def _bundle(instruction: str | None = None):
    values = [
        ("draft_findings", f"finding:{draft['id']}", ContextRepresentation("current_artifact"), draft)
        for draft in DRAFTS
    ] + [
        ("finding_exception_keys", f"finding:{keys['finding_id']}:keys", ContextRepresentation("datatest_exception_keys"), keys)
        for keys in KEYS
    ] + [
        ("finding_overlaps", "findings:overlaps", ContextRepresentation("current_artifact"), OVERLAPS),
    ]
    if instruction is not None:
        values.append(("instruction", "instruction:abcdef123456", ContextRepresentation("auditor_instruction"), instruction))
    items = tuple(
        ContextBundleItem(
            source_id=source_id, source_ref=source_ref, representation=representation,
            content=content, supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in values
    )
    return ContextBundle(
        capability_id="findings.consolidated",
        unit_id="finding_consolidation",
        items=items,
        supplied_size=total_supplied_size(item.supplied_size for item in items),
    )


def _request(bundle=None):
    return WorkerRequest(
        worker_id="reporting.finding_consolidation",
        capability_id="findings.consolidated",
        unit_id="finding_consolidation",
        context=bundle if bundle is not None else _bundle(),
        unit_input={"input_sha1": "consolidation-input"},
        activity={"artifact_refs": ["finding:F-A"]},
    )


def _proposal(**overrides):
    value = {
        "groups": [
            {
                "finding_ids": ["F-A", "F-B"],
                "lead_finding_id": "F-A",
                "relation": "same_condition",
                "basis": "entity",
                "proposed_title": "Payments released before goods receipt",
                "root_cause_hypothesis": "The three-way match does not block payment.",
                "rationale": "Both flag INV1, INV2 and INV3.",
            },
            {
                "finding_ids": ["F-C", "F-D"],
                "lead_finding_id": "F-D",
                "relation": "shared_cause",
                "basis": "entity",
                "proposed_title": "Transactions processed for an inactive vendor",
                "root_cause_hypothesis": "Vendor status is not enforced at PO or payment.",
                "rationale": "Both flag vendor V9 at successive stages.",
            },
        ],
        "singletons": [],
    }
    value.update(overrides)
    return value


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
def test_the_worker_reads_its_bundle_and_returns_a_validated_proposal():
    gateway = _Gateway([json.dumps(_proposal())])

    result = WORKERS.execute(_request(), gateway)

    assert [group["relation"] for group in result.proposal["groups"]] == ["same_condition", "shared_cause"]
    # The shared identifiers are attached from the table, not taken from the model.
    assert dict(result.proposal["groups"][0]["shared_entities"]) == {"INVOICE_ID": ("INV1", "INV2", "INV3")} or \
        result.proposal["groups"][0]["shared_entities"]["INVOICE_ID"] == ("INV1", "INV2", "INV3")
    assert gateway.calls[0]["system"] == reporting_worker.CONSOLIDATION_SYSTEM
    sent = json.loads(gateway.calls[0]["user"])
    assert [draft["id"] for draft in sent["DRAFT FINDINGS"]] == ["F-A", "F-B", "F-C", "F-D"]
    assert sent["OVERLAP TABLE"]["entity_pairs"] == 2
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "finding_consolidation"


def test_the_validator_refuses_an_unknown_finding():
    with pytest.raises(WorkerResponseValidationError, match="not a supplied draft"):
        validate_consolidation_proposal(
            _proposal(groups=[{**_proposal()["groups"][0], "finding_ids": ["F-A", "F-Z"]}], singletons=["F-B", "F-C", "F-D"]),
            _request(),
        )


def test_the_validator_refuses_a_singleton_group():
    with pytest.raises(WorkerResponseValidationError, match="at least two"):
        validate_consolidation_proposal(
            _proposal(groups=[{**_proposal()["groups"][0], "finding_ids": ["F-A"]}], singletons=["F-B", "F-C", "F-D"]),
            _request(),
        )


def test_the_validator_refuses_a_same_condition_group_without_entity_overlap():
    # F-A and F-D share a process, not records: they may be a shared cause,
    # never the same condition.
    with pytest.raises(WorkerResponseValidationError, match="same condition"):
        validate_consolidation_proposal(
            _proposal(
                groups=[{**_proposal()["groups"][0], "finding_ids": ["F-A", "F-D"], "lead_finding_id": "F-A"}],
                singletons=["F-B", "F-C"],
            ),
            _request(),
        )


def test_a_process_only_group_must_say_so():
    with pytest.raises(WorkerResponseValidationError, match="share no records"):
        validate_consolidation_proposal(
            _proposal(
                groups=[{
                    "finding_ids": ["F-A", "F-D"], "lead_finding_id": "F-A",
                    "relation": "shared_cause", "basis": "entity",
                    "proposed_title": "x", "root_cause_hypothesis": "y", "rationale": "z",
                }],
                singletons=["F-B", "F-C"],
            ),
            _request(),
        )
    accepted = validate_consolidation_proposal(
        _proposal(
            groups=[{
                "finding_ids": ["F-A", "F-D"], "lead_finding_id": "F-A",
                "relation": "shared_cause", "basis": "process",
                "proposed_title": "x", "root_cause_hypothesis": "y", "rationale": "z",
            }],
            singletons=["F-B", "F-C"],
        ),
        _request(),
    )
    assert accepted["groups"][0]["basis"] == "process"


def test_the_validator_refuses_a_finding_placed_twice_and_one_placed_nowhere():
    with pytest.raises(WorkerResponseValidationError, match="more than once"):
        validate_consolidation_proposal(
            _proposal(groups=[_proposal()["groups"][0], {**_proposal()["groups"][1], "finding_ids": ["F-A", "F-C", "F-D"], "lead_finding_id": "F-D", "basis": "process"}]),
            _request(),
        )
    with pytest.raises(WorkerResponseValidationError, match="missing F-D"):
        validate_consolidation_proposal(
            _proposal(groups=[_proposal()["groups"][0]], singletons=["F-C"]),
            _request(),
        )


def test_every_draft_as_a_singleton_is_a_real_answer():
    gateway = _Gateway([json.dumps({"groups": [], "singletons": ["F-A", "F-B", "F-C", "F-D"]})])

    result = WORKERS.execute(_request(), gateway)

    assert result.proposal["groups"] == ()
    assert result.repaired is False


def test_the_worker_is_told_what_the_auditor_asked_for():
    gateway = _Gateway([json.dumps(_proposal())])

    WORKERS.execute(_request(_bundle(instruction="Do not merge across processes.")), gateway)

    assert json.loads(gateway.calls[0]["user"])["auditor_instruction"] == "Do not merge across processes."


def test_the_preset_admits_keys_under_their_own_door_and_never_rows():
    spec = PRESETS.compile("reporting.finding_consolidation")
    privacy = spec.privacy
    assert privacy.allow_datatest_exception_keys is True
    assert privacy.allow_datatest_exception_rows is False
    assert privacy.allow_table_rows is False
    kinds = {rep.kind for source in spec.sources for rep in source.representations}
    assert "datatest_exception_keys" in kinds
    assert "datatest_exception_rows" not in kinds


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
def _draft(**overrides):
    value = {"title": "Duplicate invoice processing", "severity": "medium", "narrative": NARRATIVE, "cause_pending": False}
    value.update(overrides)
    return value


def _commit_finding(ws, observation, run_id="run-1", title="Duplicate invoice processing"):
    request = ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": _draft(title=title)},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"observation:{observation['id']}"]),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )
    target = FindingExecutorTarget(ws, run_id, observation["id"])
    EXECUTORS.execute(request, target)
    return target.workspace


def _two_drafts(workspace_with_data):
    """Two findings on different rows whose tests flag overlapping invoices."""
    ws = workspace_with_data
    left_row = _row(ws, risk="Paid before receipt")
    right_row = _row(ws, risk="Paid without approval")
    _polars_test(ws, left_row, title="Left", code=_selecting(1001, 1003, 1005))
    _polars_test(ws, right_row, title="Right", code=_selecting(1001, 1003))
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    # Titled after the test each observation came from, so "Left finding" is
    # always the finding on the Left test whatever the random ids sort to.
    test_titles = {str(item["id"]): str(item["title"]) for item in ws.data_tests}
    for observation in sorted(ws.observations, key=lambda item: test_titles[item["test_id"]]):
        ws = _commit_finding(ws, observation, title=f"{test_titles[observation['test_id']]} finding")
    return workspaces.load_workspace(ws.id)


def test_the_adapter_supplies_keys_only_and_computes_the_overlap_table(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    assert len(ws.findings) == 2

    scope = finding_consolidation_scope(ws)

    drafts = scope.candidates["draft_findings"]
    assert {item.source["process"] for item in drafts} == {"Procurement"}
    assert all("narrative" not in item.source for item in drafts)
    keys = {item.source["finding_id"]: item.source for item in scope.candidates["finding_exception_keys"]}
    assert all(set(value["keys"]) == {"invoice_no"} for value in keys.values())
    # Identifiers and nothing else: no amount, date or customer reaches the turn.
    assert all(not (set(value) - {"finding_id", "keys", "ids_supplied", "ids_withheld"}) for value in keys.values())
    table = scope.candidates["finding_overlaps"][0].source
    assert table["entity_pairs"] == 1
    [pair] = [item for item in table["pairs"] if item["basis"] == "entity"]
    assert pair["shared_count"] == 2
    assert pair["jaccard"] == round(2 / 3, 4)
    assert pair["shared_ids"] == ["1001", "1003"]


def test_same_process_pairs_without_shared_records_are_flagged_as_process_only():
    pairs = finding_overlaps(
        {"F-1": {"keys": {"INVOICE_ID": ["A"]}}, "F-2": {"keys": {"INVOICE_ID": ["B"]}}, "F-3": {"keys": {"VENDOR_ID": ["V"]}}},
        {"F-1": "Payment", "F-2": "Payment", "F-3": "Requisition"},
    )
    assert pairs == [
        {"finding_ids": ["F-1", "F-2"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []}
    ]


# --------------------------------------------------------------------------- #
# Readiness and basis
# --------------------------------------------------------------------------- #
def test_readiness_moves_from_missing_to_review_required_to_satisfied(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    capability = audit_capabilities.REGISTRY.get("findings.consolidated")

    readiness = capability.readiness(ws, {})
    assert readiness.state == "missing"
    assert readiness.details["drafts"] == 2
    units = capability.expand_units(ws, {})
    assert [unit.kind for unit in units] == ["finding_consolidation"]
    assert set(units[0].parent_refs) == {f"finding:{item['id']}" for item in ws.findings}

    basis = finding_consolidation.basis_sha1(ws)
    ids = [item["id"] for item in ws.findings]
    finding_consolidation.save(
        ws, basis,
        {"groups": [{"finding_ids": ids, "lead_finding_id": ids[0], "relation": "same_condition", "basis": "entity", "rationale": "same invoices"}], "singletons": []},
        run_id="run-c",
    )
    readiness = capability.readiness(ws, {})
    assert readiness.state == "review_required"
    assert readiness.details["undecided"] == 1
    # A suggestion on file for this basis expands no second unit.
    assert capability.expand_units(ws, {}) == []
    assert len(capability.expand_units(ws, {"generation_mode": "force"})) == 1

    saved = finding_consolidation.load(ws, basis)
    finding_consolidation.decide(ws, basis, saved["groups"][0]["group_id"], "dismissed")
    readiness = capability.readiness(ws, {})
    assert readiness.state == "satisfied"
    assert readiness.details["groups"] == 1


def test_one_draft_needs_no_review(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Only", code=_selecting(1001, 1003))
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    ws = _commit_finding(ws, ws.observations[0])
    capability = audit_capabilities.REGISTRY.get("findings.consolidated")

    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_the_basis_moves_when_the_finding_set_or_its_evidence_moves(workspace_with_data):
    ws = _two_drafts(workspace_with_data)
    before = finding_consolidation.basis_sha1(ws)

    # Confirming and editing prose do not re-ask the question.
    from app import findings

    findings.update(ws, ws.findings[0]["id"], {"title": "Renamed"})
    assert finding_consolidation.basis_sha1(ws) == before

    # A third finding does.
    findings.add(ws, {"title": "Manual finding"})
    after_add = finding_consolidation.basis_sha1(ws)
    assert after_add != before

    # And so does a re-run that changes a member's result.
    twin = next(item for item in ws.data_tests if item["title"] == "Right")
    data_tests.update(ws, twin["id"], {
        "spec": {"schema_version": 2, "steps": [{"label": "Right", "instruction": "Right", "table_refs": ["transactions"], "code": _selecting(1001)}]},
    })
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    assert finding_consolidation.basis_sha1(ws) != after_add


def test_the_binder_is_registered_as_proposal_only():
    from app.agent import audit_execution

    source = open(audit_execution.__file__, encoding="utf-8").read()
    assert '"findings.consolidated": (' in source
    assert '{"worker": "reporting.finding_consolidation", "executor": None}' in source
    assert audit_execution._PARTIAL_DEPENDENCIES["report.working_draft"] == {"findings.drafted", "findings.consolidated"}
