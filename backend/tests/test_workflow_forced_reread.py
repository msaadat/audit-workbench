"""A forced re-read of a document that reads the same must settle, not fail.

Found on the expenses engagement. ``Regenerate the RCM and the tests that cover
it`` routed under ``generation_mode: "force"``, which re-expands the whole
dependency closure, and ``documents.evidence_read`` fanned out over all twelve
payment vouchers — every one of them already read by an earlier run. The
reconciler read the type's ``documents_read`` as proof that *this* commit had
landed, reported ``already_applied``, and described a call that published no
revision the only truthful way there is: ``before == after``. The receipt
contract refused it, all twelve units failed with "executor_result.
workspace_revision_after must be greater than workspace_revision_before", and
``documents.schemas_stamped`` was blocked behind them.

Two things were wrong and only one of them was visible.

The invariant belongs to *executing*: a commit that mutated has to advance the
workspace or its receipt proves nothing. A reconciliation makes no commit of its
own, so equality is what happened and the receipt now says so.

And the reconciler was answering the wrong question. A forced refresh re-asks a
question whose earlier answer is still on disk; membership in ``documents_read``
cannot tell that answer from this one. Fixing only the invariant would have made
the run *succeed* while discarding twelve fresh readings, which is the worse of
the two failures. The reading's own run and unit ids settle it, exactly as
``reconcile_document_analysis`` already settled the same question — and folding a
document into its master twice, the hazard that argument was built to avoid,
costs nothing now that ``apply_reading`` counts a document once.
"""

from __future__ import annotations

import pytest

from app import document_analysis, document_masters, document_schemas
from app import documents, llm, workspaces
from app.agent import runner
from app.agent.capabilities import documents as document_capabilities
from app.agent.executors import EXECUTORS
from app.agent.executors import documents as document_executors
from app.agent.executors.documents import document_ref
from app.agent.executors.model import ExecutorRequest
from app.agent.workflows import documents as documents_workflow
from app.workspace_transactions import parent_hashes
from conftest import FakeAgentLLM, wait_run

READ_TAG = "agent:document_evidence_read"
CLASSIFY_TAG = "agent:document_classification"
MAP_TAG = "agent:document_analysis_map"
TYPE = "vendor_invoice"


def _declared(name: str, **overrides) -> dict:
    field = {
        "name": name,
        "role": "attribute",
        "value_type": "text",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
        "label": name.replace("_", " ").title(),
        "reason": f"The document states its {name.replace('_', ' ')}.",
        "values": [{"record": 1, "entry": 1, "value": "stated", "citation": "1"}],
    }
    field.update(overrides)
    return field


def _reading(*, fields=(), new_fields=()) -> dict:
    return {
        "records": [{"fields": list(fields)}],
        "new_fields": list(new_fields),
        "renames": [],
        "audit_notes": [],
        "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
    }


#: The one answer every document in this fixture gives, before and after the
#: force. An unchanged re-read is the case under test, so the model is scripted
#: to say the same thing twice rather than to drift.
STATED = _reading(
    fields=[
        {"name": "invoice_number", "entry": 1, "value": "INV-1041", "citation": "1"}
    ]
)
INTRODUCES = _reading(new_fields=[_declared("invoice_number")])


def _workspace(name: str, count: int = 2):
    ws = workspaces.create_workspace(name)
    created = [
        documents.add_document(
            ws,
            f"invoice-{index}.txt",
            f"Invoice No. INV-104{index}\nTotal Due USD {index}00.00".encode(),
            category="evidence",
        )
        for index in range(count)
    ]
    return ws, created


def _fake(monkeypatch):
    """Names every document a vendor invoice, then answers each reading."""

    answers = [INTRODUCES, STATED, STATED, STATED]
    calls = {"read": 0}

    def read(_messages):
        answer = answers[min(calls["read"], len(answers) - 1)]
        calls["read"] += 1
        return answer

    fake = FakeAgentLLM(
        {
            CLASSIFY_TAG: {
                "document_type": TYPE,
                "document_type_other": "",
                "confidence": "high",
                "rationale": "The header reads Invoice.",
            },
            READ_TAG: read,
            MAP_TAG: {
                "summary_markdown": "An invoice. [1]",
                "audit_notes_markdown": "Nothing is demonstrated by it alone.",
                "citations": [{"id": "1", "page": 1, "excerpt": "Invoice No."}],
            },
        }
    )
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


