"""What each exception row failed, how many records that is, and out of how many.

An exception frame answers "which records came back" but never "what was wrong
with them": a step's filter is one predicate with several alternative branches,
and the frame it returns keeps no trace of which branch a row satisfied. A
multi-step test then stacks its steps, so the frame's row count is not a record
count either, and neither number carries the population it was drawn from.

All three are recoverable from what the runner already has. The step's own code
names the branches, the frame the step returned still carries the columns those
branches read, and the workspace tables hold the population. This module
reconstructs the predicate from the step source, attributes every row to the
first branch it satisfies, and counts distinct records against their table.

Attribution is refused rather than guessed. If the reconstructed predicate does
not account for every row the step returned, the reconstruction is wrong about
what the step did, and the caller falls back to naming the step itself.
"""

from __future__ import annotations

import ast

import polars as pl

# Columns the runner adds to an exception frame. They describe the row's
# provenance rather than the record, so they never stand as an entity key.
INTERNAL_COLUMNS = ("_step_id", "_step_label", "_reason")

_DATE_DTYPES = ("Date", "Datetime", "Time")

# A step is free to say why itself. Where it does, that beats anything derived.
_AUTHORED_REASON_COLUMNS = frozenset(
    {"exception_reason", "_reason", "reason", "exception_type", "failure_reason"}
)

# How strongly a column name reads as the identifier of a record. A test's
# exception frame is a join of several tables, so the widest non-null column is
# rarely the record — the identifier is.
_KEY_SUFFIXES = {"_ID": 3, "_NUMBER": 2, "_NO": 2, "_KEY": 2, "_CODE": 2}


def _is_date(dtype: str) -> bool:
    return dtype.startswith(_DATE_DTYPES)


def _column_of(node: ast.AST) -> str | None:
    """The column name when ``node`` is exactly ``pl.col("NAME")``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "col"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pl"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _columns_in(node: ast.AST) -> list[str]:
    names: list[str] = []
    for child in ast.walk(node):
        name = _column_of(child)
        if name and name not in names:
            names.append(name)
    return names


def _literal(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        value = node.value
        return f"'{value}'" if isinstance(value, str) else str(value)
    return ast.unparse(node)


def _split(node: ast.AST, operator: type[ast.operator]) -> list[ast.AST]:
    """Flatten one boolean operator into its operands, left to right."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, operator):
        return _split(node.left, operator) + _split(node.right, operator)
    return [node]


def _describe_term(node: ast.AST, dtypes: dict[str, str]) -> str:
    """One conjunct, as the sentence an auditor would write for it."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
        return f"not {_describe_term(node.operand, dtypes)}"

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        subject = _column_of(node.func.value) or ast.unparse(node.func.value)
        if node.func.attr == "is_null":
            return f"{subject} is missing"
        if node.func.attr == "is_not_null":
            return f"{subject} is present"
        if node.func.attr == "is_in" and len(node.args) == 1:
            return f"{subject} is one of {_literal(node.args[0])}"
        if node.func.attr == "is_duplicated":
            return f"{subject} is repeated"

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = node.left, node.comparators[0]
        left_name, right_name = _column_of(left), _column_of(right)
        subject = left_name or ast.unparse(left)
        obj = right_name or _literal(right)
        chronological = _is_date(dtypes.get(left_name or "", "")) and (
            right_name is None or _is_date(dtypes.get(right_name, ""))
        )
        operator = node.ops[0]
        # Two columns are equal to each other; a column *is* a literal value.
        verb = "equals" if right_name else "is"
        if isinstance(operator, ast.Eq):
            return f"{subject} {verb} {obj}"
        if isinstance(operator, ast.NotEq):
            return f"{subject} does not equal {obj}" if right_name else f"{subject} is not {obj}"
        if isinstance(operator, (ast.Lt, ast.LtE)):
            return f"{subject} is {'earlier than' if chronological else 'less than'} {obj}"
        if isinstance(operator, (ast.Gt, ast.GtE)):
            return f"{subject} is {'later than' if chronological else 'greater than'} {obj}"

    return ast.unparse(node)


def _describe(node: ast.AST, dtypes: dict[str, str]) -> str:
    """One predicate branch, as a phrase.

    A branch that compares two columns normally guards itself against nulls
    first (``A.is_not_null() & B.is_not_null() & (B < A)``). Those guards state
    the mechanics of the comparison, not the exception, so they are dropped
    whenever the column they guard is spoken for by another conjunct.
    """
    terms = _split(node, ast.BitAnd)

    def is_guard(term: ast.AST) -> bool:
        if not (
            isinstance(term, ast.Call)
            and isinstance(term.func, ast.Attribute)
            and term.func.attr == "is_not_null"
        ):
            return False
        guarded = set(_columns_in(term))
        return any(
            other is not term and guarded <= set(_columns_in(other))
            for other in terms
            if not (
                isinstance(other, ast.Call)
                and isinstance(other.func, ast.Attribute)
                and other.func.attr == "is_not_null"
            )
        )

    substantive = [term for term in terms if not is_guard(term)]
    return " and ".join(_describe_term(term, dtypes) for term in (substantive or terms))


def _filter_predicate(code: str) -> ast.AST | None:
    """The argument of the step's last ``.filter(...)`` call."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    found: ast.AST | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "filter"
            and len(node.args) == 1
            and not node.keywords
        ):
            # ``ast.walk`` is breadth-first, so compare positions rather than
            # trusting visit order to run down the method chain.
            if found is None or node.args[0].lineno >= found.lineno:
                found = node.args[0]
    return found


