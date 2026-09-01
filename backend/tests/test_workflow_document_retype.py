"""An auditor retype has to reach the extraction, not stop at the label.

Found on a treasury engagement. A counterparty confirmation was classified
``investment_confirmation`` and extracted under that type's schema; the auditor
retyped it to a coined ``local.internal_deal_confirmation`` and re-ran the
analysis. Sampling and induction did their part — the new type got a schema —
and then ``documents.analysis_chunks_ready`` and ``documents.analysis_generated``
each expanded to zero units, because readiness asked whether the document had an
analysis rather than whether that analysis was made under the schema for the
type the document now carries. The stored extraction kept reading
``investment_confirmation``, and the correction was half-applied.

The second half of the module is the budget that made the only offered repair
unusable: ``action: "refresh"`` re-derived every schema in the workspace while
the turn allowance was sized from the targeted documents alone.
"""

from __future__ import annotations

from app import document_analysis, document_classification as dc
from app import document_schemas, documents, llm, workspaces
from app.agent import routing, runner
from app.agent.workflows import documents as documents_workflow
from conftest import FakeAgentLLM, wait_run
from app.agent.capabilities.documents import (
    _pending_types,
    has_generated_analysis,
    has_usable_analysis,
    preparation_model_turns,
)

CONFIRMATION_FIELDS = [
    {"name": "confirmation_number", "role": "identifier",
     "value_type": "identifier", "cardinality": "one", "verbatim": True,
     "confidence": "high"},
    {"name": "notional_amount", "role": "attribute", "value_type": "number",
     "cardinality": "one", "verbatim": True, "confidence": "high"},
]


def _extracted(ws, name: str, document_type: str, *, category: str = "evidence"):
    """A document classified and extracted under ``document_type``'s schema."""

    document = documents.add_document(
        ws, name, b"Confirmation CNF-2025-0517\nNotional USD 5,000,000",
        category=category,
    )
    dc.assign(ws, str(document["id"]), document_type, assigned_by="model")
    schema = document_schemas.get_schema(ws, document_type)
    document_analysis.persist_analysis(
        ws, document,
        {"pages": [{"page": 1, "text": "Confirmation CNF-2025-0517"}]},
        {
            "analysis_profile": "structured",
            "summary_markdown": f"Read as {document_type}.",
            "audit_notes_markdown": "Structured evidence.",
            "schema_ref": {
                "document_type": schema["document_type"],
                "schema_version": schema["schema_version"],
                "schema_hash": schema["schema_hash"],
            },
            "records": [{
                "fields": [{"name": "confirmation_number", "entry": 1,
                            "value": "CNF-2025-0517", "citation": "c1"}],
                "additional_fields": [],
            }],
            "citations": [],
        },
        provider="local", model="test",
    )
    return document


# ------------------------------------------------------------------ readiness
def test_retyping_a_document_makes_its_extraction_unusable():
    """The stamp is current; it is simply not this document's stamp any more.

    ``is_current`` alone answers yes here — nothing about the
    ``investment_confirmation`` schema moved — which is exactly why an
    existence-shaped readiness reused the analysis and the chunks never
    re-expanded.
    """

    ws = workspaces.create_workspace("Retype readiness")
    document_schemas.save_schema(ws, "investment_confirmation", CONFIRMATION_FIELDS)
    ws = workspaces.load_workspace(ws.id)
    document = _extracted(ws, "cnf.txt", "investment_confirmation")
    ws = workspaces.load_workspace(ws.id)

    assert has_usable_analysis(ws, str(document["id"])) is True

    dc.retype(ws, str(document["id"]), coin="Internal deal confirmation")
    ws = workspaces.load_workspace(ws.id)

    # The old type's schema is untouched, so the stamp itself is still current.
    assert document_schemas.is_current(
        ws, document_analysis.generated_record(ws, str(document["id"]))["schema_ref"]
    ) is True
    # The analysis still exists and the auditor still reviews it. What changed is
    # whether it may be reused for a document that is no longer of that type.
    assert has_generated_analysis(ws, str(document["id"])) is True
    assert has_usable_analysis(ws, str(document["id"])) is False


