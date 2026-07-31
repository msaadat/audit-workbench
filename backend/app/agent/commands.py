"""Structured command registry.

A command is an explicit, named shortcut to a registered goal template
(:data:`routing.GOAL_TEMPLATES`) — the deterministic counterpart to the
ask/act and workflow/action classifiers. It is resolved outright, never
guessed, in three ways:

* A slash-prefixed message (``/generate apm``) always resolves to a command
  and nothing else — it never enters ask/act classification. An unrecognized
  slash is reported back rather than silently falling through.
* A structured caller (a tab button, a shortcut chip) can pass the command id
  directly, bypassing text matching entirely.
* A free-typed message that already resolved to "act" is checked against each
  command's ``phrases`` before it reaches the routing-layer phrase tables and
  bounded router. This dictionary is deliberately small and conservative —
  it only shortcuts unambiguous, fully-worded requests; anything else still
  falls through to routing.py exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    id: str
    goal_template: str
    label: str
    # Exact-match names accepted after "/". Matched after normalization, so
    # spacing, hyphens, and underscores are interchangeable.
    slash: tuple[str, ...]
    # Conservative substring phrases checked against already-"act" free text.
    # Kept short and fully-worded to avoid false positives inside unrelated
    # sentences (a bare "plan" or "report" would match too much).
    phrases: tuple[str, ...] = ()


COMMANDS: dict[str, Command] = {
    command.id: command for command in (
        Command(
            "full_audit", "full_audit_working_draft", "Full audit",
            slash=("full audit", "audit"),
            phrases=("full audit", "entire audit", "end-to-end audit", "end to end audit"),
        ),
        Command(
            "plan", "planning", "Plan the engagement",
            slash=("plan", "planning"),
            phrases=("plan the audit", "prepare planning", "prepare engagement planning"),
        ),
        Command(
            "generate_apm", "apm_only", "Generate APM",
            slash=("generate apm", "apm"),
            phrases=("generate apm", "generate the apm", "draft the apm", "update the apm"),
        ),
        Command(
            "draft_findings", "finding_draft", "Draft findings",
            slash=("draft findings", "findings"),
            phrases=("draft findings", "draft eligible findings"),
        ),
        Command(
            "generate_report", "report", "Generate report",
            slash=("generate report", "report"),
            phrases=("generate the report", "draft the report", "audit report"),
        ),
        Command(
            "analyze_data", "data_analysis", "Analyze data",
            slash=("analyze data", "data analysis"),
            phrases=("analyze the data", "analyse the data", "explore the data"),
        ),
        Command(
            "relate_tables", "table_relationships", "Relate tables",
            slash=("relate tables", "table relationships"),
            phrases=("relationships between tables", "join the tables", "relate the tables"),
        ),
        Command(
            "analyze_documents", "document_analysis", "Analyze documents",
            slash=("analyze documents", "document analysis"),
            phrases=("analyze the documents", "analyse the documents", "summarize the documents"),
        ),
        Command(
            "prepare_document_tests", "document_test_preparation", "Prepare document tests",
            slash=("prepare document tests", "prepare tests"),
            phrases=("prepare document tests", "prepare the document tests"),
        ),
        Command(
            "run_document_tests", "document_test_execution", "Run document tests",
            slash=("run document tests", "run tests"),
            phrases=("run document test", "run the document tests", "execute the document tests"),
        ),
    )
}


def _normalize(text: str) -> str:
    return re.sub(r"[\s_-]+", " ", text.strip().casefold()).strip()


_SLASH_INDEX: dict[str, Command] = {
    _normalize(alias): command for command in COMMANDS.values() for alias in command.slash
}


def is_slash(content: str) -> bool:
    return content.strip().startswith("/")


def match_slash(content: str) -> Command | None:
    """Resolve a slash-prefixed message to its command, or ``None`` if unknown."""

    body = _normalize(content.strip()[1:])
    return _SLASH_INDEX.get(body)


def match_phrase(content: str) -> Command | None:
    """Resolve free text already classified as "act" to a command, if any."""

    folded = content.casefold()
    for command in COMMANDS.values():
        if any(phrase in folded for phrase in command.phrases):
            return command
    return None


def help_text() -> str:
    return ", ".join(f"/{command.slash[0]}" for command in COMMANDS.values())
