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

This is an incremental clean-slate cutover, not a big-bang rewrite. Each phase
must leave the application runnable, but no pre-cutover workspace or run is
supported. Existing `Workspaces/` contents are assumed disposable and are
removed or moved aside before the target architecture is used. Old code is
deleted after active callers move and the replacement path passes focused tests;
no historical reader or resume adapter is retained.

## Implementation Status And Session Handoff

**Last updated:** 2026-07-22

**Current position:**

- Overall migration: in progress.
- Current phase: Phase 5 (in progress).
- Current task: `P5.3A` (not started).
- Last completed task: `P5.3`.
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
| 0 | Complete | — |
| 1 | Complete | — |
| 2 | Complete | — |
| 3 | Complete | — |
| 4 | Complete | — |
| 5 | In progress | `P5.3A` |
| 6 | Pending Phase 5 gate | `P6.1` |
| 7 | Pending Phase 6 gate | `P7.1` |
| 8 | Pending Phase 7 gate | `P8.1` |
| 9 | Pending Phase 8 gate | `P9.1` |
| 10 | Pending Phase 9 gate | `P10.1` |
| 11 | Pending required workflow migrations | `P11.1` |
| 12 | Pending routing consolidation | `P12.1` |
| 13 | Pending final cleanup | `P13.1` |

**Status update rules:**

1. At the start of a session, read this section, the current phase checklist,
   recent Git history, and the worktree status before changing code.
2. Work on only the `Current task` unless the task is explicitly split in this
   document first. Do not start the next task merely because time remains.
3. Mark a task complete only after its focused tests pass and its stated active
   behavior expectations are met. A phase remains incomplete until its
   exit or deletion gate passes.
4. Commit the implementation, tests, and this status update together. The Git
   commit is the authoritative handoff between sessions and computers.
5. Before ending a session, update `Current position`, the phase table, the
   relevant checkbox, test results, decisions, and the exact next task.
6. If blocked, leave the task unchecked, record the blocker and evidence in
   the status notes, and do not silently advance to a later task.

**Status notes and decisions:**

- `P0.1` completed on 2026-07-21. The active writer, creation, continuation,
  routing, dispatch, API, chat, UI, intake, document-test, document-analysis,
  projection, debug, and auxiliary-reader surfaces are recorded in
  [agent-runtime-active-surface-inventory.md](agent-runtime-active-surface-inventory.md).
- `P0.1` verification used repository-wide call-site searches for the two store
  writers, runner control functions, run routes, assistant-chat projections,
  frontend run endpoints, specialized runners, and `AgentRuns` consumers;
  documentation links and whitespace validation passed. No runtime behavior
  changed, so no behavioral test was required for this inventory-only task.
- `P0.2` completed on 2026-07-21. Current-writer recovery now has a durable
  checkpoint matrix covering queued, partially committed, approval-blocked,
  interaction-blocked, interrupted-provider, and completed command runs.
  Orphan recovery preserves the checkpoint payload, appends one interrupted
  status event for active records, and leaves completed records untouched.
  Focused verification: `2 passed` in `test_agent_runner.py` (the recovery
  matrix plus approval-checkpoint resume-to-completion).
- `P0.3` completed on 2026-07-21. Routing characterization covers every
  registered goal template, representative phrases for every deterministic
  audit mapping, the broad-audit fail-closed workflow route, and the unknown
  command path from bounded workflow router into the generic action
  interpreter. Focused verification: `19 passed` in `test_workflow_v2.py`.
- `P0.4` completed on 2026-07-21. The control-surface suite now explicitly
  proves offline approval and interaction responses are persisted before
  resume and that Continue creates a linked workflow from durable
  `next_outcomes`. Together with the existing SSE cursor, activity, chat
  projection, confirmation, queued-command FIFO, and retry tests, focused
  verification passed `9` selected tests across `test_agent_api.py`,
  `test_assistant_chats.py`, and `test_command_agent.py`.
- `P0.5` completed on 2026-07-21. Workflow characterization now explicitly
  locks repeated materialization to stable semantic unit IDs/input hashes and
  proves a durable proposal sidecar bypasses the model without charging an
  additional turn. The focused gate also covers readiness pruning and stale
  detection, bounded all-settled ordering, dynamic expansion, unrelated-write
  merge versus parent conflict, and linked-write recovery: `8 passed` in
  `test_workflow_v2.py`.
- `P0.6` completed on 2026-07-21. Generic action characterization now
  explicitly rejects duplicate semantic intent across distinct IDs and proves
  failure propagation blocks direct and transitive dependents without
  execution. The focused gate also covers graph repair, cycle validation,
  preconditions and external conflict, resume/rebase, batch rollback,
  after-apply reconciliation, and undo: `9 passed` in
  `test_command_agent.py`.
- `P0.7` completed on 2026-07-21. Provider characterization proves turn and
  token accounting occurs at the single runner choke point, exhausted budgets
  block before another provider call, and a provider/model semaphore bounds
  calls across separate runs. Privacy characterization proves model-backed
  data-test context contains schema metadata without a sentinel row value,
  oversized unscoped documents are withheld, and durable AI activity stores
  prompt/response hashes rather than content. Focused verification: `5 passed`
  across `test_agent_runner.py`, `test_workflow_v2.py`, and
  `test_document_analysis_search.py`.
- `P0.8` completed on 2026-07-21. The complete Phase 0 focused gate passed
  `147` tests across `test_workflow_v2.py`, `test_command_agent.py`,
  `test_agent_runner.py`, `test_agent_api.py`, and
  `test_assistant_chats.py` in `62.32s`. Broad audit requests are locally
  materialized as workflows before v2 interpretation; isolated mutations
  retain the generic action graph; same-writer recovery, controls, projections,
  scheduling, conflicts, provider accounting, and privacy boundaries are
  characterized. Repository verification found no pre-cutover `Workspaces/`,
  `AgentRuns/`, `run.json`, or legacy-run fixture tree. Pre-cutover records
  remain outside the supported target contract and the documented cutover
  requires an empty application workspace root; no converter or fixture reader
  is introduced.
- `P1.1` completed on 2026-07-21. Both durable run writers now persist an
  explicit `engine`, summaries and frontend contracts expose it, deterministic
  isolated actions are labeled `action` before launch, and bounded-router
  action fallbacks persist the engine transition before delegating. Worker
  dispatch and command control checks use only the explicit engine; missing and
  unsupported values fail closed and no load-time inference was added. To keep
  the application runnable during the incremental migration, the still-live
  leaf schedulers have temporary explicit protocol values: `analysis`,
  `intake`, `doc_test`, and `document_analysis`. Phase 9, Phase 10, and Phase 12
  remove or finalize those values as their callers migrate. Focused verification
  passed `171` tests across `test_agent_store.py`, `test_workflow_v2.py`,
  `test_command_agent.py`, `test_agent_runner.py`, `test_agent_api.py`, and
  `test_assistant_chats.py`; the frontend production build also passed.
- `P1.2` completed on 2026-07-21. The action-graph scheduler module and class
  were renamed directly from `command_runner.py` / `CommandRunner` to
  `action_runner.py` / `ActionRunner`. Every live production import and test
  now uses the new names; `WorkflowRunner` temporarily inherits from the
  renamed class until Phase 6, and there is no compatibility module, class
  alias, or re-export. Current-state architecture and runtime inventory docs
  were updated without changing scheduler behavior. Focused verification passed
  `168` tests across `test_command_agent.py`, `test_workflow_v2.py`,
  `test_agent_runner.py`, `test_planning.py`, and `test_assistant_chats.py`.
  No frontend payload or API contract changed, so a frontend build was not
  required for this task.
- `P1.3` completed on 2026-07-21. `ActionRunner.execute()` now validates its
  command before recovery, legacy planning preparation, interpretation, or
  execution. Explicit workflow outcomes, broad-audit/planning templates, and
  broad-audit/planning phrases fail closed with a durable error instructing the
  caller to use workflow routing. A bounded-router miss therefore cannot reach
  the canonical action planner, while target-specific planning artifact edits
  remain valid isolated operations. At that checkpoint, the obsolete full-audit
  implementation remained present for its deletion in `P1.4`; only its
  production entry was guarded.
  Focused verification passed `112` tests across `test_command_agent.py` and
  `test_workflow_v2.py`, including the bounded-router fallback and zero action-
  planner-call assertions. No frontend payload or API contract changed, so a
  frontend build was not required for this task.
- `P1.4` completed on 2026-07-21. The canonical `ActionRunner` no longer runs
  the legacy planning pre-pass, inserts deterministic audit terminal stages,
  reserves action capacity for a full audit, validates proposed full-audit
  coverage, applies full-audit-specific adaptive expansion, or produces its
  audit-specific completion summary. The P1.3 fail-closed guard remains the
  defensive boundary for workflow-owned requests, while isolated action DAGs
  continue through the generic interpreter, ledger, and executor path. Shared
  planning helpers still inherited by `WorkflowRunner` remain in place until
  their later extraction, and the obsolete interpreter prompt fields are left
  intentionally for `P1.5`. Focused verification passed `110` tests across
  `test_command_agent.py` and `test_workflow_v2.py`, `21` selected planning-
  workflow tests in `test_planning.py`, and `43` recovery and chat integration
  tests across `test_agent_runner.py` and `test_assistant_chats.py`. No frontend
  payload or API contract changed, so a frontend build was not required for
  this task.
