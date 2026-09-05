"""Focused tests for the deterministic ``tests.generate`` executor.

Replaces the draft executor and both spec executors: one guarded RCM-row
commit creates or updates complete, ready Data and Document Tests in a single
transaction, per docs/test-capability-merge-plan.md sections 6 and 8.
"""

from __future__ import annotations

import polars as pl
import pytest

from app import doc_tests, document_analysis, documents, workspaces
from app.agent import capabilities as audit_capabilities
from app.agent.executors import EXECUTORS, ExecutorRequest
from app.agent.executors.tests import (
    GENERATE_EXECUTOR,
    TestGenerateExecutorTarget,
    semantic_test_id,
    stable_test_id,
)
from app.workspace_transactions import ParentConflict, parent_hashes


def _workspace(name="Test generate executor"):
    workspace = workspaces.create_workspace(name)
    frame = pl.DataFrame({"invoice": ["INV-1", "INV-1", "INV-2"], "amount": [100, 100, 200]})
    workspace.add_table("transactions.csv", frame.write_csv().encode())
    doc = documents.add_document(
        workspace, "Approval.txt", b"Management approved the payment.", category="evidence"
    )
    workspace = workspaces.load_workspace(workspace.id)
    workspace.add_rcm(
        {
            "process": "Accounts payable",
            "risk": "Duplicate payments are processed",
            "control": "Duplicate invoice validation",
            "risk_rating": "high",
        }
    )
    return workspace, workspace.rcm[0]["id"], doc["id"]


def _data_test(**overrides):
    value = {
        "source": "data",
        "title": "Duplicate payment detection",
        "objective": "Determine whether duplicate payments were prevented.",
        "steps": [
            {
                "label": "Find duplicate invoice keys",
                "instruction": "Compare invoice numbers for duplicates.",
                "code": "result = transactions.filter(pl.col('invoice').is_duplicated())",
            }
        ],
        "methodology_refs": [],
    }
    value.update(overrides)
    return value


def _document_test(doc_id, **overrides):
    value = {
        "source": "document",
        "title": "Payment approval review",
        "objective": "Determine whether selected payments were approved.",
        "steps": [
            {
                "label": "Inspect approval evidence",
                "instruction": "Determine whether the payment was approved.",
                "mode": "question",
                "document_ids": [doc_id],
                "question": "Was this payment approved before release?",
                "missing_evidence": "",
            }
        ],
        "methodology_refs": [],
    }
    value.update(overrides)
    return value


def _request(workspace, rcm_id, tests):
    return ExecutorRequest(
        executor_id="tests.generate",
        capability_id="tests.specified",
        unit_id=f"test_generation:{rcm_id.lower()}",
        proposal={"tests": tests},
        expected_revision=workspace.revision,
        expected_parents=parent_hashes(workspace, [f"rcm:{rcm_id}"]),
        activity={"artifact_refs": [f"rcm:{rcm_id}"]},
    )


def test_generate_executor_creates_ready_data_and_document_tests_atomically():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test(), _document_test(doc_id)])
    target = TestGenerateExecutorTarget(workspace, "run-mixed", rcm_id)
    before = workspace.revision

    receipt = EXECUTORS.execute(request, target)

    data_test = target.workspace.data_tests[0]
    doc_test = doc_tests.list_tests(target.workspace)[0]
    assert data_test["status"] == "ready"
    assert data_test["created_by"] == "agent"
    assert doc_test["status"] == "ready"
    assert doc_test["created_by"] == "agent"
    assert data_test["rcm_id"] == rcm_id
    assert doc_test["rcm_id"] == rcm_id
    refs = set(target.workspace.rcm[0]["test_refs"])
    assert refs == {f"datatest:{data_test['id']}", f"doctest:{doc_test['id']}"}
    assert receipt.workspace_revision_before == before
    assert len(receipt.artifact_refs) == 2
    assert {item["action"] for item in receipt.output["tests"]} == {"created"}


def test_generate_executor_data_step_exposes_all_workspace_tables_and_retains_code():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test()])
    target = TestGenerateExecutorTarget(workspace, "run-data", rcm_id)

    EXECUTORS.execute(request, target)

    committed = target.workspace.data_tests[0]
    assert committed["engine"] == "polars"
    assert committed["table_refs"] == []
    steps = committed["spec"]["steps"]
    assert steps[0]["label"] == "Find duplicate invoice keys"
    assert "table_refs" not in steps[0]
    assert steps[0]["step_id"]
    assert "result" in steps[0]["code"]


