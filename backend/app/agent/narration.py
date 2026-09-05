"""Plain-language narration: what the agent says while it works.

The scheduler speaks in capability ids, unit statuses and readiness codes.
None of that belongs in a transcript an auditor reads. This module is the one
place that turns durable run state into sentences — the progress notes emitted
as stages settle, the closing turn written when a run ends, and the humanized
blockers derived from units that stopped because they need a person.

Narration never decides anything: it is a projection of state that already
exists, so a run behaves identically whether or not anyone reads it. That also
means every function here has to tolerate partial records, because it runs
against live runs, interrupted runs, and records written by older builds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from . import store

if TYPE_CHECKING:
    from .context import ContextManifest

# Narration is an unbounded append log on a record that is rewritten in full on
# every save, so it is capped. The tail is what a reader wants anyway.
NARRATION_LIMIT = 200

_SUBJECT_SPLIT = re.compile(r"\s+[—–-]\s+")


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def note(
    run: dict,
    emit,
    text: str,
    *,
    kind: str = "progress",
    stage_id: str | None = None,
    unit_id: str | None = None,
    status: str | None = None,
) -> dict:
    """Append one progress line to the run's narration log and publish it.

    Callers hold the run's write lock through ``emit``'s runtime; the entry is
    appended in place so the next ``save()`` persists it.

    ``status`` carries what the line is reporting on, so a reader does not have
    to infer it from the prose. Without it every settled stage drew the same
    tick, and a blocked stage read as a completed one at a glance however
    carefully its sentence was worded.
    """
    entry = {
        "at": store.utcnow(),
        "kind": kind,
        "text": str(text or "").strip(),
        "stage_id": stage_id,
        "unit_id": unit_id,
        "status": status,
    }
    log = run.setdefault("narration", [])
    log.append(entry)
    if len(log) > NARRATION_LIMIT:
        del log[: len(log) - NARRATION_LIMIT]
    emit("narration", {"entry": entry})
    return entry


def say(run: dict, emit, text: str) -> dict | None:
    """Append an agent turn to the run's conversational log.

    ``assistant_chats`` projects these into the chat transcript as assistant
    bubbles, so this is how a run speaks to the auditor rather than only
    updating a status card.
    """
    content = str(text or "").strip()
    if not content:
        return None
    message = {"role": "agent", "content": content, "at": store.utcnow()}
    run.setdefault("messages", []).append(message)
    emit("message", {"message": message})
    return message


def clip(value: object, limit: int) -> str:
    """Trim to a length without cutting a word in half.

    Milestone text is model-authored and routinely lands a few characters over
    a cap; a hard slice left a briefing ending mid-word, which reads as a bug
    rather than as a summary. Falls back to a hard cut only when the text has
    no word boundary to fall back to.
    """
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    spaced = head.rsplit(" ", 1)[0] if " " in head else head
    return f"{spaced.rstrip(' ,;:—-')}…"


def milestone(
    run: dict,
    emit,
    *,
    capability: str,
    stage_id: str,
    status: str,
    headline: str,
    summary: str,
    metrics: list[dict] | None = None,
    highlights: list[dict] | None = None,
    stats: list[dict] | None = None,
    artifact_refs: list[str] | None = None,
) -> dict | None:
    """Persist one deterministic, idempotent workflow milestone.

    A milestone is a structured transcript item rather than a model-authored
    message. Its hash deliberately excludes time, so replaying a settled stage
    after a restart cannot duplicate the same result. A materially different
    projection for the same stage is retained as a later update.
    """
    headline = str(headline or "").strip()
    summary = str(summary or "").strip()
    if not headline or not summary:
        return None
    body = {
        "capability": str(capability or "").strip(),
        "stage_id": str(stage_id or "").strip(),
        "status": str(status or "").strip(),
        "headline": clip(headline, 160),
        "summary": clip(summary, 1200),
        "metrics": [
            {
                "label": str(item.get("label") or "")[:80],
                "value": item.get("value"),
            }
            for item in (metrics or [])[:8]
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ],
        "highlights": [
            {
                "severity": str(item.get("severity") or "info"),
                "label": clip(item.get("label"), 160),
                "detail": clip(item.get("detail"), 320),
                "artifact_ref": str(item.get("artifact_ref") or "") or None,
            }
            for item in (highlights or [])[:3]
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        ],
        "artifact_refs": list(dict.fromkeys(
            str(item) for item in (artifact_refs or []) if str(item).strip()
        ))[:20],
    }
    # A short severity-graded tally, for a stage whose result is a distribution
    # rather than a list — a matrix is read as "one critical, eight high" before
    # any single row is. Absent from the body unless a stage fills it, so the
    # hash of every milestone that does not use it is unchanged and a settled
    # stage still replays to the same id.
    graded = [
        {
            "label": str(item.get("label") or "")[:24],
            "value": item.get("value"),
            "severity": str(item.get("severity") or "info"),
        }
        for item in (stats or [])[:6]
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    if graded:
        body["stats"] = graded
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()
    milestone_id = f"{body['stage_id']}:{body['status']}:{digest[:12]}"
    existing = run.setdefault("milestones", [])
    if any(str(item.get("id") or "") == milestone_id for item in existing):
        return None
    entry = {
        "id": milestone_id,
        **body,
        "summary_sha1": digest,
        "created_at": store.utcnow(),
    }
    existing.append(entry)
    emit("milestone", {"milestone": entry})
    return entry


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
def humanize(value: object) -> str:
    """Turn a capability id, status or error code into readable words."""
    text = str(value or "").strip()
    if not text:
        return ""
    # Capability ids are ``domain.outcome``; the domain is redundant next to
    # the outcome in a sentence ("documents.analysis_generated" reads fine as
    # "analysis generated").
    text = text.rsplit(".", 1)[-1]
    return text.replace("_", " ").strip()


def subject_of(unit: dict) -> str:
    """The thing a unit is about, from its ``Verb — subject`` title."""
    parts = _SUBJECT_SPLIT.split(str(unit.get("title") or ""), maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _sentence(value: str) -> str:
    value = str(value or "").strip()
    return value[:1].upper() + value[1:] if value else ""


def _joined(values: list[str], conjunction: str = "then") -> str:
    values = [item for item in values if item]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} {conjunction} {values[1]}"
    return f"{', '.join(values[:-1])}, {conjunction} {values[-1]}"


def _count(number: int, singular: str, plural: str | None = None) -> str:
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


def plan_sentence(
    running_titles: list[str],
    reused_titles: list[str],
    *,
    added_prerequisites: bool = False,
) -> str:
    """The opening 'here is what I'll do' line shown on the run card.

    Replaces the capability-id listing that used to reach the UI verbatim.
    """
    parts: list[str] = []
    if running_titles:
        work = _joined([title.strip().lower() for title in running_titles])
        prefix = "I'll work through " if added_prerequisites else "I'll do "
        parts.append(f"{prefix}{work}.")
    else:
        parts.append("Everything this needs is already in place.")
    if reused_titles:
        reuse = _joined([title.strip().lower() for title in reused_titles], "and")
        single = len(reused_titles) == 1
        parts.append(
            f"{_sentence(reuse)} {'is' if single else 'are'} already done, so I'll "
            f"reuse {'it' if single else 'them'} rather than repeat the work."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Model turns: what is being read, and what a repair is for
# --------------------------------------------------------------------------- #
# Source ids are declared per context preset (see agent/context/presets.py) and
# are stable across workspaces, unlike the refs they select. (singular, plural)
# so a lone item and a group both read naturally.
_SOURCE_LABELS: dict[str, tuple[str, str]] = {
    "instruction": ("your instruction", "your instruction"),
    "planning_context": ("the planning context", "the planning context"),
    "current_planning_context": ("the current planning context", "the current planning context"),
    "planning_documents": ("a supporting document", "supporting documents"),
    "apm_template": ("the APM template", "the APM template"),
    "rcm_template": ("the RCM template", "the RCM template"),
    "current_apm": ("the current APM", "the current APM"),
    "current_rcm": ("the current RCM", "the current RCM"),
    "rcm_row": ("the target RCM row", "the target RCM rows"),
    "table_metadata": ("a table's metadata", "table metadata items"),
    "table_profiles": ("a table profile", "table profiles"),
    "table_profile": ("a table profile", "table profiles"),
    "documents": ("a document", "documents"),
    "methodology": ("the methodology pack", "the methodology pack"),
    "analysis_summary": ("the analysis summary", "the analysis summary"),
    "population_summary": ("the population summary", "the population summary"),
    "relationship_evidence": ("the relationship evidence", "relationship evidence items"),
}

# A planning-scale step supplies a handful of governance documents, and which
# ones they were is the single most useful fact in the sentence. Only a bulk
# step exceeds this, and there a count genuinely reads better than a list.
_NAMED_DOCUMENT_LIMIT = 8

# Document categories as they read mid-sentence. (singular, plural) — used
# when documents are too numerous to name, so the reader still learns what
# kind of material was involved rather than only how much.
_CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "policy": ("a policy document", "policy documents"),
    "minutes": ("a set of minutes", "sets of minutes"),
    "background": ("a background document", "background documents"),
    "evidence": ("a transaction document", "transaction documents"),
    # A document imported but not yet read. There is no ``other`` category any
    # more, and an uncategorized document is genuinely one nothing has looked
    # at rather than one that fits nothing.
    "": ("a document", "documents"),
}

# Omission reasons are authored sentences from the context resolver
# (agent/context/resolver.py) rather than stable codes, so they are matched
# loosely. They fall into three kinds that mean genuinely different things to
# an auditor, and collapsing them into one clause — as an undifferentiated
# "leaving out" list once did — reads as four failures rather than one
# decision and two facts.
#
# Order matters: "Selector item limit reached." is a scope decision and must
# be classified before the generic "limit" test catches it as capacity.
_OMISSION_KINDS: tuple[tuple[str, str], ...] = (
    ("did not match", "scope"),
    ("selector item limit", "scope"),
    ("limit", "capacity"),
    ("unavailable", "absent"),
    ("no permitted items", "absent"),
    ("representation", "absent"),
)
_OMISSION_ORDER = ("scope", "capacity", "absent")


def _fallback_labels(source_id: str) -> tuple[str, str]:
    """Readable words for a source id no preset has spelled out.

    Many ids are already plural — ``target_aggregates``, ``analysis_results``,
    ``rcm_rows`` — so appending an "s" unconditionally produced "8 target
    aggregatess". The singular takes an article, because a bare "observation"
    reads as a heading rather than a thing the model was handed.
    """
    word = humanize(source_id)
    if not word:
        return ("", "")
    plural = word if word.endswith("s") else f"{word}s"
    return (f"the {word}", plural)


def _omission_kind(reason: str) -> str:
    lowered = reason.casefold()
    for needle, kind in _OMISSION_KINDS:
        if needle in lowered:
            return kind
    return "absent"


def _document_record(source_ref: str | None, workspace: object) -> dict | None:
    """The inventory entry behind a document ref, if there is one.

    Refs are ``document:<id>`` and, where a source selects pages rather than
    whole files, ``document:<id>:page:<n>``. Both name the same document.
    """
    ref = str(source_ref or "")
    if not ref.startswith("document:"):
        return None
    document_id = ref.split(":")[1]
    for item in getattr(workspace, "documents", None) or []:
        if str(item.get("id")) == document_id:
            return item
    return None


def _resolve_documents(items: list, workspace: object) -> list[dict]:
    """The distinct documents behind a group of selections or omissions.

    A document can be selected more than once — chunked analysis supplies one
    selection per chunk — and the reader cares how many documents were
    involved, not how many slices of them were passed.
    """
    records: list[dict] = []
    seen: set[str] = set()
    for item in items:
        record = _document_record(getattr(item, "source_ref", None), workspace)
        if record is None:
            continue
        identity = str(record.get("id") or "")
        if identity in seen:
            continue
        seen.add(identity)
        records.append(record)
    return records


def _document_name(record: dict) -> str:
    """What the auditor calls this document.

    ``source`` is the file as it arrived — "Minutes of Meeting - CFO.docx" —
    and is what someone recognises. ``title`` is a slug derived from it
    ("minutes_of_meeting_cfo"), readable only as a fallback.
    """
    name = str(record.get("source") or "").strip()
    if name:
        return name
    slug = str(record.get("title") or "").strip()
    return slug.replace("_", " ").strip()


def _document_labels(records: list[dict], count: int, *, named: bool) -> list[str]:
    """Name the documents, or say what kind they were.

    Naming is right for what a step read: which four governance documents the
    memorandum rests on is the fact the auditor wants. It is wrong for what a
    step declined, where the *kind* carries the decision — "5 vouchers" says
    at a glance that transaction evidence was held out of planning, while five
    filenames make the reader work it out.
    """
    if named:
        names = [name for name in (_document_name(item) for item in records) if name]
        if names and len(names) <= _NAMED_DOCUMENT_LIMIT:
            return [_joined(names, "and")]
    # Grouped by category, so "5 vouchers" survives where a bare "5 documents"
    # would have thrown the only useful fact away.
    by_category: dict[str, int] = {}
    order: list[str] = []
    for item in records:
        category = str(item.get("category") or "").strip()
        by_category[category] = by_category.get(category, 0) + 1
        if category not in order:
            order.append(category)
    unknown = count - len(records)
    if not order:
        return [_count(count, "document")]
    labels = []
    for category in order:
        total = by_category[category]
        singular, plural = _CATEGORY_LABELS.get(category, _CATEGORY_LABELS[""])
        labels.append(singular if total == 1 else f"{total} {plural}")
    if unknown > 0:
        labels.append(_count(unknown, "document"))
    return [_joined(labels, "and")]


def _grouped_source_labels(
    items: list, workspace: object, *, name_documents: bool = True
) -> list[str]:
    """One label per distinct source, folding repeats into a count.

    Documents are named individually up to ``_NAMED_DOCUMENT_LIMIT``; every
    other source type is too numerous or too undifferentiated to be worth
    naming twice (``table_profiles`` selected six times is six labels the
    reader would skim past, not six facts).

    A group is treated as documents when its refs say so, not when its source
    id happens to be "documents" — the same presets also declare document
    sources under ids like ``planning_documents`` and ``policy_documents``.
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for item in items:
        source_id = str(getattr(item, "source_id", "") or "")
        groups.setdefault(source_id, []).append(item)
        if source_id not in order:
            order.append(source_id)
    labels: list[str] = []
    for source_id in order:
        group = groups[source_id]
        records = _resolve_documents(group, workspace)
        if records:
            labels.extend(
                _document_labels(records, len(records), named=name_documents)
            )
            continue
        singular, plural = _SOURCE_LABELS.get(source_id) or _fallback_labels(source_id)
        labels.append(singular if len(group) == 1 else f"{len(group)} {plural}")
    return labels


