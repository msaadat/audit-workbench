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

## Implementation Status And Session Handoff

**Last updated:** 2026-07-21

**Current position:**

- Overall migration: not started.
- Current phase: Phase 0.
- Current task: `P0.1`.
- Last completed task: none.
- Active blockers: none.

The checklists under each phase are the durable execution ledger for this
migration. A task ID identifies the smallest intended implementation and review
unit. An unchecked task is pending and a checked task is complete. The
`Current position` above identifies the next task when idle or the one task in
progress when active; blockers and material deviations are recorded in the
status notes below.

**Phase status:**

| Phase | Status | Next task |
|---|---|---|
| 0 | Not started | `P0.1` |
| 1 | Pending Phase 0 gate | `P1.1` |
| 2 | Pending Phase 1 gate | `P2.1` |
| 3 | Pending Phase 2 gate | `P3.1` |
| 4 | Pending Phase 3 gate | `P4.1` |
| 5 | Pending Phase 4 gate | `P5.1` |
| 6 | Pending Phase 5 gate | `P6.1` |
| 7 | Pending Phase 6 gate | `P7.1` |
| 8 | Pending Phase 7 gate | `P8.1` |
| 9 | Pending Phase 8 gate | `P9.1` |
| 10 | Pending Phase 9 gate | `P10.1` |
| 11 | Pending required workflow migrations | `P11.1` |
| 12 | Pending routing consolidation | `P12.1` |
| 13 | Pending compatibility horizons | `P13.1` |

**Status update rules:**

1. At the start of a session, read this section, the current phase checklist,
   recent Git history, and the worktree status before changing code.
2. Work on only the `Current task` unless the task is explicitly split in this
   document first. Do not start the next task merely because time remains.
3. Mark a task complete only after its focused tests pass and its stated
   compatibility expectations are met. A phase remains incomplete until its
   exit or deletion gate passes.
4. Commit the implementation, tests, and this status update together. The Git
   commit is the authoritative handoff between sessions and computers.
5. Before ending a session, update `Current position`, the phase table, the
   relevant checkbox, test results, decisions, and the exact next task.
6. If blocked, leave the task unchecked, record the blocker and evidence in
   the status notes, and do not silently advance to a later task.

**Status notes and decisions:**

- No implementation task has started.
- Migration verification: not run; only the implementation plan has been
  prepared for task-level tracking.
- The persisted-run compatibility policy must be made explicit in `P0.2` and
  `P1.2` before v2 full-audit code is removed.
- Partially executed v2 broad-audit runs must not be converted in a way that
  repeats committed actions or loses pending approvals or interactions.
- Run-engine inference is intentionally scheduled before action-runner policy
  deletion, even though final routing consolidation occurs in Phase 11.

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

**Tasks:**

- [ ] `P0.1` Inventory current run shapes and add representative fixture
  directories for v1, v2 action, v3 workflow, intake, document-test, and
  document-analysis runs.
- [ ] `P0.2` Define and characterize the resume policy for every fixture,
  including unstarted, partially committed, approval-blocked,
  interaction-blocked, interrupted-provider, and completed runs.
- [ ] `P0.3` Characterize deterministic local routing, bounded-router fallback,
  generic action-interpreter fallback, and fail-closed broad-audit behavior.
- [ ] `P0.4` Characterize SSE events, activity records, run projections,
  approvals, interactions, queued commands, retry, and continue behavior.
- [ ] `P0.5` Characterize workflow materialization, semantic unit IDs,
  readiness and staleness, stable parallel results, serialized commits,
  sidecar reuse, and conflict recovery.
- [ ] `P0.6` Characterize generic action DAG validation, repair,
  preconditions, idempotence, reconciliation, undo, resume, and failure
  propagation without changing production behavior.
- [ ] `P0.7` Add provider-accounting and privacy assertions covering budgets,
  concurrency, table-row exclusion, bounded document context, and hash-only
  provenance.
- [ ] `P0.8` Run the Phase 0 focused suites, document any intentionally
  unsupported historical shape, and update the phase gate and current status.

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

**Tasks:**

- [ ] `P1.1` Add additive `engine` and `engine_schema_version` fields for new
  runs plus a pure engine-inference helper for old run shapes; keep dispatch
  behavior unchanged.
- [ ] `P1.2` Implement the Phase 0 compatibility matrix: distinguish
  convertible unstarted v2 broad-audit runs from partially executed runs that
  require a compatibility path.
- [ ] `P1.3` Introduce `ActionRunner` as the canonical class name with a
  temporary `CommandRunner` import alias; make no policy deletion in this
  naming-only task.