def test_generate_executor_document_item_retains_its_own_documents_and_question():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_document_test(doc_id)])
    target = TestGenerateExecutorTarget(workspace, "run-doc", rcm_id)

    EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(target.workspace, doc_tests.list_tests(target.workspace)[0]["id"])
    assert committed["kind"] == "qa"
    item = committed["items"][0]
    assert item["document_ids"] == [doc_id]
    assert item["question"] == "Was this payment approved before release?"
    assert item["instruction"] == "Determine whether the payment was approved."


def _voucher_with_fields(workspace, filename: str, identifier: str, amount: float):
    """Attach transaction evidence carrying an extracted structured record."""

    entry = documents.add_document(
        workspace,
        filename,
        f"Invoice for {identifier} totalling {amount:.0f}.".encode(),
        category="evidence",
    )
    extracted = documents.extract_document(workspace, entry["id"])
    reloaded = workspaces.load_workspace(workspace.id)
    document = next(
        item for item in reloaded.documents if str(item["id"]) == str(entry["id"])
    )
    document_analysis.persist_analysis(
        reloaded,
        document,
        extracted,
        {
            "summary_markdown": "Invoice.",
            "audit_notes_markdown": "None.",
            "citations": [
                {
                    "id": "C1",
                    "page": 1,
                    "excerpt": f"Invoice for {identifier} totalling {amount:.0f}.",
                }
            ],
            "fields": {
                "document_type": "invoice",
                "identifiers": [
                    {"kind": "invoice_number", "value": identifier, "citation": "C1"}
                ],
                "amounts": [{"kind": "total", "value": amount, "citation": "C1"}],
            },
            "analysis_profile": "voucher",
        },
        provider="test",
        model="test",
    )
    return str(entry["id"])


def test_generate_executor_vouch_step_builds_items_from_the_population():
    """The accepted proposal is a plan; the workspace produces the items.

    This is the whole point of the cycle shape: the model writes paths, and the
    executor materializes one item per population row that linked to a document,
    with the row's own values frozen as what the document is compared against.
    """
    workspace, rcm_id, _doc_id = _workspace()
    _voucher_with_fields(workspace, "INV-1.txt", "INV-1", 100.0)
    workspace = workspaces.load_workspace(workspace.id)

    vouch = _document_test(
        _doc_id,
        steps=[
            {
                "label": "Vouch payments to invoices",
                "instruction": "Agree each recorded payment to its invoice.",
                "mode": "vouch",
                "anchor_table": "transactions",
                "anchor_key": "invoice",
                "document_roles": [{"role": "invoice", "required": True}],
                "checks": [
                    {
                        "field": "amount agrees",
                        "method": "numeric_tolerance",
                        "tolerance": 0,
                        "left": "row.amount",
                        "right": "invoice.amount.total",
                    }
                ],
            }
        ],
    )
    request = _request(workspace, rcm_id, [vouch])
    target = TestGenerateExecutorTarget(workspace, "run-vouch", rcm_id)

    with pytest.raises(
        workspaces.WorkspaceError, match="removed vouch-step cycle schema"
    ):
        EXECUTORS.execute(request, target)

def test_generate_executor_missing_evidence_blocks_the_test_and_creates_a_request():
    workspace, rcm_id, doc_id = _workspace()
    blocked = _document_test(
        doc_id,
        steps=[
            {
                "label": "Inspect approval evidence",
                "instruction": "Determine whether the payment was approved.",
                "mode": "question",
                "document_ids": [],
                "question": "Was this payment approved before release?",
                "missing_evidence": "Signed approval memo",
            }
        ],
    )
    request = _request(workspace, rcm_id, [blocked])
    target = TestGenerateExecutorTarget(workspace, "run-blocked", rcm_id)

    EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(target.workspace, doc_tests.list_tests(target.workspace)[0]["id"])
    assert committed["status"] == "blocked"
    assert committed["scope_limitations"] == "Signed approval memo"
    assert len(target.workspace.evidence_requests) == 1
    request_record = target.workspace.evidence_requests[0]
    assert request_record["reason"] == "Signed approval memo"
    assert request_record["document_test_id"] == committed["id"]
    assert committed["items"][0]["evidence_request_ids"] == [request_record["id"]]