def _omission_clauses(omissions: list, workspace: object) -> list[str]:
    """One sentence per kind of omission, each source named at most once.

    A source that lands in more than one bucket — a population summary whose
    candidates hit the size limit and whose source then supplied nothing —
    used to be listed under both reasons in a single clause, so the same words
    appeared twice in one sentence. It is reported once, under the most
    specific reason it earned.
    """
    buckets: dict[str, list] = {}
    claimed: set[str] = set()
    for kind in _OMISSION_ORDER:
        for item in omissions:
            source_id = str(getattr(item, "source_id", "") or "")
            if source_id in claimed:
                continue
            if _omission_kind(str(getattr(item, "reason", "") or "")) != kind:
                continue
            buckets.setdefault(kind, []).append(item)
        claimed.update(
            str(getattr(item, "source_id", "") or "") for item in buckets.get(kind, [])
        )

    clauses: list[str] = []
    scope = _grouped_source_labels(
        buckets.get("scope", []), workspace, name_documents=False
    )
    if scope:
        # A selector that declined a candidate made a scope decision, and
        # saying so plainly is the difference between a tool that chose and a
        # tool that failed. It never claims the material is irrelevant to the
        # engagement — only that this step did not call for it.
        clauses.append(f"Holding back {_joined(scope, 'and')} — outside this step's scope.")
    capacity = _grouped_source_labels(
        buckets.get("capacity", []), workspace, name_documents=False
    )
    if capacity:
        clauses.append(f"Leaving out {_joined(capacity, 'and')} — past the size limit.")
    absent = _grouped_source_labels(
        buckets.get("absent", []), workspace, name_documents=False
    )
    if absent:
        single = len(buckets.get("absent", [])) == 1
        clauses.append(
            f"{_sentence(_joined(absent, 'and'))} "
            f"{'was' if single else 'were'} not available."
        )
    return clauses