- [ ] `P1.4` Add a fail-closed guard so newly routed broad-audit and planning
  requests cannot enter the canonical action planner.
- [ ] `P1.5` Convert eligible unstarted persisted v2 broad-audit runs to the
  workflow engine with a durable compatibility event and stable run identity.
- [ ] `P1.6` Isolate any still-supported partially executed v2 broad-audit
  behavior in a compatibility adapter that preserves actions, approvals,
  interactions, receipts, and queued commands.
- [ ] `P1.7` Remove planning preparation, audit action insertion, audit budget
  reservations, and full-audit validation from the canonical `ActionRunner`.
- [ ] `P1.8` Remove obsolete interpreter prompt clauses and update focused
  action, workflow, recovery, and routing tests.
- [ ] `P1.9` Prove the deletion gate and update the compatibility horizon and
  current status.

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

**Tasks:**

- [ ] `P2.1` Move any lifecycle behavior still required by resumable legacy v2
  runs behind the Phase 1 compatibility adapter, with fixture-based tests.
- [ ] `P2.2` Convert audit-lifecycle normalization tests into workflow or
  compatibility tests while retaining generic DAG characterization.
- [ ] `P2.3` Remove `audit_lifecycle` from `append_actions(...)` and its
  canonical callers.
- [ ] `P2.4` Remove audit lifecycle constants and enforcement from
  `ledger.py`, then run the action and workflow focused suites.
- [ ] `P2.5` Prove the domain-neutral ledger exit gate and update status.

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

**Tasks:**

- [ ] `P3.1` Define the small `RunRuntime` and `ModelGateway` public contracts
  plus compatibility tests, without moving behavior.
- [ ] `P3.2` Extract the provider semaphore, model profiles, token charging,
  retries, stage tags, telemetry, and hash-only provenance into
  `ModelGateway`; make `BaseRunner` delegate to it.
- [ ] `P3.3` Extract run save, event emission, activity projection, status, and
  durable timing operations into `RunRuntime` with delegation from
  `BaseRunner`.
- [ ] `P3.4` Extract budgets, dynamic limits, deadlines, checkpoints,
  pause/resume, cancellation, and inbox draining into `RunRuntime`.
- [ ] `P3.5` Extract approval and structured-interaction transitions, including
  blocked-time deadline extension and restart behavior.
- [ ] `P3.6` Inject the runtime into `ActionRunner` while retaining the
  compatibility facade and existing API behavior.
- [ ] `P3.7` Inject the runtime into the existing `WorkflowRunner` without yet
  making it domain-neutral or moving its stage handlers.
- [ ] `P3.8` Prove shared budgets, controls, queued follow-ups, terminal crash
  handling, leaf-runner compatibility, and the no-direct-provider-call gate.

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

**Tasks:**

- [ ] `P4.1` Define versioned `ContextSpec`, source, representation, budget,
  privacy, selector, manifest, and bundle models with serialization tests.
- [ ] `P4.2` Add preset and selector registries with duplicate, unknown,
  unversioned, unsupported-source, and invalid-privacy validation.
- [ ] `P4.3` Implement deterministic manifest identity, atomic persistence,
  source hashing, omission records, truncation records, and supplied-size
  metrics without persisting bundle content.
- [ ] `P4.4` Implement resolver ordering, global and per-source limits,
  required/optional source behavior, deny-by-default representations, and
  stable automatic-selection reasons.
- [ ] `P4.5` Adapt existing document and methodology context builders for the
  APM slice without copying their domain logic.
- [ ] `P4.6` Adapt table metadata/profile context and structurally reject
  row-level representations before worker invocation.
- [ ] `P4.7` Specify and implement the intended auditor-editable surface for
  context selection, or amend the objective and contracts to make the scope
  explicitly declaration-only.
- [ ] `P4.8` Route one real capability through `ContextResolver`, verify
  invalidation on spec/selector changes, and prove the privacy exit gate.

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

**Tasks:**

- [ ] `P5.1` Define immutable worker request/result models, versioned registry
  metadata, response-schema validation, and bounded repair contracts.
- [ ] `P5.2` Define executor request/result and receipt models, registry
  metadata, parent-hash/CAS requirements, and reconciliation contracts.
- [ ] `P5.3` Implement a runner-independent unit-pipeline service for context,
  manifest, worker, proposal, approval, executor, receipt, and readiness
  sequencing.
- [ ] `P5.4` Add proposal and receipt sidecar validation and recovery tests for
  every interruption boundary in the recovery matrix.