- `P1.5` completed on 2026-07-21. The canonical action interpreter and adaptive
  planner prompts no longer describe prepared planning, accept a
  `prepared_planning` payload, receive an RCM execution manifest, or reserve
  rollup/report/verification work for a full-audit orchestrator. The prompt
  builders and their `ActionRunner` callers now expose only generic action
  context, and the action runner no longer imports `rcm_execution`. Focused
  tests assert the removed prompt fields and clauses at the direct action,
  bounded-router fallback, workflow-local-routing, and same-schema recovery
  boundaries while retaining the `P1.3` fail-closed guard and isolated action
  execution. Focused verification passed `111` tests across
  `test_command_agent.py` and `test_workflow_v2.py`, plus `43` recovery and
  assistant-routing tests across `test_agent_runner.py` and
  `test_assistant_chats.py`. No frontend payload or API contract changed, so a
  frontend build was not required for this task.
- `P1.6` completed on 2026-07-21. The Phase 1 deletion gate now proves that a
  missing engine remains missing through load and summary projection, dispatch
  fails closed, and the retired `command_runner.py` / `CommandRunner` surface
  has no module, class alias, re-export, or `agent/compat*` package. It also
  proves the obsolete full-audit constants, methods, `rcm_execution` import,
  interpreter inputs, planner inputs, and prompt clauses are absent while the
  P1.3 workflow-request guard and isolated action behavior remain covered.
  The gate deliberately asserts that `ledger.AUDIT_LIFECYCLE_STAGES`,
  `audit_lifecycle`, and `enforce_audit_lifecycle` remain for Phase 2 rather
  than starting `P2.1` early. Focused verification passed `204` tests across
  `test_agent_store.py`, `test_workflow_v2.py`, `test_command_agent.py`,
  `test_agent_runner.py`, `test_agent_api.py`, `test_assistant_chats.py`, and
  `test_planning.py` in `77.54s`. No frontend payload or API contract changed,
  so a frontend build was not required.
- `P2.1` completed on 2026-07-21. Audit ordering and cycle-normalization
  assertions were removed from the action-ledger suite and replaced with exact
  direct-edge, topological closure, and parallel-branch characterization of the
  authoritative workflow capability graph. The action suite retains generic
  created-artifact argument and target resolution, explicit dependency
  injection, cycle detection, duplicate-intent rejection, failure propagation,
  reconciliation, rollback, resume, and undo coverage. This task intentionally
  leaves the `audit_lifecycle` parameter and its callers, lifecycle constants,
  and `enforce_audit_lifecycle(...)` in place for `P2.2` and `P2.3`. Focused
  verification passed `110` tests across `test_command_agent.py` and
  `test_workflow_v2.py` in `17.99s`. No production or frontend contract changed,
  so a frontend build was not required.
- `P2.2` completed on 2026-07-21. `ledger.append_actions(...)` no longer accepts
  or invokes the `audit_lifecycle` switch, and both canonical action-planning
  call sites now use only the domain-neutral append contract. The lifecycle
  constants and unreachable `enforce_audit_lifecycle(...)` helper remain
  intentionally for their direct deletion in `P2.3`. Focused verification
  passed `110` tests across `test_command_agent.py` and `test_workflow_v2.py`
  in `17.97s`. No frontend payload or API contract changed, so a frontend build
  was not required.
- `P2.3` completed on 2026-07-21. The action ledger no longer defines audit
  lifecycle stages or ranks and no longer contains the unreachable lifecycle
  enforcement or traversal helpers. The existing boundary test now asserts
  that these policy surfaces are absent while the generic append contract and
  action-runner boundary remain intact. Focused verification passed `110` tests
  across `test_command_agent.py` and `test_workflow_v2.py` in `17.94s`. No
  frontend payload or API contract changed, so a frontend build was not
  required.
- `P2.4` completed on 2026-07-21. The remaining catalog-specific action names
  used for result significance and created-artifact reference normalization
  moved from `ledger.py` into the action catalog. `ActionDefinition` now
  declares the default significance bit, and the ledger invokes the catalog's
  reference normalizer while retaining generic cycle detection, dependency
  validation, atomic append rollback, created-target normalization,
  preconditions, idempotency, failure propagation, and reconciliation. A
  static exit-gate test rejects APM, RCM, fieldwork, finding, working-paper,
  dashboard, report, or audit-completion policy in the ledger. Focused
  verification passed `111` tests across `test_command_agent.py` and
  `test_workflow_v2.py` in `17.92s`. No frontend payload or API contract
  changed, so a frontend build was not required. Phase 2 is complete; the exact
  next task is `P3.1`.
- `P3.1` completed on 2026-07-21. The new `agent.runtime` package defines small,
  behavior-free, runtime-checkable `RunRuntime` and `ModelGateway` structural
  contracts. `RunRuntime` names the current durable state, event, status,
  activity, warning, checkpoint, input, and approval surface. `ModelGateway`
  exposes one budgeted and attributed completion operation while leaving prompt
  construction and response parsing with callers. Contract tests bind those
  interfaces to the active `BaseRunner` state/event and provider-accounting
  behavior, including durable activity revisions, idempotent status events,
  provider usage, stage attribution, and hash-only provenance. No production
  behavior, provider call, or scheduler call site moved. Focused verification
  passed `152` tests across `test_agent_runtime_contracts.py`,
  `test_agent_runner.py`, `test_command_agent.py`, and `test_workflow_v2.py` in
  `47.95s`. No frontend payload or API contract changed, so a frontend build
  was not required. The exact next task is `P3.2`.
- `P3.2` completed on 2026-07-21. `DefaultModelGateway` now owns the
  process-wide provider/model semaphore, agent-profile calls, pre-call turn and
  estimated-token charging, retry attribution, stage-tag extraction,
  thread-local unit correlation, model-wait activity, debug trace context,
  actual-token reconciliation, per-worker metrics, and hash-only provenance.
  `BaseRunner._llm_content` is now a thin delegation facade; its remaining
  callbacks only bridge durable save/event operations, provenance persistence,
  and template lookup until `RunRuntime` extraction. Contract tests bind the
  concrete gateway to the public protocol, verify delegation, profile and retry
  attribution, stage tags, privacy, and the absence of provider-call behavior
  from `BaseRunner`. Focused verification passed `154` tests across
  `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `48.52s`. No frontend
  payload or API contract changed, so a frontend build was not required. The
  exact next task is `P3.3`.
- `P3.3` completed on 2026-07-21. `DefaultRunRuntime` now owns synchronized run
  saves, event emission, the runtime clock, durable run start/finish markers,
  idempotent status events, warnings, activity revisions, and concurrent
  model-wait projection. `BaseRunner` retains thin delegation methods so the
  active action, workflow, legacy analysis, intake, document-test, and
  document-analysis runners use the runtime without changing their public run
  or event payloads. `DefaultModelGateway` now delegates model-wait activity to
  the runtime and no longer writes activity state or events itself. Contract
  tests cover concrete runtime persistence and timing, facade delegation,
  status/event idempotence, activity revision behavior, model-wait delegation,
  and the absence of extracted projection behavior from `BaseRunner` and
  `DefaultModelGateway`. Focused verification passed `158` tests across
  `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `50.38s`. No frontend
  payload or API contract changed, so a frontend build was not required. The
  exact next task is `P3.4`.
- `P3.4` completed on 2026-07-21. `DefaultRunRuntime` now owns the durable
  model-budget ledger, provider-usage reconciliation, generic dynamic-limit
  updates, the monotonic run deadline, checkpoint pause/resume and cancellation
  checks, and durable draining of steering messages and queued follow-up
  commands. `DefaultModelGateway` retains provider calls, concurrency,
  telemetry, stage attribution, and hash-only provenance while delegating
  budget reservation and reconciliation to the runtime. `BaseRunner` retains
  thin compatibility methods, `WorkflowRunner` supplies only its domain count
  calculation before delegating limit updates, and `ActionRunner` delegates
  command-queue draining. Contract tests cover limit growth, pre-call charging,
  actual-usage reconciliation, retry and per-worker accounting, pause/resume
  deadline extension, cancellation, deadline exhaustion, both inbox modes,
  and removal of the extracted behavior from `BaseRunner` and
  `DefaultModelGateway`. Focused verification passed `162` tests across
  `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `54.37s`. No frontend
  payload or API contract changed, so a frontend build was not required. The
  exact next task is `P3.5`.
- `P3.5` completed on 2026-07-21. `runtime/interactions.py` now owns editable
  approval batches, free-text input waits, structured-interaction waits and
  resolution, durable API response submission, and offline-response recovery.
  Approval and interaction waits restore their blocked status across
  pause/resume, poll the durable record after a restart, and add time blocked
  on the auditor back to the monotonic deadline. `BaseRunner`, `ActionRunner`,
  `WorkflowRunner`, and the public runner control functions now delegate those
  transitions while preserving approval edits, default rejection, action
  response interpretation, queued follow-ups, events, and API payloads.
  Contract tests cover concrete runtime ownership, offline approval and
  structured-interaction recovery, exact deadline extension, facade and
  scheduler delegation, and removal of the extracted transition behavior.
  Focused verification passed `165` tests across
  `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `55.85s`; four targeted
  approval/interaction API and scheduler regression tests also passed in
  `7.02s`. No frontend payload or API contract changed, so a frontend build was
  not required. The exact next task is `P3.6`.
