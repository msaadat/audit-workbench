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
    document_media,
    document_schemas,
    document_types,
    documents as document_service,
    intake,
)
from ...text import counted, verb
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import documents as documents_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "documents.text_ready",
    "documents.categorized",
    "documents.types_classified",
    "documents.schemas_sampled",
    "documents.schemas_induced",
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


def analysis_profile(workspace: Workspace, document_id: str) -> str:
    """The analysis profile one document's text chunks are mapped under.

    ``structured`` for transaction evidence whose type has an induced schema.
    There is no separate voucher profile any more: the fields such a document is
    read under come from the schema rather than from a pack. Both halves are
    load-bearing — the schema supplies the vocabulary, and the category says
    this engagement holds the document as transaction evidence at all. A policy
    that happens to share a type with vouchers is not read under their fields.

    Everything else gets a narrative analysis — readable, citable, and not cycle
    evidence — which is the honest description of what is known about it, and
    the form planning consumes.
    """

    document = next(
        (item for item in workspace.documents if str(item.get("id")) == document_id),
        None,
    )
    if document is None:
        return "standard"
    if (
        document_classification.category(workspace, document_id)
        not in intake.EVIDENCE_DOCUMENT_CATEGORIES
    ):
        return "standard"
    document_type = document_classification.document_type(workspace, document_id)
    if document_type and document_schemas.load_schema(workspace, document_type):
        return "structured"
    return "standard"


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
    no schema, and ``documents.schemas_induced`` reported satisfied having
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
    # Which profile a text chunk is mapped under is a property of the document,
    # not of the chunk. A document whose type has an induced schema is extracted
    # against those fields; everything else is read as prose. The structured
    # profile returns fields and audit notes and has its summary rendered
    # locally, because those facts are already exact.
    text_kind = (
        "document_structured_analysis"
        if analysis_profile(workspace, document_id) == "structured"
        else "document_chunk_analysis"
    )
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
# documents.schemas_sampled / documents.schemas_induced
# --------------------------------------------------------------------------- #
#
# Two capabilities, not one, and for a reason that is easy to get wrong: units
# within a stage execute in sorted *id* order, never declaration order. A single
# capability holding both the sample readings and the freeze that consumes them
# would bind the freeze first — ``document_schema:x`` sorts before
# ``document_schema_sample:x:y`` — and it would read back nothing. The map/reduce
# split is what makes the ordering a dependency edge the scheduler honours,
# exactly as chunk analysis and its reduction already do.
def _sampled_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Sample readings are run-local, so readiness is the outcome they feed.

    A reading is an input to the frozen schema and never a durable record of its
    own, so the only thing outside the run that can show the sampling happened is
    the schema itself. The scheduler's durable unit state — not a workspace
    probe — is what stops a resumed run from re-reading a sample it already paid
    for.
    """

    awaiting = document_classification.types_awaiting_schema(workspace)
    details = {"types_awaiting_schema": awaiting}
    if not awaiting:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (
            f"{counted(len(awaiting), 'document type')} "
            f"{verb(len(awaiting), 'has', 'have')} no sampled fields",
        ),
        details=details,
    )


def _pending_types(workspace: Workspace, scope: dict) -> list[str]:
    """The types this run samples and freezes a schema for.

    Forcing re-derives, but only for the types the run was actually asked about.
    Re-deriving every type in the workspace is what a one-document ``refresh``
    used to do: on an 84-document engagement it spent a budget sized for one
    document's chunks on re-sampling schemas it was never pointed at, failed on
    the turn limit before reaching the analysis, and bumped every schema a
    version — orphaning 68 completed extractions as ``stale_schema_reference``.
    Scoping it makes the small-target case do what its button says while a
    whole-workspace refresh still re-derives the whole workspace, because then
    every type *is* a targeted type.

    Types with no schema at all stay in scope whether or not they were targeted.
    Nothing is orphaned by inducing what does not exist yet, and this is the
    only path that fills the gap ``schemas_induced`` reports.
    """

    awaiting = document_classification.types_awaiting_schema(workspace)
    if not _forced(scope):
        return awaiting
    inducible = set(document_classification.types_for_induction(workspace))
    targeted = {
        document_classification.document_type(workspace, document_id)
        for document_id in resolve_document_scope(workspace, scope).document_ids
    }
    return sorted(set(awaiting) | (targeted & inducible))


def _sample_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit per sampled document, read independently of its siblings.

    Handing one worker every sample at once would produce a tidier answer and
    destroy the only signal worth having: whether documents of this type actually
    agree about what fields they carry.
    """

    known = {str(item.get("id")): item for item in workspace.documents}
    units: list[UnitSpec] = []
    for document_type in _pending_types(workspace, scope):
        for document_id in document_classification.sample_for_induction(
            workspace, document_type
        ):
            units.append(
                UnitSpec(
                    semantic_unit_id(
                        "document_schema_sample", document_type, document_id
                    ),
                    "document_schema_sample",
                    f"Read fields — {known[document_id].get('title') or document_id}",
                    (f"document:{document_id}",),
                    {
                        "document_type": document_type,
                        "document_id": document_id,
                        "title": str(known[document_id].get("title") or ""),
                        "text": document_classification.induction_text(
                            workspace, document_id
                        ),
                    },
                )
            )
    return units