# Work products a step reads as input, as opposed to the templates it needs to
# write one and the row-level scaffolding it walks. These are the artifacts an
# auditor recognises and can open, and for a stage like the RCM the memorandum
# is the main context — leaving it in the footer beside the template said the
# opposite. Keyed by exact ref: everything numerous (`rcm:RCM-…`,
# `analysis:A-…`, `datatest:…`) is row-level and stays in the footer.
_WORK_PRODUCT_CARDS: dict[str, tuple[str, str, str]] = {
    "planning:apm": ("Audit planning memorandum", "APM", "apm"),
    "analysis_summary:current": ("Data analysis summary", "EDA", "analysis"),
}


def context_read(
    manifest: "ContextManifest", workspace: object, *, label: str = ""
) -> dict | None:
    """The same reading, as structure rather than a sentence.

    A sentence can list four filenames; it cannot show at a glance that four
    were taken and five deliberately left. Both are projections of the same
    content-free manifest — the sentence stays because it is the accessible
    reading and the durable record, and this is what the transcript draws.

    Documents are named individually here however many there are: the card
    that renders them is scannable in a way a clause is not, so the naming
    limit that protects the sentence does not apply.
    """
    selections = list(getattr(manifest, "selections", None) or [])
    if not selections:
        return None

    def described(record: dict, reason: str = "") -> dict:
        entry = {
            "document_id": str(record.get("id") or ""),
            "name": _document_name(record),
            "category": str(record.get("category") or ""),
            "pages": record.get("pages"),
        }
        return {**entry, "reason": reason} if reason else entry

    documents = [described(record) for record in _resolve_documents(selections, workspace)]

    artifacts: list[dict] = []
    for item in selections:
        ref = str(getattr(item, "source_ref", "") or "")
        card = _WORK_PRODUCT_CARDS.get(ref)
        # A stage revising its own artifact reads it, but naming it beside the
        # stage's own title says the same words twice.
        if card is None or card[0] == str(label or ""):
            continue
        if any(entry["ref"] == ref for entry in artifacts):
            continue
        artifacts.append({"ref": ref, "name": card[0], "badge": card[1], "destination": card[2]})

    omissions = list(getattr(manifest, "omissions", None) or [])
    scoped = [item for item in omissions if _omission_kind(str(getattr(item, "reason", "") or "")) == "scope"]
    withheld = [described(record) for record in _resolve_documents(scoped, workspace)]

    promoted = {entry["ref"] for entry in artifacts}
    supporting = _grouped_source_labels(
        [
            item
            for item in selections
            if not str(getattr(item, "source_ref", "") or "").startswith("document:")
            and str(getattr(item, "source_ref", "") or "") not in promoted
        ],
        workspace,
    )
    absent = _grouped_source_labels(
        [
            item
            for item in omissions
            if _omission_kind(str(getattr(item, "reason", "") or "")) == "unavailable"
        ],
        workspace,
        name_documents=False,
    )
    if not documents and not withheld and not artifacts:
        # Nothing a card would show that the sentence does not say better.
        return None
    return {
        "at": store.utcnow(),
        "stage_title": str(label or ""),
        "artifacts": artifacts,
        "documents": documents,
        "withheld": withheld,
        "supporting": supporting,
        "unavailable": absent,
        # The prose reading of the same manifest. It is not a second thing the
        # transcript shows — the card renders it for assistive technology and
        # nothing else — but it stays on the record so a run read back as JSON
        # still says in words what it read.
        "sentence": context_note(manifest, workspace, label=label),
    }