def _run(ws, created, *, action="analyze"):
    run = runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": "Analyse the documents.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{item['id']}" for item in created],
            "generation_mode": "reuse_existing" if action == "analyze" else "force",
        },
        context={"document_ids": [item["id"] for item in created], "action": action},
    )
    return wait_run(ws, run["id"])


def _stage(run: dict, stage_id: str) -> dict:
    return next(
        stage for stage in run["workflow"]["stages"] if stage["id"] == stage_id
    )


def _read_request(ws, document_id: str, unit_id: str) -> ExecutorRequest:
    return ExecutorRequest(
        executor_id=document_executors.READ_EXECUTOR_ID,
        capability_id="documents.evidence_read",
        unit_id=unit_id,
        proposal=STATED,
        expected_parents=parent_hashes(ws, [document_ref(document_id)]),
        expected_revision=ws.revision,
    )


def _read_target(ws, run_id: str, document_id: str, **overrides):
    values = {
        "extracted": document_capabilities.analyzable(ws, document_id),
        "action": "refresh",
        "vocabulary_mode": "frozen",
    }
    values.update(overrides)
    return document_executors.DocumentReadExecutorTarget(
        ws, run_id, document_id, TYPE, **values
    )


# ------------------------------------------------------------ the whole defect
def test_a_forced_re_read_of_an_unchanged_document_finishes(monkeypatch):
    """End to end: the stage settles, and the fresh readings are what landed."""

    ws, created = _workspace("Forced re-read")
    _fake(monkeypatch)
    first = _run(ws, created)
    assert first["status"] == "completed"

    before = workspaces.load_workspace(ws.id)
    analyses_before = {
        item["id"]: document_analysis.generated_record(before, item["id"])["id"]
        for item in created
    }

    second = _run(before, created, action="refresh")

    assert second["status"] == "completed"
    assert "workspace_revision" not in str(second.get("error") or "")
    read_stage = _stage(second, "document_masters")
    assert [unit["status"] for unit in read_stage["units"]] == ["succeeded"] * 2
    assert read_stage["status"] == "succeeded"
    # Blocked behind the twelve failures on the live run. It is a dependent
    # capability, so a read stage that fails takes the stamp with it.
    assert _stage(second, "document_schemas")["status"] != "blocked"

    after = workspaces.load_workspace(ws.id)
    # The force actually re-read: a reconciliation that reported the earlier
    # readings as this commit's would have left these artifact ids untouched,
    # having spent a model turn per document to learn nothing.
    assert all(
        document_analysis.generated_record(after, item["id"])["id"]
        != analyses_before[item["id"]]
        for item in created
    )
    assert all(
        document_capabilities.has_evidence_reading(after, item["id"])
        for item in created
    )


def test_a_re_read_does_not_count_its_document_twice(monkeypatch):
    """``fill_count`` is breadth — the documents that stated a field — and the
    vocabulary view divides it by ``len(documents_read)``. Folding a re-reading
    in as a second document made a field two of two documents state read as
    three of two."""

    ws, created = _workspace("Forced re-read counts")
    _fake(monkeypatch)
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    assert document_masters.master(reloaded, TYPE)["fields"][0]["fill_count"] == 2

    _run(reloaded, created, action="refresh")

    master = document_masters.master(workspaces.load_workspace(ws.id), TYPE)
    assert master["documents_read"] == sorted(item["id"] for item in created)
    assert master["fields"][0]["fill_count"] == 2


def test_the_stamped_schema_survives_a_forced_re_read(monkeypatch):
    """The stamp is a dependent capability, and a forced run re-expands it. Its
    reconciler reports the stored schema as already applied — which is now a
    legal thing for a reconciliation to say and was not."""

    ws, created = _workspace("Forced re-read stamp")
    _fake(monkeypatch)
    _run(ws, created)
    stamped = document_schemas.get_schema(workspaces.load_workspace(ws.id), TYPE)

    second = _run(workspaces.load_workspace(ws.id), created, action="refresh")

    assert second["status"] == "completed"
    reloaded = workspaces.load_workspace(ws.id)
    current = document_schemas.get_schema(reloaded, TYPE)
    # Unmoved: the vocabulary did not change, so neither did the version. A
    # bumped version here would orphan every extraction made under the old one.
    assert current["schema_version"] == stamped["schema_version"]
    assert current["schema_hash"] == stamped["schema_hash"]
    assert all(
        document_capabilities.has_usable_analysis(reloaded, item["id"])
        for item in created
    )


