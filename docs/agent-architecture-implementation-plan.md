# Agent Architecture Implementation Plan

## 1. Purpose

This document turns [agent-architecture.md](agent-architecture.md) into an
incremental implementation plan. The goal is to simplify the agent framework
without breaking durable runs, audit behavior, privacy guarantees, approvals,
or the current frontend contract.

The target has two schedulers:

- `WorkflowRunner` schedules declarative capability graphs for durable
  outcomes.
- `ActionRunner` schedules bounded action DAGs for imperative artifact
  operations.

Both use `RunRuntime` through composition. Domain behavior is expressed by
capabilities, context declarations, workers, and executors rather than by
domain-specific runners or scheduler methods.

This is a strangler migration, not a rewrite. Each phase must leave the
application runnable and persisted runs recoverable. Old code is deleted only
after the replacement path has characterization tests and has become the only
writer for that behavior.

## 2. Outcomes And Non-Goals

### Required Outcomes

- One authoritative audit dependency graph in `agent/workflows/audit.py`.
- A domain-neutral `WorkflowRunner` with no APM, RCM, fieldwork, document,
  finding, dashboard, or report handlers.
- An `ActionRunner` that retains the useful generic action planner and ledger
  but contains no audit-lifecycle policy.
- Shared runtime services for persistence, events, checkpoints, budgets,
  controls, interactions, approvals, and model accounting.
- Auditable, versioned context declarations in every model-backed capability.
- Workers that only transform supplied context into validated proposals.
- Executors that deterministically commit proposals using parent hashes or
  compare-and-swap and return receipts.
- One document-analysis map/reduce implementation shared by standalone
  document analysis and audit planning.
- A declared exploratory data-analysis workflow for requests involving table
  relationships, joins, and relevant analysis generation.
- Compatibility for persisted v2 action runs and v3 workflow runs throughout
  the migration.
- Removal or isolation of v1 only after supported callers have migrated.

### Non-Goals

- Redesigning the assistant drawer or audit screens.
- Changing workspace domain schemas unless a migration requirement is
  identified and separately approved.
- Sending row-level table data to the provider.
- Replacing Polars, document extraction, report services, or other domain
  services that already have suitable deterministic APIs.
- Replacing the action catalog with arbitrary model tools.
- Deleting every compatibility projection in the first change.
- Combining v1 retirement with the initial v2/v3 separation.

## 3. Delivery Principles

1. Preserve behavior before reorganizing it. Add characterization tests before
   changing routing, dispatch, persisted state, or recovery.
2. Separate by responsibility, not by artifact name. Runners schedule;
   capabilities declare; resolvers gather; workers generate; executors mutate.
3. Move code before improving prompts. Prompt or context-quality changes mixed
   with structural changes make regressions difficult to attribute.
4. Keep persisted schemas additive during migration. New readers accept old
   shapes; new writers may emit compatibility projections until the frontend
   and recovery paths no longer need them.
5. Give each capability family one implementation before deleting the old
   caller. Compatibility adapters delegate to that implementation and do not
   fork domain logic.
6. Make privacy enforcement structural. Context permissions and budgets are
   validated before a worker is invoked, not left to prompt instructions.
7. Persist before side effects. Model proposals and context manifests are
   durable before approval or commit; executor receipts are durable after a
   successful commit.
8. Keep commits small enough to review and revert by phase or capability
   family.

## 4. Target Runtime Contracts

These contracts should be introduced early and kept small. Domain-specific
types must not leak into the runtime package.

### RunRuntime

`RunRuntime` is a composed service used by both runners. It owns:

- Run persistence and locking.
- Event emission and durable activity projection.
- Pause, resume, cancel, deadline, and checkpoint behavior.
- Approval and interaction lifecycle.
- Budget charging and dynamic limit updates.
- Provider concurrency and provenance accounting through `ModelGateway`.
- Proposal-sidecar and receipt persistence helpers.

It does not resolve dependencies, choose context, build prompts, or mutate
domain artifacts.

Initial extraction should wrap existing `BaseRunner` behavior rather than
rewrite it. `BaseRunner` can temporarily delegate to `RunRuntime` to keep leaf
runners working while the graph runners switch to composition.

### Capability

A capability declaration contains at least:

```python
Capability(
    id="planning.apm_ready",
    version=2,
    depends_on=("planning.context_ready",),
    readiness=apm_readiness,
    expand_units=single_workspace_unit,
    context=APM_CONTEXT,
    worker="apm_v2",
    executor="commit_apm_v1",
    approval="artifact_change",
    invalidate_on=("planning:context", "documents", "methodology"),
)
```

The concrete model may use callables or registry keys consistently, but it
must serialize a normalized definition identity. That identity participates
in readiness and proposal reuse.

### ContextSpec, ContextManifest, And ContextBundle

`ContextSpec` declares:

- Required and optional sources.
- Presets and selector parameters.
- Representations such as metadata, profiles, excerpts, or extracted text.
- Item, character, and estimated token limits.
- Per-source limits and truncation rules.
- Privacy permissions such as `allow_table_rows=False`.
- Named, versioned automatic selection strategies.

`ContextResolver` compiles presets, resolves sources, enforces policies, and
returns both a `ContextManifest` and a `ContextBundle`.

`ContextManifest` records references and hashes, never raw row data or full
document content. It includes selection reasons, omissions, truncations,
privacy decisions, source representations, resolver version, spec version,
and supplied-size metrics.