def test_a_retyped_document_re_expands_both_analysis_capabilities():
    """Readiness is what re-opens the units; nothing else in the run can."""

    from app.agent.capabilities.documents import (
        _chunk_units, _chunks_ready, _generated_ready, _generated_units,
    )

    ws = workspaces.create_workspace("Retype expansion")
    document_schemas.save_schema(ws, "investment_confirmation", CONFIRMATION_FIELDS)
    ws = workspaces.load_workspace(ws.id)
    document = _extracted(ws, "cnf.txt", "investment_confirmation")
    ws = workspaces.load_workspace(ws.id)
    scope = {"document_ids": [str(document["id"])]}

    assert _generated_ready(ws, scope).state == "satisfied"
    assert _generated_units(ws, scope) == []

    dc.retype(ws, str(document["id"]), coin="Internal deal confirmation")
    ws = workspaces.load_workspace(ws.id)

    assert _chunks_ready(ws, scope).state == "missing"
    assert _generated_ready(ws, scope).state == "missing"
    assert len(_generated_units(ws, scope)) == 1
    assert _chunk_units(ws, scope) != []


def test_retyping_between_two_schema_bearing_types_is_not_a_reclassification():
    """The Phase 2a note rules out re-running analysis when the *catalog* moves.

    A document's own type changing is the other question. Coining a type must
    not re-analyze the corpus; retyping this document must re-analyze this
    document. Both hold here: the untouched sibling is left alone.
    """

    ws = workspaces.create_workspace("Retype is not reclassification")
    document_schemas.save_schema(ws, "investment_confirmation", CONFIRMATION_FIELDS)
    ws = workspaces.load_workspace(ws.id)
    retyped = _extracted(ws, "cnf-a.txt", "investment_confirmation")
    untouched = _extracted(ws, "cnf-b.txt", "investment_confirmation")
    ws = workspaces.load_workspace(ws.id)

    dc.retype(ws, str(retyped["id"]), coin="Internal deal confirmation")
    ws = workspaces.load_workspace(ws.id)

    assert has_usable_analysis(ws, str(retyped["id"])) is False
    assert has_usable_analysis(ws, str(untouched["id"])) is True


def test_a_narrative_analysis_carries_no_type_to_contradict():
    """It has no stamp, so a retype has nothing to supersede.

    Deliberate, and the counterpart of the schema case: an unstamped analysis is
    never cycle evidence, so reusing it after a retype misattributes nothing.
    """

    ws = workspaces.create_workspace("Retype narrative")
    document = documents.add_document(
        ws, "policy.txt", b"Treasury policy\nApprovals are required.",
        category="policy",
    )
    document_analysis.persist_analysis(
        ws, document, {"pages": [{"page": 1, "text": "Treasury policy"}]},
        {
            "summary_markdown": "A policy. [1]",
            "audit_notes_markdown": "Nothing is demonstrated by it alone.",
            "citations": [{"id": "1", "page": 1, "excerpt": "Treasury policy"}],
        },
        provider="local", model="test",
    )
    ws = workspaces.load_workspace(ws.id)
    dc.retype(ws, str(document["id"]), coin="Treasury policy note")
    ws = workspaces.load_workspace(ws.id)

    assert has_usable_analysis(ws, str(document["id"])) is True


# ---------------------------------------------------- forced re-derivation scope
def _typed_corpus(ws, per_type: int = 3) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = {}
    for document_type in ("investment_confirmation", "vendor_invoice"):
        document_schemas.save_schema(ws, document_type, CONFIRMATION_FIELDS)
    reloaded = workspaces.load_workspace(ws.id)
    for document_type in ("investment_confirmation", "vendor_invoice"):
        for index in range(per_type):
            document = _extracted(
                reloaded, f"{document_type}-{index}.txt", document_type
            )
            by_type.setdefault(document_type, []).append(str(document["id"]))
    return by_type