def _columns_read(code: str, frame: pl.DataFrame) -> list[str]:
    """Every column the step's source names, in the frame's own order.

    The fallback for a step whose conditions cannot be told apart. It is a
    weaker answer than the columns one condition reads, but far stronger than
    "every column that happens to hold a value": a step's output is a whole
    joined record, and almost none of it is what the step was looking at.
    """
    try:
        named = {name for name in _columns_in(ast.parse(code))}
    except SyntaxError:
        return []
    return [name for name in frame.columns if name in named]


def reasons_for_step(
    step: dict, frame: pl.DataFrame
) -> tuple[pl.Series | None, dict[str, list[str]]]:
    """One reason per exception row, and the columns each reason reads.

    A step that already labels its own rows is taken at its word. Otherwise the
    reason is recovered from the step's filter. The series is ``None`` — rather
    than a partial answer — whenever the predicate cannot be reconstructed,
    cannot be evaluated against the frame the step returned, or does not account
    for every row in it.
    """
    code = str(step.get("code") or "")
    # Where the reason cannot be attributed the step's own label stands in for
    # it, and the fields the step reads stand in for the fields a condition
    # would have named.
    fallback = {str(step.get("label") or ""): _columns_read(code, frame)}
    if frame.is_empty():
        return None, {}
    authored = next(
        (name for name in frame.columns if name.lower() in _AUTHORED_REASON_COLUMNS),
        None,
    )
    if authored and not frame[authored].null_count():
        labelled = frame[authored].cast(pl.String)
        # The step named its own reasons but not the fields behind each one, so
        # every reason gets the same answer: the fields the step reads.
        return labelled.rename("_reason"), {
            str(value): _columns_read(code, frame) for value in labelled.unique()
        }

    predicate = _filter_predicate(code)
    if predicate is None:
        return None, fallback
    branches = _split(predicate, ast.BitOr)
    if len(branches) < 2:
        return None, fallback

    dtypes = {name: str(dtype) for name, dtype in zip(frame.columns, frame.dtypes)}
    namespace = {"__builtins__": {}, "pl": pl}
    labels: list[str] = []
    masks: list[list[bool]] = []
    columns: dict[str, list[str]] = {}
    for branch in branches:
        try:
            expression = eval(  # noqa: S307 - a sub-expression of already-validated step code
                compile(ast.Expression(branch), "<predicate>", "eval"), namespace
            )
            column = frame.select(pl.Expr.alias(expression, "_match")).to_series()
        except Exception:
            return None, fallback
        if column.dtype != pl.Boolean:
            return None, fallback
        masks.append([bool(value) for value in column.fill_null(False)])
        label = _describe(branch, dtypes)
        labels.append(label)
        # What this condition reads is what the auditor needs in front of them
        # to see it; everything else about the record is a click away.
        columns.setdefault(label, []).extend(
            name for name in _columns_in(branch) if name in frame.columns
        )

    attributed: list[str] = []
    for index in range(frame.height):
        match = next((label for label, mask in zip(labels, masks) if mask[index]), None)
        # Every row came back through this filter. A row no branch claims means
        # the reconstruction is not the predicate the step actually ran.
        if match is None:
            return None, fallback
        attributed.append(match)
    return pl.Series("_reason", attributed, dtype=pl.String), columns


