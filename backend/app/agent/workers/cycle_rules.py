"""The cycle's rules: parsing, checking and the coverage a test claims.

Shared by two workers, which is why it lives on its own. ``planning.cycle``
designs the cycle whole — its steps, the document roles each holds, and the
rules that link those documents: the anchor a population row is vouched from,
the join keys that say which record reaches which, and the assertions that say
what linked records must then agree on. ``tests.generate`` later says, for one
matrix row, which of those approved assertions answer each of its
transaction-cycle requirements.

The two halves used to be one turn, ``tests.cycle_linkage``, which ran after
the matrix and only when the matrix asked for linked evidence. Every check that
turn ran is kept here, split along the seam the redesign cut: what is a
property of the documents (roles, fields, identifiers, reachability, the field
pairs the schemas propose) is checked against the cycle design; what is a
property of a requirement (which roles it reads, which documents its words
name, which assertions answer it) is checked against the test that cites it.

Everything here is pure. It is handed the schemas, the roles and the response,
and reaches no workspace, so a proposal is judged against exactly what the
turn was shown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ... import cycle_field_pairs, cycle_rulesets, document_types
from ...text import counted
from .model import WorkerResponseValidationError


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def operand(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    role = str(raw.get("role") or "").strip()
    field = str(raw.get("field") or "").strip()
    if not role or not field:
        raise WorkerResponseValidationError(f"{label} needs a role and a field.")
    return {"role": role, "field": field}


def name_list(raw: object, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise WorkerResponseValidationError(f"{label} must be an array of names.")
    names: list[str] = []
    for value in raw:
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _objects(raw: object, label: str) -> list[Mapping]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise WorkerResponseValidationError(f"{label} must be an array.")
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise WorkerResponseValidationError(f"{label}[{index}] must be an object.")
    return list(raw)


def parse_rules(payload: object) -> dict[str, Any]:
    """The rules half of a cycle design, in its normalized shape.

    Roles are not here: they are the shape's, declared once on the steps that
    hold them. Coverage is not here either — no requirement has been written
    when the cycle is designed.
    """

    if not isinstance(payload, Mapping):
        raise WorkerResponseValidationError(
            "rules must be an object with anchor, join_keys and assertions."
        )
    join_keys = [
        {
            "id": str(raw.get("id") or "").strip(),
            "left": operand(raw.get("left"), f"rules.join_keys[{index}].left"),
            "right": operand(raw.get("right"), f"rules.join_keys[{index}].right"),
            "match": "normalized_equal",
            "rationale": str(raw.get("rationale") or "").strip(),
        }
        for index, raw in enumerate(_objects(payload.get("join_keys"), "rules.join_keys"))
    ]
    assertions = []
    for index, raw in enumerate(_objects(payload.get("assertions"), "rules.assertions")):
        right = raw.get("right")
        assertions.append({
            "id": str(raw.get("id") or "").strip(),
            "label": str(raw.get("label") or "").strip(),
            "left": operand(raw.get("left"), f"rules.assertions[{index}].left"),
            "right": (
                operand(right, f"rules.assertions[{index}].right")
                if right is not None
                else None
            ),
            "requirement": str(raw.get("requirement") or raw.get("rationale") or "").strip(),
            "rationale": str(raw.get("rationale") or "").strip(),
        })
    declined_pairs = [
        {"id": str(raw.get("id") or "").strip(), "reason": str(raw.get("reason") or "").strip()}
        for raw in _objects(payload.get("declined_pairs"), "rules.declined_pairs")
    ]
    unreachable = [
        {"role": str(raw.get("role") or "").strip(), "reason": str(raw.get("reason") or "").strip()}
        for raw in _objects(payload.get("unreachable"), "rules.unreachable")
    ]
    anchor = payload.get("anchor")
    if not isinstance(anchor, Mapping):
        raise WorkerResponseValidationError(
            "rules.anchor must name the role, field and column a population row "
            "is vouched from."
        )
    return {
        "anchor": {
            "role": str(anchor.get("role") or "").strip(),
            "field": str(anchor.get("field") or "").strip(),
            "column": str(anchor.get("column") or "").strip(),
        },
        "join_keys": join_keys,
        "assertions": assertions,
        "declined_pairs": declined_pairs,
        "unreachable": unreachable,
    }


def parse_coverage(raw: object, label: str) -> dict[str, Any]:
    """One requirement's answer: the roles it reads, its assertions, its gaps."""

    if not isinstance(raw, Mapping):
        raise WorkerResponseValidationError(f"{label} must be an object.")
    if raw.get("assertion_ids") is not None:
        name_list(raw.get("assertion_ids"), f"{label}.assertion_ids")
    entry: dict[str, Any] = {
        "reads": name_list(raw.get("reads"), f"{label}.reads"),
        "assertion_ids": cycle_rulesets.coverage_assertion_ids(raw),
    }
    gaps = []
    for gap_index, gap in enumerate(raw.get("gaps") or []):
        if not isinstance(gap, Mapping):
            raise WorkerResponseValidationError(f"{label}.gaps[{gap_index}] must be an object.")
        gaps.append({
            "role": str(gap.get("role") or "").strip(),
            "reason": str(gap.get("reason") or "").strip(),
        })
    if gaps:
        entry["gaps"] = gaps
    return entry


