"""Generic capability graph and durable workflow-v2 state primitives."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..workspaces import Workspace, WorkspaceError, slugify
from . import store

WORKFLOW_DEFINITION = "audit_workflow_v2"
READINESS_STATES = {"satisfied", "missing", "stale", "blocked", "review_required"}
UNIT_STATUSES = {
    "queued", "running", "succeeded", "failed", "blocked", "awaiting_input",
    "awaiting_confirmation", "conflict", "skipped", "cancelled",
}
TERMINAL_UNIT_STATUSES = UNIT_STATUSES - {"queued", "running"}


def canonical_sha1(value: object) -> str:
    return hashlib.sha1(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def semantic_unit_id(kind: str, *refs: object) -> str:
    suffix = ":".join(slugify(str(ref)) for ref in refs if str(ref or "").strip())
    return f"{kind}:{suffix}" if suffix else kind


@dataclass(frozen=True)
class Readiness:
    state: str
    reasons: tuple[str, ...] = ()
    blocking_on: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in READINESS_STATES:
            raise ValueError(f"Unknown workflow readiness state '{self.state}'.")

    @property
    def satisfied(self) -> bool:
        return self.state == "satisfied"

    def payload(self) -> dict:
        return {
            "state": self.state,
            **({"reasons": list(self.reasons)} if self.reasons else {}),
            **({"blocking_on": list(self.blocking_on)} if self.blocking_on else {}),
            **self.details,
        }


@dataclass(frozen=True)
class UnitSpec:
    id: str
    kind: str
    title: str
    parent_refs: tuple[str, ...] = ()
    input_payload: object | None = None

    @property
    def input_sha1(self) -> str:
        return canonical_sha1(self.input_payload if self.input_payload is not None else self.parent_refs)


@dataclass(frozen=True)
class Capability:
    id: str
    stage_id: str
    title: str
    worker_kind: str
    depends_on: tuple[str, ...]
    readiness: Callable[[Workspace, dict], Readiness]
    expand_units: Callable[[Workspace, dict], list[UnitSpec]]
    context: object | None = None
    barrier: str = "all_settled_then_validate"
    commit_policy: str = "serialized"
    approval_policy: str = "auto_or_stage_batch"
    invalidate_on: tuple[str, ...] = ()


class CapabilityRegistry:
    def __init__(self) -> None:
        self._values: dict[str, Capability] = {}

    def register(self, capability: Capability) -> Capability:
        if capability.id in self._values:
            raise ValueError(f"Capability '{capability.id}' is already registered.")
        self._values[capability.id] = capability
        return capability

    def get(self, capability_id: str) -> Capability:
        try:
            return self._values[capability_id]
        except KeyError as error:
            raise WorkspaceError(f"Unknown workflow outcome '{capability_id}'.") from error

    def all(self) -> list[Capability]:
        return list(self._values.values())

    def closure(self, requested: Iterable[str]) -> list[str]:
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(capability_id: str) -> None:
            if capability_id in visited:
                return
            if capability_id in visiting:
                raise RuntimeError(f"Capability dependency cycle at '{capability_id}'.")
            visiting.add(capability_id)
            capability = self.get(capability_id)
            for dependency in capability.depends_on:
                visit(dependency)
            visiting.remove(capability_id)
            visited.add(capability_id)
            ordered.append(capability_id)

        for item in requested:
            visit(str(item))
        return ordered

    def workflow_state(self, workspace: Workspace, scope: dict | None = None) -> dict[str, dict]:
        scope = dict(scope or {})
        result: dict[str, dict] = {}
        for capability in self.all():
            readiness = capability.readiness(workspace, scope)
            payload = readiness.payload()
            unmet = [dep for dep in capability.depends_on if result.get(dep, {}).get("state") != "satisfied"]
            if unmet:
                payload["state"] = "blocked"
                payload["blocking_on"] = unmet
            result[capability.id] = payload
        return result


def new_unit(spec: UnitSpec, capability_id: str) -> dict:
    return {
        "id": spec.id,
        "kind": spec.kind,
        "title": spec.title,
        "capability": capability_id,
        "parent_refs": list(spec.parent_refs),
        "status": "queued",
        "attempts": 0,
        "input_sha1": spec.input_sha1,
        "context_manifest": None,
        "proposal_sidecar": None,
        "receipt_sidecar": None,
        "result_refs": [],
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


def materialize(
    registry: CapabilityRegistry,
    workspace: Workspace,
    requested_outcomes: list[str],
    scope: dict | None = None,
) -> tuple[list[str], list[dict], list[str]]:
    """Resolve closure and materialize only missing/stale/review units.

    This is where the "action plan" for an audit command comes from — a
    dependency closure plus deterministic readiness, not a model. Walking the
    closure in topological order lets each decision see what earlier
    capabilities already scheduled.
    """
    scope = dict(scope or {})
    resolved = registry.closure(requested_outcomes)
    stages: list[dict] = []
    reused: list[str] = []
    scheduled: set[str] = set()
    for capability_id in resolved:
        capability = registry.get(capability_id)
        readiness = capability.readiness(workspace, scope)
        # Satisfied output is reused unless something upstream is about to
        # rewrite its inputs, or the auditor explicitly forced a refresh.
        dependency_will_change = any(dependency in scheduled for dependency in capability.depends_on)
        if (
            readiness.satisfied
            and not dependency_will_change
            and scope.get("refresh_policy", "missing_or_stale") != "force"
        ):
            reused.append(capability_id)
            continue
        # Fan the capability out into concrete units against the *current*
        # workspace. Unit ids are semantic (semantic_unit_id), so re-expanding
        # after a resume produces the same ids and the same work.
        specs = capability.expand_units(workspace, scope)
        stages.append(
            {
                "id": capability.stage_id,
                "capability": capability.id,
                "title": capability.title,
                "status": "queued",
                "barrier": capability.barrier,
                "units": [new_unit(spec, capability.id) for spec in specs],
                "readiness_before": readiness.payload(),
            }
        )
        scheduled.add(capability_id)
    return resolved, stages, reused


def recovery(workflow: dict) -> None:
    """Make interrupted workflow units resumable without repeating commits."""
    for stage in workflow.get("stages") or []:
        for unit in stage.get("units") or []:
            if unit.get("status") == "running":
                unit["status"] = "queued"
                unit["started_at"] = None
                unit["error"] = None
        statuses = {unit.get("status") for unit in stage.get("units") or []}
        if "running" in statuses:
            stage["status"] = "running"
        elif "queued" in statuses:
            stage["status"] = "queued"


def transition_unit(unit: dict, status: str, *, error: str | None = None, result_refs: list[str] | None = None) -> None:
    if status not in UNIT_STATUSES:
        raise ValueError(f"Unknown workflow unit status '{status}'.")
    previous = unit.get("status")
    allowed = {
        "queued": {"running", "failed", "skipped", "cancelled", "blocked", "conflict"},
        "running": TERMINAL_UNIT_STATUSES | {"queued"},
        "failed": {"queued", "running", "cancelled"},
        "blocked": {"queued", "cancelled"},
        "conflict": {"queued", "cancelled"},
        "awaiting_input": {"queued", "running", "cancelled"},
        "awaiting_confirmation": {"queued", "running", "cancelled"},
    }
    if previous != status and status not in allowed.get(previous, set()):
        raise ValueError(f"Illegal workflow-unit transition {previous!r} -> {status!r}.")
    now = store.utcnow()
    if status == "running":
        unit["started_at"] = now
        unit["finished_at"] = None
        unit["attempts"] = int(unit.get("attempts") or 0) + 1
    elif status in TERMINAL_UNIT_STATUSES:
        unit["finished_at"] = now
    unit["status"] = status
    unit["error"] = error
    if result_refs is not None:
        unit["result_refs"] = list(result_refs)


def stage_counts(stage: dict) -> dict[str, int]:
    statuses = [str(unit.get("status") or "queued") for unit in stage.get("units") or []]
    return {status: statuses.count(status) for status in UNIT_STATUSES if statuses.count(status)} | {
        "total": len(statuses)
    }


def stable_all_settled(
    units: list[dict],
    worker: Callable[[dict], Any],
    *,
    max_workers: int = 4,
    on_settled: Callable[[dict, Any | None, Exception | None], None] | None = None,
) -> list[tuple[dict, Any | None, Exception | None]]:
    """Execute independent compute/model work concurrently and return ID order.

    All-settled, never fail-fast: one bad RCM row must not cancel the other
    twenty in flight. Results are collected as they complete (so `on_settled`
    can persist each proposal immediately) but returned sorted by unit id, so
    the caller's commit order stays deterministic regardless of timing.
    """
    if not units:
        return []
    results: dict[str, tuple[Any | None, Exception | None]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(units)))) as executor:
        future_units = {executor.submit(worker, unit): unit for unit in units}
        for future in as_completed(future_units):
            unit = future_units[future]
            try:
                value, failure = future.result(), None
            except Exception as caught:  # all-settled: siblings keep running
                value = None
                failure = caught
            results[unit["id"]] = (value, failure)
            if on_settled is not None:
                on_settled(unit, value, failure)
    return [(unit, *results[unit["id"]]) for unit in sorted(units, key=lambda item: item["id"])]
