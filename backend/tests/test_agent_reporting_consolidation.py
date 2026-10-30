"""Findings consolidation, phase 2: the ``findings.consolidated`` capability.

The review runs before any finding is drafted. The worker proposes groups over
the exception observations and the validator holds it to the overlap table the
adapter computed locally; the capability's readiness is keyed by the basis the
suggestion was made against; and an accepted group is drafted once, as one
finding, instead of once per member and again as a merged lead.
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
from app.agent.context.adapters import flags_identical_records, observation_overlaps
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


OBSERVATIONS = [
    {"id": "OBS-A", "summary": "Payments before receipt", "exception_count": 3, "process": "Payment", "test": {"id": "DAT-1", "title": "Paid before GRN"}, "entity_keys": ["INVOICE_ID"]},
    {"id": "OBS-B", "summary": "Payment released ahead of goods receipt", "exception_count": 3, "process": "Payment", "test": {"id": "DAT-2", "title": "Payment date vs receipt"}, "entity_keys": ["INVOICE_ID"]},
    {"id": "OBS-C", "summary": "Inactive vendor on PO", "exception_count": 1, "process": "Purchase order", "test": {"id": "DAT-3", "title": "Vendor status"}, "entity_keys": ["VENDOR_ID"]},
    {"id": "OBS-D", "summary": "Inactive vendor paid", "exception_count": 2, "process": "Payment", "test": {"id": "DAT-4", "title": "Vendor status at payment"}, "entity_keys": ["VENDOR_ID"]},
]
KEYS = [
    {"observation_id": "OBS-A", "keys": {"INVOICE_ID": ["INV1", "INV2", "INV3"]}, "ids_supplied": 3, "ids_withheld": 0},
    {"observation_id": "OBS-B", "keys": {"INVOICE_ID": ["INV1", "INV2", "INV3"]}, "ids_supplied": 3, "ids_withheld": 0},
    {"observation_id": "OBS-C", "keys": {"VENDOR_ID": ["V9"]}, "ids_supplied": 1, "ids_withheld": 0},
    {"observation_id": "OBS-D", "keys": {"VENDOR_ID": ["V9"], "INVOICE_ID": ["INV7"]}, "ids_supplied": 2, "ids_withheld": 0},
]
OVERLAPS = {
    "pairs": [
        {"observation_ids": ["OBS-A", "OBS-B"], "basis": "entity", "key": "INVOICE_ID", "shared_count": 3, "jaccard": 1.0, "shared_ids": ["INV1", "INV2", "INV3"]},
        {"observation_ids": ["OBS-C", "OBS-D"], "basis": "entity", "key": "VENDOR_ID", "shared_count": 1, "jaccard": 1.0, "shared_ids": ["V9"]},
        {"observation_ids": ["OBS-A", "OBS-D"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []},
        {"observation_ids": ["OBS-B", "OBS-D"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []},
    ],
    "entity_pairs": 2,
    "process_pairs": 2,
}


def _bundle(instruction: str | None = None):
    values = [
        ("exception_observations", f"observation:{item['id']}", ContextRepresentation("current_artifact"), item)
        for item in OBSERVATIONS
    ] + [
        ("observation_exception_keys", f"observation:{keys['observation_id']}:keys", ContextRepresentation("datatest_exception_keys"), keys)
        for keys in KEYS
    ] + [
        ("observation_overlaps", "observations:overlaps", ContextRepresentation("current_artifact"), OVERLAPS),
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
        activity={"artifact_refs": ["observation:OBS-A"]},
    )


def _proposal(**overrides):
    value = {
        "groups": [
            {
                "observation_ids": ["OBS-A", "OBS-B"],
                "lead_observation_id": "OBS-A",
                "relation": "same_condition",
                "basis": "entity",
                "proposed_title": "Payments released before goods receipt",
                "root_cause_hypothesis": "The three-way match does not block payment.",
                "rationale": "Both flag INV1, INV2 and INV3.",
            },
            {
                "observation_ids": ["OBS-C", "OBS-D"],
                "lead_observation_id": "OBS-D",
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
    assert tuple(result.proposal["groups"][0]["shared_entities"]["INVOICE_ID"]) == ("INV1", "INV2", "INV3")
    assert gateway.calls[0]["system"] == reporting_worker.CONSOLIDATION_SYSTEM
    # Asked before drafting: the prompt says so and sends no draft.
    assert "No finding has been drafted yet" in gateway.calls[0]["system"]
    sent = json.loads(gateway.calls[0]["user"])
    assert [item["id"] for item in sent["EXCEPTION OBSERVATIONS"]] == ["OBS-A", "OBS-B", "OBS-C", "OBS-D"]
    assert "DRAFT FINDINGS" not in sent
    assert sent["OVERLAP TABLE"]["entity_pairs"] == 2
    assert gateway.calls[0]["activity"]["context_metrics"]["worker_kind"] == "finding_consolidation"


def test_the_validator_refuses_an_unknown_observation():
    with pytest.raises(WorkerResponseValidationError, match="not a supplied observation"):
        validate_consolidation_proposal(
            _proposal(groups=[{**_proposal()["groups"][0], "observation_ids": ["OBS-A", "OBS-Z"]}], singletons=["OBS-B", "OBS-C", "OBS-D"]),
            _request(),
        )


def test_the_validator_refuses_a_singleton_group():
    with pytest.raises(WorkerResponseValidationError, match="at least two"):
        validate_consolidation_proposal(
            _proposal(groups=[{**_proposal()["groups"][0], "observation_ids": ["OBS-A"]}], singletons=["OBS-B", "OBS-C", "OBS-D"]),
            _request(),
        )


def test_the_validator_refuses_a_same_condition_group_without_entity_overlap():
    # OBS-A and OBS-D share a process, not records: they may be a shared cause,
    # never the same condition — and a same-condition group is the one an auto
    # run accepts without asking.
    with pytest.raises(WorkerResponseValidationError, match="same condition"):
        validate_consolidation_proposal(
            _proposal(
                groups=[{**_proposal()["groups"][0], "observation_ids": ["OBS-A", "OBS-D"], "lead_observation_id": "OBS-A"}],
                singletons=["OBS-B", "OBS-C"],
            ),
            _request(),
        )


def test_a_process_only_group_must_say_so():
    with pytest.raises(WorkerResponseValidationError, match="share no records"):
        validate_consolidation_proposal(
            _proposal(
                groups=[{
                    "observation_ids": ["OBS-A", "OBS-D"], "lead_observation_id": "OBS-A",
                    "relation": "shared_cause", "basis": "entity",
                    "proposed_title": "x", "root_cause_hypothesis": "y", "rationale": "z",
                }],
                singletons=["OBS-B", "OBS-C"],
            ),
            _request(),
        )
    accepted = validate_consolidation_proposal(
        _proposal(
            groups=[{
                "observation_ids": ["OBS-A", "OBS-D"], "lead_observation_id": "OBS-A",
                "relation": "shared_cause", "basis": "process",
                "proposed_title": "x", "root_cause_hypothesis": "y", "rationale": "z",
            }],
            singletons=["OBS-B", "OBS-C"],
        ),
        _request(),
    )
    assert accepted["groups"][0]["basis"] == "process"


def test_the_validator_refuses_an_observation_placed_twice_and_one_placed_nowhere():
    with pytest.raises(WorkerResponseValidationError, match="more than once"):
        validate_consolidation_proposal(
            _proposal(groups=[_proposal()["groups"][0], {**_proposal()["groups"][1], "observation_ids": ["OBS-A", "OBS-C", "OBS-D"], "lead_observation_id": "OBS-D", "basis": "process"}]),
            _request(),
        )
    with pytest.raises(WorkerResponseValidationError, match="missing OBS-D"):
        validate_consolidation_proposal(
            _proposal(groups=[_proposal()["groups"][0]], singletons=["OBS-C"]),
            _request(),
        )


def test_every_observation_as_a_singleton_is_a_real_answer():
    gateway = _Gateway([json.dumps({"groups": [], "singletons": ["OBS-A", "OBS-B", "OBS-C", "OBS-D"]})])

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
    assert [source.id for source in spec.sources][:3] == [
        "exception_observations", "observation_exception_keys", "observation_overlaps",
    ]


# --------------------------------------------------------------------------- #
# Workspace helpers
# --------------------------------------------------------------------------- #
def _draft(**overrides):
    value = {"title": "Duplicate invoice processing", "severity": "medium", "narrative": NARRATIVE, "cause_pending": False}
    value.update(overrides)
    return value


def _commit_finding(ws, observation, run_id="run-1", title="Duplicate invoice processing", group=None):
    request = ExecutorRequest(
        executor_id="reporting.finding",
        capability_id="findings.drafted",
        unit_id=f"finding:{observation['id']}",
        proposal={"finding": _draft(title=title)},
        expected_revision=ws.revision,
        expected_parents=parent_hashes(ws, [f"observation:{observation['id']}"]),
        activity={"artifact_refs": [f"observation:{observation['id']}"]},
    )
    target = FindingExecutorTarget(ws, run_id, observation["id"], group=group)
    EXECUTORS.execute(request, target)
    return target.workspace


def _two_exceptions(workspace_with_data):
    """Two exception observations on different rows whose tests flag
    overlapping invoices, with no finding drafted yet."""
    ws = workspace_with_data
    left_row = _row(ws, risk="Paid before receipt")
    right_row = _row(ws, risk="Paid without approval")
    _polars_test(ws, left_row, title="Left", code=_selecting(1001, 1003, 1005))
    _polars_test(ws, right_row, title="Right", code=_selecting(1001, 1003))
    data_tests.run_all(ws)
    return workspaces.load_workspace(ws.id)


def _by_test(ws):
    """The two observations, Left first, whatever the random ids sort to."""
    titles = {str(item["id"]): str(item["title"]) for item in ws.data_tests}
    return sorted(ws.observations, key=lambda item: titles[item["test_id"]])


def _two_drafts(workspace_with_data):
    """Two findings, each drafted from its own observation, as a run that never
    grouped them would leave them."""
    ws = _two_exceptions(workspace_with_data)
    test_titles = {str(item["id"]): str(item["title"]) for item in ws.data_tests}
    for observation in _by_test(ws):
        ws = _commit_finding(ws, observation, title=f"{test_titles[observation['test_id']]} finding")
    return workspaces.load_workspace(ws.id)


def _suggest(ws, *, relation="shared_cause", decision=None):
    basis = finding_consolidation.basis_sha1(ws)
    left, right = _by_test(ws)
    finding_consolidation.save(
        ws, basis,
        {
            "groups": [{
                "observation_ids": [left["id"], right["id"]], "lead_observation_id": left["id"],
                "relation": relation, "basis": "entity", "proposed_title": "One issue",
                "root_cause_hypothesis": "One cause.", "rationale": "Same invoices.",
                "shared_entities": {"invoice_no": ["1001", "1003"]},
                "decision": decision,
            }],
            "singletons": [],
        },
        run_id="run-c",
    )
    return basis, finding_consolidation.load(ws, basis)["groups"][0]


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
def test_the_adapter_reads_observations_supplies_keys_only_and_computes_the_overlap_table(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    assert len(ws.observations) == 2 and not ws.findings

    scope = finding_consolidation_scope(ws)

    observations = scope.candidates["exception_observations"]
    assert {item.source["process"] for item in observations} == {"Procurement"}
    assert {item.source["test"]["title"] for item in observations} == {"Left", "Right"}
    keys = {item.source["observation_id"]: item.source for item in scope.candidates["observation_exception_keys"]}
    assert all(set(value["keys"]) == {"invoice_no"} for value in keys.values())
    # Identifiers and nothing else: no amount, date or customer reaches the turn.
    assert all(not (set(value) - {"observation_id", "keys", "ids_supplied", "ids_withheld"}) for value in keys.values())
    table = scope.candidates["observation_overlaps"][0].source
    assert table["entity_pairs"] == 1
    [pair] = [item for item in table["pairs"] if item["basis"] == "entity"]
    assert pair["shared_count"] == 2
    assert pair["jaccard"] == round(2 / 3, 4)
    assert pair["shared_ids"] == ["1001", "1003"]


def test_same_process_pairs_without_shared_records_are_flagged_as_process_only():
    pairs = observation_overlaps(
        {"OBS-1": {"keys": {"INVOICE_ID": ["A"]}}, "OBS-2": {"keys": {"INVOICE_ID": ["B"]}}, "OBS-3": {"keys": {"VENDOR_ID": ["V"]}}},
        {"OBS-1": "Payment", "OBS-2": "Payment", "OBS-3": "Requisition"},
    )
    assert pairs == [
        {"observation_ids": ["OBS-1", "OBS-2"], "basis": "process", "process": "Payment", "shared_count": 0, "jaccard": 0.0, "shared_ids": []}
    ]


def test_identical_flagged_records_are_recognised_whatever_the_label(workspace_with_data):
    # Left flags 1001, 1003, 1005 and Right 1001, 1003: overlapping, not equal.
    ws = _two_exceptions(workspace_with_data)
    left, right = _by_test(ws)
    assert flags_identical_records(ws, [left["id"], right["id"]]) is False
    assert flags_identical_records(ws, [left["id"]]) is False
    assert flags_identical_records(ws, [left["id"], "OBS-MISSING"]) is False

    twin = next(item for item in ws.data_tests if item["title"] == "Right")
    data_tests.update(ws, twin["id"], {
        "spec": {"schema_version": 2, "steps": [{"label": "Right", "instruction": "Right", "table_refs": ["transactions"], "code": _selecting(1001, 1003, 1005)}]},
    })
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    left, right = _by_test(ws)
    assert flags_identical_records(ws, [left["id"], right["id"]]) is True


# --------------------------------------------------------------------------- #
# Readiness and basis
# --------------------------------------------------------------------------- #
def test_the_review_is_asked_of_observations_before_any_draft(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    capability = audit_capabilities.REGISTRY.get("findings.consolidated")

    readiness = capability.readiness(ws, {})
    assert readiness.state == "missing"
    assert readiness.details["groupable"] == 2
    units = capability.expand_units(ws, {})
    assert [unit.kind for unit in units] == ["finding_consolidation"]
    assert set(units[0].parent_refs) == {f"observation:{item['id']}" for item in ws.observations}

    basis, group = _suggest(ws)
    readiness = capability.readiness(ws, {})
    assert readiness.state == "review_required"
    assert readiness.details["undecided"] == 1
    # A suggestion on file for this basis expands no second unit.
    assert capability.expand_units(ws, {}) == []
    assert len(capability.expand_units(ws, {"generation_mode": "force"})) == 1

    finding_consolidation.decide(ws, basis, group["group_id"], "dismissed")
    readiness = capability.readiness(ws, {})
    assert readiness.state == "satisfied"
    assert readiness.details["groups"] == 1


def test_drafted_observations_are_not_asked_about(workspace_with_data):
    # Grouping drafts is the merge's job; asking a model about them would buy
    # the saving only after it was spent.
    ws = _two_drafts(workspace_with_data)
    capability = audit_capabilities.REGISTRY.get("findings.consolidated")

    assert finding_consolidation.groupable_observations(ws) == []
    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_one_exception_needs_no_review(workspace_with_data):
    ws = workspace_with_data
    row = _row(ws)
    _polars_test(ws, row, title="Only", code=_selecting(1001, 1003))
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    capability = audit_capabilities.REGISTRY.get("findings.consolidated")

    assert capability.readiness(ws, {}).state == "satisfied"
    assert capability.expand_units(ws, {}) == []


def test_the_basis_moves_with_the_evidence_and_not_with_decisions_or_drafts(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    before = finding_consolidation.basis_sha1(ws)

    # Deciding, and drafting the group, do not re-ask the question.
    basis, group = _suggest(ws)
    finding_consolidation.apply_group(ws, group, basis=basis)
    ws = workspaces.load_workspace(ws.id)
    assert finding_consolidation.basis_sha1(ws) == before
    left, _right = _by_test(ws)
    ws = _commit_finding(ws, left, group=left["consolidation"])
    assert finding_consolidation.basis_sha1(ws) == before

    # A re-run that changes a member's result does.
    twin = next(item for item in ws.data_tests if item["title"] == "Right")
    data_tests.update(ws, twin["id"], {
        "spec": {"schema_version": 2, "steps": [{"label": "Right", "instruction": "Right", "table_refs": ["transactions"], "code": _selecting(1001)}]},
    })
    data_tests.run_all(ws)
    ws = workspaces.load_workspace(ws.id)
    assert finding_consolidation.basis_sha1(ws) != before


# --------------------------------------------------------------------------- #
# An accepted group is drafted once
# --------------------------------------------------------------------------- #
def test_accepting_before_drafting_marks_the_members_and_expands_only_the_lead(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    basis, group = _suggest(ws)
    left, right = _by_test(ws)

    assert finding_consolidation.apply_group(ws, group, basis=basis, title="Chosen") is None

    ws = workspaces.load_workspace(ws.id)
    left, right = _by_test(ws)
    assert left["consolidation"]["role"] == "lead"
    assert left["consolidation"]["members"] == [right["id"]]
    assert left["consolidation"]["proposed_title"] == "Chosen"
    assert right["consolidation"] == {
        **{key: left["consolidation"][key] for key in ("group_id", "relation", "basis_sha1", "decided_by", "decided_at")},
        "role": "member",
        "into": left["id"],
    }
    drafted = audit_capabilities.REGISTRY.get("findings.drafted")
    units = drafted.expand_units(ws, {})
    assert [unit.parent_refs[0] for unit in units] == [f"observation:{left['id']}"]
    readiness = drafted.readiness(ws, {})
    assert readiness.state == "missing"
    assert readiness.details["eligible"] == 1
    assert readiness.details["grouped"] == 1
    # A member cannot be grouped a second time.
    with pytest.raises(workspaces.WorkspaceError, match="already in a consolidated group"):
        finding_consolidation.apply_group(ws, group, basis=basis)


def test_the_lead_draft_is_the_groups_one_finding(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    basis, group = _suggest(ws)
    finding_consolidation.apply_group(ws, group, basis=basis)
    ws = workspaces.load_workspace(ws.id)
    left, right = _by_test(ws)

    ws = _commit_finding(ws, left, title="One issue", group=left["consolidation"])

    [finding] = ws.findings
    assert finding["semantic_id"] == f"finding:consolidated:{group['group_id']}"
    assert finding["source_observation_id"] == left["id"]
    # Every member's references, as a merge would have unioned them.
    assert set(finding["rcm_refs"]) == {left["rcm_id"], right["rcm_id"]}
    assert set(finding["test_refs"]) == {left["test_id"], right["test_id"]}
    assert len(finding["execution_refs"]) == 2
    assert len(finding["evidence_refs"]) == 2
    assert finding["consolidation"]["role"] == "lead"
    assert finding["consolidation"]["members"] == []
    assert finding["consolidation"]["observations"] == [right["id"]]
    assert finding["consolidation"]["narrative_pending"] is False
    from app import findings

    assert findings.support_issues(ws, finding) == []
    assert findings.consolidation_observation_ids(ws, finding) == [right["id"]]
    # Nothing is left to draft: the group is one finding, not two.
    drafted = audit_capabilities.REGISTRY.get("findings.drafted")
    assert drafted.readiness(ws, {}).state == "satisfied"
    assert drafted.expand_units(ws, {}) == []

    # And the finding stands on the member: an exception that clears is a
    # support issue on the lead.
    member = next(item for item in ws.observations if item["id"] == right["id"])
    member["outcome"] = "resolved"
    ws.save()
    assert any(f"member observation {right['id']}" in issue for issue in findings.support_issues(ws, finding))


def test_ungrouping_a_member_returns_it_to_its_own_draft(workspace_with_data):
    ws = _two_exceptions(workspace_with_data)
    basis, group = _suggest(ws)
    finding_consolidation.apply_group(ws, group, basis=basis)
    ws = workspaces.load_workspace(ws.id)
    left, right = _by_test(ws)

    finding_consolidation.ungroup_observation(ws, right["id"])

    ws = workspaces.load_workspace(ws.id)
    assert all("consolidation" not in item for item in ws.observations)
    units = audit_capabilities.REGISTRY.get("findings.drafted").expand_units(ws, {})
    assert len(units) == 2
    with pytest.raises(workspaces.WorkspaceError, match="not grouped"):
        finding_consolidation.ungroup_observation(ws, right["id"])


def test_accepting_after_drafting_merges_the_drafts(workspace_with_data):
    # A shared-cause group an auto run left undecided: its members were drafted
    # separately, and accepting it now merges them.
    ws = _two_exceptions(workspace_with_data)
    basis, group = _suggest(ws)
    test_titles = {str(item["id"]): str(item["title"]) for item in ws.data_tests}
    for observation in _by_test(ws):
        ws = _commit_finding(ws, observation, title=f"{test_titles[observation['test_id']]} finding")
    assert finding_consolidation.basis_sha1(ws) == basis

    lead = finding_consolidation.apply_group(ws, group, basis=basis)

    ws = workspaces.load_workspace(ws.id)
    assert lead is not None
    assert lead["consolidation"]["role"] == "lead"
    assert lead["consolidation"]["narrative_pending"] is True
    roles = sorted((item.get("consolidation") or {}).get("role") for item in ws.findings)
    assert roles == ["absorbed", "lead"]


def test_the_binder_is_registered_as_proposal_only_and_drafting_does_not_wait_on_it():
    from app.agent import audit_execution

    source = open(audit_execution.__file__, encoding="utf-8").read()
    assert '"findings.consolidated": (' in source
    assert '{"worker": "reporting.finding_consolidation", "executor": None}' in source
    # Grouping saves turns and never withholds a draft.
    assert audit_execution._PARTIAL_DEPENDENCIES["findings.drafted"] == {"findings.consolidated"}
    from app.agent.workflows import audit as audit_workflow
    assert "findings.consolidated" in audit_workflow.dependencies("findings.drafted")
    assert "findings.consolidated" not in audit_workflow.dependencies("report.working_draft")
