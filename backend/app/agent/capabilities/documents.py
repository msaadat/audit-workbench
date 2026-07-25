"""Document-analysis capability group.

Owns every outcome of the authoritative document graph in
:mod:`agent.workflows.documents`: ``documents.text_ready``,
``documents.analysis_chunks_ready``, ``documents.analysis_generated``, and
``documents.analysis_reviewed``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry key for its
declared context. The dependency edges come from the authoritative graph; this
module never restates them.

Document scope resolution lives here too because every capability in the group is
scoped the same way: explicitly named documents, or — for an audit run that named
nothing — the bounded set of planning-relevant documents under the same declared
category rule the planning presets use.

Everything in this module is read-only. Unit expansion in particular never
extracts: it reads the extraction cache, so asking what work remains cannot
itself perform the work.
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import document_analysis, documents as document_service, intake
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import documents as documents_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "documents.text_ready",
    "documents.analysis_chunks_ready",
    "documents.analysis_generated",
    "documents.analysis_reviewed",
)

# The subset the audit graph also declares. Auditor review is excluded: an audit
# run must never wait on, or imply, an auditor's review of a generated analysis.
AUDIT_CAPABILITY_IDS: tuple[str, ...] = documents_workflow.AUDIT_CAPABILITY_IDS

# The bounded fallback: with no explicitly named document the workflow analyses
# at most this many planning-relevant documents. Analysis is one model turn per
# chunk, so the cap is what keeps an unscoped audit from turning a large document
# library into an unbounded provider bill.
MAX_SCOPE_DOCUMENTS = 12

# Extraction states that carry model-usable text. ``image_only`` and ``failed``
# documents are reported rather than analyzed — the existing eligibility rule.
ELIGIBLE_TEXT_STATES = frozenset({"extracted", "partial"})


@dataclass(frozen=True)
class DocumentScope:
    """The resolved document scope shared by every document capability."""

    document_ids: tuple[str, ...]
    requested: tuple[str, ...]
    unknown: tuple[str, ...]
    ambiguity: str | None = None

    @property
    def explicit(self) -> bool:
        return bool(self.requested)


def _requested_ids(scope: dict) -> tuple[str, ...]:
    """Explicit document targets from the command or a selected UI artifact."""

    names: list[str] = []
    for value in scope.get("document_ids") or []:
        text = str(value or "").strip()
        if text:
            names.append(text)
    for value in scope.get("target_refs") or []:
        ref = str(value or "").strip()
        kind, separator, item = ref.partition(":")
        if separator and kind == "document" and item.strip():
            names.append(item.strip())
    return tuple(dict.fromkeys(names))


def _planning_relevant(document: dict) -> bool:
    """The declared category rule planning material is selected by.

    Identical to the rule the planning context and APM presets constrain their
    document selectors on, so an audit that named no document analyses exactly
    the documents planning would consume — never a transaction voucher.
    """
    category = str(document.get("category") or "")
    return not category or category in intake.PLANNING_DOCUMENT_CATEGORIES


def resolve_document_scope(workspace: Workspace, scope: dict) -> DocumentScope:
    """Resolve explicitly named documents or a bounded planning-relevant set."""

    known = {str(item.get("id")): item for item in workspace.documents}
    requested = _requested_ids(scope)
    selected = [value for value in requested if value in known]
    unknown = [value for value in requested if value not in known]

    ambiguity: str | None = None
    if requested:
        document_ids = tuple(dict.fromkeys(selected))
    else:
        eligible = tuple(
            document_id
            for document_id in sorted(known)
            if _planning_relevant(known[document_id])
        )
        document_ids = eligible[:MAX_SCOPE_DOCUMENTS]
        if len(eligible) > MAX_SCOPE_DOCUMENTS:
            ambiguity = (
                f"{len(eligible)} planning-relevant documents are available and none "
                f"was named; analysing the first {MAX_SCOPE_DOCUMENTS} in identifier "
                "order. Name the documents to analyse instead."
            )
    return DocumentScope(
        document_ids=document_ids,
        requested=requested,
        unknown=tuple(dict.fromkeys(unknown)),
        ambiguity=ambiguity,
    )


def scoped_documents(workspace: Workspace, scope: dict) -> list[dict]:
    """The workspace document entries in scope, in resolved order."""

    resolved = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    return [known[document_id] for document_id in resolved.document_ids]


def _forced(scope: dict) -> bool:
    return str(scope.get("generation_mode") or "") == "force"


def _unknown_documents(document_scope: DocumentScope) -> Readiness | None:
    if document_scope.unknown:
        return Readiness(
            "blocked",
            tuple(
                f"'{document_id}' is not an imported document"
                for document_id in document_scope.unknown
            ),
        )
    return None


def analyzable(workspace: Workspace, document_id: str) -> dict | None:
    """The cached extraction for a document whose text a worker may consume.

    ``None`` means there is nothing to analyze *yet* or *ever*: no extraction has
    been cached for the current source, or the document is image-only/failed.
    Callers distinguish the two by asking for the extraction directly.
    """
    extracted = document_service.cached_extraction(workspace, document_id)
    if extracted is None or extracted.get("state") not in ELIGIBLE_TEXT_STATES:
        return None
    return extracted


def chunk_specs(workspace: Workspace, document_id: str, scope: dict) -> list[dict]:
    """The bounded source chunks one document contributes, in page order.

    The chunking is the existing ``document_analysis.analysis_chunks`` split, so
    chunk identity, page attribution, and character ranges are unchanged from the
    former runner. ``DOCUMENT_ANALYSIS_PAGE_LIMIT`` still bounds how many pages a
    single analysis covers; the omitted pages become the artifact's partial
    coverage rather than silently disappearing.
    """
    extracted = analyzable(workspace, document_id)
    if extracted is None:
        return []
    chunks = document_analysis.analysis_chunks(extracted)
    limit = page_limit(scope)
    if limit:
        allowed = set(sorted({chunk["page"] for chunk in chunks})[:limit])
        chunks = [chunk for chunk in chunks if chunk["page"] in allowed]
    return chunks


def page_limit(scope: dict) -> int:
    """The configured per-analysis page bound, or 0 when unbounded."""

    try:
        return max(0, int(scope.get("page_limit") or 0))
    except (TypeError, ValueError):
        return 0


def has_generated_analysis(workspace: Workspace, document_id: str) -> bool:
    """Whether a durable generated analysis exists for the document.

    Existence only. Whether that analysis is substantively current with respect
    to a changed source or a newer prompt is deliberately not assessed here — the
    auditor decides when to force a regeneration.
    """
    return document_analysis.generated_record(workspace, document_id) is not None


# --------------------------------------------------------------------------- #
# documents.text_ready (P9.3)
# --------------------------------------------------------------------------- #
def _text_ready(workspace: Workspace, scope: dict) -> Readiness:
    document_scope = resolve_document_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    if not document_scope.document_ids:
        # No document in scope is a satisfied outcome, not a blocked one: an
        # engagement with no documents has nothing to extract.
        return Readiness("satisfied", details={"documents": 0})
    pending = [
        document_id
        for document_id in document_scope.document_ids
        if document_service.cached_extraction(workspace, document_id) is None
    ]
    details = {
        "documents": len(document_scope.document_ids),
        "unextracted": len(pending),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(pending)} document(s) have no extracted text",),
        details=details,
    )


def _text_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    document_scope = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    return [
        UnitSpec(
            semantic_unit_id("document_text", document_id),
            "document_extraction",
            f"Extract text — {known[document_id].get('title') or document_id}",
            (f"document:{document_id}",),
            {"document_id": document_id},
        )
        for document_id in document_scope.document_ids
        if forced
        or document_service.cached_extraction(workspace, document_id) is None
    ]


def _documents_text_ready() -> Capability:
    return Capability(
        "documents.text_ready",
        "document_text",
        "Document text",
        "document_extraction",
        documents_workflow.dependencies("documents.text_ready"),
        _text_ready,
        _text_units,
        # Deterministic local extraction: no model ever sees this capability's
        # inputs, so it declares no context.
        context=None,
        invalidate_on=("documents",),
    )


# --------------------------------------------------------------------------- #
# documents.analysis_chunks_ready (P9.4 / P9.5)
# --------------------------------------------------------------------------- #
def _chunks_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Chunk maps are run-local, so readiness is the outcome they feed.

    A chunk analysis is an input to the reduced artifact, never a durable
    engagement record: the only thing outside the run that can show the mapping
    happened is the generated analysis itself. Readiness therefore reports
    existence of that artifact, and the scheduler's own durable unit state — not
    a workspace probe — is what stops a resumed run from re-mapping a chunk it
    already paid for.
    """
    document_scope = resolve_document_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    pending = [
        document_id
        for document_id in document_scope.document_ids
        if not has_generated_analysis(workspace, document_id)
        and analyzable(workspace, document_id) is not None
    ]
    details = {
        "documents": len(document_scope.document_ids),
        "unanalyzed": len(pending),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(pending)} document(s) have no analyzed source chunks",),
        details=details,
    )


