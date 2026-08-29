"""The vocabulary-agnostic half of cycle evidence testing.

What survives here never depended on where the vocabulary came from: value
normalization, the deterministic sampler, the
citation catalogue, the result rollup, the grid projection, and the state
words an auditor's dispositions are recorded in.

Where the vocabulary *does* matter — which documents link, what a role means,
what must agree — the work is :mod:`cycle_linking`'s, against a ruleset the
auditor approved. The entry points below delegate to it, and exist because a
great many callers reach cycle vouching through this module's names.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import unicodedata
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import polars as pl



class CycleSchemaError(ValueError):
    """A cycle-evidence payload violates the closed schema."""

    def __init__(self, errors: str | Iterable[str]):
        values = (errors,) if isinstance(errors, str) else tuple(errors)
        normalized = tuple(str(item).strip() for item in values if str(item).strip())
        if not normalized:
            normalized = ("The payload violates the cycle schema.",)
        #: Every independent violation found, not just the first. A caller that
        #: feeds violations back to a model needs all of them: repairing one of
        #: five and being told nothing about the other four cannot converge.
        self.errors = normalized
        super().__init__("; ".join(normalized))


class SelectionConfirmationRequired(CycleSchemaError):
    """An evidence-linked reach exceeds the item cap and needs a sample decision.

    Carried as an exception rather than an alternate return value so no caller
    can mistake the deterministic sample proposal for a persisted test.
    """

    def __init__(self, proposal: dict) -> None:
        super().__init__(str(proposal.get("reason") or "Confirm a deterministic sample."))
        self.proposal = proposal


SCHEMA_VERSION = 2
CARDINALITIES = frozenset({"one", "many"})
REUSE_RULES = frozenset({"exclusive", "allowed"})
ASSURANCE_SCOPES = frozenset({"targeted_evidence_only", "sampled_population"})
SELECTION_MODES = frozenset({"evidence_linked", "sample"})
SAMPLING_METHODS = frozenset({"random", "interval", "stratified"})
ASSERTIONS = frozenset(
    {
        "Existence",
        "Completeness",
        "Accuracy",
        "Authorization",
        "Valuation",
        "Cut-off",
        "Compliance",
        "Operational",
    }
)
ENTRY_QUANTIFIERS = frozenset({"one", "any", "all"})
ROLE_QUANTIFIERS = frozenset({"all", "any"})
NORMALIZATION_STATUSES = frozenset({"normalized", "invalid"})
EVALUATION_STATES = frozenset(
    {"not_run", "passed", "failed", "incomplete", "needs_review", "stale"}
)
CURRENT_EVALUATION_STATES = frozenset(
    {"passed", "failed", "incomplete", "needs_review"}
)
DISPOSITION_STATES = frozenset({"pending", "confirmed", "exception"})
#: ``cannot_determine`` is the reader's answer, and it is a real answer rather
#: than a failure to produce one: the operands resolved, and what they state
#: still does not settle the requirement — a reference ambiguous in a way only
#: scanning produces, a field too damaged to read. It is deliberately distinct
#: from ``missing_evidence`` and ``invalid_extraction``, which are resolution
#: failures decided locally before anything is judged. All three roll up to
#: ``incomplete``: none of them is a tested pass.
ASSERTION_VERDICTS = frozenset(
    {
        "match",
        "mismatch",
        "cannot_determine",
        "missing_evidence",
        "invalid_extraction",
        "ambiguous",
        "not_run",
    }
)
#: What the reader may return for a pair it was asked to judge, and how each
#: lands in the durable vocabulary above.
JUDGED_VERDICTS = {
    "agrees": "match",
    "disagrees": "mismatch",
    "cannot_determine": "cannot_determine",
}

MAX_GRAPH_HOPS = 6
MAX_CYCLE_RECORDS = 25
MAX_TRAVERSED_EDGES = 100
MAX_ROLES = 20
MAX_ASSERTIONS = 50
MAX_ITEMS = 500
MAX_GRID_PAGE_SIZE = 200
MAX_GRID_RELATED_ITEMS = 25

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)
# Day/month order the accepted formats above cannot see. Never used to *accept* a
# value — only to detect that a purely numeric date has two readings, so it is
# reported invalid rather than silently resolved to one of them.
_AMBIGUOUS_DATE_FORMATS = ("%m-%d-%Y", "%m/%d/%Y")
# Whitespace is deliberately *not* in the digit class: including it let a raw
# value that spans two numbers ("25 25", common when OCR emits a label column
# and a value column separately) concatenate into 2525.
_NUMBER_RE = re.compile(r"[-+]?\d(?:[\d,]*\d)?(?:\.\d+)?")


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise CycleSchemaError(f"{label} must be an object.")
    return dict(value)


def _list(value: object, label: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list):
        raise CycleSchemaError(f"{label} must be an array.")
    if nonempty and not value:
        raise CycleSchemaError(f"{label} must not be empty.")
    return list(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CycleSchemaError(f"{label} must be a non-empty string.")
    return value.strip()


def _key(value: object, label: str) -> str:
    text = _text(value, label)
    if not _KEY_RE.fullmatch(text):
        raise CycleSchemaError(f"{label} contains unsupported characters.")
    return text


def _collect(errors: list[str], operation) -> object:
    """Run one independent validation, recording rather than raising its errors.

    Independent here means what it says: a sibling comparison's operator does
    not depend on this one's operand, so a caller that stops at the first
    failure reports one violation out of five and cannot be repaired in one
    turn. Callers use this where the units genuinely are independent, and plain
    raising where a later check depends on an earlier one's result.
    """

    try:
        return operation()
    except CycleSchemaError as error:
        errors.extend(error.errors)
        return None


def _date_candidate(raw: str) -> str:
    """Strip only presentation whitespace around date separators."""

    return re.sub(r"\s*-\s*", "-", raw)


def _parsed_dates(candidate: str, formats: Iterable[str]) -> set[str]:
    parsed: set[str] = set()
    for date_format in formats:
        try:
            parsed.add(datetime.strptime(candidate, date_format).date().isoformat())
        except ValueError:
            continue
    return parsed


def normalize_evidence_value(
    raw_value: object,
    *,
    semantic_type: str,
    citation: object,
) -> dict:
    """Normalize one extracted value locally while retaining failed evidence.

    Workers report the verbatim value and its citation. They do not get to
    choose the normalized form the graph compares on.
    """

    raw = str(raw_value or "").strip()
    if not raw:
        raise CycleSchemaError("An extracted raw value must not be empty.")
    normalized: object | None = None
    error: str | None = None
    try:
        if semantic_type == "identifier":
            from . import cycle_measurement

            normalized = cycle_measurement.normalize(raw)
        elif semantic_type == "date":
            # Human-authored vouchers commonly contain whitespace around date
            # separators (for example ``29-Apr -2024``).  Removing only that
            # presentation whitespace is deterministic and does not guess a
            # missing digit or swap day/month order.
            candidate = _date_candidate(raw)
            accepted = _parsed_dates(candidate, _DATE_FORMATS)
            if not accepted:
                error = "unrecognized date format"
            elif len(accepted | _parsed_dates(candidate, _AMBIGUOUS_DATE_FORMATS)) > 1:
                # ``04-01-2024`` is 4 January or 1 April depending on the
                # record's convention, which this value does not state. Choosing
                # by format order would decide a cut-off comparison silently.
                error = "ambiguous day and month order"
            else:
                normalized = next(iter(accepted))
        elif semantic_type == "number":
            negative = raw.startswith("(") and raw.endswith(")")
            match = _NUMBER_RE.search(raw)
            if match is None:
                error = "unrecognized numeric format"
            elif _parsed_dates(_date_candidate(raw), _DATE_FORMATS):
                # ``19 Apr 2024`` yields 19 from a bare numeric scan. Reporting
                # it invalid is what lets the map validator send a date supplied
                # for an amount back for repair instead of committing a wrong
                # number that normalized cleanly.
                error = "value is a date, not a number"
            else:
                value = Decimal(match.group(0).replace(",", ""))
                if negative:
                    value = -value
                normalized = int(value) if value == value.to_integral() else float(value)
        elif semantic_type == "boolean":
            token = raw.casefold()
            if token in {"true", "yes", "y", "present", "1"}:
                normalized = True
            elif token in {"false", "no", "n", "absent", "missing", "0"}:
                normalized = False
            else:
                error = "unrecognized boolean format"
        elif semantic_type == "text":
            normalized = raw
        else:
            raise CycleSchemaError(f"Unsupported evidence semantic type '{semantic_type}'.")
    except (InvalidOperation, ValueError) as exc:
        error = str(exc) or f"invalid {semantic_type} value"
        normalized = None
    return {
        "raw_value": raw,
        "value": normalized,
        "normalization_status": "normalized" if normalized is not None else "invalid",
        "normalization_error": None if normalized is not None else error,
        "citation": citation,
    }


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _dedupe_evidence(values: Iterable[object]) -> list[object]:
    output: list[object] = []
    seen: set[str] = set()
    for value in values:
        identity = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        if identity not in seen:
            seen.add(identity)
            output.append(value)
    return output


def apply_cross_item_reuse(items: Iterable[object], roles: Iterable[object]) -> list[dict]:
    """Annotate shared records according to each role's cross-item reuse rule."""

    role_rules = {}
    for role in (_object(value, "role") for value in roles):
        name = _key(role.get("role"), "role.role")
        rule = str(role.get("reuse_across_items") or "exclusive")
        if rule not in REUSE_RULES:
            raise CycleSchemaError(f"Role '{name}' has an unsupported reuse rule.")
        role_rules[name] = rule
    output = [json.loads(json.dumps(_object(value, "cycle item"), default=str)) for value in items]
    uses: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for item in output:
        for binding in item.get("role_bindings") or []:
            key = (str(binding.get("role") or ""), str(binding.get("record_id") or ""))
            uses.setdefault(key, []).append((item, binding))
    for (role, record_id), values in sorted(uses.items()):
        if len(values) < 2:
            continue
        rule = role_rules.get(role, "exclusive")
        related = sorted(str(item.get("id") or "") for item, _binding in values)
        for item, binding in values:
            fact = {
                "role": role,
                "record_id": record_id,
                "related_item_ids": [value for value in related if value != str(item.get("id") or "")],
                "reuse_across_items": rule,
                "identifier_edge": (binding.get("matched_by") or [None])[-1],
            }
            item.setdefault("shared_record_facts", []).append(fact)
            if rule == "exclusive":
                item.setdefault("collisions", []).append({**fact, "kind": "cross_item_collision"})
                item["linkage_state"] = "needs_review"
    return output


