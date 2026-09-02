"""Document classification: what a document *is*, what it is *to this
engagement*, and who decided each.

Two answers, one record. ``category`` says whether the engagement holds the
document as transaction evidence or as planning material; ``document_type`` says
what form it takes and therefore which fields it is read under and which role it
may fill in a cycle. Neither derives from the other, and both have to say yes
before a schema is induced: an approval matrix is genuinely a
``delegation_of_authority`` and genuinely still policy.

Category used to be guessed at intake from the filename. It is now read from the
document's opening page by ``documents.categorized``, one stage ahead of the
type, because a filename cannot support the answer and a wrong one is not merely
imprecise — a category outside both sets makes a document invisible, and one on
the wrong side of the partition either replaces planning's narrative with a
record dump or withholds evidence from the cycle entirely.

Every assignment records ``assigned_by``. That is not bookkeeping: a
reclassification rerun is what makes retyping useful — coin one type and the
remaining ``other`` documents are swept with the extended list — and the same
rerun would silently undo an auditor's decision if provenance were not tracked.
Model assignments may be replaced by a rerun; auditor assignments may not.

Assignments live in ``Documents/.types`` sidecars rather than on the document
entry, for the same reason generated analyses do. Capability readiness runs
against whatever workspace handle its caller is holding, which may be several
revisions behind by the time a stage is scheduled; a lazily hydrated artifact
collection read from that handle would report a document unclassified moments
after it was classified, and the capability would re-run forever. A sidecar is
read from disk, so readiness sees what actually happened.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from . import document_schemas, document_types, documents, intake
from .workspaces import Workspace, WorkspaceError, write_json_atomic

#: Where assignments live, one file per document.
DIRNAME = "Documents/.types"

CONFIDENCES = frozenset({"high", "medium", "low"})
ASSIGNERS = frozenset({"model", "auditor"})

#: How much of the document the classifier reads. Page one is where a document
#: says what it is; feeding more costs tokens without improving the label, and a
#: long tail of body text actively invites the model to classify by subject
#: matter rather than by form.
CLASSIFICATION_PAGES = 1
CLASSIFICATION_CHARACTERS = 4000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(workspace: Workspace, document_id: str) -> Path:
    """The sidecar path for one document, named without asking the filesystem.

    The containment check is lexical. ``Path.resolve`` answers the same
    question, but on Windows it is a ``_getfinalpathname`` syscall per path
    component, and readiness names this path once per document per capability —
    thousands of times to render one record view. The slowest case is the one
    that matters most: where ``Documents/.types`` does not exist yet, resolution
    falls back to walking the path component by component, so an engagement pays
    the most for the documents it has not classified.

    ``normpath`` collapses any ``..`` the identifier smuggled in as string
    arithmetic, and ``is_relative_to`` compares parts. Together they reject
    exactly what the pair of ``resolve`` calls rejected. What they no longer do
    is follow symlinks, which this path never needed: the caller reads, writes,
    or unlinks the sidecar, and the operating system resolves links for those.
    """
    root = Path(os.path.normpath(workspace.root / DIRNAME))
    path = Path(os.path.normpath(root / f"{str(document_id)}.json"))
    if not path.is_relative_to(root):
        raise WorkspaceError("Unsafe document classification reference.")
    return path


def _require_document(workspace: Workspace, document_id: str) -> dict:
    for item in workspace.documents:
        if str(item.get("id")) == str(document_id):
            return item
    raise WorkspaceError(f"Document '{document_id}' not found.")


# Readiness asks the same handful of questions about every document once per
# capability: whether it is categorized, classified, auditor-assigned, of a
# type the catalog can induce. Each of those is a separate sidecar read, so
# rendering one record view read 88 sidecars 1,936 times over. Like
# ``doc_tests.request_cache_scope``, this lets a caller that is certain no
# assignment is written in its span memoize the read for its duration. It is
# reentrant, and only the outermost scope pays for setup and teardown.
_cache: ContextVar[dict | None] = ContextVar("document_classification_request_cache", default=None)


@contextmanager
def request_cache_scope():
    if _cache.get() is not None:
        yield
        return
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def _read(workspace: Workspace, document_id: str) -> dict:
    cache = _cache.get()
    key = (str(workspace.root), str(document_id))
    if cache is not None and key in cache:
        # A copy, because assignment reads a record, edits it, and writes it
        # back — handing out the cached object would let that edit answer the
        # next reader from memory.
        return copy.deepcopy(cache[key])
    try:
        path = _path(workspace, document_id)
    except WorkspaceError:
        return empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = empty()
    if not isinstance(value, dict):
        value = empty()
    if cache is not None:
        cache[key] = copy.deepcopy(value)
    return value


def empty() -> dict:
    return {
        "category": None,
        "category_assigned_by": None,
        "category_assigned_at": None,
        "category_confidence": None,
        "category_rationale": "",
        "category_agent_run_id": None,
        "category_unit_id": None,
        "document_type": None,
        "document_type_other": None,
        "assigned_by": None,
        "assigned_at": None,
        "confidence": None,
        "rationale": "",
        "previous_document_type": None,
        "agent_run_id": None,
        "unit_id": None,
        "catalog_sha1": None,
    }


def classification(workspace: Workspace, document_id: str) -> dict:
    return _read(workspace, document_id)


def document_type(workspace: Workspace, document_id: str) -> str:
    return str(classification(workspace, document_id).get("document_type") or "")


def category(workspace: Workspace, document_id: str) -> str:
    """What this engagement holds the document as. Empty until page one is read.

    The sidecar answers where it has one, and the document entry is the fallback.
    Both halves are needed. The sidecar is what readiness asks, because a
    capability's workspace handle is routinely several revisions behind and a
    lazily hydrated collection read from it would report a document
    uncategorized moments after it was categorized. The entry is what an upload
    that named a category outright wrote, before any stage ran — an explicit
    answer that should not have to wait for a model to agree with it.

    The fallback cannot go stale the way a bare entry read would: a value
    written *during* a run goes to the sidecar, which wins here, and a value
    present only on the entry was there from the revision the document arrived
    at.
    """

    stored = str(classification(workspace, document_id).get("category") or "")
    if stored:
        return stored
    for item in workspace.documents:
        if str(item.get("id")) == str(document_id):
            return str(item.get("category") or "")
    return ""


def is_categorized(workspace: Workspace, document_id: str) -> bool:
    return bool(category(workspace, document_id))


def is_category_auditor_assigned(workspace: Workspace, document_id: str) -> bool:
    record = classification(workspace, document_id)
    return str(record.get("category_assigned_by") or "") == "auditor"


def is_auditor_assigned(workspace: Workspace, document_id: str) -> bool:
    return str(classification(workspace, document_id).get("assigned_by") or "") == "auditor"


def is_classified(workspace: Workspace, document_id: str) -> bool:
    return bool(document_type(workspace, document_id))


def is_other(workspace: Workspace, document_id: str) -> bool:
    return document_type(workspace, document_id) == document_types.OTHER


def remove_sidecars(workspace: Workspace, document_id: str) -> None:
    try:
        _path(workspace, document_id).unlink(missing_ok=True)
    except WorkspaceError:
        return


# --------------------------------------------------------------------------- #
# assignment
# --------------------------------------------------------------------------- #
def assign(
    workspace: Workspace,
    document_id: str,
    type_id: str,
    *,
    assigned_by: str,
    confidence: str = "medium",
    rationale: str = "",
    other_label: str = "",
    agent_run_id: str = "",
    unit_id: str = "",
    catalog_sha1: str = "",
) -> dict:
    """Record a type for one document.

    A model assignment against an auditor-assigned document is **not** an error
    and **not** applied: it returns what is stored. A reclassification rerun
    expands its units from :func:`reclassifiable_ids`, so it should never reach
    one — but an auditor retyping while a run is in flight would, and failing the
    unit for that would be wrong. The auditor's decision simply stands.
    """

    if assigned_by not in ASSIGNERS:
        raise WorkspaceError(f"Unknown assigner '{assigned_by}'.")
    if confidence not in CONFIDENCES:
        raise WorkspaceError(f"Unknown classification confidence '{confidence}'.")
    _require_document(workspace, document_id)
    existing = classification(workspace, document_id)
    if assigned_by == "model" and str(existing.get("assigned_by") or "") == "auditor":
        return existing

    type_id = document_types.validate(
        type_id, local_types=document_schemas.local_type_ids(workspace)
    )
    label = str(other_label or "").strip()
    if type_id == document_types.OTHER and not label:
        raise WorkspaceError(
            "An 'other' classification must name what the document is."
        )
    record = {
        # Merged onto what is stored, never replacing it: the category half of
        # this record was written by an earlier stage, and rebuilding the file
        # from the type alone would silently drop it.
        **existing,
        "document_type": type_id,
        "document_type_other": label or None if type_id == document_types.OTHER else None,
        "assigned_by": assigned_by,
        "assigned_at": utcnow(),
        "confidence": confidence,
        "rationale": str(rationale or "").strip(),
        "previous_document_type": existing.get("document_type"),
        # Which run and unit wrote this. A resumed run reads it back to tell an
        # interrupted commit that landed from one that never ran — the assignment
        # changes the document entry, so a moved parent hash alone cannot say
        # which happened.
        "agent_run_id": str(agent_run_id or "") or None,
        "unit_id": str(unit_id or "") or None,
        # Which vocabulary this answer was chosen from. An ``other`` is only
        # worth re-asking when the catalog has grown since — re-posing the same
        # question against the same list returns the same answer, and sweeping
        # unconditionally would leave classification re-running on every run.
        "catalog_sha1": str(catalog_sha1 or "") or None,
    }
    path = _path(workspace, document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    return record


def assign_category(
    workspace: Workspace,
    document_id: str,
    value: str,
    *,
    assigned_by: str,
    confidence: str = "medium",
    rationale: str = "",
    agent_run_id: str = "",
    unit_id: str = "",
) -> dict:
    """Record what this engagement holds one document as.

    The same provenance rule the type carries: a model assignment against an
    auditor's is not an error and is not applied, it returns what is stored. An
    auditor who has said a document is policy has made a decision on the record,
    and a rerun is not another decision.

    Written twice, deliberately. The sidecar is authoritative and is what
    readiness and the evidence gate read, because a capability's workspace handle
    is routinely several revisions behind. The document entry carries a copy
    because a dozen readers — planning context selection, the artifact index,
    narration — hold a document dict and no workspace, and rewriting all of them
    to reach a sidecar would be a far wider change than the answer moving here.

    The entry is mutated in memory only; **the caller persists the workspace**.
    Inside an executor that is ``mutate()``, which saves a callback that only
    touched memory and publishes the revision.
    """

    if assigned_by not in ASSIGNERS:
        raise WorkspaceError(f"Unknown assigner '{assigned_by}'.")
    if confidence not in CONFIDENCES:
        raise WorkspaceError(f"Unknown classification confidence '{confidence}'.")
    document = _require_document(workspace, document_id)
    value = str(value or "").strip().lower()
    if value not in intake.DOCUMENT_CATEGORIES:
        raise WorkspaceError(f"Unknown document category '{value}'.")
    existing = classification(workspace, document_id)
    if assigned_by == "model" and str(existing.get("category_assigned_by") or "") == "auditor":
        return existing

    record = {
        **existing,
        "category": value,
        "category_assigned_by": assigned_by,
        "category_assigned_at": utcnow(),
        "category_confidence": confidence,
        "category_rationale": str(rationale or "").strip(),
        # Which run and unit wrote this. The type half records the same, and for
        # the same reason: the reconciler has to tell an interrupted commit that
        # landed from one that never ran, and a category is not distinctive
        # enough to prove it wrote itself.
        "category_agent_run_id": str(agent_run_id or "") or None,
        "category_unit_id": str(unit_id or "") or None,
    }
    path = _path(workspace, document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    document["category"] = value
    return record


def retype(
    workspace: Workspace,
    document_id: str,
    *,
    type_id: str | None = None,
    coin: str | None = None,
    rationale: str = "",
    discriminator: str = "",
) -> dict:
    """An auditor's assignment, either to a listed type or to a coined one.

    Coining registers the type in the workspace's effective list first, so the
    reclassification rerun that follows can sweep the remaining ``other``
    documents onto it. Retyping one document and leaving forty like it in the
    bucket would starve induction, which needs several documents of a type.

    Not restricted to the ``other`` bucket. The provenance rule already makes
    correcting a wrong model label safe; whether the interface offers that is a
    separate decision from whether the store permits it.

    ``discriminator`` says what separates the coined type from its neighbours. It
    is worth asking for rather than defaulting to nothing: a shipped type carries
    one from the catalog, so a coined type left blank is the *least* described
    entry in the engagement's vocabulary while being the one no reader has seen
    before. That gap has already cost a matrix — a type coined
    ``Internal deal confirmation`` for one anomalous document was read by the RCM
    authoring turn as the deal record, on its name alone, because the name was
    all there was to read.
    """

    if bool(type_id) == bool(coin):
        raise WorkspaceError("Retyping needs exactly one of a type id or a name to coin.")
    if coin:
        type_id = document_schemas.coin_local_type(
            workspace, coin, discriminator=discriminator
        )["id"]
    return assign(
        workspace,
        document_id,
        str(type_id),
        assigned_by="auditor",
        confidence="high",
        rationale=rationale,
    )


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def catalog_signature(workspace: Workspace) -> str:
    """Identity of the vocabulary a classification was offered.

    Coining a type moves this; nothing else does. It is what decides whether the
    ``other`` bucket is worth sweeping again.
    """

    payload = json.dumps(sorted(document_schemas.effective_type_ids(workspace)), separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def classifiable(workspace: Workspace) -> list[dict]:
    """Documents with enough extracted text to classify.

    An image-only or failed document is deliberately absent rather than counted
    unclassified: it will never classify without auditor action, and readiness
    reports it through the text capability that owns that problem.
    """

    values = []
    for document in workspace.documents:
        extraction = documents.cached_extraction(workspace, str(document.get("id")))
        if not extraction:
            continue
        # Pages alone are not enough: an image-only PDF has pages carrying no
        # text. Requiring text here is what keeps a unit from expanding only to
        # settle for review the moment it is bound.
        if any(str(page.get("text") or "").strip() for page in extraction.get("pages") or []):
            values.append(document)
    return values


def uncategorized_ids(workspace: Workspace) -> list[str]:
    """Readable documents with no category yet. What ``documents.categorized``
    expands over.

    Drawn from :func:`classifiable` rather than every document, because the
    category is read from page one and a document with no extracted text has no
    page one. The text capability owns that gap and reports it.
    """

    return [
        str(document.get("id"))
        for document in classifiable(workspace)
        if not is_categorized(workspace, str(document.get("id")))
    ]


def transaction_evidence(workspace: Workspace) -> list[dict]:
    """Classifiable documents intake routed as transaction-level source material.

    Schema induction and structured extraction run over these and nothing else.
    Classification, schema induction and structured extraction all run over
    these and nothing else.

    Type says what a document *is*; category says what it is *to this
    engagement*, and a procurement policy correctly classified
    ``delegation_of_authority`` is still prose here. Reading it under a field
    schema would replace the narrative analysis planning actually consumes with
    a record dump, because a structured document's summary is rendered from its
    records rather than written.

    Classification shares the gate rather than running corpus-wide, because a
    type assigned to non-transaction material is inert: it cannot fill a cycle
    role, induction skips it, and an RCM comparison addressing ``{document_type,
    field}`` needs a schema it will never have. The label cost a model call and
    nothing reads it. Gating here also keeps the two axes agreeing about where
    type stops mattering, instead of the category gate sitting one stage lower
    than the question it governs.

    Explicit only: a document intake left uncategorized is never treated as
    transaction evidence, so the gate opens on a decision rather than on a gap.
    """

    return [
        document
        for document in classifiable(workspace)
        if category(workspace, str(document.get("id")))
        in intake.EVIDENCE_DOCUMENT_CATEGORIES
    ]


def unclassified_ids(workspace: Workspace) -> list[str]:
    """Transaction evidence still needing a type. Prose is not counted."""

    return [
        str(document.get("id"))
        for document in transaction_evidence(workspace)
        if not is_classified(workspace, str(document.get("id")))
    ]


def reclassifiable_ids(workspace: Workspace) -> list[str]:
    """Documents a rerun may revisit: the ``other`` bucket, model-assigned only,
    and only where the catalog has grown since they were classified.

    An auditor's ``other`` is a considered judgement that nothing fits, and is
    left alone. A model's ``other`` chosen from the current catalog is left alone
    too — the same question against the same list has the same answer, and
    re-asking it would mean classification never settles.
    """

    signature = catalog_signature(workspace)
    return [
        str(document.get("id"))
        for document in transaction_evidence(workspace)
        if is_other(workspace, str(document.get("id")))
        and not is_auditor_assigned(workspace, str(document.get("id")))
        and str(
            classification(workspace, str(document.get("id"))).get("catalog_sha1") or ""
        )
        != signature
    ]


def other_bucket(workspace: Workspace) -> list[dict]:
    """What an auditor is offered to retype, newest assignment first."""

    values = [
        {
            "document_id": str(document.get("id")),
            "title": str(document.get("title") or ""),
            **classification(workspace, str(document.get("id"))),
        }
        for document in workspace.documents
        if is_other(workspace, str(document.get("id")))
    ]
    return sorted(values, key=lambda item: str(item.get("assigned_at") or ""), reverse=True)


def assignments(workspace: Workspace) -> list[dict]:
    """Every classified document with its assignment, grouped type-first.

    The ``other`` bucket is only the subset that *announced* it needed attention.
    A confident wrong label is the more damaging case and never enters it: a
    broker's confirmation typed as the counterparty confirmation it accompanies
    pollutes the identifier the two would otherwise join on, and no amount of
    reviewing ``other`` would ever surface it. The store has always permitted
    correcting one (:func:`retype`); this is what lets an auditor find one.
    """

    values = [
        {
            "document_id": str(document.get("id")),
            "title": str(document.get("title") or ""),
            **classification(workspace, str(document.get("id"))),
        }
        for document in workspace.documents
        if is_classified(workspace, str(document.get("id")))
    ]
    return sorted(
        values,
        key=lambda item: (
            str(item.get("document_type") or ""),
            str(item.get("title") or ""),
        ),
    )


def types_present(workspace: Workspace) -> list[str]:
    """Distinct assigned types, excluding ``other``.

    Every type the corpus carries, which is a classification fact and reported
    as one. Induction expands over the narrower :func:`types_for_induction`.
    """

    return sorted({
        document_type(workspace, str(document.get("id")))
        for document in workspace.documents
        if is_classified(workspace, str(document.get("id")))
        and not is_other(workspace, str(document.get("id")))
    })


def types_for_induction(workspace: Workspace) -> list[str]:
    """Distinct types carried by transaction evidence, excluding ``other``.

    What the read expands over and what ``schemas_stamped`` measures against: a
    type nothing carries needs no schema, and a type only prose carries needs no
    schema either.
    """

    return sorted({
        document_type(workspace, str(document.get("id")))
        for document in transaction_evidence(workspace)
        if is_classified(workspace, str(document.get("id")))
        and not is_other(workspace, str(document.get("id")))
    })


def documents_of_type(workspace: Workspace, type_id: str) -> list[dict]:
    return [
        document
        for document in workspace.documents
        if document_type(workspace, str(document.get("id"))) == str(type_id)
    ]


def types_awaiting_schema(workspace: Workspace) -> list[str]:
    """Types the corpus's evidence carries that have no stamped schema yet.

    Under 4b.1 a schema is an *output* of reading the evidence rather than an
    input to it, so this reports a gap rather than naming work: what fills it is
    ``documents.evidence_read`` followed by ``documents.schemas_stamped``, and
    the stamp expands over types carrying a master instead.
    """

    return [
        type_id
        for type_id in types_for_induction(workspace)
        if document_schemas.load_schema(workspace, type_id) is None
    ]


# --------------------------------------------------------------------------- #
# the text the classifier reads
# --------------------------------------------------------------------------- #
def classification_text(
    workspace: Workspace,
    document_id: str,
    *,
    pages: int = CLASSIFICATION_PAGES,
    characters: int = CLASSIFICATION_CHARACTERS,
) -> str:
    extraction = documents.cached_extraction(workspace, str(document_id))
    if not extraction:
        return ""
    selected = sorted(
        (page for page in (extraction.get("pages") or []) if page.get("text")),
        key=lambda page: int(page.get("page") or 0),
    )[:max(1, int(pages))]
    text = "\n\n".join(str(page.get("text") or "").strip() for page in selected)
    return text[:max(1, int(characters))].strip()


def summary(workspace: Workspace) -> dict:
    """Counts the classification capability reports and the UI shows.

    Counted over transaction evidence, which is what classification runs over.
    Counting prose here would report an engagement as permanently part-classified
    against documents nothing intends to classify.
    """

    eligible = transaction_evidence(workspace)
    classified = [
        document for document in eligible
        if is_classified(workspace, str(document.get("id")))
    ]
    other = [
        document for document in classified
        if is_other(workspace, str(document.get("id")))
    ]
    return {
        "documents": len(eligible),
        "classified": len(classified),
        "unclassified": len(eligible) - len(classified),
        "other": len(other),
        "auditor_assigned": sum(
            1 for document in classified
            if is_auditor_assigned(workspace, str(document.get("id")))
        ),
        "types_present": types_present(workspace),
        "local_types": [str(item.get("id")) for item in document_schemas.local_types(workspace)],
    }
