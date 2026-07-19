"""Focused model-worker contracts for composable audit workflow units."""

from __future__ import annotations

from typing import Any

from .. import data_tests, doc_tests
from ..workspaces import PLANNED_TEST_METHODS, Workspace, WorkspaceError
from . import context_bundles, prompts

ROUTER_SYSTEM = f"""[agent:workflow_router]
Classify one audit-assistant command. You are a router, not a planner. Return
route (workflow|generic_action|question|unsupported), requested_outcomes (only
supported outcome IDs), objective, target_refs, refresh_policy
(missing_or_stale|force), action_intent (null or a short normalized operation),
constraints, needs_clarification, and clarification. Never propose actions,
workers, dependencies, tests, columns, or execution steps. {prompts.JSON_RULES}"""

PLANNED_TEST_SYSTEM = f"""[agent:work_program]
Draft executable planned tests for exactly one supplied RCM row. Return an
object with planned_tests. Each item must contain operation (create|update),
planned_test_id for updates, stable_slug, title, objective, criteria, steps as
non-empty strings, method (data_analytics|validation|document_inspection|inquiry|hybrid|evidence_unavailable),
and expected_evidence. Optional sampling must be an object containing only
strategy (string), size (positive integer or null), seed (integer), and
stratify_by (string or null). Optional thresholds must be an object with short
names and JSON scalar values; never put explanatory prose directly in sampling
or thresholds. Link only to the supplied RCM row using rcm_id. Choose the
method based on the available evidence and data. Do not define Data Tests or
Document Tests here. {prompts.JSON_RULES}"""

DATA_TEST_SPEC_SYSTEM = f"""[agent:data_test_spec]
Translate one planned test into exactly one executable durable Data Test.
Return an object with data_test containing title, objective, engine
(analytics|validation|polars), table_refs, and spec. Use exact table and column
names and only supplied registry IDs. For analytics, spec contains test_id and
params; test_id is an analytics_registry id, never the planned-test id. For
validation, spec contains non-empty rules shaped as objects with check (a
validation_registry id), optional column, and params; SQL is not supported.
For polars, spec contains safe Polars code assigning a DataFrame to result;
imports and filesystem or network access are forbidden. Do not return document
work. {prompts.JSON_RULES}"""

DOCUMENT_TEST_SPEC_SYSTEM = f"""[agent:document_test_spec]
Translate one planned test into exactly one substantive durable Document Test.
Return an object with document_test containing title, kind
(vouching|attribute|review|qa), spec, and non-empty items. Every item must have
a label and its exact attached document_ids when evidence is available.
Vouching items need checks with field, expected, method, and optional tolerance;
method must be exact, normalized, fuzzy, numeric_tolerance, or date_tolerance.
attribute items need attributes; review items need page, excerpt, or summary;
Q&A items need a concrete question. If evidence is unavailable, still return a
concrete question/attribute/check plus missing_evidence containing required
document types/identifiers and rationale. Never return a description-only
manual-review item. {prompts.JSON_RULES}"""

FINDING_SYSTEM = f"""[agent:finding]
Draft one unconfirmed audit finding from the supplied auditor-dispositioned
observation and immutable execution reference. Return finding with title,
severity (critical|high|medium|low|info), condition, criteria, cause or
cause_pending, effect, recommendation, and severity_rationale. Do not create or
alter RCM, planned-test, execution, or evidence references. Do not claim auditor
confirmation. {prompts.JSON_RULES}"""


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected an array")
    result = [str(item).strip() for item in value]
    if not result or any(not item for item in result):
        raise ValueError("array must contain non-empty strings")
    return result


def _contract_error(errors: list[str]) -> None:
    if errors:
        raise ValueError("; ".join(errors))


def validate_route(payload: dict, supported: set[str]) -> dict:
    route = str(payload.get("route") or "")
    if route not in {"workflow", "generic_action", "question", "unsupported"}:
        raise ValueError("route is unsupported")
    outcomes = payload.get("requested_outcomes") or []
    if not isinstance(outcomes, list) or any(str(item) not in supported for item in outcomes):
        raise ValueError("requested_outcomes contains an unsupported capability")
    refresh = str(payload.get("refresh_policy") or "missing_or_stale")
    if refresh not in {"missing_or_stale", "force"}:
        raise ValueError("refresh_policy is unsupported")
    needs = bool(payload.get("needs_clarification"))
    if route == "workflow" and not outcomes and not needs:
        raise ValueError("a workflow route needs at least one requested outcome")
    return {
        "route": route,
        "requested_outcomes": [str(item) for item in outcomes],
        "objective": str(payload.get("objective") or "").strip(),
        "target_refs": [str(item) for item in payload.get("target_refs") or []],
        "refresh_policy": refresh,
        "action_intent": str(payload.get("action_intent") or "").strip() or None,
        "constraints": [str(item) for item in payload.get("constraints") or []],
        "needs_clarification": needs,
        "clarification": str(payload.get("clarification") or "").strip() or None,
    }