def test_forcing_re_derives_only_the_targeted_documents_types():
    """A one-document refresh must not re-derive the whole workspace.

    Re-derivation bumps a schema's version, and every extraction stamped against
    the old one becomes ``stale_schema_reference``. Doing that for types the run
    was never pointed at is how a one-document repair orphaned 68 completed
    extractions on the engagement this came from.
    """

    ws = workspaces.create_workspace("Forced re-derivation scope")
    by_type = _typed_corpus(ws)
    ws = workspaces.load_workspace(ws.id)
    forced = {
        "generation_mode": "force",
        "document_ids": [by_type["investment_confirmation"][0]],
    }

    assert _pending_types(ws, forced) == ["investment_confirmation"]

    # A whole-workspace refresh still re-derives the whole workspace, because
    # then every type is a targeted type.
    assert _pending_types(
        ws,
        {
            "generation_mode": "force",
            "document_ids": [
                document_id for ids in by_type.values() for document_id in ids
            ],
        },
    ) == ["investment_confirmation", "vendor_invoice"]


def test_a_type_with_no_schema_is_induced_whether_or_not_it_was_targeted():
    """Nothing is orphaned by inducing what does not exist yet."""

    ws = workspaces.create_workspace("Untargeted gap")
    by_type = _typed_corpus(ws)
    document_schemas.remove_schema(ws, "vendor_invoice")
    ws = workspaces.load_workspace(ws.id)

    assert _pending_types(
        ws,
        {
            "generation_mode": "force",
            "document_ids": [by_type["investment_confirmation"][0]],
        },
    ) == ["investment_confirmation", "vendor_invoice"]


# ------------------------------------------------------------------- budgeting
def test_the_turn_budget_pays_for_the_schema_work_the_run_will_do():
    """Preparation is model-backed and was outside the arithmetic entirely.

    The measured failure: one targeted document, a budget of 7, and six turns
    spent re-sampling schemas before the analysis it was started for was ever
    reached — "model turn limit reached", with nothing to show for the run.
    """

    ws = workspaces.create_workspace("Forced refresh budget")
    by_type = _typed_corpus(ws)
    ws = workspaces.load_workspace(ws.id)
    target = by_type["investment_confirmation"][0]
    forced = {"generation_mode": "force", "document_ids": [target]}
    reuse = {"generation_mode": "reuse_existing", "document_ids": [target]}

    preparation = preparation_model_turns(ws, forced)
    # One classification for the targeted document, plus its type's samples and
    # the freeze that consumes them. Nothing for the type it was not pointed at.
    assert preparation > 1
    assert routing._document_model_turns(ws, forced) >= preparation

    # Reuse pays for no preparation at all: every document is classified and
    # every type already has a current schema.
    assert preparation_model_turns(ws, reuse) == 0
    assert routing._document_model_turns(ws, forced) > routing._document_model_turns(
        ws, reuse
    )


# --------------------------------------------------------------------- end to end
CLASSIFY_TAG = "agent:document_classification"
SAMPLE_TAG = "agent:document_schema_sample"
STRUCTURED_TAG = "agent:document_analysis_structured"


def _sampled(name: str) -> dict:
    return {
        "name": name,
        "role": "identifier",
        "value_type": "identifier",
        "cardinality": "one",
        "verbatim": True,
        "confidence": "high",
        "label": name.replace("_", " ").title(),
    }


def _scripted(monkeypatch, *, classified, sample_fields):
    """A model that names every document ``classified`` and reads one field."""

    def sample(_messages):
        return {"fields": [_sampled(name) for name in sample_fields()]}

    fake = FakeAgentLLM({
        CLASSIFY_TAG: {
            "document_type": classified,
            "document_type_other": "",
            "confidence": "high",
            "rationale": "The header names it.",
        },
        SAMPLE_TAG: sample,
        STRUCTURED_TAG: {
            "records": [{
                "fields": [],
                "additional_fields": [{
                    "name": "stated_reference", "value_type": "identifier",
                    "value": "CNF-2025-0517", "entry": 1, "citation": "1",
                }],
            }],
            "audit_notes": [],
            "citations": [{"id": "1", "page": 1, "excerpt": "Confirmation"}],
        },
    })
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    return fake