def context_note(manifest: "ContextManifest", workspace: object, *, label: str = "") -> str:
    """What a model turn is about to read, and what it left out.

    A pure projection of the content-free manifest the context resolver
    already persists (agent/context/manifest.py): it names sources and titles,
    never excerpts, and returns "" when there is nothing worth saying so a
    caller never has to guard against an empty narration line.
    """
    selections = list(getattr(manifest, "selections", None) or [])
    if not selections:
        return ""
    # Documents lead their own sentence. They are the sources an auditor
    # recognises and can open, and burying four filenames at the end of a list
    # of templates and table profiles hid the only part anyone reads. The
    # supporting material follows in a second sentence, where it belongs.
    documents = [
        item
        for item in selections
        if str(getattr(item, "source_ref", "") or "").startswith("document:")
    ]
    supporting = [item for item in selections if item not in documents]
    # No token count. It is the one number in this sentence the auditor cannot
    # act on, and it turns a line about evidence into a line about the model.
    # The manifest still carries `supplied_size` for anyone debugging a run.
    subject = f" for {label}" if label else ""

    sentences: list[str] = []
    if documents:
        records = _resolve_documents(documents, workspace)
        counted = _count(len(records) or len(documents), "document")
        if records and len(records) <= _NAMED_DOCUMENT_LIMIT:
            named = _document_labels(records, len(records), named=True)
            sentences.append(f"Reading {counted}{subject}: {named[0]}.")
        else:
            named = (
                _document_labels(records, len(records), named=False)
                if records
                else []
            )
            sentences.append(f"Reading {_joined(named, 'and') or counted}{subject}.")
        rest = _grouped_source_labels(supporting, workspace)
        if rest:
            sentences.append(f"Also {_joined(rest, 'and')}.")
    else:
        rest = _grouped_source_labels(supporting, workspace)
        if not rest:
            return ""
        sentences.append(f"Reading {_joined(rest, 'and')}{subject}.")

    sentences.extend(
        _omission_clauses(list(getattr(manifest, "omissions", None) or []), workspace)
    )
    return " ".join(sentences)


