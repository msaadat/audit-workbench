"""Durable Data Tests and their current execution result.

Definitions live in ``workspace.json`` so they participate in engagement
migration and reconciliation. Each test owns one replaceable result under
``DataTestResults``; the workspace stores only its latest-run metadata. A test
may be exploratory or linked to one RCM row. Creating or editing a definition
never counts as execution.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from . import analytics, exception_profile, explore, sandbox, validation
from .text import counted, plural_word
from .agent import joins as join_diagnostics
from .workspaces import (
    CONTROL_CONCLUSIONS,
    TEST_STATUSES,
    Workspace,
    WorkspaceError,
    slugify,
    write_json_atomic,
)

ENGINES = {"analytics", "validation", "polars"}
STATUSES = TEST_STATUSES
CURRENT_RESULT_ID = "DTR-CURRENT"
SUMMARY_ROWS = 500
EXCEPTION_ROWS = 200

# What the run found and what somebody decided about it are two separate facts.
# ``status`` survives as the joint projection of the two so every existing
# rollup, dashboard, and report reader carries on working, but these are the
# vocabularies that actually get written.
EVALUATION_STATES = {"not_run", "passed", "failed", "inconclusive"}
DISPOSITION_STATES = {"pending", "accepted", "exception", "needs_review"}
# Who concluded. An auto run concludes without an auditor, which is the point of
# auto mode — but the file has to say that is what happened, and the auditor has
# to be able to win. Both need the conclusion to carry its author.
CONCLUSION_SOURCES = {"none", "agent", "auditor"}
# Where the exception frame cannot be attributed to named conditions, the whole
# frame is still one thing an auditor can rule on.
ALL_EXCEPTIONS = "All exceptions"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _step_id(semantic_id: str, label: str, ordinal: int) -> str:
    return f"STEP-{_sha1([semantic_id, label, ordinal])[:10].upper()}"


def _normalize_steps(values: object) -> list[dict]:
    """Preserve declared step fields as objects; reject a step that is not an object."""
    steps: list[dict] = []
    for value in values or []:
        if not value:
            continue
        if not isinstance(value, dict):
            raise WorkspaceError("Each test step must be an object.")
        steps.append(dict(value))
    return steps


def new_evaluation(
    state: str = "not_run",
    note: str = "",
    *,
    exception_count: int = 0,
    reasons: list[dict] | None = None,
    suggested_control_conclusion: str = "no_conclusion",
    input_sha1: str | None = None,
    ran_at: str | None = None,
) -> dict:
    """What the run found, and against which definition and data it found it."""
    if state not in EVALUATION_STATES:
        raise WorkspaceError("Unknown Data Test evaluation state.")
    if suggested_control_conclusion not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown control conclusion.")
    return {
        "state": state,
        "note": str(note or ""),
        "exception_count": max(0, int(exception_count or 0)),
        # The inventory of dispositionable groups, carried on the record so the
        # rollup, the dashboard, and the completion gate never have to open the
        # result document to know what is outstanding.
        "reasons": [dict(reason) for reason in reasons or []],
        # What the run reads as, offered to whoever concludes. Never a
        # conclusion in itself: nothing signs the file by running.
        "suggested_control_conclusion": suggested_control_conclusion,
        "input_sha1": input_sha1,
        "ran_at": ran_at,
    }


def new_disposition(
    key: str,
    state: str = "pending",
    note: str = "",
    *,
    rows: int = 0,
    records: int = 0,
    actor: str | None = None,
    source: str = "none",
    at: str | None = None,
    evaluated_input_sha1: str | None = None,
    stale: bool = False,
) -> dict:
    """One ruling on one group of exceptions: what, by whom, against what."""
    if state not in DISPOSITION_STATES:
        raise WorkspaceError("Unknown Data Test disposition state.")
    if source not in CONCLUSION_SOURCES:
        raise WorkspaceError("Unknown Data Test disposition source.")
    return {
        "scope": "reason",
        "key": str(key),
        "state": state,
        "note": str(note or ""),
        # What the group covered when the ruling was made. Kept so a stale
        # ruling can still say how much it once spoke for.
        "rows": max(0, int(rows or 0)),
        "records": max(0, int(records or 0)),
        "actor": actor,
        "source": source,
        "at": at,
        "evaluated_input_sha1": evaluated_input_sha1,
        "stale": bool(stale),
    }


def evaluation_input_sha1(source_sha1: str, dataset_fingerprints: dict) -> str:
    """Hash the definition and the data one run consumed.

    Deliberately not the result hash: ``run_at`` is inside that, so it changes
    on every run and would mark every ruling stale for no reason. The definition
    and the tables are the two things whose change actually invalidates a
    ruling.
    """
    return _sha1({"source": source_sha1, "data": dataset_fingerprints})


# What a finding actually rests on when it cites a result: the definition and
# data the run consumed, and the outcome it produced. The same reasoning as
# ``evaluation_input_sha1`` above, applied to evidence anchors — ``run_at``,
# presentation (``viz``, ``stdout``, ``statistics``, ``verdict_text``), runner
# diagnostics, and the auditor's own conclusion are all excluded, because a
# re-run that changed nothing must not read as "the evidence changed". An
# allowlist rather than a denylist, so a new presentational field cannot
# silently start invalidating evidence.
_RESULT_EVIDENCE_FIELDS = (
    "data_test_id",
    "rcm_id",
    "source_sha1",
    "dataset_fingerprints",
    "status",
    "verdict",
    "exception_count",
    "exception_frame",
    "summary_frame",
    "semantic_valid",
)


def result_evidence_projection(result: dict) -> dict:
    """Return the evidentiary basis of one Data Test result."""
    return {field: result.get(field) for field in _RESULT_EVIDENCE_FIELDS}


def result_evidence_sha1(result: dict) -> str:
    return _sha1(result_evidence_projection(result))


def reason_inventory(exception_profile: dict | None, exception_count: int) -> list[dict]:
    """The exception groups an auditor can rule on, one row each.

    Only the Polars engine reconstructs named conditions. Everything else — and
    any frame whose predicate could not be attributed — still has to be
    rulable, so its exceptions stand as one group rather than none.
    """
    reasons = (exception_profile or {}).get("reasons") or []
    if reasons:
        return [
            {
                "label": str(reason.get("label") or ALL_EXCEPTIONS),
                "rows": max(0, int(reason.get("rows") or 0)),
                "records": max(0, int(reason.get("records") or 0)),
            }
            for reason in reasons
        ]
    if exception_count > 0:
        return [
            {"label": ALL_EXCEPTIONS, "rows": exception_count, "records": exception_count}
        ]
    return []


def current_dispositions(item: dict) -> list[dict]:
    """Rulings that still stand: decided, and made against the current inputs."""
    return [
        disposition
        for disposition in item.get("exception_dispositions") or []
        if disposition.get("state") != "pending" and not disposition.get("stale")
    ]


def open_exception_count(item: dict) -> int:
    """Exceptions that still stand against the control.

    Accepting a group is what retires its exceptions; everything else — ruled an
    exception, flagged for review, or never looked at — stays open. Counting the
    residual rather than the ruled groups keeps this honest when the reasons do
    not partition the frame exactly.
    """
    evaluation = item.get("evaluation") or {}
    total = max(0, int(evaluation.get("exception_count") or 0))
    accepted = sum(
        disposition["rows"]
        for disposition in current_dispositions(item)
        if disposition["state"] == "accepted"
    )
    return max(0, total - accepted)


def project_status(item: dict) -> str:
    """Read one test jointly: the ruling where there is one, else the run.

    A ruling wins because somebody looked; that is the whole point of having a
    disposition layer. What the run found is not erased by it — ``evaluation``
    and ``exception_count`` both stay on the record — so a working paper can
    still say how many exceptions were found alongside how many still stand.
    """
    if not item.get("engine") or str(item.get("status") or "") == "draft":
        return "draft"
    if (
        str(item.get("control_conclusion") or "") == "not_applicable"
        and str(item.get("control_conclusion_source") or "none") != "none"
    ):
        # Retiring a test is a conclusion about the control, not a run outcome.
        return "not_applicable"
    evaluation = item.get("evaluation") or {}
    state = str(evaluation.get("state") or "not_run")
    if state == "not_run":
        return "ready"
    standing = current_dispositions(item)
    if any(disposition["state"] == "needs_review" for disposition in standing):
        return "review_required"
    if state == "inconclusive" and not (item.get("semantic_review") or {}).get("at"):
        # A run that could not execute produced no evidence at all, which is a
        # different thing from evidence a warning qualifies: there is nothing to
        # conclude over until somebody says why the failure does not matter.
        return "review_required"
    if state == "passed":
        return "completed_no_exception"
    if any(disposition["state"] == "exception" for disposition in standing):
        return "completed_with_exception"
    return (
        "completed_no_exception"
        if open_exception_count(item) == 0
        else "completed_with_exception"
    )


def result_stale(workspace: Workspace, item: dict) -> bool:
    """Whether the stored result no longer describes the current basis.

    Distinct from a stale *ruling*, which compares a decision against the run it
    was made on. This compares the run against the workspace as it stands now,
    so evidence moving under a test surfaces before anyone re-runs it rather
    than only afterwards.
    """
    last_run = item.get("last_run")
    if not last_run:
        return False
    refs = _execution_table_refs(workspace, item)
    return bool(
        last_run.get("source_sha1")
        != _sha1({"engine": item.get("engine"), "table_refs": refs, "spec": item.get("spec") or {}})
        or last_run.get("dataset_fingerprints") != _dataset_fingerprints(workspace, refs)
    )


def _evaluation_from_last_run(last_run: dict) -> dict:
    """Read the evaluation off a record that predates the field.

    Everything it needs is already on ``last_run`` — what the run concluded,
    how many exceptions it found, and the definition and data it read. Only the
    reason breakdown is unrecoverable, so those exceptions stand as one group
    until the test is run again.
    """
    status = str(last_run.get("status") or "")
    exceptions = int(last_run.get("exception_count") or 0)
    return new_evaluation(
        _RESULT_EVALUATION_STATES.get(status, "inconclusive"),
        exception_count=exceptions,
        reasons=reason_inventory(None, exceptions),
        suggested_control_conclusion=(
            "ineffective"
            if status == "completed_with_exception"
            else "effective"
            if status == "completed_no_exception"
            else "no_conclusion"
        ),
        input_sha1=evaluation_input_sha1(
            str(last_run.get("source_sha1") or ""),
            dict(last_run.get("dataset_fingerprints") or {}),
        ),
        ran_at=str(last_run.get("run_at") or "") or None,
    )


def _normalize_marking(item: dict) -> None:
    """Validate one test's run verdict and rulings, then re-project its status."""
    evaluation = item.get("evaluation") or {}
    if not evaluation and item.get("last_run"):
        evaluation = _evaluation_from_last_run(item["last_run"])
    item["evaluation"] = new_evaluation(
        str(evaluation.get("state") or "not_run"),
        str(evaluation.get("note") or ""),
        exception_count=evaluation.get("exception_count") or 0,
        reasons=evaluation.get("reasons") or [],
        suggested_control_conclusion=str(
            evaluation.get("suggested_control_conclusion") or "no_conclusion"
        ),
        input_sha1=evaluation.get("input_sha1"),
        ran_at=evaluation.get("ran_at"),
    )
    current = item["evaluation"]["input_sha1"]
    dispositions = []
    for raw in item.get("exception_dispositions") or []:
        signed = raw.get("evaluated_input_sha1")
        dispositions.append(
            new_disposition(
                str(raw.get("key") or ALL_EXCEPTIONS),
                str(raw.get("state") or "pending"),
                str(raw.get("note") or ""),
                rows=raw.get("rows") or 0,
                records=raw.get("records") or 0,
                actor=raw.get("actor"),
                source=str(raw.get("source") or "none"),
                at=raw.get("at"),
                evaluated_input_sha1=signed,
                # A ruling made against inputs that have since changed stays on
                # the record — somebody did decide this — but stops counting.
                # A retired run counts as a change: there is nothing left for
                # the ruling to be current against.
                stale=bool(
                    str(raw.get("state") or "pending") != "pending"
                    and signed is not None
                    and signed != current
                ),
            )
        )
    item["exception_dispositions"] = dispositions
    for key in ("conclusion_source", "control_conclusion_source"):
        if str(item.get(key) or "none") not in CONCLUSION_SOURCES:
            raise WorkspaceError("Unknown Data Test conclusion source.")
        item[key] = str(item.get(key) or "none")
    if str(item.get("control_conclusion") or "no_conclusion") not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown control conclusion.")
    signed = item.get("control_conclusion_input_sha1")
    item["control_conclusion_stale"] = bool(
        item["control_conclusion_source"] != "none"
        and signed is not None
        and signed != current
    )
    item["open_exception_count"] = open_exception_count(item)
    item["status"] = project_status(item)