def _analysis_run(ws, document_ids, action="analyze"):
    run = runner.start_command_run(
        ws, "auto",
        {
            "source": "tab_button",
            "text": "Analyse.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": "force" if action == "refresh" else "reuse_existing",
        },
        context={"document_ids": list(document_ids), "action": action},
    )
    return wait_run(ws, run["id"])


def test_analyze_repairs_a_retyped_document_without_touching_its_siblings(monkeypatch):
    """The whole point: ``analyze`` is the repair, and it costs one document.

    Before this, the only thing that fixed a retyped document was deleting
    ``Documents/.analysis/<id>/`` by hand — ``analyze`` reused the stale
    extraction and ``refresh`` re-derived the workspace.
    """

    _scripted(monkeypatch, classified="investment_confirmation",
              sample_fields=lambda: ["confirmation_number"])
    ws = workspaces.create_workspace("Retype end to end")
    created = [
        documents.add_document(
            ws, f"cnf-{index}.txt",
            f"Confirmation CNF-2025-051{index}\nNotional USD {index}00".encode(),
            category="evidence",
        )
        for index in range(2)
    ]
    assert _analysis_run(ws, [item["id"] for item in created])["status"] == "completed"

    ws = workspaces.load_workspace(ws.id)
    target, sibling = (str(item["id"]) for item in created)
    stamped = document_analysis.generated_record(ws, target)["schema_ref"]
    assert stamped["document_type"] == "investment_confirmation"

    dc.retype(ws, target, coin="Internal deal confirmation")
    ws = workspaces.load_workspace(ws.id)

    # Plain ``analyze``: no force, no schema re-derivation, no hand-deletion.
    assert _analysis_run(ws, [target])["status"] == "completed"
    ws = workspaces.load_workspace(ws.id)

    repaired = document_analysis.generated_record(ws, target)["schema_ref"]
    assert repaired["document_type"] == "local.internal_deal_confirmation"
    assert document_schemas.is_current(ws, repaired)
    # The sibling was never in scope and is still read under its own type.
    assert document_analysis.generated_record(ws, sibling)["schema_ref"] == stamped
    assert has_usable_analysis(ws, sibling) is True


def test_a_one_document_refresh_leaves_the_other_types_extractions_intact(monkeypatch):
    """Re-derivation is the destructive half of ``refresh``; scope bounds it.

    The sampler answers with a different field the second time, so any type it
    re-reads is frozen at a new version and every extraction stamped against the
    old one is orphaned. Only the targeted document's type may be re-read.
    """

    fields = {"value": ["confirmation_number"]}
    _scripted(monkeypatch, classified="investment_confirmation",
              sample_fields=lambda: list(fields["value"]))
    ws = workspaces.create_workspace("Refresh blast radius")
    confirmations = [
        documents.add_document(
            ws, f"cnf-{index}.txt",
            f"Confirmation CNF-{index}\nNotional USD {index}00".encode(),
            category="evidence",
        )
        for index in range(2)
    ]
    assert _analysis_run(
        ws, [item["id"] for item in confirmations]
    )["status"] == "completed"

    # A second type, extracted and then left alone for the rest of the test.
    ws = workspaces.load_workspace(ws.id)
    document_schemas.save_schema(ws, "vendor_invoice", CONFIRMATION_FIELDS)
    ws = workspaces.load_workspace(ws.id)
    invoice = _extracted(ws, "inv.txt", "vendor_invoice")
    ws = workspaces.load_workspace(ws.id)
    invoice_stamp = document_analysis.generated_record(ws, str(invoice["id"]))["schema_ref"]

    # Re-sampling would now freeze a different schema for whatever it reads.
    fields["value"] = ["confirmation_number", "trade_reference"]
    target = str(confirmations[0]["id"])
    _analysis_run(ws, [target], action="refresh")
    ws = workspaces.load_workspace(ws.id)

    # The untargeted type was never re-read, so its extraction is still evidence.
    assert document_schemas.get_schema(ws, "vendor_invoice")["schema_version"] == 1
    assert document_analysis.generated_record(
        ws, str(invoice["id"])
    )["schema_ref"] == invoice_stamp
    assert has_usable_analysis(ws, str(invoice["id"])) is True