def test_generate_executor_updates_a_matched_test_and_preserves_its_id():
    workspace, rcm_id, doc_id = _workspace()
    first = _request(workspace, rcm_id, [_data_test()])
    target = TestGenerateExecutorTarget(workspace, "run-update", rcm_id)
    EXECUTORS.execute(first, target)
    committed_id = target.workspace.data_tests[0]["id"]

    second = _request(
        target.workspace, rcm_id, [_data_test(objective="Revised objective")]
    )
    receipt = EXECUTORS.execute(second, target)

    assert len(target.workspace.data_tests) == 1
    assert target.workspace.data_tests[0]["id"] == committed_id
    assert target.workspace.data_tests[0]["objective"] == "Revised objective"
    assert receipt.output["tests"][0]["action"] == "updated"


def test_generate_executor_upgrades_a_matching_agent_created_draft_in_place():
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("doctest", rcm_id, "Payment approval review")
    doc_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("doctest", semantic),
            "semantic_id": semantic,
            "title": "Payment approval review",
            "objective": "Determine whether selected payments were approved.",
            "rcm_id": rcm_id,
            "agent_run_id": "prior-run",
        },
    )
    draft_id = doc_tests.list_tests(workspace)[0]["id"]
    assert doc_tests.list_tests(workspace)[0]["status"] == "draft"

    request = _request(workspace, rcm_id, [_document_test(doc_id)])
    target = TestGenerateExecutorTarget(workspace, "run-upgrade", rcm_id)
    receipt = EXECUTORS.execute(request, target)

    upgraded = doc_tests.list_tests(target.workspace)
    assert len(upgraded) == 1
    assert upgraded[0]["id"] == draft_id
    assert upgraded[0]["status"] == "ready"
    assert receipt.output["tests"][0]["action"] == "updated"


def test_generate_executor_preserves_an_auditor_owned_test_without_permission():
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("datatest", rcm_id, "Duplicate payment detection")
    from app import data_tests

    data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    request = _request(workspace, rcm_id, [_data_test()])
    target = TestGenerateExecutorTarget(workspace, "run-preserve", rcm_id)

    receipt = EXECUTORS.execute(request, target)

    assert target.workspace.data_tests[0]["objective"] == "Auditor objective"
    assert receipt.output["tests"][0]["action"] == "preserved"


def test_generate_executor_replaces_an_auditor_test_with_permission():
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("datatest", rcm_id, "Duplicate payment detection")
    from app import data_tests

    data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    request = _request(workspace, rcm_id, [_data_test()])
    target = TestGenerateExecutorTarget(
        workspace, "run-permission", rcm_id, allow_auditor_overwrite=True
    )

    receipt = EXECUTORS.execute(request, target)

    assert target.workspace.data_tests[0]["objective"] == (
        "Determine whether duplicate payments were prevented."
    )
    assert receipt.output["tests"][0]["action"] == "updated"


def test_generate_executor_parent_hash_rejects_a_concurrent_rcm_change():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test()])
    workspace.update_rcm(rcm_id, {"control": "Auditor rewrote the control"})
    target = TestGenerateExecutorTarget(workspace, "run-parent", rcm_id)

    with pytest.raises(ParentConflict):
        GENERATE_EXECUTOR.implementation(request, target)


def test_generate_executor_allows_derived_rcm_state_to_change():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test()])

    # These are workflow projections, not the auditor's RCM definition. A
    # sibling unit may update them before this serialized unit commits.
    workspace.rcm[0]["execution_rollup"] = {"status": "in_progress"}
    workspace.rcm[0]["finding_refs"] = ["finding:FND-001"]
    workspace.save()

    target = TestGenerateExecutorTarget(workspace, "run-derived", rcm_id)
    receipt = EXECUTORS.execute(request, target)

    assert receipt.output["tests"][0]["action"] == "created"


def test_generate_reconciliation_explains_a_precommit_rcm_conflict():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test()])
    workspace.update_rcm(rcm_id, {"control": "Auditor rewrote the control"})
    target = TestGenerateExecutorTarget(workspace, "run-precommit-conflict", rcm_id)

    reconciliation = GENERATE_EXECUTOR.reconciler(request, target)

    assert reconciliation.disposition == "conflict"
    assert "material fields changed" in reconciliation.reason
    assert "no commit was applied" in reconciliation.reason