def _invalidate_evaluation(item: dict, reason: str) -> None:
    """Retire a run whose definition just changed.

    Rulings made against it are left in place for :func:`_normalize_marking` to
    mark stale rather than dropped: that somebody decided remains part of the
    record, it just stops counting as current.
    """
    item["evaluation"] = new_evaluation("not_run", reason)


def results_dir(workspace: Workspace, data_test_id: str | None = None) -> Path:
    path = workspace.root / "DataTestResults"
    if data_test_id:
        path = path / data_test_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _result_path(workspace: Workspace, data_test_id: str, run_id: str) -> Path:
    if not data_test_id.startswith("DAT-") or not run_id.startswith("DTR-"):
        raise WorkspaceError("Invalid Data Test result reference.")
    return results_dir(workspace, data_test_id) / f"{run_id}.json"


def _finding_result_ids(workspace: Workspace, data_test_id: str) -> set[str]:
    """Legacy result IDs that must remain available as finding evidence."""
    ids: set[str] = set()
    for finding in workspace.findings:
        for anchor in finding.get("evidence_refs") or []:
            if not isinstance(anchor, dict) or anchor.get("source_kind") != "datatest":
                continue
            test_id, separator, result_id = str(anchor.get("source_id") or "").partition(":")
            if separator and test_id == data_test_id and result_id:
                ids.add(result_id)
    return ids


def _discard_superseded_results(workspace: Workspace, data_test_id: str) -> None:
    """Remove prior non-evidence results after the current result is committed."""
    retained = _finding_result_ids(workspace, data_test_id)
    folder = workspace.root / "DataTestResults" / data_test_id
    if not folder.is_dir():
        return
    for path in folder.glob("DTR-*.json"):
        if path.stem != CURRENT_RESULT_ID and path.stem not in retained:
            path.unlink()


def _record(workspace: Workspace, data_test_id: str) -> dict:
    item = next(
        (value for value in workspace.data_tests if value.get("id") == data_test_id),
        None,
    )
    if item is None:
        raise WorkspaceError(f"Data Test '{data_test_id}' not found.")
    # Execution history was retired. Old workspaces can still carry this field
    # until their next mutation, but it is never returned or persisted again.
    item.pop("runs", None)
    item.setdefault("last_run", None)
    item.setdefault("evidence_refs", [])
    item.setdefault("criteria", "")
    item.setdefault("steps", [])
    item.setdefault("methodology_refs", [])
    item.setdefault("conclusion", "")
    item.setdefault("control_conclusion", "no_conclusion")
    item.setdefault("conclusion_source", "none")
    item.setdefault("control_conclusion_source", "none")
    item.setdefault("control_conclusion_input_sha1", None)
    item.setdefault("evaluation", new_evaluation())
    item.setdefault("exception_dispositions", [])
    item.setdefault("semantic_review", None)
    item.setdefault("result_summary", "")
    item.setdefault("scope_limitations", "")
    item.setdefault("next_action", "")
    item.setdefault("exception_count", 0)
    item.setdefault("open_exception_count", 0)
    item.setdefault("finding_refs", [])
    _normalize_marking(item)
    return item


def _validate_rcm_id(workspace: Workspace, rcm_id: object) -> str | None:
    """Resolve the optional RCM row a test covers.

    Leaving it blank is exploration; an exploratory result is a durable analysis
    artifact but never RCM coverage.
    """
    value = str(rcm_id or "").strip() or None
    if value and not any(row.get("id") == value for row in workspace.rcm):
        raise WorkspaceError(f"RCM row '{value}' not found.")
    return value


def _table_refs(workspace: Workspace, values: object, *, required: bool = True) -> list[str]:
    refs = list(dict.fromkeys(str(value).strip() for value in (values or []) if str(value).strip()))
    if not refs and required:
        raise WorkspaceError("A Data Test needs at least one table.")
    missing = next((value for value in refs if value not in workspace.table_names()), None)
    if missing:
        raise WorkspaceError(f"Unknown table '{missing}'.")
    return refs


