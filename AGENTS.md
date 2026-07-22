# AGENTS.md

## 1. System Overview

**Purpose:** Audit Workbench is a local-first audit analysis and execution app.
The unit of work is a workspace per engagement. Auditors import CSV/TSV/Excel
data, create derived joins, query and aggregate with Polars, run canned audit
analytics, stage mixed file-folder intake, manage planning artifacts, execute
data and document tests, promote observations into findings, and draft reports.
Workspace state and generated artifacts live under `Workspaces/<id>/`.

**Tech stack:** Python 3.12, FastAPI, Polars only for tabular compute, and
XlsxWriter/fastexcel for Excel I/O. The frontend is Vue 3 + TypeScript + Vite +
PrimeVue 4. There is no Tailwind, pandas, or DuckDB in the core path.

**Privacy boundary:** Computation runs on the user's machine. The read-only
assistant and the audit agent can call local tools, but row-level table data is
not sent to the model provider. The privacy choke points are the assistant
context builders and the bounded agent context bundles.

**Current product shape:** The original data-workbench surfaces still exist
(`Data`, `Query`, saved analyses, dashboard tiles), but the shipped product is
now centered on an RCM-driven audit workflow: planning context and APM, RCM
rows, planned tests, executable data/document tests, execution rollups,
working-paper generation, findings, and report drafting.

## 2. Architecture