def _chunk_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per bounded source chunk of every document still to analyze.

    Expansion is empty until ``documents.text_ready`` has cached an extraction,
    which is exactly what the scheduler's re-expansion between stages is for: a
    stage that materialized empty at routing time fans out for real once its
    prerequisite has run.
    """
    document_scope = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    units: list[UnitSpec] = []
    for document_id in document_scope.document_ids:
        if not forced and has_generated_analysis(workspace, document_id):
            continue
        title = known[document_id].get("title") or document_id
        for chunk in chunk_specs(workspace, document_id, scope):
            units.append(
                UnitSpec(
                    semantic_unit_id("document_chunk", document_id, chunk["id"]),
                    "document_chunk_analysis",
                    f"Analyze {title} — page {chunk['page']}",
                    (f"document:{document_id}",),
                    {
                        "document_id": document_id,
                        "chunk_id": chunk["id"],
                        "page": chunk["page"],
                        "start_character": chunk["start_character"],
                        "end_character": chunk["end_character"],
                    },
                )
            )
    return units


def _documents_analysis_chunks_ready() -> Capability:
    return Capability(
        "documents.analysis_chunks_ready",
        "document_chunks",
        "Document chunk analysis",
        "document_chunk_analysis",
        documents_workflow.dependencies("documents.analysis_chunks_ready"),
        _chunks_ready,
        _chunk_units,
        context="documents.analysis_chunk",
        # Chunks are independent of each other and never commit, so they are the
        # one capability whose units the scheduler may run concurrently.
        barrier="all_settled_parallel",
        invalidate_on=("documents",),
    )


# --------------------------------------------------------------------------- #
# documents.analysis_generated (P9.6 / P9.7)
# --------------------------------------------------------------------------- #
def _generated_ready(workspace: Workspace, scope: dict) -> Readiness:
    document_scope = resolve_document_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    analyzed = [
        document_id
        for document_id in document_scope.document_ids
        if has_generated_analysis(workspace, document_id)
    ]
    # A document with no extractable text is deliberately *not* treated as
    # satisfied. It has no analysis and never will without auditor action, so the
    # outcome stays unmet and its unit settles for review; reporting it is the
    # whole point of scoping the document into the request.
    pending = [
        document_id
        for document_id in document_scope.document_ids
        if document_id not in analyzed
    ]
    details = {
        "documents": len(document_scope.document_ids),
        "generated": len(analyzed),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(pending)} document(s) have no generated analysis",),
        details=details,
    )


def _generated_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    document_scope = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    return [
        UnitSpec(
            semantic_unit_id("document_analysis", document_id),
            "document_analysis_reduction",
            f"Consolidate analysis — {known[document_id].get('title') or document_id}",
            (f"document:{document_id}",),
            {"document_id": document_id},
        )
        for document_id in document_scope.document_ids
        if forced or not has_generated_analysis(workspace, document_id)
    ]


def _documents_analysis_generated() -> Capability:
    return Capability(
        "documents.analysis_generated",
        "document_analysis",
        "Document analysis",
        "document_analysis_reduction",
        documents_workflow.dependencies("documents.analysis_generated"),
        _generated_ready,
        _generated_units,
        context="documents.analysis_reduction",
        invalidate_on=("documents",),
    )


# --------------------------------------------------------------------------- #
# documents.analysis_reviewed (P9.8)
# --------------------------------------------------------------------------- #
def _reviewed_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Generated and auditor-reviewed are separate outcomes, deliberately.

    A generated summary is never evidence that a control operated, so nothing the
    agent does can satisfy this outcome. It is satisfied only by the auditor's own
    review decision recorded through the Documents tab.
    """
    document_scope = resolve_document_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    pending = [
        document_id
        for document_id in document_scope.document_ids
        if has_generated_analysis(workspace, document_id)
        and str(
            document_analysis.load_review(workspace, document_id).get("review_state")
            or ""
        )
        != "reviewed"
    ]
    details = {
        "documents": len(document_scope.document_ids),
        "awaiting_review": len(pending),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "review_required",
        (f"{len(pending)} generated analysis/analyses await auditor review",),
        details=details,
    )


