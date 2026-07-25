"""The audit agent: two schedulers, one durable run store, one router.

A request becomes exactly one durable run. ``routing`` classifies it once and
persists the selected engine before the worker thread starts, ``runner``
launches the thread and owns the control surface, and the engine does the work:

    routing              one classifier, one normalized route, one engine
    runtime/             RunRuntime, ModelGateway, the domain-neutral
                         WorkflowRunner, the unit pipeline, and interactions
    action_runner        the bounded action-DAG scheduler
    intake_runner        the one retained protocol runner (folder intake)
    workflows/           authoritative capability graphs
    capabilities/        readiness and semantic unit expansion per graph
    context/             declared context presets, resolver, and manifests
    workers/             registered model workers (bundle in, proposal out)
    executors/           registered deterministic, conflict-aware commits
    store                durable run persistence (run.json + events.jsonl)
    actions / ledger     the registered action catalog and its DAG validation
    suggest / joins      deterministic local diagnostics used by both engines
"""
