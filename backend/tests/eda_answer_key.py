"""The Appendix A regression fixture (``S6``) for the exploratory EDA pipeline.

The EDA forward plan names this scorer as the regression gate. Earlier
run-by-run tables were rebuilt by hand three times and were wrong once. This
module is the rebuild, once, in code: an answer key of *computations*, a scorer
over a workspace's saved analyses, and a rendering of the same reach table.

Two rules govern scoring and are intentionally enforced here rather than left to
documentation:

* **Match the computation, never the title.** Titles move between runs and the
  test a frame ran does not. A key item names a test id and the columns that
  identify it; a ``compare_columns`` pair is unordered, because ``A ≥ B`` and
  ``B ≤ A`` are one computation written two ways.
* **A saturated hit is not a hit.** A result flagging its whole population
  establishes nothing about any row in it, and the application already says so
  — so the scorer asks ``analysis_results.uninformative_reason`` rather than
  keeping a second opinion. That function exempts ``referential`` deliberately:
  A30 is a legitimate 96-of-96 finding and a blanket rule would delete it.

Where an item's population matters the key names a ``root`` — the base table the
frame's rows are rows *of*. That is what separates A03 (four invoices whose own
payment status is Rejected) from A03r (four rejected requisitions), and A21
(vendors sharing a bank account) from A29 (staff sharing one). It is the same
distinction ``analysis_semantic_id`` draws, for the same reason.

Three key items carry no signature. A25 is a confirmed negative, A26 needs a
windowed pairing no library test expresses, and A33 is the absence of a column
rather than a property of one. They are reported as unscoreable rather than
silently counted absent, because "no procedure can express this" and "no
procedure was written" are different failures.

Run it directly to score a workspace::

    .venv/bin/python backend/tests/eda_answer_key.py Workspaces/pro4
    .venv/bin/python backend/tests/eda_answer_key.py Workspaces/pro4 Workspaces/pro5
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

if __name__ == "__main__":  # pragma: no cover - direct-invocation convenience
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analysis_results
from app.agent import joins as join_diagnostics
from app.workspaces import Workspace


# Parameter names that carry a spec's identifying columns, per test family. A
# spec is identified by the set of columns it reads, so the scorer collects them
# without needing to know each test's own argument order.
_COLUMN_PARAMS = ("column", "other", "columns", "date_column", "amount_column")


@dataclass(frozen=True)
class Signature:
    """One computation, in the terms that make two runs comparable.

    ``columns`` is a frozen set rather than a sequence: a comparison names two
    columns and the direction it names them in is a property of how the model
    worded it, not of what was measured.
    """

    test: str
    columns: frozenset[str] = frozenset()
    lookup: tuple[str, str] | None = None
    values: frozenset[str] = frozenset()
    #: ``value_filter`` only. Which side of the named set the spec flags, and it
    #: is never optional: ``flag`` on {Active} and ``allow`` on {Active} are
    #: opposite findings over one column, and the second is 37 of 39 vendors
    #: behaving correctly. ``value_filter`` is not saturation-sensitive, so
    #: nothing downstream would have caught the inversion either.
    mode: str = ""

    def matches(self, spec: Mapping[str, object]) -> bool:
        if str(spec.get("test") or "") != self.test:
            return False
        params = spec.get("params")
        params = params if isinstance(params, Mapping) else {}
        if self.columns and _spec_columns(params) != self.columns:
            return False
        if self.lookup is not None:
            found = (
                str(params.get("lookup_table") or ""),
                str(params.get("lookup_column") or ""),
            )
            if found != self.lookup:
                return False
        if self.mode and str(params.get("mode") or "") != self.mode:
            return False
        if self.values:
            named = {str(value) for value in params.get("values") or ()}
            if named != self.values:
                return False
        return True


def _spec_columns(params: Mapping[str, object]) -> frozenset[str]:
    found: set[str] = set()
    for name in _COLUMN_PARAMS:
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
        elif isinstance(value, (list, tuple)):
            found.update(str(item).strip() for item in value if str(item).strip())
    return frozenset(found)


@dataclass(frozen=True)
class KeyItem:
    """One answer-key item and the computation that would reach it."""

    id: str
    label: str
    signatures: tuple[Signature, ...] = ()
    #: The base table the frame's rows must be rows of, where the population is
    #: what distinguishes this item from another reading the same columns.
    root: str | None = None
    #: Why no signature can express this one. Present exactly when signatures is
    #: empty, and reported rather than scored.
    unscoreable: str = ""


def _compare(*columns: str) -> Signature:
    return Signature("compare_columns", frozenset(columns))


def _referential(column: str, table: str, key: str) -> Signature:
    return Signature("referential", frozenset({column}), lookup=(table, key))


def _duplicates(*columns: str) -> Signature:
    return Signature("duplicates", frozenset(columns))


def _flags(column: str, *values: str) -> Signature:
    """A ``value_filter`` that flags the rows holding one of ``values``."""
    return Signature(
        "value_filter", frozenset({column}), values=frozenset(values), mode="flag"
    )


def _allows(column: str, *values: str) -> Signature:
    """A ``value_filter`` that flags the rows holding anything but ``values``."""
    return Signature(
        "value_filter", frozenset({column}), values=frozenset(values), mode="allow"
    )


# The key. Item text is Appendix A of ``docs/procurement-pipeline-review.md``;
# the signatures are the computations verified against the six workbooks.
ANSWER_KEY: tuple[KeyItem, ...] = (
    KeyItem(
        "A01",
        "invoice PO link that is not a PO",
        (_referential("PO_NUMBER_LINK", "po_data", "PO_NUMBER"),),
    ),
    KeyItem(
        "A02",
        "invoice paid with no vendor, no PO, no GRN",
        (
            Signature("completeness", frozenset({"PO_NUMBER_LINK"})),
            Signature("completeness", frozenset({"GRN_ID_LINK"})),
            Signature("completeness", frozenset({"VENDOR_ID"})),
        ),
        root="invoice_data",
    ),
    KeyItem(
        "A03",
        "invoices against Rejected requisitions (invoice side)",
        (_flags("PAYMENT_STATUS", "Rejected"),),
        root="invoice_data",
    ),
    KeyItem(
        "A03r",
        "Rejected requisitions (requisition side)",
        (_flags("REQUISITION_STATUS", "Rejected"),),
        root="requisitions",
    ),
    KeyItem(
        "A04",
        "a rejected requisition re-raised for the identical amount",
        (_duplicates("PO_NUMBER_LINK", "INVOICE_AMOUNT"),),
        root="invoice_data",
    ),
    KeyItem(
        "A05",
        "VINSUSP re-billing",
        (Signature("format_anomaly", frozenset({"VENDOR_INVOICE_NUMBER"})),),
    ),
    KeyItem(
        "A06",
        "invoice exceeds its PO total",
        (
            _compare("INVOICE_AMOUNT", "PO_LINE_TOTAL"),
            _compare("INVOICE_AMOUNT", "PO_TOTAL_AMOUNT"),
        ),
    ),
    KeyItem(
        "A07/A08",
        "duplicate vendor invoice number",
        (_duplicates("VENDOR_INVOICE_NUMBER"),),
    ),
    KeyItem(
        "A09",
        "same vendor, identical amount, both paid",
        (_duplicates("VENDOR_ID", "INVOICE_AMOUNT"),),
    ),
    KeyItem(
        "A10",
        "requisition approved above the limit",
        (_compare("MAX_APPROVAL_AMOUNT", "ESTIMATED_TOTAL_COST"),),
    ),
    KeyItem(
        "A11",
        "invoice approved beyond delegated authority",
        (_compare("INVOICE_AMOUNT", "MAX_APPROVAL_AMOUNT"),),
    ),
    KeyItem(
        "A12",
        "approver title absent from the matrix",
        (_referential("JOB_TITLE", "financial_approval_matrix", "JOB_TITLE"),),
    ),
    KeyItem(
        "A13",
        "requester also verifies or approves",
        (
            _compare("REQUESTER_ID", "VERIFIED_BY_ID"),
            _compare("REQUESTER_ID", "FIN_APPROVED_BY_ID"),
            _compare("REQUESTER_ID", "APPROVED_BY_ID"),
        ),
    ),
    KeyItem(
        "A14",
        "approval outside the verifier's chain",
        (
            _compare("VERIFIED_BY_ID", "SUPERVISOR_APPROVAL_ID"),
            _compare("SUPERVISOR_APPROVAL_ID", "SUPERVISOR_ID"),
        ),
    ),
    KeyItem(
        "A15",
        "invoice approver is also the requisition approver",
        (
            _compare("SUPERVISOR_APPROVAL_ID", "APPROVED_BY_ID"),
            _compare("SUPERVISOR_APPROVAL_ID", "FIN_APPROVED_BY_ID"),
        ),
    ),
    KeyItem("A16", "invoice dated before its PO", (_compare("INVOICE_DATE", "PO_DATE"),)),
    KeyItem(
        "A17",
        "receipt dated before the GRN",
        (_compare("DATE_RECEIVED", "GRN_DATE"),),
    ),
    KeyItem(
        "A18",
        "payment before goods receipt",
        (_compare("PAYMENT_DATE", "GRN_DATE"),),
    ),
    KeyItem(
        "A19",
        "payment before the invoice arrived",
        (
            _compare("DATE_RECEIVED", "PAYMENT_DATE"),
            _compare("INVOICE_DATE", "PAYMENT_DATE"),
        ),
    ),
    KeyItem(
        "A20",
        "invoices with no GRN link",
        (Signature("completeness", frozenset({"GRN_ID_LINK"})),),
        root="invoice_data",
    ),
    KeyItem(
        "A21",
        "vendors sharing a bank account",
        (_duplicates("BANK_ACCOUNT_NUMBER"),),
        root="vendor_master_file",
    ),
    KeyItem(
        "A22",
        "vendor created and approved by one person",
        (_compare("UPDATED_BY", "APPROVED_BY"),),
        root="vendor_master_file",
    ),
    KeyItem(
        "A23",
        "payment to a vendor that is not Active",
        (
            _allows("VENDOR_STATUS", "Active"),
            _flags("VENDOR_STATUS", "Inactive", "Under Review"),
            _flags("VENDOR_STATUS", "Under Review"),
            _flags("VENDOR_STATUS", "Inactive"),
        ),
    ),
    KeyItem(
        "A24",
        "vendor approved the same day it was added",
        (_compare("DATE_ADDED", "APPROVED_DATE"),),
        root="vendor_master_file",
    ),
    KeyItem(
        "A25",
        "no vendor approved from outside Procurement",
        unscoreable=(
            "a confirmed negative — the finding is that no row breaches it, "
            "which no exception count distinguishes from a test never written"
        ),
    ),
    KeyItem(
        "A26",
        "same-vendor requisition pairs within 30 days",
        unscoreable=(
            "needs a windowed self-pairing; no library test expresses it and a "
            "python spec's identity is its code, which no signature can match"
        ),
    ),
    KeyItem(
        "A27",
        "same item at wildly different unit prices",
        (
            _compare("UNIT_PRICE", "ESTIMATED_TOTAL_COST"),
            _compare("UNIT_PRICE", "ESTIMATED_UNIT_COST"),
        ),
    ),
    KeyItem(
        "A28",
        "requisition department contradicts the HR master",
        (_compare("REQUESTER_DEPARTMENT", "DEPARTMENT"),),
    ),
    KeyItem(
        "A29",
        "staff sharing a bank account",
        (_duplicates("BANK_ACCOUNT_NUMBER"),),
        root="staff_details",
    ),
    KeyItem(
        "A30",
        "BUYER_ID resolves to no staff record",
        # Deliberately unconstrained on the lookup. These buyer codes resolve in
        # *no* imported master, so every candidate fails at exactly the same
        # rate and which one a spec names is decided by nothing the data
        # establishes — alphabetical order among the tables outside the frame's
        # lineage, in practice. Requiring ``staff_details`` here would score the
        # identical 96-row finding as reached or absent depending on that.
        (Signature("referential", frozenset({"BUYER_ID"})),),
    ),
    KeyItem(
        "A33",
        "no field anywhere records competitive bidding",
        unscoreable=(
            "negative space over the whole column inventory; it is a statement "
            "the memo makes, not a procedure any frame runs"
        ),
    ),
)

# Document-side items, outside the EDA's scope entirely (§6.2).
OUT_OF_SCOPE = ("A31", "A32")


@dataclass(frozen=True)
class Hit:
    """One saved analysis that reaches a key item."""

    analysis_id: str
    table: str
    root: str
    exceptions: int
    tested: int | None

    @property
    def fraction(self) -> str:
        return f"{self.exceptions}/{self.tested}" if self.tested else str(self.exceptions)


@dataclass(frozen=True)
class ItemScore:
    item: KeyItem
    hits: tuple[Hit, ...] = ()
    #: Analyses matching the signature that were rejected, and why.
    rejected: tuple[tuple[str, str], ...] = ()

    @property
    def reached(self) -> bool:
        return bool(self.hits)

    @property
    def saturated(self) -> bool:
        return not self.hits and bool(self.rejected)


@dataclass(frozen=True)
class Score:
    workspace_id: str
    run_id: str | None
    items: tuple[ItemScore, ...]
    analyses: int
    #: Key items whose signature no run can express (A25, A26, A33).
    unscoreable: tuple[KeyItem, ...] = field(default=())

    @property
    def reached(self) -> tuple[ItemScore, ...]:
        return tuple(item for item in self.items if item.reached)

    @property
    def addressable(self) -> int:
        return len(self.items)


def _analysis_root(workspace: Workspace, table: str) -> str:
    try:
        return join_diagnostics.frame_root(workspace, table)
    except Exception:  # noqa: BLE001 - an unresolvable frame is its own root
        return table


def score_workspace(workspace: Workspace, *, run_id: str | None = None) -> Score:
    """Score every saved analysis in a workspace against the answer key.

    ``run_id`` restricts scoring to one agent run, which is what makes two runs
    in the same workspace comparable. With it absent every saved analysis
    counts, including any the auditor wrote — the same basis used for the pinned
    measurement, since the measured runs are unattended and author everything
    they hold.
    """
    analyses = [
        item
        for item in workspace.analyses
        if run_id is None or str(item.get("agent_run_id") or "") == run_id
    ]
    scored: list[ItemScore] = []
    unscoreable: list[KeyItem] = []
    for key in ANSWER_KEY:
        if not key.signatures:
            unscoreable.append(key)
            continue
        hits: list[Hit] = []
        rejected: list[tuple[str, str]] = []
        for analysis in analyses:
            spec = analysis.get("spec")
            spec = spec if isinstance(spec, Mapping) else {}
            if not any(signature.matches(spec) for signature in key.signatures):
                continue
            table = str(analysis.get("table") or "")
            root = _analysis_root(workspace, table)
            if key.root is not None and root != key.root:
                continue
            result = analysis.get("last_result")
            result = result if isinstance(result, Mapping) else {}
            exceptions = int(result.get("exception_count") or 0)
            tested = result.get("tested")
            tested = int(tested) if isinstance(tested, int) else None
            if not exceptions:
                # A clean result is a test that ran and found nothing. It is a
                # real answer about the data and it is not this item.
                continue
            reason = analysis_results.uninformative_reason(
                analysis,
                exception_count=exceptions,
                denominator=tested,
                rate=(exceptions / tested) if tested else None,
            )
            identifier = str(analysis.get("id") or "")
            if reason:
                rejected.append((identifier, reason))
                continue
            hits.append(Hit(identifier, table, root, exceptions, tested))
        hits.sort(key=lambda hit: (-hit.exceptions, hit.analysis_id))
        scored.append(ItemScore(key, tuple(hits), tuple(rejected)))
    return Score(
        workspace_id=str(workspace.id),
        run_id=run_id,
        items=tuple(scored),
        analyses=len(analyses),
        unscoreable=tuple(unscoreable),
    )


def reach_table(scores: Sequence[Score], labels: Sequence[str]) -> str:
    """Render the comparable reach table across one or more scored runs."""
    header = "| item | " + " | ".join(labels) + " |"
    rule = "|---|" + "|".join("---:" for _ in labels) + "|"
    lines = [header, rule]
    for index, key in enumerate(scores[0].items):
        cells = []
        for score in scores:
            entry = score.items[index]
            if entry.reached:
                cells.append(entry.hits[0].fraction)
            elif entry.saturated:
                cells.append("*sat.*")
            else:
                cells.append("—")
        lines.append(f"| {key.item.id} {key.item.label} | " + " | ".join(cells) + " |")
    totals = " | ".join(f"**{len(score.reached)}**" for score in scores)
    lines.append(f"| **Reached** | {totals} |")
    return "\n".join(lines)


def _report(score: Score, label: str) -> str:
    lines = [
        f"{label}: {len(score.reached)} of {score.addressable} scoreable items "
        f"from {score.analyses} analyses"
    ]
    missing = [item.item.id for item in score.items if not item.reached]
    if missing:
        lines.append("  absent: " + ", ".join(missing))
    saturated = [item.item.id for item in score.items if item.saturated]
    if saturated:
        lines.append("  rejected as saturated: " + ", ".join(saturated))
    if score.unscoreable:
        lines.append(
            "  not scoreable by signature: "
            + ", ".join(item.id for item in score.unscoreable)
        )
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:  # pragma: no cover - CLI
    if not argv:
        print(__doc__)
        return 2
    scores = []
    labels = []
    for path in argv:
        root = Path(path).resolve()
        score = score_workspace(Workspace(root))
        scores.append(score)
        labels.append(root.name)
        print(_report(score, root.name))
    print()
    print(reach_table(scores, labels))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv[1:]))
