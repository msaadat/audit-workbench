"""What a proposed ruleset actually does to this corpus.

Every number here is computed by local code from stored extractions. None of it
is model-supplied, and that is the point: an auditor approving a join key is
approving a claim about how documents relate, and the only honest way to show
that claim is to apply it and count.

Fan-out is the statistic that matters. A join key whose values reach one record
apiece is a transaction identifier; one whose values reach hundreds is an entity
identifier — a vendor number, a bank account, a cost centre — and approving it
would fuse every unrelated transaction into a single cluster. That is the most
damaging mistake available in this design, and it is invisible in the rule text.
Measuring it turns it into a number a reviewer can read without having to reason
about identifier semantics at all.
"""

from __future__ import annotations

import unicodedata
from typing import Iterable, Mapping

from . import document_analysis, document_classification, document_schemas
from .workspaces import Workspace


def normalize(value: object) -> str:
    """Presentation-only normalization, preserving punctuation and alphanumerics.

    Deliberately conservative. Case and surrounding or repeated whitespace are
    presentation: ``PO-2025/17`` and ``  po-2025/17 `` are the same reference
    written differently. Punctuation is not — ``PO-2025-17`` is a different
    reference, and a normalizer that stripped separators would silently merge
    the two.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.strip().split()).casefold()


def structured_records(
    workspace: Workspace, *, excluded: list[dict] | None = None
) -> list[dict]:
    """Every extraction made against a current schema, with its type attached.

    An analysis whose schema stamp no longer matches is excluded rather than
    reinterpreted — it was extracted against fields that have since changed, so
    reading it under today's schema would attribute values to a vocabulary that
    never produced them. An analysis whose stamp names a type the document no
    longer carries is excluded for the same reason from the other direction:
    the schema it names is current, but it is not this document's schema.

    Each exclusion is appended to ``excluded`` with its reason. A document that
    silently contributes nothing is the failure mode this whole design exists to
    remove, so the gap is reported rather than left to be inferred from a count.
    """

    rows: list[dict] = []
    for document in workspace.documents:
        document_id = str(document.get("id") or "")
        detail = document_analysis.load_analysis(workspace, document_id, document=document)
        artifact = detail.get("effective")

        def exclude(reason: str) -> None:
            if excluded is not None:
                excluded.append({"document_id": document_id, "reason": reason})

        if not artifact:
            continue
        if artifact.get("analysis_profile") != "structured":
            # An analysis written before the rules moved into the workspace was
            # extracted against a vocabulary that no longer exists. It stays
            # readable, and it is never cycle evidence: re-running
            # classification, induction and extraction is what makes it one.
            if artifact.get("records") or artifact.get("record_fragments"):
                exclude("legacy_pack_analysis")
            continue
        schema_ref = artifact.get("schema_ref") or {}
        document_type = document_classification.document_type(workspace, document_id)
        if not document_schemas.is_current(workspace, schema_ref):
            exclude("stale_schema_reference")
            continue
        if str(schema_ref.get("document_type") or "") != document_type:
            # Retyped since it was extracted. The stamp is still current — the
            # old type's schema never moved — so the staleness check above lets
            # it through, and taking the type from the stamp would file the
            # records under a type the auditor has said this document is not.
            # Re-analysis under the new type's schema is the repair, and it is
            # what ``has_usable_analysis`` now re-expands the chunks for.
            exclude("retyped_since_extraction")
            continue
        # The linker needs to know when an extraction moved under a stored
        # result, and it reads this same set: carrying the hash here is what
        # keeps that a single pass over the corpus rather than two.
        extraction_hash = str(
            artifact.get("content_sha1") or artifact.get("source_sha1") or ""
        )
        for position, record in enumerate(artifact.get("records") or []):
            rows.append({
                "document_id": document_id,
                "document_type": document_type,
                "record_index": position,
                "extraction_hash": extraction_hash,
                "record": record,
            })
    return rows


def join_value(raw: object, match: str) -> str:
    """One join key's comparison form of a printed value.

    ``exact_equal`` keeps the value as printed apart from surrounding
    whitespace. It exists for the identifier that is meaningfully
    case-sensitive or space-bearing, and choosing it is a decision the auditor
    makes when they approve the key.
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    return normalize(text) if match != "exact_equal" else text


def _values(
    record: Mapping[str, object], field_name: str, match: str = "normalized_equal"
) -> list[str]:
    return [
        join_value(field.get("value"), match)
        for field in record.get("fields") or []
        if str(field.get("name") or "") == field_name and str(field.get("value") or "").strip()
    ]


