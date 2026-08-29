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
assistant and the audit agent can call local tools, and row-level table data is
withheld from the model provider with exactly two declared exceptions, each
carrying its own permission and its own cap:

1. The rows a saved analysis *flagged as exceptions* reach the EDA-summary
   capability, capped per procedure, under `allow_analysis_exception_rows`.
2. The rows a durable Data Test *flagged as exceptions* reach the finding-draft
   capability, capped by row count and serialized size in the adapter
   (`FINDING_EXCEPTION_ROW_LIMIT`), under `allow_datatest_exception_rows`. This
   is what lets a finding name the invoice that failed instead of only counting
   it; the projection always reports how many rows were withheld so a truncated
   table cannot be drafted as a complete population.

The two are deliberately separate permissions so revoking one never silently
revokes the other. Nothing else may request rows — `allow_table_rows` is denied
everywhere and the resolver rejects the `table_rows` representation
structurally. The privacy choke points
are the assistant context builders and the bounded agent context bundles.

**Current product shape:** The original data-workbench surfaces still exist
(`Data`, `Query`, saved analyses, dashboard tiles), but the shipped product is
now centered on an RCM-driven audit workflow: planning context and APM, RCM
rows, the Document and Data Tests that cover them, execution rollups,
working-paper generation, findings, and report drafting. A test is one durable
record with one source: an RCM row links to its tests through ``test_refs`` and
holds no test description of its own. A finding is a typed spine — severity,
provenance, and its RCM/test/execution/evidence references — carrying one
free-Markdown ``narrative`` whose sections are the ``##`` headings of the
workspace's ``finding`` template. Completeness is checked against those
headings, so a firm changes what a finding must say by editing the template, not
the code, and the narrative is written as final report prose and copied into the
report unchanged.

## 2. Architecture