```text
backend/app/
|- main.py                     - app factory; mounts all /api routers; serves
|                                frontend/dist with SPA history fallback; adds
|                                optimistic workspace revision middleware
|- workspaces.py               - Workspace model, persistence, revisioned save
|                                semantics, IDs, CRUD helpers, audit entities
|- workspace_transactions.py   - compare-and-swap mutation helper used by the
|                                workflow engine for conflict-aware commits
|- loader.py                   - typed CSV/TSV/Excel loading and frame cache
|- profiler.py                 - dataset and per-column profiling
|- explore.py                  - declarative query engine and frame payloads
|- analytics.py                - canned analytics registry and result metadata
|- validation.py               - durable table validation rules and run support
|- dashboard.py                - dashboard tiles and saved analysis payloads
|- assistant.py                - read-only NL assistant tool loop
|- assistant_chats.py          - durable workspace-scoped chats, artifacts,
|                                ask/act routing, chat-run projections
|- assistant_settings.py       - provider/model settings validation
|- llm.py                      - OpenAI-compatible stdlib transport for both
|                                assistant and agent profiles
|- sandbox.py                  - guarded local Python/Polars execution
|- intake.py                   - staged mixed file/document intake
|- documents.py                - document inventory, versions, extraction,
|                                metadata, and activity logs
|- document_context.py         - bounded document context packaging
|- document_search.py          - retrieval helpers over extracted docs
|- document_analysis.py        - document-analysis jobs and conflict model
|- doc_tests.py                - durable document test definitions and runs
|- data_tests.py               - durable exploratory or RCM-linked data tests
|- methodology.py              - methodology pack storage and retrieval
|- evidence.py                 - typed evidence anchors and provenance helpers
|- findings.py                 - evidence-linked findings CRUD and promotion
|- report.py                   - draft report generation, reconciliation, QA
|- rcm_execution.py            - coverage, rollups, observations, completion,
|                                and RCM working-paper assembly
|- working_papers.py           - legacy working-paper compatibility layer
|- templates_store.py          - editable markdown template persistence
|- debug_store.py              - local telemetry storage for calls/events/state
|- debug_service.py            - debug read models, timing, causal analysis
|- model_context.py            - shared model-context helpers
|- embedding.py                - local embedding utilities
|- field_names.py              - shared field name normalization helpers
|- routes/
|  |- workspace_routes.py      - workspace/table/join CRUD
|  |- analysis_routes.py       - table preview/profile/query/analytics/export
|  |- analyses_routes.py       - saved analyses CRUD
|  |- dashboard_routes.py      - dashboard tiles and engagement status
|  |- validation_routes.py     - validation rules and runs
|  |- assistant_routes.py      - legacy ask/run-python assistant endpoints
|  |- assistant_chat_routes.py - durable unified assistant chat API
|  |- agent_routes.py          - durable run CRUD, SSE, approvals,
|  |                             interactions, continue/retry, action coverage
|  |- intake_routes.py         - folder intake manifests and apply flow
|  |- planning_routes.py       - planning, templates, RCM, planned tests,
|  |                             observations, and working papers
|  |- document_routes.py       - document CRUD, extraction, QA, and activity
|  |- doc_test_routes.py       - document test CRUD and execution
|  |- report_routes.py         - findings/report endpoints and reconciliation
|  `- debug_routes.py          - workspace debug console APIs and live stream
`- agent/
|- runtime/                 - RunRuntime contract and active durable-run
|                             implementation, restart-safe auditor response,
|                             shared ModelGateway, unit pipeline, and the
|                             domain-neutral workflow scheduler
   |- context/                 - normalized context declarations, content-free
   |                             manifest identity/persistence, local bundle
   |                             models, and validated preset/selector registries
   |- workers/                 - immutable model-worker contracts, registry,
   |                             bounded validation/repair, and planning worker
   |- executors/               - deterministic mutation/reconciliation
   |                             contracts, registry, receipts, and APM executor
   |- workflows/               - authoritative workflow definitions; audit.py
   |                             owns the audit dependency graph, workflow id,
   |                             and hash-identified definition metadata
   |- capabilities/            - grouped audit capability composition
   |                             (planning/fieldwork/reporting) with startup
   |                             validation against the authoritative graph
   |- store.py                 - durable run storage in AgentRuns/
   |- base.py                  - temporary BaseRunner delegation facade and
   |                             task/artifact hooks for current runners
   |- runner.py                - thread orchestration, active-handle registry,
   |                             recovery, pause/resume/cancel/retry/continue
   |- action_runner.py         - action-graph runner for isolated mutations
   |- workflow.py              - generic capability graph primitives
   |- audit_execution.py       - temporary audit capability execution adapters
   |- audit_capabilities.py    - audit capability registry and readiness rules
   |- audit_workers.py         - single-turn model workers with validators
   |- context_bundles.py       - hard-budgeted agent prompt context builders
   |- actions.py               - registered action catalog and executors
   |- ledger.py                - domain-neutral action graph validation
   |- artifact_index.py        - artifact selectors and canonical resolution
   |- joins.py                 - deterministic join inference/diagnostics
   |- suggest.py               - deterministic validation rule suggestions
   |- prompts.py               - bounded prompts keyed by [agent:<stage>] tags
   |- summary.py               - finding validation and fallback markdown
   |- intake_runner.py         - intake batch runner
   |- doc_test_runner.py       - resumable document-test execution
   |- document_analysis_runner.py - map/reduce document-analysis execution

frontend/src/
|- api.ts                      - fetch wrapper, uploads, downloads, error model
|- types.ts                    - frontend mirror of backend payloads
|- views/
|  |- HomeView.vue             - workspace list/create/delete
|  |- WorkspaceView.vue        - main engagement shell and tab navigation
|  `- DebugView.vue            - local telemetry console
|- composables/
|  |- useAgentRun.ts           - shared run store, SSE connection, live refresh
|  |- useAssistantChat.ts      - durable chat state and send/retry/artifacts
|  |- useWorkspaceNavigation.ts - workspace query-string tab state
|  |- useFileDrop.ts           - global drag/drop import helpers
|  `- documentStatus.ts        - document/test status helpers
`- components/
   |- DashboardTab.vue         - dashboard and engagement home
   |- DataTab.vue              - data upload/list/remove
   |- QueryTab.vue             - interactive query builder and pin flow
   |- AnalysisTab.vue          - saved analyses rail and editors
   |- PlanningTab.vue          - planning context, APM, RCM, planned tests
   |- DocumentsTab.vue         - document inventory and review
   |- DocTestsTab.vue          - document test worklists and execution
   |- DataTestsTab.vue         - data test authoring/history/results
   |- FindingsTab.vue          - finding editing and support checks
   |- ReportTab.vue            - report draft, preview, sources, quality
   |- ImportDialog.vue         - staged folder/file import review
   |- PostImportPlanningOffer.vue - shortcut from intake into planning
   |- MarkdownEditor.vue / MarkdownView.vue - safe markdown editing/rendering
   |- CodeEditor.vue           - local code editing surface
   |- EvidenceAnchorDialog.vue - provenance source picker
   |- ReportReconcileDialog.vue - prevents silent report overwrites
   |- ChartView.vue / FrameTable.vue / PinDialog.vue / JoinDialog.vue
   |- planning/RcmGrid.vue     - RCM grid
   |- validation/*             - validation authoring and run results
   |- analysis/*               - saved analysis editors
   |- agent/*                  - persistent right-side assistant drawer:
   |                             chats, transcript, composer, approvals,
   |                             interactions, artifacts, run cards/history
   `- debug/JsonTree.vue       - debug JSON inspector
```

## 3. Runtime Model

### Workspace and persistence

- Every workspace is persisted on disk under `Workspaces/<id>/`.
- Workspace writes are revisioned. `main.py` exposes `ETag` /
  `X-Workspace-Revision`, and mutating requests may supply `If-Match` to get
  strict optimistic concurrency.
- The workflow engine uses `workspace_transactions.mutate(...)` plus parent hash
  checks to detect conflicts when committing generated artifacts.

### Assistant surfaces

- There are two assistant paths:
  - `assistant.py`: read-only Q&A with local tools and editable Python results.
  - `assistant_chats.py`: durable chat UX that routes each message as `ask`,
    `act`, or clarification, stores local artifacts, and projects linked runs
    back into the transcript.
- Chat messages can start new runs or queue commands onto the active run.
- Assistant artifacts are revisioned and rerunnable; editable Python artifacts
  are re-executed locally through the sandbox.

### Agent generations

One durable run store supports multiple runner styles:

```text
BaseRunner
|- _Runner                  legacy v1 fixed analysis pipeline
|- IntakeRunner             one import batch
|- DocTestRunner            one document test
|- DocumentAnalysisRunner   document analysis map/reduce
|- ActionRunner             action graph
`- AuditWorkflowExecution  temporary audit execution adapter

WorkflowRunner             domain-neutral capability graph scheduler
```

- `ActionRunner` is still used for isolated mutations and repairable action
  graphs.
- `WorkflowRunner` is the main audit scheduler. It receives a materialized
  route and registered capability executions, fans outcomes into units, and
  records `next_outcomes` for `Continue audit`. Temporary audit handlers are
  composed from `audit_execution.py` pending their Phase 7 registry moves.
- Local routing in `routing.local_resolution(...)` is important. It
  catches common phrases and goal templates before any model call. If routing
  misses, a bounded router worker may still resolve the command.

### From command to execution

- `assistant_chats._process_message` resolves intent. An `act` intent calls
  `runner.start_command_run`, which enforces one live run per workspace
  (`AgentBusyError`; `AGENT_MAX_CONCURRENT` caps the process), creates the run
  document, tries `initialize_known_workflow`, then launches a daemon thread.
- `runner._execute` dispatches on the explicit `run["engine"]`, guarantees the run reaches a
  terminal status even on a crash, and in its `finally` starts the next
  `pending_commands` entry. Control flows through a `RunHandle` (cancel, pause,
  resume, inbox, interaction responses) held in the `_HANDLES` registry.
- v3 derives its plan from the registry, not the model.
  `workflow.materialize(...)` takes the transitive `depends_on` closure over
  `audit_capabilities.REGISTRY`, skips any capability whose deterministic
  `readiness()` is already satisfied (content hashes detect staleness, not just
  absence), and calls `expand_units()` to fan the rest into units. Unit IDs are
  semantic, so re-expanding after a resume yields the same work.
- v2 derives its plan from one `[agent:command_interpreter]` turn that returns a
  DAG of registered action types. `ledger.append_actions(...)` normalizes
  created-target references and validates the graph; a rejected batch is fed
  back once with the specific error.
  `_drive_graph` then runs a single-threaded priority loop: pending interaction,
  then proposed action to gate, then ready action to execute.

### Scheduling, concurrency, and budgets

- Stages run strictly in dependency order. Parallelism lives inside a stage:
  `workflow.stable_all_settled(...)` fans model work out under
  `limits.max_llm_concurrency`, all-settled, and returns results in unit-ID
  order so commits stay deterministic.
- Commits are serialized and conflict-aware. Model proposals are written to
  sidecars before commit, so a crash between generation and commit resumes from
  the sidecar instead of re-billing the provider.
- Backpressure is budgetary, not queue-based: per-workspace serialization,
  `pending_commands` FIFO between runs, `max_model_turns` /
  `max_estimated_prompt_tokens` / `max_completion_tokens` (sized in
  `_install_resolution` from actual RCM, planned-test, and Q&A counts, then
  refreshed before every stage), `max_actions`, `max_waves`,
  `max_units_per_stage`, and a runtime deadline extended by time spent blocked
  on the auditor. `DefaultRunRuntime` owns the durable model-budget ledger,
  dynamic limit updates, deadline, checkpoint controls, live inbox draining,
  approval batches, and structured-interaction waits. Offline auditor
  responses are persisted before wakeup and consumed on same-schema restart.

### Model call discipline

- `agent.runtime.DefaultModelGateway.complete` is the only runner path to the
  provider. It reserves turn and token budgets through `RunRuntime` before
  calling, derives the `[agent:<stage>]` tag that drives UI labels and
  per-worker accounting, holds a process-wide semaphore keyed on
  `provider:model`, records debug telemetry, and appends hash-only provenance.
  `BaseRunner._llm_content` is a temporary delegation facade for existing
  callers.
- `agent.runtime` defines the target `RunRuntime` and `ModelGateway` structural
  contracts. `DefaultRunRuntime` owns synchronized run saves, event emission,
  durable run timing, status and warning transitions, activity/model-wait
  projections, budgets, dynamic limits, deadlines, checkpoints, controls,
  inbox draining, approvals, and auditor interactions. `BaseRunner` delegates
  that surface while retaining temporary task and artifact-activity hooks.
  `ActionRunner` accepts an optional injected `RunRuntime` while preserving its
  existing three-argument default construction. The domain-neutral
  `WorkflowRunner` receives `RunRuntime` and all scheduler dependencies by
  composition; the temporary audit execution adapter still uses `BaseRunner`
  hooks until its Phase 7 capability-family migrations.
- Services outside `agent/` (`documents.document_chat`, `doc_tests.run_item`)
  accept an injected `model_adapter` so their calls are charged to the same
  budget and provenance ledger.
- The Phase 3 gate exercises runtime budgets and controls across both graph
  runners and every active leaf runner, preserves queued follow-ups after a
  terminal crash, and statically confines direct agent provider calls to
  `runtime/model_gateway.py`.
- Phase 4 completed with normalized context declarations and serialization contracts
  under `agent/context/`, plus hash-identified preset and selector registries
  that reject duplicate or unknown keys, unhashable definitions, unsupported
  source types, and invalid privacy combinations. Deterministic source and
  manifest hashes, supplied-size metrics, omission/truncation record helpers,
  and atomic per-unit manifest sidecars now keep durable context records
  content-free while bundles remain local. The resolver enforces declaration
  order, privacy, hard budgets, stable tie-breaking, and a closed provider-free
  selector set: deterministic metadata, local lexical scoring, or local
  embeddings bound to model/index hashes. The APM adapter reuses the existing
  bounded document context, methodology section index, table schema builder,
  and statistical profiler. Table candidates omit category literals, expose no
  row preview, and the context models reject `table_rows` before a bundle can
  reach a worker. Context policy is declaration-only: registered application
  capability and preset definitions are authoritative, while auditor source
  curation and explicit regeneration operate within those declarations. The
  live `planning.apm_ready` capability now declares `planning.apm`, persists
  its content-free manifest before the provider call, and supplies its pure APM
  worker only the resolver bundle. APM proposal recovery rejects sidecars when
  the capability, unit, manifest, spec, resolver, selector, prompt, worker, or
  schema identity changes. Existing usable artifacts still bypass resolution;
  explicit force resolves current candidates, and no automatic freshness
  monitoring was added.
- Phase 5 completed with immutable, hash-identified worker and executor
  contracts plus a runner-independent unit pipeline. The pipeline persists the
  context manifest before a model call, persists an exact-identity proposal
  before approval or mutation, reconciles interrupted commits, persists a
  postcondition receipt, and reevaluates readiness last. The live
  `planning.apm_ready` capability is the first vertical slice: its registered
  worker receives only the resolved local bundle, and its registered executor
  owns parent-guarded workspace mutation and auditor-edit preservation. Resume
  after proposal persistence does not rebill; incompatible context, selector,
  prompt, worker, schema, capability, or unit identities regenerate with stable
  rejection reasons. Workflow units project content-free context, proposal,
  and receipt references; the former APM caller and writer paths were removed.
- Phase 6 completed with `runtime.WorkflowRunner` as the only active capability
  scheduler. It is domain-neutral, composed with `RunRuntime` and validated
  capability executions, and has no `ActionRunner` inheritance or audit-stage
  methods. Routing lives in `agent/routing.py`; active audit dispatch composes
  temporary handlers from `audit_execution.py` until their Phase 7 registry
  migrations. The former `agent/workflow_runner.py`, legacy adoption, and
  run-shape translation were deleted. Runtime import-boundary tests reject
  planning, RCM, document, finding, report, and audit-domain imports.

### Known duplication

- The obsolete `ActionRunner` full-audit path and its prompt policy were
  removed in Phase 1. Broad-audit and planning requests fail closed if they
  bypass workflow routing; isolated action DAGs remain supported.
- The action ledger is domain-neutral: its audit-lifecycle switch, constants,
  enforcement helper, and catalog-specific artifact mappings have been removed.
  Action-specific reference rules live in the action catalog, while
  `audit_capabilities.build_registry` is the active authoritative audit graph
  until its Phase 7 move.

### Events and live UI

- Runs persist `run.json`, `events.jsonl`, and sidecars under
  `Workspaces/<id>/AgentRuns/<run_id>/`.
- The UI consumes SSE from `agent_routes.py`; events are replayable by cursor or
  `Last-Event-ID`.
- `useAgentRun.ts` is the shared client-side run bus for tabs and the assistant
  drawer.

### Debug telemetry

- Debug tracing is local only and lives under the workspace debug store.
- The debug console exposes:
  - LLM call records and retries
  - state transitions and snapshots
  - run timing metrics
  - causal-analysis read models
  - a replayable live event stream
- Historical runs may predate full telemetry. The debug UI explicitly shows
  gaps instead of synthesizing missing history.

## 4. Key Conventions

- Raise `WorkspaceError`, `QueryError`, `SandboxError`, `SettingsError`, or
  `AnalysisConflict` from core modules. `main.py` maps these into stable HTTP
  responses.
- Table payloads are always `{columns, dtypes, rows}` with JSON-safe values.
- Exports re-run the computation; responses stay stateless per request.
- Analytics are registry-driven. Add a backend function plus param metadata;
  the SPA form renders from metadata.
- Saved analyses and dashboard tiles are spec-not-data. They recompute against
  current workspace frames.
- The assistant and agent do not use arbitrary tool loops inside `agent/`.
  Worker calls are single-turn, bounded, and budgeted through `BaseRunner`.
- Existing uncommitted workspace or code changes may be user-owned. Do not
  revert them unless explicitly asked.

## 5. Commands

```bash
# Backend deps (Python 3.12)
uv venv .venv
uv pip install -r backend/requirements-dev.txt

# Backend tests
cd backend
uv run --no-project pytest

# API dev server
uv run --no-project uvicorn app.main:app --app-dir backend --reload

# Frontend dev server
cd frontend
npm run dev

# Frontend type/build gate
cd frontend
npm run build
```

## 6. Current Notes

- The product has moved past the older "Ask AI tab" model. The active UX is the
  persistent right-side assistant drawer backed by durable chats.
- `ProfileTab.vue` is not part of the current shipped workspace navigation even
  though profiling code still exists on the backend.
- Legacy `work_program` and legacy working-paper behavior remain only for
  compatibility and rollback paths. The active audit model is RCM-first.
- The debug console is a first-class local diagnostics surface, not just test
  scaffolding.
- PrimeVue `Select` still needs the full pointer event sequence in UI-driving
  tests; bare synthetic `.click()` remains unreliable on this machine.