def _by_type(rows: Iterable[Mapping[str, object]]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("document_type") or ""), []).append(dict(row))
    return grouped


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def measure_join_key(
    join_key: Mapping[str, object],
    roles: Mapping[str, Mapping[str, object]],
    grouped: Mapping[str, list[dict]],
) -> dict:
    """Apply one join key to the corpus and count what it reaches."""

    left = join_key.get("left") or {}
    right = join_key.get("right") or {}
    left_type = str((roles.get(str(left.get("role"))) or {}).get("document_type") or "")
    right_type = str((roles.get(str(right.get("role"))) or {}).get("document_type") or "")
    left_rows = grouped.get(left_type) or []
    right_rows = grouped.get(right_type) or []
    match = str(join_key.get("match") or "normalized_equal")

    # Keyed on the record, not the document, because that is the unit the
    # linker traverses. Counting documents here would report a fan-out an
    # auditor approved and the engine then exceeded on a multi-record voucher.
    right_index: dict[str, set[tuple[str, int]]] = {}
    for row in right_rows:
        for value in _values(row["record"], str(right.get("field")), match):
            right_index.setdefault(value, set()).add(
                (str(row["document_id"]), int(row.get("record_index") or 0))
            )

    fan_out: list[int] = []
    matched_pairs = 0
    unmatched = 0
    left_documents: set[str] = set()
    for row in left_rows:
        left_documents.add(str(row["document_id"]))
        reached: set[tuple[str, int]] = set()
        stated = _values(row["record"], str(left.get("field")), match)
        for value in stated:
            reached |= right_index.get(value, set())
        if not stated:
            # A record that never states the key is not evidence the key is bad;
            # it is a document this rule has nothing to say about.
            continue
        fan_out.append(len(reached))
        matched_pairs += len(reached)
        if not reached:
            unmatched += 1
    return {
        "left_documents": len(left_documents),
        "right_documents": len({str(row["document_id"]) for row in right_rows}),
        "left_stating_key": len(fan_out),
        "matched_pairs": matched_pairs,
        "left_unmatched": unmatched,
        # The distribution, not just an average: one runaway value is exactly
        # what an average hides, and it is the case that matters.
        "fan_out_p50": _percentile(fan_out, 0.5),
        "fan_out_p95": _percentile(fan_out, 0.95),
        "fan_out_max": max(fan_out) if fan_out else 0,
    }


def measure_assertion(
    assertion: Mapping[str, object],
    roles: Mapping[str, Mapping[str, object]],
    grouped: Mapping[str, list[dict]],
) -> dict:
    """Count how many records could actually be tested by one assertion.

    An assertion nothing can evaluate is not failing — it is silent, which is
    worse, because a rule that never runs looks the same as one that always
    passes.
    """

    def stating(operand: object) -> int:
        item = operand or {}
        document_type = str(
            (roles.get(str(item.get("role"))) or {}).get("document_type") or ""
        )
        field = str(item.get("field") or "")
        return sum(
            1
            for row in grouped.get(document_type) or []
            if _values(row["record"], field)
        )

    left = stating(assertion.get("left"))
    right = stating(assertion.get("right")) if assertion.get("right") else None
    evaluable = left if right is None else min(left, right)
    return {
        "left_stating": left,
        "right_stating": right,
        "evaluable_records": evaluable,
        "silent": evaluable == 0,
    }


def measure(workspace: Workspace, ruleset: Mapping[str, object]) -> dict:
    """Measure every rule in one ruleset against the workspace's extractions."""

    grouped = _by_type(structured_records(workspace))
    roles = {str(role.get("name")): role for role in ruleset.get("roles") or []}
    return {
        "join_keys": {
            str(item.get("id")): measure_join_key(item, roles, grouped)
            for item in ruleset.get("join_keys") or []
        },
        "assertions": {
            str(item.get("id")): measure_assertion(item, roles, grouped)
            for item in ruleset.get("assertions") or []
        },
        "records_measured": sum(len(rows) for rows in grouped.values()),
    }


#: Above this, a join key is reaching so many records per value that it is
#: almost certainly an entity identifier rather than a transaction one.
ENTITY_FAN_OUT = 5


def concerns(measured: Mapping[str, object]) -> list[dict]:
    """Turn the measurements into the specific things a reviewer should look at.

    Stated as observations rather than refusals: an engagement can have a
    legitimately one-to-many cycle, and it is the auditor's call. What must not
    happen is the number going unnoticed.
    """

    found: list[dict] = []
    for key, stats in (measured.get("join_keys") or {}).items():
        if stats.get("fan_out_p95", 0) > ENTITY_FAN_OUT:
            found.append({
                "rule": key,
                "kind": "join_key",
                "concern": "entity_fan_out",
                "detail": (
                    f"Values of this key reach {stats['fan_out_p95']} records at the "
                    "95th percentile. A transaction key reaches about one; this "
                    "looks like an entity identifier, and joining on it would fuse "
                    "unrelated transactions."
                ),
            })
        if stats.get("left_stating_key") and stats.get("left_unmatched"):
            share = stats["left_unmatched"] / stats["left_stating_key"]
            if share >= 0.5:
                found.append({
                    "rule": key,
                    "kind": "join_key",
                    "concern": "poor_coverage",
                    "detail": (
                        f"{stats['left_unmatched']} of {stats['left_stating_key']} "
                        "records stating this key match nothing. The field may be "
                        "inconsistently named or inconsistently present."
                    ),
                })
    for key, stats in (measured.get("assertions") or {}).items():
        if stats.get("silent"):
            found.append({
                "rule": key,
                "kind": "assertion",
                "concern": "silent",
                "detail": (
                    "No record states both sides of this comparison, so it would "
                    "never run. A rule that never runs looks the same as one that "
                    "always passes."
                ),
            })
    return found