def test_generate_executor_reconciles_an_interrupted_commit():
    workspace, rcm_id, doc_id = _workspace()
    request = _request(workspace, rcm_id, [_data_test()])
    target = TestGenerateExecutorTarget(workspace, "run-reconcile", rcm_id)

    assert GENERATE_EXECUTOR.reconciler(request, target).disposition == "not_applied"

    GENERATE_EXECUTOR.implementation(request, target)
    committed_id = target.workspace.data_tests[0]["id"]

    recovered = GENERATE_EXECUTOR.reconciler(request, target)
    assert recovered.disposition == "already_applied"
    assert recovered.result.postcondition_hashes == parent_hashes(
        target.workspace, [f"datatest:{committed_id}"]
    )


def test_generate_executor_rejects_regenerating_a_settled_document_test():
    # Regeneration over an already-executed test is an accepted, deferred gap
    # (merge plan section 13): the row commit fails rather than silently
    # discarding auditor-settled work.
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("doctest", rcm_id, "Payment approval review")
    existing = doc_tests.create_test(
        workspace,
        {
            "id": stable_test_id("doctest", semantic),
            "semantic_id": semantic,
            "kind": "qa",
            "title": "Payment approval review",
            "objective": "Determine whether selected payments were approved.",
            "rcm_id": rcm_id,
            "agent_run_id": "prior-run",
            "items": [
                {
                    "label": "Inspect approval evidence",
                    "question": "Was this approved?",
                    "document_ids": [doc_id],
                }
            ],
        },
    )
    # An executed test is never silently re-specified, whether or not an auditor
    # has signed the runner's verdict off.
    existing["items"][0]["evaluation"] = doc_tests.new_evaluation(
        "passed", "Model assessment outcome: accepted."
    )
    doc_tests.save_test(workspace, existing)
    request = _request(workspace, rcm_id, [_document_test(doc_id)])
    target = TestGenerateExecutorTarget(workspace, "run-settled", rcm_id)

    with pytest.raises(Exception, match="final-result items"):
        EXECUTORS.execute(request, target)


def test_generate_executor_keeps_a_partly_sourced_document_test_runnable():
    """A scope note beside a sourced question must not block the whole test.

    Generation habitually pairs a real question over attached documents with a
    document-less step naming what else would be needed. Reading that second
    step as an unattached item blocked tests whose actual question had evidence
    to work with.
    """
    workspace, rcm_id, doc_id = _workspace()
    mixed = _document_test(
        doc_id,
        steps=[
            {
                "label": "Assess approval evidence",
                "instruction": "Determine whether the payment was approved.",
                "mode": "question",
                "document_ids": [doc_id],
                "question": "Was this payment approved before release?",
                "missing_evidence": "",
            },
            {
                "label": "Identify missing approval evidence",
                "instruction": "Record evidence that is not available.",
                "mode": "question",
                "document_ids": [],
                "question": "What approval evidence is still outstanding?",
                "missing_evidence": "Signed approval memo",
            },
        ],
    )
    request = _request(workspace, rcm_id, [mixed])
    target = TestGenerateExecutorTarget(workspace, "run-mixed", rcm_id)

    EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(
        target.workspace, doc_tests.list_tests(target.workspace)[0]["id"]
    )
    assert committed["status"] != "blocked"
    assert committed["scope_limitations"] == "Signed approval memo"
    # Only the sourced step is an executable item, so nothing reports an
    # unattached document.
    assert len(committed["items"]) == 1
    assert committed["items"][0]["document_ids"] == [doc_id]
    assert doc_tests.execution_issues(committed) == []
    # The absent evidence is still requested, just not as a blocker.
    assert len(target.workspace.evidence_requests) == 1
    assert target.workspace.evidence_requests[0]["reason"] == "Signed approval memo"