def _validate_spec(
    workspace: Workspace, engine: str, refs: list[str], spec: object, *, semantic_id: str = ""
) -> tuple[dict, list[str], list[str]]:
    value = dict(spec or {})
    warnings: list[str] = []
    if engine == "analytics":
        test_id = analytics.canonical_test_id(value.get("test_id"))
        if test_id not in analytics.ANALYTICS:
            raise WorkspaceError(f"Unknown analytics test '{test_id}'.")
        try:
            params = analytics.canonicalize_params(
                workspace.get_frame(refs[0]),
                test_id,
                dict(value.get("params") or {}),
                source=workspace.frame_source(),
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        value = {**value, "test_id": test_id, "params": params}
        if test_id == "rare_values":
            column = str(params.get("column") or "")
            frame = workspace.get_frame(refs[0])
            if column and frame.height and frame[column].n_unique() / frame.height > 0.9:
                warnings.append("Rare-value screening targets a naturally unique field.")
    elif engine == "validation":
        rules = value.get("rules") or []
        if not isinstance(rules, list) or not rules:
            raise WorkspaceError("A validation Data Test needs at least one rule.")
        frame = workspace.get_frame(refs[0])
        try:
            rules = validation.canonicalize_rules(
                frame, rules, resolve=workspace.get_frame, strict=True
            )
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        value = {**value, "rules": rules}
        warnings.extend(validation.generated_rule_issues(frame, rules, workspace.get_frame))
    else:
        if "code" in value:
            raise WorkspaceError("A Polars Data Test uses spec.steps, not a single spec.code.")
        if value.get("schema_version") != 2:
            raise WorkspaceError("A Polars Data Test needs spec.schema_version 2.")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise WorkspaceError("A Polars Data Test needs at least one step.")
        steps: list[dict] = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, dict):
                raise WorkspaceError("Each Polars Data Test step must be an object.")
            label = str(raw_step.get("label") or "").strip()
            instruction = str(raw_step.get("instruction") or "").strip()
            if not label:
                raise WorkspaceError(f"Data Test step {index + 1} needs a label.")
            if not instruction:
                raise WorkspaceError(f"Data Test step '{label}' needs an instruction.")
            code = str(raw_step.get("code") or "").strip()
            if not code:
                raise WorkspaceError(f"Data Test step '{label}' needs code.")
            try:
                sandbox.validate(code)
            except ValueError as error:
                raise WorkspaceError(str(error)) from error
            step_id = str(raw_step.get("step_id") or "").strip() or _step_id(semantic_id, label, index)
            step = {
                "step_id": step_id,
                "label": label,
                "instruction": instruction,
                "code": code,
            }
            # Which population the step makes a statement about. Optional so an
            # auditor-authored step stays valid, but durable where supplied:
            # coverage reconciliation after fieldwork has no other way to know
            # which populations the executed tests actually spoke about.
            population = str(raw_step.get("population") or "").strip()
            if population:
                if population not in workspace.table_names():
                    raise WorkspaceError(
                        f"Data Test step '{label}' names unknown population "
                        f"'{population}'."
                    )
                step["population"] = population
            steps.append(step)
        value = {"schema_version": 2, "steps": steps}
    return value, warnings, refs


def _link(workspace: Workspace, item: dict) -> None:
    """Keep ``rcm[].test_refs`` in step with this test's RCM link."""
    ref = f"datatest:{item['id']}"
    for row in workspace.rcm:
        refs = row.setdefault("test_refs", [])
        if row.get("id") == item.get("rcm_id"):
            if ref not in refs:
                refs.append(ref)
        elif ref in refs:
            row["test_refs"] = [value for value in refs if value != ref]


def _base_record(workspace: Workspace, payload: dict, *, title: str, now: str) -> dict:
    item_id = str(payload.get("id") or f"DAT-{uuid.uuid4().hex[:10].upper()}")
    semantic_id = str(payload.get("semantic_id") or f"datatest:{slugify(title)}")
    if any(
        value.get("id") == item_id or value.get("semantic_id") == semantic_id
        for value in workspace.data_tests
    ):
        raise WorkspaceError("A Data Test with that ID or semantic ID already exists.")
    return {
        "id": item_id,
        "semantic_id": semantic_id,
        "rcm_id": _validate_rcm_id(workspace, payload.get("rcm_id")),
        "title": title,
        "objective": str(payload.get("objective") or "").strip(),
        # Audit plan — the fields that used to live on the RCM planned test.
        "criteria": str(payload.get("criteria") or ""),
        "steps": _normalize_steps(payload.get("steps")),
        "methodology_refs": list(payload.get("methodology_refs") or []),
        # Outcome. What the run found lives in ``evaluation``; what somebody
        # decided about it lives in the conclusion fields and the dispositions.
        "conclusion": "",
        "conclusion_source": "none",
        "control_conclusion": "no_conclusion",
        "control_conclusion_source": "none",
        "control_conclusion_input_sha1": None,
        "evaluation": new_evaluation(),
        "exception_dispositions": [],
        "semantic_review": None,
        "result_summary": "",
        "scope_limitations": "",
        "next_action": "",
        "exception_count": 0,
        "open_exception_count": 0,
        "finding_refs": [],
        "last_run": None,
        "evidence_refs": [],
        "created_by": "agent" if payload.get("agent_run_id") else "user",
        "agent_run_id": payload.get("agent_run_id"),
        "workflow_parent_sha1": str(payload.get("workflow_parent_sha1") or "") or None,
        # Set only on a test promoted from a saved analysis. It is what lets
        # the coverage assertion check that an exploratory procedure which
        # found exceptions actually became something the audit executes.
        "source_analysis_id": str(payload.get("source_analysis_id") or "") or None,
        "created": now,
        "updated": now,
    }


def create(workspace: Workspace, payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    engine = str(payload.get("engine") or "").strip().lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    semantic_id = str(payload.get("semantic_id") or f"datatest:{slugify(title)}")
    refs = [] if engine == "polars" else _table_refs(workspace, payload.get("table_refs"))
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, payload.get("spec"), semantic_id=semantic_id
    )
    item = {
        **_base_record(workspace, {**payload, "semantic_id": semantic_id}, title=title, now=utcnow()),
        "engine": engine,
        "table_refs": refs,
        "spec": spec,
        "status": "ready",
        "semantic_warnings": warnings,
    }
    workspace.data_tests.append(item)
    _link(workspace, item)
    workspace.save()
    return item


def create_draft(workspace: Workspace, payload: dict) -> dict:
    """Create a planned-but-unspecified Data Test.

    This is what the draft pass of test generation commits: the audit plan for
    one test, with no engine, tables, or code yet. :func:`apply_spec` fills those
    in and moves the record out of ``draft``.
    """
    title = str(payload.get("title") or "").strip()
    objective = str(payload.get("objective") or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    item = {
        **_base_record(workspace, payload, title=title, now=utcnow()),
        "engine": None,
        "table_refs": [],
        "spec": {},
        "status": "draft",
        "semantic_warnings": [],
    }
    workspace.data_tests.append(item)
    _link(workspace, item)
    workspace.save()
    return item


PLAN_FIELDS = (
    "title",
    "objective",
    "criteria",
    "steps",
    "methodology_refs",
)


def update_plan(workspace: Workspace, data_test_id: str, payload: dict) -> dict:
    """Rewrite one test's audit plan, leaving its spec and results untouched.

    This is what a re-run of the draft pass commits onto a test it has already
    created; the executable spec belongs to :func:`apply_spec`.
    """
    item = _record(workspace, data_test_id)
    for key in PLAN_FIELDS:
        if key not in payload:
            continue
        if key == "steps":
            item["steps"] = _normalize_steps(payload["steps"])
        elif key == "methodology_refs":
            item["methodology_refs"] = list(payload["methodology_refs"] or [])
        else:
            item[key] = str(payload[key] or "")
    if "rcm_id" in payload:
        item["rcm_id"] = _validate_rcm_id(workspace, payload["rcm_id"])
        _link(workspace, item)
    for key in ("agent_run_id", "workflow_parent_sha1"):
        if payload.get(key):
            item[key] = str(payload[key])
    item["updated"] = utcnow()
    workspace.save()
    return item


def apply_spec(workspace: Workspace, data_test_id: str, payload: dict) -> dict:
    """Write the executable spec onto an existing test.

    The current result is retained until the updated definition is run again.
    """
    item = _record(workspace, data_test_id)
    engine = str(payload.get("engine") or "polars").strip().lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    refs = [] if engine == "polars" else _table_refs(workspace, payload.get("table_refs"))
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, payload.get("spec"), semantic_id=item["semantic_id"]
    )
    item.update(
        engine=engine,
        table_refs=refs,
        spec=spec,
        semantic_warnings=warnings,
        status="ready",
        updated=utcnow(),
    )
    if payload.get("title"):
        item["title"] = str(payload["title"]).strip()
    if payload.get("workflow_parent_sha1"):
        item["workflow_parent_sha1"] = str(payload["workflow_parent_sha1"])
    if payload.get("agent_run_id"):
        item["agent_run_id"] = str(payload["agent_run_id"])
    workspace.save()
    return item