- `P3.6` completed on 2026-07-21. `ActionRunner` now exposes an explicit,
  optional `RunRuntime` constructor dependency and passes it through the
  temporary `BaseRunner` delegation facade. When no runtime is supplied, the
  existing three-argument constructor still creates the same
  `DefaultRunRuntime`, preserving current production construction, action
  scheduling, events, budgets, controls, approvals, interactions, and API
  payloads. The focused contract test proves both the injected-runtime path
  and the unchanged default path. Focused verification passed `166` tests
  across `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `55.23s`. No frontend
  payload or API contract changed, so a frontend build was not required. The
  exact next task is `P3.7`.
- `P3.7` completed on 2026-07-21. `WorkflowRunner` now exposes an explicit,
  optional `RunRuntime` constructor dependency and passes it through the
  current `ActionRunner` inheritance and temporary `BaseRunner` delegation
  facade. When no runtime is supplied, the existing three-argument constructor
  still creates the same `DefaultRunRuntime`, preserving workflow routing,
  stage scheduling, events, budgets, controls, approvals, interactions, and API
  payloads. The audit-specific stage handlers and temporary scheduler
  inheritance remain unchanged for their later planned migrations. The focused
  contract test proves both the injected-runtime path and the unchanged default
  path. Focused verification passed `167` tests across
  `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `56.74s`. No frontend
  payload or API contract changed, so a frontend build was not required. The
  exact next task is `P3.8`.
- `P3.8` completed on 2026-07-21. The Phase 3 gate now runs the active action,
  workflow, intake, document-test, and document-analysis runner classes through
  one durable runtime budget and gateway contract and through the same
  pause/resume/cancel checkpoints. Leaf runners remain independent of both
  graph schedulers while using the shared runtime facade. Process-level crash
  coverage proves an unexpected scheduler exception becomes a durable failed
  terminal state before the next queued command is launched with its parent and
  context intact. An AST gate permits direct `llm.chat` or `llm.chat_stream`
  calls only in `runtime/model_gateway.py`. Focused verification passed `180`
  tests across `test_agent_runtime_contracts.py`, `test_agent_runner.py`,
  `test_command_agent.py`, and `test_workflow_v2.py` in `56.94s`; five real
  intake, document-test, and document-analysis integration tests passed in
  `1.14s`. No production payload or frontend contract changed, so a frontend
  build was not required. Phase 3 is complete; the exact next task is `P4.1`.
- `P4.1` completed on 2026-07-21. The new `agent.context` package defines
  normalized, typed declaration models for context sources,
  representations, global and per-source budgets, deny-by-default content
  privacy permissions, named deterministic selectors, and bounded
  `AutoSelect` declarations. It also defines content-free selection, omission,
  truncation, privacy-decision, size, and manifest records alongside explicitly
  local bundle-item and bundle models. Every model has strict, deterministic
  JSON-object and canonical JSON-string round trips; manifest models contain
  only references, hashes, metrics, and decisions while bundle serialization is
  the only new context contract that can contain supplied source content.
  Registry validation, definition hashing, persistence, and resolution remain
  intentionally unimplemented for `P4.2` through `P4.4A`. Focused verification
  passed `7` tests in `test_agent_context_models.py` in `0.01s`. No active
  runtime path or frontend payload changed, so broader behavioral suites and a
  frontend build were not required. The exact next task is `P4.2`.
- `P4.2` completed on 2026-07-21. The context package now provides typed,
  hash-identified selector and preset registries. Registered selectors declare
  their kind, supported source types, canonical configuration keys,
  implementation hash, deterministic tie-breaker, and reason behavior; bounded
  automatic declarations are checked against per-source item budgets. The
  `documents.policies` preset compiles to a detached normalized `ContextSpec`
  that declares the policy-category document selector used by the later
  resolver.
  Construction and lookup validation rejects duplicate or unknown preset and
  selector keys, unknown selector configuration keys, unhashable
  implementation identities, unsupported source types, and invalid provider,
  sensitive-representation, or row-level privacy combinations. Focused
  verification passed `14` tests in `test_agent_context_models.py` in `0.02s`.
  No active runtime path or frontend payload changed, so broader behavioral
  suites and a frontend build were not required. The exact next task is
  `P4.3`.
- `P4.3` completed on 2026-07-21. Context manifests now have canonical SHA-256
  identities, and source material has stable type-aware hashes. Deterministic
  supplied-size helpers record item, Unicode-character, and estimated-token
  counts; omission and truncation builders record source hashes and before/
  after sizes without retaining source content. Per-unit manifests are written
  atomically to `AgentRuns/<run_id>/contexts/<unit_id>.json`, references carry
  the manifest hash and unit ID, and reads reject missing, malformed, replaced,
  or hash-mismatched sidecars. `RunRuntime` owns the locked persistence/load
  boundary, with `BaseRunner` retaining temporary delegation for active
  callers. The writer accepts only `ContextManifest`, so local `ContextBundle`
  content cannot cross the durable boundary. Focused verification passed `49`
  tests across `test_agent_context_models.py` and
  `test_agent_runtime_contracts.py` in `0.95s`. No active worker, API payload,
  or frontend contract changed, so broader behavioral suites and a frontend
  build were not required. The exact next task is `P4.4`.
- `P4.4` completed on 2026-07-21. `ContextResolver` now resolves normalized
  declarations in source-declaration order and orders candidate results by
  selector rank with a stable source-reference tie-break. It rejects
  undeclared sources, denies unknown or undeclared representations by default,
  records allow/deny privacy decisions, and emits stable, content-free reasons
  for every automatic selection. Per-source selector, item, character, and
  estimated-token limits are applied before the global limits; bounded text is
  truncated deterministically while over-budget non-text values and remaining
  candidates are recorded as omissions. Required-source absence or failure to
  supply a permitted item blocks resolution, while optional absence is
  represented in the manifest. `ContextScope` and `ContextCandidate` provide
  the local-only input boundary for later domain adapters and selector
  implementations; no active worker or domain loader was moved in this slice.
  Focused verification passed `54` tests across
  `test_agent_context_models.py`, `test_agent_context_resolver.py`, and
  `test_agent_runtime_contracts.py` in `0.98s`. No active runtime, API, or
  frontend contract changed, so broader behavioral suites and a frontend build
  were not required. The exact next task is `P4.4A`.
- `P4.4A` completed on 2026-07-21. Selector execution is now a closed,
  data-only resolver boundary that accepts only deterministic metadata,
  deterministic lexical, or hash-identified local-embedding strategies.
  Adapters supply metadata, lexical text, or numeric vectors rather than ranks
  or opaque reasons; the resolver computes stable scores and always applies a
  registered source-reference or declared-reference tie-break. Selector
  registrations accept definitions rather than executable callables, selector
  inputs reject non-data service objects, and static tests prohibit
  `ModelGateway`, provider-client, or network imports and injection points in
  the selector implementation modules. Local-embedding definitions and query
  inputs must agree on SHA-256 model and index identities, both hashes
  participate in selector identity, and mismatches fail before selection.
  Focused verification passed `59` tests across
  `test_agent_context_models.py`, `test_agent_context_resolver.py`, and
  `test_agent_runtime_contracts.py` in `0.90s`. No active worker, domain
  adapter, API payload, or frontend contract changed, so broader behavioral
  suites and a frontend build were not required. The exact next task is
  `P4.5`.
- `P4.5` completed on 2026-07-21. The APM context preset now declares bounded
  document-summary and methodology-excerpt sources selected by registered
  provider-free lexical strategies. The new APM domain adapter populates only
  data-only resolver candidates: document content is composed through
  `document_context.get_document_context(...)`, preserving current-analysis
  validity and auditor overrides, while methodology candidates come from the
  same indexed-section inventory now shared by `methodology.search(...)`.
  Neither extraction, document search, analysis-sidecar reads, nor methodology
  indexing logic is copied into the agent context package. Adapter integration
  tests prove deterministic real-workspace selection, local-only bundle
  content, content-free manifests, resolver-owned truncation and omission, and
  no document activity side effect before worker invocation. Focused
  verification passed `62` tests across `test_agent_context_adapters.py`,
  `test_agent_context_models.py`, `test_agent_context_resolver.py`, and
  `test_agent_runtime_contracts.py` in `0.98s`; `44` related document,
  methodology, search, and planning regression tests passed across
  `test_documents.py`, `test_document_analysis_search.py`, and
  `test_planning.py` in `20.33s`. No active capability, API payload, or
  frontend contract changed, so a frontend build was not required. The exact
  next task is `P4.6`.
- `P4.6` completed on 2026-07-21. The normalized `planning.apm` preset now
  declares bounded table-metadata and table-profile sources selected by the
  registered deterministic `tables.all` strategy. The APM adapter delegates to
  the existing assistant schema and profile builders; its profile projection
  disables category literals and never calls the row-preview projection, while
  preserving schema, row-count, type, null, distinct, and numeric/date summary
  metadata. Row-level `table_rows` representations are structurally rejected
  when candidates enter the resolver boundary and again when manifest
  selections or local bundle items are constructed, before any worker can
  receive them. Adapter tests prove real-workspace metadata/profile supply,
  omission of a sentinel row value, content-free manifests, reuse of the
  established projection choke points, and the absence of direct frame or
  profiler logic in the adapter. Focused verification passed `65` tests across
  `test_agent_context_adapters.py`, `test_agent_context_models.py`,
  `test_agent_context_resolver.py`, and `test_agent_runtime_contracts.py`; `43`
  related assistant/profile and workflow privacy regression tests also passed,
  for `108 passed` in `2.08s`. No active capability, API payload, or frontend
  contract changed, so a frontend build was not required. The exact next task
  is `P4.7`.
