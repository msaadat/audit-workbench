"""Identity for one saved analysis: which computation, over which rows.

Two questions decide whether two saved analyses are the same procedure, and
both are answered here. *Which computation* is the spec with every column name
rewritten to the base table it came from, so an invoice date lag written
against ``invoice_data`` and the identical lag written against a frame that
joined three masters onto it resolve to one identity. *Which rows* is the
frame's root and the key each joined-in table was reached by, so reconciling a
job title against the approval matrix stays three different questions when it
is asked once per member of staff, once per invoice keyed to its approver, and
once per invoice keyed to its verifier.

This lived inside the analysis-definition worker while the worker was the only
caller. It is not worker logic: the assertion register deduplicates the sweep
with it before any model turn exists, and the identity of a procedure is a
property of the procedure. The worker imports it from here and re-exports it,
so every existing caller is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _resolve_provenance(
    value: object, origins: Mapping[str, str]
) -> tuple[object, set[str]]:
    """Rewrite every column name in a spec to ``origin_table.column``.

    Walks the spec structurally rather than consulting the parameter contract:
    a column is recognised by being a name the frame actually has, which holds
    for every test in the library without this needing to know any of them.
    """
    if isinstance(value, Mapping):
        resolved: dict[str, object] = {}
        scope: set[str] = set()
        for key, item in value.items():
            resolved[str(key)], found = _resolve_provenance(item, origins)
            scope |= found
        return resolved, scope
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        scope = set()
        for item in value:
            resolved_item, found = _resolve_provenance(item, origins)
            items.append(resolved_item)
            scope |= found
        return items, scope
    if isinstance(value, str) and value in origins:
        return f"{origins[value]}.{value}", {origins[value]}
    return value, set()


def spec_scope(spec: Mapping[str, Any], origins: Mapping[str, str]) -> set[str]:
    """The base tables a spec's columns actually come from."""
    _, tables = _resolve_provenance(_plain_json(spec), origins)
    return tables


def python_spec_scope(spec: Mapping[str, Any], origins: Mapping[str, str]) -> set[str]:
    """Return the origins of schema columns referenced by safe Polars code.

    Python definitions are deliberately constrained to a small static sandbox.
    We do not need to execute a procedure (and therefore touch rows) merely to
    establish whether a materialized join contributes to it: any quoted value
    that is also an exact schema column name is a column reference.  This is
    conservative by design; an ambiguous literal only makes the procedure use
    *more* origins, never permits a single-sided joined-frame procedure.

    Code that quotes no column name at all returns an empty set, which says
    nothing either way — a frame-wide preview and a procedure that builds its
    column names by concatenation look identical here.  Callers read an empty
    result as unknown rather than as one-sided.
    """
    code = str(spec.get("code") or "")
    literals = re.findall(r"['\"]([^'\"]+)['\"]", code)
    return {origins[value] for value in literals if value in origins}


def analysis_semantic_id(
    kind: str,
    table: str,
    spec: Mapping[str, Any],
    origins: Mapping[str, str] | None = None,
    root: str = "",
    route: Mapping[str, str] | None = None,
) -> str:
    """Stable identity for one analysis definition.

    Derived from the canonical spec rather than the title, so a reworded
    proposal for the same computation deduplicates against the analysis already
    saved instead of creating a second one.

    ``origins`` maps the frame's columns to the base tables they come from. With
    it, identity is the computation itself — which columns, from which tables —
    rather than the frame it happened to be written against. That is what makes
    an invoice date lag proposed on ``invoice_data`` and the identical lag
    proposed on ``invoice_data_po_data_joined`` one analysis instead of two:
    the join adds columns, but it does not make an invoice-only test a
    different test. A spec spanning both sides of a join still resolves to its
    own identity, because its scope names both tables.

    Without ``origins`` the frame name carries identity, as before — a Python
    analysis reaches frames the spec never names, so its code is only
    meaningfully identified against the frame it was written for.

    ``root`` and ``route`` say which population the computation was asked over,
    and they are the half that provenance alone gets wrong. The columns are only
    one of the two things an analysis is; the rows are the other. Reconciling
    ``staff_details.JOB_TITLE`` against the approval matrix reads one column of
    one table whichever frame asks it, so provenance calls every version of it
    the same analysis — and it answers 48 of 52 over the staff master, 110 of
    112 over invoices keyed to their approver, and 118 of 118 over invoices
    keyed to their verifier. Three questions, three answers, one identity, and
    whichever frame ran first silently deleted the other two. The 2,855.6M
    finding was the one that lost.

    A spec reading only its frame's root keeps the identity it always had: the
    root is the same table on every frame in the family, and an invoice-only
    date lag really is one analysis however many masters were joined on.
    """
    resolved_spec: object = _plain_json(spec)
    scope = str(table)
    if origins and str(kind) != "python":
        resolved_spec, tables = _resolve_provenance(resolved_spec, origins)
        if tables:
            scope = "+".join(sorted(tables))
            if root:
                # Only the tables this spec reads matter; a join that brought in
                # something it never touches did not change what it counts.
                reached = sorted(
                    (name, str((route or {}).get(name) or ""))
                    for name in tables
                    if name != root and name in (route or {})
                )
                scope = "|".join(
                    [scope, str(root), *(f"{name}@{key}" for name, key in reached)]
                )
    canonical = json.dumps(
        {"kind": str(kind), "table": scope, "spec": resolved_spec},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "analysis:" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "analysis_semantic_id",
    "python_spec_scope",
    "spec_scope",
]
