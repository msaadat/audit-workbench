"""Document-analysis capability group.

Owns every outcome of the authoritative document graph in
:mod:`agent.workflows.documents`: ``documents.text_ready``,
``documents.analysis_chunks_ready``, ``documents.analysis_generated``, and
``documents.analysis_reviewed``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry key for its
declared context. The dependency edges come from the authoritative graph; this
module never restates them.

Document scope resolution lives here too. Standalone document analysis defaults
to every imported document; audit planning uses the bounded planning-relevant
subset that its context presets consume.

Everything in this module is read-only. Unit expansion in particular never
extracts: it reads the extraction cache, so asking what work remains cannot
itself perform the work.
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import (
    document_analysis,
    document_classification,
    document_masters,
    document_media,
    document_schemas,
    document_types,
    documents as document_service,
    intake,
)
from ...text import counted, verb
from ...workspaces import Workspace
from ..context import presets
from ..workflow import (
    READ_REPAIR_ATTEMPTS,
    Capability,
    Readiness,
    UnitSpec,
    semantic_unit_id,
)
from ..workflows import documents as documents_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "documents.text_ready",
    "documents.categorized",
    "documents.types_classified",
    "documents.evidence_read",
    "documents.schemas_stamped",
    "documents.analysis_chunks_ready",
    "documents.analysis_generated",
    "documents.analysis_reviewed",
)

# The subset the audit graph also declares. Auditor review is excluded: an audit
# run must never wait on, or imply, an auditor's review of a generated analysis.
AUDIT_CAPABILITY_IDS: tuple[str, ...] = documents_workflow.AUDIT_CAPABILITY_IDS

# The bounded planning fallback: with no explicitly named document, an audit
# planning workflow analyzes at most this many planning-relevant documents.
MAX_SCOPE_DOCUMENTS = 12

# Extraction states that carry model-usable text. ``image_only`` and ``failed``
# documents are reported rather than analyzed — the existing eligibility rule.
ELIGIBLE_TEXT_STATES = frozenset({"extracted", "partial"})
ELIGIBLE_CONTENT_STATES = frozenset({"extracted", "partial", "image_only"})


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
    """Whether a document is eligible for unscoped audit planning.

    Reads the entry's mirrored copy rather than the sidecar, because callers hold
    a document dict. An unset category is relevant — a document nothing has read
    yet must stay in scope, or the stage that would categorize it never sees it.
    """

    category = str(document.get("category") or "")
    return not category or category in intake.PLANNING_DOCUMENT_CATEGORIES


def resolve_document_scope(workspace: Workspace, scope: dict) -> DocumentScope:
    """Resolve the document workflow scope for its owning route.

    ``document_scope_mode == "planning"`` preserves the bounded
    planning-relevant default used by the audit workflow. The standalone
    document-analysis workflow defaults to every imported document, including
    transaction evidence, which is read under its type's induced schema.
    """

    known = {str(item.get("id")): item for item in workspace.documents}
    requested = _requested_ids(scope)
    selected = [value for value in requested if value in known]
    unknown = [value for value in requested if value not in known]

    ambiguity: str | None = None
    if requested:
        document_ids = tuple(dict.fromkeys(selected))
    elif scope.get("document_scope_mode") == "planning":
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
    else:
        document_ids = tuple(sorted(known))
    return DocumentScope(
        document_ids=document_ids,
        requested=requested,
        unknown=tuple(dict.fromkeys(unknown)),
        ambiguity=ambiguity,
    )


def corpus_scope(workspace: Workspace, scope: dict) -> DocumentScope:
    """Every readable document, whatever the run's analysis scope.

    Three capabilities run over the corpus rather than the planning-scoped
    subset: text extraction, categorization and type classification. What a
    document *is* is not a planning question, and asking it of the planning
    subset made an audit run answer it about nothing.

    The bound this lifts is real and stays where it belongs. ``planning`` mode
    exists so an unscoped audit does not push eighty documents through the
    expensive analysis pass, and ``documents.analysis_chunks_ready`` still honours
    it. But the cheap stages ahead of it were bounded by the same rule, and
    ``_planning_relevant`` is disjoint from the evidence category by construction
    — so an audit run extracted no evidence text, classified no evidence, induced
    no schema, and ``documents.schemas_stamped`` reported satisfied having
    induced nothing. Both Phase 8 edges into ``planning.rcm_ready`` were then
    satisfied by an empty vocabulary, and the RCM was written against fields no
    document stated.

    An explicitly named document set still wins: naming files is the auditor
    saying which documents this run is about, and that is not overridden here.
    """

    if _requested_ids(scope):
        return resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    return DocumentScope(
        document_ids=tuple(sorted(known)),
        requested=(),
        unknown=(),
        ambiguity=None,
    )


def scoped_documents(workspace: Workspace, scope: dict) -> list[dict]:
    """The workspace document entries in scope, in resolved order."""

    resolved = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    return [known[document_id] for document_id in resolved.document_ids]


def _forced(scope: dict) -> bool:
    return str(scope.get("generation_mode") or "") == "force"


#: What a forced run is allowed to do to a type's vocabulary. ``force`` has to
#: split, because under a master the one button that exists today is being asked
#: two different questions and can only answer one of them.
#:
#: The old scoping trick — re-derive only the targeted documents' own types —
#: rested on a schema coming from a *sample*, which made re-deriving one type a
#: couple of documents' work. A master comes from every document of the type, in
#: order, so "re-read this document" and "possibly move the vocabulary" are no
#: longer separable and there is nothing left for the trick to scope.
#: Three, not two, and the third is the ordinary one. ``accumulate`` is what an
#: unforced pass does: the master grows as the type is read, which is the whole
#: mechanism. The other two are the two halves ``force`` had to split into.
VOCABULARY_MODES = ("accumulate", "frozen", "rebuild")


def vocabulary_mode(scope: dict) -> str:
    """What this pass may do to a type's vocabulary.

    ``accumulate``          the ordinary read. A document states something the
                            master has no place for and the master takes the
                            field. Additions are monotone and cannot invalidate
                            an earlier reading.
    ``frozen``              ``refresh``. The targeted documents are re-read under
                            exactly the vocabulary their siblings were read
                            under, so nothing about them is disturbed and the
                            action stays cheap by construction. Its *refusal* is
                            the useful part: a refresh is asked for because
                            something looks wrong, and one thing that can be
                            wrong is that the master has no place for what this
                            document states — which a frozen re-read cannot fix
                            and would otherwise fail at silently, reading the
                            document a second time under the same blind spot.
    ``rebuild``             ``revise_vocabulary``. An ordinary read pass over a
                            narrower corpus, from the start in order, which is
                            what keeps ``introduced_at`` meaningful and the
                            sweep bounded. Appending instead would leave indices
                            that no longer describe what any document was asked.

    An unforced run is always ``accumulate``: a first pass has nothing to freeze,
    and treating it as frozen would refuse every field the corpus states and
    stamp an empty vocabulary.
    """

    if not _forced(scope):
        return "accumulate"
    value = str(scope.get("vocabulary_mode") or "frozen")
    return value if value in VOCABULARY_MODES else "frozen"


def _rebuilding(scope: dict) -> bool:
    return vocabulary_mode(scope) == "rebuild"


def _revision_types(workspace: Workspace, scope: dict) -> set[str] | None:
    """The types a forced run may move, or None for "every type in scope".

    ``_pending_types`` computed exactly this set and used it to scope schema
    *derivation*; ``revise_vocabulary`` feeds it back into the *document* scope
    instead — every document of those types — which is what makes the expensive
    action expensive in the honest way rather than a small button quietly doing
    it.
    """

    if not _forced(scope):
        return None
    requested = _requested_ids(scope)
    if not requested:
        return None
    inducible = set(document_classification.types_for_induction(workspace))
    return {
        document_classification.document_type(workspace, document_id)
        for document_id in requested
    } & inducible


def _revision_documents(workspace: Workspace, scope: dict) -> set[str] | None:
    """Which documents a forced read re-reads, or None for the whole scope.

    A refresh re-reads the documents it was pointed at and no others. A
    ``revise_vocabulary`` widens to every document of their types, because a
    master is rebuilt from the pass rather than patched — and re-reading
    eighteen payment instructions to fix one document's vocabulary is what the
    repair actually costs. Neither action surprises anyone: one is a document,
    one is a type, and the expensive one is only ever reached deliberately.
    """

    if not _forced(scope):
        return None
    requested = _requested_ids(scope)
    if not requested:
        return None
    if not _rebuilding(scope):
        return set(requested)
    types = _revision_types(workspace, scope) or set()
    return {
        str(document.get("id"))
        for document in workspace.documents
        if document_classification.document_type(workspace, str(document.get("id")))
        in types
    } | set(requested)


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
    if extracted is None or extracted.get("state") not in ELIGIBLE_CONTENT_STATES:
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
    chunks = [
        chunk
        for chunk in document_analysis.analysis_chunks(extracted)
        if not any(
            bool(page.get("image_only"))
            or bool(page.get("no_usable_text_no_image"))
            for page in extracted.get("pages") or []
            if int(page.get("page") or 0) == int(chunk.get("page") or 0)
        )
    ]
    limit = page_limit(scope)
    if limit:
        allowed = set(sorted({chunk["page"] for chunk in chunks})[:limit])
        chunks = [chunk for chunk in chunks if chunk["page"] in allowed]
    return chunks


def visual_page_limit(scope: dict) -> int:
    try:
        return min(
            document_media.MAX_VISUAL_PAGES,
            max(
                1,
                int(
                    scope.get("visual_page_limit")
                    or document_media.MAX_VISUAL_PAGES
                ),
            ),
        )
    except (TypeError, ValueError):
        return document_media.MAX_VISUAL_PAGES


def _full_visual_coverage(scope: dict, document_id: str) -> bool:
    return document_id in {
        str(value)
        for value in scope.get("full_visual_document_ids") or []
    }


def _visual_page(
    document: dict,
    extracted: dict,
    page: dict,
    scope: dict,
) -> tuple[bool, str | None]:
    """Apply the plan's four-clause visual routing predicate."""

    suffix = str(extracted.get("source_suffix") or "").lower()
    standalone = suffix in document_service.IMAGE_SUFFIXES
    if standalone:
        return True, None
    if suffix == ".docx" and bool(page.get("image_only")):
        return False, "document_visual_source_unsupported"
    if bool(page.get("image_only")):
        return True, None
    if suffix == ".pdf" and bool(page.get("no_usable_text_no_image")):
        return True, None
    if _full_visual_coverage(scope, str(document.get("id"))):
        return True, None
    return False, None