def _key_rank(name: str) -> int:
    upper = name.upper()
    for suffix, rank in _KEY_SUFFIXES.items():
        if upper.endswith(suffix):
            return rank
    return 1 if "ID" in upper else 0


def _entity_key(
    frame: pl.DataFrame,
    step_frames: list[pl.DataFrame],
    tables: dict[str, pl.DataFrame],
) -> tuple[str, tuple[str, int] | None] | None:
    """The column identifying one record across every step, and its population.

    A key has to hold for every step, be populated everywhere, and identify one
    record *within* each step — a step that returns the same value twice is
    keyed on something else, and counting its distinct values would understate
    the exceptions rather than de-duplicate them. Steps that test unrelated
    populations share no such column, and are left without a record count
    instead of being given a foreign key that happens to be common to both.

    Resolving to a population outranks looking like an identifier: a column that
    is unique in an imported table is a record there, whatever it is named.
    """
    best: tuple[int, int, int] | None = None
    chosen: tuple[str, tuple[str, int] | None] | None = None
    for position, name in enumerate(frame.columns):
        if name in INTERNAL_COLUMNS:
            continue
        rank = _key_rank(name)
        if not rank or frame[name].null_count():
            continue
        if not all(
            name in step.columns and step[name].n_unique() == step.height
            for step in step_frames
        ):
            continue
        population = _population(name, tables)
        score = (1 if population else 0, rank, -position)
        if best is None or score > best:
            best, chosen = score, (name, population)
    return chosen


def _population(key: str, tables: dict[str, pl.DataFrame]) -> tuple[str, int] | None:
    """The table the exceptions were drawn from, and how many records it holds.

    The population is the table where the key identifies a record — the one it
    is unique in. Where several qualify, the largest is the wider population.
    """
    candidates = [
        (frame[key].n_unique() == frame.height, frame.height, name)
        for name, frame in tables.items()
        if key in frame.columns and frame.height
    ]
    if not candidates:
        return None
    unique, height, name = max(candidates)
    return (name, height) if unique else None


def build(
    frame: pl.DataFrame | None,
    step_frames: list[pl.DataFrame],
    tables: dict[str, pl.DataFrame],
    reason_columns: dict[str, list[str]] | None = None,
) -> dict | None:
    """The record-level reading of one test's exception frame."""
    if frame is None or frame.is_empty():
        return None
    identified = _entity_key(frame, step_frames, tables)
    key, population = identified if identified else (None, None)

    grouped = "_reason" if "_reason" in frame.columns else "_step_label"
    # Steps whose predicate could not be reconstructed carry their own label as
    # the reason. Where that is true of every row, the breakdown is by step and
    # says nothing a step list would not.
    attributed = grouped == "_reason" and (
        "_step_label" not in frame.columns
        or bool((frame["_reason"] != frame["_step_label"]).any())
    )
    reasons: list[dict] = []
    if grouped in frame.columns:
        counts = frame.group_by(grouped).len(name="rows").sort("rows", descending=True)
        for label, rows in counts.iter_rows():
            matching = frame.filter(pl.col(grouped) == label)
            reasons.append(
                {
                    "label": label,
                    "rows": int(rows),
                    "records": int(matching[key].n_unique()) if key else int(rows),
                    # The columns worth showing for this reason: the ones its
                    # condition reads. Where the reason is not a condition, fall
                    # back to whatever still holds a value for its rows.
                    "columns": (reason_columns or {}).get(label)
                    or [
                        name
                        for name in matching.columns
                        if name not in INTERNAL_COLUMNS
                        and matching[name].null_count() < matching.height
                    ],
                }
            )
    return {
        "entity_key": key,
        "record_count": int(frame[key].n_unique()) if key else int(frame.height),
        "row_count": int(frame.height),
        "population": population[1] if population else None,
        "population_table": population[0] if population else None,
        "reason_source": "predicate" if attributed else "step",
        "reasons": reasons,
    }