def _frame_signature(frame: pl.DataFrame) -> str:
    hashes = frame.hash_rows(seed=0).to_list() if frame.height else []
    return _canonical_hash(
        {
            "schema": [(name, str(dtype)) for name, dtype in frame.schema.items()],
            "height": frame.height,
            "row_hashes": hashes,
        }
    )


def validate_control_attributes(
    value: object,
    *,
    workspace: object | None = None,
) -> list[dict]:
    """Validate one control's attributes against this engagement's schemas.

    ``workspace`` is what makes an attribute exact: without one only its shape
    is checked, which is right for a response validator that has no engagement
    in hand and wrong for the commit that persists the row. Both call this;
    only the second supplies a workspace.
    """

    from . import cycle_linking

    attributes = _list(value, "control_attributes", nonempty=True)
    keys: set[str] = set()
    normalized: list[dict] = []
    errors: list[str] = []
    for index, raw in enumerate(attributes):
        # Attributes are independent requirements of one control: a malformed
        # third attribute says nothing about the first two, so all of them are
        # reported together.
        local: list[str] = []
        attribute = cycle_linking.validate_control_attribute(
            workspace,
            _object(raw, f"control_attributes[{index}]"),
            label=f"control_attributes[{index}]",
            keys=keys,
            errors=local,
        )
        errors.extend(local)
        if not local:
            normalized.append(attribute)
    if errors:
        raise CycleSchemaError(errors)
    return normalized