def update(
    workspace: Workspace,
    data_test_id: str,
    changes: dict,
    *,
    agent: bool = False,
) -> dict:
    item = _record(workspace, data_test_id)
    allowed = {
        "title", "objective", "engine", "table_refs", "spec", "rcm_id",
        "workflow_parent_sha1", "criteria", "steps",
        "methodology_refs", "conclusion", "control_conclusion",
        "scope_limitations", "next_action",
    }
    unknown = set(changes) - allowed
    if "workflow_parent_sha1" in changes and not agent:
        unknown.add("workflow_parent_sha1")
    if unknown:
        raise WorkspaceError(f"Unknown Data Test field: {sorted(unknown)[0]}.")
    title = str(changes.get("title", item["title"]) or "").strip()
    objective = str(changes.get("objective", item["objective"]) or "").strip()
    if not title or not objective:
        raise WorkspaceError("Data Test title and objective are required.")
    engine = str(changes.get("engine", item["engine"]) or "").lower()
    if engine not in ENGINES:
        raise WorkspaceError("Data Test engine must be analytics, validation, or polars.")
    rcm_id = _validate_rcm_id(workspace, changes.get("rcm_id", item["rcm_id"]))
    refs = (
        []
        if engine == "polars"
        else _table_refs(workspace, changes.get("table_refs", item["table_refs"]))
    )
    spec, warnings, refs = _validate_spec(
        workspace, engine, refs, changes.get("spec", item["spec"]), semantic_id=item["semantic_id"]
    )
    conclusion = str(
        changes.get("control_conclusion", item.get("control_conclusion") or "no_conclusion")
    )
    if conclusion not in CONTROL_CONCLUSIONS:
        raise WorkspaceError("Unknown control conclusion.")
    # Departing from the run is the judgement a working paper most wants to be
    # able to show, and the UI asks for it — but it is asked for, not demanded.
    # Refusing the enum change until prose exists stopped an auditor recording
    # what they had decided just because they had not yet written up why.
    item.update(
        title=title,
        objective=objective,
        engine=engine,
        rcm_id=rcm_id,
        table_refs=refs,
        spec=spec,
        semantic_warnings=warnings,
        control_conclusion=conclusion,
        criteria=str(changes.get("criteria", item.get("criteria") or "")),
        steps=_normalize_steps(changes.get("steps", item.get("steps"))),
        methodology_refs=list(changes.get("methodology_refs", item.get("methodology_refs")) or []),
        conclusion=str(changes.get("conclusion", item.get("conclusion") or "")),
        scope_limitations=str(
            changes.get("scope_limitations", item.get("scope_limitations") or "")
        ),
        next_action=str(changes.get("next_action", item.get("next_action") or "")),
        workflow_parent_sha1=str(
            changes.get("workflow_parent_sha1", item.get("workflow_parent_sha1")) or ""
        ) or None,
        updated=utcnow(),
    )
    if not agent and item.get("created_by") == "agent":
        item["created_by"] = "user"
    source = "agent" if agent else "auditor"
    if "control_conclusion" in changes:
        item["control_conclusion_source"] = source
        item["control_conclusion_input_sha1"] = item["evaluation"]["input_sha1"]
    if "conclusion" in changes:
        item["conclusion_source"] = source
    # A changed *definition* must be executed again; history remains immutable.
    # Editing the plan or recording an outcome is not a definition change, so it
    # must not discard the result the current run established.
    if any(key in changes for key in ("engine", "table_refs", "spec")):
        _invalidate_evaluation(
            item, "The definition changed after this run; run the test again."
        )
    _normalize_marking(item)
    _link(workspace, item)
    workspace.save()
    return item


def remove(workspace: Workspace, data_test_id: str) -> None:
    item = _record(workspace, data_test_id)
    workspace.data_tests.remove(item)
    ref = f"datatest:{data_test_id}"
    for row in workspace.rcm:
        row["test_refs"] = [value for value in row.get("test_refs", []) if value != ref]
    workspace.save()


def _dataset_fingerprints(workspace: Workspace, refs: list[str]) -> dict[str, str]:
    return {name: _sha1(workspace._table_signature(name)) for name in refs}


def _execution_table_refs(workspace: Workspace, item: dict) -> list[str]:
    """Return every frame available to a Polars test at execution time.

    A Polars definition deliberately carries no table selection. This keeps its
    sandbox environment in step with the workspace as tables and joins change,
    and makes the result provenance cover every frame that its code could read.
    """
    if item.get("engine") == "polars":
        return workspace.table_names()
    return list(item.get("table_refs") or [])


def _join_issues(workspace: Workspace, refs: list[str]) -> tuple[list[str], list[dict]]:
    issues: list[str] = []
    diagnostics: list[dict] = []
    for name in refs:
        join = next((value for value in workspace.joins if value.get("name") == name), None)
        if not join or join.get("how") == "cross" or len(join.get("left_on") or []) != 1:
            continue
        detail = join_diagnostics.diagnose(
            workspace.get_frame(join["left"]),
            workspace.get_frame(join["right"]),
            join["left_on"][0],
            join["right_on"][0],
        )
        diagnostics.append({"table": name, **detail})
        if detail["match_rate"] == 0:
            issues.append(f"Join '{name}' has 0% key match coverage.")
        elif detail["match_rate"] < join_diagnostics.GOOD_MATCH_RATE:
            issues.append(
                f"Join '{name}' key match coverage is only {detail['match_rate']:.1%}."
            )
        if detail["row_multiplication"] > join_diagnostics.MAX_ROW_MULTIPLICATION:
            issues.append(
                f"Join '{name}' multiplies rows by {detail['row_multiplication']:.2f}."
            )
    return issues, diagnostics


def _all_null_columns(frame: pl.DataFrame | None) -> list[str]:
    if frame is None or frame.height == 0:
        return []
    return [name for name in frame.columns if frame[name].null_count() == frame.height]


# A generated step is written against column names and dtypes only — the values a
# text column actually holds are withheld from the generating turn by design. A
# predicate over a guessed category literal therefore fails silently in one of two
# directions, and both are invisible to the schema-only validation the worker runs:
# it matches every row (reported as a total control failure) or none (reported as a
# clean control). ``SATURATION_RATIO`` is the share of the largest referenced table
# at or above which a step is treated as saturated rather than as evidence.
SATURATION_RATIO = 0.9
# Saturation is a statistical signal about a population, so it is only reported
# where there is a population to speak of. Below this, "every row is an
# exception" is as likely to be a correct result on a handful of rows.
MIN_SATURATION_POPULATION = 20
# A short vocabulary is a category column; a long one is an identifier or a free
# text field, where an absent literal says nothing about the predicate.
MAX_CATEGORY_CARDINALITY = 24
_COLUMN_REF_RE = re.compile(r"""pl\.col\(\s*['"]([^'"]+)['"]\s*\)""")
# Only a literal in *predicate* position says anything about whether a step can
# match. A column name is also a bare string in ``select``, ``join(on=...)`` and
# ``group_by``, so the comparison forms are matched explicitly rather than by
# reading every string in the snippet.
_COMPARISON_RE = re.compile(
    r"""pl\.col\(\s*['"](?P<column>[^'"]+)['"]\s*\)"""
    r"""(?P<chain>(?:\s*\.\s*str\s*\.\s*\w+\(\s*\))*)"""
    r"""\s*(?:"""
    r"""(?P<operator>==|!=)\s*(?P<literal>['"][^'"]*['"])"""
    r"""|\.\s*is_in\(\s*\[(?P<members>[^\]]*)\]"""
    r"""|\.\s*str\s*\.\s*contains\(\s*(?P<pattern>['"][^'"]*['"])\s*\)"""
    r""")"""
)
_QUOTED_RE = re.compile(r"""['"]([^'"]*)['"]""")
# ``pl.col("A") == pl.col("B")``, optionally casting either side. Two identifier
# columns drawn from different namespaces compare cleanly and match nothing.
_COLUMN_PAIR_RE = re.compile(
    r"""pl\.col\(\s*['"](?P<left>[^'"]+)['"]\s*\)(?:\s*\.\s*\w+\([^()]*\))*"""
    r"""\s*==\s*"""
    r"""pl\.col\(\s*['"](?P<right>[^'"]+)['"]\s*\)"""
)

