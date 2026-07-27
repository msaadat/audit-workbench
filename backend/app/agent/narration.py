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

import re

from . import store

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
) -> dict:
    """Append one progress line to the run's narration log and publish it.

    Callers hold the run's write lock through ``emit``'s runtime; the entry is
    appended in place so the next ``save()`` persists it.
    """
    entry = {
        "at": store.utcnow(),
        "kind": kind,
        "text": str(text or "").strip(),
        "stage_id": stage_id,
        "unit_id": unit_id,
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
    "document_test_results_await_auditor_disposition": {
        "message": "The results for {subject} are ready and waiting for your disposition.",
        "fallback_subject": "this test",
        "severity": "review",
        "where": "doc-tests",
    },
    "ambiguous_relationship_requires_confirmation": {
        "message": "More than one join looks plausible for {subject}; I won't guess which one is right.",
        "fallback_subject": "these tables",
        "where": "data",
    },
    "no_safe_join_evidence": {
        "message": "I found no evidence for a safe join on {subject}, so I stopped rather than invent one.",
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
    if template:
        message = template.format(subject=subject or "it")
    elif code:
        # An unmapped code still beats a raw identifier: say what stopped and
        # show the code as supporting detail rather than as the message.
        message = _sentence(
            f"{unit.get('title') or 'A step'} stopped: {humanize(code)}."
        )
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


def stage_settled(stage: dict) -> str:
    units = stage.get("units") or []
    counts: dict[str, int] = {}
    for unit in units:
        status = str(unit.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    title = str(stage.get("title") or "Work").strip()
    done = counts.get("succeeded", 0) + counts.get("skipped", 0)
    failed = sum(counts.get(status, 0) for status in _FAILED_UNIT_STATUSES)
    open_units = sum(counts.get(status, 0) for status in _OPEN_UNIT_STATUSES)
    parts = [f"{title} — {done} of {len(units)} done"] if len(units) > 1 else [f"{title} done"]
    if failed:
        parts.append(f"{failed} failed")
    if open_units:
        parts.append(f"{open_units} waiting on you")
    elapsed = _elapsed(stage)
    if elapsed:
        parts.append(elapsed)
    return " · ".join(parts)


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
    elif status == "cancelled":
        lines.append("Stopped, as you asked.")
        if produced:
            lines.append(f"Work that had already committed is kept: {_joined(produced, 'and')}.")
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
    if status == "completed_with_open_items" and next_outcomes and not open_items:
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
# What to offer someone looking at an empty chat. Each command is worded to hit
# a deterministic phrase in ``routing.GENERATION_RULES``, so accepting a
# suggestion classifies without spending a router turn.
_NEXT_STEPS: dict[str, tuple[str, str]] = {
    "documents.analysis_generated": ("Analyse the documents", "Analyse the documents."),
    "planning.apm_ready": ("Draft the APM", "Draft the APM."),
    "planning.rcm_ready": ("Generate the RCM", "Generate the RCM."),
    "tests.specified": ("Generate the tests", "Generate each executable test the RCM rows need."),
    "fieldwork.definitions_ready": ("Prepare document tests", "Prepare the document tests."),
    "findings.drafted": ("Draft findings", "Draft findings."),
    "report.working_draft": ("Draft the report", "Draft the report."),
}


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
        if readiness.get("state") != "missing":
            continue
        reasons = [str(item) for item in readiness.get("reasons") or []]
        suggestions.append(
            {
                "capability": capability_id,
                "label": label,
                "command": command,
                "reason": reasons[0] if reasons else "",
            }
        )
        if len(suggestions) >= limit:
            break
    return suggestions