def repair_note(reason: str = "") -> str:
    """What the agent says when a draft failed its quality gate and is retried.

    ``reason`` is the worker's own validation message (agent/workers/model.py)
    and is deliberately *not* repeated to the auditor: those messages describe
    the response contract, not the audit — "the response must be a JSON object
    with a `finding` object" told a reader nothing they could act on and made a
    routine, recovered retry read like a defect. The parameter stays so callers
    need not change and the reason remains available on the attempt itself.
    """
    return "The first draft didn't match the required shape, so I'm redoing it."


_COMPLETION_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


def _apm_completion(proposal: Mapping) -> str:
    markdown = str(proposal.get("apm_markdown") or "")
    headings = [match.group(1).strip() for match in _COMPLETION_HEADING_RE.finditer(markdown)]
    if not headings:
        return ""
    checklist = "\n".join(f"- {heading}" for heading in headings)
    return f"Drafted every section of the memorandum:\n{checklist}"


def _rcm_completion(proposal: Mapping) -> str:
    rows = proposal.get("rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        return ""
    return f"Drafted {_count(len(rows), 'row')} for the risk and control matrix."


# Keyed by capability id. A response worth reading as a checklist or a count
# once it lands, not just as a headline metric on the eventual milestone card.
_COMPLETION_NOTES: dict[str, Callable[[Mapping], str]] = {
    "planning.apm_ready": _apm_completion,
    "planning.rcm_ready": _rcm_completion,
}


def completion_note(capability_id: str, proposal: Mapping) -> str:
    """What a unit actually produced, once its model call has returned.

    A pure read of the accepted proposal, never the raw response, so this is
    safe to call whether or not a repair happened along the way. An unmapped
    capability returns "" rather than guessing at a proposal shape it does
    not own — most capabilities are already well served by their milestone.
    """
    reader = _COMPLETION_NOTES.get(str(capability_id or ""))
    if reader is None:
        return ""
    try:
        return reader(proposal if isinstance(proposal, Mapping) else {})
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Blockers
# --------------------------------------------------------------------------- #
# Unit error codes are stable machine identities used across executors. This
# catalogue is the only place they become something an auditor can act on.
# ``suggestions`` are chat commands: answering a blocker steers the agent
# through the ordinary command path, so no blocker needs its own endpoint.
_BLOCKERS: dict[str, dict] = {
    "document_has_no_extractable_text": {
        "message": "{subject} has no text I can read — it is most likely a scan or an image.",
        "fallback_subject": "One document",
        "suggestions": [
            ("Skip it and continue", "Skip {subject} and continue with the remaining work."),
            ("Try again with OCR", "Re-extract {subject} with OCR, then analyse it."),
        ],
    },
    "generated_analysis_awaits_auditor_review": {
        "message": "I drafted the analysis for {subject}; it needs your review before it can be used as planning input.",
        "fallback_subject": "these documents",
        "severity": "review",
        "where": "documents",
        "suggestions": [("Continue anyway", "Continue with the generated analysis as it stands.")],
    },
    "analysis_coverage_is_partial": {
        "message": "I could only analyse part of {subject} within the configured page limit.",
        "fallback_subject": "the document",
        "severity": "review",
        "suggestions": [("Continue anyway", "Continue with the partial analysis.")],
    },
    "document_test_definition_needs_auditor_attention": {
        "message": "The document test for {subject} needs your attention before it can run.",
        "fallback_subject": "this test",
        "where": "doc-tests",
    },
    "ambiguous_relationship_requires_confirmation": {
        "message": "More than one join looks plausible for {subject}; I won't guess which one is right.",
        "fallback_subject": "these tables",
        "where": "data",
    },
    "auditor_owned_apm_preserved": {
        "message": "You have edited the audit planning memorandum, so I kept your version instead of overwriting it.",
        "severity": "review",
        "suggestions": [("Regenerate it anyway", "Regenerate the audit planning memorandum from scratch.")],
    },
    "auditor_owned_analysis_preserved": {
        "message": "You have edited the analysis for {subject}, so I kept your version instead of overwriting it.",
        "fallback_subject": "this document",
        "severity": "review",
    },
}

# Statuses that mean "a person has to act", as opposed to "this went wrong".
_OPEN_UNIT_STATUSES = {"blocked", "awaiting_input", "awaiting_confirmation"}
_FAILED_UNIT_STATUSES = {"failed", "conflict"}


def _blocker(stage: dict, unit: dict) -> dict:
    code = str(unit.get("error") or "").strip()
    entry = _BLOCKERS.get(code, {})
    subject = subject_of(unit) or entry.get("fallback_subject") or ""
    template = entry.get("message")
    # `humanize` strips the domain prefix and the separators, so a code that is
    # only punctuation — "." — reduces to nothing and used to leave the sentence
    # "… stopped: ." on screen. An unmapped code is only worth showing when it
    # survives into words.
    detail = humanize(code) if code else ""
    if template:
        message = template.format(subject=subject or "it")
    elif detail:
        # An unmapped code still beats a raw identifier: say what stopped and
        # show the code as supporting detail rather than as the message.
        message = _sentence(f"{unit.get('title') or 'A step'} stopped: {detail}.")
    else:
        message = _sentence(f"{unit.get('title') or 'A step'} needs your input before it can continue.")
    suggestions = [
        {"label": label, "command": command.format(subject=subject or "it")}
        for label, command in entry.get("suggestions", ())
    ]
    failed = unit.get("status") in _FAILED_UNIT_STATUSES
    return {
        "unit_id": str(unit.get("id") or ""),
        "stage_id": str(stage.get("id") or ""),
        "stage_title": str(stage.get("title") or ""),
        "subject": subject,
        "code": code or None,
        "status": str(unit.get("status") or ""),
        "severity": "failed" if failed else entry.get("severity", "blocked"),
        "message": message,
        "where": entry.get("where"),
        "suggestions": suggestions,
    }


def blockers(run: dict) -> list[dict]:
    """Every unit that stopped needing a person, as readable questions.

    Units that merely failed are included too: a failure the auditor cannot see
    without expanding a card and decoding an identifier is, in practice, silent.
    """
    result: list[dict] = []
    for stage in (run.get("workflow") or {}).get("stages") or []:
        for unit in stage.get("units") or []:
            if unit.get("status") in _OPEN_UNIT_STATUSES | _FAILED_UNIT_STATUSES:
                result.append(_blocker(stage, unit))
    # One card per distinct question: twelve documents blocked on the same code
    # is one thing to decide, not twelve.
    grouped: dict[tuple[str, str], dict] = {}
    for item in result:
        key = (item["code"] or item["unit_id"], item["severity"])
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {**item, "unit_ids": [item["unit_id"]], "subjects": [item["subject"]] if item["subject"] else []}
            continue
        existing["unit_ids"].append(item["unit_id"])
        if item["subject"]:
            existing["subjects"].append(item["subject"])
    for item in grouped.values():
        if len(item["unit_ids"]) > 1:
            subjects = _joined(item["subjects"], "and")
            item["message"] = (
                f"{_count(len(item['unit_ids']), 'item')} stopped for the same reason"
                + (f" ({subjects})" if subjects and len(subjects) < 120 else "")
                + f": {item['message'][0].lower() + item['message'][1:]}"
            )
            item["suggestions"] = []
    return list(grouped.values())


# --------------------------------------------------------------------------- #
# Stage and run narration
# --------------------------------------------------------------------------- #
def stage_started(stage: dict) -> str:
    units = stage.get("units") or []
    title = str(stage.get("title") or "work").strip()
    if len(units) > 1:
        return f"{title} — {_count(len(units), 'item')} to work through."
    return f"{title}…"


def _elapsed(stage: dict) -> str:
    started, finished = stage.get("started_at"), stage.get("finished_at")
    millis = store.elapsed_ms(started, finished) if started and finished else None
    if not millis or millis < 1000:
        return ""
    seconds = round(millis / 1000)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60}s".replace(" 0s", "")