```text
backend/app/
|- main.py                     - app factory; mounts all /api routers; serves
|                                frontend/dist with SPA history fallback; adds
|                                the principal scope and optimistic workspace
|                                revision middleware
|- db.py                       - SQLite control plane: connections, pragmas,
|                                user_version migrations. Identity and indexes
|                                only; audit content stays on the filesystem
|- telemetry_db.py             - per-workspace SQLite for telemetry and event
|                                logs (debug calls/events, state, AI activity,
|                                run events) plus the one-time legacy import
|- accounts.py                 - admin-provisioned user records, scrypt
|                                passwords, the local single-user account
|- auth.py                     - Principal, AUTH_MODE, request-scoped principal
|- registry.py                 - the Users/<owner>/Workspaces/<dir> layout and
|                                the uid -> location registry; the only module
|                                that joins a path under the data root
|- workspaces.py               - Workspace model, persistence, revisioned save
|                                semantics, the open_workspace resolver, CRUD
|                                helpers, audit entities
|- workspace_transactions.py   - compare-and-swap mutation helper used by the
|                                workflow engine for conflict-aware commits
|- loader.py                   - typed CSV/TSV/Excel loading and frame cache
|- profiler.py                 - dataset and per-column profiling
|- explore.py                  - declarative query engine and frame payloads
|- analytics.py                - canned analytics registry and result metadata
|- validation.py               - durable table validation rules and run support
|- dashboard.py                - dashboard tiles and saved analysis payloads
|- engagement_progress.py      - the index card's phase states: a screen over
|                                stored fields settles amber, anything it
|                                cannot settle escalates to `dashboard`, and
|                                the answer is memoized per workspace revision
|- analysis_results.py         - the saved-analysis execution contract: run it,
|                                bound it, record it, classify it
|- analysis_memo.py            - the EDA memo's embed grammar: parsing and
|                                flattening, shared by the worker that writes
|                                the memo and the context adapter that hands it
|                                to planning (neither may import the other)
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
|- cycle_vouching.py           - domain-neutral cycle-test contracts,
|                                registry-reference validation, and shared
|                                execution/disposition state accessors
|- cycle_registry/             - immutable registry core, common evidence
|  |- models.py / registry.py    definitions, expanded pack hashes, and
|  `- packs/                     namespaced business-cycle packs
|- data_tests.py               - durable exploratory or RCM-linked data tests
|- methodology.py              - methodology pack storage and retrieval
|- evidence.py                 - typed evidence anchors and provenance helpers
|- findings.py                 - evidence-linked findings CRUD, promotion, and
|                                template-derived narrative completeness
|- report.py                   - draft report generation, reconciliation, QA
|- rcm_execution.py            - coverage, rollups, observations, completion,
|                                and RCM working-paper assembly
|- working_papers.py           - legacy working-paper compatibility layer
|- templates_store.py          - editable markdown template persistence and the
|                                shared `##`-section parsing those templates
|                                define (headings, bodies, scaffold)
|- debug_store.py              - telemetry writes for calls/events/state, into
|                                the workspace telemetry database
|- debug_service.py            - debug read models, timing, causal analysis
|- model_context.py            - shared model-context helpers
|- embedding.py                - local embedding utilities
|- field_names.py              - shared field name normalization helpers
|- routes/
|  |- workspace_routes.py      - workspace/table/join CRUD
|  |- analysis_routes.py       - table preview/profile/query/analytics/export
|  |- analyses_routes.py       - saved analyses CRUD, execution, and export
|  |- dashboard_routes.py      - dashboard tiles and engagement status
|  |- validation_routes.py     - validation rules and runs
|  |- assistant_routes.py      - legacy ask/run-python assistant endpoints
|  |- assistant_chat_routes.py - durable unified assistant chat API
|  |- agent_routes.py          - durable run CRUD, SSE, approvals,
|  |                             interactions, continue/retry, action coverage
|  |- intake_routes.py         - folder intake manifests and apply flow
|  |- planning_routes.py       - planning, templates, RCM, observations,
|  |                             and working papers
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
   |                             bounded validation/repair, and the registered
   |                             planning/fieldwork/reporting/analysis/document/
   |                             intake workers (analysis owns both the
   |                             definition worker and the EDA summary worker)
   |- executors/               - deterministic mutation/reconciliation
   |                             contracts, registry, receipts, and the
   |                             registered domain executors
   |- workflows/               - authoritative workflow definitions; audit.py
   |                             owns the audit dependency graph, analysis.py
   |                             the exploratory data-analysis graph,
   |                             documents.py the document-analysis graph, and
   |                             doc_tests.py the standalone document-test graph,
   |                             each with its workflow id and hash-identified
   |                             metadata
   |- capabilities/            - grouped capability composition
   |                             (documents/planning/fieldwork/reporting for
   |                             audit, analysis for data analysis, documents for
   |                             document analysis, doc_tests for document tests)
   |                             with startup validation against the
   |                             authoritative graphs
   |- routing.py               - the only classifier: pure deterministic
   |                             precedence, the bounded router worker, and
   |                             route/engine persistence
   |- store.py                 - durable run records in AgentRuns/; the run's
   |                             event stream lives in telemetry_db
   |- base.py                  - the shared run-projection base: plan/tasks,
   |                             artifacts, approval batches, proposal items,
   |                             and model-call provenance, over an injected
   |                             RunRuntime and ModelGateway
   |- runner.py                - process layer only: record creation, the
   |                             one-live-run rule, thread launch, the
   |                             active-handle registry, recovery, and
   |                             pause/resume/cancel/retry/continue
   |- action_runner.py         - action-graph runner for isolated mutations
   |- workflow.py              - generic capability graph primitives
   |- workflow_dispatch.py     - selects a workflow composition from the run's
   |                             persisted workflow definition id
   |- audit_execution.py       - audit-side composition: one execution binding
   |                             per capability plus audit projections
   |- analysis_execution.py    - analysis-side composition: relationship,
   |                             join, definition, and execution bindings
   |- documents_execution.py   - document-side composition: extraction, chunk,
   |                             reduction, and review bindings, shared with the
   |                             audit composition
   |- doc_tests_execution.py   - the one Document Test unit binder plus the
   |                             standalone document-test composition, both
   |                             shared with the audit composition
   |- context_bundles.py       - bounded command-router context bundle
   |- actions.py               - registered action catalog and executors
   |- ledger.py                - domain-neutral action graph validation
   |- artifact_index.py        - artifact selectors and canonical resolution
   |- joins.py                 - deterministic join inference/diagnostics
   |- suggest.py               - deterministic validation rule suggestions
   |- prompts.py               - the action engine's own bounded prompts, keyed
   |                             by [agent:<stage>] tags; every capability
   |                             prompt lives with its registered worker
   `- intake_runner.py         - retained single-unit folder-intake protocol
                                 runner (RunRuntime, registered worker, declared
                                 context, proposal-only pipeline unit)

frontend/src/
|- api.ts                      - fetch wrapper, uploads, downloads, error model
|- types.ts                    - frontend mirror of backend payloads
|- views/
|  |- HomeView.vue             - workspace list/create/delete, each card
|  |                             carrying `WorkspaceProgress.vue`: four
|  |                             states (data, planning, fieldwork,
|  |                             report) in the console rail's colours,
|  |                             taken from the backend, never recomputed
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
   |- AnalysisTab.vue          - analysis triage rail, filters, and editors
   |- PlanningTab.vue          - planning context, APM, RCM, linked tests
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
   |- ui/UiStatusLanes.vue     - the status bar every fieldwork surface sits
   |                             under: three lanes saying what has run, what
   |                             it concludes, and what is written up, each
   |                             carrying the action it still owes. Purely
   |                             presentational — one `*Status.ts` module per
   |                             page derives the lanes and owns the filter
   |                             predicates its counts stand for
   |                             (planning/rcmStatus, findings/findingsStatus,
   |                             data-tests/dataTestStatus,
   |                             doc-tests/docTestStatus)
   |- validation/*             - validation authoring and run results
   |- analysis/*               - analysis list, recorded-outcome banner,
   |                             classification vocabulary, and the three
   |                             editors (library, saved python, new python)
   |- agent/*                  - persistent right-side assistant drawer:
   |                             chats, transcript, composer, approvals,
   |                             interactions, artifacts, run cards/history
   |- agent/EngagementState.vue - the console rail's Progress column: the same
   |                             vocabulary as the lanes above, one 17rem
   |                             column instead of a grid. Finished phases
   |                             collapse to their totals and reopen on click;
   |                             the phase in flight leads with the RCM bar's
   |                             fraction and the action scoped to its gap.
   |                             `agent/engagementStatus.ts` derives it, and
   |                             its disclosures qualify the backend's phase
   |                             ticks rather than moving them
   `- debug/JsonTree.vue       - debug JSON inspector
