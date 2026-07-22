"""Domain-neutral golden behavior for the workflow scheduler extraction.

The synthetic catalog workflow deliberately has no audit imports or workspace
fixture.  Phase 6 can run these tests against the extracted runtime scheduler
while the production audit workflow remains on the current runner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.agent import workflow


SOURCES_READY = "catalog.sources_ready"
RECORDS_READY = "catalog.records_ready"
PREVIEW_READY = "catalog.preview_ready"
INDEX_READY = "catalog.index_ready"
PUBLISHED = "catalog.published"


@dataclass(frozen=True)
class SyntheticCatalog:
    ready_outcomes: frozenset[str]
    records: tuple[tuple[str, int], ...] = (
        ("alpha", 10),
        ("beta", 20),
        ("gamma", 30),
    )


def _readiness(outcome: str):
    def assess(catalog: SyntheticCatalog, _scope: dict) -> workflow.Readiness:
        if outcome in catalog.ready_outcomes:
            return workflow.Readiness(
                "satisfied",
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