def analysis_unit_specs(
    workspace: Workspace, document_id: str, scope: dict
) -> list[dict]:
    """Generalized text and visual map units in document source order."""

    extracted = analyzable(workspace, document_id)
    if extracted is None:
        return []
    document = next(
        item for item in workspace.documents if str(item.get("id")) == document_id
    )
    text_chunks = chunk_specs(workspace, document_id, scope)
    # There is one text kind left. ``analysis_profile`` asked two questions — is
    # this document transaction evidence, and does its type have a schema — and
    # routed to the structured profile only when both said yes. The second
    # disappears under 4b.1: a schema is now an *output* of reading the evidence
    # rather than an input to it, so there is nothing to ask at routing time.
    # And the first moves to ``evidence_read``'s unit generation, where it is the
    # only question asked — this pass carries planning prose, which needs no
    # vocabulary, and excludes transaction evidence in both modalities.
    text_kind = "document_chunk_analysis"
    by_page: dict[int, list[dict]] = {}
    for chunk in text_chunks:
        by_page.setdefault(int(chunk["page"]), []).append(
            {**chunk, "kind": text_kind, "modality": "text"}
        )
    routed_visual: list[tuple[dict, str | None]] = []
    for page in extracted.get("pages") or []:
        selected, unsupported = _visual_page(document, extracted, page, scope)
        if selected or unsupported:
            routed_visual.append((page, unsupported))
    limit = visual_page_limit(scope)
    allowed_visual_pages = {
        int(page.get("page") or 0)
        for page, unsupported in routed_visual[:limit]
        if unsupported is None
    }
    unsupported_by_page = {
        int(page.get("page") or 0): unsupported
        for page, unsupported in routed_visual
        if unsupported
    }
    values: list[dict] = []
    for page in extracted.get("pages") or []:
        page_no = int(page.get("page") or 0)
        values.extend(by_page.get(page_no, ()))
        if page_no in unsupported_by_page:
            values.append(
                {
                    "id": f"VISUAL-UNSUPPORTED-{page_no:04d}",
                    "kind": "document_visual_page_analysis",
                    "modality": "image",
                    "page": page_no,
                    "frame": page_no,
                    "prepared_set_identity": document_media.planned_prepared_set_hash(
                        str(document.get("sha1") or ""), page_no
                    ),
                    "unsupported_reason": unsupported_by_page[page_no],
                }
            )
        elif page_no in allowed_visual_pages:
            prepared_set_identity = document_media.planned_prepared_set_hash(
                str(document.get("sha1") or ""), page_no
            )
            values.append(
                {
                    "id": f"VISUAL-{page_no:04d}-{prepared_set_identity[-12:]}",
                    "kind": "document_visual_page_analysis",
                    "modality": "image",
                    "page": page_no,
                    "frame": page_no,
                    "prepared_set_identity": prepared_set_identity,
                    "unsupported_reason": None,
                }
            )
    return values


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

    A superseded *schema* stamp is the one exception, and it is a different
    question — see :func:`has_usable_analysis`.
    """
    return document_analysis.generated_record(workspace, document_id) is not None


def has_usable_analysis(workspace: Workspace, document_id: str) -> bool:
    """Whether a generated analysis exists that can still be used as evidence.

    A stale source or an older prompt leaves an analysis that is merely out of
    date, and the auditor decides what to do about it. A structured extraction
    whose ``schema_ref`` no longer matches the live schema is not out of date —
    it is excluded from evidence outright, because reinterpreting values under
    fields they were never read against is exactly what the stamp prevents.

    Readiness has to ask the second question or the exclusion is silent: with
    five schemas re-derived, every voucher extraction in an engagement was
    superseded, ``documents.analysis_generated`` still reported satisfied,
    every capability was reused, no unit expanded, and the run completed having
    left the workspace with no usable cycle evidence at all. The interlock that
    would have re-generated each chunk — the schema descriptor moving the unit's
    input hash — never got the chance, because the capability was reused whole
    before any unit ran.

    A retype asks the same question from the other side, and the stamp answers
    it only if the *type* on it is compared too. An extraction stamped
    ``investment_confirmation`` is still current under that type's schema after
    an auditor retypes the document to ``local.internal_deal_confirmation`` —
    nothing about that schema moved — so an existence-shaped check reuses it,
    the chunks never re-expand, and the correction is half-applied: the type
    changes, the extraction stays under the old type's fields.

    This is *not* the reclassification case the Phase 2a note in
    ``docs/dynamic-cycle-contracts.md`` rules out. That note is about the
    **catalog** changing — re-examining what an ``other`` document might be is a
    question about the vocabulary, and the answer has no bearing on what the map
    worker extracted, so ``POST /documents/reclassify`` requests only
    ``documents.types_classified``. Here the **document's own type** changed, so
    the fields it was read against are the wrong fields. Conflating the two
    would either re-analyze a corpus every time a type is coined, or leave every
    auditor correction half-applied.
    """

    record = document_analysis.generated_record(workspace, document_id)
    if record is None:
        return False
    if str(record.get("analysis_profile") or "") != "structured":
        return True
    return document_schemas.is_current_for(
        workspace,
        record.get("schema_ref"),
        document_classification.document_type(workspace, document_id),
    )


# --------------------------------------------------------------------------- #
# documents.text_ready (P9.3)
# --------------------------------------------------------------------------- #
def _text_ready(workspace: Workspace, scope: dict) -> Readiness:
    document_scope = corpus_scope(workspace, scope)
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
        (f"{counted(len(pending), 'document')} {verb(len(pending), 'has', 'have')} no extracted text",),
        details=details,
    )


def _text_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    document_scope = corpus_scope(workspace, scope)
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
        "Document content",
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
# documents.categorized
# --------------------------------------------------------------------------- #
def _categorized_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Every readable document says what this engagement holds it as.

    Unlike the type, there is no truthful "none of these fits": the four values
    partition the corpus by construction, so a document without one is a gap and
    is reported as one rather than passed over.
    """

    document_scope = corpus_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    scoped = set(document_scope.document_ids)
    pending = [
        document_id
        for document_id in document_classification.uncategorized_ids(workspace)
        if document_id in scoped
    ]
    categorized = [
        document_id
        for document_id in scoped
        if document_classification.is_categorized(workspace, document_id)
    ]
    details = {
        "documents": len(scoped),
        "categorized": len(categorized),
        "evidence": len(document_classification.transaction_evidence(workspace)),
    }
    if pending:
        return Readiness(
            "missing",
            (
                f"{counted(len(pending), 'document')} "
                f"{verb(len(pending), 'has', 'have')} no category",
            ),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _categorized_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per document still to be categorized.

    The unit carries the opening page it will be asked about, from the same
    ``classification_text`` the type worker reads, so both calls agree on what
    "page one" means and a document is never categorized from a wider window
    than it is typed from.
    """

    document_scope = corpus_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    scoped = set(document_scope.document_ids)
    # A forced regeneration does **not** re-ask this. Every other document stage
    # widens under force, and this one deliberately does not: re-categorizing
    # can move a document across the planning/evidence partition mid-run, which
    # takes its type, its schema and its extraction with it — a refresh asking
    # for one document's analysis would silently invalidate the vocabulary the
    # rest of the corpus was read under. Correcting a category is an auditor
    # assignment, the same answer the type gives, and it is not overwritten by
    # any rerun.
    candidates = [
        document_id
        for document_id in document_classification.uncategorized_ids(workspace)
        if document_id in scoped and document_id in known
    ]
    return [
        UnitSpec(
            semantic_unit_id("document_category", document_id),
            "document_category",
            f"Classify document — {known[document_id].get('title') or document_id}",
            (f"document:{document_id}",),
            {
                "document_id": document_id,
                "title": str(known[document_id].get("title") or ""),
                "text": document_classification.classification_text(
                    workspace, document_id
                ),
            },
        )
        for document_id in dict.fromkeys(candidates)
    ]


def _documents_categorized() -> Capability:
    return Capability(
        "documents.categorized",
        "document_categories",
        "Document classification",
        "document_category",
        documents_workflow.dependencies("documents.categorized"),
        _categorized_ready,
        _categorized_units,
        context={"document_category": "documents.category"},
        # Sequential, for the reason the type capability is: the commit mirrors
        # the category onto the shared ``documents`` collection so the readers
        # that hold a document dict keep working, and two units landing at once
        # would race on it. Independence of inputs is not independence of
        # commits.
        invalidate_on=("documents",),
    )


# --------------------------------------------------------------------------- #
# documents.types_classified
# --------------------------------------------------------------------------- #
def _classified_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Every scoped document with usable text carries a document type.

    An ``other`` assignment satisfies this. It is a truthful answer — nothing in
    the catalog fits — and blocking on it would make an engagement wait for an
    auditor to review a bucket they may reasonably leave alone. The gap is
    reported instead, in ``details``, so a workspace whose evidence never became
    classifiable says so rather than passing quietly.
    """

    document_scope = corpus_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    scoped = set(document_scope.document_ids)
    pending = [
        document_id
        for document_id in document_classification.unclassified_ids(workspace)
        if document_id in scoped
    ]
    summary = document_classification.summary(workspace)
    details = {
        "documents": len(scoped),
        "classified": len(scoped) - len(pending),
        "other": summary["other"],
        "types_present": summary["types_present"],
    }
    if pending:
        return Readiness(
            "missing",
            (
                f"{counted(len(pending), 'document')} "
                f"{verb(len(pending), 'has', 'have')} no document type",
            ),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _classified_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per document needing a type, plus any ``other`` a rerun may revisit.

    The offered catalog travels on the unit input rather than being read from the
    global list at validation time, because a workspace's coined types are part
    of what the prompt showed. It is also what re-expands these units when an
    auditor coins a type: the catalog is in ``input_payload``, so ``input_sha1``
    moves and the remaining ``other`` documents are swept again.
    """

    document_scope = corpus_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    scoped = set(document_scope.document_ids)
    # Classification runs over transaction evidence only. A forced regeneration
    # widens *which* of those are re-asked, never what the question applies to —
    # otherwise `refresh` would classify the prose that the normal path, the
    # readiness check and the summary all agree to leave alone.
    evidence = {
        str(document.get("id"))
        for document in document_classification.transaction_evidence(workspace)
    }
    forced = _forced(scope)
    if forced:
        candidates = [
            document_id for document_id in document_scope.document_ids
            if document_id in known and document_id in evidence
        ]
    else:
        candidates = [
            document_id
            for document_id in (
                *document_classification.unclassified_ids(workspace),
                *document_classification.reclassifiable_ids(workspace),
            )
            if document_id in scoped
        ]
    local = document_schemas.local_types(workspace)
    selectable = list(document_schemas.effective_type_ids(workspace))
    catalog = document_types.prompt_catalog(local_types=local)
    signature = document_classification.catalog_signature(workspace)
    return [
        UnitSpec(
            semantic_unit_id("document_classification", document_id),
            "document_classification",
            f"Identify document type — {known[document_id].get('title') or document_id}",
            (f"document:{document_id}",),
            {
                "document_id": document_id,
                "title": str(known[document_id].get("title") or ""),
                "text": document_classification.classification_text(
                    workspace, document_id
                ),
                "selectable_types": selectable,
                "catalog": catalog,
                "catalog_sha1": signature,
            },
        )
        for document_id in dict.fromkeys(candidates)
    ]


def _documents_types_classified() -> Capability:
    return Capability(
        "documents.types_classified",
        "document_types",
        "Document types",
        "document_classification",
        documents_workflow.dependencies("documents.types_classified"),
        _classified_ready,
        _classified_units,
        context={"document_classification": "documents.classification"},
        # Sequential, despite each document being classified independently of
        # every other. The assignment commits onto the shared ``documents``
        # artifact collection, so two units landing at once would race on the
        # same collection — which is exactly what the parallel barrier asserts
        # cannot happen. Independence of *inputs* is not independence of commits.
        invalidate_on=("documents",),
    )


# --------------------------------------------------------------------------- #
# documents.evidence_read / documents.schemas_stamped (4b.1)
# --------------------------------------------------------------------------- #
#
# One sequential unit per evidence document, then one stamp per type. Two
# capabilities rather than one, for the reason induction already paid for: units
# within a stage execute in sorted *id* order, so a capability holding both the
# readings and the freeze binds the freeze first — ``document_schema:x`` sorts
# before ``document_schema_sample:x:y`` because ``:`` precedes ``_`` — and reads
# back nothing. Making the stamp a dependent capability is what makes the
# ordering something the scheduler honours rather than something a sort order
# has to be trusted for.
def evidence_read_text(workspace: Workspace, document_id: str) -> str:
    """The whole document's text, in page order, bounded by the read's window.

    Not ``induction_text``: that is a 3-page, 12,000-character sample window, and
    a read that produces citations across a document needs the document. The
    bound is applied by :func:`read_over_window`, which reports rather than
    truncating — a citation binds to text the worker saw, and a master built
    from a clipped document would record absence for pages nobody read.
    """

    extracted = analyzable(workspace, document_id)
    if extracted is None:
        return ""
    pages = sorted(
        (page for page in (extracted.get("pages") or []) if page.get("text")),
        key=lambda page: int(page.get("page") or 0),
    )
    return "\n\n".join(str(page.get("text") or "").strip() for page in pages).strip()


def evidence_read_pages(workspace: Workspace, document_id: str) -> list[int]:
    extracted = analyzable(workspace, document_id)
    if extracted is None:
        return []
    return sorted(
        int(page.get("page") or 0)
        for page in (extracted.get("pages") or [])
        if page.get("text")
    )


def evidence_read_media(
    workspace: Workspace, document_id: str, scope: dict
) -> list[dict]:
    """The document's visually-routed pages, as prepared-media specs.

    Page routing is independent of the text profile: ``_visual_page`` sends a
    page to images when the source is a standalone image, when the page is
    ``image_only``, when a PDF page has ``no_usable_text_no_image``, or when the
    auditor asked for full coverage — and ``chunk_specs`` then *excludes* those
    pages from the text. So a scanned page contributes no text at all, and a
    text-only read of a mostly-digital PDF misses exactly the page a stamp or a
    countersignature is on.

    The same specs ``analysis_unit_specs`` builds, so ``prepared_set_identity``
    travels with them: re-preparing a page moves the read's ``input_sha1`` and
    the document is read again rather than being reduced under images it never
    saw.
    """

    return [
        spec
        for spec in analysis_unit_specs(workspace, document_id, scope)
        if spec.get("kind") == "document_visual_page_analysis"
        and not spec.get("unsupported_reason")
    ]


def read_over_window(
    workspace: Workspace, document_id: str, scope: dict
) -> str | None:
    """Why this document exceeds the read's bound, or None.

    One rule covers both halves, which is the point: the read either saw the
    document or it says it did not. A document over either bound is reported
    rather than silently truncated — the same rule the chunk budgets already
    keep, and the property that makes absence in a master mean *the document
    does not state this*.
    """

    characters = len(evidence_read_text(workspace, document_id))
    if characters > presets.EVIDENCE_READ_CHARACTERS:
        return (
            f"This document carries {characters:,} characters, above the "
            f"{presets.EVIDENCE_READ_CHARACTERS:,} one reading covers. It is "
            "reported rather than read in part, because a vocabulary built from "
            "half a document records absence for pages nobody read."
        )
    media = evidence_read_media(workspace, document_id, scope)
    if len(media) > presets.EVIDENCE_READ_VISUAL_MEDIA:
        return (
            f"This document routes {len(media)} page images to a single reading, "
            f"above the {presets.EVIDENCE_READ_VISUAL_MEDIA} one call carries."
        )
    return None


def has_evidence_reading(workspace: Workspace, document_id: str) -> bool:
    """Whether the read is done with this document.

    A third state, and it has to exist. ``has_usable_analysis`` asks two
    questions — does the stored ``schema_ref`` still match the live schema, and
    does the type stamped on it still match the assignment — and an *unstamped*
    reading has no ``schema_ref`` to answer either with. It is therefore not
    "usable" by that test, correctly: it is a reading and not yet evidence. But
    a read whose units were generated from that test would re-read every
    document on each re-expansion within the same run.

    So: a reading exists when a structured analysis carries either a master
    reference (read, awaiting its type's stamp) or a current schema stamp (read
    and stamped). Both mean the same thing to the *read* — this document has
    been read under the vocabulary its siblings are being read under.
    """

    record = document_analysis.generated_record(workspace, document_id)
    if record is None:
        return False
    if str(record.get("analysis_profile") or "") != "structured":
        return False
    document_type = document_classification.document_type(workspace, document_id)
    if str(record.get("master_ref") or ""):
        # Read, awaiting its type's stamp — but only if it was read under the
        # type this document now carries. A retype leaves the reading perfectly
        # present and made against the wrong vocabulary, which is the same
        # half-applied correction ``is_current_for`` exists to catch one stage
        # later.
        return str(record.get("master_type") or "") == str(document_type or "")
    return document_schemas.is_current_for(
        workspace, record.get("schema_ref"), document_type
    )


def _readable_evidence(workspace: Workspace, scope: dict) -> list[str]:
    """Evidence documents this run reads, in type-then-document order.

    Both gates, the pair Phase 9 restored. Category says whether the engagement
    holds the document as transaction evidence; type says what it is. An
    approval matrix is genuinely a ``delegation_of_authority`` and genuinely
    still policy, and only one of those was once being asked.

    ``other`` is excluded here and lands in 4b.2, which coins a type for it. It
    is a transient state — a document is ``other`` until something reads it and
    names it — not a terminal one.
    """

    # ``revise_vocabulary`` widens the document scope to whole types, which is
    # the honest price of rebuilding a vocabulary and the thing the action exists
    # to make deliberate. Every other pass reads what it was scoped to.
    widened = _revision_documents(workspace, scope) if _rebuilding(scope) else None
    scoped = widened or set(corpus_scope(workspace, scope).document_ids)
    return [
        str(document.get("id"))
        for document in document_classification.transaction_evidence(workspace)
        if str(document.get("id")) in scoped
        and document_classification.is_classified(workspace, str(document.get("id")))
        and not document_classification.is_other(workspace, str(document.get("id")))
        and analyzable(workspace, str(document.get("id"))) is not None
    ]


def _evidence_read_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Every evidence document in scope has been read under its type's master."""

    document_scope = corpus_scope(workspace, scope)
    blocked = _unknown_documents(document_scope)
    if blocked is not None:
        return blocked
    documents = _readable_evidence(workspace, scope)
    pending = [
        document_id
        for document_id in documents
        if not has_evidence_reading(workspace, document_id)
    ]
    details = {
        "evidence": len(documents),
        "read": len(documents) - len(pending),
        "types": document_classification.types_for_induction(workspace),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (
            f"{counted(len(pending), 'evidence document')} "
            f"{verb(len(pending), 'has', 'have')} not been read",
        ),
        details=details,
    )


def _evidence_read_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per evidence document, keyed ``<type>:<document>``.

    Units execute in sorted id order, so keying the type first puts a type's
    documents in one contiguous run for free — which is what lets the master
    accumulate in a sensible order and bounds the late-field sweep. The
    contiguity is a convenience, not the stamp's correctness condition: the
    stamp is a dependent capability and runs after the whole stage settles.
    """

    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    targeted = _revision_documents(workspace, scope)
    units: list[UnitSpec] = []
    for document_id in _readable_evidence(workspace, scope):
        if not forced and has_evidence_reading(workspace, document_id):
            continue
        if forced and targeted is not None and document_id not in targeted:
            continue

        document_type = document_classification.document_type(workspace, document_id)
        master = document_masters.master(workspace, document_type)
        units.append(
            UnitSpec(
                semantic_unit_id("evidence_read", document_type, document_id),
                "document_evidence_read",
                f"Read evidence — {known[document_id].get('title') or document_id}",
                (f"document:{document_id}",),
                {
                    "document_type": document_type,
                    "document_id": document_id,
                    "title": str(known[document_id].get("title") or ""),
                    # The vocabulary this document is read against travels on the
                    # input, so a master that moved while its siblings were read
                    # moves this unit's ``input_sha1`` and the document is read
                    # again rather than reduced under names it never saw.
                    "master_ref": str(master.get("master_ref") or ""),
                    "vocabulary_mode": vocabulary_mode(scope),
                },
            )
        )
    return units


def _documents_evidence_read() -> Capability:
    return Capability(
        "documents.evidence_read",
        "document_masters",
        "Evidence readings",
        "document_evidence_read",
        documents_workflow.dependencies("documents.evidence_read"),
        _evidence_read_ready,
        _evidence_read_units,
        context={"document_evidence_read": "documents.evidence_read"},
        # SEQUENTIAL, and this one is the mechanism rather than a concession.
        # A serialized unit sees its predecessor's work by rebinding against
        # committed workspace state; the parallel path binds every unit before
        # running any of them, so a unit's input is resolved at stage start and
        # can never see what a sibling settled. Per-document calls can only agree
        # about a vocabulary if they are not independent — which is why "make the
        # read parallel and lock the master" is not an option: the reads would
        # not be wrong about the master, they would never have been shown it.
        invalidate_on=("documents",),
    )


def unread_documents_of_type(
    workspace: Workspace, document_type: str, scope: dict
) -> list[str]:
    """Evidence documents of one type that this run has no reading for.

    The stamp's correctness condition, asked per type. A master built from eight
    of eighteen documents is not the type's vocabulary, and stamping it writes a
    ``schema_version`` claiming otherwise.
    """

    read = set(document_masters.master(workspace, document_type).get("documents_read") or [])
    return [
        document_id
        for document_id in _readable_evidence(workspace, scope)
        if document_classification.document_type(workspace, document_id) == document_type
        and document_id not in read
    ]


def _types_awaiting_stamp(workspace: Workspace, scope: dict) -> list[str]:
    """Types carrying a complete master whose vocabulary is not yet a schema.

    ``types_awaiting_schema``'s counterpart: same shape, different predicate.

    **A type whose documents were not all read is not offered for stamping**,
    and that guard belongs *here* rather than on the capability edge. It was on
    the edge, which made it a claim about the whole stage: one bank statement
    failing on a dangling citation blocked every stamp in the corpus, and
    ``fx_contract`` and ``payment_instruction`` — both read cleanly, both
    corroborated by two documents — got no schema either. The plan puts the
    guarantee on *the type*: a read that dies at document 9 leaves that type
    with no vocabulary and no evidence, and says so. It does not cost a
    different type its vocabulary, because nothing about that type failed.
    """

    targeted = _revision_types(workspace, scope)
    return [
        document_type
        for document_type in document_masters.types_with_master(workspace)
        if (targeted is None or document_type in targeted)
        and not unread_documents_of_type(workspace, document_type, scope)
        and (
            _forced(scope)
            or document_schemas.load_schema(workspace, document_type) is None
            or not _stamp_current(workspace, document_type)
        )
    ]


def _stamp_current(workspace: Workspace, document_type: str) -> bool:
    """Whether the stored schema already says what the master says.

    ``save_schema`` is a no-op when the meaning has not moved, so a stamp that
    runs anyway costs nothing and bumps nothing — but expanding a unit for it on
    every re-expansion would make the capability never settle.
    """

    schema = document_schemas.load_schema(workspace, document_type)
    if schema is None:
        return False
    master = document_masters.master(workspace, document_type)
    return str(schema.get("schema_hash") or "") == document_schemas.canonical_sha256(
        document_schemas.meaning(
            document_type,
            document_schemas.validate_fields(document_masters.schema_fields(master)),
        )
    )


def _schemas_stamped_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Every type the corpus's evidence carries has a current schema.

    Measured against the types those documents actually carry, exactly as the
    induced-schema readiness was: a type nothing carries needs no schema, and a
    type only planning material carries needs none either.
    """

    inducible = document_classification.types_for_induction(workspace)
    awaiting = [
        document_type
        for document_type in inducible
        if document_schemas.load_schema(workspace, document_type) is None
    ]
    stamped = document_schemas.list_schemas(workspace)
    # Reported rather than inferred from the gap: a type left unstamped because
    # one of its documents could not be read is a different problem from one
    # nothing has read yet, and they are repaired in different places.
    incomplete = {
        document_type: unread_documents_of_type(workspace, document_type, scope)
        for document_type in inducible
    }
    details = {
        "types_for_induction": inducible,
        "stamped": len(stamped),
        "types_with_unread_documents": {
            document_type: len(unread)
            for document_type, unread in incomplete.items()
            if unread
        },
        "low_confidence": [
            record["document_type"] for record in stamped if record.get("low_confidence")
        ],
    }
    if awaiting:
        return Readiness(
            "missing",
            (
                f"{counted(len(awaiting), 'document type')} "
                f"{verb(len(awaiting), 'has', 'have')} no stamped schema",
            ),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _schemas_stamped_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per type carrying a master with no current schema.

    Keyed ``document_schema:<type>`` — the same id the freeze used — so the
    executor's artifact ref, its postcondition and every receipt keyed on it
    keep their meaning across the change.
    """

    return [
        UnitSpec(
            semantic_unit_id("document_schema", document_type),
            "document_schema_stamp",
            f"Stamp vocabulary — {document_types.label(document_type)}",
            tuple(
                f"document:{document_id}"
                for document_id in document_masters.master(
                    workspace, document_type
                ).get("documents_read")
                or []
            ),
            {"document_type": document_type},
        )
        for document_type in _types_awaiting_stamp(workspace, scope)
    ]


def _documents_schemas_stamped() -> Capability:
    return Capability(
        "documents.schemas_stamped",
        "document_schemas",
        "Document schemas",
        "document_schema_stamp",
        documents_workflow.dependencies("documents.schemas_stamped"),
        _schemas_stamped_ready,
        _schemas_stamped_units,
        # No model turn. The stamp reads the finished master, calls
        # ``save_schema`` once, and back-stamps the type's readings — all through
        # ``commit_local``, the shape the freeze binder already used when its
        # samples agreed.
        context=None,
        invalidate_on=("documents",),
    )


def preparation_model_turns(workspace: Workspace, scope: dict) -> int:
    """Model turns the stages *before* analysis will spend for this scope.

    These were model-backed stages of the same workflow sitting entirely outside
    the document budget's arithmetic, which was sized from chunks and documents
    alone. On a one-document refresh that was the whole failure: seven turns, six
    of them gone on schema work, and the analysis it was asked for never reached.

    Under 4b.1 the terms move in both directions and leaving it stale reproduces
    the failure it was written for. Categorization is unchanged. The sample pass
    and its reconcile call are gone; in their place is a turn for *every*
    evidence document, which is a much larger number. The stamp adds nothing —
    it takes no model turn at all. And the analysis budget that follows shrinks,
    because evidence documents leave the chunk path entirely: their reading is
    the extraction pass.

    Counted rather than expanded. The unit builders carry each document's text
    with them, and a budget has no use for it.
    """

    document_scope = resolve_document_scope(workspace, scope)
    scoped = set(document_scope.document_ids)
    evidence_ids = {
        str(document.get("id"))
        for document in document_classification.transaction_evidence(workspace)
    }
    if _forced(scope):
        # Forcing widens which documents are re-asked, not what the question
        # applies to: ``_classified_units`` re-classifies scoped transaction
        # evidence and leaves prose alone, so counting the whole scope here
        # would buy turns for units that are never expanded.
        classifications = len(scoped & evidence_ids)
    else:
        classifications = len(
            {
                document_id
                for document_id in (
                    *document_classification.unclassified_ids(workspace),
                    *document_classification.reclassifiable_ids(workspace),
                )
                if document_id in scoped
            }
        )
    # One per evidence document the read will expand for, times what a *failed*
    # one is allowed to cost. A repair is a model turn like any other, and the
    # read takes two of them where the other document workers take one — so a
    # budget counting one turn per read would be short by a factor of three on
    # exactly the run that needs it most. ``revise_vocabulary`` widens the
    # document set to the targeted documents' whole types, which is the honesty
    # the split is for: re-reading eighteen payment instructions to fix one
    # document's vocabulary is expensive, and it is what the repair costs.
    readings = len(_evidence_read_units(workspace, scope)) * (
        1 + READ_REPAIR_ATTEMPTS
    )
    # The stamp is deliberately absent. It reads the finished master, calls
    # ``save_schema``, and back-stamps — no model sees any of it.
    return classifications + readings


# --------------------------------------------------------------------------- #
# documents.analysis_chunks_ready (P9.4 / P9.5)
# --------------------------------------------------------------------------- #
def _prose_documents(workspace: Workspace, scope: dict) -> list[str]:
    """The documents this pass analyses: everything that is not evidence.

    ``analysis_chunks_ready`` carries planning prose under 4b.1, and transaction
    evidence has its own pass in both modalities — text chunks and visual pages
    alike. Leaving evidence here would analyse it twice under two vocabularies,
    which is the drift the read removes, relocated.

    The exclusion is by *category*, which is the gate Phase 9 restored and the
    one this capability still needs. A document the engagement does not hold as
    transaction evidence is prose whatever its type says it is: an approval
    matrix is genuinely a ``delegation_of_authority`` and genuinely still policy,
    and routing it to a record dump replaces the narrative planning consumes.

    **An uncategorized document is not prose — it is undecided, and it expands
    to nothing.** Treating a blank category as "not evidence" is the inverse of
    the default ``_planning_relevant`` takes, and it has to be, because
    ``ensure_stage_units`` only ever *adds* units: a stage expanded before
    ``documents.categorized`` has run keeps whatever it materialized then, and
    re-expansion cannot withdraw it. Measured on the treasury corpus: at routing
    time nothing carried a category, all nine documents looked like prose, nine
    chunk units were written into the stage, and their reductions later
    overwrote all eight structured readings with narrative ones — a document
    read twice under two vocabularies, with the second silently winning. The
    capability's dependency on ``documents.categorized`` is what makes expanding
    to nothing safe: by the time it legitimately runs, every document has an
    answer.
    """

    classifiable = {
        str(document.get("id"))
        for document in document_classification.classifiable(workspace)
    }
    prose: list[str] = []
    for document_id in resolve_document_scope(workspace, scope).document_ids:
        category = document_classification.category(workspace, document_id)
        if category:
            if category not in intake.EVIDENCE_DOCUMENT_CATEGORIES:
                prose.append(document_id)
            continue
        if document_service.cached_extraction(workspace, document_id) is None:
            # Text has not been extracted yet, so the category question has not
            # been *asked*. Undecided, and it expands to nothing.
            continue
        if document_id not in classifiable:
            # Extracted, and carries no page-one text to read a category from —
            # an image-only PDF, a PNG, a failed extraction. It will never be
            # categorized, so the question is settled rather than pending, and
            # excluding it here would leave it analysed by nothing at all. Its
            # visual pages are exactly what this pass still handles.
            prose.append(document_id)
    return prose


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
    prose = _prose_documents(workspace, scope)
    pending = [
        document_id
        for document_id in prose
        if not has_usable_analysis(workspace, document_id)
        and analyzable(workspace, document_id) is not None
        and bool(analysis_unit_specs(workspace, document_id, scope))
    ]
    details = {
        "documents": len(prose),
        "unanalyzed": len(pending),
        "evidence_excluded": len(document_scope.document_ids) - len(prose),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{counted(len(pending), 'document')} {verb(len(pending), 'has', 'have')} no analysed source chunks",),
        details=details,
    )


def _chunk_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per bounded source chunk of every document still to analyze.

    Expansion is empty until ``documents.text_ready`` has cached an extraction,
    which is exactly what the scheduler's re-expansion between stages is for: a
    stage that materialized empty at routing time fans out for real once its
    prerequisite has run.
    """
    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    units: list[UnitSpec] = []
    for document_id in _prose_documents(workspace, scope):
        if not forced and has_usable_analysis(workspace, document_id):
            continue
        title = known[document_id].get("title") or document_id
        for chunk in analysis_unit_specs(workspace, document_id, scope):
            visual = chunk["kind"] == "document_visual_page_analysis"
            units.append(
                UnitSpec(
                    (
                        semantic_unit_id("document_chunk", document_id, chunk["id"])
                        if not visual
                        else semantic_unit_id(
                            "document_visual_page",
                            document_id,
                            chunk["page"],
                            chunk["prepared_set_identity"],
                        )
                    ),
                    chunk["kind"],
                    (
                        f"Analyze {title} — page {chunk['page']}"
                        if not visual
                        else f"Analyze visual page — {title}, page {chunk['page']}"
                    ),
                    (f"document:{document_id}",),
                    {
                        "document_id": document_id,
                        "chunk_id": chunk["id"],
                        "page": chunk["page"],
                        **(
                            {
                                "start_character": chunk["start_character"],
                                "end_character": chunk["end_character"],
                            }
                            if not visual
                            else {
                                "frame": chunk["frame"],
                                "prepared_set_identity": chunk[
                                    "prepared_set_identity"
                                ],
                                "unsupported_reason": chunk[
                                    "unsupported_reason"
                                ],
                            }
                        ),
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
        context={
            "document_chunk_analysis": "documents.analysis_chunk",
            "document_visual_page_analysis": "documents.analysis_visual_page",
            "document_structured_analysis": "documents.analysis_structured",
        },
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
    # Prose only. Transaction evidence has its own pass, which commits the same
    # analysis artifact directly rather than reducing chunk proposals into one —
    # and a reading that is not yet stamped has no ``schema_ref``, so counting it
    # here would leave this outcome permanently unmet and block planning behind a
    # reduction that is never going to run.
    prose = _prose_documents(workspace, scope)
    analyzed = [
        document_id
        for document_id in prose
        if has_usable_analysis(workspace, document_id)
    ]
    # A document with no extractable text is deliberately *not* treated as
    # satisfied. It has no analysis and never will without auditor action, so the
    # outcome stays unmet and its unit settles for review; reporting it is the
    # whole point of scoping the document into the request.
    pending = [
        document_id
        for document_id in prose
        if document_id not in analyzed
    ]
    details = {
        "documents": len(prose),
        "generated": len(analyzed),
        "evidence_read": sum(
            1
            for document_id in document_scope.document_ids
            if document_id not in prose and has_evidence_reading(workspace, document_id)
        ),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{counted(len(pending), 'document')} {verb(len(pending), 'has', 'have')} no generated analysis",),
        details=details,
    )


def _generated_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
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
        for document_id in _prose_documents(workspace, scope)
        if forced or not has_usable_analysis(workspace, document_id)
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
    "documents.categorized": _documents_categorized,
    "documents.types_classified": _documents_types_classified,
    "documents.evidence_read": _documents_evidence_read,
    "documents.schemas_stamped": _documents_schemas_stamped,
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
    "ELIGIBLE_CONTENT_STATES",
    "MAX_SCOPE_DOCUMENTS",
    "STAGE_CHECKPOINTS",
    "DocumentScope",
    "analyzable",
    "analysis_unit_specs",
    "capabilities",
    "VOCABULARY_MODES",
    "chunk_specs",
    "evidence_read_media",
    "evidence_read_pages",
    "evidence_read_text",
    "has_evidence_reading",
    "has_generated_analysis",
    "has_usable_analysis",
    "preparation_model_turns",
    "page_limit",
    "read_over_window",
    "vocabulary_mode",
    "visual_page_limit",
    "resolve_document_scope",
    "scoped_documents",
]