- `P4.7` completed on 2026-07-21 by making context policy explicitly
  declaration-only. Registered application capability and preset definitions
  are the sole authority for permitted sources, selectors, representations,
  budgets, and privacy; neither `ContextSpec` nor `ContextManifest` is a
  workspace or per-run auditor-edit surface. Auditors retain domain-level
  source curation and explicit regeneration, which resolves the then-current
  candidate set without widening policy. Model and registry documentation now
  state this boundary, and contract tests prove unknown top-level policy
  overrides and source-reference selections are rejected during normalized
  deserialization. Focused verification passed `67` tests across
  `test_agent_context_adapters.py`, `test_agent_context_models.py`,
  `test_agent_context_resolver.py`, and `test_agent_runtime_contracts.py` in
  `0.97s`. No active capability, API payload, or frontend contract changed, so
  a frontend build was not required. The exact next task is `P4.8`.
- `P4.8` completed on 2026-07-22. The real `planning.apm_ready`
  capability now declares the normalized `planning.apm` preset and obtains
  every model-facing input through `ContextResolver`: planning context and
  ownership, the active template, the current APM, bounded table metadata and
  profiles, current document analyses, and methodology excerpts. Its pure APM
  worker receives only the local `ContextBundle`, has no workspace parameter or
  lookup, and the content-free `ContextManifest` is durably persisted before
  the provider call. APM proposal sidecars now carry an exact execution
  identity covering the capability declaration, unit input, manifest, context
  spec, resolver, every declared selector definition, prompt, worker,
  response schema, and proposal schema; recovery rejects and regenerates a
  cached proposal when either the context spec or a selector implementation
  hash changes. Integration coverage proves real-workspace deterministic
  selection, local-only methodology content, content-free manifests and
  provenance, row-value exclusion, manifest availability before worker
  invocation, and no document activity side effect at that boundary. Existing
  structurally usable artifacts still bypass materialization and explicit
  `force` resolves the then-current candidates; no freshness watch, automatic
  invalidation, or stale propagation was added. The focused context/workflow
  gate passed `120` tests in `11.62s`, related planning/document regressions
  passed `44` tests in `23.55s`, and the final full backend Phase 4 gate passed
  `560` tests in `99.27s`. No API or frontend payload changed, so a frontend build
  was not required. Phase 4 is complete; the exact next task is `P5.1`, and no
  P5.1 work has started.
- `P5.1` completed on 2026-07-22. The new `agent.workers` package defines
  immutable, detached worker requests and validated proposal results, typed
  attempt and repair-policy contracts, hash-identified response schemas and
  worker definitions, and a duplicate/unknown-safe registry. The registry owns
  one common response-validation loop with a hard maximum of two repair turns,
  bounded error count and guidance characters, immediate failure for worker or
  schema implementation contract violations, and structural enforcement that
  execution receives the shared `ModelGateway`. Requests accept only an
  already-resolved local `ContextBundle`, matching capability/unit identity,
  JSON unit input, and activity metadata; results expose no raw model response.
  Focused verification passed `16` tests in
  `test_agent_worker_models.py`. No active worker, scheduler, provider call,
  persisted payload, API, or frontend contract changed, so broader behavioral
  suites and a frontend build were not required. The exact next task is
  `P5.2`.
- `P5.2` completed on 2026-07-22. The new `agent.executors` package defines
  immutable, detached executor requests, implementation results, durable
  receipt payloads, typed interrupted-commit reconciliation outcomes, and a
  duplicate/unknown-safe hash-identified registry. Each executor definition
  declares either strict workspace-revision CAS or material parent-hash
  concurrency. The registry rejects missing or extraneous guards, validates
  executor/capability/unit identity, exact applied parent hashes, revision
  claims, artifact postcondition coverage, and ambiguous reconciliation
  outcomes before producing a receipt. Reconciliation can prove an interrupted
  commit already applied and produce a marked receipt without invoking the
  executor again; not-applied and conflict outcomes cannot produce receipts.
  The contract layer imports no workspace implementation, transaction helper,
  model gateway, context resolver, run store, or scheduler; domain executors
  will supply the actual CAS/mutation behavior in `P5.6`. Focused verification
  passed `39` tests across `test_agent_executor_models.py` and
  `test_agent_worker_models.py`; backend compile validation also passed. No
  active executor, scheduler, persisted run, API, or frontend contract changed,
  so broader behavioral suites and a frontend build were not required. The
  exact next task is `P5.3`.
- `P5.3` completed on 2026-07-22. The new runner-independent
  `runtime.unit_pipeline` service sequences declared context resolution,
  content-free manifest persistence, registered worker execution, atomic
  semantic-unit proposal persistence, optional approval, registered executor
  execution, atomic receipt persistence, and post-commit readiness evaluation.
  Auditor-edited accepted proposals replace the proposed sidecar before any
  mutation, rejected approvals never invoke an executor, and a failed
  post-commit readiness check occurs only after the receipt is durable. The
  concrete sidecar store establishes the target `proposals/<unit_id>.json` and
  `receipts/<unit_id>.json` layout without importing either scheduler or audit
  domain policy. Focused verification passed `44` tests across
  `test_agent_unit_pipeline.py`, `test_agent_executor_models.py`, and
  `test_agent_worker_models.py`. No live capability uses the service yet and no
  API or frontend contract changed. The exact next task is `P5.3A`.
- Clean-slate cutover is an explicit project assumption: all pre-cutover
  workspaces, runs, chats, artifacts, and debug records are disposable and
  unsupported after cutover.
- Recovery requirements apply only to runs created by the target schema. The
  target has no run-shape inference, conversion, historical projection, or
  legacy resume path.
- `agent-architecture.md` is authoritative for target runtime behavior and
  contract semantics. Section 7 of this plan is authoritative for the target
  package tree and incremental file locations.
- Capability definitions use stable registry keys and normalized
  content/implementation hashes; manual component versions and `_v1` key
  suffixes are not part of the target design.
- Generalized freshness assessment, invalidation watches, and automatic stale
  propagation are deferred. Existing structurally usable artifacts are reused
  with `currency_status="not_assessed"`; the auditor decides when to regenerate.
- New workflow runs use `reuse_existing` by default and `force` only for an
  explicit improve, generate-again, regenerate, or refresh instruction.
- Automatic context selectors cannot call `ModelGateway` or a provider service.
  Provider-model selection is modeled as a worker capability instead.
- Manual component versions, `_v1` registry-key suffixes, and initial run-schema
  versions are intentionally omitted; normalized content and implementation
  hashes provide execution identity.

## 2. Outcomes And Non-Goals

### Required Outcomes

- One authoritative audit dependency graph in `agent/workflows/audit.py`.
- A domain-neutral `WorkflowRunner` with no APM, RCM, fieldwork, document,
  finding, dashboard, or report handlers.
- An `ActionRunner` that retains the useful generic action planner and ledger
  but contains no audit-lifecycle policy.
- Shared runtime services for persistence, events, checkpoints, budgets,
  controls, interactions, approvals, and model accounting.
- Auditable, normalized, hash-identified context declarations in every
  model-backed capability.
- Workers that only transform supplied context into validated proposals.
- Executors that deterministically commit proposals using parent hashes or
  compare-and-swap and return receipts.
- One document-analysis map/reduce implementation shared by standalone
  document analysis and audit planning.
- A declared exploratory data-analysis workflow for requests involving table
  relationships, joins, and relevant analysis generation.
- One clean target run shape with explicit engine selection and same-schema
  recovery.
- Direct removal of v1 after its active callers migrate.

### Non-Goals

- Redesigning the assistant drawer or audit screens.
- Changing workspace domain schemas unless a migration requirement is
  identified and separately approved.
- Sending row-level table data to the provider.
- Replacing Polars, document extraction, report services, or other domain
  services that already have suitable deterministic APIs.
- Replacing the action catalog with arbitrary model tools.
- Supporting or converting disposable pre-cutover workspaces and run history.
- Combining v1 retirement with the initial action/workflow separation.

## 3. Delivery Principles

1. Preserve active behavior before reorganizing it. Add characterization tests
   before changing routing, dispatch, current-schema persistence, or recovery.
2. Separate by responsibility, not by artifact name. Runners schedule;
   capabilities declare; resolvers gather; workers generate; executors mutate.
3. Move code before improving prompts. Prompt or context-quality changes mixed
   with structural changes make regressions difficult to attribute.
4. Maintain one target persisted shape. Before release it may change directly;
   tests and disposable development workspaces move with it. Do not add readers
   or projections for superseded shapes.
5. Give each capability family one implementation before deleting the old
   caller. Temporary delegation may bridge active call sites within a phase, but
   it does not read old records and is deleted with the final caller.
6. Make privacy enforcement structural. Context permissions and budgets are
   validated before a worker is invoked, not left to prompt instructions.
7. Persist before side effects. Model proposals and context manifests are
   durable before approval or commit; executor receipts are durable after a
   successful commit.
8. Keep commits small enough to review and revert by phase or capability
   family.

## 4. Target Runtime Contract Implementation Rules

