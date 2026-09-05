"""The cycle drawn: nodes, edges, and the fields the rules actually rest on.

A read-only join over what the workspace already holds — the shape from
:mod:`planning_cycle`, the record kinds from :mod:`document_classification`,
the field vocabulary from :mod:`document_schemas`, the table relationships from
``workspace.joins``, and the rules from :mod:`cycle_rulesets`. No model call and
no inference: the frontend draws what this returns and derives nothing of its
own, so what an auditor sees on the page is what the engagement asserts.

The two layers fill in at different times and the projection says so rather
than waiting for both. Before any schema exists a document node carries no
fields and the strip shows the flow between steps, the populations and the
table joins; the field edges appear when a ruleset does.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import cycle_rulesets, document_classification, document_schemas
from .workspaces import Workspace

#: Every relationship the page draws, and nothing else. A one-operand assertion
#: is not an edge — it says a field must be stated, which is a mark on that
#: field rather than a line to somewhere.
EDGE_KINDS = ("join", "assert", "anchor", "table_join")


def _ruleset(workspace: Workspace) -> dict:
    """The approved rules, or the latest proposal, or nothing.

    A proposal is drawn because the page's job is to let an auditor read the
    rules before approving them; it is labelled as proposed rather than shown as
    if it were in force.
    """

    approved = cycle_rulesets.effective(workspace)
    if approved:
        return approved
    proposals = [
        record
        for record in cycle_rulesets.list_rulesets(workspace)
        if str(record.get("status")) != "rejected"
    ]
    return proposals[-1] if proposals else {}


def _type_labels(workspace: Workspace) -> dict[str, str]:
    """Every type this engagement may name, catalogued and auditor-coined."""

    from . import document_types

    return {
        str(item.get("id")): str(item.get("label") or item.get("id"))
        for item in (*document_types.catalog(), *document_schemas.local_types(workspace))
    }


def _table_rows(workspace: Workspace, table: str) -> int | None:
    """The population's size, or ``None`` where the frame cannot be opened.

    One frame per named population, which is why the whole projection is cached
    on the workspace signature. A table that will not open is a fact about the
    engagement, not an error for the page: the node draws without a count.
    """

    try:
        return int(workspace.get_frame(table).height)
    except Exception:
        return None


def _population_columns(workspace: Workspace, table: str) -> list[str]:
    try:
        return [str(name) for name in workspace.get_frame(table).columns]
    except Exception:
        return []


def cycle_graph(workspace: Workspace) -> dict:
    """Assemble the drawable cycle: one entry per step, plus the edges."""

    cycle = workspace.planning.get("cycle") or {}
    if not cycle.get("steps"):
        return {
            "name": "",
            "steps": [],
            "edges": [],
            "ruleset": None,
            "cross_cutting": None,
        }

    labels = _type_labels(workspace)
    counts = {
        str(entry.get("document_type")): int(entry.get("documents") or 0)
        for entry in document_classification.evidence_type_counts(workspace)
    }
    schemas = {
        str(item.get("document_type")): [
            {
                "name": str(field.get("name") or ""),
                "role": str(field.get("role") or ""),
            }
            for field in item.get("fields") or []
        ]
        for item in document_schemas.list_schemas(workspace)
    }
    ruleset = _ruleset(workspace)
    roles = {
        str(role.get("name")): role for role in ruleset.get("roles") or []
    }

    # Which node each role sits on, so an edge between two fields can name the
    # step it leaves and the step it enters. A role the shape declares but the
    # rules dropped as unreachable is still a node — the step still happens.
    step_of_role: dict[str, str] = {}
    for step in cycle["steps"]:
        for role in step.get("roles") or []:
            step_of_role[str(role.get("name"))] = str(step.get("name"))

    edges: list[dict] = []
    stated: set[tuple[str, str]] = set()

    def operand(side: object) -> dict | None:
        if not isinstance(side, Mapping):
            return None
        role = str(side.get("role") or "")
        if role not in step_of_role:
            return None
        return {
            "step": step_of_role[role],
            "node": role,
            "field": str(side.get("field") or ""),
        }

    for key in ruleset.get("join_keys") or []:
        left, right = operand(key.get("left")), operand(key.get("right"))
        if left and right:
            edges.append({
                "kind": "join",
                "from": left,
                "to": right,
                "rule_id": str(key.get("id") or ""),
                "label": str(key.get("rationale") or ""),
            })
    for item in ruleset.get("assertions") or []:
        left, right = operand(item.get("left")), operand(item.get("right"))
        if left and right:
            edges.append({
                "kind": "assert",
                "from": left,
                "to": right,
                "rule_id": str(item.get("id") or ""),
                "label": str(item.get("requirement") or item.get("label") or ""),
            })
        elif left:
            # One operand is not a relationship. It says this field must be
            # stated at all, which the page marks on the field itself.
            stated.add((left["node"], left["field"]))

    anchor = ruleset.get("anchor") or {}
    anchor_role = str(anchor.get("role") or "")

    steps: list[dict] = []
    for step in cycle["steps"]:
        step_name = str(step.get("name") or "")
        documents = []
        for role in step.get("roles") or []:
            role_name = str(role.get("name") or "")
            document_type = str(role.get("document_type") or "")
            declared = roles.get(role_name)
            documents.append({
                "node": role_name,
                "document_type": document_type,
                "label": labels.get(document_type, document_type),
                "count": counts.get(document_type, 0),
                "fields": schemas.get(document_type, []),
                # A position the shape holds that the rules could not bind. The
                # page greys it and says why rather than hiding a step of the
                # process.
                "bound": bool(declared) if ruleset else None,
            })
        populations = []
        for population in step.get("populations") or []:
            table = str(population.get("table") or "")
            named = [str(value) for value in population.get("columns") or []]
            populations.append({
                "table": table,
                "rows": _table_rows(workspace, table),
                # A step whose records live on a neighbour's table names the
                # columns that hold them; otherwise the whole table is its
                # population and every column belongs to it.
                "columns": named or _population_columns(workspace, table),
                "borrowed": bool(named),
                "anchor": bool(population.get("anchor")),
            })
            if population.get("anchor") and anchor_role in step_of_role:
                edges.append({
                    "kind": "anchor",
                    "from": {
                        "step": step_name,
                        "node": table,
                        "field": str(anchor.get("column") or ""),
                    },
                    "to": {
                        "step": step_of_role[anchor_role],
                        "node": anchor_role,
                        "field": str(anchor.get("field") or ""),
                    },
                    "rule_id": "anchor",
                    "label": "population row to its document",
                })
        steps.append({
            "name": step_name,
            "documents": documents,
            "populations": populations,
            "themes": [str(theme) for theme in step.get("themes") or []],
            "stated": sorted(
                {field for node, field in stated if node in
                 {document["node"] for document in documents}}
            ),
        })

    # The relationships the tables already have, drawn between populations that
    # are actually on the strip. Inferred and measured long before the cycle
    # existed, and worth seeing beside it: a step whose population joins its
    # neighbour's is a flow the data already supports.
    # A table can appear on two steps — the one that owns it and one whose rows
    # live on a few of its columns. A join between tables belongs to the step
    # that owns the population, so an owning occurrence always wins.
    drawn_tables: dict[str, str] = {}
    for step in steps:
        for population in step["populations"]:
            table = population["table"]
            if not population["borrowed"] or table not in drawn_tables:
                drawn_tables[table] = step["name"]
    base_tables = {str(table.get("name") or "") for table in workspace.tables}
    for join in workspace.joins:
        left, right = str(join.get("left") or ""), str(join.get("right") or "")
        if left not in drawn_tables or right not in drawn_tables:
            continue
        if left not in base_tables or right not in base_tables:
            continue
        left_on = [str(value) for value in join.get("left_on") or []]
        right_on = [str(value) for value in join.get("right_on") or []]
        if not left_on or not right_on:
            continue
        edges.append({
            "kind": "table_join",
            "from": {"step": drawn_tables[left], "node": left, "field": left_on[0]},
            "to": {"step": drawn_tables[right], "node": right, "field": right_on[0]},
            "rule_id": str(join.get("name") or ""),
            "label": f"{left_on[0]} = {right_on[0]}",
        })

    cross = cycle.get("cross_cutting") or None
    return {
        "name": str(cycle.get("name") or ""),
        "steps": steps,
        "edges": edges,
        "cross_cutting": (
            {
                "name": str(cross.get("name") or ""),
                "themes": [str(theme) for theme in cross.get("themes") or []],
            }
            if isinstance(cross, Mapping) and cross.get("name")
            else None
        ),
        "ruleset": (
            {
                "ruleset_id": str(ruleset.get("ruleset_id") or ""),
                "status": str(ruleset.get("status") or ""),
                "cycle_label": str(ruleset.get("cycle_label") or ""),
            }
            if ruleset
            else None
        ),
        "created_by": str(cycle.get("created_by") or ""),
        "updated": str(cycle.get("updated") or ""),
    }


def relationship_fields(graph: Mapping[str, object]) -> dict[str, list[str]]:
    """Per node, the fields an edge leaves or enters, in edge order.

    The page shows the vocabulary of the rules rather than the schema: a
    ``purchase_order`` induced with fourteen fields draws the two a rule rests
    on. Kept here rather than in the component so the ordering the layout
    depends on is the same one the tests assert.
    """

    order: dict[str, list[str]] = {}
    for edge in graph.get("edges") or []:
        for side in ("from", "to"):
            operand = edge.get(side) or {}
            node = str(operand.get("node") or "")
            field = str(operand.get("field") or "")
            if not node or not field:
                continue
            fields = order.setdefault(node, [])
            if field not in fields:
                fields.append(field)
    for step in graph.get("steps") or []:
        for document in step.get("documents") or []:
            for field in step.get("stated") or []:
                names = {item["name"] for item in document.get("fields") or []}
                if field in names:
                    fields = order.setdefault(str(document["node"]), [])
                    if field not in fields:
                        fields.append(field)
    return order


__all__ = ["EDGE_KINDS", "cycle_graph", "relationship_fields"]