`ContextBundle` contains the bounded local material available to a worker. A
worker cannot access the workspace or context services to fetch more data.

### Worker

A worker owns:

- Context-to-message transformation.
- Prompt text and prompt version.
- Structured response schema.
- Semantic validation.
- Bounded repair instructions.

Its input is an immutable unit plus `ContextBundle`; its output is a validated
proposal. A worker cannot persist run state, schedule units, request approval,
or mutate a workspace.

### Executor

An executor owns:

- Proposal-to-domain command conversion.
- Parent-hash and workspace-revision checks.
- Deterministic validation.
- Workspace mutation through transactional domain APIs.
- Preservation of auditor-owned edits.
- Artifact references and a serializable receipt.
- Reconciliation after an interrupted commit.

An executor cannot call an LLM or append work to either scheduler.

### Registries

Use separate registries for:

- Capability definitions.
- Context presets and automatic selectors.
- Workers.
- Executors.
- Action definitions and action executors.

Registry validation runs at construction or application startup and rejects
missing references, duplicate IDs, dependency cycles, invalid privacy
combinations, and unversioned model-facing components.

## 5. Persisted State Strategy

### Compatibility Rule

Do not destructively rewrite old `run.json` documents. Runtime readers infer a
run engine from existing fields and persist an explicit engine for new runs:

```text
engine = "workflow" | "action" | "legacy_v1" | "intake_compat" |
         "doc_test_compat" | "document_analysis_compat"
engine_schema_version = <integer>
```

Existing `kind`, `schema_version`, `actions`, `workflow`, `plan`, and task
projections remain readable until their compatibility window closes.

### New Workflow Records

Each workflow run should persist:

- Workflow definition ID and version.
- Requested outcomes and normalized scope.
- Materialized capability stages.
- Semantic unit IDs and unit input hashes.
- Capability-definition hash.
- Context-policy hash and manifest reference.
- Worker ID, prompt version, and schema version.
- Proposal sidecar reference and proposal hash.
- Approval or interaction reference.
- Executor ID, receipt, and committed artifact references.
- Readiness result before and after execution.
- Failure, conflict, retry, and recovery state.

### Sidecar Layout

Preserve the current `AgentRuns/<run_id>/` root. Add versioned subdirectories
or typed sidecar names rather than moving all existing files immediately:

