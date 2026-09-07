"""A typed record population: what a document step is written *against*.

A Q&A item has always named documents by id. That works when a test asks one
named contract a question and fails completely when a row asks something of
*every* voucher: the ids have to be enumerated at generation time, by a turn
that was shown six of them, and a voucher imported the next day is not in the
test.

A population names a document **type** and the schema fields the question is
read from, and the workspace resolves it — every run, from the readings that
exist at that moment. Nothing in the test enumerates ids.

The unit of assessment is the **record**, not the document. A voucher pack
holds three line items and each is a separate transaction; assessing the pack
as one unit would let two clean lines carry a third that is not, which is the
same silent aggregation this module exists to remove. ``structured_records``
has always been record-grained and the cycle engine has always traversed it
that way; this makes document tests agree with both.

Everything here is read-only. :mod:`doc_tests` owns the writes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

from . import document_classification, document_context, document_schemas
from .workspaces import Workspace, WorkspaceError

#: ``all`` is the whole resolved population; ``sample`` draws from it with the
#: same deterministic sampler an approved cycle selection is drawn with.
SELECTION_MODES = frozenset({"all", "sample"})
SAMPLING_METHODS = frozenset({"random", "interval", "stratified"})

#: One model call per record, so the population is what the run costs. Above
#: this the population is still resolved and still reported in full — what
#: changes is that the assessed set is stated as partial rather than presented
#: as complete coverage.
MAX_POPULATION_RECORDS = 500
#: A question reads a handful of fields; a step naming every one of them is
#: describing the schema rather than asking something of it. Raised from 12
#: because a real fraud question over a 16-field deal ticket wants most of the
#: transactional ones, and 12 refused it — the cap is meant to catch a step
#: that names the whole schema, not one that reads widely on purpose.
MAX_POPULATION_FIELDS = 20
MAX_CRITERIA_REFS = 4
#: How many identifying fields a grid row leads with when the schema marks no
#: identifier — see :func:`identifier_fields`.
IDENTIFIER_FIELDS = 2
#: The excerpt one criteria reference resolves to. Criteria are quoted, not
#: reproduced: the answer cites the section, it does not restate the policy.
CRITERIA_EXCERPT_CHARACTERS = 4_000

#: Separates a document from the record inside it in an assessment key. Chosen
#: because a document id is hex and can never contain it, so an old key that is
#: a bare document id stays unambiguously a document-grained one.
UNIT_SEPARATOR = "#"


def _sha1(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# assessment units
# --------------------------------------------------------------------------- #
def unit_key(document_id: str, record_index: object = None) -> str:
    """The key one assessment is stored under.

    A document-grained item keeps the bare document id it has always used, so
    every stored ``qa_answers`` map keeps resolving. A record-grained one adds
    the record's position within its document.
    """

    if record_index is None:
        return str(document_id)
    return f"{document_id}{UNIT_SEPARATOR}{int(record_index)}"


def parse_unit_key(key: str) -> tuple[str, int | None]:
    document_id, separator, index = str(key).partition(UNIT_SEPARATOR)
    if not separator:
        return document_id, None
    try:
        return document_id, int(index)
    except ValueError:
        return document_id, None


def has_population(item: Mapping[str, object]) -> bool:
    return bool((item or {}).get("population"))


def assessment_units(item: Mapping[str, object]) -> list[dict]:
    """Every (document, record) the item is assessed over, in stable order.

    Pure over the stored item: resolution already happened and wrote its result
    onto the population, so a budget or a rollup never has to reopen the corpus
    to learn how many calls an item costs.
    """

    population = (item or {}).get("population") or {}
    if population:
        return [
            {
                "document_id": str(record.get("document_id") or ""),
                "record_index": int(record.get("record_index") or 0),
                "key": unit_key(
                    str(record.get("document_id") or ""),
                    int(record.get("record_index") or 0),
                ),
            }
            for record in population.get("resolved_records") or []
        ]
    return [
        {"document_id": str(document_id), "record_index": None, "key": str(document_id)}
        for document_id in (item or {}).get("document_ids") or []
    ]


# --------------------------------------------------------------------------- #
# schema helpers
# --------------------------------------------------------------------------- #
def schema_field_names(schema: Mapping[str, object] | None) -> list[str]:
    return [str(field.get("name") or "") for field in (schema or {}).get("fields") or []]


def identifier_fields(
    schema: Mapping[str, object] | None, *, limit: int = IDENTIFIER_FIELDS
) -> list[str]:
    """The fields that say *which* record a grid row is.

    The schema marks roles, so an ``identifier`` field is the answer where one
    exists. Where none does, the first verbatim fields are the fallback: a
    verbatim field is one the reader copied off the page rather than derived,
    so it is at least something an auditor can find again in the document.
    """

    fields = list((schema or {}).get("fields") or [])
    identifiers = [
        str(field.get("name") or "")
        for field in fields
        if str(field.get("role") or "") == "identifier"
    ]
    if identifiers:
        return identifiers[:limit]
    return [
        str(field.get("name") or "")
        for field in fields
        if bool(field.get("verbatim", True))
    ][:limit]


def evidence_type_items(workspace: Workspace) -> list[dict]:
    """One item per document type: what the engagement holds, not which files.

    This is what replaces twelve — or eighty-four — per-document identity items
    in the generation context. A step names a type and the workspace resolves
    the documents, so the ids were never what the turn needed; the counts are,
    because a requirement written against a population cannot be answered by a
    type carrying one record.
    """

    from . import cycle_linking

    records, _hashes = cycle_linking.structured_evidence(workspace)
    by_type: dict[str, list[dict]] = {}
    for record in records:
        by_type.setdefault(str(record.get("document_type") or ""), []).append(record)
    items = []
    for entry in document_classification.evidence_type_counts(workspace):
        document_type = str(entry.get("document_type") or "")
        rows = by_type.get(document_type) or []
        schema = document_schemas.load_schema(workspace, document_type)
        items.append(
            {
                "document_type": document_type,
                "documents": int(entry.get("documents") or 0),
                # The population a step over this type actually runs over, and
                # the one whose size decides whether the question is worth
                # asking. A type whose documents carry no current reading
                # reports zero here beside a non-zero document count, which is
                # the gap being stated rather than left to be inferred.
                "records": len(rows),
                "sample_document_ids": sorted({
                    str(row.get("document_id") or "") for row in rows
                })[:3],
                "schema_ref": (
                    {
                        "document_type": document_type,
                        "schema_version": schema.get("schema_version"),
                        "schema_hash": schema.get("schema_hash"),
                    }
                    if schema
                    else None
                ),
            }
        )
    return items


# --------------------------------------------------------------------------- #
# declaration
# --------------------------------------------------------------------------- #
def normalize_criteria_ref(workspace: Workspace, raw: object) -> dict:
    """Normalize one reference to the text an answer is judged against.

    Two shapes, because criteria live in two places. Most name a policy or SOP
    document and a section of it. Some exist only in the RCM row's own
    ``criteria`` field — a control described in the matrix and nowhere else —
    and refusing those would push the model into restating the criterion inside
    the question, where nothing can check it against a source.
    """

    if isinstance(raw, str):
        text = raw.strip()
        kind, separator, rest = text.partition(":")
        if separator and kind == "rcm":
            rcm_id, _hash, field = rest.partition("#")
            raw = {"rcm_id": rcm_id.strip(), "field": (field or "criteria").strip()}
        elif text:
            raw = {"document_id": text}
    if not isinstance(raw, Mapping):
        raise WorkspaceError("A criteria reference must be an object.")
    rcm_id = str(raw.get("rcm_id") or "").strip()
    if rcm_id:
        known = {str(row.get("id")) for row in workspace.rcm}
        if rcm_id not in known:
            raise WorkspaceError(f"Criteria reference names unknown RCM row '{rcm_id}'.")
        field = str(raw.get("field") or "criteria").strip() or "criteria"
        if field not in {"criteria", "control", "risk"}:
            raise WorkspaceError(
                f"An RCM criteria reference may name criteria, control, or risk, not '{field}'."
            )
        return {"kind": "rcm", "rcm_id": rcm_id, "field": field}
    document_id = str(raw.get("document_id") or "").strip()
    if not document_id:
        raise WorkspaceError(
            "A criteria reference needs a document_id or an rcm_id."
        )
    known = {str(document.get("id")) for document in workspace.documents}
    if document_id not in known:
        raise WorkspaceError(
            f"Criteria reference names unknown document '{document_id}'."
        )
    return {
        "kind": "document",
        "document_id": document_id,
        "section": str(raw.get("section") or "").strip(),
    }


def normalize_selection(raw: object) -> dict:
    if raw in (None, ""):
        return {"mode": "all"}
    if not isinstance(raw, Mapping):
        raise WorkspaceError("A population selection must be an object.")
    mode = str(raw.get("mode") or "all").strip()
    if mode not in SELECTION_MODES:
        raise WorkspaceError(
            f"A population selection mode must be all or sample, not '{mode}'."
        )
    if mode == "all":
        return {"mode": "all"}
    method = str(raw.get("method") or "").strip()
    if method not in SAMPLING_METHODS:
        raise WorkspaceError(
            f"A population sample method must be one of {sorted(SAMPLING_METHODS)}."
        )
    try:
        size = int(raw.get("size"))
    except (TypeError, ValueError) as error:
        raise WorkspaceError("A population sample needs an integer size.") from error
    if size < 1 or size > MAX_POPULATION_RECORDS:
        raise WorkspaceError(
            f"A population sample size must be between 1 and {MAX_POPULATION_RECORDS}."
        )
    try:
        seed = int(raw.get("seed") or 0)
    except (TypeError, ValueError) as error:
        raise WorkspaceError("A population sample needs an integer seed.") from error
    selection = {"mode": "sample", "method": method, "size": size, "seed": seed}
    if method == "stratified":
        stratify_by = str(raw.get("stratify_by") or "").strip()
        if not stratify_by:
            raise WorkspaceError("A stratified sample needs a stratify_by field.")
        selection["stratify_by"] = stratify_by
    return selection


def normalize_population(workspace: Workspace, raw: object) -> dict:
    """Validate one declared population and return it without resolving it.

    The type must have a stamped schema and every named field must belong to
    it. Both refusals say the same thing in the two directions it can be wrong:
    a question about a field no document of that type records is unanswerable,
    and the honest report is that the *type carries no such field*, not that no
    documents exist.
    """

    if not isinstance(raw, Mapping):
        raise WorkspaceError("A population must be an object.")
    document_type = str(raw.get("document_type") or "").strip()
    if not document_type:
        raise WorkspaceError("A population needs a document_type.")
    schema = document_schemas.load_schema(workspace, document_type)
    if schema is None:
        raise WorkspaceError(
            f"Population names '{document_type}', a type this engagement holds no schema for."
        )
    known_fields = set(schema_field_names(schema))
    fields = [str(value or "").strip() for value in raw.get("fields") or []]
    fields = [value for value in fields if value]
    if not fields:
        raise WorkspaceError(
            f"A population over '{document_type}' must name at least one schema field."
        )
    if len(fields) > MAX_POPULATION_FIELDS:
        raise WorkspaceError(
            f"A population may read at most {MAX_POPULATION_FIELDS} fields."
        )
    unknown = [value for value in fields if value not in known_fields]
    if unknown:
        raise WorkspaceError(
            f"'{unknown[0]}' is not a field of the '{document_type}' schema."
        )
    criteria_refs = list(raw.get("criteria_refs") or [])
    if len(criteria_refs) > MAX_CRITERIA_REFS:
        raise WorkspaceError(
            f"A population may name at most {MAX_CRITERIA_REFS} criteria references."
        )
    return {
        "document_type": document_type,
        "fields": list(dict.fromkeys(fields)),
        "selection": normalize_selection(raw.get("selection")),
        "criteria_refs": [
            normalize_criteria_ref(workspace, value) for value in criteria_refs
        ],
        "schema_ref": {
            "document_type": document_type,
            "schema_version": schema.get("schema_version"),
            "schema_hash": schema.get("schema_hash"),
        },
    }


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _stated(record: Mapping[str, object], field_name: str) -> list[Mapping[str, object]]:
    return [
        field
        for field in record.get("fields") or []
        if str(field.get("name") or "") == field_name
        and str(field.get("value") or "").strip()
    ]


def _sample(rows: list[dict], selection: Mapping[str, object], fields: list[str]) -> list[dict]:
    """Draw a deterministic sample with the sampler an approved cycle uses.

    The rows are projected into a frame first — one column per named field, plus
    the record's own coordinates — so ``stratified`` can stratify by a schema
    field and ``random`` hashes the values rather than the position. Reusing the
    cycle sampler rather than writing a second one keeps one audited definition
    of what "seed 7, interval, 25" means across the whole product.
    """

    import polars as pl

    from .cycle_vouching import _sample_row_indices

    # Stratifying by a field the question does not read is legitimate — the
    # stratum is a property of the population, not of the assertion — so the
    # column is projected whether or not the question names it.
    stratify_by = str(selection.get("stratify_by") or "")
    columns = list(dict.fromkeys([*fields, stratify_by] if stratify_by else fields))
    frame = pl.DataFrame(
        [
            {
                "document_id": str(row.get("document_id") or ""),
                "record_index": int(row.get("record_index") or 0),
                **{
                    name: (
                        str((_stated(row, name) or [{}])[0].get("value") or "")
                    )
                    for name in columns
                },
            }
            for row in rows
        ]
    )
    return [rows[index] for index in _sample_row_indices(frame, selection)]


def resolve(workspace: Workspace, population: Mapping[str, object]) -> dict:
    """Resolve a declared population against the workspace as it stands now.

    Three lists come back, and the second and third are the point. ``records``
    is what will be assessed. ``unread_documents`` is every document of the type
    that produced no current reading — retyped, re-extracted against a schema
    that has since moved, or never analysed — because a document that silently
    contributes nothing is indistinguishable from one that passed. ``omitted``
    is what a cap or a sample left out, so a partial run is never reported as
    full coverage.
    """

    from . import cycle_linking

    document_type = str(population.get("document_type") or "")
    fields = [str(value) for value in population.get("fields") or []]
    records, extraction_hashes = cycle_linking.structured_evidence(workspace)
    rows = [
        row for row in records if str(row.get("document_type") or "") == document_type
    ]
    typed_documents = document_classification.documents_of_type(workspace, document_type)
    with_records = {str(row.get("document_id") or "") for row in rows}
    unread_documents = [
        {
            "document_id": str(document.get("id") or ""),
            "title": str(document.get("title") or document.get("source") or ""),
        }
        for document in typed_documents
        if str(document.get("id") or "") not in with_records
    ]
    selection = population.get("selection") or {"mode": "all"}
    population_size = len(rows)
    if str(selection.get("mode") or "all") == "sample" and rows:
        selected = _sample(rows, selection, fields)
    else:
        selected = list(rows)
    capped = len(selected) > MAX_POPULATION_RECORDS
    if capped:
        selected = selected[:MAX_POPULATION_RECORDS]
    resolved_records = [
        {
            "document_id": str(row.get("document_id") or ""),
            "record_index": int(row.get("record_index") or 0),
            "record_id": str(row.get("record_id") or ""),
            "content_sha1": str(row.get("content_hash") or ""),
        }
        for row in selected
    ]
    return {
        "resolved_records": resolved_records,
        "resolved_document_ids": list(
            dict.fromkeys(record["document_id"] for record in resolved_records)
        ),
        "unread_documents": unread_documents,
        "population_records": population_size,
        "population_documents": len(typed_documents),
        "omitted_records": population_size - len(resolved_records),
        "capped": capped,
        "inputs_sha1": _inputs_sha1(
            workspace, population, resolved_records, unread_documents, extraction_hashes
        ),
        "resolved_at": _utcnow(),
    }


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _criteria_sha1(workspace: Workspace, criteria_refs: Iterable[Mapping[str, object]]) -> list:
    """What the answers were judged against, so an edited policy re-runs them."""

    documents = {
        str(document.get("id")): str(document.get("sha1") or "")
        for document in workspace.documents
    }
    rows = {str(row.get("id")): row for row in workspace.rcm}
    stamped = []
    for ref in criteria_refs or []:
        if str(ref.get("kind") or "") == "rcm":
            row = rows.get(str(ref.get("rcm_id") or "")) or {}
            stamped.append(
                [
                    "rcm",
                    str(ref.get("rcm_id") or ""),
                    _sha1(str(row.get(str(ref.get("field") or "criteria")) or "")),
                ]
            )
            continue
        document_id = str(ref.get("document_id") or "")
        stamped.append(
            ["document", document_id, documents.get(document_id, ""), str(ref.get("section") or "")]
        )
    return stamped


def _inputs_sha1(
    workspace: Workspace,
    population: Mapping[str, object],
    resolved_records: list[dict],
    unread_documents: list[dict],
    extraction_hashes: Mapping[str, str],
) -> str:
    """Fingerprint everything one run of this population consumes.

    The records and their content, the readings behind them, the documents that
    contributed none, the fields read, the criteria judged against, and the
    selection drawn. A voucher imported since, a reading re-run, a policy
    edited, or a field added to the question all move this — which is what
    tells a current population from one that has to be drawn again.
    """

    return _sha1(
        {
            "document_type": str(population.get("document_type") or ""),
            "fields": [str(value) for value in population.get("fields") or []],
            "selection": dict(population.get("selection") or {}),
            "criteria": _criteria_sha1(workspace, population.get("criteria_refs") or []),
            "records": [
                [
                    record["document_id"],
                    record["record_index"],
                    record["content_sha1"],
                    extraction_hashes.get(record["document_id"], ""),
                ]
                for record in resolved_records
            ],
            "unread_documents": [
                entry["document_id"] for entry in unread_documents
            ],
        }
    )


def resolved_population(workspace: Workspace, population: Mapping[str, object]) -> dict:
    """A validated population with the current resolution written onto it."""

    normalized = normalize_population(workspace, population)
    return {**normalized, **resolve(workspace, normalized)}


def is_current(workspace: Workspace, population: Mapping[str, object]) -> bool:
    """Whether the stored resolution still describes the corpus.

    Cheap enough for a read: it re-resolves and compares fingerprints, which is
    the same walk over the structured records the cycle projection already pays
    for and shares a request cache with.
    """

    stored = str((population or {}).get("inputs_sha1") or "")
    if not stored:
        return False
    try:
        normalized = normalize_population(workspace, population)
    except WorkspaceError:
        return False
    return stored == resolve(workspace, normalized)["inputs_sha1"]


def assurance_scope(population: Mapping[str, object]) -> str:
    """Structural coverage, read from the resolution rather than the intent.

    A selection saying ``all`` over a population a cap or a missing reading cut
    into is not full coverage, and reporting it as such is the specific lie this
    field exists to prevent.
    """

    mode = str(((population or {}).get("selection") or {}).get("mode") or "all")
    if mode == "sample":
        return "sampled_population"
    if population.get("capped") or int(population.get("omitted_records") or 0):
        return "sampled_population"
    if population.get("unread_documents"):
        return "sampled_population"
    return "full_population"


# --------------------------------------------------------------------------- #
# what one assessment reads
# --------------------------------------------------------------------------- #
def _analysis_citations(workspace: Workspace, document_id: str) -> dict[str, dict]:
    from . import document_analysis

    detail = document_analysis.load_analysis(
        workspace, str(document_id), with_status=False
    )
    artifact = detail.get("effective") or {}
    return {
        str(citation.get("id") or ""): dict(citation)
        for citation in artifact.get("citations") or []
    }


def citation_anchor(
    workspace: Workspace, document_id: str, citation_id: str
) -> dict | None:
    """Resolve a reading's citation id to the page and excerpt it stands on.

    A field citation is an identity inside one analysis; an evidence anchor is a
    page and an exact excerpt. The reading already carries both halves, so an
    answer citing ``expense_description`` is grounded in a real page without the
    model ever being shown one.
    """

    citation = _analysis_citations(workspace, document_id).get(str(citation_id))
    if citation is None:
        return None
    return {
        "page": int(citation.get("page") or 1),
        "excerpt": str(citation.get("excerpt") or ""),
    }


def record_projection(
    workspace: Workspace,
    document_id: str,
    record_index: int,
    *,
    fields: Iterable[str],
    identifiers: Iterable[str] = (),
) -> dict:
    """One record, projected to what the question reads and what names it.

    A few hundred characters of JSON where a page excerpt was twenty-six
    thousand, and every value already carries the citation the reading recorded
    for it. ``missing_fields`` is what the reading does not state: the executor
    reaches for pages only for those, so an ordinary answer never costs a page
    fetch.
    """

    from . import cycle_linking

    records, _hashes = cycle_linking.structured_evidence(workspace)
    row = next(
        (
            value
            for value in records
            if str(value.get("document_id") or "") == str(document_id)
            and int(value.get("record_index") or 0) == int(record_index)
        ),
        None,
    )
    if row is None:
        raise WorkspaceError(
            f"Document '{document_id}' has no current record {record_index}."
        )
    document = next(
        (
            value
            for value in workspace.documents
            if str(value.get("id")) == str(document_id)
        ),
        None,
    )
    names = list(dict.fromkeys([*identifiers, *fields]))
    stated: dict[str, list[dict]] = {}
    missing: list[str] = []
    for name in names:
        entries = _stated(row, name)
        if not entries:
            missing.append(name)
            continue
        stated[name] = [
            {
                "value": entry.get("value"),
                "citation": str(entry.get("citation") or ""),
            }
            for entry in entries
        ]
    return {
        "document_id": str(document_id),
        "document_title": str(
            (document or {}).get("title") or (document or {}).get("source") or ""
        ),
        "document_type": str(row.get("document_type") or ""),
        "record_index": int(record_index),
        "record_id": str(row.get("record_id") or ""),
        "identifiers": [name for name in identifiers if name in stated],
        "fields": stated,
        # Named rather than omitted: a field the reading does not state is the
        # thing the question was about, and silence about it reads as a value.
        "missing_fields": missing,
    }


def criteria_excerpts(
    workspace: Workspace,
    criteria_refs: Iterable[Mapping[str, object]],
    *,
    question: str = "",
) -> list[dict]:
    """The text each criteria reference resolves to, bounded and cited."""

    excerpts = []
    rows = {str(row.get("id")): row for row in workspace.rcm}
    for ref in criteria_refs or []:
        if str(ref.get("kind") or "") == "rcm":
            row = rows.get(str(ref.get("rcm_id") or "")) or {}
            field = str(ref.get("field") or "criteria")
            text = str(row.get(field) or "").strip()
            if not text:
                continue
            excerpts.append(
                {
                    "kind": "rcm",
                    "rcm_id": str(ref.get("rcm_id") or ""),
                    "label": f"{ref.get('rcm_id')} {field}",
                    "text": text[:CRITERIA_EXCERPT_CHARACTERS],
                    "citations": [],
                }
            )
            continue
        document_id = str(ref.get("document_id") or "")
        section = str(ref.get("section") or "")
        query = " ".join(part for part in (f"section {section}" if section else "", question) if part)
        try:
            context = document_context.get_document_context(
                workspace,
                document_id,
                "search_excerpts" if query.strip() else "summary",
                query=query.strip() or None,
                max_characters=CRITERIA_EXCERPT_CHARACTERS,
                purpose="document_population_criteria",
                stage="document_qa",
                record_activity=False,
            )
        except WorkspaceError:
            continue
        text = str(context.get("content") or "").strip()
        if not text:
            continue
        excerpts.append(
            {
                "kind": "document",
                "document_id": document_id,
                "section": section,
                "label": " ".join(
                    part
                    for part in (str(context.get("title") or document_id), f"§{section}" if section else "")
                    if part
                ),
                "text": text,
                "citations": list(context.get("citations") or []),
            }
        )
    return excerpts


__all__ = [
    "CRITERIA_EXCERPT_CHARACTERS",
    "IDENTIFIER_FIELDS",
    "MAX_CRITERIA_REFS",
    "MAX_POPULATION_FIELDS",
    "MAX_POPULATION_RECORDS",
    "SAMPLING_METHODS",
    "SELECTION_MODES",
    "UNIT_SEPARATOR",
    "assessment_units",
    "assurance_scope",
    "citation_anchor",
    "criteria_excerpts",
    "evidence_type_items",
    "has_population",
    "identifier_fields",
    "is_current",
    "normalize_criteria_ref",
    "normalize_population",
    "normalize_selection",
    "parse_unit_key",
    "record_projection",
    "resolve",
    "resolved_population",
    "schema_field_names",
    "unit_key",
]