#: What a settled stage's status says became of it. ``succeeded`` is absent
#: deliberately: it is the one outcome that reads as plain completion, and it
#: is told with a unit tally instead.
_STAGE_OUTCOMES = {
    "skipped": "skipped",
    "blocked": "could not start",
    "review_required": "needs you",
    "failed": "failed",
    "cancelled": "cancelled",
}


def stage_settled(stage: dict) -> str:
    """One line saying how a stage ended, led by what became of the stage.

    Led by ``status`` rather than by a tally of units, because the two disagree
    exactly where it matters. A stage that never ran has no units to count, and
    counting them alone said "done": one treasury run narrated a blocked
    approval stage as "Cycle rules made effective done" while the rules it would
    have approved did not exist, and the reader had no way to tell. A failed
    stage fared no better, reading "done · 1 failed" in one breath.

    A count is kept only where it still adds something the outcome has not
    already said — work that did land under a stage that failed, or a failure
    among several units rather than the single one the outcome describes.
    """

    units = stage.get("units") or []
    counts: dict[str, int] = {}
    for unit in units:
        status = str(unit.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    title = str(stage.get("title") or "Work").strip()
    outcome = _STAGE_OUTCOMES.get(str(stage.get("status") or ""))
    done = counts.get("succeeded", 0) + counts.get("skipped", 0)
    failed = sum(counts.get(status, 0) for status in _FAILED_UNIT_STATUSES)
    open_units = sum(counts.get(status, 0) for status in _OPEN_UNIT_STATUSES)
    if outcome:
        parts = [f"{title} {outcome}"]
        # Work that did land is worth saying even when the stage as a whole did
        # not: "3 of 5 done" reads very differently from a bare failure.
        if done:
            parts.append(f"{done} of {len(units)} done")
        # A stage that never began has no tally to account for it. Its readiness
        # is the only account there is, and — unlike on a stage that ran and
        # moved past it — it is still current.
        if not stage.get("started_at"):
            reason = next(
                (
                    str(item).strip()
                    for item in (stage.get("readiness_before") or {}).get("reasons") or []
                    if str(item).strip()
                ),
                "",
            )
            if reason:
                parts.append(reason)
    else:
        parts = [f"{title} — {done} of {len(units)} done"] if len(units) > 1 else [f"{title} done"]
    if failed and len(units) > 1:
        parts.append(f"{failed} failed")
    if open_units and len(units) > 1:
        parts.append(f"{open_units} waiting on you")
    elapsed = _elapsed(stage)
    if elapsed:
        parts.append(elapsed)
    return " · ".join(parts)


def stage_handoff(stage: dict, next_title: str) -> str:
    """The line the agent says between a finished stage and the next one.

    A milestone states the result; this states the movement. Together they are
    what makes a long run read as work arriving piece by piece rather than as a
    status block that fills itself in once, at the end.

    Returns empty when nothing follows: the closing turn is the last word on a
    run, and a handoff with nowhere to hand off to would only pre-empt it.
    """
    title = str(stage.get("title") or "").strip()
    following = str(next_title or "").strip().lower()
    if not title or not following:
        return ""
    unresolved = sum(
        1
        for unit in stage.get("units") or []
        if unit.get("status") in _OPEN_UNIT_STATUSES | _FAILED_UNIT_STATUSES
    )
    done = (
        f"{title} is done"
        if not unresolved
        else f"{title} is done, with {_count(unresolved, 'item')} needing you"
    )
    return f"{_sentence(done)} — now working on {following}."


def _produced(run: dict) -> list[str]:
    produced: list[str] = []
    for stage in (run.get("workflow") or {}).get("stages") or []:
        units = stage.get("units") or []
        done = sum(1 for unit in units if unit.get("status") == "succeeded")
        if not done:
            continue
        title = str(stage.get("title") or "").strip().lower()
        produced.append(f"{title} ({_count(done, 'item')})" if len(units) > 1 else title)
    return produced


# Why a unit was stepped over, phrased to follow "I skipped X —".
_SKIP_REASONS = {
    "document_has_no_extractable_text": "it has no text I can read, most likely a scan or an image",
}


def skipped(run: dict) -> list[dict]:
    """Units the run stepped over without doing the work.

    Auto mode settles a question that has one sensible answer by taking it,
    which is only acceptable if the result is stated plainly afterwards. These
    are not blockers — nothing is waiting on anyone — but a document that
    contributed nothing must never disappear quietly.
    """
    result: list[dict] = []
    for stage in (run.get("workflow") or {}).get("stages") or []:
        for unit in stage.get("units") or []:
            if unit.get("status") != "skipped" or not unit.get("error"):
                continue
            code = str(unit.get("error") or "")
            result.append({
                "unit_id": str(unit.get("id") or ""),
                "stage_id": str(stage.get("id") or ""),
                "subject": subject_of(unit),
                "code": code or None,
                "reason": _SKIP_REASONS.get(code) or humanize(code),
            })
    return result


def closing_text(run: dict, status: str | None = None) -> str:
    """The agent's closing turn: what happened, what is open, what is next.

    Deterministic on purpose — a run's last word should not depend on a model
    call that can fail, cost a turn, or contradict the record it summarizes.

    ``status`` lets a runner compose the turn against the terminal status it is
    about to publish, so the message is already on the record when clients see
    the run finish.
    """
    status = str(status or run.get("status") or "")
    open_items = blockers(run)
    stepped_over = skipped(run)
    produced = _produced(run)
    lines: list[str] = []

    if status == "failed":
        error = str(run.get("error") or "").strip()
        lines.append("I couldn't finish this one." + (f" {_sentence(error)}" if error else ""))
        if produced:
            lines.append(f"I did get through {_joined(produced, 'and')} before stopping.")
    elif status == "completed_with_failures":
        # Lead with what landed. This status exists precisely because the run
        # committed real work, and opening on the failure would misdescribe it.
        error = str(run.get("error") or "").strip()
        if produced:
            lines.append(f"Done — {_joined(produced, 'and')}.")
        lines.append(
            "Some of it didn't get through." + (f" {_sentence(error)}" if error else "")
        )
    elif status == "cancelled":
        lines.append("Stopped, as you asked.")
        if produced:
            lines.append(f"Work that had already committed is kept: {_joined(produced, 'and')}.")
    elif produced and run.get("milestones"):
        lines.append(
            "The requested work is complete."
            if status == "completed"
            else "I completed the work that could be finished on this pass."
        )
    elif produced:
        lines.append(f"Done — {_joined(produced, 'and')}.")
    elif open_items or stepped_over:
        # Nothing committed, but something was open or stepped over: that is the
        # whole story, and it must not be reported as "nothing needed doing".
        lines.append("I couldn't commit anything on this pass.")
    else:
        lines.append("Nothing needed doing — everything this asked for was already in place.")

    if stepped_over:
        # Said before the open items: a decision the run made on its own is the
        # part the auditor never got to weigh in on.
        if len(stepped_over) == 1:
            item = stepped_over[0]
            lines.append(f"I skipped {item['subject'] or 'one item'} — {item['reason']}.")
        else:
            lines.append(f"I skipped {_count(len(stepped_over), 'item')} and carried on:")
            lines.extend(
                f"- {item['subject'] or 'One item'} — {item['reason']}"
                for item in stepped_over
            )

    if open_items:
        blocking = [item for item in open_items if item["severity"] != "review"]
        reviewing = [item for item in open_items if item["severity"] == "review"]
        if blocking:
            # Each of these gets its own card with the question and the answers,
            # so restating the sentence here would print it twice in a row. The
            # count is what the narrative owes the reader.
            lines.append(
                f"{_count(len(blocking), 'thing needs', 'things need')} a decision from you"
                + (" — the options are below." if any(item["suggestions"] for item in blocking) else ", below.")
            )
        if reviewing:
            # Review items carry no card, so this is the only place they are said.
            lines.append(
                f"{_count(len(reviewing), 'item is', 'items are')} waiting on your review:"
            )
            lines.extend(f"- {item['message']}" for item in reviewing)

    next_outcomes = list((run.get("workflow") or {}).get("next_outcomes") or [])
    if (
        status in {"completed_with_open_items", "completed_with_failures"}
        and next_outcomes
        and not open_items
    ):
        lines.append(
            "Say “continue” and I'll pick up with "
            f"{_joined([humanize(item) for item in next_outcomes], 'and')}."
        )
    elif status == "failed":
        lines.append("Tell me how you'd like to handle it, or say “retry” to try again.")

    return "\n".join(lines)


def summary_markdown(heading: str, rows: list[tuple[str, object]], note: str = "") -> str:
    """A work-product summary with the noise taken out.

    The closing message already says what happened, so this is the record, not
    a restatement of it: rows that count nothing are dropped rather than listed
    as zeroes, and labels are words rather than capability ids.
    """
    lines = [f"# {heading}", ""]
    for label, value in rows:
        if isinstance(value, (int, float)) and not value:
            continue
        if value in (None, "", [], ()):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"- {label}: {value}")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Next steps