The normative `RunRuntime`, capability, context, worker, executor, explicit
regeneration, integrity, and routing contracts live in
[agent-architecture.md](agent-architecture.md).
This plan does not maintain a second copy of those contracts. The following are
migration-specific implementation rules:

- Domain-specific types must not leak into the runtime package.
- Initial runtime extraction may wrap existing `BaseRunner` behavior through
  delegation while live callers migrate; the wrapper is not a persisted-run
  compatibility layer.
- Capability declarations serialize only stable registry keys for readiness,
  unit expansion, context, workers, executors, and approval.
  Registered Python callables implement those keys but are not identities; the
  normalized declaration plus registered component content/implementation hashes
  form the capability-definition hash.
- Registry validation runs at construction or startup and rejects missing or
  unhashable references, duplicate IDs, dependency cycles, and invalid privacy
  combinations.
- Exact proposal execution identity protects recovery and billing reuse. The
  target framework does not maintain a second identity for committed-artifact
  currency.
- Context selectors remain deterministic local resolver components and never
  call `ModelGateway`. Any provider-model selection is a declared worker
  capability with normal proposal, budget, approval, and recovery behavior.
- `reuse_existing` and `force` are the normalized generation modes. Existing
  output is reused without a freshness claim unless the auditor explicitly
  requests regeneration.
- Section 7 of this plan is the sole authoritative target package tree.

## 5. Persisted State Strategy

### Clean-Slate Rule

The target starts with an empty application workspace root. Pre-cutover
`run.json`, workspace, chat, artifact, and debug shapes are unsupported inputs
and are removed or moved outside `Workspaces/` before cutover. Every new run
persists an explicit engine:

```text
engine = "workflow" | "action"
```

If Phase 10 proves that intake or document tests require a distinct scheduling
protocol, it may add a plainly named current engine value. It must not add a
`compat` value or a reader for the old record shape. Same-schema recovery begins
only after a run has been created by the target writer.

### New Workflow Records

Each workflow run should persist:

- Workflow definition ID and normalized definition hash.
- Requested outcomes and normalized scope.
- Materialized capability stages.
- Semantic unit IDs and unit input hashes.
- Capability-definition hash.
- Context-policy hash and manifest reference, including selected-source hashes.
- Worker ID, prompt hash, and response-schema hash.
- Exact proposal execution-identity hash.
- Proposal sidecar reference and proposal hash.
- Approval or interaction reference.
- `reuse_existing` or `force` generation mode.
- Executor ID, receipt, and committed artifact references.
- Readiness result before and after execution.
- Failure, conflict, retry, and recovery state.

### Sidecar Layout

Use this single target layout under `AgentRuns/<run_id>/`:

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
- A changed parent or auditor-edited target becomes `conflict`; it is not
  silently overwritten.
- Readiness is always re-evaluated after recovery and after each commit.

## 6. Phased Implementation

### Phase 0: Baseline And Characterization

**Objective:** Freeze the behavior that must survive the refactor.

**Tasks:**

- [x] `P0.1` Inventory every active run-creation, routing, API, UI, chat, intake,
  document-test, and document-analysis caller; record the clean-slate cutover
  boundary and the requirement that `Workspaces/` be empty.
- [x] `P0.2` Characterize same-schema recovery behavior for unstarted,
  partially committed, approval-blocked, interaction-blocked,
  interrupted-provider, and completed runs created during a test.
- [x] `P0.3` Characterize deterministic local routing, bounded-router fallback,
  generic action-interpreter fallback, and fail-closed broad-audit behavior.
- [x] `P0.4` Characterize SSE events, activity records, run projections,
  approvals, interactions, queued commands, retry, and continue behavior.
- [x] `P0.5` Characterize workflow materialization, semantic unit IDs,
  readiness and staleness, stable parallel results, serialized commits,
  sidecar reuse, and conflict recovery.
- [x] `P0.6` Characterize generic action DAG validation, repair,
  preconditions, idempotence, reconciliation, undo, resume, and failure
  propagation without changing active behavior.
- [x] `P0.7` Add provider-accounting and privacy assertions covering budgets,
  concurrency, table-row exclusion, bounded document context, and hash-only
  provenance.
- [x] `P0.8` Run the Phase 0 focused suites, prove legacy records are outside
  the supported contract, and update the phase gate and current status.

**Work:**

- Inventory live creators and consumers rather than historical persisted shapes.
  Tests may create current records and restart them within one test, but no
  pre-cutover fixture directory is maintained.
- Characterize deterministic routing for all goal templates and common phrases.
- Characterize unknown-command fallback to the bounded router and generic
  action interpreter.
- Characterize current SSE event types, activity projections, approval records,
  interaction records, and assistant-chat run projections.
- Characterize v3 materialization, semantic unit stability, readiness skipping,
  staleness, bounded parallel execution, stable commit order, and conflict
  handling.
- Characterize current stale scheduling only to define the intentional Phase 6
  replacement with `reuse_existing` and explicit `force`; no legacy behavior is
  retained.
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

**Exit gate:** Tests demonstrate that broad audit requests bypass the v2
interpreter, isolated mutations still use the action graph, current-schema
recovery works, and pre-cutover records are explicitly unsupported.

### Phase 1: Remove V2 Full-Audit Policy

**Objective:** Make the action scheduler genuinely generic and delete its
obsolete full-audit policy without preserving old run shapes.

**Tasks:**

- [x] `P1.1` Add the required `engine` field to every new run writer and dispatch
  only current records by that field; add no inference or schema-version field.
- [x] `P1.2` Rename `CommandRunner` and its module to `ActionRunner`, update all
  live imports and tests in the same task, and retain no alias.
- [x] `P1.3` Add a fail-closed guard so broad-audit and planning
  requests cannot enter the canonical action planner.
- [x] `P1.4` Remove planning preparation, audit action insertion, audit budget
  reservations, and full-audit validation from the canonical `ActionRunner`.
- [x] `P1.5` Remove obsolete interpreter prompt clauses and update focused
  action, workflow, recovery, and routing tests.
- [x] `P1.6` Prove the deletion gate, the absence of legacy readers and aliases,
  and update current status.

**Work:**

- Rename `CommandRunner` and `command_runner.py` directly to `ActionRunner` and
  `action_runner.py`; update all live imports in the same task.
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

**Primary files:**

- `backend/app/agent/command_runner.py` -> `action_runner.py`
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

- [x] `P2.1` Convert audit-lifecycle normalization tests into workflow graph
  tests while retaining generic DAG characterization.
- [x] `P2.2` Remove `audit_lifecycle` from `append_actions(...)` and its
  canonical callers.
- [x] `P2.3` Remove audit lifecycle constants and enforcement from
  `ledger.py`, then run the action and workflow focused suites.
- [x] `P2.4` Prove the domain-neutral ledger exit gate and update status.

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

- [x] `P3.1` Define the small `RunRuntime` and `ModelGateway` public contracts
  plus active-behavior contract tests, without moving behavior.
- [x] `P3.2` Extract the provider semaphore, model profiles, token charging,
  retries, stage tags, telemetry, and hash-only provenance into
  `ModelGateway`; make `BaseRunner` delegate to it.
- [x] `P3.3` Extract run save, event emission, activity projection, status, and
  durable timing operations into `RunRuntime` with delegation from
  `BaseRunner`.
- [x] `P3.4` Extract budgets, dynamic limits, deadlines, checkpoints,
  pause/resume, cancellation, and inbox draining into `RunRuntime`.
- [x] `P3.5` Extract approval and structured-interaction transitions, including
  blocked-time deadline extension and restart behavior.
- [x] `P3.6` Inject the runtime into `ActionRunner` while retaining the
  current API behavior.
- [x] `P3.7` Inject the runtime into the existing `WorkflowRunner` without yet
  making it domain-neutral or moving its stage handlers.
- [x] `P3.8` Prove shared budgets, controls, queued follow-ups, terminal crash
  handling, active leaf-runner behavior, and the no-direct-provider-call gate.

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
- Keep `BaseRunner` only as a temporary delegation facade for live v1 and leaf
  callers. Its methods delegate to `RunRuntime`; delete the facade when the last
  caller migrates and do not maintain two implementations.
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
  unchanged through the API.
- A live leaf runner and a graph runner can both use the runtime
  without inheriting from one another.

**Exit gate:** New graph-runner code contains no direct store writes or direct
provider calls outside the runtime APIs.

### Phase 4: Introduce Context Contracts And Resolver

**Objective:** Make capability context selection complete, bounded, inspectable,
and governed by hash-identified, code-authored declarations.

**Tasks:**

- [x] `P4.1` Define normalized `ContextSpec`, source, representation, budget,
  privacy, selector, manifest, and bundle models with serialization tests.
- [x] `P4.2` Add preset and selector registries with duplicate, unknown,
  unhashable, unsupported-source, and invalid-privacy validation.
- [x] `P4.3` Implement deterministic manifest identity, atomic persistence,
  source hashing, omission records, truncation records, and supplied-size
  metrics without persisting bundle content.
- [x] `P4.4` Implement resolver ordering, global and per-source limits,
  required/optional source behavior, deny-by-default representations, and
  stable automatic-selection reasons.
- [x] `P4.4A` Enforce that automatic selectors cannot call `ModelGateway` or a
  provider/network service; support only deterministic metadata, lexical, or
  hash-identified local-embedding strategies with stable tie-breaking and
  identity tests.
