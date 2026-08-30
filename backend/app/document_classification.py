"""Document type assignment: what a document *is*, and who decided.

Type is orthogonal to intake's ``category``. Category governs routing and the
planning/voucher boundary — whether this engagement holds a document as
transaction evidence at all; type governs what fields it is read under and which
role it may fill in a cycle. Neither derives from the other, and a document may
carry both — ``category: contract, document_type: employment_contract``. Both
have to say yes before a schema is induced: an approval matrix is genuinely a
``delegation_of_authority`` and genuinely still policy.

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

import hashlib
import json
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
    root = (workspace.root / DIRNAME).resolve()
    path = (root / f"{str(document_id)}.json").resolve()
    if not path.is_relative_to(root):
        raise WorkspaceError("Unsafe document classification reference.")
    return path


def _require_document(workspace: Workspace, document_id: str) -> dict:
    for item in workspace.documents:
        if str(item.get("id")) == str(document_id):
            return item
    raise WorkspaceError(f"Document '{document_id}' not found.")


def _read(workspace: Workspace, document_id: str) -> dict:
    try:
        path = _path(workspace, document_id)
    except WorkspaceError:
        return empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty()
    return value if isinstance(value, dict) else empty()


def empty() -> dict:
    return {
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
        if str(document.get("category") or "") in intake.VOUCHER_DOCUMENT_CATEGORIES
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

    What induction expands over and what ``schemas_induced`` measures against: a
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


#: How many documents of a type are read to induce its schema. Two is enough to
#: corroborate; a third is bought only where the type is common enough that two
#: could easily share a layout by chance.
INDUCTION_SAMPLES = 2
INDUCTION_SAMPLES_HIGH_VOLUME = 3
HIGH_VOLUME_DOCUMENTS = 10

#: How much of a sample the induction worker reads. Larger than the classifier's
#: window: naming a document needs its first page, but listing the fields it
#: carries needs the body, and a field that only appears late would otherwise be
#: missing from every extraction of that type.
INDUCTION_CHARACTERS = 12000
INDUCTION_PAGES = 3


def _stratum(document: Mapping[str, object]) -> tuple[str, str]:
    """A cheap heterogeneity signal to spread samples across.

    Neither half is evidence of layout on its own. Together they separate the
    common case this guards against: two hundred invoices from a dozen vendors,
    where the first two in identifier order happen to come from one of them and
    the schema is frozen on a view of the corpus that most of it does not share.
    """

    path = str(document.get("relative_path") or "")
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    pages = int(document.get("pages") or 0)
    bucket = "1" if pages <= 1 else "2-4" if pages <= 4 else "5+"
    return folder, bucket


def sample_for_induction(
    workspace: Workspace, type_id: str, *, limit: int | None = None
) -> list[str]:
    """Pick the documents whose fields define this type's schema.

    Spread across strata rather than taken in identifier order, then round-robin
    so the picks come from as many strata as there are picks. Deterministic
    throughout: the same corpus yields the same sample, which is what lets a unit
    be re-expanded without silently inducing against different documents.
    """

    eligible = {str(document.get("id")) for document in transaction_evidence(workspace)}
    candidates = [
        document
        for document in documents_of_type(workspace, type_id)
        if str(document.get("id")) in eligible
    ]
    if not candidates:
        return []
    wanted = int(
        limit
        if limit is not None
        else (
            INDUCTION_SAMPLES_HIGH_VOLUME
            if len(candidates) >= HIGH_VOLUME_DOCUMENTS
            else INDUCTION_SAMPLES
        )
    )
    strata: dict[tuple[str, str], list[str]] = {}
    for document in sorted(candidates, key=lambda item: str(item.get("id"))):
        strata.setdefault(_stratum(document), []).append(str(document.get("id")))
    picked: list[str] = []
    while len(picked) < wanted:
        drawn = False
        for key in sorted(strata):
            group = strata[key]
            if not group:
                continue
            picked.append(group.pop(0))
            drawn = True
            if len(picked) >= wanted:
                break
        if not drawn:
            break
    return picked


def induction_text(
    workspace: Workspace,
    document_id: str,
    *,
    pages: int = INDUCTION_PAGES,
    characters: int = INDUCTION_CHARACTERS,
) -> str:
    return classification_text(
        workspace, document_id, pages=pages, characters=characters
    )


def types_awaiting_schema(workspace: Workspace) -> list[str]:
    """Assigned types with no current schema. What induction expands over."""

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