# --------------------------------------------------------------------------- #
# What to offer someone looking at an empty chat. The displayed command is
# paired with its declared capability, so accepting it never needs to infer a
# route from the wording.
_NEXT_STEPS: dict[str, tuple[str, str]] = {
    "documents.analysis_generated": ("Analyse the documents", "Analyse the documents."),
    "planning.apm_ready": ("Draft the APM", "Draft the APM."),
    "planning.rcm_ready": ("Generate the RCM", "Generate the RCM."),
    "tests.specified": ("Draft the tests", "Draft the tests the RCM rows still need."),
    "tests.promoted_from_analysis": (
        "Place saved analyses",
        "Place the saved analyses that found exceptions into the matrix.",
    ),
    "doc_tests.executed": ("Run document tests", "Run the outstanding Document Tests."),
    "findings.drafted": ("Draft findings", "Draft findings."),
    "report.working_draft": ("Draft the report", "Draft the report."),
}

# Guided workflows remain available as a fallback for an empty chat, but a
# completed workflow should not be offered again.  Each command maps to the
# outcomes it can still contribute to; the button is visible while any one of
# those outcomes remains unsatisfied.
_GUIDED_WORKFLOWS: tuple[dict[str, object], ...] = (
    {
        "label": "Full audit",
        "command": "full_audit",
        # Curating the dashboard is something a full audit does, not something
        # a finished audit is missing, so it is not one of the outcomes that
        # keeps this shortcut on offer. Listing it held the button open on
        # engagements where every audit step was done — which reads as the
        # console failing to notice completed work.
        "outcomes": (
            "analysis.executed",
            "findings.drafted",
            "working_papers.generated",
            "report.working_draft",
            "audit.verified",
        ),
    },
    {
        "label": "Planning",
        "command": "plan",
        "outcomes": ("planning.apm_ready", "planning.rcm_ready", "tests.specified"),
    },
    {
        "label": "Data analysis",
        "command": "analyze_data",
        "outcomes": ("analysis.executed",),
    },
    {
        "label": "Document tests",
        "command": "run_document_tests",
        "outcomes": ("doc_tests.executed",),
    },
    {
        "label": "Report",
        "command": "generate_report",
        "outcomes": ("report.working_draft", "audit.verified"),
    },
)