- [ ] `P5.5` Extract the existing APM prompt and semantic validation into a
  planning worker that has no workspace access.
- [ ] `P5.6` Extract APM mutation, parent checks, edit preservation,
  postconditions, reconciliation, and receipts into a planning executor with
  no model dependency.
- [ ] `P5.7` Switch only `planning.apm_ready` to the registered pipeline,
  remove its old writer, and preserve payload and UI projections.
- [ ] `P5.8` Prove no rebilling after proposal persistence, conflict behavior,
  approval ordering, and the APM vertical-slice exit gate.

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

**Tasks:**

- [ ] `P6.1` Add a synthetic non-audit workflow registry and golden tests for
  closure, readiness, semantic units, stable scheduling, and recovery.
- [ ] `P6.2` Extract generic materialization, unit transitions, stage folding,
  stable all-settled scheduling, and finish behavior into the new runtime
  scheduler without switching production dispatch.
- [ ] `P6.3` Replace domain handler dispatch with capability, worker, executor,
  and context registry lookup through the Phase 5 unit pipeline.
- [ ] `P6.4` Move legacy adoption and run-shape translation behind the
  persisted-run compatibility adapter.
- [ ] `P6.5` Inject routing results into the scheduler and remove scheduler
  fallback calls to the action interpreter; leave final routing consolidation
  for Phase 11.
- [ ] `P6.6` Switch production workflow dispatch to the composed scheduler and
  retain only a temporary import adapter at the old module path.
- [ ] `P6.7` Add and pass import-boundary enforcement for runtime modules.
- [ ] `P6.8` Prove parity for dynamic expansion, partial failure, deterministic
  commit order, next outcomes, and the no-inheritance/no-domain-handler gate.

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

**Tasks:**

- [ ] `P7.1` Create `workflows/audit.py` with versioned workflow metadata and
  the current dependency graph, preserving capability IDs and ordering behind
  a compatibility re-export.
- [ ] `P7.2` Add registry composition and startup validation for grouped audit
  capability, context, worker, and executor modules before moving a writer.
- [ ] `P7A.1` Move `planning.context_ready` readiness, invalidation, and unit
  expansion into the planning capability module with golden identity tests.
- [ ] `P7A.2` Extract context synthesis and planning-context commit behavior,
  switch the capability to the registered pipeline, and remove its old handler.
- [ ] `P7B.1` Move the completed `planning.apm_ready` declaration and registry
  wiring into the grouped planning modules without changing identity.
- [ ] `P7B.2` Remove the temporary APM vertical-slice adapter and prove the
  Phase 5 behavior through the authoritative audit workflow.
- [ ] `P7C.1` Move `planning.rcm_ready` readiness, invalidation, semantic units,
  context, matching, and validation into grouped declarations and workers.
- [ ] `P7C.2` Extract row-level commit, edit preservation, CAS, reconciliation,
  and receipts; switch the writer and delete the old RCM handler.
- [ ] `P7D.1` Move `planning.planned_tests_ready` readiness and per-RCM unit
  expansion with stable unit and matching tests.
- [ ] `P7D.2` Extract planned-test generation, validation, commit, rollback,
  and receipts; switch the writer and delete the old handler.
- [ ] `P7E.1` Move fieldwork-definition readiness and unit expansion while
  preserving required execution-engine and planned-test linkage rules.
- [ ] `P7E.2` Migrate data-test definition workers and linked-write executors,
  switch their writer, and prove rollback and recovery.
- [ ] `P7E.3` Migrate document-test definition workers and linked-write
  executors, switch their writer, and prove attachment/linkage compatibility.
- [ ] `P7F.1` Move fieldwork-execution readiness, invalidation, attempt limits,
  and data/document semantic units.
- [ ] `P7F.2` Migrate deterministic and data-test execution paths with local
  Polars computation, receipts, and recovery.
- [ ] `P7F.3` Migrate model-backed document QA through the injected gateway and
  declared context, then remove the old execution handler.
- [ ] `P7G.1` Move rollup readiness and deterministic execution into a local
  executor with stable result and observation identities.
- [ ] `P7G.2` Migrate the observation/disposition interaction to a declared
  checkpoint and prove pause, resume, and auditor-edit behavior.
- [ ] `P7H.1` Move finding readiness, eligible-observation units, context,
  evidence rules, and proposal validation.
- [ ] `P7H.2` Extract evidence-preserving finding commits and receipts, switch
  the writer, and remove the old finding handler.
- [ ] `P7I.1` Move working-paper readiness and semantic units into the
  reporting capability module.
- [ ] `P7I.2` Switch working-paper generation to a deterministic executor and
  remove the old handler.