```text
AgentRuns/<run_id>/
|- run.json
|- events.jsonl
|- contexts/<unit_id>.json
|- proposals/<unit_id>.json
`- receipts/<unit_id>.json
```

Sidecar writers use atomic replacement. Sidecars carry enough hashes to reject
reuse after a capability, context policy, prompt, worker, schema, unit input,
or source changes.

### Recovery Rules

- A `running` model unit with no valid proposal returns to `ready`.
- A valid proposal sidecar skips the provider call.
- A proposed unit requiring approval returns to its approval checkpoint.
- A proposal with no receipt is reconciled against workspace postconditions
  before any repeat commit.
- A valid receipt marks the unit succeeded if its postcondition still holds.
- A changed parent or auditor-edited target becomes `conflict` or `stale`; it
  is not silently overwritten.
- Readiness is always re-evaluated after recovery and after each commit.

## 6. Phased Implementation

### Phase 0: Baseline And Characterization

**Objective:** Freeze the behavior that must survive the refactor.

**Work:**

- Document current run shapes for legacy v1, generic v2 actions, broad-audit
  commands promoted to v3, native v3 runs, intake, document test, and document
  analysis.
- Add fixtures containing representative persisted runs, including interrupted
  units, awaiting approvals, pending interactions, valid proposal sidecars,
  missing receipts, and completed actions.
- Characterize deterministic routing for all goal templates and common phrases.
- Characterize unknown-command fallback to the bounded router and generic
  action interpreter.
- Characterize current SSE event types, activity projections, approval records,
  interaction records, and assistant-chat run projections.
- Characterize v3 materialization, semantic unit stability, readiness skipping,
  staleness, bounded parallel execution, stable commit order, and conflict
  handling.
- Characterize generic action graph validation, repair, preconditions,
  idempotence, approvals, reconciliation, undo, and resume.
- Capture provider-call and privacy assertions: all calls are budgeted, table
  rows are absent, and provenance stores hashes rather than prompt content.

**Primary files:**

- `backend/tests/test_workflow_v2.py`
- `backend/tests/test_command_agent.py`
- `backend/tests/test_agent_runner.py`
- `backend/tests/test_agent_api.py`
- `backend/tests/test_assistant_chats.py`
- New persisted-run fixtures under `backend/tests/fixtures/agent_runs/`

**Exit gate:** Tests demonstrate that broad audit requests bypass the v2
interpreter, isolated mutations still use the action graph, and every supported
persisted run shape has an explicit resume expectation.

### Phase 1: Remove V2 Full-Audit Policy

**Objective:** Make the v2 scheduler a genuinely generic action runner before
changing class structure.

**Work:**

- Rename `CommandRunner` to `ActionRunner` or introduce `ActionRunner` as the
  canonical class with a temporary import alias for compatibility.
- Delete the broad-audit path from the action runner:
  `_prepare_planning`, `_ensure_full_audit_stages`,
  `_validate_full_audit_action_graph`, full-audit adaptive expansion, terminal
  action insertion, special summaries, and budget reservations.
- Remove `ORCHESTRATED_FULL_AUDIT_ACTION_TYPES`,
  `FULL_AUDIT_TERMINAL_RESERVE`, and `FULL_AUDIT_ADAPTIVE_RESERVE`.
- Remove `prepared_planning` and execution-manifest prompt clauses from the
  command interpreter contract.
- Make broad-audit and planning templates fail closed into workflow routing if
  they somehow reach the action runner. Do not let them fall back to a partial
  generic action graph.
- Keep all isolated action definitions and behavior, including planning
  artifact edits requested as specific operations.
- Retain readers for persisted v2 broad-audit runs. On resume, translate their
  requested goal and scope to a workflow run before execution; preserve the
  original run ID where feasible and emit a compatibility event.

**Primary files:**

- `backend/app/agent/command_runner.py` -> transitional `action_runner.py`
- `backend/app/agent/prompts.py`
- `backend/app/agent/runner.py`
- `backend/app/agent/workflow_runner.py`
- `backend/tests/test_command_agent.py`

**Deletion gate:** No reachable action-runner branch checks for a full audit,
prepares planning, reserves terminal audit actions, or inserts report/rollup/
verification stages.

### Phase 2: Remove Audit Policy From The Action Ledger

**Objective:** Make the action ledger domain-neutral.

**Work:**

- Remove `AUDIT_LIFECYCLE_STAGES` and `AUDIT_LIFECYCLE_RANK`.
- Remove `enforce_audit_lifecycle(...)`.
- Remove the `audit_lifecycle` parameter from `append_actions(...)` and all
  callers.
- Retain generic DAG cycle detection, explicit dependencies, created-target
  normalization, action preconditions, idempotency, failure propagation, and
  reconciliation.
- Convert tests that asserted audit lifecycle normalization into workflow graph
  tests. Keep action tests only for generic DAG semantics.

**Primary files:**

- `backend/app/agent/ledger.py`
- `backend/app/agent/action_runner.py`
- `backend/tests/test_command_agent.py`
- `backend/tests/test_workflow_v2.py`

**Exit gate:** `ledger.py` has no knowledge of APM, RCM, fieldwork, findings,
working papers, dashboard, reports, or audit completion.

### Phase 3: Introduce Shared Runtime Services

**Objective:** Separate scheduler algorithms from durable-run housekeeping.

**Work:**

- Add `agent/runtime/run_runtime.py` and move or delegate persistence, event,
  status, activity, budget, deadline, checkpoint, pause/cancel, approval, and
  interaction behavior from `BaseRunner`.
- Add `agent/runtime/model_gateway.py` as the only model-call entry point.
  Initially wrap the exact `BaseRunner._llm_content` behavior, including model
  profiles, semaphores, token estimates, retries, stage tags, debug telemetry,
  and hash-only provenance.
- Add `agent/runtime/interactions.py` for approval and structured-interaction
  state transitions if that extraction remains coherent after the first pass.
- Inject `RunRuntime` into `ActionRunner` and `WorkflowRunner`.
- Keep `BaseRunner` as a compatibility facade for v1 and leaf runners. Its
  methods delegate to a `RunRuntime`; do not maintain two implementations.
- Move thread creation, `_HANDLES`, workspace serialization, pending-command
  FIFO, and terminal crash guarantees only if doing so does not widen the
  phase. They may remain in `runner.py` as process-level orchestration while
  `RunRuntime` owns per-run services.

**Primary files:**

- New `backend/app/agent/runtime/` package
- `backend/app/agent/base.py`
- `backend/app/agent/runner.py`
- `backend/app/agent/action_runner.py`
- `backend/app/agent/workflow_runner.py`
- `backend/app/agent/store.py`

**Tests:**

- Both schedulers emit the same durable status and activity events through the
  runtime.
- Provider budgets and concurrency are shared across worker types.
- Time blocked on approval or interaction extends the deadline as before.
- Cancel, pause/resume, approval, retry, and queued follow-up behavior remain
  compatible through the API.
- A leaf compatibility runner and a graph runner can both use the runtime
  without inheriting from one another.

**Exit gate:** New graph-runner code contains no direct store writes or direct
provider calls outside the runtime APIs.

### Phase 4: Introduce Context Contracts And Resolver

**Objective:** Make capability context selection complete, bounded, inspectable,
and manually editable.

**Work:**

- Add typed models for `ContextSpec`, source declarations, representations,
  budgets, privacy policy, deterministic selectors, and `AutoSelect`.
- Add a preset registry. Implement concise presets such as
  `documents.policies`, but normalize them into the typed form before use.
- Add a selector registry. Every automatic selector has an ID, version,
  supported source type, deterministic tie-breaking, item limit, and reason
  output.
- Add `ContextResolver.resolve(workspace, capability, unit, scope)` returning a
  manifest and bundle.
- Implement manifest persistence through `RunRuntime` before the model call.
- Port existing bounded context builders behind adapters rather than copying
  them. Preserve current privacy choke points in `context_bundles.py`,
  `document_context.py`, `document_search.py`, `model_context.py`, and
  assistant context helpers where applicable.
- Reject undeclared sources, row-level table representations, unknown presets,
  unknown selector versions, and over-budget bundles before worker execution.
- Define deterministic ordering and truncation so the same inputs produce the
  same manifest and hash.

**Primary files:**

- New `backend/app/agent/context/model.py`
- New `backend/app/agent/context/presets.py`
- New `backend/app/agent/context/resolver.py`
- New `backend/app/agent/context/manifest.py`
- `backend/app/agent/context_bundles.py`
- `backend/app/document_context.py`
- `backend/app/document_search.py`
- `backend/app/model_context.py`

**Tests:**

- `documents.policies` selects only documents marked as policies.
- A named `auto` strategy is bounded, versioned, stable, and explains every
  selected item.
- Required-source absence blocks the unit; optional-source absence is recorded.
- Global and per-source budgets are enforced deterministically.
- Document truncation and omission are recorded in the manifest.
- Table metadata and profiles are permitted while table rows are rejected.
- Bundle content is local; persisted manifests and provider provenance contain
  references, hashes, metrics, and decisions only.
- Context spec or selector version changes invalidate proposal reuse.

**Exit gate:** At least one real workflow capability obtains all model context
through `ContextResolver`, and its worker has no workspace access.

### Phase 5: Introduce Worker And Executor Interfaces

**Objective:** Establish the model-generation/mutation boundary before moving
all audit stages.

**Work:**

- Add worker request/result types, worker registry validation, prompt/version
  metadata, response schema validation, and bounded repair handling.
- Add executor request/result types, executor registry validation, parent hash
  checks, receipts, and interrupted-commit reconciliation.
- Add a generic workflow unit pipeline:
  resolve context -> persist manifest -> invoke worker -> persist proposal ->
  approval if declared -> invoke executor -> persist receipt -> reevaluate
  readiness.
- Select `planning.apm_ready` as the first vertical slice because it exercises
  declared dependencies, document/methodology context, one model proposal,
  approval policy, CAS commit, quality validation, and staleness.
- Extract the APM prompt and validator from current planning helpers into a
  planning worker.
- Extract APM mutation and reconciliation into a planning executor.
- Keep output shape and workspace fields unchanged.

**Primary files:**

- New `backend/app/agent/workers/model.py`
- New `backend/app/agent/workers/planning.py`
- New `backend/app/agent/executors/model.py`
- New `backend/app/agent/executors/planning.py`
- `backend/app/agent/runtime/workflow_runner.py`
- `backend/app/agent/audit_workers.py`
- `backend/app/agent/command_runner.py`

**Tests:**

- Worker unit tests use constructed bundles and require no workspace fixture.
- Executor tests use proposals and workspace fixtures with no model stub.
- A worker cannot mutate workspace state.
- An executor has no gateway dependency and cannot call a model.
- Proposal persistence happens before approval and commit.
- Resume after proposal generation does not rebill the provider.
- Parent changes produce a conflict instead of overwriting the APM.

**Exit gate:** APM generation runs end to end through declared context, a
registered worker, a proposal sidecar, and a registered executor.

### Phase 6: Make WorkflowRunner Domain-Neutral

**Objective:** Remove inheritance from the action runner and make capability
execution entirely registry-driven.

**Work:**

- Move generic graph materialization, recovery, stage scheduling, stable
  all-settled execution, unit transitions, and finish logic into
  `agent/runtime/workflow_runner.py`.
- Replace `class WorkflowRunner(CommandRunner)` with a class that receives
  `RunRuntime`, workflow definitions, capability/worker/executor registries,
  and `ContextResolver`.
- Move reusable quality checks and matching functions out of
  `CommandRunner`. Put pure domain checks near the relevant worker or executor;
  put cross-artifact readiness checks near capabilities.
- Replace `_run_stage` domain dispatch with registry lookup and the generic
  unit pipeline.
- Move workflow resolution out of the runner into `agent/routing.py`.
- Keep `_adopt_legacy` behavior in a compatibility adapter, not in the generic
  scheduler core.
- Preserve stage-level dependency ordering, bounded unit parallelism,
  all-settled failures, deterministic result ordering, serialized commits,
  dynamic expansion, and `next_outcomes`.

**Primary files:**

- `backend/app/agent/workflow_runner.py` -> compatibility import or adapter
- New `backend/app/agent/runtime/workflow_runner.py`
- New `backend/app/agent/routing.py`
- `backend/app/agent/workflow.py`
- `backend/app/agent/command_runner.py`
- `backend/app/agent/runner.py`

**Tests:**

- A synthetic non-audit registry runs through the scheduler without importing
  any audit module.
- Dependency closure, readiness skipping, unit expansion, recovery, stable
  ordering, sidecar reuse, and partial failure behavior match existing v3.
- The runtime package passes an import-boundary test that rejects imports from
  planning, RCM, document, findings, report, or audit capability modules.
- Workflow routing never calls the action interpreter for recognized outcomes.

**Exit gate:** `WorkflowRunner` does not inherit from `ActionRunner` or
`CommandRunner`, and it has no methods named for domain stages.

### Phase 7: Move The Audit Workflow And Capability Families

**Objective:** Make one file authoritative for audit dependencies and migrate
the remaining v3 handlers to declarations, workers, and executors.

**Work:**

- Create `agent/workflows/audit.py` with the complete dependency graph and
  workflow metadata.
- Move readiness and unit expansion from `audit_capabilities.py` into grouped
  capability modules without changing their semantic IDs.
- Keep existing capability IDs and semantic unit-ID construction stable.
- Add explicit versioned context specs to every model-backed capability.
- Migrate capability families in dependency order:

| Slice | Capabilities | Main extraction |
|---|---|---|
| 7A | `planning.context_ready` | Context synthesis worker and planning-context executor |
| 7B | `planning.apm_ready` | Completed vertical slice from Phase 5 |
| 7C | `planning.rcm_ready` | RCM worker, matching logic, row commit executor |
| 7D | `planning.planned_tests_ready` | Per-RCM unit worker and planned-test executor |
| 7E | `fieldwork.definitions_ready` | Data/document definition workers and linked-write executors |
| 7F | `fieldwork.executed` | Deterministic/data execution and injected document QA worker paths |
| 7G | `results.rolled_up` | Local rollup executor and observation checkpoint declaration |
| 7H | `findings.drafted` | Finding worker and evidence-preserving executor |
| 7I | `working_papers.generated` | Deterministic working-paper executor |
| 7J | `dashboard.curated` | Curation worker or deterministic selector and tile executor |
| 7K | `report.working_draft` | Report worker, reconciliation policy, and executor |
| 7L | `audit.verified` | Deterministic verification executor and quality result |

- Decide per capability whether a model worker is required. Deterministic work
  uses a no-model worker or direct executor path declared by the capability;
  do not call the model merely to fit a uniform shape.
- Preserve the observation/disposition interaction as a declared checkpoint
  between rollup and finding creation.
- Preserve linked-write rollback and recovery for RCM-linked data and document
  test definitions.
- Replace `audit_capabilities.build_registry()` with composition of grouped
  declarations from the authoritative audit workflow.

**Primary files:**

- New `backend/app/agent/workflows/audit.py`
- New `backend/app/agent/capabilities/planning.py`
- New `backend/app/agent/capabilities/fieldwork.py`
- New `backend/app/agent/capabilities/reporting.py`
- New grouped worker and executor modules
- `backend/app/agent/audit_capabilities.py` -> compatibility re-export, then
  deletion
- `backend/app/agent/audit_workers.py` -> split, then deletion
- `backend/app/agent/workflow_runner.py` -> compatibility adapter only

**Per-slice gate:** Existing readiness, staleness, unit IDs, artifact shapes,
approvals, interactions, and receipts remain stable. Delete the old handler as
soon as its slice passes; do not keep dual writers.

**Phase exit gate:** The audit lifecycle appears only in
`workflows/audit.py`, and the generic workflow runner imports no audit modules.

### Phase 8: Add Exploratory Data Analysis Workflow

**Objective:** Handle requests such as "see the two tables, perform relevant
joins and data analysis" as a durable outcome workflow.

**Workflow:**

```text
data.relationships_inferred
-> data.joins_ready
-> analysis.definitions_ready
-> analysis.executed
```

**Work:**

- Create `agent/workflows/analysis.py` and grouped analysis capabilities.
- Define scope resolution for explicitly named tables, selected UI artifacts,
  or all eligible workspace tables within limits.
- Reuse deterministic join diagnostics in `agent/joins.py`; represent inferred
  relationships and materialized joins as separate outcomes.
- Declare table context using schemas, profiles, bounded aggregates, and local
  diagnostic results. Keep `allow_table_rows=False` for provider context.
- Generate candidate analysis definitions through an analysis worker using the
  declared metadata context.
- Validate analytics IDs, validation checks, query specs, and sandboxed Polars
  code before persistence.
- Execute table operations locally through Polars and existing analytics,
  validation, explore, and sandbox services.
- Persist useful definitions and immutable bounded results using deterministic
  executors and receipts.
- Route the `data_analysis` goal template and equivalent phrases to
  `analysis.executed`, not to the generic action interpreter or legacy v1.
- Keep isolated "run this existing analysis" or "pin this result" operations in
  `ActionRunner`.

**Primary files:**

- New `backend/app/agent/workflows/analysis.py`
- New `backend/app/agent/capabilities/analysis.py`
- New `backend/app/agent/workers/analysis.py`
- New `backend/app/agent/executors/analysis.py`
- `backend/app/agent/routing.py`
- `backend/app/agent/joins.py`
- Existing analytics, validation, explore, data-test, and sandbox services

**Tests:**

- A two-table request selects the requested tables and infers plausible joins
  deterministically.
- Ambiguous or unsafe joins require clarification or are reported, not silently
  applied.
- Model context contains metadata and bounded aggregates but no row-level data.
- Invalid model-generated definitions receive bounded repair and never commit.
- Local execution uses Polars and persists rerunnable specs rather than remote
  result data.
- Repeating the workflow reuses current outcomes and does not duplicate saved
  analyses or joins.
- A pin request still routes to `ActionRunner`.

**Exit gate:** Generic analysis and complete-audit requests use the same
workflow scheduler with different declared outcome sets.

### Phase 9: Unify Document Analysis

**Objective:** Replace duplicate standalone and planning document-analysis
implementations with one capability workflow.

**Workflow:**

```text
documents.text_ready
-> documents.analysis_chunks_ready
-> documents.analysis_generated
-> documents.analysis_reviewed
```

**Work:**

- Create document readiness and semantic units per document and per chunk.
- Treat extraction as deterministic readiness/execution and preserve existing
  eligible text-state rules.
- Make chunk mapping and document reduction separate workers. The scheduler
  owns fan-out, progress, retry, resume, all-settled behavior, and reduce
  ordering.
- Reuse validation, citation, cache-identity, status, persistence, review, and
  conflict functions in `document_analysis.py`.
- Persist per-chunk proposals so successful chunks are not rebilled after a
  crash.
- Persist the reduced analysis through one executor using source and extracted
  text hashes.
- Keep generated and auditor-reviewed outcomes separate. Audit planning may
  depend on generated analyses; evidence-sensitive flows can explicitly depend
  on reviewed analyses where required.
- Route standalone document analysis to the same workflow with selected
  document refs and requested outcome `documents.analysis_generated`.
- Turn `DocumentAnalysisRunner` into a thin compatibility adapter for old
  `kind="document_analysis"` runs. It translates context to workflow scope and
  delegates; it contains no map/reduce logic.
- Remove `CommandRunner._ensure_planning_analysis` and any second chunk/reduce
  implementation.

**Primary files:**

- New `backend/app/agent/workflows/documents.py`
- New `backend/app/agent/capabilities/documents.py`
- New `backend/app/agent/workers/documents.py`
- New `backend/app/agent/executors/documents.py`
- `backend/app/agent/document_analysis_runner.py`
- `backend/app/document_analysis.py`
- `backend/app/routes/document_routes.py`

**Tests:**

- Multi-document and multi-chunk fan-out is bounded and stably ordered.
- One chunk failure does not discard successful chunk sidecars.
- Resume reuses valid chunk and reduction proposals without provider rebilling.
- Document replacement invalidates analysis through source hashes.
- Citation validation, review conflicts, refresh semantics, status projection,
  and page/character limits match current behavior.
- Standalone analysis and planning produce the same artifact format through the
  same workers and executor.

**Exit gate:** There is exactly one document map worker, one reduction worker,
and one persistence executor.

### Phase 10: Evaluate Intake And Document-Test Runners

**Objective:** Apply the architecture consistently without forcing unlike
scheduling protocols into the workflow runner.

#### Intake

- Model intake as capabilities only if its classify/review/apply lifecycle maps
  cleanly to declared outcomes and semantic units.
- Keep explicit human review of staged manifests as a declared checkpoint.
- Preserve local file operations, idempotent application, and batch identity.
- If intake remains a distinct protocol, keep `IntakeRunner` but convert it to
  use `RunRuntime`, `ModelGateway`, and declared context/worker contracts.

#### Document Tests

- Separate test definition readiness, item execution, and result/disposition
  outcomes.
- Reuse `doc_tests.run_item` with an injected `ModelGateway` adapter so all
  calls share budgets and provenance.
- Preserve resumable per-item work, attachments, evidence anchors, comparison
  checks, disposition state, and RCM-linked receipts.
- If standalone document-test execution is merely fan-out over semantic items,
  migrate it to `WorkflowRunner`. Otherwise retain a thin protocol-specific
  runner using the shared runtime and workers.

**Decision record required:** For each runner, document the scheduling feature
that either justifies retention or proves it fits `WorkflowRunner`. File count
or historical separation is not sufficient justification.

**Tests:** Existing intake and evidence-aware document-test suites must pass,
plus runtime budget, recovery, approval, and privacy tests for the selected
implementation.

### Phase 11: Consolidate Routing And Dispatch

**Objective:** Give requests one clear route and make the execution engine
explicit.

**Work:**

- Centralize deterministic templates and phrase mappings in
  `agent/routing.py`.
- Define a bounded router-worker schema with four results:
  workflow outcomes, action intent, clarification, or unsupported.
- Validate requested outcomes against registered workflows and action intents
  against the action registry.
- Keep routing pure: it cannot execute actions, gather domain context, or
  mutate the workspace.
- Replace `initialize_known_workflow` and scattered local-resolution branches
  with the centralized router.
- Make `runner.start_command_run` persist the selected engine and normalized
  route before launching the thread.
- Simplify `_execute` to dispatch by explicit engine, falling back to a
  compatibility classifier only for old runs.
- Preserve one live run per workspace, global concurrency, pending-command
  FIFO, retry parent links, and terminal-state guarantees.

**Routing matrix:**

| Request shape | Engine | Requested result |
|---|---|---|
| Prepare/improve APM or RCM | Workflow | Relevant planning outcome |
| Complete audit lifecycle | Workflow | `audit.verified` plus requested deliverables |
| Infer joins and analyze tables | Workflow | `analysis.executed` |
| Analyze selected documents | Workflow | `documents.analysis_generated` |
| Run declared RCM fieldwork | Workflow | `fieldwork.executed` |
| Attach, detach, rename, delete, edit, pin | Action | Registered operation DAG |
| Ambiguous target or scope | None | Clarification interaction |
| Unsupported request | None | Durable unsupported result |

**Exit gate:** A request is classified once, the engine is persisted, and no
scheduler contains a fallback call into the other scheduler.

### Phase 12: Isolate And Retire V1

**Objective:** Remove the final obsolete general runner without coupling that
risk to the v2/v3 cleanup.

**Work:**

- Inventory every caller of `start_run(..., kind="analysis")` and every UI/API
  entry point that still expects the fixed v1 pipeline.
- Route supported exploratory analysis requests to the new analysis workflow.
- Move `_Runner`, fixed `STAGES`, v1 validators, and legacy limits into
  `agent/compat/legacy_v1_runner.py` while persisted v1 resume remains
  supported.
- Stop creating new v1 runs.
- Define a compatibility horizon for existing v1 runs. Options are read-only
  history, resume through the legacy adapter, or explicit conversion when the
  mapping is safe.
- Delete the v1 implementation only after production-supported entry points
  and fixtures no longer require execution.

**Exit gate:** No new run uses v1, all current UI actions use workflow or action
engines, and removal does not alter historical run display.

### Phase 13: Remove Compatibility Scaffolding

**Objective:** Finish the simplification after all replacement paths are
stable.

**Work:**

- Remove temporary import aliases and domain runner adapters whose persisted
  compatibility window has ended.
- Remove old run writers while retaining minimal readers or migration helpers
  required for history.
- Remove legacy workflow and action projections only after confirming the
  frontend and debug console no longer consume them.
- Delete empty modules and re-export shims.
- Update `AGENTS.md`, architecture documentation, API schemas, debug telemetry
  documentation, and developer examples.
- Add static import-boundary enforcement to prevent scheduler/domain coupling
  from returning.

**Exit gate:** The package structure matches the target architecture closely,
with no duplicate scheduler, document-analysis, audit-lifecycle, context, or
provider-call implementation.

## 7. Proposed Package Migration

Create packages incrementally; do not move every file before extracting its
responsibility.

```text
backend/app/agent/
|- runtime/
|  |- run_runtime.py
|  |- workflow_runner.py
|  |- action_runner.py
|  |- model_gateway.py
|  `- interactions.py
|- routing.py
|- workflows/
|  |- audit.py
|  |- analysis.py
|  `- documents.py
|- capabilities/
|  |- model.py
|  |- planning.py
|  |- fieldwork.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
|- context/
|  |- model.py
|  |- resolver.py
|  |- presets.py
|  `- manifest.py
|- workers/
|  |- model.py
|  |- planning.py
|  |- fieldwork.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
|- executors/
|  |- model.py
|  |- planning.py
|  |- fieldwork.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
|- actions/
|  |- catalog.py
|  |- planner.py
|  `- executors.py
`- compat/
   |- persisted_runs.py
   |- legacy_v1_runner.py
   |- intake_runner.py
   |- doc_test_runner.py
   `- document_analysis_runner.py