def resolve_command(runner, bundle: context_bundles.ContextBundle, supported: set[str]) -> dict:
    return runner.llm_json(
        ROUTER_SYSTEM,
        bundle.serialized(),
        activity={"context_metrics": bundle.metrics()},
        validator=lambda payload: validate_route(payload, supported),
    )


def validate_planned_tests(payload: dict, rcm_id: str) -> dict:
    values = payload.get("planned_tests") or payload.get("procedures")
    if not isinstance(values, list) or not values:
        raise ValueError("planned_tests must be a non-empty array")
    normalized = []
    errors: list[str] = []
    for index, raw in enumerate(values, 1):
        path = f"planned_tests[{index - 1}]"
        if not isinstance(raw, dict):
            errors.append(f"{path} must be an object")
            continue
        value = dict(raw)
        required = ("operation", "stable_slug", "title", "objective", "criteria", "method", "expected_evidence")
        missing = [key for key in required if not isinstance(value.get(key), str) or not value[key].strip()]
        errors.extend(f"{path}.{key} must be a non-empty string" for key in missing)
        operation = value.get("operation")
        if operation not in {"create", "update"}:
            errors.append(f"{path}.operation is unsupported")
        if operation == "update" and not str(value.get("planned_test_id") or "").strip():
            errors.append(f"{path}.planned_test_id is required for update")
        if value.get("method") not in PLANNED_TEST_METHODS:
            errors.append(f"{path}.method is unsupported")
        try:
            value["steps"] = _strings(value.get("steps"))
        except ValueError as error:
            errors.append(f"{path}.steps {error}")
        sampling = value.get("sampling")
        if sampling is not None:
            if not isinstance(sampling, dict):
                errors.append(f"{path}.sampling must be an object")
            else:
                unknown = sorted(set(sampling) - {"strategy", "size", "seed", "stratify_by"})
                if unknown:
                    errors.append(f"{path}.sampling.{unknown[0]} is not supported")
                if "strategy" in sampling and not isinstance(sampling["strategy"], str):
                    errors.append(f"{path}.sampling.strategy must be a string")
                size = sampling.get("size")
                if size is not None and (
                    not isinstance(size, int) or isinstance(size, bool) or size < 1
                ):
                    errors.append(f"{path}.sampling.size must be a positive integer or null")
                seed = sampling.get("seed")
                if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
                    errors.append(f"{path}.sampling.seed must be an integer")
                if sampling.get("stratify_by") is not None and not isinstance(
                    sampling.get("stratify_by"), str
                ):
                    errors.append(f"{path}.sampling.stratify_by must be a string or null")
        thresholds = value.get("thresholds")
        if thresholds is not None:
            if not isinstance(thresholds, dict):
                errors.append(f"{path}.thresholds must be an object")
            else:
                invalid = next(
                    (
                        key
                        for key, threshold in thresholds.items()
                        if not isinstance(threshold, (str, int, float, bool, type(None)))
                    ),
                    None,
                )
                if invalid is not None:
                    errors.append(f"{path}.thresholds.{invalid} must be a JSON scalar")
        value["rcm_id"] = rcm_id
        value["rcm_refs"] = [rcm_id]
        normalized.append(value)
    _contract_error(errors)
    return {"planned_tests": normalized}


def planned_tests(runner, bundle: context_bundles.ContextBundle, rcm_id: str) -> list[dict]:
    payload = runner.llm_json(
        PLANNED_TEST_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": [f"rcm:{rcm_id}"], "context_metrics": bundle.metrics()},
        validator=lambda value: validate_planned_tests(value, rcm_id),
        attempts=3,
    )
    return payload["planned_tests"]