# A duplicate screen states its notion of identity as the key it groups on. The
# key is the whole test: widen it by one column and the collision it was meant
# to find stops being a collision.
_DUPLICATE_KEY_RE = re.compile(
    r"""(?:group_by|unique)\(\s*(?:subset\s*=\s*)?\[(?P<key>[^\]]+)\]""",
)


def _category_values(frames: dict[str, pl.DataFrame]) -> dict[str, set[str]]:
    """Map each low-cardinality text column to the values it actually holds."""
    values: dict[str, set[str]] = {}
    for frame in frames.values():
        if frame is None:
            continue
        for name, dtype in zip(frame.columns, frame.dtypes):
            if dtype != pl.String:
                continue
            column = frame[name].drop_nulls()
            distinct = column.unique()
            if 0 < distinct.len() <= MAX_CATEGORY_CARDINALITY:
                values.setdefault(name, set()).update(
                    str(value) for value in distinct.to_list()
                )
    return values


def _column_values(frames: dict[str, pl.DataFrame], column: str) -> set[str]:
    """Every value one column name holds, as text, across the tables that carry it.

    Comparisons are read across the joined frames the step builds, so the column
    is located by name rather than by table; stringifying makes the numeric and
    text sides of a cast comparison directly comparable.
    """
    values: set[str] = set()
    for frame in frames.values():
        if frame is None or column not in frame.columns:
            continue
        values.update(str(value) for value in frame[column].drop_nulls().unique().to_list())
    return values


def _step_reality_issues(
    step: dict,
    result: pl.DataFrame,
    frames: dict[str, pl.DataFrame],
    categories: dict[str, set[str]],
) -> list[str]:
    """Flag a step whose predicate cannot be evidence, using the real frames.

    Neither check can be made by the generating worker: it is given column names
    and dtypes but never the values, so a literal it guessed wrong is only
    discoverable here, against the data the step actually runs on.
    """
    issues: list[str] = []
    code = str(step.get("code") or "")
    label = step["label"]
    referenced = [name for name in frames if re.search(rf"\b{re.escape(name)}\b", code)]
    population = max(
        (frames[name].height for name in referenced if frames[name] is not None),
        default=0,
    )
    if (
        population >= MIN_SATURATION_POPULATION
        and result.height >= SATURATION_RATIO * population
    ):
        issues.append(
            f"Step '{label}' flags {result.height} of {population} rows in the "
            "tables it reads. A step that excepts nearly its whole population is "
            "usually a mis-specified predicate rather than a control failure; "
            "confirm the condition before relying on this result."
        )
    known_columns = {
        name for frame in frames.values() if frame is not None for name in frame.columns
    }
    named_columns: set[str] = set()
    absent: list[str] = []
    for match in _COMPARISON_RE.finditer(code):
        column = match.group("column")
        lowered = "to_lowercase" in (match.group("chain") or "")
        if match.group("literal") is not None:
            literals = _QUOTED_RE.findall(match.group("literal"))
        elif match.group("members") is not None:
            literals = _QUOTED_RE.findall(match.group("members"))
        else:
            # ``str.contains`` takes a regex; only a plain alternation of literal
            # words can be checked against a vocabulary without interpreting it.
            pattern = _QUOTED_RE.findall(match.group("pattern") or "")
            raw = pattern[0] if pattern else ""
            if not raw or re.search(r"[.^$*+?()\[\]{}\\]", raw):
                continue
            literals = raw.split("|")
        vocabulary = categories.get(column)
        for literal in literals:
            if literal in known_columns:
                # ``pl.col("A") == "B"`` where B names a column compares a value
                # against a column *name* rather than against that column.
                named_columns.add(literal)
            elif vocabulary is not None and literal:
                # The empty string is the ordinary way to test for a blank, and
                # finding none is that test succeeding, not a dead predicate.
                candidates = (
                    {value.casefold() for value in vocabulary} if lowered else vocabulary
                )
                if (literal.casefold() if lowered else literal) not in candidates:
                    absent.append(literal)
    for match in _COLUMN_PAIR_RE.finditer(code):
        left, right = match.group("left"), match.group("right")
        if left == right:
            continue
        left_values, right_values = _column_values(frames, left), _column_values(frames, right)
        if left_values and right_values and not (left_values & right_values):
            issues.append(
                f"Step '{label}' compares {left} to {right}, but the two columns "
                "share no value in the supplied data; the comparison can never "
                "match, so a result of no exceptions is not evidence."
            )
    for literal in sorted(named_columns):
        issues.append(
            f"Step '{label}' compares against the string '{literal}', which is the "
            "name of a column rather than a value; the comparison can never match."
        )
    if absent:
        issues.append(
            f"Step '{label}' filters on the "
            f"{plural_word(len(set(absent)), 'value')} {sorted(set(absent))}, "
            "which do not occur in the category columns it reads; the step cannot match the "
            "rows it describes."
        )
    return list(dict.fromkeys(issues))


def _overwide_duplicate_key_issues(
    step: dict, result: pl.DataFrame, frames: dict[str, pl.DataFrame]
) -> list[str]:
    """Flag a duplicate screen whose key is too wide to see its own risk.

    A duplicate-payment screen keyed on ``(VENDOR_ID, VENDOR_INVOICE_NUMBER,
    ...)`` returned "no duplicate keys found" on a population holding two
    vendor invoice numbers each billed under *two different* vendor ids. The
    key excluded the collision by construction: the field whose repetition is
    the risk was inside the definition of identity. Reported as a clean pass
    under a critical row, that is worse than not testing at all.

    Checked only where the step found nothing, and only by dropping columns
    from the key the step itself declared — so a screen that already finds
    exceptions is left alone, and the check can never demand a key the
    generating turn did not choose.
    """
    if result.height:
        return []
    code = str(step.get("code") or "")
    issues: list[str] = []
    for match in _DUPLICATE_KEY_RE.finditer(code):
        key = [name for name in _QUOTED_RE.findall(match.group("key")) if name]
        if len(key) < 2:
            continue
        for frame in frames.values():
            if frame is None or not set(key) <= set(frame.columns):
                continue
            if frame.height != frame.unique(subset=key).height:
                continue  # the key does collide; the step is simply clean
            narrower = [
                dropped
                for dropped in key
                if frame.height
                != frame.unique(subset=[name for name in key if name != dropped]).height
            ]
            if narrower:
                issues.append(
                    f"Step '{step['label']}' finds no duplicates on "
                    f"{key}, but the same rows do collide once "
                    f"{sorted(narrower)} is dropped from the key. A duplicate "
                    "screen cannot include the field whose repetition is the "
                    "risk; this result is a property of the key, not evidence "
                    "that no duplicate exists."
                )
            break
    return list(dict.fromkeys(issues))


def _coincident_step_issues(results: list[tuple[str, pl.DataFrame]]) -> list[str]:
    """Flag steps of one test that excepted exactly the same rows.

    Three steps of one live test read as three separate authority tests — an
    approver was designated, was within the matrix limit, approved before the
    order — and returned one identical set of 22 rows, every one of them a row
    where the join had produced nulls. Each condition was written as a chain of
    alternatives beginning with the same null check, so the null check decided
    every result and the two substantive conditions never ran against a
    populated row. The exception counts hid it: three steps, 22 each, reads as
    corroboration rather than as one finding counted three times.

    Compared on the columns the steps share, since steps select what their own
    condition needs and identity is about the rows reached, not the projection.
    """
    issues: list[str] = []
    for index, (label, frame) in enumerate(results):
        for other_label, other in results[index + 1 :]:
            if frame.height != other.height or not frame.height:
                continue
            shared = sorted(set(frame.columns) & set(other.columns))
            if not shared:
                continue
            if frame.select(shared).sort(shared).equals(
                other.select(shared).sort(shared)
            ):
                issues.append(
                    f"Steps '{label}' and '{other_label}' excepted the same "
                    f"{frame.height} rows. Two conditions that never disagree on "
                    "any row are one condition: the exceptions are being decided "
                    "by a term the steps share, so the rest of each predicate is "
                    "untested and the counts double-count one result."
                )
    return issues


def _base_table_names(workspace: Workspace) -> list[str]:
    """Imported tables only. A join is a view of a population, not a population."""
    return [table["name"] for table in workspace.tables]


def _verdict_text(rows: int, steps: int, profile: dict | None) -> str:
    """The headline, led by records rather than rows wherever both are known.

    A multi-step test returns one row per step a record failed, so its row count
    reads as an exception rate several times the real one. State the records
    first, against the population they came from, and keep the row count as the
    secondary figure it is.
    """
    tail = f"{counted(rows, 'exception row')} across {counted(steps, 'check')}."
    if not profile or not profile.get("entity_key"):
        return tail
    records = profile["record_count"]
    population = profile.get("population")
    if population:
        rate = records / population
        lead = (
            f"{records} of {counted(population, 'record')} in "
            f"{profile['population_table']} failed ({rate:.0%})"
        )
    elif records == rows:
        return tail
    else:
        lead = f"{counted(records, 'record')} failed"
    return f"{lead}; {tail}"