def test_a_forced_refresh_is_budgeted_for_the_schema_work_it_starts(monkeypatch):
    """It has to be able to finish. Previously it could not."""

    _scripted(monkeypatch, classified="investment_confirmation",
              sample_fields=lambda: ["confirmation_number"])
    ws = workspaces.create_workspace("Refresh budget end to end")
    created = [
        documents.add_document(
            ws, f"cnf-{index}.txt",
            f"Confirmation CNF-{index}\nNotional USD {index}00".encode(),
            category="evidence",
        )
        for index in range(4)
    ]
    _analysis_run(ws, [item["id"] for item in created])
    ws = workspaces.load_workspace(ws.id)

    refreshed = _analysis_run(ws, [str(created[0]["id"])], action="refresh")

    assert "model turn limit reached" not in str(refreshed.get("error") or "")
    # Classification, the samples, the freeze, the chunk and the reduction all
    # fit — the allowance is no longer sized as though preparation were free.
    assert refreshed["usage"]["llm_turns"] <= refreshed["limits"]["max_model_turns"]
    assert refreshed["limits"]["max_model_turns"] >= preparation_model_turns(
        ws, {"generation_mode": "force", "document_ids": [str(created[0]["id"])]}
    )


# ------------------------------------------------------------ commit interlock
def test_a_retype_mid_run_conflicts_the_commit_rather_than_storing_it():
    """The same window as a schema re-derived in flight, from the other side.

    Readiness would re-expand the chunks on the next run either way. Storing an
    extraction already known to be under the wrong type — and reporting the unit
    as succeeded — is what this refuses.
    """

    import pytest

    from app.agent.executors import documents as document_executors
    from app.agent.executors.model import ExecutorRequest
    from app.workspace_transactions import parent_hashes
    from app.workspaces import WorkspaceError

    ws = workspaces.create_workspace("Retype mid-run")
    document_schemas.save_schema(ws, "investment_confirmation", CONFIRMATION_FIELDS)
    ws = workspaces.load_workspace(ws.id)
    document = documents.add_document(
        ws, "cnf.txt", b"Confirmation CNF-2025-0517\nNotional USD 5,000,000",
        category="evidence",
    )
    dc.assign(ws, str(document["id"]), "investment_confirmation", assigned_by="model")
    ws = workspaces.load_workspace(ws.id)
    schema = document_schemas.get_schema(ws, "investment_confirmation")
    extracted = documents.extract_document(ws, str(document["id"]))

    def _request() -> ExecutorRequest:
        parent_ref = document_executors.document_ref(str(document["id"]))
        return ExecutorRequest(
            executor_id=document_executors.ANALYSIS_EXECUTOR_ID,
            capability_id="documents.analysis_generated",
            unit_id="document_analysis:x",
            proposal={
                "summary_markdown": "Read as investment_confirmation.",
                "audit_notes_markdown": "Structured evidence.",
                "citations": [],
                "coverage": {"state": "complete", "analyzed_pages": [1],
                             "omitted_pages": []},
                "analysis_profile": "structured",
                "schema_ref": {
                    "document_type": schema["document_type"],
                    "schema_version": schema["schema_version"],
                    "schema_hash": schema["schema_hash"],
                },
                "records": [],
            },
            expected_revision=ws.revision,
            expected_parents=parent_hashes(ws, [parent_ref]),
            activity={},
        )

    target = document_executors.DocumentAnalysisExecutorTarget(
        ws, "run-1", str(document["id"]), extracted=extracted
    )
    # Nothing has changed yet, so the same proposal validates.
    assert document_executors._validated_analysis(_request(), target)

    dc.retype(ws, str(document["id"]), coin="Internal deal confirmation")
    reloaded = workspaces.load_workspace(ws.id)
    retyped_target = document_executors.DocumentAnalysisExecutorTarget(
        reloaded, "run-1", str(document["id"]), extracted=extracted
    )

    with pytest.raises(WorkspaceError, match="local.internal_deal_confirmation"):
        document_executors._validated_analysis(_request(), retyped_target)
