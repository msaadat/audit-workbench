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
* A free-typed message that *is* one of a command's ``phrases`` — the whole
  message, not a fragment of it — resolves without a model turn. Matching is
  deliberately whole-message: "generate the audit report" is the command,
  while "tell me about the audit report" is a question that merely mentions
  it. Anything else goes to the assistant's tool loop, which decides for
  itself whether to answer or to start a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    id: str
    goal_template: str | None
    label: str
    # Exact-match names accepted after "/". Matched after normalization, so
    # spacing, hyphens, and underscores are interchangeable. The first alias
    # is the canonical name shown in the composer's autocomplete.
    slash: tuple[str, ...]
    # Local commands are handled by the chat coordinator and never start a
    # model-backed run. They may still appear in the same slash-command menu.
    local_handler: str | None = None
    description: str = ""
    # Complete messages that mean this command and nothing else. Matched
    # whole-message, never as a substring, so a phrase can be an ordinary
    # imperative without swallowing questions that mention the same artifact.
    phrases: tuple[str, ...] = ()


COMMANDS: dict[str, Command] = {
    command.id: command for command in (
        Command(
            "full_audit", "full_audit_working_draft", "Full audit",
            slash=("full audit", "audit"),
            description="Execute RCM-linked tests through an evidence-linked report working draft.",
            phrases=(
                "run the full audit", "do the full audit", "complete the audit",
                "prepare a full audit working draft",
            ),
        ),
        Command(
            "plan", "planning", "Plan the engagement",
            slash=("plan", "planning"),
            description="Prepare or improve engagement planning and the RCM tests that cover it.",
            phrases=(
                "plan the audit", "plan the engagement", "prepare planning",
                "prepare the engagement planning",
            ),
        ),
        Command(
            "generate_apm", "apm_only", "Generate APM",
            slash=("generate apm", "apm"),
            description="Prepare or revise only the audit planning memorandum.",
            phrases=(
                "generate the apm", "draft the apm", "update the apm",
                "generate the audit planning memorandum",
            ),
        ),
        Command(
            "generate_rcm", "rcm_only", "Generate RCM",
            slash=("generate rcm", "rcm"),
            description="Prepare or revise only the risk and control matrix.",
            phrases=(
                "generate the rcm", "draft the rcm", "update the rcm",
                "generate the risk and control matrix",
            ),
        ),
        Command(
            "draft_findings", "finding_draft", "Draft findings",
            slash=("draft findings", "findings"),
            description="Draft evidence-linked findings for the selected observation or risk.",
            phrases=(
                "draft the findings", "draft eligible findings",
                "draft all eligible findings",
            ),
        ),
        Command(
            "generate_report", "report", "Generate report",
            slash=("generate report", "report"),
            description="Prepare evidence-linked audit report working content and run quality checks.",
            phrases=(
                "generate the report", "generate the audit report",
                "draft the report", "draft the audit report",
            ),
        ),
        Command(
            "analyze_data", "data_analysis", "Analyze data",
            slash=("analyze data", "data analysis"),
            description="Analyze available structured data and preserve useful validated work.",
            phrases=("analyze the data", "analyse the data", "explore the data"),
        ),
        Command(
            "relate_tables", "table_relationships", "Relate tables",
            slash=("relate tables", "table relationships"),
            description="Infer table relationships and materialize supported joins.",
            phrases=(
                "relate the tables", "join the tables",
                "infer the table relationships",
            ),
        ),
        Command(
            "analyze_documents", "document_analysis", "Analyze documents",
            slash=("analyze documents", "document analysis"),
            description="Analyze the documents in scope.",
            phrases=(
                "analyze the documents", "analyse the documents",
                "summarize the documents", "summarise the documents",
            ),
        ),
        Command(
            "prepare_document_tests", "document_test_preparation", "Prepare document tests",
            slash=("prepare document tests", "prepare tests"),
            description="Write the executable specification for each drafted test.",
            phrases=("prepare the document tests", "prepare document tests"),
        ),
        Command(
            "run_document_tests", "document_test_execution", "Run document tests",
            slash=("run document tests", "run tests"),
            description="Execute the Document Tests in scope.",
            phrases=(
                "run the document tests", "run document tests",
                "execute the document tests",
            ),
        ),
        Command(
            "status", None, "Audit status",
            slash=("status",), local_handler="status",
            description="Show the current audit status without using the model.",
        ),
    )
}


def _normalize(text: str) -> str:
    # Trailing punctuation is presentation, not meaning: "plan the audit." is
    # the same request as "plan the audit".
    return re.sub(r"[\s_-]+", " ", text.strip().rstrip(".!").casefold()).strip()


_SLASH_INDEX: dict[str, Command] = {
    _normalize(alias): command for command in COMMANDS.values() for alias in command.slash
}

_PHRASE_INDEX: dict[str, Command] = {}
for _command in COMMANDS.values():
    for _phrase in _command.phrases:
        _key = _normalize(_phrase)
        # Two commands claiming one phrase would make the winner depend on
        # registration order, which is exactly the ambiguity whole-message
        # matching exists to remove.
        if _PHRASE_INDEX.setdefault(_key, _command) is not _command:
            raise RuntimeError(f"Command phrase '{_phrase}' is claimed by two commands.")


def is_slash(content: str) -> bool:
    return content.strip().startswith("/")


def match_slash(content: str) -> Command | None:
    """Resolve a slash-prefixed message to its command, or ``None`` if unknown."""

    body = _normalize(content.strip()[1:])
    return _SLASH_INDEX.get(body)


def match_phrase(content: str) -> Command | None:
    """Resolve a message that *is* one known command phrase, or ``None``.

    Whole-message only. "generate the audit report" is the command; "tell me
    about the audit report" is a question that mentions it, and belongs to the
    assistant's tool loop rather than to a run started without a model turn.
    """

    return _PHRASE_INDEX.get(_normalize(content))


def help_text() -> str:
    return ", ".join(f"/{command.slash[0]}" for command in COMMANDS.values())


def list_for_ui() -> list[dict]:
    """The registry projected for the composer's slash-command autocomplete."""

    return [
        {"id": command.id, "slash": command.slash[0], "label": command.label, "description": command.description}
        for command in COMMANDS.values()
    ]
