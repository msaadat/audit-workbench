"""Focused model-worker contracts for composable audit workflow units."""

from __future__ import annotations

from typing import Any

from ..workspaces import PLANNED_TEST_METHODS, WorkspaceError
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
expected_evidence, sampling, and thresholds. Link only to the supplied RCM row
using rcm_id. Choose the method based on the available evidence and data. Do
not define Data Tests or Document Tests here. {prompts.JSON_RULES}"""

DATA_TEST_SPEC_SYSTEM = f"""[agent:data_test_spec]
Translate one planned test into exactly one executable durable Data Test.
Return an object with data_test containing title, objective, engine
(analytics|validation|polars), table_refs, and spec. Use exact table and column
names and only supplied registry IDs. For analytics, spec contains test_id and
params. For validation, spec contains non-empty rules. For polars, spec contains
safe Polars code assigning a DataFrame to result; imports and filesystem or
network access are forbidden. Do not return document work. {prompts.JSON_RULES}"""

DOCUMENT_TEST_SPEC_SYSTEM = f"""[agent:document_test_spec]
Translate one planned test into exactly one substantive durable Document Test.
Return an object with document_test containing title, kind
(vouching|attribute|review|qa), spec, and non-empty items. Every item must have
a label and its exact attached document_ids when evidence is available.
Vouching items need checks with field, expected, method, and optional tolerance;
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
    if any(not item for item in result):
        raise ValueError("array values must be non-empty strings")
    return result


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
    for index, raw in enumerate(values, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"planned test {index} must be an object")
        value = dict(raw)
        required = ("operation", "stable_slug", "title", "objective", "criteria", "method", "expected_evidence")
        missing = [key for key in required if not isinstance(value.get(key), str) or not value[key].strip()]
        if missing:
            raise ValueError(f"planned test {index} is missing {missing[0]}")
        if value["operation"] not in {"create", "update"}:
            raise ValueError(f"planned test {index} operation is unsupported")
        if value["operation"] == "update" and not str(value.get("planned_test_id") or "").strip():
            raise ValueError(f"planned test {index} needs planned_test_id for update")
        if value["method"] not in PLANNED_TEST_METHODS:
            raise ValueError(f"planned test {index} method is unsupported")
        value["steps"] = _strings(value.get("steps"))
        if value.get("sampling") is not None and not isinstance(value.get("sampling"), dict):
            raise ValueError(f"planned test {index} sampling must be an object")
        if value.get("thresholds") is not None and not isinstance(value.get("thresholds"), dict):
            raise ValueError(f"planned test {index} thresholds must be an object")
        value["rcm_id"] = rcm_id
        value["rcm_refs"] = [rcm_id]
        normalized.append(value)
    return {"planned_tests": normalized}


def planned_tests(runner, bundle: context_bundles.ContextBundle, rcm_id: str) -> list[dict]:
    payload = runner.llm_json(
        PLANNED_TEST_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": [f"rcm:{rcm_id}"], "context_metrics": bundle.metrics()},
        validator=lambda value: validate_planned_tests(value, rcm_id),
    )
    return payload["planned_tests"]


def validate_data_test(payload: dict) -> dict:
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
    return {"data_test": value}


def data_test_spec(runner, bundle: context_bundles.ContextBundle, parent_refs: list[str]) -> dict:
    payload = runner.llm_json(
        DATA_TEST_SPEC_SYSTEM,
        bundle.serialized(),
        activity={"artifact_refs": parent_refs, "context_metrics": bundle.metrics()},
        validator=validate_data_test,
    )
    return dict(payload["data_test"])


def validate_document_test(payload: dict) -> dict:
    value = payload.get("document_test")
    if not isinstance(value, dict):
        raise ValueError("document_test must be an object")
    if value.get("kind") not in {"vouching", "attribute", "review", "qa"}:
        raise ValueError("document_test kind is unsupported")
    if not str(value.get("title") or "").strip():
        raise ValueError("document_test title is required")
    items = value.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("document_test items must be non-empty")
    kind = value["kind"]
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or not str(item.get("label") or "").strip():
            raise ValueError(f"document_test item {index} needs a label")
        if kind == "vouching" and not item.get("checks"):
            raise ValueError(f"document_test item {index} needs comparison checks")
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
        validator=validate_document_test,
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