- [x] `P4.5` Adapt existing document and methodology context builders for the
  APM slice without copying their domain logic.
- [x] `P4.6` Adapt table metadata/profile context and structurally reject
  row-level representations before worker invocation.
- [x] `P4.7` Specify and implement the intended auditor-editable surface for
  context selection, or amend the objective and contracts to make the scope
  explicitly declaration-only.
- [x] `P4.8` Route one real capability through `ContextResolver`, verify
  proposal-reuse rejection on spec/selector changes, and prove the privacy exit
  gate without adding automatic freshness monitoring.

**Work:**

- Add typed models for `ContextSpec`, source declarations, representations,
  budgets, privacy policy, deterministic selectors, and `AutoSelect`.
- Add a preset registry. Implement concise presets such as
  `documents.policies`, but normalize them into the typed form before use.
- Add a selector registry. Every automatic selector has a stable ID, normalized
  configuration, implementation hash, supported source type, deterministic
  tie-breaking, item limit, and reason output.
- Keep selectors provider-free. A fixed local embedding strategy is permitted
  only when its model/index hashes and deterministic tie-breaking participate in
  selector identity. Provider-model judgment must be a separate worker
  capability.
- Run automatic selectors only while materializing missing or explicitly forced
  work. Do not monitor candidate inventories or rerun selectors when an existing
  artifact is being reused.
- Add `ContextResolver.resolve(workspace, capability, unit, scope)` returning a
  manifest and bundle.
- Implement manifest persistence through `RunRuntime` before the model call.
- Port existing bounded context builders behind adapters rather than copying
  them. Preserve current privacy choke points in `context_bundles.py`,
  `document_context.py`, `document_search.py`, `model_context.py`, and
  assistant context helpers where applicable.
- Reject undeclared sources, row-level table representations, unknown presets,
  unknown selector keys or unhashable definitions, and over-budget bundles
  before worker execution.
- Define deterministic ordering and truncation so the same inputs produce the
  same manifest and hash.
- Keep context policy declaration-only: registered application capability and
  preset definitions are authoritative. Auditor source curation and explicit
  regeneration may change the candidates resolved under that policy, but no
  per-run or per-workspace override may widen sources, selectors,
  representations, budgets, or privacy.

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
- A named `auto` strategy is bounded, hash-identified, stable, and explains every
  selected item.
- Required-source absence blocks the unit; optional-source absence is recorded.
- Global and per-source budgets are enforced deterministically.
- Document truncation and omission are recorded in the manifest.
- Table metadata and profiles are permitted while table rows are rejected.
- Bundle content is local; persisted manifests and provider provenance contain
  references, hashes, metrics, and decisions only.
- Context-spec or selector definition-hash changes invalidate proposal reuse.
- Reusing an existing artifact does not rerun automatic selection; forcing its
  regeneration resolves the then-current candidate set.
- Selector tests fail if a strategy imports or receives `ModelGateway`, and a
  local-embedding selector records its model/index identity.

**Exit gate:** At least one real workflow capability obtains all model context
through `ContextResolver`, and its worker has no workspace access.

### Phase 5: Introduce Worker And Executor Interfaces

**Objective:** Establish the model-generation/mutation boundary before moving
all audit stages.

**Tasks:**

- [x] `P5.1` Define immutable worker request/result models, hash-identified
  registry metadata, response-schema validation, and bounded repair contracts.
- [x] `P5.2` Define executor request/result and receipt models, registry
  metadata, parent-hash/CAS requirements, and reconciliation contracts.
- [x] `P5.3` Implement a runner-independent unit-pipeline service for context,
  manifest, worker, proposal, approval, executor, receipt, and readiness
  sequencing.
- [ ] `P5.3A` Implement and persist exact proposal execution identities with
  explicit rejection reasons for incompatible sidecar reuse; do not introduce
  a committed-artifact freshness identity.
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

- Add worker request/result types, worker registry validation, prompt/hash
  metadata, response schema validation, and bounded repair handling.
- Add executor request/result types, executor registry validation, parent hash
  checks, receipts, and interrupted-commit reconciliation.
- Add a generic workflow unit pipeline:
  resolve context -> persist manifest -> invoke worker -> persist proposal ->
  approval if declared -> invoke executor -> persist receipt -> reevaluate
  readiness.
- Treat capability-definition, prompt, worker, response-schema, selector,
  resolver, exact-context, selected-source, and unit-input changes as
  proposal-reuse invalidators. These hashes protect an uncommitted execution;
  they do not assess the currency of an already committed artifact.
- Select `planning.apm_ready` as the first vertical slice because it exercises
  declared dependencies, document/methodology context, one model proposal,
  approval policy, CAS commit, quality validation, and explicit regeneration.
- Extract the APM prompt and validator from current planning helpers into a
  planning worker.
- Extract APM mutation and reconciliation into a planning executor.
- Keep output shape and workspace fields unchanged.

**Primary files:**

- New `backend/app/agent/workers/model.py`
- New `backend/app/agent/workers/planning.py`
- New `backend/app/agent/executors/model.py`
- New `backend/app/agent/executors/planning.py`
- New `backend/app/agent/runtime/unit_pipeline.py`
- `backend/app/agent/workflow_runner.py`
- `backend/app/agent/audit_workers.py`
- `backend/app/agent/action_runner.py`

**Tests:**

- Worker unit tests use constructed bundles and require no workspace fixture.
- Executor tests use proposals and workspace fixtures with no model stub.
- A worker cannot mutate workspace state.
- An executor has no gateway dependency and cannot call a model.
- Proposal persistence happens before approval and commit.
- Resume after proposal generation does not rebill the provider.
- A prompt, worker, selector, or selected-source change rejects an incompatible
  uncommitted proposal sidecar but does not assess or change the currency of an
  already committed APM.
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
  scheduler without switching active dispatch.
- [ ] `P6.3` Implement normalized `reuse_existing` and `force` generation modes
  and label reused artifact currency as not assessed; delete legacy
  `missing_or_stale` scheduling rather than adapting it.
- [ ] `P6.4` Replace domain handler dispatch with capability, worker, executor,
  and context registry lookup through the Phase 5 unit pipeline.
- [ ] `P6.5` Inject routing results into the scheduler and remove scheduler
  fallback calls to the action interpreter; leave final routing consolidation
  for Phase 11.
- [ ] `P6.6` Switch active workflow dispatch to the composed scheduler and
  update live imports, then delete the old scheduler module without an adapter.
- [ ] `P6.7` Add and pass import-boundary enforcement for runtime modules.
- [ ] `P6.8` Prove parity for dynamic expansion, partial failure, deterministic
  commit order, next outcomes, and the no-inheritance/no-domain-handler gate.

**Work:**

- Move generic graph materialization, recovery, stage scheduling, stable
  all-settled execution, unit transitions, and finish logic into
  `agent/runtime/workflow_runner.py`.
- Replace `class WorkflowRunner(ActionRunner)` with a class that receives
  `RunRuntime`, workflow definitions, capability/worker/executor registries,
  and `ContextResolver`.
- Move reusable quality checks and matching functions out of
  `ActionRunner`. Put pure domain checks near the relevant worker or executor;
  put cross-artifact readiness checks near capabilities.
- Default ordinary outcome requests to `reuse_existing`. Existing structurally
  usable outcomes are reused with `currency_status="not_assessed"`; missing
  prerequisites are still materialized.
- Map explicit improve, generate-again, regenerate, and refresh instructions to
  `force`, which rematerializes the requested outcome closure and resolves
  context at execution time. Do not regenerate because of workspace mutation,
  application startup, hash comparison, or readiness observation alone.
- Delete legacy `missing_or_stale` scheduling. New runs never write or propagate
  stale state.
- Replace `_run_stage` domain dispatch with registry lookup and the generic
  unit pipeline.
- Move workflow resolution out of the runner into `agent/routing.py`.
- Delete `_adopt_legacy` and all run-shape translation; target dispatch accepts
  only records written by the target schema.
- Preserve stage-level dependency ordering, bounded unit parallelism,
  all-settled failures, deterministic result ordering, serialized commits,
  dynamic expansion, and `next_outcomes`.

**Primary files:**

- `backend/app/agent/workflow_runner.py` -> deletion after live imports move
- New `backend/app/agent/runtime/workflow_runner.py`
- New `backend/app/agent/routing.py`
- `backend/app/agent/workflow.py`
- `backend/app/agent/action_runner.py`
- `backend/app/agent/runner.py`

**Tests:**

- A synthetic non-audit registry runs through the scheduler without importing
  any audit module.
- Dependency closure, readiness skipping, unit expansion, recovery, stable
  ordering, sidecar reuse, and partial failure behavior match existing v3.
- `reuse_existing` makes no provider call for a present structurally usable
  outcome and reports currency as not assessed; `force` regenerates the explicit
  closure, while both modes retain sidecar and receipt recovery guarantees.
- The runtime package passes an import-boundary test that rejects imports from
  planning, RCM, document, findings, report, or audit capability modules.
- Workflow routing never calls the action interpreter for recognized outcomes.

**Exit gate:** `WorkflowRunner` does not inherit from `ActionRunner`, and it has
no methods named for domain stages.

### Phase 7: Move The Audit Workflow And Capability Families

**Objective:** Make one file authoritative for audit dependencies and migrate
the remaining v3 handlers to declarations, workers, and executors.