```

Compatibility modules are temporary and may be absent when a runner migrates
directly. Avoid a file per individual capability unless size or ownership
independently warrants it.

## 8. Test Plan

### Unit Tests

- Capability registry validation and cycle reporting.
- Workflow closure, readiness, invalidation, and semantic unit IDs.
- Context preset normalization and selector determinism.
- Context privacy, limits, truncation, and manifest hashing.
- Worker prompt and schema validation using fixed bundles.
- Executor CAS, reconciliation, auditor-edit preservation, and receipts.
- Action ledger DAG behavior without audit-specific rules.
- Routing classification and validation.
- Persisted-run engine inference and compatibility translation.

### Integration Tests

- One vertical capability from context resolution through committed receipt.
- Full audit closure through its human disposition checkpoint.
- Partial audit outcomes with prerequisite reuse and stale-parent regeneration.
- Generic table analysis with joins and local Polars execution.
- Standalone and audit-triggered document analysis using identical internals.
- Action DAG execution, approval, resume, reconciliation, and undo.
- Provider failure, invalid structured output, bounded repair, and partial
  completion.
- Concurrent proposal generation with stable serialized commits.
- Workspace revision conflict and unrelated concurrent-write merge.

### Recovery Matrix

Test restart or resume at each boundary:

| Boundary | Expected recovery |
|---|---|
| Before context resolution | Resolve normally |
| After manifest persistence | Reuse if hashes still match |
| During provider call | Return unit to ready and call again |
| After proposal persistence | Reuse proposal without provider call |
| Awaiting approval | Restore the same approval |
| After approval, before commit | Commit after precondition check |
| During commit, before receipt | Reconcile postcondition before retry |
| After receipt | Mark succeeded if postcondition remains current |
| Parent changed | Mark stale/conflict; do not overwrite |
| Process crash with queued command | Recover current run, then preserve FIFO |

### API And Frontend Contract Tests

- Start, status, events, pause, resume, cancel, approval, interaction, retry,
  continue, action coverage, and SSE replay endpoints.
- Assistant-chat durable messages and linked run projections.
- Existing activity labels and terminal states, or an explicit versioned UI
  migration when a label changes.
- Historical action and workflow run cards remain renderable.
- Frontend TypeScript build passes after any payload type change.

### Privacy And Security Tests

- Table rows never appear in model messages for agent workflows.
- Context sources outside the declared `ContextSpec` are inaccessible.
- Document text is supplied only when the capability permits it.
- Manifests, events, telemetry, and provider provenance contain no raw document
  text or row-level data.
- Sandbox-generated analysis remains constrained to local safe Polars
  execution with no imports or file I/O.
- Approval requirements cannot be bypassed by a worker proposal.

### Regression Commands

Run focused tests after each slice, then the full gates at phase completion:

```powershell
uv run --no-project pytest backend/tests/test_workflow_v2.py
uv run --no-project pytest backend/tests/test_command_agent.py
uv run --no-project pytest backend/tests/test_agent_api.py backend/tests/test_assistant_chats.py
uv run --no-project pytest backend/tests
npm --prefix frontend run build
```

Adjust the invocation to the active virtual environment, but retain the full
backend suite and frontend build as release gates.

## 9. Observability And Auditability

Add debug fields without storing sensitive content:

- Engine, workflow, capability, and unit IDs.
- Definition, context-policy, source, prompt, worker, proposal, and receipt
  hashes.
- Context item counts, supplied size, omissions, and truncation counts.
- Selector IDs, versions, and selection reasons.
- Provider profile, turns, estimated/actual tokens where available, latency,
  retries, and semaphore wait.
- Readiness transitions and invalidation reasons.
- Proposal reuse, reconciliation, conflict, and commit outcomes.

The debug console should explicitly distinguish absent historical telemetry
from zero values. Do not backfill invented details for old runs.

## 10. Risk Register

| Risk | Mitigation |
|---|---|
| Persisted runs become unreadable | Add engine inference and fixture-based resume tests before class moves |
| Duplicate commits after crash | Persist proposals first; reconcile postconditions before retry; persist receipts |
| Auditor edits are overwritten | Parent hashes, CAS, domain merge rules, and explicit conflict states |
| Context refactor leaks table rows | Typed representations, deny-by-default policy, pre-worker validation, privacy tests |
| Prompt quality changes during extraction | Move prompts unchanged first; version and tune only in later commits |
| Workflow runner remains domain-coupled through helper imports | Registry injection and automated import-boundary test |
| Action runner accidentally accepts broad audits | Routing validation and fail-closed action-runner guard |
| Semantic unit IDs change | Golden unit-ID tests and stable capability IDs during migration |
| Sidecars are reused after context or prompt changes | Include definition, context, selector, prompt, worker, schema, input, and source hashes |
| Document analysis gets billed twice on resume | Per-chunk and reduction proposal sidecars with recovery tests |
| New analysis workflow invents unsafe joins | Local diagnostics, explicit ambiguity handling, bounded scope, no automatic destructive replacement |
| Compatibility adapters become permanent duplicate logic | Adapters translate and delegate only; each has a documented deletion gate |
| File movement creates oversized reviews | Move one vertical slice at a time and delete its old handler in the same slice |

## 11. Review And Commit Strategy

Prefer reviewable pull requests in this order:

1. Characterization fixtures and tests only.
2. V2 full-audit removal and action-runner naming.
3. Ledger audit-policy removal.
4. Runtime and model-gateway extraction with delegation.
5. Context contracts plus one adapter-backed resolver slice.
6. Worker/executor interfaces plus APM vertical slice.
7. Domain-neutral workflow scheduler and routing extraction.
8. One pull request per audit capability family or tightly related group.
9. Exploratory analysis workflow.
10. Document-analysis unification.
11. Intake and document-test decisions and migrations.
12. Routing/dispatch consolidation.
13. V1 isolation and later retirement.
14. Compatibility cleanup and documentation.

Do not combine prompt tuning, workspace schema redesign, or frontend redesign
with these structural changes. Each pull request should state which old code is
now unreachable and either delete it or identify the exact compatibility test
that still requires it.

## 12. Phase Checklist

Every phase is complete only when all applicable items are true:

- [ ] Behavior was characterized before modification.
- [ ] New persisted fields are additive and versioned.
- [ ] Old persisted runs still load, display, and follow their declared resume
      policy.
- [ ] No new duplicate writer or scheduler branch was introduced.
- [ ] Model calls go through `ModelGateway` and are budgeted.
- [ ] Context is declared, bounded, manifested, and privacy-checked.
- [ ] Proposals are persisted before approval or commit.
- [ ] Commits use CAS or parent hashes and produce receipts.
- [ ] Semantic unit IDs and deterministic ordering are stable.
- [ ] Focused tests pass.
- [ ] Full backend tests pass at the phase boundary.
- [ ] Frontend build passes when shared payloads change.
- [ ] Architecture and handoff documentation reflect the actual state.

## 13. Definition Of Done

The architecture migration is complete when:

- Only `WorkflowRunner` and `ActionRunner` are general scheduling engines.
- They share runtime services through composition and never call each other.
- `WorkflowRunner` is domain-neutral and contains no artifact-specific stage
  handlers.
- The audit lifecycle is declared once in `workflows/audit.py`.
- The action ledger contains no audit lifecycle ranks or normalization.
- Every model-backed capability has a versioned, normalized `ContextSpec` and
  persists a `ContextManifest`.
- Workers receive only declared bundles and cannot mutate workspaces.
- Executors cannot call models and all commits are conflict-aware and
  receipted.
- Generic data analysis is a declared workflow ending in
  `analysis.executed`.
- Standalone and audit document analysis share one map/reduce implementation.
- Intake and document-test runner decisions are documented and use the shared
  runtime boundaries.
- New v1 runs cannot be created, and remaining v1 compatibility is explicit.
- Persisted run recovery, approvals, interactions, SSE replay, debug telemetry,
  privacy tests, the full backend suite, and the frontend build all pass.

## 14. Recommended Starting Point

Begin with Phase 0 and land it independently. The first production-code change
should then remove v2 full-audit orchestration while preserving the v3 audit
path and generic action behavior. Do not start by moving files or rewriting
`WorkflowRunner`; doing so before the compatibility tests and policy cleanup
would obscure which scheduler owns each behavior and make persisted-run
regressions harder to diagnose.