def unanswerable_cycle_requirements(workspace, rcm_row: Mapping[str, object]) -> list[str]:
    """Degradation notes for one row's unanswerable transaction-cycle attributes.

    The generation turn stays silent about these because it cannot act on them.
    The stage must not, or the run reports success over a requirement nothing
    tested.
    """

    from . import cycle_linking

    return cycle_linking.unanswerable_cycle_requirements(workspace, rcm_row)


def normalize_item(test: Mapping[str, object], item: object) -> dict:
    """Normalize one stored cycle item under the rules its test names."""

    from . import cycle_linking

    return cycle_linking.normalize_cycle_item(test, item)


def validate_cycle_test(value: object, *, workspace: object | None = None) -> dict:
    """Normalize a cycle test into the shape the engine executes."""

    from . import cycle_linking

    return cycle_linking.validate_cycle_test(workspace, _object(value, "cycle test"))


def cycle_definition_sha1(test: Mapping[str, object]) -> str:
    from . import cycle_linking

    return cycle_linking.cycle_definition_sha1(test)


def stable_test_semantic_id(test: Mapping[str, object]) -> str:
    from . import cycle_linking

    return cycle_linking.stable_test_semantic_id(test)


def stable_cycle_test_id(test: Mapping[str, object]) -> str:
    from . import cycle_linking

    return cycle_linking.stable_cycle_test_id(test)


def stable_cycle_item_id(test: Mapping[str, object], anchor_value: object) -> str:
    from . import cycle_linking

    return cycle_linking.stable_cycle_item_id(test, anchor_value)


def selection_confirmation(reach: Mapping[str, object]) -> dict | None:
    from . import cycle_linking

    return cycle_linking.selection_confirmation(reach)


def build_cycle_vouch_test(workspace, payload: Mapping[str, object]) -> dict:
    """Validate and persist one cycle test against the approved rules.

    Raises :class:`SelectionConfirmationRequired` when an evidence-linked reach
    exceeds the item cap: no test is persisted and no rows are truncated until
    the caller confirms a deterministic sample.
    """

    from . import cycle_linking

    return cycle_linking.build_cycle_vouch_test(workspace, payload)


def materialize_cycle_items(workspace, test: Mapping[str, object]) -> list[dict]:
    """Select population rows and bind each one's linked record closure."""

    from . import cycle_linking

    return cycle_linking.materialize_cycle_items(workspace, test)


def evaluate_cycle_item(
    workspace,
    test: Mapping[str, object],
    item: dict,
    *,
    judgments: Mapping[str, Mapping[str, object]] | None = None,
) -> dict:
    from . import cycle_linking

    return cycle_linking.evaluate_cycle_item(
        workspace, test, item, judgments=judgments
    )


def judgment_request(workspace, test: Mapping[str, object], item_id: str) -> dict:
    """One item's pending checks, with the values each one reads."""

    from . import cycle_linking

    return cycle_linking.judgment_request(workspace, test, item_id)


def evaluate_cycle_test(
    workspace,
    test: Mapping[str, object],
    *,
    judgments: Mapping[str, Mapping[str, Mapping[str, object]]] | None = None,
) -> dict:
    """Materialize current inputs and evaluate only work that is not current.

    ``judgments`` is keyed by item id and then by assertion id. Without it the
    evidence still binds and anything awaiting a verdict stays ``not_run``,
    which is what makes this safe to call from a read.
    """

    from . import cycle_linking

    return cycle_linking.evaluate_cycle_test(workspace, test, judgments=judgments)


