"""Exploratory data-analysis capability group.

Owns every outcome of the authoritative analysis graph in
:mod:`agent.workflows.analysis`: ``data.relationships_inferred``,
``data.joins_ready``, ``analysis.definitions_ready``, ``analysis.executed``, and
``analysis.summarized``.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry key for its
declared context. The dependency edges come from the authoritative graph; this
module never restates them.

Table scope resolution lives here too because every capability in the group is
scoped the same way: explicitly named tables, the tables behind selected UI
artifacts, or a bounded fallback over the eligible workspace tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ... import analytics
from ...analysis_results import analysis_result_state, summary_basis_digest
from ...workspaces import Workspace
from .. import joins as join_diagnostics
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import analysis as analysis_workflow

CAPABILITY_IDS: tuple[str, ...] = (
    "data.relationships_inferred",
    "data.join_utility_ready",
    "data.joins_ready",
    "analysis.definitions_ready",
    "analysis.executed",
    "analysis.summarized",
)

# The memo is a single workspace artifact, so it takes exactly one reference.
ANALYSIS_SUMMARY_REF = "analysis_summary:current"

# The bounded fallback: with no explicit target the workflow analyses at most
# this many base tables. Pair-wise relationship diagnosis is quadratic in the
# scope, so the cap is what keeps an unscoped request from turning a large
# workspace into an unbounded Polars sweep.
MAX_SCOPE_TABLES = 6

# Analyses this workflow authored. ``Workspace._apply_provenance`` stamps
# ``created_by="agent"`` for any item carrying an ``agent_run_id``, and a manual
# edit flips it back to ``"user"`` — which is exactly the auditor-edit boundary
# this group honours: user-owned analyses are never regenerated or re-executed
# by the workflow.
AGENT_OWNER = "agent"


# --------------------------------------------------------------------------- #
# Scope resolution (P8.3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableScope:
    """The resolved table scope shared by every analysis capability."""

    tables: tuple[str, ...]
    joins: tuple[str, ...]
    requested: tuple[str, ...]
    unknown: tuple[str, ...]
    ambiguity: str | None = None

    @property
    def explicit(self) -> bool:
        return bool(self.requested)

    @property
    def targets(self) -> tuple[str, ...]:
        """Every frame an analysis definition may be written against.

        The scoped base tables plus the joins already materialized between two
        of them — analysing the join is the point of having created it.
        """

        return (*self.tables, *self.joins)

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(combinations(self.tables, 2))


def _requested_names(scope: dict) -> tuple[str, ...]:
    """Explicit table targets from the command or a selected UI artifact."""

    names: list[str] = []
    for value in scope.get("tables") or []:
        text = str(value or "").strip()
        if text:
            names.append(text)
    for value in scope.get("target_refs") or []:
        ref = str(value or "").strip()
        kind, separator, item = ref.partition(":")
        if not separator or kind not in {"table", "join", "analysis"}:
            continue
        if item.strip():
            names.append(f"{kind}:{item.strip()}")
    return tuple(dict.fromkeys(names))


def resolve_table_scope(workspace: Workspace, scope: dict) -> TableScope:
    """Resolve explicit targets, selected artifacts, or a bounded fallback.

    Resolution order is deliberate: an explicitly named table wins, a selected
    join or saved analysis contributes the base tables behind it, and only a
    request that names nothing at all falls back to the eligible workspace
    tables. The fallback is bounded and reports its own ambiguity rather than
    silently analysing an arbitrary subset.
    """

    base = {str(item.get("name")): item for item in workspace.tables}
    joins = {str(item.get("name")): item for item in workspace.joins}
    analyses = {str(item.get("id")): item for item in workspace.analyses}

    requested = _requested_names(scope)
    selected: list[str] = []
    unknown: list[str] = []
    for value in requested:
        kind, separator, item = value.partition(":")
        name = item if separator else value
        if not separator or kind == "table":
            if name in base:
                selected.append(name)
                continue
            if name in joins:
                selected.extend(_join_sides(joins, name))
                continue
            unknown.append(name)
        elif kind == "join":
            if name in joins:
                selected.extend(_join_sides(joins, name))
            else:
                unknown.append(value)
        else:  # analysis
            bound = str((analyses.get(name) or {}).get("table") or "")
            if not bound:
                unknown.append(value)
            elif bound in base:
                selected.append(bound)
            elif bound in joins:
                selected.extend(_join_sides(joins, bound))
            else:
                unknown.append(value)

    ambiguity: str | None = None
    if selected:
        tables = tuple(dict.fromkeys(selected))
    else:
        eligible = tuple(sorted(base))
        tables = eligible[:MAX_SCOPE_TABLES]
        if len(eligible) > MAX_SCOPE_TABLES:
            ambiguity = (
                f"{len(eligible)} tables are available and none was named; "
                f"analysing the first {MAX_SCOPE_TABLES} in name order "
                f"({', '.join(tables)}). Name the tables to analyse instead."
            )

    # A join belongs to the scope when every base table it was built from is
    # scoped — which admits a chained join whose sides are themselves joins,
    # where checking the two immediate sides for membership would not.
    scoped = set(tables)
    materialized = tuple(
        name
        for name in sorted(joins)
        if join_diagnostics.frame_lineage(workspace, name) <= scoped
    )
    return TableScope(
        tables=tables,
        joins=materialized,
        requested=requested,
        unknown=tuple(dict.fromkeys(unknown)),
        ambiguity=ambiguity,
    )


def _join_sides(joins: dict[str, dict], name: str) -> list[str]:
    join = joins[name]
    return [str(join.get("left")), str(join.get("right"))]


def pair_join(workspace: Workspace, left: str, right: str) -> dict | None:
    """The existing join that directly connects two tables, if any."""

    sides = {left, right}
    return next(
        (
            join
            for join in workspace.joins
            if {str(join.get("left")), str(join.get("right"))} == sides
        ),
        None,
    )


def uncovered_pairs(
    workspace: Workspace, table_scope: TableScope
) -> tuple[tuple[str, str], ...]:
    """Scoped table pairs with no join directly connecting them yet."""

    return tuple(
        pair
        for pair in table_scope.pairs()
        if pair_join(workspace, pair[0], pair[1]) is None
    )


def frame_ref(workspace: Workspace, name: str) -> str:
    """The parent reference for a frame, by what the frame actually is.

    A join and a base table project differently, so a chained join used as a
    fact side must be guarded as ``join:`` — asking for ``table:`` would hash
    a missing entry and guard nothing.
    """

    if any(str(item.get("name")) == name for item in workspace.joins):
        return f"join:{name}"
    return f"table:{name}"


def chain_pairs(
    workspace: Workspace, table_scope: TableScope
) -> tuple[tuple[str, str], ...]:
    """(materialized join, scoped table) pairs that could extend a chain.

    A dimension two hops from the transaction that needs it — an approval limit
    held against the approver's job title — is only reachable once the first
    hop exists, so these pairs come into being *during* the join stage rather
    than at its start. Pairs already connected, and tables the chain already
    contains, are excluded.
    """

    scoped = set(table_scope.tables)
    # One frame per set of base tables. Without this, invoice+PO+staff is built
    # once from the invoice-PO join and again from the invoice-staff join —
    # different frames, different names, identical content — and every one of
    # them then costs a model call to analyse.
    claimed = {
        join_diagnostics.frame_lineage(workspace, name)
        for name in workspace.table_names()
    }
    pairs = []
    for chain in join_diagnostics.chain_fact_frames(workspace):
        lineage = join_diagnostics.frame_lineage(workspace, chain)
        if not lineage <= scoped:
            continue
        for table in table_scope.tables:
            if table in lineage:
                continue
            if pair_join(workspace, chain, table) is not None:
                continue
            combined = lineage | {table}
            if combined in claimed:
                continue
            if not join_diagnostics.chain_extends_reach(workspace, lineage, table):
                continue
            claimed.add(combined)
            pairs.append((chain, table))
    return tuple(pairs)


def agent_analyses(workspace: Workspace, table_scope: TableScope) -> list[dict]:
    """Workflow-authored analyses bound to a frame in scope.

    A manually edited analysis is user-owned (``created_by`` is flipped by
    ``Workspace.update_analysis``) and is deliberately excluded: the workflow
    neither regenerates nor re-executes an analysis the auditor took over.
    """

    targets = set(table_scope.targets)
    return [
        item
        for item in workspace.analyses
        if item.get("created_by") == AGENT_OWNER
        and str(item.get("table") or "") in targets
        and not (
            item.get("kind") == "analytics"
            and analytics.canonical_test_id(
                (item.get("spec") or {}).get("test")
            )
            in analysis_workflow.EXCLUDED_ANALYTICS_TEST_IDS
        )
    ]


def _forced(scope: dict) -> bool:
    return str(scope.get("generation_mode") or "") == "force"


def _no_tables(table_scope: TableScope) -> Readiness | None:
    if table_scope.unknown:
        return Readiness(
            "blocked",
            tuple(f"'{name}' is not an imported table" for name in table_scope.unknown),
        )
    if not table_scope.tables:
        return Readiness("blocked", ("no imported tables are available",))
    return None


# --------------------------------------------------------------------------- #
# data.relationships_inferred (P8.4)
# --------------------------------------------------------------------------- #
def _relationships_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    blocked = _no_tables(table_scope)
    if blocked is not None:
        return blocked
    if len(table_scope.tables) < 2:
        return Readiness(
            "satisfied",
            details={"tables": len(table_scope.tables), "pairs": 0},
        )
    pending = uncovered_pairs(workspace, table_scope)
    details = {
        "tables": len(table_scope.tables),
        "pairs": len(table_scope.pairs()),
        "undiagnosed_pairs": len(pending),
    }
    if not pending:
        # Every scoped pair is already connected by a durable join, so there is
        # nothing left for local diagnosis to establish.
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(pending)} table pair(s) have no established relationship",),
        details=details,
    )


def _relationship_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    table_scope = resolve_table_scope(workspace, scope)
    pairs = (
        table_scope.pairs()
        if _forced(scope)
        else uncovered_pairs(workspace, table_scope)
    )
    return [
        UnitSpec(
            semantic_unit_id("relationship", left, right),
            "relationship_inference",
            f"Diagnose {left} ↔ {right}",
            (frame_ref(workspace, left), frame_ref(workspace, right)),
            {"left": left, "right": right},
        )
        for left, right in pairs
    ]


def _data_relationships_inferred() -> Capability:
    return Capability(
        "data.relationships_inferred",
        "relationships",
        "Table relationships",
        "relationship_inference",
        analysis_workflow.dependencies("data.relationships_inferred"),
        _relationships_ready,
        _relationship_units,
        # Deterministic local diagnostics only: no declared context because no
        # model ever sees this capability's inputs or produces its facts.
        context=None,
        invalidate_on=("tables",),
    )


# --------------------------------------------------------------------------- #
# data.join_utility_ready
# --------------------------------------------------------------------------- #
def _join_utility_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    if len(table_scope.tables) < 2:
        return Readiness("satisfied", details={"candidates": 0})
    # This is run-local intermediate work. It deliberately remains missing
    # while any direct table pair is unjoined so every fresh run gets an
    # identity-persisted utility decision before it can mutate the workspace.
    pending = uncovered_pairs(workspace, table_scope)
    return (
        Readiness("satisfied", details={"candidates": 0})
        if not pending
        else Readiness("missing", (f"{len(pending)} table pair(s) need join-utility selection",), details={"pairs": len(pending)})
    )


def _join_utility_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    table_scope = resolve_table_scope(workspace, scope)
    pending = uncovered_pairs(workspace, table_scope)
    if not pending:
        return []
    return [UnitSpec(
        semantic_unit_id("join_utility", *table_scope.tables), "join_utility",
        "Select audit-useful joins", tuple(f"table:{name}" for name in table_scope.tables),
        {"pairs": [list(pair) for pair in pending]},
    )]


def _data_join_utility_ready() -> Capability:
    return Capability(
        "data.join_utility_ready", "join_utility", "Join utility selection",
        "join_utility", analysis_workflow.dependencies("data.join_utility_ready"),
        _join_utility_ready, _join_utility_units, context="analysis.join_utility",
        invalidate_on=("tables",),
    )


# --------------------------------------------------------------------------- #
# data.joins_ready (P8.5)
# --------------------------------------------------------------------------- #
def _joins_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    blocked = _no_tables(table_scope)
    if blocked is not None:
        return blocked
    if len(table_scope.tables) < 2:
        return Readiness("satisfied", details={"joins": len(table_scope.joins)})
    pending = uncovered_pairs(workspace, table_scope)
    details = {
        "joins": len(table_scope.joins),
        "pairs": len(table_scope.pairs()),
        "unmaterialized_pairs": len(pending),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(pending)} scoped table pair(s) are not joined",),
        details=details,
    )


def _join_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    table_scope = resolve_table_scope(workspace, scope)
    pairs = list(
        table_scope.pairs()
        if _forced(scope)
        else uncovered_pairs(workspace, table_scope)
    )
    # Chain pairs exist only once a first hop has been materialized, so this
    # expansion yields more units each time the stage re-expands. Readiness
    # stays on the base pairs: a chain that finds no strong evidence is a
    # relationship that is not there, not an unfinished stage.
    pairs.extend(chain_pairs(workspace, table_scope))
    return [
        UnitSpec(
            semantic_unit_id("join", left, right),
            "join_materialization",
            f"Join {left} → {right}",
            (frame_ref(workspace, left), frame_ref(workspace, right)),
            {"left": left, "right": right},
        )
        for left, right in pairs
    ]


def _data_joins_ready() -> Capability:
    return Capability(
        "data.joins_ready",
        "joins",
        "Materialized joins",
        "join_materialization",
        analysis_workflow.dependencies("data.joins_ready"),
        _joins_ready,
        _join_units,
        context=None,
        invalidate_on=("tables",),
    )


# --------------------------------------------------------------------------- #
# analysis.definitions_ready (P8.7 / P8.8)
# --------------------------------------------------------------------------- #
def _definitions_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    blocked = _no_tables(table_scope)
    if blocked is not None:
        return blocked
    defined = {
        str(item.get("table") or "") for item in agent_analyses(workspace, table_scope)
    }
    missing = [target for target in table_scope.targets if target not in defined]
    details = {"targets": len(table_scope.targets), "defined": len(defined)}
    if not missing:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(missing)} scoped frame(s) have no analysis definition",),
        details=details,
    )


def _definition_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    table_scope = resolve_table_scope(workspace, scope)
    defined = {
        str(item.get("table") or "") for item in agent_analyses(workspace, table_scope)
    }
    joins = {str(item.get("name")) for item in workspace.joins}
    forced = _forced(scope)
    return [
        UnitSpec(
            semantic_unit_id("analysis_definitions", target),
            "analysis_definition",
            f"Propose analyses — {target}",
            (f"{'join' if target in joins else 'table'}:{target}",),
            {"target": target},
        )
        for target in table_scope.targets
        if forced or target not in defined
    ]


def _analysis_definitions_ready() -> Capability:
    return Capability(
        "analysis.definitions_ready",
        "analysis_definitions",
        "Analysis definitions",
        "analysis_definition",
        analysis_workflow.dependencies("analysis.definitions_ready"),
        _definitions_ready,
        _definition_units,
        context="analysis.definitions",
        invalidate_on=("tables", "joins"),
    )


# --------------------------------------------------------------------------- #
# analysis.executed (P8.9)
# --------------------------------------------------------------------------- #
def _executed_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    blocked = _no_tables(table_scope)
    if blocked is not None:
        return blocked
    items = agent_analyses(workspace, table_scope)
    if not items:
        return Readiness(
            "missing",
            ("no analysis definitions exist for the requested scope",),
            details={"definitions": 0, "executed": 0},
        )
    executed = [
        item for item in items if analysis_result_state(workspace, item) == "current"
    ]
    details = {"definitions": len(items), "executed": len(executed)}
    if len(executed) == len(items):
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (f"{len(items) - len(executed)} analysis definition(s) have no result",),
        details=details,
    )


def _execution_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    forced = _forced(scope)
    table_scope = resolve_table_scope(workspace, scope)
    return [
        UnitSpec(
            semantic_unit_id("analysis_run", str(item["id"])),
            "analysis_execution",
            f"Run analysis — {item.get('title') or item['id']}",
            (f"analysis:{item['id']}",),
            {"analysis_id": str(item["id"]), "table": item.get("table")},
        )
        for item in agent_analyses(workspace, table_scope)
        if forced or analysis_result_state(workspace, item) != "current"
    ]


def _analysis_executed() -> Capability:
    return Capability(
        "analysis.executed",
        "analysis_execution",
        "Analysis results",
        "analysis_execution",
        analysis_workflow.dependencies("analysis.executed"),
        _executed_ready,
        _execution_units,
        context=None,
        invalidate_on=("analyses",),
    )


# --------------------------------------------------------------------------- #
# analysis.summarized
# --------------------------------------------------------------------------- #
# The memo covers every saved analysis, not only the workflow's own and not only
# the requested scope. An auditor reading "what did the EDA find" is asking
# about the engagement, and a procedure they wrote by hand is part of that
# answer exactly as much as one the workflow proposed. Scope governs what a run
# *does*; it does not narrow what the memo is about.
def _summarized_ready(workspace: Workspace, scope: dict) -> Readiness:
    table_scope = resolve_table_scope(workspace, scope)
    blocked = _no_tables(table_scope)
    if blocked is not None:
        return blocked
    executed = [
        item
        for item in workspace.analyses
        if analysis_result_state(workspace, item) == "current"
    ]
    details = {"analyses": len(workspace.analyses), "executed": len(executed)}
    if not executed:
        return Readiness(
            "missing",
            ("no executed analysis has a current result to summarize",),
            details=details,
        )
    memo = workspace.analysis_summary or {}
    if not str(memo.get("markdown") or "").strip():
        return Readiness("missing", ("no analysis summary has been written",), details=details)
    if str(memo.get("basis_sha1") or "") != summary_basis_digest(workspace):
        # Results moved after the memo was written, so its prose describes a
        # state that no longer holds. Nothing is wrong with the memo; it is
        # simply out of date, which is a missing outcome rather than a failure.
        return Readiness(
            "missing",
            ("the analysis summary predates the current results",),
            details=details,
        )
    return Readiness("satisfied", details=details)


def _summary_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    # Exactly one unit: the memo reads across every frame at once, which is the
    # whole reason it can say anything a per-procedure card cannot.
    return [
        UnitSpec(
            semantic_unit_id("analysis_summary"),
            "analysis_summary",
            "Summarize the analysis",
            (ANALYSIS_SUMMARY_REF,),
            {},
        )
    ]


def _analysis_summarized() -> Capability:
    return Capability(
        "analysis.summarized",
        "analysis_summary",
        "Analysis summary",
        "analysis_summary",
        analysis_workflow.dependencies("analysis.summarized"),
        _summarized_ready,
        _summary_units,
        context="analysis.summary",
        invalidate_on=("analyses",),
    )


# Declared auditor-judgment checkpoints for this group, resolved to concrete
# blocking handlers by the composition (which owns the live run and runtime).
# Scope ambiguity is a question only the auditor can settle, and it must be
# settled before any capability fans out against a guessed scope. It is declared
# on both fan-out boundaries — the first capability, and the one that spends a
# model turn per frame — because a run may reuse the first; the handler itself is
# idempotent and asks at most once.
ANALYSIS_SCOPE_CHECKPOINT = "analysis_scope"
STAGE_CHECKPOINTS: dict[str, str] = {
    "data.relationships_inferred": ANALYSIS_SCOPE_CHECKPOINT,
    "analysis.definitions_ready": ANALYSIS_SCOPE_CHECKPOINT,
}


_BUILDERS = {
    "data.relationships_inferred": _data_relationships_inferred,
    "data.join_utility_ready": _data_join_utility_ready,
    "data.joins_ready": _data_joins_ready,
    "analysis.definitions_ready": _analysis_definitions_ready,
    "analysis.executed": _analysis_executed,
    "analysis.summarized": _analysis_summarized,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)


__all__ = [
    "AGENT_OWNER",
    "ANALYSIS_SCOPE_CHECKPOINT",
    "ANALYSIS_SUMMARY_REF",
    "STAGE_CHECKPOINTS",
    "CAPABILITY_IDS",
    "MAX_SCOPE_TABLES",
    "TableScope",
    "agent_analyses",
    "capabilities",
    "pair_join",
    "resolve_table_scope",
    "uncovered_pairs",
]