def validate_data_test(payload: dict, workspace: Workspace) -> dict:
    value = payload.get("data_test")
    if not isinstance(value, dict):
        raise ValueError("data_test must be an object")
    required = ("title", "objective", "engine", "table_refs", "spec")
    if any(key not in value for key in required):
        raise ValueError("data_test is missing required fields")
    if value.get("engine") not in {"analytics", "validation", "polars"}:
        raise ValueError("data_test engine is unsupported")
    if not isinstance(value.get("table_refs"), list) or not value["table_refs"]:
        raise ValueError("data_test table_refs must be non-empty")
    if not isinstance(value.get("spec"), dict):
        raise ValueError("data_test spec must be an object")
    normalized = dict(value)
    try:
        normalized["table_refs"] = data_tests._table_refs(workspace, value["table_refs"])
        normalized["spec"], _warnings = data_tests._validate_spec(
            workspace, str(value["engine"]), normalized["table_refs"], value["spec"]
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"data_test definition is invalid: {error}") from error
    return {"data_test": normalized}


def data_test_spec(runner, bundle: context_bundles.ContextBundle, parent_refs: list[str]) -> dict:
    payload = runner.llm_json(
        DATA_TEST_SPEC_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": parent_refs, "context_metrics": bundle.metrics()},
        validator=lambda value: validate_data_test(value, runner.ws),
        attempts=3,
    )
    return dict(payload["data_test"])


def validate_document_test(payload: dict, workspace: Workspace) -> dict:
    value = payload.get("document_test")
    if not isinstance(value, dict):
        raise ValueError("document_test must be an object")
    if value.get("kind") not in {"vouching", "attribute", "review", "qa"}:
        raise ValueError("document_test kind is unsupported")
    if not str(value.get("title") or "").strip():
        raise ValueError("document_test title is required")
    if not isinstance(value.get("spec"), dict):
        raise ValueError("document_test spec must be an object")
    items = value.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("document_test items must be non-empty")
    kind = value["kind"]
    known_documents = {str(item.get("id")) for item in workspace.documents}
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or not str(item.get("label") or "").strip():
            raise ValueError(f"document_test item {index} needs a label")
        missing_document = next(
            (
                document_id
                for document_id in item.get("document_ids") or []
                if str(document_id) not in known_documents
            ),
            None,
        )
        if missing_document is not None:
            raise ValueError(f"document_test item {index} references unknown document '{missing_document}'")
        if kind == "vouching" and not item.get("checks"):
            raise ValueError(f"document_test item {index} needs comparison checks")
        if kind == "vouching":
            for check_index, check in enumerate(item.get("checks") or [], 1):
                if not isinstance(check, dict):
                    raise ValueError(
                        f"document_test item {index} check {check_index} must be an object"
                    )
                method = str(check.get("method") or "normalized")
                if method not in doc_tests.METHODS:
                    raise ValueError(f"Unknown comparison method '{method}'.")
        if kind == "attribute" and not item.get("attributes"):
            raise ValueError(f"document_test item {index} needs attributes")
        if kind == "review" and not any(item.get(key) not in (None, "") for key in ("page", "excerpt", "summary")):
            raise ValueError(f"document_test item {index} needs a review location or summary")
        if kind == "qa" and not str(item.get("question") or "").strip():
            raise ValueError(f"document_test item {index} needs a question")
    return {"document_test": value}


def document_test_spec(runner, bundle: context_bundles.ContextBundle, parent_refs: list[str]) -> dict:
    payload = runner.llm_json(
        DOCUMENT_TEST_SPEC_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": parent_refs, "context_metrics": bundle.metrics()},
        validator=lambda value: validate_document_test(value, runner.ws),
        attempts=3,
    )
    return dict(payload["document_test"])


def validate_finding(payload: dict) -> dict:
    value = payload.get("finding")
    if not isinstance(value, dict):
        raise ValueError("finding must be an object")
    required = ("title", "severity", "condition", "criteria", "effect", "recommendation", "severity_rationale")
    missing = [key for key in required if not str(value.get(key) or "").strip()]
    if missing:
        raise ValueError(f"finding is missing {missing[0]}")
    if value.get("severity") not in {"critical", "high", "medium", "low", "info"}:
        raise ValueError("finding severity is unsupported")
    if not str(value.get("cause") or "").strip() and not value.get("cause_pending"):
        raise ValueError("finding needs cause or cause_pending")
    return {"finding": value}


def finding(runner, bundle: context_bundles.ContextBundle, parent_refs: list[str]) -> dict:
    payload = runner.llm_json(
        FINDING_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": parent_refs, "context_metrics": bundle.metrics()},
        validator=validate_finding,
    )
    return dict(payload["finding"])
