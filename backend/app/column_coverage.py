"""Which columns of the imported populations the audit ever framed a test over.

The completeness question every other coverage measure in this package asks
from the audit's side — does each RCM row have a test, does each test have a
result, does each finding reach the report — asked instead from the *data's*
side: of the columns the auditee supplied, which ones did no procedure ever
evaluate?

It exists because the two signals that look like they should answer it do not.
Matching a saved analysis against RCM row wording scores every analysis in one
engagement at 4–11 shared tokens with some row, with no gap between the
analyses that found exceptions and the ones that did not — a matrix broad
enough to be good is broad enough to mention everything. Matching column names
against control-requirement prose is worse: requirements name 14 of 64 columns,
because a requirement says "invoice amounts are accurately recorded" and not
``INVOICE_AMOUNT``. Test code is the only artifact in the chain that names
columns literally, so it is the only one coverage can be measured against.

What that measurement showed on the engagement it was written for: eight of
``po_data``'s sixteen columns were evaluated by no test — the whole value and
quantity side of the purchase-order population, including ``PO_TOTAL_AMOUNT``,
``UNIT_PRICE`` and ``ORDERED_QUANTITY``. An invoice of 80,000,000 billed
against a purchase order of 8,000,000 sat inside that gap, found by an
exploratory analysis, named in the planning memorandum as the most significant
analytic result of the engagement, and never tested.

Two readings come out of one computation:

* every unevaluated column is a **scope statement** — what the audit did not
  look at, which belongs in the report whether or not anything was wrong there;
* an unevaluated column a saved analysis *flagged exceptions over* is evidenced
  as control-relevant rather than merely untouched, and is the one that has to
  reach test generation.

No row of engagement data is read anywhere in this module. Column names are
schema, already supplied to planning and test generation under
``allow_table_metadata``; exception counts are aggregates. The row-level
permissions in ``ContextPrivacy`` are deliberately not involved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .text import counted
from .workspaces import Workspace

# A Python identifier, which is what a column name is once it reaches either
# a Polars expression or an analytics parameter.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# What a joined frame does to a column name that both sides carry. Polars
# appends ``_right``; this workspace's join builder appends the contributing
# table's name. Either way the underlying population column is the prefix, and
# coverage is stated about the population.
_JOIN_SUFFIXES = ("_right", "_left")


def _base_column(name: str, table_names: frozenset[str]) -> str:
    """The imported column a possibly join-decorated name refers to."""
    for suffix in _JOIN_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    for table in table_names:
        suffix = f"_{table}"
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


class ColumnVocabulary:
    """The imported columns, and how to recognise one inside arbitrary text.

    Scoped to imported tables rather than to every frame, because a derived
    join frame's columns are these columns under another name, and coverage is
    a statement about the populations the auditee supplied.
    """

    def __init__(self, columns_by_table: Mapping[str, Iterable[str]]) -> None:
        self.columns_by_table = {
            str(table): [str(column) for column in columns]
            for table, columns in columns_by_table.items()
        }
        self._table_names = frozenset(self.columns_by_table)
        self._known = frozenset(
            column
            for columns in self.columns_by_table.values()
            for column in columns
        )

    def __bool__(self) -> bool:
        return bool(self._known)

    @property
    def columns(self) -> frozenset[str]:
        return self._known

    def tables_carrying(self, column: str) -> list[str]:
        return sorted(
            table
            for table, columns in self.columns_by_table.items()
            if column in columns
        )

    def found_in(self, payload: object) -> set[str]:
        """Every imported column named anywhere inside ``payload``.

        Deliberately the most generous reading available: any identifier in any
        string, at any depth, that resolves to a known column counts as naming
        it. A stricter rule — only ``pl.col`` arguments, only filter predicates
        — would report more columns as untested, and the error this measure
        must not make is claiming the audit ignored something it did evaluate.
        A gap this reports is a gap under every stricter reading too.
        """
        found: set[str] = set()

        def scan(value: object) -> None:
            if isinstance(value, str):
                for token in _IDENTIFIER.findall(value):
                    base = _base_column(token, self._table_names)
                    if base in self._known:
                        found.add(base)
            elif isinstance(value, Mapping):
                for item in value.values():
                    scan(item)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    scan(item)

        scan(payload)
        return found


def vocabulary(workspace: Workspace) -> ColumnVocabulary:
    """Read the imported populations' column names.

    Failure to read one table is not failure of the measure: coverage over the
    tables that did load is still true, and a measure that refuses to report
    anything because one import is unreadable reports nothing exactly when the
    workspace is in the state most worth describing.
    """
    from . import tooling

    columns_by_table: dict[str, list[str]] = {}
    for table in workspace.tables:
        name = str(table.get("name") or "")
        if not name:
            continue
        try:
            schemas = tooling.table_schemas(workspace, [name])
        except Exception:
            continue
        for schema in schemas:
            if schema.get("error"):
                continue
            columns_by_table[name] = [
                str(column.get("name"))
                for column in schema.get("columns") or []
                if isinstance(column, Mapping) and column.get("name")
            ]
    return ColumnVocabulary(columns_by_table)


def tested_columns(workspace: Workspace, vocab: ColumnVocabulary) -> set[str]:
    """Columns some durable data test's steps name.

    Keyed on the definitions rather than on executed results, because the two
    describe different failures and the run record already carries the other
    one. A test that exists and has not run is incomplete execution; a column no
    test mentions at all is a procedure the audit never wrote.
    """
    found: set[str] = set()
    for test in workspace.data_tests:
        for step in test.get("steps") or []:
            if isinstance(step, Mapping):
                found |= vocab.found_in(step.get("code"))
    return found


def flagging_analyses(
    workspace: Workspace, vocab: ColumnVocabulary
) -> dict[str, list[dict[str, Any]]]:
    """Per column, the saved analyses that recorded exceptions over it.

    Read from each analysis' *definition* — its parameters and its code — not
    from the flagged rows, which stay in their sidecar. The definition is what
    says which columns the procedure evaluated, and it is authored text rather
    than engagement data.
    """
    by_column: dict[str, list[dict[str, Any]]] = {}
    for analysis in workspace.analyses:
        result = analysis.get("last_result")
        if not isinstance(result, Mapping):
            continue
        try:
            exceptions = int(result.get("exception_count") or 0)
        except (TypeError, ValueError):
            continue
        if exceptions <= 0:
            continue
        entry = {
            "analysis_id": str(analysis.get("id") or ""),
            "title": str(analysis.get("title") or ""),
            "exception_count": exceptions,
        }
        for column in sorted(vocab.found_in(analysis.get("spec"))):
            by_column.setdefault(column, []).append(entry)
    return by_column


def untested_columns(workspace: Workspace) -> list[dict[str, Any]]:
    """Per imported table, the columns no data test names, worst tables first.

    The disclosure half. Ordered by how much of a population is unevaluated so
    that a table the audit barely touched leads, which is the order a scope
    statement is read in.
    """
    vocab = vocabulary(workspace)
    if not vocab:
        return []
    tested = tested_columns(workspace, vocab)
    flagged = flagging_analyses(workspace, vocab)
    gaps = []
    for table, columns in vocab.columns_by_table.items():
        missing = [column for column in columns if column not in tested]
        if not missing:
            continue
        gaps.append(
            {
                "table": table,
                "column_count": len(columns),
                "untested_count": len(missing),
                "columns": [
                    {"column": column, "analyses": flagged.get(column) or []}
                    for column in missing
                ],
            }
        )
    return sorted(
        gaps, key=lambda gap: (-gap["untested_count"], gap["table"])
    )


def coverage_by_table(workspace: Workspace) -> dict[str, list[dict[str, Any]]]:
    """Per table, per column, the data tests whose steps name that column.

    The auditor's view of the same measurement ``untested_columns`` discloses.
    That function answers "what did the audit never evaluate" for the report,
    and reaches the report's model context as counts only, deliberately; this
    answers "which test evaluates this column" for the person reading the
    table, and so names the tests.

    Computed for the whole workspace in one pass because the expensive half —
    reading every table's schema — is shared: asking it per table would rebuild
    the vocabulary once per table.

    A column name shared by two tables is credited to a test that names it
    either way. Test code names a column, not a table's column, which is the
    limit ``ColumnVocabulary`` already documents; it errs towards reporting
    coverage rather than inventing a gap.
    """
    vocab = vocabulary(workspace)
    named: list[tuple[str, set[str]]] = []
    for test in workspace.data_tests:
        found: set[str] = set()
        for step in test.get("steps") or []:
            if isinstance(step, Mapping):
                found |= vocab.found_in(step.get("code"))
        if found:
            named.append((str(test.get("id") or ""), found))
    return {
        table: [
            {
                "column": column,
                "tests": [test_id for test_id, found in named if column in found],
            }
            for column in columns
        ]
        for table, columns in vocab.columns_by_table.items()
    }


def table_coverage(workspace: Workspace, table_name: str) -> dict[str, Any] | None:
    """``coverage_by_table`` for one table, or None when it cannot be read."""
    columns = coverage_by_table(workspace).get(str(table_name))
    return None if columns is None else {"table": str(table_name), "columns": columns}


def untested_flagged_columns(workspace: Workspace) -> list[dict[str, Any]]:
    """Columns an analysis flagged exceptions over and no data test names.

    The half that has to act rather than be disclosed. Small by construction —
    an engagement's analyses touch a fraction of its columns and most of those
    are tested — which is what makes it affordable to put in front of every
    test-generation turn.
    """
    vocab = vocabulary(workspace)
    if not vocab:
        return []
    tested = tested_columns(workspace, vocab)
    return [
        {
            "column": column,
            "tables": vocab.tables_carrying(column),
            "analyses": analyses,
        }
        for column, analyses in sorted(flagging_analyses(workspace, vocab).items())
        if column not in tested
    ]


def untested_column_summary(workspace: Workspace) -> list[dict[str, Any]]:
    """The same gaps as counts per population, naming no column.

    The shape that may cross into a model context. Report generation declares a
    narrower privacy boundary than planning or test generation — it is given
    conclusions and narrative, never the shape of the data — and
    ``test_report_context_excludes_rows_and_document_excerpts`` pins that with
    an analysis' own key column as its sentinel. A count answers the question
    the report has to answer: how much of a population went unexamined. Which
    columns those are is an auditor's question, answered in the workspace,
    where no provider is involved.
    """
    return [
        {
            "table": gap["table"],
            "column_count": gap["column_count"],
            "untested_count": gap["untested_count"],
            "flagged_count": sum(
                1 for column in gap["columns"] if column["analyses"]
            ),
        }
        for gap in untested_columns(workspace)
    ]


def untested_column_warning(summary: list[Mapping[str, Any]]) -> str:
    """One sentence on the least-covered populations, or nothing.

    Written for the report's scope narrative, so it leads with how much was not
    looked at and separates that from the part exploratory analysis had already
    flagged — the difference between "we did not test this" and "we did not
    test this and something in it was already known to be irregular".
    """
    if not summary:
        return ""
    total = sum(int(gap.get("untested_count") or 0) for gap in summary)
    leading = [
        f"{gap['table']} ({gap['untested_count']} of {gap['column_count']})"
        for gap in summary[:3]
    ]
    sentence = (
        f"No data test evaluates {counted(total, 'imported column')}: "
        f"{', '.join(leading)}"
    )
    if len(summary) > 3:
        sentence += f", and {counted(len(summary) - 3, 'further table')}"
    flagged = sum(int(gap.get("flagged_count") or 0) for gap in summary)
    if flagged:
        sentence += (
            f". Exploratory analysis recorded exceptions over "
            f"{counted(flagged, 'of those columns', 'of those columns')}"
        )
    return sentence + "."