def _documents_schemas_sampled() -> Capability:
    return Capability(
        "documents.schemas_sampled",
        "document_schemas",
        "Document field readings",
        "document_schema_sample",
        documents_workflow.dependencies("documents.schemas_sampled"),
        _sampled_ready,
        _sample_units,
        context={"document_schema_sample": "documents.schema_sample"},
        # Independent of one another and never committing — the same grounds the
        # chunk capability qualifies on.
        barrier="all_settled_parallel",
        invalidate_on=("documents",),
    )


def _schemas_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Every type the engagement's transaction evidence carries has a schema.

    Measured against the types those documents actually carry, not the catalog
    and not every type present: a type nothing carries needs no schema, and a
    type only planning material carries needs none either. An engagement whose
    documents are all still unidentified therefore has nothing to induce and is
    satisfied, which is correct — the gap it has is a classification gap, and
    that capability reports it.
    """

    awaiting = document_classification.types_awaiting_schema(workspace)
    induced = document_schemas.list_schemas(workspace)
    details = {
        "types_for_induction": document_classification.types_for_induction(workspace),
        "induced": len(induced),
        "low_confidence": [
            record["document_type"] for record in induced if record.get("low_confidence")
        ],
    }
    if awaiting:
        return Readiness(
            "missing",
            (
                f"{counted(len(awaiting), 'document type')} "
                f"{verb(len(awaiting), 'has', 'have')} no induced schema",
            ),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _schema_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One freeze unit per document type, parented to its own samples."""

    units: list[UnitSpec] = []
    for document_type in _pending_types(workspace, scope):
        samples = document_classification.sample_for_induction(workspace, document_type)
        if not samples:
            continue
        units.append(
            UnitSpec(
                semantic_unit_id("document_schema", document_type),
                "document_schema_freeze",
                f"Settle schema — {document_types.label(document_type)}",
                tuple(f"document:{document_id}" for document_id in samples),
                {"document_type": document_type, "sample_document_ids": list(samples)},
            )
        )
    return units


def preparation_model_turns(workspace: Workspace, scope: dict) -> int:
    """Model turns the stages *before* analysis will spend for this scope.

    Classification, schema sampling and the freeze that consumes them each call
    the model once per unit, and none of that was in the document budget: it was
    sized from chunks and documents alone, so every preparation turn was spent
    against an allowance that had not counted it. On a one-document ``refresh``
    that was the whole failure — seven turns, six of them gone on schema work,
    and the analysis it was asked for never reached.

    Counted rather than expanded. The unit builders carry each document's
    classification and induction text with them, and a budget has no use for it.
    """

    document_scope = resolve_document_scope(workspace, scope)
    scoped = set(document_scope.document_ids)
    if _forced(scope):
        # Forcing widens which documents are re-asked, not what the question
        # applies to: ``_classified_units`` re-classifies scoped transaction
        # evidence and leaves prose alone, so counting the whole scope here
        # would buy turns for units that are never expanded.
        classifications = len(
            scoped
            & {
                str(document.get("id"))
                for document in document_classification.transaction_evidence(workspace)
            }
        )
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
    samples = 0
    freezes = 0
    for document_type in _pending_types(workspace, scope):
        picked = document_classification.sample_for_induction(workspace, document_type)
        samples += len(picked)
        freezes += 1 if picked else 0
    return classifications + samples + freezes


def _documents_schemas_induced() -> Capability:
    return Capability(
        "documents.schemas_induced",
        "document_schemas",
        "Document schemas",
        "document_schema_reconcile",
        documents_workflow.dependencies("documents.schemas_induced"),
        _schemas_ready,
        _schema_units,
        context={"document_schema_freeze": "documents.schema_reconcile"},
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
        if not has_usable_analysis(workspace, document_id)
        and analyzable(workspace, document_id) is not None
        and bool(analysis_unit_specs(workspace, document_id, scope))
    ]
    details = {
        "documents": len(document_scope.document_ids),
        "unanalyzed": len(pending),
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
    document_scope = resolve_document_scope(workspace, scope)
    known = {str(item.get("id")): item for item in workspace.documents}
    forced = _forced(scope)
    units: list[UnitSpec] = []
    for document_id in document_scope.document_ids:
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
    analyzed = [
        document_id
        for document_id in document_scope.document_ids
        if has_usable_analysis(workspace, document_id)
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
        (f"{counted(len(pending), 'document')} {verb(len(pending), 'has', 'have')} no generated analysis",),
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
    "documents.schemas_sampled": _documents_schemas_sampled,
    "documents.schemas_induced": _documents_schemas_induced,
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
    "chunk_specs",
    "has_generated_analysis",
    "has_usable_analysis",
    "preparation_model_turns",
    "page_limit",
    "visual_page_limit",
    "resolve_document_scope",
    "scoped_documents",
]