def test_generate_executor_still_blocks_when_no_step_has_a_document():
    workspace, rcm_id, doc_id = _workspace()
    blocked = _document_test(
        doc_id,
        steps=[
            {
                "label": "Assess approval evidence",
                "instruction": "Determine whether the payment was approved.",
                "mode": "question",
                "document_ids": [],
                "question": "Was this payment approved before release?",
                "missing_evidence": "Approval register",
            },
            {
                "label": "Identify missing approval evidence",
                "instruction": "Record evidence that is not available.",
                "mode": "question",
                "document_ids": [],
                "question": "What approval evidence is still outstanding?",
                "missing_evidence": "Signed approval memo",
            },
        ],
    )
    request = _request(workspace, rcm_id, [blocked])
    target = TestGenerateExecutorTarget(workspace, "run-all-blocked", rcm_id)

    EXECUTORS.execute(request, target)

    committed = doc_tests.load_test(
        target.workspace, doc_tests.list_tests(target.workspace)[0]["id"]
    )
    assert committed["status"] == "blocked"
    assert committed["scope_limitations"] == "Approval register; Signed approval memo"
    assert len(target.workspace.evidence_requests) == 2


def test_a_retitled_test_naming_what_it_revises_updates_instead_of_duplicating():
    """The title-drift duplicate, reproduced and fixed.

    Regeneration is free to reword a control. Because identity used to be
    derived from ``slugify(title)``, the reworded version matched nothing and
    was stored beside the original — one engagement kept two "confirmation
    traceability" tests on a single row, the second a strict superset of the
    first.
    """
    workspace, rcm_id, _doc_id = _workspace()
    first_target = TestGenerateExecutorTarget(workspace, "run-1", rcm_id)
    first = EXECUTORS.execute(_request(workspace, rcm_id, [_data_test()]), first_target)
    original_id = first.output["tests"][0]["id"]
    assert len(first_target.workspace.data_tests) == 1

    workspace = workspaces.load_workspace(workspace.id)
    retitled = _data_test(
        title="Duplicate payment detection across the period",
        objective="Determine whether duplicate payments were prevented, period-wide.",
        revises=original_id,
    )
    second = EXECUTORS.execute(
        _request(workspace, rcm_id, [retitled]),
        TestGenerateExecutorTarget(workspace, "run-2", rcm_id),
    )

    assert second.output["tests"][0]["action"] == "updated"
    assert second.output["tests"][0]["id"] == original_id
    assert len(second.output["tests"]) == 1
    workspace = workspaces.load_workspace(workspace.id)
    assert len(workspace.data_tests) == 1
    assert workspace.data_tests[0]["title"] == "Duplicate payment detection across the period"
    # Identity is the record's, not the new title's: the stored semantic id
    # still reads from the original wording, and must keep doing so or the next
    # rewrite is a fresh mismatch again.
    assert workspace.data_tests[0]["semantic_id"] == semantic_test_id(
        "datatest", rcm_id, "Duplicate payment detection"
    )


def test_a_retitled_test_that_names_nothing_still_creates_a_second_test():
    """The unfixed half, pinned deliberately.

    Without ``revises`` there is nothing to match a reworded test on, and the
    title-derived fallback has to stay for proposals and stored tests that
    predate the field. This is what the post-run redundancy detector is for.
    """
    workspace, rcm_id, _doc_id = _workspace()
    EXECUTORS.execute(
        _request(workspace, rcm_id, [_data_test()]),
        TestGenerateExecutorTarget(workspace, "run-1", rcm_id),
    )
    workspace = workspaces.load_workspace(workspace.id)
    EXECUTORS.execute(
        _request(workspace, rcm_id, [_data_test(title="Duplicate payments, restated")]),
        TestGenerateExecutorTarget(workspace, "run-2", rcm_id),
    )

    workspace = workspaces.load_workspace(workspace.id)
    assert len(workspace.data_tests) == 2


def test_an_unchanged_title_still_matches_without_revises():
    """The fallback keeps working for everything generated before the field."""
    workspace, rcm_id, _doc_id = _workspace()
    first = EXECUTORS.execute(
        _request(workspace, rcm_id, [_data_test()]),
        TestGenerateExecutorTarget(workspace, "run-1", rcm_id),
    )
    workspace = workspaces.load_workspace(workspace.id)
    second = EXECUTORS.execute(
        _request(workspace, rcm_id, [_data_test(objective="Reworded objective.")]),
        TestGenerateExecutorTarget(workspace, "run-2", rcm_id),
    )

    assert second.output["tests"][0]["id"] == first.output["tests"][0]["id"]
    assert len(workspaces.load_workspace(workspace.id).data_tests) == 1