def _run_polars_steps(
    workspace: Workspace, item: dict
) -> tuple[dict, pl.DataFrame | None, pl.DataFrame | None, int, list[str]]:
    """Run every step independently and roll the results up deterministically."""
    steps = item["spec"]["steps"]
    issues: list[str] = []
    step_results: list[dict] = []
    summary_frames: list[pl.DataFrame] = []
    exception_frames: list[pl.DataFrame] = []
    step_frames: list[pl.DataFrame] = []
    reason_columns: dict[str, list[str]] = {}
    coincidence_inputs: list[tuple[str, pl.DataFrame]] = []
    stdout_parts: list[str] = []
    total_exceptions = 0
    any_step_failed = False
    frames = {name: workspace.get_frame(name) for name in workspace.table_names()}
    categories = _category_values(frames)
    for step in steps:
        try:
            result, stdout = sandbox.run(step["code"], frames)
        except Exception as exc:
            any_step_failed = True
            issues.append(f"Step '{step['label']}' failed to execute: {exc}")
            step_results.append(
                {
                    "step_id": step["step_id"],
                    "step_label": step["label"],
                    "status": "error",
                    "exception_count": 0,
                    "error": str(exc),
                }
            )
            continue
        stdout_parts.append(stdout)
        step_null_columns = _all_null_columns(result)
        # Exception queries commonly select the field that is expected to be
        # null (for example, ``GRN_ID_LINK.is_null()``).  That is substantive
        # evidence when identifiers or other fields are populated. Only reject
        # a frame whose *every* output column is null.
        if result.width and len(step_null_columns) == result.width:
            issues.append(
                f"Step '{step['label']}' result is entirely null."
            )
        issues.extend(_step_reality_issues(step, result, frames, categories))
        issues.extend(_overwide_duplicate_key_issues(step, result, frames))
        step_exception_count = result.height
        total_exceptions += step_exception_count
        coincidence_inputs.append((step["label"], result))
        summary_frames.append(result)
        if step_exception_count:
            # A step's filter is several alternative conditions; which one a row
            # met is the first thing an auditor needs and the one thing the
            # returned frame does not say. Recover it where the step allows.
            reasons, columns_read = exception_profile.reasons_for_step(step, result)
            reason_columns.update(columns_read)
            step_frames.append(result)
            exception_frames.append(
                result.with_columns(
                    pl.lit(step["step_id"]).alias("_step_id"),
                    pl.lit(step["label"]).alias("_step_label"),
                    (reasons if reasons is not None else pl.lit(step["label"])).alias(
                        "_reason"
                    ),
                )
            )
        step_results.append(
            {
                "step_id": step["step_id"],
                "step_label": step["label"],
                "status": "completed_with_exception" if step_exception_count else "completed_no_exception",
                "exception_count": step_exception_count,
                "error": None,
            }
        )
    issues.extend(_coincident_step_issues(coincidence_inputs))
    summary = pl.concat(summary_frames, how="diagonal_relaxed") if summary_frames else None
    exceptions = pl.concat(exception_frames, how="diagonal_relaxed") if exception_frames else None
    if any_step_failed:
        issues.append("One or more steps failed to execute; see step results for detail.")
    profile = exception_profile.build(
        exceptions,
        step_frames,
        {name: frames[name] for name in _base_table_names(workspace) if name in frames},
        reason_columns,
    )
    output = {
        "verdict": "error" if any_step_failed else ("fail" if total_exceptions else "ok"),
        "statistics": [
            {"label": "Steps", "value": str(len(steps))},
            {"label": "Exception rows", "value": str(total_exceptions)},
        ],
        "verdict_text": _verdict_text(total_exceptions, len(steps), profile),
        "viz": {"type": "table"},
        "stdout": "\n".join(part for part in stdout_parts if part),
        "step_results": step_results,
        "exception_profile": profile,
    }
    return output, summary, exceptions, total_exceptions, issues


def _run_engine(workspace: Workspace, item: dict) -> tuple[dict, pl.DataFrame | None, pl.DataFrame | None, int, list[str]]:
    engine = item["engine"]
    issues = list(item.get("semantic_warnings") or [])
    if engine == "analytics":
        frame = workspace.get_frame(item["table_refs"][0])
        result = analytics.run_test(
            frame,
            item["spec"]["test_id"],
            item["spec"].get("params") or {},
            source=workspace.frame_source(),
        )
        payload = result.payload()
        summary, exceptions = result.summary, result.detail
        exception_count = result.detail.height if result.detail is not None else 0
        verdict = result.verdict
        # What this test's flagged output *is*, read from the registry rather
        # than from a list kept here. The list named benford, last_two_digits and
        # outliers, which the registry still classifies exactly that way; what it
        # could not say is that a weekend scan or a threshold cluster needs the
        # same corroboration, so those results reached a control conclusion
        # unqualified. Only an `exception` test is evidence on its face.
        if (
            analytics.signal_for(item["spec"]["test_id"]) != analytics.SIGNAL_EXCEPTION
            and verdict in {"warn", "fail"}
        ):
            issues.append("Screening result requires corroboration before it can support a control exception.")
        output = {
            "verdict": verdict,
            "statistics": payload["stats"],
            "verdict_text": payload["verdict_text"],
            "viz": payload.get("viz"),
        }
    elif engine == "validation":
        frame = workspace.get_frame(item["table_refs"][0])
        rules = item["spec"]["rules"]
        run = validation.run_rules(frame, rules, item["table_refs"][0], workspace.get_frame)
        summary = validation.summary_frame(run)
        failing = []
        for rule, result in zip(rules, run["results"]):
            if result["fail_count"] and result["verdict"] not in {"error", "skipped"}:
                detail = validation.rule_failures(frame, rule, workspace.get_frame).head(EXCEPTION_ROWS)
                failing.append(detail.with_columns(pl.lit(result["rule_id"]).alias("_rule_id")))
        exceptions = pl.concat(failing, how="diagonal_relaxed") if failing else None
        exception_count = sum(result["fail_count"] for result in run["results"])
        output = {
            "verdict": run["verdict"],
            "statistics": [
                {"label": label.replace("_", " ").title(), "value": str(value)}
                for label, value in run["counts"].items()
            ],
            "verdict_text": f"{counted(exception_count, 'validation exception')}.",
            "viz": {"type": "table"},
        }
        issues.extend(validation.generated_rule_issues(frame, rules, workspace.get_frame))
    else:
        output, summary, exceptions, exception_count, step_issues = _run_polars_steps(workspace, item)
        issues.extend(step_issues)
        return output, summary, exceptions, exception_count, list(dict.fromkeys(issues))
    null_columns = _all_null_columns(summary)
    if engine == "validation":
        # ``validation.summary_frame`` always includes the optional diagnostic
        # error column. A null value means the rule evaluated normally, so it
        # must not invalidate an otherwise substantive result frame.
        null_columns = [name for name in null_columns if name != "error"]
    if null_columns:
        issues.append(f"Result columns are entirely null: {', '.join(null_columns)}.")
    return output, summary, exceptions, exception_count, list(dict.fromkeys(issues))


