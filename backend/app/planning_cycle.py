"""The shape of an engagement's cycle: its steps, roles, populations and themes.

The first of the two layers a cycle is authored in. The *shape* — the steps of
the process, the document roles each step holds, the population each reads, the
planned themes each owns — is read out of the audit planning memorandum before
anything has been extracted from a document. The *bindings* — the anchor field,
the join keys, the assertions — are :mod:`cycle_rulesets`, authored after the
schemas exist, and take their roles from here rather than inventing them.

The validator is pure so that the drafting turn's gate and the commit can be the
same code: the turn checks a proposal against the lists it was handed, the
workspace checks it against what it actually holds, and a shape that passes the
first cannot then surprise the second. Nothing here reads a workspace, which is
also what lets the model worker import it.
"""

from __future__ import annotations

from collections.abc import Mapping

from . import cycle_rulesets

MAX_CYCLE_STEPS = 12
MAX_CYCLE_NAME_CHARACTERS = 60


class CycleShapeError(ValueError):
    """A cycle shape that cannot be stored, with every problem it has."""


def validate_cycle_shape(
    payload: object,
    *,
    allowed_types: set[str] | frozenset[str],
    base_tables: set[str] | frozenset[str],
    join_names: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Validate and normalize a cycle shape against supplied vocabularies.

    Pure: the two vocabularies a shape is checked against — the document types
    the engagement holds and the tables it has imported — are passed in rather
    than read from a workspace. That is what lets the drafting turn's gate and
    the commit run the *same* validator: the turn checks the shape against the
    lists it was handed, the workspace checks it against what it actually
    holds, and a shape that passes the first cannot then surprise the second.

    Every problem is collected rather than raised at the first, because the one
    repair attempt this turn gets should see all of them.
    """
    # A proposal reaches the drafting gate as frozen mappings rather than
    # dicts, and the same validator serves both paths, so every container test
    # here is against the interface and not the concrete type.
    if not isinstance(payload, Mapping):
        raise CycleShapeError("Planning cycle must be an object.")
    problems: list[str] = []

    def named(value: object, label: str) -> str:
        name = str(value or "").strip()
        if not name:
            problems.append(f"{label} is required.")
        elif len(name) > MAX_CYCLE_NAME_CHARACTERS:
            problems.append(
                f"{label} '{name[:20]}...' is longer than "
                f"{MAX_CYCLE_NAME_CHARACTERS} characters."
            )
        return name

    name = named(payload.get("name"), "Cycle name")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise CycleShapeError("A cycle needs at least one step.")
    if len(raw_steps) > MAX_CYCLE_STEPS:
        problems.append(
            f"A cycle has at most {MAX_CYCLE_STEPS} steps; {len(raw_steps)} were given."
        )

    steps: list[dict] = []
    step_names: dict[str, str] = {}
    role_names: dict[str, str] = {}
    themes_seen: dict[str, str] = {}
    anchors = 0
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            problems.append(f"Cycle step {index + 1} must be an object.")
            continue
        step_name = named(raw.get("name"), f"Cycle step {index + 1} name")
        duplicate = step_names.get(step_name.casefold())
        if duplicate is not None:
            problems.append(f"Cycle step '{step_name}' is named twice.")
        step_names[step_name.casefold()] = step_name

        roles = []
        for raw_role in raw.get("roles") or []:
            if not isinstance(raw_role, Mapping):
                problems.append(f"A role of '{step_name}' must be an object.")
                continue
            role_name = str(raw_role.get("name") or "").strip()
            if not cycle_rulesets.valid_rule_id(role_name):
                problems.append(
                    f"Role '{role_name}' of '{step_name}' must be a lowercase "
                    "identifier such as 'purchase_order'."
                )
            owner = role_names.get(role_name)
            if owner is not None:
                problems.append(
                    f"Role '{role_name}' is declared by both '{owner}' and "
                    f"'{step_name}'; a role fills one position in the cycle."
                )
            role_names[role_name] = step_name
            document_type = str(raw_role.get("document_type") or "").strip()
            if document_type == "other" or document_type not in allowed_types:
                problems.append(
                    f"Role '{role_name}' of '{step_name}' names document type "
                    f"'{document_type}', which this engagement does not hold; "
                    f"choose from: {', '.join(sorted(allowed_types)) or 'none held'}."
                )
            roles.append({"name": role_name, "document_type": document_type})

        populations = []
        for raw_population in raw.get("populations") or []:
            if not isinstance(raw_population, Mapping):
                problems.append(f"A population of '{step_name}' must be an object.")
                continue
            table = str(raw_population.get("table") or "").strip()
            if table in join_names and table not in base_tables:
                problems.append(
                    f"Population '{table}' of '{step_name}' is a derived join, "
                    "not an imported population."
                )
            elif table not in base_tables:
                problems.append(
                    f"Population '{table}' of '{step_name}' is not an imported "
                    f"table; choose from: "
                    f"{', '.join(sorted(base_tables)) or 'none imported'}."
                )
            population = {"table": table}
            columns = raw_population.get("columns")
            if columns:
                population["columns"] = [
                    str(column).strip() for column in columns if str(column).strip()
                ]
            if raw_population.get("anchor"):
                anchors += 1
                population["anchor"] = True
            populations.append(population)

        themes = []
        for entry in raw.get("themes") or []:
            theme = str(entry or "").strip()
            if not theme:
                continue
            # Assignment is a partition: the point of the shape is that each
            # planned theme has exactly one place to be answered. A theme in two
            # steps is two steps each entitled to assume the other covered it.
            owner = themes_seen.get(theme.casefold())
            if owner is not None:
                problems.append(
                    f"Risk theme '{theme}' is assigned to both '{owner}' and "
                    f"'{step_name}'; assign it to exactly one."
                )
                continue
            themes_seen[theme.casefold()] = step_name
            themes.append(theme)

        steps.append(
            {
                "name": step_name,
                "roles": roles,
                "populations": populations,
                "themes": themes,
            }
        )

    if anchors > 1:
        problems.append(
            f"A cycle has at most one anchor population; {anchors} were flagged."
        )

    cross_cutting = None
    raw_cross = payload.get("cross_cutting")
    if raw_cross:
        if not isinstance(raw_cross, Mapping):
            problems.append("The cross-cutting bucket must be an object.")
        else:
            cross_name = named(raw_cross.get("name"), "Cross-cutting bucket name")
            if cross_name.casefold() in step_names:
                problems.append(
                    f"Cross-cutting bucket '{cross_name}' repeats a step name."
                )
            cross_themes = []
            for entry in raw_cross.get("themes") or []:
                theme = str(entry or "").strip()
                if not theme:
                    continue
                owner = themes_seen.get(theme.casefold())
                if owner is not None:
                    problems.append(
                        f"Risk theme '{theme}' is assigned to both '{owner}' and "
                        f"'{cross_name}'; assign it to exactly one."
                    )
                    continue
                themes_seen[theme.casefold()] = cross_name
                cross_themes.append(theme)
            cross_cutting = {"name": cross_name, "themes": cross_themes}

    if problems:
        raise CycleShapeError(" ".join(problems))
    return {"name": name, "steps": steps, "cross_cutting": cross_cutting}


def cycle_process_names(cycle: object) -> list[str]:
    """The closed vocabulary a matrix row's ``process`` is chosen from.

    The step names in the order the cycle runs, then the cross-cutting bucket.
    One function so the prompt, the gate and the page cannot disagree about
    what the allowed values are.
    """
    if not isinstance(cycle, dict):
        return []
    names = [
        str(step.get("name") or "").strip()
        for step in cycle.get("steps") or []
        if str(step.get("name") or "").strip()
    ]
    cross = cycle.get("cross_cutting") or {}
    cross_name = str(cross.get("name") or "").strip() if isinstance(cross, dict) else ""
    if cross_name:
        names.append(cross_name)
    return names