def test_revises_naming_a_test_on_another_row_falls_back_rather_than_hijacking():
    """A reference the row does not own must not reach across to another row."""
    workspace, rcm_id, _doc_id = _workspace()
    workspace.add_rcm(
        {
            "process": "Payroll",
            "risk": "Unapproved pay changes",
            "control": "Change review",
            "risk_rating": "high",
        }
    )
    other_row = workspace.rcm[1]["id"]
    owned = EXECUTORS.execute(
        _request(workspace, rcm_id, [_data_test()]),
        TestGenerateExecutorTarget(workspace, "run-1", rcm_id),
    )
    foreign_id = owned.output["tests"][0]["id"]

    workspace = workspaces.load_workspace(workspace.id)
    EXECUTORS.execute(
        _request(
            workspace,
            other_row,
            [_data_test(title="Payroll change review", revises=foreign_id)],
        ),
        TestGenerateExecutorTarget(workspace, "run-2", other_row),
    )

    workspace = workspaces.load_workspace(workspace.id)
    by_row = {item["id"]: item["rcm_id"] for item in workspace.data_tests}
    assert len(by_row) == 2
    assert by_row[foreign_id] == rcm_id


# --------------------------------------------------------------------------- #
# Redrafting one named test rather than the row (step 2)
# --------------------------------------------------------------------------- #
def test_a_named_redraft_replaces_that_test_and_leaves_its_siblings_alone():
    """The whole promise of "redraft DT-123": DT-124 is not collateral."""
    workspace, rcm_id, doc_id = _workspace()
    EXECUTORS.execute(
        _request(
            workspace,
            rcm_id,
            [_data_test(), _data_test(title="Sibling screening")],
        ),
        TestGenerateExecutorTarget(workspace, "run-seed", rcm_id),
    )
    seeded = workspaces.load_workspace(workspace.id)
    by_title = {item["title"]: item for item in seeded.data_tests}
    named = by_title["Duplicate payment detection"]
    sibling = dict(by_title["Sibling screening"])

    target = TestGenerateExecutorTarget(
        seeded,
        "run-redraft",
        rcm_id,
        regenerate_test_ids=(named["id"],),
    )
    receipt = EXECUTORS.execute(
        _request(
            seeded,
            rcm_id,
            [_data_test(objective="Rewritten objective", revises=named["id"])],
        ),
        target,
    )

    after = {item["title"]: item for item in target.workspace.data_tests}
    assert len(after) == 2
    # Replaced in place: the same record, so every reference to it still holds.
    assert after["Duplicate payment detection"]["id"] == named["id"]
    assert after["Duplicate payment detection"]["objective"] == "Rewritten objective"
    assert receipt.output["tests"][0]["action"] == "updated"
    # Untouched, field for field.
    assert after["Sibling screening"] == sibling


def test_naming_an_auditor_owned_test_is_the_permission_to_replace_it():
    """Pointing at a record and asking for it to be redrafted is consent."""
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("datatest", rcm_id, "Duplicate payment detection")
    from app import data_tests

    auditor_test = data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    target = TestGenerateExecutorTarget(
        workspace,
        "run-named",
        rcm_id,
        regenerate_test_ids=(auditor_test["id"],),
    )

    receipt = EXECUTORS.execute(_request(workspace, rcm_id, [_data_test()]), target)

    assert receipt.output["tests"][0]["action"] == "updated"
    assert target.workspace.data_tests[0]["objective"] == (
        "Determine whether duplicate payments were prevented."
    )


def test_naming_one_auditor_test_is_not_permission_over_another():
    """The permission is the id, not a mode: it does not spread down the row."""
    workspace, rcm_id, doc_id = _workspace()
    semantic = semantic_test_id("datatest", rcm_id, "Duplicate payment detection")
    from app import data_tests

    data_tests.create_draft(
        workspace,
        {
            "id": stable_test_id("datatest", semantic),
            "semantic_id": semantic,
            "title": "Auditor test",
            "objective": "Auditor objective",
            "rcm_id": rcm_id,
        },
    )
    target = TestGenerateExecutorTarget(
        workspace, "run-other", rcm_id, regenerate_test_ids=("DAT-SOMETHING-ELSE",)
    )

    receipt = EXECUTORS.execute(_request(workspace, rcm_id, [_data_test()]), target)

    assert receipt.output["tests"][0]["action"] == "preserved"
    assert target.workspace.data_tests[0]["objective"] == "Auditor objective"