def guided_workflows(state: dict[str, dict] | None) -> list[dict]:
    """Return only guided workflows that still have useful work to do.

    When readiness cannot be computed, preserve every shortcut rather than
    hiding an action based on an incomplete projection.
    """
    workflows: list[dict] = []
    for workflow in _GUIDED_WORKFLOWS:
        outcomes = tuple(workflow["outcomes"])
        if state is not None and all(
            (state.get(outcome) or {}).get("state") == "satisfied"
            for outcome in outcomes
        ):
            continue
        workflows.append({
            "label": str(workflow["label"]),
            "command": str(workflow["command"]),
        })
    return workflows


def next_steps(workspace, state: dict[str, dict] | None = None, *, limit: int = 3) -> list[dict]:
    """Suggestions for what to do next, from deterministic workspace readiness.

    The same readiness projection that decides what a run would schedule also
    answers "what is worth asking for", so an empty chat can offer the two or
    three things this engagement actually needs instead of a fixed template
    menu. Failures are swallowed: a suggestion strip must never be the reason a
    chat fails to load.
    """
    try:
        if state is None:
            from . import capabilities as audit_capabilities

            state = audit_capabilities.workflow_state(workspace)
    except Exception:
        return []
    suggestions: list[dict] = []
    for capability_id, (label, command) in _NEXT_STEPS.items():
        readiness = (state or {}).get(capability_id) or {}
        # The Document Test execution chain can be blocked by a separate
        # definition that needs review while other tests still have unchecked
        # items. It remains the relevant worklist in that case. Findings can
        # likewise be eligible while their deterministic result roll-up has not
        # run; requesting findings schedules that prerequisite automatically.
        document_tests_waiting = (
            capability_id == "doc_tests.executed"
            and readiness.get("state") == "blocked"
            and int(readiness.get("pending") or 0) > 0
        )
        findings_waiting = (
            capability_id == "findings.drafted"
            and readiness.get("state") == "blocked"
            and int(readiness.get("eligible") or 0) > 0
        )
        blocked_behind_unlisted_dependency = (
            capability_id not in {"doc_tests.executed", "findings.drafted"}
            and readiness.get("state") == "blocked"
            and bool(readiness.get("blocking_on"))
            and not any(
                dependency in _NEXT_STEPS
                for dependency in readiness.get("blocking_on") or []
            )
        )
        if readiness.get("state") != "missing" and not (
            document_tests_waiting
            or findings_waiting
            or blocked_behind_unlisted_dependency
        ):
            continue
        reasons = [str(item) for item in readiness.get("reasons") or []]
        suggestions.append(
            {
                "capability": capability_id,
                # This is a declared outcome, not just wording for the
                # assistant to interpret again.
                "requested_outcomes": [capability_id],
                "label": label,
                "command": command,
                "reason": reasons[0] if reasons else "",
            }
        )
        if len(suggestions) >= limit:
            break
    return suggestions