def _review_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    document_scope = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    return [
        UnitSpec(
            semantic_unit_id("document_review", document_id),
            "document_analysis_review",
            f"Auditor review — {known[document_id].get('title') or document_id}",
            (f"document:{document_id}",),
            {"document_id": document_id},
        )
        for document_id in document_scope.document_ids
        if has_generated_analysis(workspace, document_id)
        and str(
            document_analysis.load_review(workspace, document_id).get("review_state")
            or ""
        )
        != "reviewed"
    ]


def _documents_analysis_reviewed() -> Capability:
    return Capability(
        "documents.analysis_reviewed",
        "document_review",
        "Document analysis review",
        "document_analysis_review",
        documents_workflow.dependencies("documents.analysis_reviewed"),
        _reviewed_ready,
        _review_units,
        context=None,
        invalidate_on=("documents",),
    )


# Declared auditor-judgment checkpoint for this group. An unscoped request over a
# document library larger than the bounded fallback is a question only the
# auditor can settle, and it must be settled before any capability fans out
# against a guessed scope.
DOCUMENT_SCOPE_CHECKPOINT = "document_scope"
STAGE_CHECKPOINTS: dict[str, str] = {
    "documents.text_ready": DOCUMENT_SCOPE_CHECKPOINT,
    "documents.analysis_chunks_ready": DOCUMENT_SCOPE_CHECKPOINT,
}


_BUILDERS = {
    "documents.text_ready": _documents_text_ready,
    "documents.analysis_chunks_ready": _documents_analysis_chunks_ready,
    "documents.analysis_generated": _documents_analysis_generated,
    "documents.analysis_reviewed": _documents_analysis_reviewed,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)


__all__ = [
    "AUDIT_CAPABILITY_IDS",
    "CAPABILITY_IDS",
    "DOCUMENT_SCOPE_CHECKPOINT",
    "ELIGIBLE_TEXT_STATES",
    "MAX_SCOPE_DOCUMENTS",
    "STAGE_CHECKPOINTS",
    "DocumentScope",
    "analyzable",
    "capabilities",
    "chunk_specs",
    "has_generated_analysis",
    "page_limit",
    "resolve_document_scope",
    "scoped_documents",
]