def compute(workspace: Workspace, data_test_id: str) -> dict:
    """Compute an immutable result candidate without mutating the workspace."""
    item = _record(workspace, data_test_id)
    if item.get("status") == "draft" or not item.get("engine"):
        raise WorkspaceError(
            f"Data Test '{data_test_id}' has no executable specification yet."
        )
    run_id = CURRENT_RESULT_ID
    run_at = utcnow()
    execution_refs = _execution_table_refs(workspace, item)
    fingerprints = _dataset_fingerprints(workspace, execution_refs)
    source_sha1 = _sha1(
        {"engine": item["engine"], "table_refs": execution_refs, "spec": item["spec"]}
    )
    join_issues, diagnostics = _join_issues(workspace, execution_refs)
    try:
        output, summary, exceptions, exception_count, semantic_issues = _run_engine(workspace, item)
        semantic_issues = list(dict.fromkeys([*join_issues, *semantic_issues]))
        # ``semantic_valid`` grades how far the result can be trusted, not
        # whether it may be concluded over. The severe shapes below — a
        # predicate that cannot match, a join that matches nothing, steps that
        # never disagree, a pass that is a property of the key — are what the
        # panel puts in front of whoever concludes, and what the working paper
        # carries alongside the conclusion. They warn; they no longer decide.
        semantic_valid = not any(
            "0% key match" in issue
            or "multiplies rows" in issue
            or "entirely null" in issue
            or "naturally unique" in issue
            or "conditional trigger matches zero" in issue
            or "allowed values have no overlap" in issue
            or "failed to execute" in issue
            or "mis-specified predicate" in issue
            or "can never match" in issue
            or "cannot match the rows it describes" in issue
            or "excepted the same" in issue
            or "a property of the key" in issue
            for issue in semantic_issues
        )
        # The run read the data, so it reports what it read. What it could not
        # execute is the one thing it cannot report on: a step that failed
        # measured nothing, and the engine says so with an ``error`` verdict.
        status = (
            "review_required"
            if output["verdict"] == "error"
            else "completed_with_exception"
            if exception_count or output["verdict"] in {"warn", "fail"}
            else "completed_no_exception"
        )
        # What the run reads as, for whoever concludes to accept or depart from.
        # It is a suggestion on the result, never a conclusion on the test.
        suggested_control_conclusion = (
            "ineffective"
            if status == "completed_with_exception"
            else "effective"
            if status == "completed_no_exception"
            else "no_conclusion"
        )
        error = None
    except Exception as exc:
        output = {"verdict": "error", "statistics": [], "verdict_text": str(exc), "viz": None}
        summary = exceptions = None
        exception_count = 0
        semantic_issues = list(dict.fromkeys([*join_issues, str(exc)]))
        semantic_valid = False
        status = "review_required"
        suggested_control_conclusion = "no_conclusion"
        error = str(exc)
    result = {
        "id": run_id,
        "data_test_id": data_test_id,
        "rcm_id": item["rcm_id"],
        "run_at": run_at,
        "status": status,
        "suggested_control_conclusion": suggested_control_conclusion,
        "verdict": output["verdict"],
        "verdict_text": output.get("verdict_text") or "",
        "statistics": output.get("statistics") or [],
        "viz": output.get("viz"),
        "stdout": output.get("stdout") or "",
        "dataset_fingerprints": fingerprints,
        "source_sha1": source_sha1,
        "summary_frame": explore.frame_payload(summary, SUMMARY_ROWS) if summary is not None else None,
        "exception_frame": explore.frame_payload(exceptions, EXCEPTION_ROWS) if exceptions is not None else None,
        "exception_count": exception_count,
        "exception_profile": output.get("exception_profile"),
        "input_sha1": evaluation_input_sha1(source_sha1, fingerprints),
        "semantic_valid": semantic_valid,
        "semantic_issues": semantic_issues,
        "join_diagnostics": diagnostics,
        "step_results": output.get("step_results") or [],
        "error": error,
    }
    result["result_sha1"] = _sha1(result)
    return result


_RESULT_EVALUATION_STATES = {
    "completed_no_exception": "passed",
    "completed_with_exception": "failed",
    "review_required": "inconclusive",
}


def record_evaluation(item: dict, result: dict) -> dict:
    """Persist what one run found, leaving every ruling to its own author.

    Rulings survive the run that they were made against. Reconciling them here
    rather than dropping them is what lets an auditor's acceptance of "rounding
    under 1.00" stand through a re-run that finds the same rounding again.
    """
    reasons = reason_inventory(
        result.get("exception_profile"), int(result.get("exception_count") or 0)
    )
    item["evaluation"] = new_evaluation(
        _RESULT_EVALUATION_STATES.get(str(result.get("status") or ""), "inconclusive"),
        str(result.get("verdict_text") or ""),
        exception_count=int(result.get("exception_count") or 0),
        reasons=reasons,
        suggested_control_conclusion=str(
            result.get("suggested_control_conclusion") or "no_conclusion"
        ),
        input_sha1=str(result.get("input_sha1") or "") or None,
        ran_at=str(result.get("run_at") or "") or None,
    )
    labels = [reason["label"] for reason in reasons]
    by_key = {
        str(disposition.get("key")): disposition
        for disposition in item.get("exception_dispositions") or []
    }
    dispositions = []
    for reason in reasons:
        existing = by_key.get(reason["label"])
        if existing is None:
            dispositions.append(new_disposition(reason["label"], rows=reason["rows"], records=reason["records"]))
            continue
        # A group that is still there keeps its ruling; ``_normalize_marking``
        # decides whether the changed inputs made that ruling stale.
        dispositions.append({**existing, "rows": reason["rows"], "records": reason["records"]})
    # A reason the run no longer produces keeps a decided ruling as history and
    # drops an undecided one, which was only ever a prompt to look.
    dispositions.extend(
        disposition
        for key, disposition in by_key.items()
        if key not in labels and disposition.get("state") != "pending"
    )
    item["exception_dispositions"] = dispositions
    _normalize_marking(item)
    return item


def record_exception_disposition(
    workspace: Workspace,
    data_test_id: str,
    key: str,
    state: str,
    *,
    note: str = "",
    actor: str = "auditor",
    source: str = "auditor",
) -> dict:
    """Rule on one group of exceptions without touching what the run found."""
    item = _record(workspace, data_test_id)
    known = {reason["label"] for reason in item["evaluation"]["reasons"]}
    if key not in known:
        raise WorkspaceError(f"This test has no exception group '{key}'.")
    # Retiring an exception is the ruling that moves the control conclusion, so
    # the note is worth having — but an empty one is a thin working paper, not a
    # reason to refuse the ruling itself.
    reason = next(item_ for item_ in item["evaluation"]["reasons"] if item_["label"] == key)
    replacement = (
        new_disposition(key, "pending", note, rows=reason["rows"], records=reason["records"])
        if state == "pending"
        else new_disposition(
            key,
            state,
            note,
            rows=reason["rows"],
            records=reason["records"],
            actor=actor,
            source=source,
            at=utcnow(),
            evaluated_input_sha1=item["evaluation"]["input_sha1"],
        )
    )
    # A group is rulable as soon as the evaluation lists it, whether or not a
    # placeholder row was ever written for it — a record that has not been
    # re-run since the marking model landed has the inventory but no rows.
    # Replacing in place only would drop the ruling and still answer 200.
    existing_keys = {str(value.get("key")) for value in item["exception_dispositions"]}
    item["exception_dispositions"] = (
        [
            replacement if str(value.get("key")) == key else value
            for value in item["exception_dispositions"]
        ]
        if key in existing_keys
        else [*item["exception_dispositions"], replacement]
    )
    _normalize_marking(item)
    item["updated"] = utcnow()
    workspace.save()
    return item


def record_semantic_review(
    workspace: Workspace, data_test_id: str, note: str = "", *, actor: str = "auditor"
) -> dict:
    """Record that somebody read the semantic issues and judged them survivable.

    Without this a test the runner could not vouch for is stranded outside
    completion forever, whatever an auditor makes of the warning — which is why
    the record releasing it is the act, and the note on it is optional. Making
    prose the price of admission put an essay on the only unblocking path.
    """
    item = _record(workspace, data_test_id)
    item["semantic_review"] = {"at": utcnow(), "actor": actor, "note": str(note or "")}
    _normalize_marking(item)
    item["updated"] = utcnow()
    workspace.save()
    return item


def auto_disposition(
    workspace: Workspace, data_test_id: str, *, actor: str = "agent"
) -> dict:
    """Conclude one Data Test without an auditor, and say so on the record.

    This is what makes an auto run complete: the evaluation is deterministic, so
    the ruling that follows from it is too. Three things keep that honest — it
    is a separate act from running, it stamps ``agent`` as its author, and it
    never touches anything an auditor has already decided.

    A run that produced no evidence at all — never run, or failed to execute —
    is the one case it declines; there is nothing to conclude from. Evidence the
    semantic checks doubt is concluded over and the doubt recorded with it, so
    the warning reaches whoever reads the conclusion instead of stranding it.
    """
    item = _record(workspace, data_test_id)
    evaluation = item["evaluation"]
    if evaluation["state"] in {"not_run", "inconclusive"}:
        return item
    for disposition in item["exception_dispositions"]:
        if disposition["source"] == "auditor":
            continue
        item["exception_dispositions"] = [
            new_disposition(
                disposition["key"],
                "exception",
                "Recorded by the unattended run; no auditor has reviewed it.",
                rows=disposition["rows"],
                records=disposition["records"],
                actor=actor,
                source="agent",
                at=utcnow(),
                evaluated_input_sha1=evaluation["input_sha1"],
            )
            if existing is disposition
            else existing
            for existing in item["exception_dispositions"]
        ]
    if item["control_conclusion_source"] != "auditor":
        item["control_conclusion"] = evaluation["suggested_control_conclusion"]
        item["control_conclusion_source"] = "agent"
        item["control_conclusion_input_sha1"] = evaluation["input_sha1"]
    if item["conclusion_source"] != "auditor" and not str(item.get("conclusion") or "").strip():
        item["conclusion"] = evaluation["note"]
        item["conclusion_source"] = "agent"
    _normalize_marking(item)
    item["updated"] = utcnow()
    workspace.save()
    return item


