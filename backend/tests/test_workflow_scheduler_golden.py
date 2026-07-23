"""Domain-neutral golden behavior for the workflow scheduler extraction.

The synthetic catalog workflow deliberately has no audit imports or workspace
fixture.  Phase 6 can run these tests against the extracted runtime scheduler
while the production audit workflow remains on the current runner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.agent import workflow
from app.agent.runtime.workflow_runner import (
    CapabilityExecution,
    CapabilityExecutionRegistry,
    DeterministicUnitResult,
    FinishProjection,
    WorkflowRunner,
)


SOURCES_READY = "catalog.sources_ready"
RECORDS_READY = "catalog.records_ready"
PREVIEW_READY = "catalog.preview_ready"
INDEX_READY = "catalog.index_ready"
PUBLISHED = "catalog.published"


@dataclass(frozen=True)
class SyntheticCatalog:
    ready_outcomes: frozenset[str]
    stale_outcomes: frozenset[str] = frozenset()
    records: tuple[tuple[str, int], ...] = (
        ("alpha", 10),
        ("beta", 20),
        ("gamma", 30),
    )


class SyntheticRuntime:
    def __init__(self, run: dict):
        self.run = run
        self.events: list[tuple[str, dict]] = []
        self.saves = 0
        self.clock = 0

    def save(self) -> None:
        self.saves += 1

    def emit(self, type_: str, data: dict) -> None:
        self.events.append((type_, data))

    def utcnow(self) -> str:
        self.clock += 1
        return f"2026-07-22T10:00:{self.clock:02d}Z"

    def mark_started(self) -> str:
        self.run["started"] = self.utcnow()
        return self.run["started"]

    def mark_finished(self) -> str:
        self.run["finished"] = self.utcnow()
        return self.run["finished"]

    def set_status(self, status: str) -> None:
        self.run["status"] = status

    def checkpoint(self, **_kwargs) -> None:
        return None


def _readiness(outcome: str):
    def assess(catalog: SyntheticCatalog, _scope: dict) -> workflow.Readiness:
        if outcome in catalog.ready_outcomes:
            return workflow.Readiness(
                "satisfied",
                details={"artifact_ref": f"synthetic:{outcome}"},
            )
        if outcome in catalog.stale_outcomes:
            return workflow.Readiness(
                "stale",
                reasons=("legacy source hash changed",),
                details={"artifact_ref": f"synthetic:{outcome}"},
            )
        return workflow.Readiness("missing", reasons=(f"{outcome} is absent",))

    return assess


def _single_unit(unit_id: str, kind: str, title: str, input_payload: object):
    def expand(_catalog: SyntheticCatalog, _scope: dict) -> list[workflow.UnitSpec]:
        return [
            workflow.UnitSpec(
                id=unit_id,
                kind=kind,
                title=title,
                input_payload=input_payload,
            )
        ]

    return expand


def synthetic_registry() -> workflow.CapabilityRegistry:
    """Build a small non-audit DAG with fan-out and parallel branches."""
    registry = workflow.CapabilityRegistry()
    registry.register(
        workflow.Capability(
            id=SOURCES_READY,
            stage_id="stage:catalog-sources",
            title="Discover catalog sources",
            worker_kind="synthetic.discover_sources",
            depends_on=(),
            readiness=_readiness(SOURCES_READY),
            expand_units=_single_unit(
                "source:catalog",
                "catalog_source",
                "Discover the catalog source",
                {"source": "catalog"},
            ),
        )
    )

    def record_units(
        catalog: SyntheticCatalog,
        _scope: dict,
    ) -> list[workflow.UnitSpec]:
        return [
            workflow.UnitSpec(
                id=workflow.semantic_unit_id("catalog-record", record_id),
                kind="catalog_record",
                title=f"Normalize {record_id}",
                parent_refs=("source:catalog",),
                input_payload={"record_id": record_id, "value": value},
            )
            for record_id, value in sorted(catalog.records)
        ]

    registry.register(
        workflow.Capability(
            id=RECORDS_READY,
            stage_id="stage:catalog-records",
            title="Normalize catalog records",
            worker_kind="synthetic.normalize_record",
            depends_on=(SOURCES_READY,),
            readiness=_readiness(RECORDS_READY),
            expand_units=record_units,
        )
    )
    record_refs = (
        "catalog-record:alpha",
        "catalog-record:beta",
        "catalog-record:gamma",
    )
    registry.register(
        workflow.Capability(
            id=PREVIEW_READY,
            stage_id="stage:catalog-preview",
            title="Render catalog preview",
            worker_kind="synthetic.render_preview",
            depends_on=(RECORDS_READY,),
            readiness=_readiness(PREVIEW_READY),
            expand_units=_single_unit(
                "preview:html",
                "catalog_preview",
                "Render the HTML preview",
                {"format": "html"},
            ),
        )
    )
    registry.register(
        workflow.Capability(
            id=INDEX_READY,
            stage_id="stage:catalog-index",
            title="Build catalog index",
            worker_kind="synthetic.build_index",
            depends_on=(RECORDS_READY,),
            readiness=_readiness(INDEX_READY),
            expand_units=lambda _catalog, _scope: [
                workflow.UnitSpec(
                    id="index:catalog",
                    kind="catalog_index",
                    title="Build the catalog index",
                    parent_refs=record_refs,
                    input_payload={"index": "catalog"},
                )
            ],
        )
    )
    registry.register(
        workflow.Capability(
            id=PUBLISHED,
            stage_id="stage:catalog-publish",
            title="Publish catalog",
            worker_kind="synthetic.publish",
            depends_on=(PREVIEW_READY, INDEX_READY),
            readiness=_readiness(PUBLISHED),
            expand_units=lambda _catalog, _scope: [
                workflow.UnitSpec(
                    id="publish:catalog",
                    kind="catalog_publish",
                    title="Publish the catalog",
                    parent_refs=("preview:html", "index:catalog"),
                )
            ],
        )
    )
    return registry


def synthetic_executions(
    registry: workflow.CapabilityRegistry,
    executor,
) -> CapabilityExecutionRegistry:
    executions = CapabilityExecutionRegistry()
    for capability in registry.all():
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash="sha256:" + "1" * 64,
                transitional_batch_executor=(
                    lambda _runner, stage, units, execute=executor: execute(stage, units)
                ),
            )
        )
    return executions


def _unit_projection(stages: list[dict]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    return [
        (
            stage["capability"],
            [
                (unit["id"], unit["kind"], unit["input_sha1"])
                for unit in stage["units"]
            ],
        )
        for stage in stages
    ]


def test_synthetic_registry_dependency_closure_is_golden():
    registry = synthetic_registry()

    assert registry.closure([PUBLISHED]) == [
        SOURCES_READY,
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]
    assert {
        capability.id: capability.depends_on for capability in registry.all()
    } == {
        SOURCES_READY: (),
        RECORDS_READY: (SOURCES_READY,),
        PREVIEW_READY: (RECORDS_READY,),
        INDEX_READY: (RECORDS_READY,),
        PUBLISHED: (PREVIEW_READY, INDEX_READY),
    }


def test_synthetic_registry_readiness_and_dependency_blocking_are_golden():
    registry = synthetic_registry()
    catalog = SyntheticCatalog(
        ready_outcomes=frozenset({SOURCES_READY, PREVIEW_READY})
    )

    assert registry.workflow_state(catalog) == {
        SOURCES_READY: {
            "state": "satisfied",
            "artifact_ref": f"synthetic:{SOURCES_READY}",
        },
        RECORDS_READY: {
            "state": "missing",
            "reasons": [f"{RECORDS_READY} is absent"],
        },
        PREVIEW_READY: {
            "state": "blocked",
            "artifact_ref": f"synthetic:{PREVIEW_READY}",
            "blocking_on": [RECORDS_READY],
        },
        INDEX_READY: {
            "state": "blocked",
            "reasons": [f"{INDEX_READY} is absent"],
            "blocking_on": [RECORDS_READY],
        },
        PUBLISHED: {
            "state": "blocked",
            "reasons": [f"{PUBLISHED} is absent"],
            "blocking_on": [PREVIEW_READY, INDEX_READY],
        },
    }


def test_synthetic_materialization_and_semantic_units_are_golden_and_stable():
    registry = synthetic_registry()
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))

    first = workflow.materialize(registry, catalog, [PUBLISHED])
    reordered = SyntheticCatalog(
        ready_outcomes=frozenset({SOURCES_READY}),
        records=tuple(reversed(catalog.records)),
    )
    second = workflow.materialize(registry, reordered, [PUBLISHED])

    resolved, stages, reused = first
    assert resolved == [
        SOURCES_READY,
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]
    assert reused == [SOURCES_READY]
    assert [stage["id"] for stage in stages] == [
        "stage:catalog-records",
        "stage:catalog-preview",
        "stage:catalog-index",
        "stage:catalog-publish",
    ]
    assert _unit_projection(stages) == [
        (
            RECORDS_READY,
            [
                (
                    "catalog-record:alpha",
                    "catalog_record",
                    "a0c1633b2bffdc6e0940f143cc5059a4f7e410b5",
                ),
                (
                    "catalog-record:beta",
                    "catalog_record",
                    "eb49e8838e5a623e3078ddbe5c9a6eaeecc65b5f",
                ),
                (
                    "catalog-record:gamma",
                    "catalog_record",
                    "4e3080e84336a13d05a92ec4edee47f667bc1346",
                ),
            ],
        ),
        (
            PREVIEW_READY,
            [
                (
                    "preview:html",
                    "catalog_preview",
                    "0dc137db7d963b04395eba622d2644a2e9950ca1",
                )
            ],
        ),
        (
            INDEX_READY,
            [
                (
                    "index:catalog",
                    "catalog_index",
                    "aafac2609b2b8b19910a5f349749506070e65186",
                )
            ],
        ),
        (
            PUBLISHED,
            [
                (
                    "publish:catalog",
                    "catalog_publish",
                    "8c47283eccbaffdedff35496bd007bd740aa35ec",
                )
            ],
        ),
    ]
    assert _unit_projection(second[1]) == _unit_projection(stages)


def test_synthetic_stable_all_settled_is_ordered_and_failure_isolated():
    units = [
        {"id": "catalog-record:gamma"},
        {"id": "catalog-record:alpha"},
        {"id": "catalog-record:beta"},
    ]
    completed: list[str] = []

    def worker(unit: dict) -> str:
        delays = {
            "catalog-record:alpha": 0.02,
            "catalog-record:beta": 0.01,
            "catalog-record:gamma": 0.0,
        }
        time.sleep(delays[unit["id"]])
        if unit["id"] == "catalog-record:beta":
            raise RuntimeError("synthetic record failure")
        return f"proposal:{unit['id']}"

    settled = workflow.stable_all_settled(
        units,
        worker,
        max_workers=3,
        on_settled=lambda unit, _value, _error: completed.append(unit["id"]),
    )

    assert set(completed) == {unit["id"] for unit in units}
    assert [unit["id"] for unit, _value, _error in settled] == [
        "catalog-record:alpha",
        "catalog-record:beta",
        "catalog-record:gamma",
    ]
    assert [value for _unit, value, error in settled if error is None] == [
        "proposal:catalog-record:alpha",
        "proposal:catalog-record:gamma",
    ]
    assert [str(error) for _unit, _value, error in settled if error is not None] == [
        "synthetic record failure"
    ]


def test_synthetic_recovery_requeues_only_interrupted_units_and_keeps_sidecars():
    proposal_ref = {
        "path": "proposals/catalog-record%3Aalpha.json",
        "sha256": "a" * 64,
    }
    receipt_ref = {
        "path": "receipts/catalog-record%3Abeta.json",
        "sha256": "b" * 64,
    }
    workflow_state = {
        "stages": [
            {
                "id": "stage:catalog-records",
                "status": "running",
                "units": [
                    {
                        "id": "catalog-record:alpha",
                        "status": "running",
                        "attempts": 1,
                        "started_at": "2026-07-22T10:00:00Z",
                        "error": "interrupted provider call",
                        "proposal_sidecar": proposal_ref,
                        "receipt_sidecar": None,
                    },
                    {
                        "id": "catalog-record:beta",
                        "status": "succeeded",
                        "attempts": 1,
                        "started_at": "2026-07-22T09:59:00Z",
                        "finished_at": "2026-07-22T09:59:30Z",
                        "error": None,
                        "proposal_sidecar": None,
                        "receipt_sidecar": receipt_ref,
                    },
                ],
            },
            {
                "id": "stage:catalog-publish",
                "status": "awaiting_confirmation",
                "units": [
                    {
                        "id": "publish:catalog",
                        "status": "awaiting_confirmation",
                        "attempts": 1,
                        "started_at": "2026-07-22T10:01:00Z",
                        "error": None,
                    }
                ],
            },
        ]
    }

    workflow.recovery(workflow_state)

    interrupted, committed = workflow_state["stages"][0]["units"]
    assert workflow_state["stages"][0]["status"] == "queued"
    assert interrupted == {
        "id": "catalog-record:alpha",
        "status": "queued",
        "attempts": 1,
        "started_at": None,
        "error": None,
        "proposal_sidecar": proposal_ref,
        "receipt_sidecar": None,
    }
    assert committed["status"] == "succeeded"
    assert committed["receipt_sidecar"] == receipt_ref
    assert workflow.stage_counts(workflow_state["stages"][0]) == {
        "queued": 1,
        "succeeded": 1,
        "total": 2,
    }
    assert workflow_state["stages"][1]["status"] == "awaiting_confirmation"
    assert workflow_state["stages"][1]["units"][0]["status"] == (
        "awaiting_confirmation"
    )


def test_extracted_scheduler_materializes_transitions_and_finishes_generically():
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))
    run = {
        "id": "run_synthetic",
        "command": {"status": "queued"},
        "limits": {"max_execution_attempts": 2, "max_units_per_stage": 20},
    }
    runtime = SyntheticRuntime(run)
    runner: WorkflowRunner

    def complete(stage: dict, units: list[dict]) -> None:
        for unit in units:
            runner.set_unit(stage, unit, "running")
            runner.set_unit(
                stage,
                unit,
                "succeeded",
                result_refs=[f"synthetic:{unit['id']}"],
            )

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=catalog,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=synthetic_executions(registry, complete),
        finish_evaluator=lambda _subject, _workflow, _stages: FinishProjection(
            summary_markdown="# Synthetic catalog complete\n"
        ),
    )

    state = runner.materialize([PUBLISHED])
    runner.execute()

    assert state["resolved_outcomes"] == [
        SOURCES_READY,
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]
    assert state["reused_outcomes"] == [SOURCES_READY]
    assert [stage["status"] for stage in state["stages"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert all(
        unit["status"] == "succeeded"
        for stage in state["stages"]
        for unit in stage["units"]
    )
    assert run["status"] == "completed"
    assert run["command"]["status"] == "completed"
    assert run["summary_markdown"] == "# Synthetic catalog complete\n"
    assert runtime.events[-1] == ("summary_ready", {"run_id": "run_synthetic"})


def test_extracted_scheduler_blocks_dependencies_and_folds_partial_failure():
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))
    run = {
        "id": "run_partial",
        "command": {"status": "queued"},
        "limits": {"max_execution_attempts": 2, "max_units_per_stage": 20},
    }
    runtime = SyntheticRuntime(run)
    runner: WorkflowRunner

    def complete_or_fail(stage: dict, units: list[dict]) -> None:
        for unit in units:
            runner.set_unit(stage, unit, "running")
            if unit["id"] == "catalog-record:beta":
                runner.set_unit(stage, unit, "failed", error="invalid record")
            else:
                runner.set_unit(stage, unit, "succeeded")

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=catalog,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=synthetic_executions(registry, complete_or_fail),
    )
    runner.materialize([PUBLISHED])

    runner.execute()

    stages = run["workflow"]["stages"]
    assert stages[0]["status"] == "failed"
    assert [stage["status"] for stage in stages[1:]] == [
        "blocked",
        "blocked",
        "blocked",
    ]
    assert run["workflow"]["next_outcomes"] == [
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]
    assert run["status"] == "failed"
    assert run["error"] == "invalid record"


def test_extracted_scheduler_reexpands_downstream_units_after_upstream_change():
    initial = SyntheticCatalog(ready_outcomes=frozenset(), records=())
    current = {"catalog": initial}
    run = {
        "id": "run_dynamic_expansion",
        "command": {"status": "queued"},
        "limits": {"max_execution_attempts": 2, "max_units_per_stage": 20},
    }
    runtime = SyntheticRuntime(run)
    runner: WorkflowRunner

    def complete(stage: dict, units: list[dict]) -> None:
        if stage["capability"] == SOURCES_READY:
            current["catalog"] = SyntheticCatalog(
                ready_outcomes=frozenset({SOURCES_READY}),
                records=(("delta", 40), ("epsilon", 50)),
            )
        for unit in units:
            runner.set_unit(stage, unit, "running")
            runner.set_unit(stage, unit, "succeeded")

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=initial,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=synthetic_executions(registry, complete),
        refresh_subject=lambda: current["catalog"],
    )
    state = runner.materialize([PUBLISHED], generation_mode="force")
    records_stage = next(
        stage for stage in state["stages"] if stage["capability"] == RECORDS_READY
    )
    assert records_stage["units"] == []

    runner.execute()

    assert [unit["id"] for unit in records_stage["units"]] == [
        "catalog-record:delta",
        "catalog-record:epsilon",
    ]
    assert {unit["status"] for unit in records_stage["units"]} == {"succeeded"}
    assert run["status"] == "completed"


def test_extracted_scheduler_settles_in_parallel_and_commits_in_semantic_order():
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))
    run = {
        "id": "run_commit_order",
        "command": {"status": "queued"},
        "limits": {
            "max_execution_attempts": 2,
            "max_units_per_stage": 20,
            "max_llm_concurrency": 3,
        },
    }
    runtime = SyntheticRuntime(run)
    completion_order: list[str] = []
    commit_order: list[str] = []
    runner: WorkflowRunner

    def complete(stage: dict, units: list[dict]) -> None:
        if stage["capability"] != RECORDS_READY:
            for unit in units:
                runner.set_unit(stage, unit, "running")
                runner.set_unit(stage, unit, "succeeded")
            return

        def generate(unit: dict) -> str:
            delay = {
                "catalog-record:alpha": 0.03,
                "catalog-record:beta": 0.01,
                "catalog-record:gamma": 0.0,
            }[unit["id"]]
            time.sleep(delay)
            completion_order.append(unit["id"])
            return unit["id"]

        for unit, proposal, error in runner.stable_all_settled(units, generate):
            assert error is None
            assert proposal == unit["id"]
            commit_order.append(unit["id"])
            runner.set_unit(stage, unit, "running")
            runner.set_unit(stage, unit, "succeeded")

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=catalog,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=synthetic_executions(registry, complete),
    )
    runner.materialize([PUBLISHED])
    runner.execute()

    assert completion_order[0] == "catalog-record:gamma"
    assert commit_order == [
        "catalog-record:alpha",
        "catalog-record:beta",
        "catalog-record:gamma",
    ]
    assert run["status"] == "completed"


def test_extracted_scheduler_preserves_domain_projected_next_outcomes():
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))
    run = {
        "id": "run_next_outcomes",
        "command": {"status": "queued"},
        "limits": {"max_execution_attempts": 2, "max_units_per_stage": 20},
    }
    runtime = SyntheticRuntime(run)
    runner: WorkflowRunner

    def complete(stage: dict, units: list[dict]) -> None:
        for unit in units:
            runner.set_unit(stage, unit, "running")
            runner.set_unit(stage, unit, "succeeded")

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=catalog,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=synthetic_executions(registry, complete),
        finish_evaluator=lambda _subject, _state, _stages: FinishProjection(
            next_outcomes=(INDEX_READY, PUBLISHED, INDEX_READY),
            terminal_status="completed_with_open_items",
        ),
    )
    runner.materialize([PUBLISHED])
    runner.execute()

    assert run["workflow"]["next_outcomes"] == [INDEX_READY, PUBLISHED]
    assert run["status"] == "completed_with_open_items"
    assert run["command"]["status"] == "completed_with_open_items"


def test_generation_modes_reuse_without_currency_claim_and_force_full_closure():
    catalog = SyntheticCatalog(
        ready_outcomes=frozenset(
            {SOURCES_READY, RECORDS_READY, PREVIEW_READY, INDEX_READY}
        ),
        stale_outcomes=frozenset({PUBLISHED}),
    )
    reuse_run = {"id": "reuse", "command": {"status": "queued"}}
    reuse_runner = WorkflowRunner(
        subject=catalog,
        run=reuse_run,
        runtime=SyntheticRuntime(reuse_run),
        registry=synthetic_registry(),
        executions=synthetic_executions(
            synthetic_registry(), lambda _stage, _units: None
        ),
    )

    reused = reuse_runner.materialize([PUBLISHED])

    assert reused["generation_mode"] == "reuse_existing"
    assert reused["stages"] == []
    assert reused["reused_outcomes"] == [
        SOURCES_READY,
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]
    assert reused["reused_outcome_details"] == [
        {"capability": capability_id, "currency_status": "not_assessed"}
        for capability_id in reused["reused_outcomes"]
    ]

    force_run = {"id": "force", "command": {"status": "queued"}}
    force_runner = WorkflowRunner(
        subject=catalog,
        run=force_run,
        runtime=SyntheticRuntime(force_run),
        registry=(force_registry := synthetic_registry()),
        executions=synthetic_executions(
            force_registry, lambda _stage, _units: None
        ),
    )
    forced = force_runner.materialize([PUBLISHED], generation_mode="force")

    assert forced["generation_mode"] == "force"
    assert forced["reused_outcomes"] == []
    assert [stage["capability"] for stage in forced["stages"]] == [
        SOURCES_READY,
        RECORDS_READY,
        PREVIEW_READY,
        INDEX_READY,
        PUBLISHED,
    ]


def test_generation_mode_normalization_is_explicit_and_deterministic():
    assert workflow.command_generation_mode({"text": "Prepare the APM"}) == (
        "reuse_existing"
    )
    assert workflow.command_generation_mode({"text": "Improve the APM"}) == "force"
    assert workflow.command_generation_mode({"text": "Generate the APM again"}) == (
        "force"
    )
    assert workflow.command_generation_mode({"text": "Regenerate the RCM"}) == "force"
    assert workflow.command_generation_mode({"text": "Refresh audit report"}) == "force"
    assert workflow.command_generation_mode(
        {"text": "Regenerate the APM", "generation_mode": "reuse_existing"}
    ) == "reuse_existing"

    try:
        workflow.normalize_generation_mode("missing_or_stale")
    except Exception as error:
        assert "reuse_existing" in str(error)
    else:
        raise AssertionError("legacy generation modes must fail closed")


def _deterministic_executions(
    registry: workflow.CapabilityRegistry,
    executor,
) -> CapabilityExecutionRegistry:
    executions = CapabilityExecutionRegistry()
    for capability in registry.all():
        executions.register(
            CapabilityExecution(
                capability_id=capability.id,
                implementation_hash="sha256:" + "4" * 64,
                deterministic_executor=executor,
            )
        )
    return executions


def test_extracted_scheduler_runs_deterministic_bindings_and_folds():
    catalog = SyntheticCatalog(ready_outcomes=frozenset({SOURCES_READY}))
    run = {
        "id": "run_deterministic",
        "command": {"status": "queued"},
        "limits": {"max_execution_attempts": 2, "max_units_per_stage": 20},
    }
    runtime = SyntheticRuntime(run)
    seen: list[tuple[str, str]] = []

    def deterministic(subject, run_arg, capability, _stage, unit):
        # The scheduler hands the deterministic executor the local subject and
        # run without wrapping either in a model pipeline.
        assert subject is catalog
        assert run_arg is run
        seen.append((capability.id, unit["id"]))
        if capability.id == PUBLISHED:
            return DeterministicUnitResult("blocked", (), "publish gate is open")
        return DeterministicUnitResult(
            "succeeded", (f"synthetic:{unit['id']}",)
        )

    registry = synthetic_registry()
    runner = WorkflowRunner(
        subject=catalog,
        run=run,
        runtime=runtime,
        registry=registry,
        executions=_deterministic_executions(registry, deterministic),
    )
    runner.materialize([PUBLISHED])
    runner.execute()

    stages = {stage["capability"]: stage for stage in run["workflow"]["stages"]}
    assert stages[RECORDS_READY]["status"] == "succeeded"
    assert {unit["status"] for unit in stages[RECORDS_READY]["units"]} == {
        "succeeded"
    }
    assert stages[RECORDS_READY]["units"][0]["result_refs"] == [
        "synthetic:catalog-record:alpha"
    ]
    # A non-succeeded deterministic status folds through the generic stage logic.
    assert stages[PUBLISHED]["status"] == "review_required"
    assert stages[PUBLISHED]["units"][0]["status"] == "blocked"
    assert stages[PUBLISHED]["units"][0]["error"] == "publish gate is open"
    assert run["status"] == "completed_with_open_items"
    assert (RECORDS_READY, "catalog-record:alpha") in seen
    assert (PUBLISHED, "publish:catalog") in seen


def test_capability_execution_requires_exactly_one_binding():
    valid = CapabilityExecution(
        capability_id="x",
        implementation_hash="sha256:" + "5" * 64,
        deterministic_executor=lambda *_args: DeterministicUnitResult("succeeded"),
    )
    assert valid.deterministic_backed
    assert not valid.pipeline_backed

    for kwargs in (
        {},
        {
            "deterministic_executor": lambda *_a: DeterministicUnitResult(
                "succeeded"
            ),
            "transitional_batch_executor": lambda _r, _s, _u: None,
        },
    ):
        try:
            CapabilityExecution(
                capability_id="x",
                implementation_hash="sha256:" + "5" * 64,
                **kwargs,
            )
        except ValueError as error:
            assert "exactly one" in str(error)
        else:
            raise AssertionError("invalid binding count must be rejected")


def test_capability_execution_registry_rejects_duplicates_and_mismatched_graphs():
    registry = synthetic_registry()
    executions = synthetic_executions(registry, lambda _stage, _units: None)

    try:
        executions.register(
            CapabilityExecution(
                capability_id=PUBLISHED,
                implementation_hash="sha256:" + "2" * 64,
                transitional_batch_executor=lambda _runner, _stage, _units: None,
            )
        )
    except ValueError as error:
        assert "already registered" in str(error)
    else:
        raise AssertionError("duplicate capability execution must be rejected")

    incomplete = CapabilityExecutionRegistry()
    incomplete.register(
        CapabilityExecution(
            capability_id=PUBLISHED,
            implementation_hash="sha256:" + "3" * 64,
            transitional_batch_executor=lambda _runner, _stage, _units: None,
        )
    )
    try:
        incomplete.validate(registry)
    except ValueError as error:
        assert "missing" in str(error)
        assert SOURCES_READY in str(error)
    else:
        raise AssertionError("incomplete execution registry must be rejected")