def mutate_cycle_assertions(workspace, test, assertions, **_kwargs):
    """Refused: the assertions are part of what the auditor approved.

    Editing them on the test would produce rules nobody approved, under a
    ``ruleset_hash`` that says otherwise.
    """

    raise CycleSchemaError(
        "This cycle runs on an approved ruleset. Edit its assertions in the "
        "cycle rules review and approve them, rather than on the test."
    )


def assurance_scope_for(selection: Mapping[str, object]) -> str:
    mode = str(selection.get("mode") or "")
    if mode == "evidence_linked":
        return "targeted_evidence_only"
    if mode == "sample":
        return "sampled_population"
    raise CycleSchemaError(f"Unsupported selection mode '{mode}'.")


def ruleset_backed(test: Mapping[str, object]) -> bool:
    """Whether this cycle test names the rules it runs under.

    A test written before the rules moved into the workspace names none, and
    cannot be executed: the vocabulary it was built against no longer exists.
    Readers still open it; the engine refuses it by name rather than by
    reinterpreting it under rules it never saw.
    """

    return bool(str(((test.get("definition") or {}).get("ruleset_id")) or ""))


def _sha1_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha1:{hashlib.sha1(encoded.encode('utf-8')).hexdigest()}"


def _plain_json(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


def _sample_row_indices(frame: pl.DataFrame, selection: Mapping[str, object]) -> list[int]:
    """Return a deterministic sample using Polars-backed row ranking."""

    size = min(int(selection.get("size") or 0), frame.height)
    if size <= 0:
        return []
    method = str(selection.get("method") or "")
    seed = int(selection.get("seed") or 0)
    indexed = frame.with_row_index("__cycle_source_row")
    if method == "interval":
        # Evenly spaced, stable positions; no hidden random starting point.
        return sorted({min((index * frame.height) // size, frame.height - 1) for index in range(size)})
    rank = pl.struct(frame.columns).hash(seed=seed).alias("__cycle_rank")
    ranked = indexed.with_columns(rank)
    if method == "random":
        return sorted(
            int(value)
            for value in ranked.sort(["__cycle_rank", "__cycle_source_row"])
            .head(size)["__cycle_source_row"]
            .to_list()
        )
    if method != "stratified":
        raise CycleSchemaError(f"Unsupported sampling method '{method}'.")
    stratum = str(selection.get("stratify_by") or "")
    ordered = ranked.sort([stratum, "__cycle_rank", "__cycle_source_row"])
    queues: dict[str, list[int]] = {}
    for row in ordered.select([stratum, "__cycle_source_row"]).iter_rows(named=True):
        queues.setdefault(json.dumps(row[stratum], default=str), []).append(
            int(row["__cycle_source_row"])
        )
    chosen: list[int] = []
    keys = sorted(queues)
    while len(chosen) < size and any(queues.values()):
        for key in keys:
            if queues[key] and len(chosen) < size:
                chosen.append(queues[key].pop(0))
    return sorted(chosen)


def _aggregate_evaluation(item: Mapping[str, object]) -> str:
    results = list((item.get("result_by_assertion") or {}).values())
    if not results or any(result.get("verdict") == "not_run" for result in results):
        return "stale" if any(result.get("stale") for result in results) else "not_run"
    verdicts = {str(result.get("verdict") or "not_run") for result in results}
    if "mismatch" in verdicts:
        return "failed"
    if (
        "ambiguous" in verdicts
        or item.get("role_conflicts")
        or item.get("collisions")
        or item.get("linkage_state") == "needs_review"
    ):
        return "needs_review"
    if verdicts & {"missing_evidence", "invalid_extraction", "cannot_determine"}:
        return "incomplete"
    return "passed"


# Every evidence record is re-validated against every assertion on each call,
# so this is the expensive step in a Document Test read. Several independent
# read-only projections (capability readiness, worklist summaries, report
# rendering) each resolve their own test scope and call this for the same
# test within a single request. ``request_cache_scope`` lets a caller certain
# no write happens in its span memoize by (workspace instance, test id, and
# the exact prior items the test carries in), so a materialization that
# would reproduce an identical result is skipped rather than redone.
# Reentrant: nesting is safe and only the outermost scope pays for teardown.
_cache: ContextVar[dict | None] = ContextVar("cycle_vouching_request_cache", default=None)


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


def project_cycle_staleness(workspace, test: dict) -> dict:
    """Project current inputs without persisting or evaluating them."""

    if test.get("kind") != "cycle_vouch" or not test.get("items"):
        return test
    test["items"] = materialize_cycle_items(workspace, test)
    return test


def _evidence_catalog(workspace, document_ids: Iterable[str]) -> dict[tuple[str, str], dict]:
    """Resolve analysis citation IDs to stable typed document anchors."""

    from . import document_analysis
    from .evidence import normalize_anchor

    documents_by_id = {
        str(document.get("id") or ""): document for document in workspace.documents
    }
    catalog: dict[tuple[str, str], dict] = {}
    for document_id in sorted(set(document_ids)):
        document = documents_by_id.get(document_id)
        if document is None:
            continue
        artifact = (
            document_analysis.load_analysis(
                workspace, document_id, document=document
            ).get("effective")
            or {}
        )
        for citation in artifact.get("citations") or []:
            citation_id = str(citation.get("id") or "")
            if not citation_id:
                continue
            anchor_id = "EV-CYCLE-" + hashlib.sha1(
                f"{document_id}:{citation_id}".encode("utf-8")
            ).hexdigest()[:12].upper()
            catalog[(document_id, citation_id)] = normalize_anchor(
                {
                    "id": anchor_id,
                    "source_kind": "document",
                    "source_id": document_id,
                    "source_sha1": citation.get("source_sha1")
                    or document.get("sha1"),
                    "page": citation.get("page"),
                    "excerpt": str(citation.get("excerpt") or "")[:400],
                    "excerpt_hash": citation.get("excerpt_hash"),
                    "generated_by": "cycle-vouching",
                },
                require_hash=True,
            )
    return catalog


def _bounded_value(value: object) -> object:
    plain = _plain_json(value)
    if isinstance(plain, str) and len(plain) > 200:
        return plain[:197] + "..."
    return plain


# Item-first tests now carry the same two fields as cycle items, so both sides
# of these predicates read the same shape; only the state vocabularies differ.
_ITEM_PENDING_EVALUATIONS = {"not_run"}
_ITEM_CURRENT_EVALUATIONS = {"agent_checked", "passed", "failed", "inconclusive"}


def execution_pending(item: Mapping[str, object], *, cycle: bool) -> bool:
    if cycle:
        return str((item.get("evaluation") or {}).get("state") or "not_run") in {
            "not_run",
            "stale",
        }
    return (
        str((item.get("evaluation") or {}).get("state") or "not_run")
        in _ITEM_PENDING_EVALUATIONS
    )


def execution_current(item: Mapping[str, object], *, cycle: bool) -> bool:
    if cycle:
        return (
            str((item.get("evaluation") or {}).get("state") or "not_run")
            in CURRENT_EVALUATION_STATES
        )
    return (
        str((item.get("evaluation") or {}).get("state") or "not_run")
        in _ITEM_CURRENT_EVALUATIONS
    )


def disposition_current(item: Mapping[str, object], *, cycle: bool) -> bool:
    """Whether a live auditor decision stands against the current inputs.

    An auditor's ``needs_review`` is a decision to defer, not to settle, so it
    reads as not-current here for both shapes — the same way an unsigned item
    does. That is what keeps "everything is dispositioned" from being true of a
    test whose items are all still parked.
    """

    disposition = item.get("disposition") or {}
    return str(disposition.get("state") or "pending") in {
        "confirmed",
        "exception",
    } and not bool(disposition.get("stale"))


def disposition_pending(item: Mapping[str, object], *, cycle: bool) -> bool:
    return execution_current(item, cycle=cycle) and not disposition_current(
        item, cycle=cycle
    )


def _assurance_label(scope: str) -> str:
    return (
        "Targeted evidence - not a sample"
        if scope == "targeted_evidence_only"
        else "Sampled population"
    )


# A result an auditor cannot use as it stands: the fact was not found, could not
# be normalized, the selector matched several differing facts, or the reader had
# both values and still could not settle the requirement. Evidence to weigh,
# not a verdict.
_UNUSABLE_VERDICTS = frozenset(
    {"missing_evidence", "invalid_extraction", "ambiguous", "cannot_determine"}
)
# Why an item is open, for the states where the runner has not produced a
# current reading. Anything current reads as "runner: <state>", the same
# phrasing the item-first side uses.
_UNRESOLVED_READING = {
    "not_run": "not run",
    "stale": "evaluated against inputs that have since changed",
}


def conclusion_block(test: Mapping[str, object]) -> str:
    """Why a Cycle test structurally cannot carry a conclusion, or an empty string.

    The same single rule every other test kind keeps: a test that has not run
    cannot conclude. An ambiguous or incomplete deterministic result is not a
    structural bar — it is evidence the auditor weighs and, having concluded
    anyway, discloses. Gating on it here left an item the auditor had already
    reviewed and confirmed with nowhere to go, because no auditor action
    rewrites the runner's reading: the evaluation is derived on every read.
    """

    items = list(test.get("items") or [])
    if not items or not all(execution_current(item, cycle=True) for item in items):
        return "Run every item before recording a control conclusion."
    return ""


def unresolved_items(test: Mapping[str, object]) -> list[dict]:
    """Cycle items carrying no settled auditor reading, with why each is open."""

    open_items = []
    for item in test.get("items") or []:
        if disposition_current(item, cycle=True):
            continue
        evaluation = str((item.get("evaluation") or {}).get("state") or "not_run")
        open_items.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or item.get("id") or "Cycle item"),
                "reason": (
                    "signed off against evidence that has since changed"
                    if (item.get("disposition") or {}).get("stale")
                    else _UNRESOLVED_READING.get(evaluation, f"runner: {evaluation}")
                ),
            }
        )
    return open_items


def unusable_result_items(test: Mapping[str, object]) -> int:
    """Items carrying at least one assertion the runner could not settle."""

    return sum(
        any(
            result.get("verdict") in _UNUSABLE_VERDICTS
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in test.get("items") or []
    )


def result_rollup(test: Mapping[str, object]) -> dict:
    """Count cycle items and assertion cells as separate, non-additive units."""

    # The rules live in the ruleset, which this function has no workspace to
    # load. It needs exactly two things from them, and both are already on the
    # test: the selection, and which assertions have a cell per item — and an
    # item's own result keys are that, by construction.
    selection = ((test.get("definition") or {}).get("population") or {}).get(
        "selection"
    ) or {}
    assertion_keys = sorted({
        key
        for item in test.get("items") or []
        for key in (item.get("result_by_assertion") or {})
    })
    items = list(test.get("items") or [])
    item_counts = {state: 0 for state in sorted(EVALUATION_STATES)}
    disposition_counts = {state: 0 for state in sorted(DISPOSITION_STATES)}
    assertion_counts = {verdict: 0 for verdict in sorted(ASSERTION_VERDICTS)}
    for item in items:
        evaluation_state = str(
            (item.get("evaluation") or {}).get("state") or "not_run"
        )
        item_counts[evaluation_state] += 1
        disposition_state = str(
            (item.get("disposition") or {}).get("state") or "pending"
        )
        if disposition_current(item, cycle=True):
            disposition_counts[disposition_state] += 1
        else:
            disposition_counts["pending"] += 1
        results = item.get("result_by_assertion") or {}
        for key in assertion_keys:
            verdict = str((results.get(key) or {}).get("verdict") or "not_run")
            assertion_counts[verdict] += 1

    scope = assurance_scope_for(selection)
    selection_basis = str(selection.get("mode") or "")
    coverage = {
        **dict(test.get("coverage") or {}),
        "selection_basis": selection_basis,
        "assurance_scope": scope,
    }
    tested_items = sum(
        item_counts[state] for state in sorted(CURRENT_EVALUATION_STATES)
    )
    current_items = [item for item in items if execution_current(item, cycle=True)]
    failed_items = sum(
        any(
            result.get("verdict") == "mismatch"
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in current_items
    )
    incomplete_items = sum(
        any(
            result.get("verdict")
            in {"missing_evidence", "invalid_extraction", "cannot_determine"}
            for result in (item.get("result_by_assertion") or {}).values()
        )
        for item in current_items
    )
    pending_dispositions = sum(
        disposition_pending(item, cycle=True) for item in items
    )
    evaluations_current = bool(items) and tested_items == len(items)
    dispositions_current = bool(items) and (
        disposition_counts["confirmed"] + disposition_counts["exception"]
        == len(items)
    )
    conclusion_eligible = bool(
        evaluations_current
        and dispositions_current
        and not item_counts["incomplete"]
        and not item_counts["needs_review"]
    )
    control_conclusion = (
        str(test.get("control_conclusion") or "no_conclusion")
        if not conclusion_block(test)
        else "no_conclusion"
    )
    return {
        "items": len(items),
        "tested_items": tested_items,
        "item_counts": item_counts,
        "disposition_counts": disposition_counts,
        "assertion_columns": len(assertion_keys),
        "assertion_counts": {
            "total": len(items) * len(assertion_keys),
            **assertion_counts,
        },
        "failed_items": failed_items,
        "incomplete_items": incomplete_items,
        "needs_review_items": item_counts["needs_review"],
        "confirmed_items": disposition_counts["confirmed"],
        "exception_items": disposition_counts["exception"],
        "open_exceptions": disposition_counts["exception"],
        "pending_dispositions": pending_dispositions,
        "coverage": coverage,
        "assurance_scope": scope,
        "assurance_label": _assurance_label(scope),
        "conclusion_eligible": conclusion_eligible,
        # `conclusion_eligible` still means "clean": every item evaluated,
        # dispositioned, and settled by the runner. Reporting the conclusion is
        # the weaker test — an auditor may conclude over items the runner could
        # not settle, and that conclusion has to reach the RCM rollup, the
        # working paper, and the report, or overriding would silently achieve
        # nothing. What travels with it is the disclosure.
        "conclusion_disclosed": bool(test.get("conclusion_override")),
        "unresolved_items": unresolved_items(test),
        "control_conclusion": control_conclusion,
        "assertion_mismatches": assertion_counts["mismatch"],
        # Common Document Test rollup fields remain canonical for consumers
        # that aggregate all test kinds. They are not added to the item counts.
        "matched": assertion_counts["match"],
        "mismatched": sum(
            assertion_counts[verdict]
            for verdict in (
                "mismatch",
                "missing_evidence",
                "invalid_extraction",
                "ambiguous",
                "cannot_determine",
            )
        ),
        "confirmed": disposition_counts["confirmed"],
        "exceptions": disposition_counts["exception"],
        "manual_review": sum(
            1
            for item in items
            if execution_current(item, cycle=True)
            and not disposition_current(item, cycle=True)
        ),
        "pending": sum(
            1
            for item in items
            if execution_pending(item, cycle=True)
            or disposition_pending(item, cycle=True)
        ),
    }


def _grid_comparison(value: object) -> dict:
    """Project one comparison without extraction envelopes or evidence text."""

    comparison = _object(value, "assertion comparison")
    entries = [
        _object(entry, "assertion comparison entry")
        for entry in comparison.get("entries") or []
    ]
    evidence_count = sum(
        len(entry.get("evidence_refs") or []) for entry in entries
    )
    display_values = []
    for entry in comparison.get("entry_results") or []:
        entry_object = _object(entry, "assertion comparison result")
        if "value" in entry_object:
            display_values.append(_bounded_value(entry_object.get("value")))
    if not display_values and comparison.get("state") == "resolved":
        display_values.append(_bounded_value(comparison.get("value")))
    return {
        key: comparison.get(key)
        for key in ("side", "role", "document_id", "state", "verdict")
        if comparison.get(key) is not None
    } | {
        "record_ids": [
            str(record_id) for record_id in comparison.get("record_ids") or []
        ],
        "display_values": display_values,
        "entry_count": len(entries),
        "evidence_count": evidence_count,
    }


def _grid_cell(
    result: Mapping[str, object], *, attribution_stale: bool = False
) -> dict:
    comparisons = [
        _grid_comparison(value) for value in result.get("comparisons") or []
    ]
    return {
        "verdict": str(result.get("verdict") or "not_run"),
        "display": str(result.get("display") or "")[:240],
        "comparison_count": len(comparisons),
        "evidence_count": len(result.get("evidence_refs") or []),
        "comparisons": comparisons,
        # The runner's own "this needs re-running" flag, carried through.
        "stale": bool(result.get("stale")),
        # Set when the stored verdict cannot be read under the current column.
        "attribution_stale": bool(attribution_stale),
    }


def _grid_attribution(
    test: Mapping[str, object],
    *,
    definition_sha1: str,
    assertions: Mapping[str, Mapping[str, object]],
    assertion_hashes: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """Locate evaluated cells that cannot be attributed to current columns.

    This is what materialization discovers on the next write, where such a
    result is reset to ``not_run`` and flagged stale.  The grid is a read, so
    it reports the gap rather than refusing to render one: an auditor cannot
    repair a definition drift they are not allowed to look at.  Callers must
    not read an unattributable verdict as current -- :func:`grid_projection`
    keeps those cells out of the column tallies for exactly that reason.

    ``assertion_hashes`` names what each column was evaluated under, for callers
    whose display shape is not the shape the evaluator hashed. Attribution is a
    claim about the rule that produced a verdict, so it must compare against
    that rule and not against a projection of it.
    """

    attribution: dict[str, dict] = {}
    for item in test.get("items") or []:
        results = {
            str(key): value
            for key, value in (item.get("result_by_assertion") or {}).items()
        }
        evaluated = {
            key
            for key, result in results.items()
            if str(result.get("verdict") or "not_run") != "not_run"
        }
        item_definition_sha1 = str(
            (item.get("evaluation") or {}).get("definition_sha1") or ""
        )
        # A different test definition puts every evaluated cell on the item out
        # of reach, not only the ones whose own assertion happens to have moved.
        definition_stale = bool(
            evaluated and item_definition_sha1 != definition_sha1
        )
        if definition_stale:
            stale_keys = set(evaluated)
        else:
            expected = (
                dict(assertion_hashes)
                if assertion_hashes is not None
                else {key: _sha1_hash(value) for key, value in assertions.items()}
            )
            stale_keys = {
                key
                for key in evaluated
                if expected.get(key) is None
                or results[key].get("assertion_sha1") != expected[key]
            }
        attribution[str(item.get("id") or "")] = {
            "definition_stale": definition_stale,
            "stale_keys": stale_keys,
        }
    return attribution


def grid_projection(
    test: Mapping[str, object],
    *,
    offset: int = 0,
    limit: int = 100,
    workspace: object | None = None,
) -> dict:
    """Return a bounded, read-only grid over canonical cycle item results."""

    if str(test.get("kind") or "") != "cycle_vouch":
        raise CycleSchemaError("The grid is available only for cycle_vouch tests.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CycleSchemaError("Grid offset must be a non-negative integer.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_GRID_PAGE_SIZE
    ):
        raise CycleSchemaError(
            f"Grid limit must be between 1 and {MAX_GRID_PAGE_SIZE}."
        )
    from . import cycle_linking

    validated = cycle_linking.validate_cycle_test(workspace, test)
    ruleset = validated.get("ruleset") or {}
    assertion_values = cycle_linking.grid_assertions(ruleset)
    # Hashed from the rule the evaluator ran, not from the column shape shown
    # here: attribution is a claim about what produced a verdict.
    assertion_hashes = {
        str(assertion.get("id") or ""): _sha1_hash(assertion)
        for assertion in ruleset.get("assertions") or []
    }
    if len(assertion_values) > MAX_ASSERTIONS:
        # validate_cycle_test enforces this too; retaining the projection guard
        # makes the no-silent-column-truncation property explicit here.
        raise CycleSchemaError(
            f"A cycle grid may project at most {MAX_ASSERTIONS} assertions."
        )
    assertions = {
        str(assertion["key"]): assertion for assertion in assertion_values
    }
    definition_sha1 = cycle_definition_sha1(validated)
    attribution = _grid_attribution(
        test,
        definition_sha1=definition_sha1,
        assertions=assertions,
        assertion_hashes=assertion_hashes,
    )
    items = sorted(
        [dict(item) for item in test.get("items") or []],
        key=lambda item: (str(item.get("label") or ""), str(item.get("id") or "")),
    )
    rollup = result_rollup({**dict(validated), **dict(test), "items": items})
    columns = []
    for assertion in assertion_values:
        key = str(assertion["key"])
        applicable_roles: list[str] = []
        for operand in (assertion.get("left"), assertion.get("right")):
            if not isinstance(operand, Mapping):
                continue
            if operand.get("source") == "role":
                applicable_roles.append(str(operand.get("role") or ""))
            elif operand.get("source") == "roles":
                applicable_roles.extend(str(role) for role in operand.get("roles") or [])
        counts = {verdict: 0 for verdict in sorted(ASSERTION_VERDICTS)}
        stale_cells = 0
        for item in items:
            result = (item.get("result_by_assertion") or {}).get(key) or {}
            entry = attribution.get(str(item.get("id") or "")) or {}
            if key in (entry.get("stale_keys") or frozenset()):
                # An unattributable verdict is not evidence under this column,
                # so it is held out rather than counted as the match it was.
                # `sum(counts.values()) + stale_cells` is the item total.
                stale_cells += 1
                continue
            counts[str(result.get("verdict") or "not_run")] += 1
        columns.append(
            {
                "key": key,
                "label": str(assertion.get("label") or key),
                "requirement": str(
                    assertion.get("requirement") or assertion.get("rationale") or ""
                ),
                "applicable_roles": list(dict.fromkeys(applicable_roles)),
                "counts": counts,
                "stale_cells": stale_cells,
            }
        )
    page_items = items[offset : offset + limit]
    rows = []
    for item in page_items:
        results = item.get("result_by_assertion") or {}
        item_attribution = attribution.get(str(item.get("id") or "")) or {}
        item_stale_keys = item_attribution.get("stale_keys") or frozenset()
        rows.append(
            {
                "item_id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "evaluation_state": str(
                    (item.get("evaluation") or {}).get("state") or "not_run"
                ),
                "disposition_state": str(
                    (item.get("disposition") or {}).get("state") or "pending"
                ),
                "disposition_stale": bool(
                    (item.get("disposition") or {}).get("stale")
                ),
                "roles_present": sorted({
                    str(binding.get("role") or "")
                    for binding in item.get("role_bindings") or []
                }),
                "missing_roles": list(item.get("missing_roles") or []),
                "shared_record_facts": [
                    {
                        "role": str(fact.get("role") or ""),
                        "record_id": str(fact.get("record_id") or ""),
                        "related_item_ids": list(
                            fact.get("related_item_ids") or []
                        )[:MAX_GRID_RELATED_ITEMS],
                        "related_item_count": len(
                            fact.get("related_item_ids") or []
                        ),
                        "related_items_truncated": len(
                            fact.get("related_item_ids") or []
                        ) > MAX_GRID_RELATED_ITEMS,
                        "reuse_across_items": str(
                            fact.get("reuse_across_items") or ""
                        ),
                        "identifier_edge": dict(fact.get("identifier_edge") or {}),
                    }
                    for fact in item.get("shared_record_facts") or []
                ],
                "definition_stale": bool(
                    item_attribution.get("definition_stale")
                ),
                "cells": {
                    key: _grid_cell(
                        results.get(key) or {"verdict": "not_run"},
                        attribution_stale=key in item_stale_keys,
                    )
                    for key in assertions
                },
            }
        )
    selection = validated["definition"]["population"]["selection"]
    scope = assurance_scope_for(selection)
    total = len(items)
    return {
        "test_id": str(test.get("id") or ""),
        "test_sha1": str(test.get("sha1") or ""),
        "definition_sha1": definition_sha1,
        "title": str(test.get("title") or ""),
        "population": dict(validated["definition"]["population"]),
        "coverage": dict(rollup["coverage"]),
        "selection_basis": str(selection.get("mode") or ""),
        "assurance_scope": scope,
        "assurance_label": _assurance_label(scope),
        "tested_item_counts": dict(rollup["item_counts"]),
        "assertion_counts": dict(rollup["assertion_counts"]),
        "columns": columns,
        "rows": rows,
        # Advisory, not blocking.  Refusing the whole grid over this withheld
        # the only view an auditor could have used to repair it.
        "stale_definition": any(
            entry.get("definition_stale") for entry in attribution.values()
        ),
        "stale_cell_count": sum(
            len(entry.get("stale_keys") or ()) for entry in attribution.values()
        ),
        "page": {"offset": offset, "limit": limit, "total": total},
        "truncated": offset + len(rows) < total,
    }




def metadata() -> dict:
    """The closed vocabularies and bounds the cycle UI binds its pickers to.

    Nothing per-workspace: the document types, the fields and the rules are
    read from the engagement's own schemas and ruleset. What is stated here is
    what the engine itself fixes.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "cardinalities": sorted(CARDINALITIES),
        "reuse_rules": sorted(REUSE_RULES),
        "selection_modes": sorted(SELECTION_MODES),
        "sampling_methods": sorted(SAMPLING_METHODS),
        "assurance_scopes": sorted(ASSURANCE_SCOPES),
        "assertions": sorted(ASSERTIONS),
        # What a reader may answer for a pair it was asked to judge. There is no
        # comparison operator to publish: agreement is settled against the
        # values, not chosen from a vocabulary when the rule is written.
        "verdicts": sorted(JUDGED_VERDICTS),
        "limits": {
            "max_graph_hops": MAX_GRAPH_HOPS,
            "max_cycle_records": MAX_CYCLE_RECORDS,
            "max_traversed_edges": MAX_TRAVERSED_EDGES,
            "max_roles": MAX_ROLES,
            "max_assertions": MAX_ASSERTIONS,
            "max_items": MAX_ITEMS,
        },
    }