- [ ] `P7J.1` Move dashboard readiness, inputs, and deterministic selection or
  declared curation context into the reporting capability module.
- [ ] `P7J.2` Switch tile commits to a conflict-aware executor and remove the
  old dashboard handler.
- [ ] `P7K.1` Move report readiness, bounded context, reconciliation policy,
  prompt, schema validation, and source rules.
- [ ] `P7K.2` Extract report commit and reconciliation receipts, switch the
  writer, and remove the old report handler.
- [ ] `P7L.1` Move audit-verification readiness and quality checks into a
  deterministic capability and executor.
- [ ] `P7L.2` Switch verification, preserve completion projections, and remove
  the final audit-specific scheduler handler.
- [ ] `P7.3` Replace `audit_capabilities.py` and `audit_workers.py` with
  compatibility re-exports only, then delete them when imports permit.
- [ ] `P7.4` Prove the phase gate, authoritative graph uniqueness, import
  boundaries, full workflow closure, frontend projections, and status update.

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

**Tasks:**

- [ ] `P8.1` Decide and document the persisted artifact and readiness contract
  for inferred relationships, materialized joins, analysis definitions, and
  bounded results without silently changing workspace schemas.
- [ ] `P8.2` Define `workflows/analysis.py`, capability identities,
  dependencies, scope limits, and deterministic routing outcomes.
- [ ] `P8.3` Implement table scope resolution for explicit targets, selected UI
  artifacts, and bounded eligible-workspace fallback, including clarification
  for ambiguous scope.
- [ ] `P8.4` Implement `data.relationships_inferred` from deterministic local
  diagnostics, with stable evidence and no model-generated relationship facts.
- [ ] `P8.5` Implement `data.joins_ready` with ambiguity handling,
  materialization preconditions, idempotence, CAS, and receipts.
- [ ] `P8.6` Add declared schema/profile/aggregate context with structural
  table-row exclusion and privacy tests.
- [ ] `P8.7` Implement the analysis-definition worker, bounded repair, and
  validation for analytics, validations, query specs, and safe Polars code.
- [ ] `P8.8` Implement deterministic persistence of rerunnable analysis
  definitions with deduplication and receipts.
- [ ] `P8.9` Implement `analysis.executed` through existing local services and
  persist only the approved bounded result contract.
- [ ] `P8.10` Route data-analysis requests to the workflow while preserving
  isolated run/pin operations in `ActionRunner`.
- [ ] `P8.11` Prove repeat-run reuse, unsafe-join behavior, privacy, local-only
  execution, full integration tests, and the phase gate.

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

**Tasks:**

- [ ] `P9.1` Define the persistence and readiness contract for run-local chunk
  proposals, reduced document analyses, review state, and downstream source
  hashes.
- [ ] `P9.2` Define `workflows/documents.py`, stable document/chunk unit IDs,
  dependencies, eligible text states, bounds, and routing scope.
- [ ] `P9.3` Implement deterministic text readiness and extraction execution
  by adapting existing document services.
- [ ] `P9.4` Extract the single chunk-map worker with declared context,
  citation rules, validation, and per-chunk proposal persistence.
- [ ] `P9.5` Add bounded all-settled chunk scheduling and recovery so successful
  chunk proposals survive sibling failure and restart.
- [ ] `P9.6` Extract the single reduction worker with stable chunk ordering,
  proposal-sidecar inputs, bounded context, and semantic validation.
- [ ] `P9.7` Extract the single analysis persistence executor with source/text
  hashes, CAS, reconciliation, receipts, and refresh/conflict behavior.
- [ ] `P9.8` Separate generated and auditor-reviewed readiness and preserve
  status, review, citation, and activity projections.
- [ ] `P9.9` Route standalone analysis and audit-planning dependencies through
  the same workflow implementation.
- [ ] `P9.10` Reduce `DocumentAnalysisRunner` to a persisted-run adapter and
  remove every duplicate map/reduce implementation.
- [ ] `P9.11` Prove billing reuse, replacement invalidation, format parity,
  page/character bounds, integration behavior, and the phase gate.

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

**Tasks:**

- [ ] `P10.1` Inventory intake scheduling requirements and write the required
  decision record against workflow semantic units, review checkpoints, file
  operations, batch identity, and recovery.
- [ ] `P10.2` Implement the intake decision: either migrate declared outcomes
  incrementally or retain a thin runner using `RunRuntime`, `ModelGateway`, and
  declared worker/context contracts.
- [ ] `P10.3` Prove intake review, idempotent apply, local file privacy,
  budgets, restart behavior, and persisted compatibility.