**Tasks:**

- [ ] `P7.1` Create `workflows/audit.py` with hash-identified workflow metadata and
  the current dependency graph, preserving capability IDs and ordering while
  updating live imports directly.
- [ ] `P7.1A` Add golden edge, closure, and parallel-branch tests for the
  baseline audit DAG, and document how a later Phase 9 graph-definition change
  may add scoped document-analysis dependencies without making them global.
- [ ] `P7.2` Add registry composition and startup validation for grouped audit
  capability, context, worker, and executor modules before moving a writer.
- [ ] `P7.2A` Define existence/structural readiness for every audit capability,
  remove stale-state scheduling from new declarations, and delete legacy stale
  projections.
- [ ] `P7A.1` Move `planning.context_ready` readiness and unit
  expansion into the planning capability module with golden identity tests.
- [ ] `P7A.2` Extract context synthesis and planning-context commit behavior,
  switch the capability to the registered pipeline, and remove its old handler.
- [ ] `P7B.1` Move the completed `planning.apm_ready` declaration and registry
  wiring into the grouped planning modules without changing identity.
- [ ] `P7B.2` Remove the temporary APM vertical-slice adapter and prove the
  Phase 5 behavior through the authoritative audit workflow.
- [ ] `P7C.1` Move `planning.rcm_ready` readiness, semantic units,
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
  executors, switch their writer, and prove attachment/linkage parity.
- [ ] `P7F.1` Move fieldwork-execution readiness, attempt limits,
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
  grouped modules at all live imports, then delete both old modules in the same
  task.
- [ ] `P7.4` Prove the phase gate, authoritative graph uniqueness, import
  boundaries, full workflow closure, frontend projections, and status update.

**Work:**

- Create `agent/workflows/audit.py` with the complete dependency graph and
  workflow metadata.
- Preserve these exact baseline dependencies when Phase 7 first creates the
  authoritative graph:

| Capability | Direct dependencies |
|---|---|
| `planning.context_ready` | none |
| `planning.apm_ready` | `planning.context_ready` |
| `planning.rcm_ready` | `planning.apm_ready` |
| `planning.planned_tests_ready` | `planning.rcm_ready` |
| `fieldwork.definitions_ready` | `planning.planned_tests_ready` |
| `fieldwork.executed` | `fieldwork.definitions_ready` |
| `results.rolled_up` | `fieldwork.executed` |
| `findings.drafted` | `results.rolled_up` |
| `working_papers.generated` | `results.rolled_up` |
| `dashboard.curated` | `results.rolled_up` |
| `report.working_draft` | `planning.apm_ready`, `results.rolled_up`, `findings.drafted` |
| `audit.verified` | `working_papers.generated`, `dashboard.curated`, `report.working_draft` |

Working papers, dashboard curation, and finding/report work therefore branch
after rollup; the workflow is not a linear chain. Phase 9 may change the workflow
definition hash with scoped `documents.analysis_generated` edges where
declared, but must not make document analysis a universal audit prerequisite.
- Move readiness and unit expansion from `audit_capabilities.py` into grouped
  capability modules without changing their semantic IDs.
- Replace new-scheduler stale decisions with existence and structural-usability
  checks. Retain source and parent hashes where they serve provenance, proposal
  reuse, CAS, or conflict detection, but do not use them to claim currency or
  schedule regeneration.
- Keep existing capability IDs and semantic unit-ID construction stable.
- Add explicit normalized, hash-identified context specs to every model-backed
  capability.
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
  declarations from the authoritative audit workflow and update callers
  directly.

**Primary files:**

- New `backend/app/agent/workflows/audit.py`
- New `backend/app/agent/capabilities/planning.py`
- New `backend/app/agent/capabilities/fieldwork.py`
- New `backend/app/agent/capabilities/reporting.py`
- New grouped worker and executor modules
- `backend/app/agent/audit_capabilities.py` -> deletion
- `backend/app/agent/audit_workers.py` -> split, then deletion

**Per-slice gate:** Existing unit IDs, artifact shapes, approvals, interactions,
and receipts remain stable. New readiness preserves existence and structural
validation while intentionally deferring currency assessment. Delete old stale
projections and the old handler as soon as its slice passes; do not keep dual
writers.

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
  hashes used for provenance and proposal reuse, not automatic freshness.
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
  hashes, CAS, reconciliation, receipts, and explicit-force/conflict behavior.
- [ ] `P9.8` Separate generated and auditor-reviewed readiness and preserve
  status, review, citation, and activity projections.
- [ ] `P9.9` Route standalone analysis and audit-planning dependencies through
  the same workflow implementation.
- [ ] `P9.10` Migrate every live `DocumentAnalysisRunner` caller to the workflow,
  then delete the runner and every duplicate map/reduce implementation.
- [ ] `P9.11` Prove billing reuse, explicit forced replacement, format parity,
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
- Update standalone and planning callers to request the document workflow
  directly, then delete `DocumentAnalysisRunner`; no old
  `kind="document_analysis"` record is supported.
- Remove `ActionRunner._ensure_planning_analysis` and any second chunk/reduce
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
- Reuse does not assess document-analysis currency; explicit force resolves the
  current document content and source hashes.
- Citation validation, review conflicts, explicit-force semantics, status projection,
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
  budgets, and same-schema restart behavior.
- [ ] `P10.4` Inventory document-test scheduling requirements and write the
  required decision record against fan-out, comparisons, disposition,
  attachments, evidence anchors, linked writes, and recovery.
- [ ] `P10.5` Implement the document-test decision and route all model calls
  through the injected gateway without duplicating `doc_tests.run_item`.
- [ ] `P10.6` Prove per-item resume, comparison/disposition behavior,
  RCM-linked receipts, privacy, budgets, and same-schema recovery.
- [ ] `P10.7` Update the authoritative package migration to reflect both
  decisions, delete superseded live runners, then run the phase integration gate
  and update status.

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

- [ ] `P11.1` Finalize the explicit current engine values from Phase 1 and any
  protocol-specific Phase 10 decision; reject records without a supported
  engine and retain no schema inference.
- [ ] `P11.2` Move deterministic templates, phrases, outcome validation, and
  action-intent validation into pure `agent/routing.py` functions.
- [ ] `P11.2A` Implement deterministic routing precedence for registered
  outcomes, workflow-owned generation/refresh, target-specific operations,
  scope-wide execution, and compound cross-engine requests; remove
  workflow-owned generation actions from the canonical action catalog.
- [ ] `P11.3` Define and validate the bounded router-worker result schema for
  workflow, action, clarification, and unsupported outcomes.
- [ ] `P11.4` Persist one normalized route and selected engine before thread
  launch, updating the active API projections directly.
- [ ] `P11.5` Dispatch every supported run only by explicit engine and remove
  all `kind`-based and inferred dispatch.
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
- Apply the architecture routing precedence: explicit outcomes and
  lifecycle-wide scope first; generation/refresh of workflow-owned deliverables
  second; target-specific CRUD, manual edits, attachments, pins, and single-test
  runs as actions; scope-wide declared execution as workflow.
- Do not use the artifact family alone as the discriminator. "Regenerate the
  APM" is workflow-owned, while "replace this APM paragraph" is a target-specific
  action. Bounded model use is permitted for an isolated registered operation,
  but the action catalog cannot generate or refresh a registered workflow
  outcome.
- Clarify or split a compound request that genuinely spans both engines into
  separately persisted queued runs. A scheduler never invokes the other
  scheduler to finish a compound command.
- Keep routing pure: it cannot execute actions, gather domain context, or
  mutate the workspace.
- Replace `initialize_known_workflow` and scattered local-resolution branches
  with the centralized router.
- Make `runner.start_command_run` persist the selected engine and normalized
  route before launching the thread.
- Simplify `_execute` to dispatch only by explicit engine and fail closed for
  unsupported or missing values.
- Preserve one live run per workspace, global concurrency, pending-command
  FIFO, retry parent links, and terminal-state guarantees.

**Routing matrix:**

| Request shape | Engine | Requested result |
|---|---|---|
| Prepare APM/RCM | Workflow | Relevant planning outcome with `reuse_existing` |
| Improve, generate again, regenerate, or refresh APM/RCM | Workflow | Relevant planning outcome with explicit `force` |
| Complete audit lifecycle | Workflow | `audit.verified` plus requested deliverables |
| Infer joins and analyze tables | Workflow | `analysis.executed` |
| Analyze selected documents | Workflow | `documents.analysis_generated` |
| Run declared RCM fieldwork | Workflow | `fieldwork.executed` |
| Attach, detach, rename, delete, manually edit, pin | Action | Registered operation DAG |
| Rerun one identified existing test | Action | Registered target-specific execution |
| Compound request requiring both engines | None | Clarify or split into separately persisted queued runs |
| Ambiguous target or scope | None | Clarification interaction |
| Unsupported request | None | Durable unsupported result |

**Exit gate:** A request is classified once, the engine is persisted, and no
scheduler contains a fallback call into the other scheduler.

### Phase 12: Retire V1

**Objective:** Migrate every live caller and delete the obsolete fixed-stage
runner without preserving its records or projections.

**Tasks:**

- [ ] `P12.1` Inventory all live v1 creation, API, UI, and chat callers, then
  route supported exploratory-analysis paths to the Phase 8 workflow with
  focused caller and projection tests.