def commit_result(
    workspace: Workspace,
    data_test_id: str,
    result: dict,
    *,
    expected_definition_sha1: str | None = None,
) -> dict:
    """Commit one computed candidate under parent-hash/revision coordination."""
    from .workspace_transactions import (
        canonical_sha1,
        complete_linked_write,
        datatest_material_projection,
        material_projection,
        mutate,
        parent_hashes,
        prepare_linked_write,
        rollback_linked_write,
    )

    expected = expected_definition_sha1 or parent_hashes(
        workspace, [f"datatest:{data_test_id}"]
    )[f"datatest:{data_test_id}"]

    linked_writes = []

    def commit(fresh: Workspace) -> dict:
        item = _record(fresh, data_test_id)
        candidate = dict(result)
        supplied_sha1 = candidate.get("result_sha1")
        actual_sha1 = _sha1({key: value for key, value in candidate.items() if key != "result_sha1"})
        if supplied_sha1 != actual_sha1:
            raise WorkspaceError("The Data Test result candidate failed its integrity check.")
        if canonical_sha1(material_projection(datatest_material_projection(item))) != expected:
            # ``mutate`` normally catches this first. Keep the check next to
            # the file write so future direct callers cannot bypass it.
            raise WorkspaceError("The Data Test definition changed before its result could be committed.")
        result_path = _result_path(fresh, data_test_id, str(candidate["id"]))
        linked_write = prepare_linked_write(fresh, result_path, candidate)
        linked_writes.append(linked_write)
        write_json_atomic(result_path, candidate)
        latest = {
            key: candidate[key]
            for key in (
                "id", "run_at", "status", "verdict", "exception_count", "semantic_valid",
                "dataset_fingerprints", "source_sha1", "result_sha1",
            )
        }
        item.pop("runs", None)
        item["last_run"] = latest
        # Running records what the run found and nothing else. Concluding is a
        # separate act with its own author — see :func:`auto_disposition` for
        # the unattended path and :func:`update` for the auditor's.
        record_evaluation(item, candidate)
        item["updated"] = candidate["run_at"]
        return candidate

    try:
        committed = mutate(
            workspace,
            commit,
            expected_parents={f"datatest:{data_test_id}": expected},
        )
    except Exception:
        for linked_write in reversed(linked_writes):
            rollback_linked_write(linked_write)
        raise
    else:
        for linked_write in linked_writes:
            complete_linked_write(linked_write)
        _discard_superseded_results(committed.workspace, data_test_id)
    from .workspaces import sync_workspace

    # Preserve the long-standing service contract for callers that retain row,
    # planned-test, or Data Test references after ``run``.
    sync_workspace(workspace, committed.workspace)
    return committed.value


def run(workspace: Workspace, data_test_id: str) -> dict:
    expected = None
    from .workspace_transactions import parent_hashes

    expected = parent_hashes(workspace, [f"datatest:{data_test_id}"])[
        f"datatest:{data_test_id}"
    ]
    result = compute(workspace, data_test_id)
    return commit_result(
        workspace,
        data_test_id,
        result,
        expected_definition_sha1=expected,
    )


def _run_all(workspace: Workspace, test_ids: list[str]) -> dict:
    """Run a selected set of Data Tests one at a time."""
    completed: list[dict] = []
    failed: list[dict] = []
    for data_test_id in test_ids:
        try:
            result = run(workspace, data_test_id)
            completed.append(
                {
                    "data_test_id": data_test_id,
                    "status": result["status"],
                    "exception_count": result["exception_count"],
                }
            )
        except Exception as error:
            failed.append({"data_test_id": data_test_id, "error": str(error)})

    # Keep the central RCM projection current even when a subset of tests
    # could not run. Exploratory tests simply do not contribute linked rows.
    from . import rcm_execution

    rcm_execution.rollup(workspace)
    return {
        "total": len(test_ids),
        "completed": completed,
        "failed": failed,
    }


def run_all(workspace: Workspace, test_ids: list[str] | None = None) -> dict:
    """Run Data Tests in the workspace, including exploratory tests.

    ``test_ids`` restricts the batch to a subset, which is how the status bar
    offers "run the four that have not run" without re-running the rest.  An id
    that no longer resolves is dropped rather than raising: the caller's view of
    the workspace can be a moment behind, and a stale id is not a reason to run
    nothing.
    """
    known = [str(item["id"]) for item in workspace.data_tests]
    if test_ids is not None:
        requested = {str(value) for value in test_ids}
        known = [test_id for test_id in known if test_id in requested]
    return _run_all(workspace, known)


def run_all_rcm_linked(
    workspace: Workspace, test_ids: list[str] | None = None
) -> dict:
    """Run Data Tests linked to an RCM row, one at a time.

    Each test has its own durable result and guarded commit, so a bad or
    incomplete definition must not prevent the other RCM-linked tests from
    running.  The returned payload is intentionally a compact batch summary;
    callers can open individual results through the normal test endpoint.

    ``test_ids`` restricts the batch to a subset of the RCM-linked tests.  The
    filter is an intersection rather than an override, so a caller can never
    reach an unlinked test through this path.
    """
    linked = [
        str(item["id"])
        for item in workspace.data_tests
        if item.get("rcm_id")
    ]
    if test_ids is not None:
        requested = {str(value) for value in test_ids}
        linked = [test_id for test_id in linked if test_id in requested]
    return _run_all(workspace, linked)


def load_result(workspace: Workspace, data_test_id: str, run_id: str) -> dict:
    item = _record(workspace, data_test_id)
    if not item.get("last_run") or run_id != item["last_run"].get("id"):
        raise WorkspaceError(f"Data Test result '{run_id}' is not the current result.")
    return _read_result(workspace, data_test_id, run_id)


def _read_result(workspace: Workspace, data_test_id: str, run_id: str) -> dict:
    """Read a result file, including a legacy result retained as evidence."""
    path = _result_path(workspace, data_test_id, run_id)
    if not path.exists():
        raise WorkspaceError(f"Data Test result '{run_id}' not found.")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"Data Test result '{run_id}' is unreadable.") from error
    expected = result.get("result_sha1")
    actual = _sha1({key: value for key, value in result.items() if key != "result_sha1"})
    if expected != actual:
        raise WorkspaceError(f"Data Test result '{run_id}' failed its integrity check.")
    return result


def result_artifact(workspace: Workspace, source_id: str) -> dict | None:
    data_test_id, separator, run_id = str(source_id).partition(":")
    if not separator:
        item = next((value for value in workspace.data_tests if value.get("id") == source_id), None)
        if not item or not item.get("last_run"):
            return None
        run_id = item["last_run"]["id"]
        data_test_id = item["id"]
    try:
        result = _read_result(workspace, data_test_id, run_id)
    except WorkspaceError:
        return None
    # ``result_sha1`` stays the file-integrity hash checked by ``_read_result``.
    # What an evidence anchor pins is the narrower evidentiary basis.
    return {"item": result, "sha1": result_evidence_sha1(result)}


def spec_as_python_code(spec: dict) -> str:
    """Flatten a Polars spec's steps into one script for ad-hoc dashboard tiles.

    Dashboard tiles run one code block; a multi-step Data Test has none, so
    this is a display convenience only, not a second executable definition.
    """
    steps = (spec or {}).get("steps") or []
    if not steps:
        return str((spec or {}).get("code") or "")
    return "\n\n".join(
        f"# Step: {step.get('label') or index + 1}\n{step.get('code') or ''}"
        for index, step in enumerate(steps)
    )


def list_payload(workspace: Workspace) -> list[dict]:
    # Hydrate on read the way ``_record`` does for a single test, so every
    # reader sees the full marking whether or not the stored record predates it.
    # In-memory only: a GET must not advance the workspace revision.
    for item in workspace.data_tests:
        _normalize_marking(item)
    return [
        {
            **{key: value for key, value in item.items() if key != "runs"},
            # Derived against the live workspace rather than stored, because it
            # is a statement about now. The table signatures behind it are
            # cached per workspace, which is what keeps this affordable on a
            # list read.
            "result_stale": result_stale(workspace, item),
        }
        for item in sorted(workspace.data_tests, key=lambda item: item.get("updated") or "", reverse=True)
    ]