# --------------------------------------------------------------------------- #
# the rules, checked against the schemas
# --------------------------------------------------------------------------- #
def as_rule_id(value: str) -> str:
    """The nearest well-formed id to one that is not, for a repair to copy."""

    cleaned = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return cleaned if re.match(r"^[a-z]", cleaned) else f"rule_{cleaned}".rstrip("_")


def refuse_malformed_ids(rules: Mapping[str, Any]) -> None:
    """Refuse ids the ruleset store will not keep.

    Reported together and with the corrected form spelled out. A proposal that
    took the id convention from somewhere else took it from there everywhere —
    all 54 assertions of one treasury proposal were dotted — so naming the first
    and stopping would spend the repair turn on a single character.
    """

    fields = (("join_keys", "id", "Join key"), ("assertions", "id", "Assertion"))
    offenders = [
        (label, str((item or {}).get(key) or ""))
        for section, key, label in fields
        for item in rules.get(section) or []
        if not cycle_rulesets.valid_rule_id((item or {}).get(key))
    ]
    if not offenders:
        return
    shown = "; ".join(
        f"{label} '{raw}' should be '{as_rule_id(raw)}'"
        for label, raw in offenders[:5]
    )
    rest = (
        f" The same applies to {counted(len(offenders) - 5, 'other id')}."
        if len(offenders) > 5
        else ""
    )
    raise WorkerResponseValidationError(
        f"{counted(len(offenders), 'id')} in these rules "
        f"{'is' if len(offenders) == 1 else 'are'} not {cycle_rulesets.RULE_ID_RULE}. "
        f"Rewrite: {shown}.{rest} An id is a name for the rule, not a reference "
        "to anything outside it."
    )