# ------------------------------------------------------------- the reconciler
def test_a_previous_runs_reading_is_not_this_calls_commit(monkeypatch):
    """The proof that made the forced pass a no-op. A document already in
    ``documents_read`` says the type was read, not by whom."""

    ws, created = _workspace("Reconcile a previous run", count=1)
    _fake(monkeypatch)
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    document_id = created[0]["id"]
    assert document_masters.has_read(reloaded, TYPE, document_id)

    reconciliation = EXECUTORS.reconcile(
        _read_request(reloaded, document_id, f"evidence_read:{TYPE}:{document_id}"),
        _read_target(reloaded, "20260906-163045-d465f7", document_id),
    )

    assert reconciliation.disposition == "not_applied"


def test_this_runs_own_reading_still_reconciles_as_applied(monkeypatch):
    """The interrupted-commit case the reconciler exists for, and the receipt it
    produces — no revision published, and legal."""

    ws, created = _workspace("Reconcile an interrupted commit", count=1)
    _fake(monkeypatch)
    _run(ws, created)

    reloaded = workspaces.load_workspace(ws.id)
    document_id = created[0]["id"]
    run_id = document_analysis.generated_record(reloaded, document_id)["agent_run_id"]
    unit_id = document_analysis.generated_record(reloaded, document_id)["unit_id"]
    request = _read_request(reloaded, document_id, unit_id)
    target = _read_target(reloaded, run_id, document_id)

    reconciliation = EXECUTORS.reconcile(request, target)
    receipt = EXECUTORS.receipt_for_reconciliation(request, reconciliation)

    assert reconciliation.disposition == "already_applied"
    assert receipt.reconciled is True
    assert receipt.workspace_revision_after == receipt.workspace_revision_before


def test_an_unread_document_is_not_applied(monkeypatch):
    """The ordinary path, unchanged: nothing on disk, so the executor runs."""

    ws, created = _workspace("Reconcile an unread document", count=1)
    _fake(monkeypatch)
    document_id = created[0]["id"]
    documents.extract_document(ws, document_id)

    reconciliation = EXECUTORS.reconcile(
        _read_request(ws, document_id, f"evidence_read:{TYPE}:{document_id}"),
        _read_target(ws, "run-1", document_id, action="analyze",
                     vocabulary_mode="accumulate"),
    )

    assert reconciliation.disposition == "not_applied"


# --------------------------------------------------------------- the master
def test_apply_reading_folds_the_same_document_in_twice_for_free(tmp_path):
    """Why the reconciler is free to answer ``not_applied``. The master is
    written outside the workspace transaction journal, so a commit interrupted
    between the fold and the reading's own persistence leaves the fold applied
    with nothing to prove it — and the executor folds again."""

    ws, created = _workspace("Fold twice", count=1)
    document_id = created[0]["id"]

    first = document_masters.apply_reading(
        ws, TYPE, document_id=document_id,
        new_fields=[_declared("invoice_number")], filled={"invoice_number": 1},
    )
    second = document_masters.apply_reading(
        ws, TYPE, document_id=document_id, filled={"invoice_number": 1},
    )

    assert first["documents_read"] == second["documents_read"] == [document_id]
    assert first["fields"][0]["fill_count"] == 1
    assert second["fields"][0]["fill_count"] == 1
    # The vocabulary is unmoved, so a reading made against it is not stale.
    assert first["master_ref"] == second["master_ref"]


def test_a_re_reading_that_states_a_new_field_still_counts_it(tmp_path):
    """Counted once per document, not suppressed. A field this call introduces
    entered at zero and this document is its first filler, whether or not the
    document is being read again."""

    ws, created = _workspace("Fold a new field", count=1)
    document_id = created[0]["id"]

    document_masters.apply_reading(
        ws, TYPE, document_id=document_id,
        new_fields=[_declared("invoice_number")], filled={"invoice_number": 1},
    )
    master = document_masters.apply_reading(
        ws, TYPE, document_id=document_id,
        new_fields=[_declared("total_amount", value_type="number")],
        filled={"invoice_number": 1, "total_amount": 1},
    )

    counts = {field["name"]: field["fill_count"] for field in master["fields"]}
    assert counts == {"invoice_number": 1, "total_amount": 1}
    assert master["documents_read"] == [document_id]