```

## 3. Runtime Model

### Tenancy

- `AUTH_MODE` is `single_user` (default) or `multi_user`. Single-user resolves
  every request to one local account and shows no login.
- `workspaces.open_workspace(actor, ref)` is the **only** path from a workspace
  reference to a filesystem root. It validates the reference, resolves it
  through the registry, checks access, and asserts path containment. The
  architecture tests in `test_tenancy.py` fail the build if another module
  joins a path under the data root.
- Identity is separate from location: `uid` is globally unique and appears in
  URLs, `dir_name` is the per-owner slug that names the directory, and `name`
  is display text. `legacy_slug` keeps pre-migration links resolving.
- Background work never needs a principal. Agent runs execute on daemon threads
  that no request context reaches, so internal reloads use `Workspace.reload()`
  — holding a `Workspace` is itself proof the resolver authorized that root.
- `registry.reconcile()` rebuilds the registry from disk and stamps identity
  into any manifest missing it. It is both disaster recovery and the migration
  step for workspaces moved into a home by hand.

### Authentication

- Sessions are rows in the control plane, not signed tokens, so sign-out and
  account suspension genuinely revoke rather than advise. Only the token hash
  is stored.
- The cookie is `HttpOnly`, `SameSite=Lax`, and `Secure` when the request
  arrived over HTTPS. Cookies rather than a bearer header is forced: the
  frontend streams agent and debug events over `EventSource`, which cannot set
  request headers.
- CSRF has two locks: `SameSite=Lax` withholds the cookie from cross-site
  POSTs, and `main.py` refuses any state-changing request whose `Origin` names
  a different site.
- In `multi_user` the API is closed by default — the middleware refuses every
  `/api/**` request without a principal, except `/api/auth/*`. A route cannot
  accidentally be public by forgetting a check.
- Accounts are admin-provisioned. There is no self-service registration route;
  `backend/manage.py` creates the first administrator, and `/api/admin/*`
  creates the rest or issues invitations.

### Sandboxed execution

- `sandbox.run` is the single choke point for auditor- and model-authored
  Python: data tests, dashboard tiles, agent analyses, and `/run-python` all
  reach the interpreter through it.
- Single-user executes in-process, which is the original guard-rail and the
  original threat model — the snippet runs under the account that owns the data.
- Multi-user executes in a short-lived bubblewrap child: `--unshare-all` (no
  network), `--clearenv`, and a mount namespace containing only the interpreter,
  Polars, the `app` package under a synthetic root, and one exchange directory.
  The dotenv file, the `Workspaces` tree, and `workbench.db` are not mounted.
- Frames cross as Arrow files in the exchange directory, narrowed by
  `referenced_frames` to what the snippet actually names. Dynamic `tables`
  access forfeits the narrowing rather than risking a `KeyError`.
- `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_FSIZE` are applied inside the child, not
  through `preexec_fn`, which is unsafe in a process running agent threads.
- Without bubblewrap a shared server refuses to run Python. A bare subprocess is
  not a boundary: it shares the server's UID and can read the dotenv the parent's
  environment was scrubbed of.

### Workspace and persistence

- Every workspace is persisted on disk under
  `<data root>/Users/<owner_id>/Workspaces/<dir_name>/`.
- Audit artifacts are files; telemetry and event logs are rows. Findings, RCM
  rows, tests, and the rest stay independently replaceable JSON sidecars, which
  is what keeps them atomic, revisioned, and readable without the app. The debug
  console's traces, the AI activity feed, and run event streams live in that
  folder's `telemetry.db`, because they are written once and then read filtered,
  paged, and pruned. Losing that database costs history, not evidence.
- `Workspace` hydrates artifacts lazily: naming `ws.findings` reads the Findings
  sidecars, and an artifact never named is never parsed. The parsed form is
  cached against the manifest revision, so repeated loads at the same revision
  reuse it. A save diffs only what was hydrated.
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

### Engines

One durable run store, two scheduling engines, and one retained protocol
runner. `store.RUN_ENGINES` is final at `{workflow, action, intake}`; a record
whose engine is absent or outside that set fails closed.

```text
BaseRunner                  shared run projections (plan/tasks, artifacts,
|                           approvals, proposal items, model provenance)
|- IntakeRunner             one staged import batch (retained protocol runner)
|- ActionRunner             action graph
|- AuditWorkflowExecution   audit execution bindings and projections
|- AnalysisWorkflowExecution analysis execution bindings and projections
|- DocumentWorkflowExecution document execution bindings and projections
`- DocTestWorkflowExecution document-test execution bindings and projections

WorkflowRunner             domain-neutral capability graph scheduler; composed
                           with RunRuntime and validated executions, inherits
                           nothing
```

- `ActionRunner` is still used for isolated mutations and repairable action
  graphs. `IntakeRunner` is the one retained protocol runner: folder intake is a
  single-unit protocol over a staged batch whose authoritative state lives under
  `Imports/`, not a capability graph over the workspace. The decision, and the
  document-test migration that deleted `DocTestRunner`, are recorded in
  `docs/agent-protocol-runner-decisions.md`.
- `WorkflowRunner` is the main capability scheduler and now serves four declared
  workflows: the RCM audit lifecycle (`audit_workflow_v2`), the exploratory
  data-analysis workflow (`analysis_workflow_v1`, which infers table
  relationships, materializes only evidence-supported joins, proposes rerunnable
  analysis definitions, and executes them locally), the document-analysis
  workflow (`documents_workflow_v1`: extract, map each bounded source chunk,
  reduce, and separately await auditor review), and the standalone document-test
  workflow (`doc_tests_workflow_v2`: definition readiness, item execution, and
  separately the auditor's own disposition). `workflow_dispatch.py`
  selects the composition from the definition id the run persists. It receives a
  materialized route and registered capability executions, fans outcomes into units, and
  records `next_outcomes` for `Continue audit`. Every audit capability carries
  exactly one binding: a per-unit pipeline binding (registered worker plus
  registered executor) or a per-unit deterministic computation. A capability
  whose units are of mixed kinds — `fieldwork.executed` — binds each unit at its
  own boundary through the same binder. `audit_execution.py` owns only the
  audit-shaped glue: which worker/executor and declared context a unit uses,
  approval items, post-commit bookkeeping, checkpoint handlers, and the audit
  completion projection.
- `routing.classify_command(...)` is the deterministic pass and it is pure: it
  reads the command dict only, and never loads a workspace, executes an action,
  or mutates state. It applies one precedence order — explicit outcomes, a
  registered goal template, a lifecycle-wide phrase, workflow-owned
  generation/refresh, a target-specific operation, scope-wide execution, then a
  weak isolated-operation marker. If it matches nothing, a bounded router worker
  resolves the command on the worker thread.

### From command to execution

- `assistant_chats._process_message` resolves intent. An `act` intent calls
  `runner.start_command_run`, which enforces one live run per workspace
  (`AgentBusyError`; `AGENT_MAX_CONCURRENT` caps the process), creates the run
  document, calls `routing.resolve_route(...)`, then launches a daemon thread.
- A request is classified exactly once. `resolve_route` persists the normalized
  `run["route"]` and the selected `run["engine"]` before the thread starts, and
  installs the materialized graph for a workflow route. A `clarification` or
  `unsupported` route selects no engine and finishes the run without mutation. A
  command the deterministic pass cannot classify launches with
  `route.status == "pending"`; `routing.resolve_pending_route(...)` then spends
  one bounded router turn — the only routing path that calls the provider — and
  it never repeats the deterministic pass.
- `runner._execute` calls `routing.dispatch_engine(...)` and then dispatches on
  the explicit `run["engine"]`. Nothing infers an engine from `kind`,
  `schema_version`, or record contents; a missing or unsupported engine fails
  closed. `_execute` guarantees the run reaches a terminal status even on a
  crash, and in its `finally` starts the next `pending_commands` entry. Control
  flows through a `RunHandle` (cancel, pause, resume, inbox, interaction
  responses) held in the `_HANDLES` registry.
- Because a pending-route command run has no engine yet, "is this a command
  run?" is `store.is_command_run(run)` — a record-shape test — in `steer`,
  `retry_run`, and the queued-command launcher.
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
  `BaseRunner._llm_content` is the single call site every engine uses to reach
  it.
- `agent.runtime` defines the target `RunRuntime` and `ModelGateway` structural
  contracts. `DefaultRunRuntime` owns synchronized run saves, event emission,
  durable run timing, status and warning transitions, activity/model-wait
  projections, budgets, dynamic limits, deadlines, checkpoints, controls,
  inbox draining, approvals, and auditor interactions. `BaseRunner` forwards to
  that surface and owns only the shared durable *projections*: the task/stage
  plan, artifact entries, approval batches, and proposal items. `ActionRunner`,
  `IntakeRunner`, and every workflow execution adapter accept an injected
  `RunRuntime` while preserving their existing default construction. The
  domain-neutral `WorkflowRunner` receives `RunRuntime` and all scheduler
  dependencies by composition and inherits nothing.
- `documents.document_chat` accepts an injected `model_adapter` so its calls are
  charged to the same budget and provenance ledger. `doc_tests.run_item` still
  accepts one for the same reason, but no agent path supplies it any more: since
  Phase 10 a Q&A worklist reaches the provider only through the registered
  `fieldwork.document_qa` worker, and `executors.fieldwork.run_document_test`
  raises rather than making an unbudgeted call.
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
  methods. Routing lives in `agent/routing.py`. The former
  `agent/workflow_runner.py`, legacy adoption, and run-shape translation were
  deleted. Runtime import-boundary tests reject planning, RCM, document,
  finding, report, and audit-domain imports.
- Phase 7 completed with the audit lifecycle declared in exactly one place. The
  dependency graph lives in `agent/workflows/audit.py`; the twelve capability
  declarations live in the grouped `agent/capabilities/` package, which is the
  live registry; and each capability's execution is a registered worker/executor
  pair or a deterministic local computation under `agent/workers/` and
  `agent/executors/`, with declared context presets in `agent/context/`. Every
  model-facing input is resolved from a declared preset, so no capability builds
  an ad-hoc prompt bundle. The transitional batch binding kind and every batch
  stage handler are deleted, not merely unused. Two recorded behavior changes:
  planning documents are chosen by a declared deterministic category rule rather
  than a model turn, and planning no longer generates document analyses as a side
  effect — every capability consumes the document material that exists, and
  Phase 9's `documents.analysis_generated` capability restores on-demand
  generation as a declared dependency.
- Phase 8 added the exploratory data-analysis workflow on the same scheduler:
  `data.relationships_inferred` → `data.join_utility_ready` →
  `data.joins_ready` → `analysis.register_ready` →
  `analysis.definitions_ready` → `analysis.executed` → `analysis.summarized`
  (the memo and the register were both added later; see the analysis-summary
  note below and the register note under §4). It introduces no workspace
  collection — relationship evidence is run-durable, joins land in
  `workspace.joins`, definitions in `workspace.analyses`, and each execution
  records a bounded `last_result` (shape, verdict, the analytics service's own
  statistics, and the count of rows it flagged) rather than result data. The
  flagged rows themselves are the one exception, and they live in an evidence
  sidecar, not on the definition — see §4. Relationship facts are never
  model-generated: they come from the deterministic diagnostics in
  `agent/joins.py`, and a join is applied automatically only on a single strong
  candidate. Requests such as "perform relevant joins and data analysis" and the
  `data_analysis` goal template now route to this workflow; "run this saved
  analysis" and "pin this result" stay with `ActionRunner`.
- Phase 9 unified document analysis on the same scheduler:
  `documents.text_ready` → `documents.analysis_chunks_ready` →
  `documents.analysis_generated` → `documents.analysis_reviewed`. There is now
  exactly one chunk-map worker, one reduction worker, and one persistence
  executor; `DocumentAnalysisRunner`, the `document_analysis` engine and run
  kind, and the runner-era map/reduce prompts are deleted. A chunk analysis is
  run-local — its durable home is the unit's proposal sidecar, not a workspace
  collection — so chunk units declare the new `all_settled_parallel` barrier and
  are the first proposal-only pipeline units (`UnitPipelineRequest.executor_id`
  may be `None`). Only the reduced analysis is an engagement artifact, committed
  through the existing `Documents/.analysis` sidecars under the document's
  material parent hash and stamped with run/unit/content provenance so an
  interrupted commit is reconciled rather than repeated. The audit graph declares
  the same three generation capabilities and the scoped
  `planning.context_ready → documents.analysis_generated` edge, so planning is
  grounded in generated analyses again; with no planning-relevant document in
  scope every document capability is satisfied and no unit expands. Generated and
  auditor-reviewed remain distinct outcomes: nothing the agent does satisfies
  `documents.analysis_reviewed`.
- Phase 10 settled the two remaining leaf runners with an explicit decision
  record, [docs/agent-protocol-runner-decisions.md](docs/agent-protocol-runner-decisions.md).
  **Document tests migrated:** the standalone `doc_tests_workflow_v2` graph
  (`doc_tests.definitions_ready` → `doc_tests.executed` →
  `doc_tests.dispositioned`) replaced `DocTestRunner`, and the `doc_test` engine
  and run kind are deleted. Both graphs bind Document Test units through one
  function, `doc_tests_execution.bind_document_test_unit`, and expand them
  through one function, `capabilities.doc_tests.document_test_units`, so a
  worklist behaves identically whether an audit run or a standalone request
  scheduled it. A Q&A test therefore reaches the provider only through the
  registered `fieldwork.document_qa` worker and the declared page context, never
  through `doc_tests.run_item`'s model adapter. Nothing the agent does satisfies
  `doc_tests.dispositioned`. **Intake retained:** its authoritative state is the
  staged batch under `Imports/`, not the workspace; there is exactly one unit at
  every step; and `apply_batch` creates the artifacts rather than committing to
  existing ones. `IntakeRunner` was converted onto `RunRuntime`, the registered
  `intake.classification` worker, the declared `intake.classification` context
  preset (a new `staged_files` source type and `file_metadata` representation
  under the deny-by-default `allow_file_metadata` permission), and a
  proposal-only `UnitPipeline` unit, so its one model call is manifested,
  budgeted, and resumable without re-billing.
- Phase 11 gave every request one route and one engine. `agent/routing.py` is
  the only classifier: the goal templates and phrase tables moved out of
  `action_runner.py` into pure functions, `ActionRunner`'s defensive guard is
  now `routing.workflow_owned_request(...)` (the same classification, so the
  guard and the persisted route cannot disagree), and the bounded router returns
  exactly `workflow | action | clarification | unsupported` with outcomes
  validated against the registered workflows and `action_intent` against the
  action registry. `store.RUN_ENGINES` is final at
  `{workflow, action, intake, analysis}`. Eight workflow-owned generators left
  the action catalog — `generate_apm`, `infer_relationships`,
  `run_document_test`, `rollup_rcm_results`, `generate_all_rcm_working_papers`,
  `generate_report`, `curate_dashboard`, `verify_audit_completion` — so no
  request is claimed by both engines; target-specific operations on the same
  families stayed. Removing `run_document_test` also closed the last path by
  which a Q&A worklist could reach the provider outside the registered
  `fieldwork.document_qa` worker and the run's budget. The `document_testing`
  goal template is replaced by `document_test_preparation`
  (`tests.specified`) and `document_test_execution`
  (`doc_tests.executed`). A compound request that genuinely needs both engines
  resolves to `clarification` rather than being split by a scheduler.
- Phase 12 retired v1. The fixed-stage `_Runner` pipeline (discovery →
  planning → joins → validation → analyses → dashboard → verify → summary) is
  deleted with its prompts, payload validators, fixed limits, `agent/summary.py`,
  the `analysis` engine value and run kind, and the `discovery` / `domain` /
  `custom_analyses` / `context_notes` projections. The one supported
  exploratory-analysis entry point, `POST /agent/runs` with no command, now
  starts an `analysis_workflow_v1` command run through the registered
  `data_analysis` goal template, carrying any `context.objective` as the command
  text. `runner.py` is a process layer: it creates the record, enforces the
  one-live-run rule, launches the thread, dispatches on the explicit engine, and
  imports no compute or domain module. `store.new_run` accepts only
  `kind="intake"`, and a terminal record that is neither a command run nor an
  `intake` run fails closed instead of being replayed.
- Phase 13 closed the migration. Dead delegation helpers (`llm_markdown`,
  `note_context`) were removed, the `workflow.definition` read-time default was
  deleted so a workflow record without one fails closed in `workflow_dispatch`,
  and three durable gates now hold the architecture in place:
  `test_agent_v1_retirement.py` (no v1 caller, engine, import, API response, or
  UI path), `test_agent_final_boundaries.py` (workflow definitions import only
  graph primitives; capabilities never schedule or persist; workers cannot reach
  a workspace, transaction, or run store; executors cannot reach a worker or the
  gateway; context cannot call a provider; one provider call site), and
  `test_agent_definition_of_done.py` (one test per definition-of-done bullet).
  `BaseRunner` is retained deliberately — not as a delegation facade but as the
  shared run-projection surface both engines and the intake runner write
  through.

### Known duplication

- The obsolete `ActionRunner` full-audit path and its prompt policy were
  removed in Phase 1. Broad-audit and planning requests fail closed if they
  bypass workflow routing; isolated action DAGs remain supported.
- The action ledger is domain-neutral: its audit-lifecycle switch, constants,
  enforcement helper, and catalog-specific artifact mappings have been removed.
  Action-specific reference rules live in the action catalog, while
  `workflows/audit.py` is the authoritative audit graph and
  `capabilities.REGISTRY` its live composition.

### Events and live UI

- Runs persist `run.json` and sidecars under
  `Workspaces/<id>/AgentRuns/<run_id>/`. The replayable event stream is in the
  workspace's `telemetry.db`, keyed by run, so reading forward from a cursor is
  an indexed range read rather than a scan of a growing log.
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
- A saved analysis has exactly one recorded outcome, its bounded `last_result`,
  and `app/analysis_results.py` owns that contract end to end — computing,
  bounding, recording, and classifying it. Both origins go through it: the
  auditor's Run (`POST /analyses/{id}/execute`) and the workflow's
  `analysis.executed` capability. A live recomputation is a *preview* and never
  the procedure's status.
- Spec-not-data has exactly one exception: the rows a procedure *flagged*. A
  conclusion of "2 rows are duplicated" is not reviewable until an auditor can
  see which two, so every execution retains up to
  `analysis_results.EXCEPTION_ROWS` of the analytics test's `detail` frame. They
  are stored under `Analyses/.results/<id>.json`, never on the definition —
  `Analyses/*.json` is an artifact collection whose every file is parsed on each
  workspace load. Both commit paths write them through
  `analysis_results.EvidenceWriter` as a journalled linked write, so the rows and
  the result they support share one revision. Evidence is stamped with its
  `result_sha1` and is discarded whenever the conclusion it supported is: an
  edited spec, a changed outcome policy, a deleted procedure, or a rerun that
  flags nothing. `GET /analyses/{id}/exceptions` reads it back without
  recomputing; the Data Test `exception_frame` is the precedent it follows.
- Listing analyses executes nothing. `GET /analyses` returns definitions and
  recorded outcomes; recomputation happens only where it was asked for — one
  procedure (`GET /analyses/{id}`), an execution, or an export. Reading flagged
  rows executes nothing either — it is a file read.
- The Analysis tab's Summary screen is the **EDA memo**, not a gallery of every
  procedure. `analysis.summarized` is the terminal outcome of the
  `data_analysis` template and of `FULL_AUDIT_OUTCOMES`, so a full audit writes
  it before planning reads it — while still not being a planning *dependency*.
  `analysis_execution` deliberately stops at `analysis.executed`: it exists to
  spend no model turn, and summarising is one.
  The memo is a **derived, read-only artifact** (`Analyses/.summary.json`, one
  `_ARTIFACT_OBJECTS` entry): there is no editor, no `PUT`, and no
  `created_by` ownership flip, so regenerating always replaces it and it cannot
  drift from the results it cites. `dashboard_advice` is the lifecycle
  precedent; the worker/executor contract is the modern one.
  Its structure is fixed in code (`workers.analysis.SUMMARY_SECTIONS`) and
  enforced by the semantic validator, which also rejects any `embed` fence
  citing a procedure the bundle did not supply — a citation that resolves to
  nothing is worse than none. Staleness is content-hashed by
  `analysis_results.summary_basis_digest`, over each analysis' `result_sha1`,
  so re-running a procedure that concludes the same thing does *not* stale the
  memo; only a changed conclusion does.
  `analysis.summary` is the one context preset that admits row-level
  engagement data, under its own `allow_analysis_exception_rows` permission —
  never `allow_table_rows`. It permits only rows a declared test already
  flagged, capped per procedure by `adapters.MAX_SUMMARY_EXCEPTION_ROWS`, and
  splits them across two sources by severity (`analysis_exceptions` for
  failures, `analysis_anomalies` for warnings) because deterministic selectors
  order candidates by reference: one source let a truncated budget drop a
  backdating failure while keeping a weekend-activity warning.
- **The assertion register decides what an exploratory run tests, once.**
  `analysis.register_ready` sits between the joins and the definitions and is
  the only stage that reads every frame together. It is built in two passes and
  the asymmetry between them is the whole contract. The floor is deterministic:
  every frame is swept, every nomination that flagged rows is collected, and the
  collection is deduplicated by `analysis_semantic_id` — identity over the
  computation *and* the population, so one check reachable from six frames of a
  join family is one entry while the same spec asked of different rows stays
  several. Then one `analysis.reading` turn may **keep** an entry under a better
  name, **add** an assertion no single measurement reaches, **decline** one with
  a written reason, or name what the data cannot answer at all — and silence is
  keeping. A nomination the turn never mentions is committed anyway, under the
  title and note its own measurement derives.
  That is what makes one turn over the whole engagement safe to depend on: its
  worst case is the floor it was handed, so a reading that fails after its
  repair attempt settles as `skipped` and the commit unit writes what was
  measured. Measured on the procurement engagement, the floor alone reaches 18
  of 28 scoreable answer-key items with no model turn at all.
  The capability carries two units of different kinds — `analysis_reading`
  (proposal-only) then `analysis_register` (deterministic commit) — ordered by
  the sequential barrier's sorted-unit-ID rule. Neither unit id names the frames
  it covers: a stage re-expands as joins land, and an id derived from the frame
  list becomes a second, already-billed unit the moment one does.
  The register is run-durable like the relationship evidence beside it, not a
  workspace collection: it is a recomputable reading of frames that already
  exist. `analysis.definitions_ready` then spends a turn only on the frames the
  register placed an *authored* assertion on — a kept nomination is already an
  exact spec with its counts measured, and re-deriving it through a model call
  was, measured across four runs, sometimes declined.
- **The memo feeds the APM.** `planning.apm` declares an optional
  `analysis_summary` source under its own `allow_analysis_summary` permission —
  planning sees the written memo, never the flagged rows behind it, and never
  `allow_analysis_exception_rows`. `apm.md` carries a matching **Data analytics
  performed** section before *Key risks and planned response*, since the risks
  are argued from it; the APM worker's existing validator enforces every
  template heading, so the section is answered for rather than optional.
  Embed directives are flattened to inline citations
  (`analysis_memo.flatten_embeds`) before planning sees them — planning has no
  renderer for a fence, and a raw directive copied into an APM prints as stray
  text. With no memo the source supplies nothing and the section says so; the
  edge is deliberately *not* a graph dependency, so an APM-only request stays
  independent of data analysis.
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
- A test is a single durable record with a single source. The former
  `rcm[].planned_tests` layer is gone, and the two-pass `tests.drafted` ->
  `tests.specified` flow was merged into one capability: `tests.specified`
  decides each test's source and writes its complete executable definition —
  Polars steps, or document steps with checks/questions — in a single model
  turn per RCM row via the `tests.generate` worker/executor. Nothing durable
  is left in an intermediate `draft` status by the audit workflow (`draft`
  stays a valid status only for manually authored or historical tests). An
  RCM row carries only `test_refs`, and its `execution_rollup` is the tally
  over them (`tests`, `completed`, `passed`, `failed`, `findings`). The
  rationale is in `docs/test-capability-merge-plan.md`.
- Generation deliberately emits a narrow slice of what the stores support:
  Data Tests as multi-step Polars code under `spec.steps` (the analytics and
  validation engines, and the single top-level `spec.code` shape, stay only
  for auditor-authored tests) and Document Tests as `qa` or `vouching` only
  (the `attribute` and `review` builders always route to manual review, so
  generating them buys nothing). `SCHEMA_VERSION` is 3 and there is no
  migration — a pre-3 workspace keeps its rows and loses its planned tests.
- Exploratory data analysis is a declared workflow too. `POST /agent/runs`
  without a command no longer starts a fixed pipeline: it requests
  `analysis.executed` through the `data_analysis` goal template. There is no
  frontend caller left for that path (`useAgentRun.startRun` is deleted); the
  endpoint remains an active API surface.
- `analysis_execution` is a second goal template requesting the same terminal
  outcome (`analysis.executed`) as `data_analysis`, for "run the saved
  analyses" / "/run analyses" — the scheduler reuses whatever definitions
  already exist and spends no model turn when there is nothing left to
  propose. `data_analysis` and `table_relationships` also accept a `tables`
  run-context key (`analysis_execution` additionally accepts `analysis_ids`),
  so the Analysis tab can scope a run to what is on screen instead of falling
  into the bounded `MAX_SCOPE_TABLES` fallback and its scope-ambiguity
  checkpoint. Manual analysis execution (`POST /analyses/{id}/execute`,
  auditor-initiated) and workflow execution write through the same contract in
  `app/analysis_results.py`; a manual result's `run_id` is prefixed
  `manual_...` and opens nothing in the run drawer, unlike an agent run's id.
- Document analysis is a declared workflow, not a leaf runner. The Documents tab
  still calls `/documents/analysis-runs`, but that endpoint now starts a
  `documents_workflow_v1` command run.
- Document-test execution is likewise a declared workflow.
  `POST /doc-tests/{test_id}/run` starts a `doc_tests_workflow_v2` command run
  naming that test as a workflow target, and since Phase 11 the DocTests tab's
  own Run and Prepare buttons send the `document_test_execution` and
  `document_test_preparation` workflow templates through assistant chat. Run
  context is a per-template scope allowlist (`routing.TEMPLATE_RUN_CONTEXT_KEYS`),
  so the Run button can name its test without widening the route.
- The debug console is a first-class local diagnostics surface, not just test
  scaffolding.
- PrimeVue `Select` still needs the full pointer event sequence in UI-driving
  tests; bare synthetic `.click()` remains unreliable on this machine.