- [ ] `P10.4` Inventory document-test scheduling requirements and write the
  required decision record against fan-out, comparisons, disposition,
  attachments, evidence anchors, linked writes, and recovery.
- [ ] `P10.5` Implement the document-test decision and route all model calls
  through the injected gateway without duplicating `doc_tests.run_item`.
- [ ] `P10.6` Prove per-item resume, comparison/disposition behavior,
  RCM-linked receipts, privacy, budgets, and persisted compatibility.
- [ ] `P10.7` Update the package migration and compatibility horizon to reflect
  both decisions, then run the phase integration gate and update status.

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

**Tasks:**

- [ ] `P11.1` Reconcile the early engine fields and inference from Phase 1 with
  final workflow/action/compatibility engine names and schema versions.
- [ ] `P11.2` Move deterministic templates, phrases, outcome validation, and
  action-intent validation into pure `agent/routing.py` functions.
- [ ] `P11.3` Define and validate the bounded router-worker result schema for
  workflow, action, clarification, and unsupported outcomes.
- [ ] `P11.4` Persist one normalized route and selected engine before thread
  launch, preserving current API projections.
- [ ] `P11.5` Dispatch new runs only by explicit engine and old runs only
  through the compatibility classifier.
- [ ] `P11.6` Remove local resolution and cross-scheduler fallback from both
  schedulers, preserving deterministic no-model routing for common requests.
- [ ] `P11.7` Prove the routing matrix, one-live-run rule, global concurrency,
  queued FIFO, retry/continue links, terminal crash guarantees, and phase gate.

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

**Tasks:**

- [ ] `P12.1` Inventory all v1 creation, resume, API, UI, chat, and historical
  projection callers, then lock the inventory with tests.
- [ ] `P12.2` Route supported exploratory-analysis creation paths to the Phase
  8 workflow while preserving request scope and visible projections.
- [ ] `P12.3` Stop creating new v1 runs and fail closed or clarify unsupported
  legacy-only request shapes.
- [ ] `P12.4` Move the fixed v1 runner, stages, validators, and limits into the
  compatibility package without changing persisted resume behavior.
- [ ] `P12.5` Implement the chosen compatibility horizon: read-only history,
  adapter resume, or safe explicit conversion per characterized run shape.
- [ ] `P12.6` Prove all current UI/API paths use workflow or action engines,
  historical cards still render, and the phase gate passes.

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

**Tasks:**

- [ ] `P13.1` Verify each documented compatibility horizon against persisted
  fixtures and production-support policy before deleting an adapter or writer.
- [ ] `P13.2` Remove expired runner adapters, aliases, empty modules, duplicate
  writers, and re-export shims one family at a time with focused tests.
- [ ] `P13.3` Remove obsolete run projections only after frontend and debug
  consumers have migrated and historical fixtures remain renderable.
- [ ] `P13.4` Add final static import-boundary and single-provider-path
  enforcement across runtime, schedulers, workers, executors, and domains.
- [ ] `P13.5` Update architecture, API, telemetry, developer, and handoff docs;
  run the full backend suite and frontend build.
- [ ] `P13.6` Prove the definition of done, mark the migration complete, and
  record any intentionally retained compatibility reader and its owner.

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

The default Git commit unit is one checked task ID from the phase checklists.
A task commit includes its production changes, tests, and status edits. If a
session must end with a task incomplete, the handoff commit must leave the
application runnable, keep the task unchecked, identify itself as incomplete,
and record the exact remaining work under `Status notes and decisions`.

Pull requests may group consecutive task commits when they share one phase gate,
but they must remain reviewable commit by commit. Do not squash away task
boundaries until the migration is complete and persisted-run compatibility no
longer depends on the historical sequence.

Prefer reviewable pull requests in this order:

1. Characterization fixtures and tests only.
2. Engine inference, v2 compatibility policy, and action-runner naming.
3. Ledger audit-policy removal.
4. Runtime and model-gateway extraction with delegation.
5. Context contracts plus one adapter-backed resolver slice.
6. Worker/executor interfaces plus APM vertical slice.
7. Domain-neutral workflow scheduler and injected routing boundary.
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
should be `P1.1`, which adds explicit engine identity and inference without
changing dispatch. Then establish the persisted v2 compatibility path before
removing full-audit orchestration from the canonical action runner. Do not start
by moving files or rewriting `WorkflowRunner`; doing so before the compatibility
tests and policy cleanup would obscure which scheduler owns each behavior and
make persisted-run regressions harder to diagnose.