def check_fields(
    rules: Mapping[str, Any],
    roles: Mapping[str, str],
    schemas: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    """Every operand names a role and a field its document type carries.

    The identifier rule is enforced here rather than left to the store because
    it is the one mistake worth spending a repair turn on: a join key on an
    amount reads perfectly and fuses every transaction sharing that amount.
    """

    def field_of(side: Mapping[str, Any], label: str) -> Mapping[str, Any]:
        role = str(side.get("role"))
        if role not in roles:
            raise WorkerResponseValidationError(
                f"{label} names role '{role}', which no step of the cycle holds "
                f"with a schema; its roles are: {', '.join(roles) or 'none'}."
            )
        field = schemas[roles[role]].get(str(side.get("field")))
        if field is None:
            raise WorkerResponseValidationError(
                f"{label} names field '{side.get('field')}', which "
                f"'{roles[role]}' does not carry."
            )
        return field

    for item in rules.get("join_keys") or []:
        for side in ("left", "right"):
            field = field_of(item[side], f"Join key '{item['id']}' {side}")
            if str(field.get("role")) != "identifier":
                raise WorkerResponseValidationError(
                    f"Join key '{item['id']}' joins on '{field.get('name')}', which "
                    f"is a {field.get('role')} field. Only identifier fields can "
                    "join — two records sharing an amount is a coincidence, not a link."
                )
    for item in rules.get("assertions") or []:
        field_of(item["left"], f"Assertion '{item['id']}' left")
        if item.get("right") is not None:
            field_of(item["right"], f"Assertion '{item['id']}' right")
        if not str(item.get("requirement") or item.get("rationale") or "").strip():
            raise WorkerResponseValidationError(
                f"Assertion '{item['id']}' states no requirement. Say what these "
                "fields must show for the control to hold."
            )
    field_of(rules["anchor"], "The anchor")


def require_every_role_reachable(rules: Mapping[str, Any], roles: Iterable[str]) -> None:
    """Every role must be reachable from the anchor by join keys.

    ``cycle_rulesets.validate`` enforces this too, and that copy fires at commit
    — after the model turn has succeeded and outside any repair loop, so it
    costs the whole run. It cost one: a treasuryfull proposal declared
    ``broker_ack`` and wrote no join key that reaches it. Asked here, it costs
    one turn, and the repair has two honest ways out — add the join key, or say
    the role is ``unreachable`` and why.
    """

    names = set(roles)
    anchor = str((rules.get("anchor") or {}).get("role") or "")
    if not names or anchor not in names:
        return
    reachable = {anchor}
    changed = True
    while changed:
        changed = False
        for key in rules.get("join_keys") or []:
            left = str((key.get("left") or {}).get("role") or "")
            right = str((key.get("right") or {}).get("role") or "")
            for source, target in ((left, right), (right, left)):
                if source in reachable and target in names and target not in reachable:
                    reachable.add(target)
                    changed = True
    stranded = sorted(names - reachable)
    if stranded:
        raise WorkerResponseValidationError([
            f"No join key reaches role '{name}' from the anchor role "
            f"'{anchor}'. Give it a join key on an identifier field it shares "
            "with a role already reachable, or list it under rules.unreachable "
            "with the reason no identifier field can reach it."
            for name in stranded
        ])


# --------------------------------------------------------------------------- #
# field pairs
# --------------------------------------------------------------------------- #
def type_positions(document_types_held: Sequence[str]) -> list[dict]:
    """One provisional position per document type, named by the type.

    The field pairs are proposed before the turn that places the roles has run,
    so they are proposed between *types*: every pair of fields across two
    document types that share a value type and a name or a word. Once the
    shape is known, ``bind_pairs`` puts each side on the role its type fills.
    """

    return [{"name": kind, "document_type": kind} for kind in document_types_held]


def bind_pairs(
    pairs: Iterable[Mapping[str, Any]], roles: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """Move pairs proposed between types onto the roles the cycle declared.

    One role per type — the last in cycle order, the record in its final state,
    which is the rule ``cycle_field_pairs.positions`` has always applied. A pair
    on a type no role holds pairs nothing and is dropped. The id is kept, so a
    decline written against it still finds it.
    """

    by_type = {kind: role for role, kind in cycle_field_pairs.positions(roles)}
    bound = []
    for pair in pairs:
        left = dict(pair.get("left") or {})
        right = dict(pair.get("right") or {})
        left_role = by_type.get(str(left.get("role") or ""))
        right_role = by_type.get(str(right.get("role") or ""))
        if not left_role or not right_role or left_role == right_role:
            continue
        bound.append({
            **dict(pair),
            "left": {**left, "role": left_role},
            "right": {**right, "role": right_role},
        })
    return bound


def apply_field_pairs(
    rules: Mapping[str, Any],
    *,
    pairs: Sequence[Mapping[str, Any]],
    supplied_ids: set[str],
    schemas: list[Mapping[str, Any]],
    roles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Make every field pair the review kept an assertion, and record the rest.

    Silence keeps a pair. That is the property the floor exists for: a review
    that ignores the list still leaves the comparison in the rules, and cycle
    vouching evaluates every rule on every item. A pair leaves the rules only by
    being declined, with a reason, and the decline is kept on the ruleset where
    whoever approves it can disagree.

    An assertion the turn wrote over a candidate's two fields is that pair kept
    in its own words: it replaces the candidate.

    Refused here, where a repair can still fix it: a decline naming no
    candidate or giving no reason; a pair both declined and written; an
    authored rule taking a candidate's id; an authored assertion over a join
    key's own pair, which restates the link and cannot fail; and a cycle left
    with no assertion at all.

    ``pairs`` are already bound to roles; ``supplied_ids`` is every pair the
    turn was shown, including those on a type no role holds or a role listed
    unreachable — declining one of those is the right answer to a pair that
    cannot be bound, so it is accepted, and dropped with it.
    """

    names = {str(role.get("name") or "") for role in roles}
    pairs = [
        pair for pair in pairs
        if pair["left"]["role"] in names and pair["right"]["role"] in names
    ]
    by_id = {pair["id"]: pair for pair in pairs}
    problems: list[str] = []

    declined: dict[str, str] = {}
    for item in rules.get("declined_pairs") or []:
        pair_id = str(item.get("id") or "")
        if pair_id in supplied_ids and pair_id not in by_id:
            continue
        if pair_id not in by_id:
            problems.append(
                f"declined_pairs names '{pair_id or 'nothing'}', which is not one "
                "of the field pairs supplied. Decline a pair by the id it was "
                "given."
            )
        elif not str(item.get("reason") or "").strip():
            problems.append(
                f"Field pair '{pair_id}' is declined with no reason. Say why the "
                "two fields do not state the same value."
            )
        else:
            declined[pair_id] = str(item.get("reason")).strip()

    authored = [dict(item) for item in rules.get("assertions") or []]
    pair_ids = {cycle_field_pairs.signature(pair): pair["id"] for pair in pairs}
    joined = {cycle_field_pairs.signature(key) for key in rules.get("join_keys") or []}
    replaced: dict[str, str] = {}
    for item in authored:
        signature = cycle_field_pairs.signature(item)
        own_id = str(item.get("id") or "")
        if own_id in by_id and pair_ids.get(signature) != own_id:
            problems.append(
                f"Assertion '{own_id}' uses the id of a supplied field pair but "
                "compares different fields. Give it its own id, or compare the "
                "pair's two fields."
            )
        elif signature in pair_ids:
            pair_id = pair_ids[signature]
            if pair_id in declined:
                problems.append(
                    f"Field pair '{pair_id}' is declined, and assertion "
                    f"'{item.get('id')}' compares the same two fields. Keep one "
                    "answer: remove the assertion, or withdraw the decline."
                )
            replaced[pair_id] = own_id
        elif item.get("right") is not None and signature in joined:
            problems.append(
                f"Assertion '{item.get('id')}' compares the two fields a join key "
                "already links. The join made them equal for every linked "
                "record, so the rule cannot fail: remove it, and test what the "
                "linked records state instead."
            )

    kept = [
        cycle_field_pairs.as_assertion(pair, schemas, roles)
        for pair in pairs
        if pair["id"] not in declined and pair["id"] not in replaced
    ]
    assertions = authored + kept
    if not assertions and not problems:
        problems.append(
            "The rules must leave at least one assertion; a cycle whose linked "
            "documents are never compared tests nothing."
        )
    if problems:
        raise WorkerResponseValidationError(problems)

    result = dict(rules)
    result["assertions"] = assertions
    result["declined_pairs"] = [
        {
            "id": pair["id"],
            "left": dict(pair["left"]),
            "right": dict(pair["right"]),
            "basis": pair["basis"],
            "reason": declined[pair["id"]],
        }
        for pair in pairs
        if pair["id"] in declined
    ]
    return result


# --------------------------------------------------------------------------- #
# coverage: which approved assertions answer one requirement
# --------------------------------------------------------------------------- #
def reach_problems(
    target: tuple[str, str],
    entry: Mapping[str, Any],
    named: list[str],
    *,
    assertion_roles: Mapping[str, set[str]],
    known_roles: set[str],
    unreachable_roles: set[str],
) -> list[str]:
    """Hold one coverage entry to the roles it says its requirement is about.

    Naming an assertion that exists proves only that it exists. A procurement
    proposal answered "the invoice is matched to the purchase order and the
    goods receipt" with a rule that the payment voucher is signed, and a
    treasury one answered "matched on counterparty, principal, rate, value
    date and maturity date" with the rate alone. Both validated, both were
    approved, and the first left a short delivery that only the receipt states
    untested.

    So the entry declares ``reads`` — every role whose records the requirement
    is about, in the cycle's own role names — and is then held to it, in two
    directions. Every declared role is read by at least one of its assertions,
    or is stated as a gap with the reason no pair of its fields can express
    what is asked of it; a role no join key can reach is already a stated gap.
    And every assertion it names reads at least one declared role, because a
    rule over records the requirement is not about cannot answer it.

    Nothing here counts roles or asks for a comparison: a requirement can be
    about one record, and a field that must merely be stated is a one-sided
    assertion on that record. What it refuses is a declaration the named
    rules do not live up to — a structural check over role names, the same in
    any cycle. The declaration itself can still leave a document out, which is
    what ``named_document_problems`` answers from the requirement's words.
    """

    label = f"Requirement {target[0]}/{target[1]}"
    reads = name_list(entry.get("reads"), "reads")
    if not reads:
        return [
            f"{label} declares no roles in reads. Name every role whose records "
            "this requirement is about, using the cycle's role names."
        ]
    problems: list[str] = []
    unknown = [role for role in reads if role not in known_roles]
    if unknown:
        problems.append(
            f"{label} reads '{unknown[0]}', which is not a role of this "
            f"proposal; its roles are: {', '.join(sorted(known_roles))}."
        )
    gap_roles: set[str] = set()
    for gap in entry.get("gaps") or []:
        if not isinstance(gap, Mapping):
            continue
        role = str(gap.get("role") or "").strip()
        if not str(gap.get("reason") or "").strip():
            problems.append(
                f"{label} states a gap{f' on role {role!r}' if role else ''} "
                "with no reason. Say what is asked of those records that no "
                "pair of their fields can express."
            )
        if role and role not in reads:
            problems.append(
                f"{label} states a gap on role '{role}', which it does not "
                "list in reads."
            )
        if role:
            gap_roles.add(role)
    read_by_rules = set().union(*(assertion_roles.get(value, set()) for value in named))
    unread = [
        role
        for role in reads
        if role not in unknown
        and role not in read_by_rules
        and role not in unreachable_roles
        and role not in gap_roles
    ]
    if unread:
        listed = ", ".join(f"'{role}'" for role in unread)
        problems.append(
            f"{label} is about {listed}, and none of its assertions "
            f"({', '.join(named)}) reads "
            f"{'it' if len(unread) == 1 else 'them'}. For each, add an "
            "assertion over the field that must agree or be stated, or list "
            "the role under gaps with the reason no field of it can express "
            "what the requirement asks. A join key that reaches a record "
            "shows that the record exists, not that what it states agrees."
        )
    wanted = set(reads)
    for value in named:
        if assertion_roles.get(value, set()) & wanted:
            continue
        problems.append(
            f"{label} names assertion '{value}', which reads "
            f"{', '.join(sorted(assertion_roles.get(value) or {'nothing'}))} "
            f"and none of the roles it is about ({', '.join(reads)}). Remove "
            "it from this entry, or add the role to reads if the requirement "
            "is about it."
        )
    return problems


def named_document_problems(
    target: tuple[str, str],
    requirement: str,
    entry: Mapping[str, Any],
    named: list[str],
    *,
    assertion_roles: Mapping[str, set[str]],
    role_types: Mapping[str, str],
    unreachable_roles: set[str],
) -> list[str]:
    """Hold an entry to every document its requirement names, from the words.

    ``reads`` is the entry's own account of what the requirement is about, and
    the reach check above holds the entry only to that account. Procurement's
    three-way match — "the invoice is matched to the purchase order and the
    goods receipt note" — was answered reading the invoice alone: two checks
    that it cites a PO and a GRN, with ``reads`` naming the invoice and nothing
    else. It validated, and the matrix recorded a matching control as tested by
    two references that agree with nothing.

    So the documents are taken from the requirement as well. Each cycle
    document type the sentence names (by label, alias or head noun) must have
    one of its roles read by a named assertion, stated as a gap, or be
    unreachable. A name read where the sentence meant something else costs a
    gap with a reason, which the approver sees; a document left out costs the
    control's test.
    """

    by_type: dict[str, list[str]] = {}
    for role, document_type in role_types.items():
        if document_type:
            by_type.setdefault(document_type, []).append(role)
    gap_roles = {
        str(gap.get("role") or "").strip()
        for gap in entry.get("gaps") or []
        if isinstance(gap, Mapping)
    }
    read = set().union(*(assertion_roles.get(value, set()) for value in named))
    problems: list[str] = []
    for document_type in document_types.mentioned(requirement, by_type):
        roles = by_type[document_type]
        if any(role in read or role in gap_roles or role in unreachable_roles for role in roles):
            continue
        label = document_types.label(document_type)
        listed = ", ".join(f"'{role}'" for role in roles)
        problems.append(
            f"Requirement {target[0]}/{target[1]} names the {label.lower()} "
            f"(roles {listed}), and none of its assertions reads it. Add the "
            "role to reads, and either an assertion comparing what that record "
            "states with the records it is matched to, or a gap with the reason "
            "no field of it can express what the requirement asks. A check that "
            "another document cites it shows only that they refer to each other."
        )
    return problems




def assertion_roles(assertions: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """The roles each assertion reads. A one-sided assertion reads one role."""

    return {
        str(item.get("id") or ""): {
            str((item.get(side) or {}).get("role") or "")
            for side in ("left", "right")
            if isinstance(item.get(side), Mapping)
        }
        for item in assertions
        if isinstance(item, Mapping)
    }


def coverage_problems(
    target: tuple[str, str],
    requirement: str,
    entry: Mapping[str, Any],
    *,
    assertions: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
    unreachable_roles: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Everything wrong with one requirement's answer, against approved rules.

    The same three checks the linkage turn ran when it answered every
    requirement at once: the assertions it names exist; every role it declares
    in ``reads`` is read by one of them, stated as a gap, or unreachable, and
    every one of them reads a declared role; and every document the
    requirement's own words name is read. Run now by the turn writing one row's
    tests, against rules that were approved before the row's words were read —
    so where no approved assertion reads a document the requirement needs, the
    honest answer is a gap with its reason, and the approver of the next
    ruleset sees it.
    """

    named = cycle_rulesets.coverage_assertion_ids(entry)
    label = f"Requirement {target[0]}:{target[1]}"
    if not named:
        return [
            f"{label} names no assertion. List in assertion_ids every approved "
            "assertion it needs, or report the requirement as unsupported."
        ]
    by_id = {str(item.get("id") or ""): item for item in assertions}
    missing = [value for value in named if value not in by_id]
    if missing:
        return [
            f"{label} names assertion '{missing[0]}', which the approved cycle "
            f"rules do not hold; choose from: {', '.join(sorted(by_id)) or 'none'}."
        ]
    reading = assertion_roles(assertions)
    known = set(roles) | set(unreachable_roles)
    return [
        *reach_problems(
            target,
            entry,
            named,
            assertion_roles=reading,
            known_roles=known,
            unreachable_roles=set(unreachable_roles),
        ),
        *named_document_problems(
            target,
            requirement,
            entry,
            named,
            assertion_roles=reading,
            role_types={role: kind for role, kind in roles.items() if role in known},
            unreachable_roles=set(unreachable_roles),
        ),
    ]


def derived_comparison(
    assertion: Mapping[str, Any], roles: Mapping[str, Mapping[str, Any]]
) -> dict | None:
    """One matrix comparison, read off the assertion that answers a requirement.

    The matrix says what must be shown; the assertion says which fields show
    it; this restates the second in the vocabulary the first is stored in. The
    translation is the role: a rule names a *position* in the cycle, and a row
    names the document type filling it.
    """

    def side(value: object) -> dict | None:
        if not isinstance(value, Mapping):
            return None
        role = roles.get(str(value.get("role") or ""))
        document_type = str((role or {}).get("document_type") or "")
        field = str(value.get("field") or "")
        if not document_type or not field:
            return None
        return {"document_type": document_type, "field": field}

    left = side(assertion.get("left"))
    if left is None:
        return None
    raw_right = assertion.get("right")
    right = None
    if raw_right is not None:
        right = side(raw_right)
        if right is None:
            return None
    return {
        "key": str(assertion.get("id") or ""),
        "left": left,
        "right": right,
        "rationale": str(assertion.get("requirement") or assertion.get("rationale") or ""),
    }