- [ ] `P12.2` Fail closed or clarify unsupported v1-only requests, remove every
  v1 writer, update remaining active consumers, and delete `_Runner`, `STAGES`,
  validators, limits, and obsolete projections.
- [ ] `P12.3` Prove no live caller, engine value, import, API response, or UI path
  references v1 and that the phase gate passes.

**Work:**

- Inventory every caller of `start_run(..., kind="analysis")` and every live
  UI/API entry point that expects the fixed v1 pipeline.
- Route supported exploratory analysis requests to the new analysis workflow.
- Stop creating v1 records, update active projections, and delete the v1
  implementation directly. Do not move it into a compatibility package.
- Reject pre-cutover v1 records as unsupported if they are encountered.

**Exit gate:** No v1 runner, writer, reader, projection, engine value, or live
caller remains.

### Phase 13: Final Cleanup And Verification

**Objective:** Finish the clean-slate simplification after all replacement paths
are stable.

**Tasks:**

- [ ] `P13.1` Remove temporary delegation facades, aliases, empty modules,
  duplicate writers/readers, obsolete projections, and re-export shims; add
  fail-closed tests for records without a supported explicit engine.
- [ ] `P13.2` Add final static import-boundary and single-provider-path
  enforcement across runtime, schedulers, workers, executors, and domains.
- [ ] `P13.3` Update architecture, API, telemetry, developer, cutover, and
  handoff docs; run the full backend suite and frontend build.
- [ ] `P13.4` Prove the definition of done, mark the migration complete, and
  verify that no compatibility reader or package remains.

**Work:**

- Remove all temporary import aliases and delegation facades after their live
  callers move.
- Remove old run writers and readers together; retain no migration helper or
  historical projection.
- Delete empty modules and re-export shims.
- Document that cutover requires an empty `Workspaces/` root and that records
  without a supported explicit engine fail closed.
- Update `AGENTS.md`, architecture documentation, API schemas, debug telemetry
  documentation, and developer examples.
- Add static import-boundary enforcement to prevent scheduler/domain coupling
  from returning.

**Exit gate:** The package structure matches the target architecture closely,
with no duplicate scheduler, document-analysis, audit-lifecycle, context, or
provider-call implementation.

## 7. Authoritative Package Migration

This is the sole authoritative target package tree for both architecture
documents. Create packages incrementally; do not move every file before
extracting its responsibility. `agent-architecture.md` defines component
responsibilities and import boundaries but intentionally does not duplicate
this file list.

```text
backend/app/agent/
|- runtime/
|  |- run_runtime.py
|  |- workflow_runner.py
|  |- action_runner.py
|  |- unit_pipeline.py
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
`- runner.py                  - process launch and active-handle registry
```

If Phase 10 retains an intake or document-test protocol runner, place it under a
plainly named current package and include its explicit engine value in the target
schema. Do not create a `compat/` package. Avoid a file per individual capability
unless size or ownership independently warrants it.

## 8. Test Plan

### Unit Tests

- Capability registry validation and cycle reporting.
- Workflow closure, existence/structural readiness, `reuse_existing`/`force`
  generation modes, and semantic unit IDs.
- Context preset normalization, provider-free selector enforcement, local
  selector identity, and deterministic selection.
- Context privacy, limits, truncation, and manifest hashing.
- Exact proposal execution-identity validation without committed-artifact
  freshness assessment.
- Worker prompt and schema validation using fixed bundles.
- Executor CAS, reconciliation, auditor-edit preservation, and receipts.
- Action ledger DAG behavior without audit-specific rules.
- Routing classification and validation.
- Explicit-engine validation and fail-closed rejection of unsupported records.

### Integration Tests

- One vertical capability from context resolution through committed receipt.
- Full audit closure through its human disposition checkpoint.
- Partial audit outcomes with existing-artifact reuse, missing-prerequisite
  generation, explicit forced regeneration, and currency-not-assessed
  projection.
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
| After receipt | Mark succeeded if the committed postcondition still holds |
| Parent or target changed | Mark conflict; do not overwrite |
| Process crash with queued command | Recover current run, then preserve FIFO |

### API And Frontend Contract Tests

- Start, status, events, pause, resume, cancel, approval, interaction, retry,
  continue, action coverage, and SSE replay endpoints.
- Assistant-chat durable messages and linked run projections.
- Active activity labels, terminal states, and run cards use the target shape.
- Records without a supported explicit engine fail closed with a clear cutover
  error and are not inferred or converted.
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
- Selector IDs, definition hashes, and selection reasons.
- Provider profile, turns, estimated/actual tokens where available, latency,
  retries, and semaphore wait.
- Readiness transitions, generation mode, and the
  `currency_status="not_assessed"` projection for reused outcomes.
- Proposal reuse, reconciliation, conflict, and commit outcomes.

The debug console distinguishes absent current-run telemetry from zero values.
It does not render or synthesize pre-cutover history.

## 10. Risk Register

| Risk | Mitigation |
|---|---|
| Disposable workspaces are accidentally treated as supported input | Require an empty cutover root and fail closed for records without a supported explicit engine |
| Duplicate commits after crash | Persist proposals first; reconcile postconditions before retry; persist receipts |
| Auditor edits are overwritten | Parent hashes, CAS, domain merge rules, and explicit conflict states |
| Context refactor leaks table rows | Typed representations, deny-by-default policy, pre-worker validation, privacy tests |
| Prompt quality changes during extraction | Move prompts unchanged first; hash and tune only in later commits |
| Workflow runner remains domain-coupled through helper imports | Registry injection and automated import-boundary test |
| Action runner accidentally accepts broad audits | Routing validation and fail-closed action-runner guard |
| Workflow and action engines both claim an artifact request | Apply verb/scope precedence, remove workflow-owned generators from actions, and clarify or split cross-engine compounds |
| Semantic unit IDs change | Golden unit-ID tests and stable capability IDs during migration |
| Sidecars are reused after context or prompt changes | Include definition, context, selector, prompt, worker, schema, input, and source hashes |
| Users assume a reused artifact was checked for currency | Label reused outcomes `currency_status="not_assessed"` and document auditor-owned explicit regeneration |
| Automatic selectors make hidden provider calls | Prohibit `ModelGateway` and network access in selectors; model-based selection is a declared worker capability |
| Document analysis gets billed twice on resume | Per-chunk and reduction proposal sidecars with recovery tests |
| New analysis workflow invents unsafe joins | Local diagnostics, explicit ambiguity handling, bounded scope, no automatic destructive replacement |
| Temporary delegation becomes permanent duplicate logic | Delete each facade with the last live caller and prohibit a `compat/` package |
| File movement creates oversized reviews | Move one vertical slice at a time and delete its old handler in the same slice |

## 11. Review And Commit Strategy

The default Git commit unit is one checked task ID from the phase checklists.
A task commit includes its production changes, tests, and status edits. If a
session must end with a task incomplete, the handoff commit must leave the
application runnable, keep the task unchecked, identify itself as incomplete,
and record the exact remaining work under `Status notes and decisions`.

Pull requests may group consecutive task commits when they share one phase gate,
but they must remain reviewable commit by commit. Preserve task boundaries until
the migration is complete.

Prefer reviewable pull requests in this order:

1. Active-behavior characterization and same-schema recovery tests only.
2. Explicit engine field, direct action-runner rename, and v2 policy deletion.
3. Ledger audit-policy removal.
4. Runtime and model-gateway extraction with delegation.
5. Context contracts plus one resolver slice.
6. Worker/executor interfaces plus APM vertical slice.
7. Domain-neutral workflow scheduler and injected routing boundary.
8. One pull request per audit capability family or tightly related group.
9. Exploratory analysis workflow.
10. Document-analysis unification.
11. Intake and document-test decisions and migrations.
12. Routing/dispatch consolidation.
13. Direct v1 retirement.
14. Final cleanup and documentation.

Do not combine prompt tuning, workspace schema redesign, or frontend redesign
with these structural changes. Each pull request should state which old code is
now unreachable and delete it in the same task.

## 12. Phase Checklist

Every phase is complete only when all applicable items are true:

- [ ] Behavior was characterized before modification.
- [ ] The target persisted shape is internally consistent and same-schema
      recovery tests pass.
- [ ] No reader, converter, or projection for a superseded record shape was
      introduced.
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
- Every model-backed capability has a normalized, hash-identified `ContextSpec`
  and persists a `ContextManifest`.
- Workers receive only declared bundles and cannot mutate workspaces.
- Executors cannot call models and all commits are conflict-aware and
  receipted.
- Existing structurally usable outcomes are reused with currency explicitly not
  assessed; only auditor-requested `force` regenerates them in new workflows.
- Generic data analysis is a declared workflow ending in
  `analysis.executed`.
- Standalone and audit document analysis share one map/reduce implementation.
- Intake and document-test runner decisions are documented and use the shared
  runtime boundaries.
- No v1 implementation or compatibility package remains.
- Persisted run recovery, approvals, interactions, SSE replay, debug telemetry,
  privacy tests, the full backend suite, and the frontend build all pass.

## 14. Recommended Starting Point

Begin with Phase 0 and land it independently. The first implementation-code
change should be `P1.1`, which makes explicit engine identity mandatory for
target records. Then rename the action runner directly and remove its full-audit
policy.
Do not start by moving every file or rewriting `WorkflowRunner`; the active
behavior characterization and policy cleanup still establish clear scheduler
ownership even though historical persistence is deliberately unsupported.
