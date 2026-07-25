# Agent Runtime Active-Surface Inventory

## Purpose And Scope

This inventory is the Phase 0 baseline for the agent-architecture migration. It
records active run writers, creators, routing decisions, scheduler dispatch,
HTTP endpoints, chat integration, frontend callers, and non-UI consumers as of
2026-07-21. It inventories live code paths, not historical persisted shapes.

The inventory is a migration map. A later phase may remove or redirect a path
only after its callers and characterized behavior move to the target runtime.

## Durable Write Boundary

All new durable agent runs are created under
`Workspaces/<workspace-id>/AgentRuns/<run-id>/` by two functions in
`backend/app/agent/store.py`:

| Writer | Current record | Direct production caller |
|---|---|---|
| `store.new_run(...)` | The one explicit-engine `intake` protocol run | `runner.start_run(...)` |
| `store.new_command_run(...)` | Command run with **no** engine; `routing.resolve_route(...)` persists the normalized route and the selected engine before thread launch | `runner.start_command_run(...)` |

No route, chat service, or frontend component calls these writers directly.
They enter through the control surface in `backend/app/agent/runner.py`.
`BaseRunner`, `DefaultRunRuntime`, and the control functions in `runner.py`
update existing records and append events through `store.save_run(...)` and
`store.append_event(...)`.

## Run Creation And Continuation Callers

### Generic HTTP creation

`POST /api/workspaces/{workspace_id}/agent/runs` in
`backend/app/routes/agent_routes.py` is the public generic creation endpoint.

- A `command` object or `requested_outcomes` calls
  `runner.start_command_run(...)`.
- Absent `kind`, or `kind="analysis"`, is the exploratory-analysis path: since
  Phase 12 it also calls `runner.start_command_run(...)`, with the registered
  `data_analysis` goal template, so the request becomes an
  `analysis_workflow_v1` run. Any `context.objective` becomes the command text.
- `kind="intake"` calls `runner.start_run(...)`. Any other `kind` fails closed.
- The endpoint supplies `mode`, `context`, `parent_run_id`, requested outcomes,
  target references, and the normalized `reuse_existing` or `force` generation
  mode.

`frontend/src/composables/useAgentRun.ts` wraps this endpoint with
`startCommand(...)` and `startRun(...)`. The composable also owns run history,
active-run attachment, SSE connection, pause/resume/cancel, steering,
interaction responses, approvals, and workspace-change notifications. The
current primary UX starts actions through assistant chat; the generic launch
methods and HTTP endpoint remain active callable surfaces.

### Durable assistant chat

`POST /api/workspaces/{workspace_id}/assistant/chats/{chat_id}/messages` in
`backend/app/routes/assistant_chat_routes.py` calls
`assistant_chats.send_message(...)`, which is the main product entry point.

`assistant_chats._process_message(...)` may:

- Answer read-only questions without creating a run.
- Resolve a pending run question or structured clarification.
- Start `runner.start_run(..., kind="intake")` for the validated
  `folder_intake` special case.
- Start `runner.start_command_run(...)` for an action when no run is active.
- Call `runner.steer(...)` to deliver an interaction response, steer a live
  run, queue a command on a live command run, or create a linked follow-up after
  a terminal run.
- Recover from a creation race by locating the active run and queueing the
  command through `runner.steer(...)`.

The chat record stores `run_id` and `command_id` outcomes. Transcript assembly
in `assistant_chats.get_chat(...)` reads `store.list_runs(...)` and
`store.load_run(...)`, creates `_run_projection(...)` entries, links parents,
and interleaves those projections with messages. `assistant_chats._active_run`
also uses the run store to decide whether to start, queue, or answer an active
interaction.

`frontend/src/composables/useAssistantChat.ts` wraps the chat API. On a
run-bearing outcome it calls `useAgentRun(...).openRun(run_id)`, joining the
durable transcript and shared live-run state.

### Specialized production creators

| Caller | Endpoint / trigger | Control call | Current runner |
|---|---|---|---|
| Folder intake chat flow | Assistant message with `source="folder_intake"`, `run_kind="intake"`, and batch context | `runner.start_run(..., kind="intake")` | `IntakeRunner` |
| Document-test API | `POST /api/workspaces/{workspace_id}/doc-tests/{test_id}/run` | `runner.start_command_run(...)` requesting `doc_tests.executed` | runtime `WorkflowRunner` (`doc_tests_workflow_v1`) |
| Batch document analysis | `POST /api/workspaces/{workspace_id}/documents/analysis-runs` | `runner.start_command_run(...)` requesting `documents.analysis_generated` | runtime `WorkflowRunner` (`documents_workflow_v1`) |
| Single-document analysis | `POST /api/workspaces/{workspace_id}/documents/{doc_id}/analysis-runs` | `runner.start_command_run(...)` requesting `documents.analysis_generated` | runtime `WorkflowRunner` (`documents_workflow_v1`) |

The current `DocTestsTab.vue` does not call the specialized document-test
endpoint. Since Phase 11 its Run button sends the `document_test_execution`
template (naming the test through run context) and its Prepare button sends
`document_test_preparation`; both are workflow requests, and the
`document_testing` template and the `run_document_test` action are deleted. The
endpoint itself remains an active API surface with direct integration tests;
since Phase 10 it starts a `doc_tests_workflow_v1` command run rather than a
leaf runner.

### Control-surface run creators

The following functions in `backend/app/agent/runner.py` can create additional
runs without a new top-level creation request:

- `retry_run(...)` starts a linked command run for a failed schema-v2/v3 audit
  run.
- `continue_audit(...)` starts a linked command run from deterministic
  `next_outcomes` on a run completed with open items.
- `steer(...)` starts a linked command run when the addressed run is terminal —
  including a finished folder intake — and queues commands when a command run is
  live. A terminal record that is neither a command run nor an `intake` engine
  run is a pre-cutover shape and fails closed instead of being replayed.
- `_launch_next_command(...)` starts the next queued command after the
  current run reaches a terminal state; if creation races or fails, it restores
  the command to the queue.

`runner.start_run(...)` and `runner.start_command_run(...)` both enforce
same-workspace serialization and the process-wide concurrency cap before
writing a record and launching a worker thread.

## Active Routing And Dispatch

### Chat intent routing

`assistant_chats._process_message(...)` first gives pending free-text and
structured interactions precedence. For `intent="auto"`, it tries
`_deterministic_intent(...)` and then the bounded `_classifier(...)` model
fallback. The resolved intent is `ask`, `act`, or `clarify`. Only `act` enters
run routing.

### Command routing

`runner.start_command_run(...)` creates the command record and calls
`routing.resolve_route(...)` before launching it. `routing.classify_command(...)`
is pure and resolves, in precedence order:

1. Explicit `requested_outcomes`.
2. A registered goal template.
3. A lifecycle-wide completion phrase.
4. Generation or refresh of a workflow-owned deliverable.
5. A target-specific operation (CRUD, attach/detach, pin, manual edit, or the
   rerun of one identified existing artifact).
6. Scope-wide declared execution.
7. A weak isolated-operation marker.

A recognized workflow is promoted to the schema-v3 capability graph before the
UI receives the new run. A `clarification` or `unsupported` result finishes the
run with no engine and no mutation. A command that matches nothing launches with
`route.status == "pending"`, and `routing.resolve_pending_route(...)` spends one
bounded router turn on the worker thread; that is the only routing path that
calls the provider, and it never repeats the deterministic pass. The router
worker returns `workflow | action | clarification | unsupported`, with outcomes
validated against the registered workflows and `action_intent` validated against
the action registry. `ActionRunner` invokes its registered action interpreter
when an action route still needs a DAG.

### Worker-thread dispatch

`runner._execute(...)` loads the durable run, calls
`routing.dispatch_engine(...)` — which finalizes a pending route, finishes a run
whose route selects no engine, or fails closed for an absent/unsupported
engine — and then selects the current runner only from that explicit `engine`:

| Persisted discriminator | Runner |
|---|---|
| `engine="intake"` | `IntakeRunner` |
| `engine="workflow"` | runtime `WorkflowRunner` composed by `workflow_dispatch.build_workflow_runner(...)` |
| `engine="action"` | `ActionRunner` |

Missing or unsupported engines fail closed. No engine is inferred from `kind`,
`schema_version`, or record contents while loading, resuming, or dispatching a
run. Because a pending-route command run has no engine yet, "is this a command
run?" is answered by the record shape (`store.is_command_run(...)`) rather than
by engine membership — that is what `steer(...)`, `retry_run(...)`, and the
queued-command launcher now test.

A workflow run additionally persists the authoritative workflow definition ID
routing resolved, so `workflow_dispatch.build_workflow_runner(...)` selects the
composition by lookup rather than inference:

| Persisted `workflow.definition` | Composition |
|---|---|
| `audit_workflow_v2` | `audit_execution.build_audit_workflow_runner(...)` |
| `analysis_workflow_v1` | `analysis_execution.build_analysis_workflow_runner(...)` |
| `documents_workflow_v1` | `documents_execution.build_documents_workflow_runner(...)` |
| `doc_tests_workflow_v1` | `doc_tests_execution.build_doc_tests_workflow_runner(...)` |

A missing or unsupported definition fails closed the same way.

One protocol engine value remains, and it is not a compatibility alias.
Phase 9 removed the `document_analysis` engine and its runner outright, Phase 10
removed `doc_test` the same way, and Phase 12 removed the legacy `analysis`
engine with the `_Runner` pipeline itself. `intake` was *retained* with a
recorded justification (see
[agent-protocol-runner-decisions.md](agent-protocol-runner-decisions.md)), so
`store.RUN_ENGINES` is final at `{workflow, action, intake}`. The runtime `WorkflowRunner` is composed directly with
`RunRuntime` and does not inherit from another runner. Current leaf runners,
`ActionRunner`, and the temporary audit execution adapter still inherit from
the temporary `BaseRunner` facade, which delegates per-run persistence, events,
budgets, controls, approvals, interactions, and model calls to
`DefaultRunRuntime` and `DefaultModelGateway`. `DefaultRunRuntime` also
owns atomic, integrity-checked persistence for content-free per-unit context
manifests. The generic context resolver now enforces deterministic ordering,
hard source/global limits, required/optional behavior, and deny-by-default
representations over local candidate scopes. Its automatic selection boundary
is closed to provider/network services and supports only deterministic
metadata, local lexical scoring, or local embeddings bound to model/index
hashes, all with stable tie-breaking. A provider-free APM adapter now exposes
current bounded document analyses, indexed methodology sections, table schema
metadata, and statistical profiles through data-only candidate scopes without
copying extraction, search, analysis, pack-index, or profiler logic. Profile
candidates omit category literals and row previews, while the context models
structurally reject `table_rows` candidates, selections, and bundle items. The
active `planning.apm_ready` workflow capability now uses this boundary for
every APM model input and enters the runner-independent `UnitPipeline`. The
pipeline invokes the registered bundle-only `planning.apm` worker, persists the
exact-identity proposal before approval, commits through the registered
deterministic APM executor, persists its postcondition receipt, and reevaluates
readiness last. Its proposal recovery identity includes the manifest, spec,
resolver, selector definitions and selected sources, prompt, worker, schemas,
capability declaration, and unit input; mismatches regenerate rather than
reuse the sidecar. Compatible proposals resume without another provider call,
while commit-before-receipt recovery reconciles the workspace before any
repeated mutation. Workflow units durably project context, proposal, and
receipt references. The old runner-owned APM prompt, caller, validator, writer,
and quality helper are no longer active surfaces.
Context policy is declaration-only: registered application capability and
preset definitions are authoritative, with no per-run, workspace, API, or
frontend auditor override. Auditor source curation and explicit regeneration
operate within, and cannot widen, those declarations.

`ActionRunner`, `IntakeRunner`, and the audit, analysis, document, and
document-test workflow compositions all accept an injected `RunRuntime`. The `ActionRunner` `classify_import_batch` action borrows
`IntakeRunner._classify` with the action run's own runtime and ledger lock rather
than constructing a second runtime over the same record. The audit
composition constructs the document composition with its own runtime *and* ledger
lock, so both write the one durable run record under one lock.

## Active HTTP Run API

`backend/app/routes/agent_routes.py` exposes the shared agent API:

| Method and path suffix | Consumer behavior |
|---|---|
| `GET /api/agent/status` | Provider readiness shown by launch surfaces |
| `GET /api/agent/actions` | Registered action coverage |
| `POST /agent/runs` | Create a command/workflow run, or the folder-intake protocol run |
| `GET /agent/runs` | Recover orphans and list history |
| `GET /agent/runs/{run_id}` | Recover orphans and load full run |
| `GET /agent/runs/{run_id}/sidecars/{sha1}` | Load bounded interaction/action sidecar content |
| `POST /agent/runs/{run_id}/pause` | Request pause |
| `POST /agent/runs/{run_id}/resume` | Resume live or interrupted work |
| `POST /agent/runs/{run_id}/retry` | Create linked retry |
| `POST /agent/runs/{run_id}/continue` | Create linked continuation |
| `POST /agent/runs/{run_id}/cancel` | Cancel with auditor/API context |
| `POST /agent/runs/{run_id}/messages` | Steer, answer, queue, or follow up |
| `POST /agent/runs/{run_id}/approvals/{approval_id}` | Persist and deliver approval decisions |
| `POST /agent/runs/{run_id}/interactions/{interaction_id}/respond` | Persist and deliver structured input |
| `GET /agent/runs/{run_id}/events` | Replay/tail `events.jsonl` as SSE by cursor or `Last-Event-ID` |

The document-analysis and document-test creation endpoints listed above are
additional active run APIs.
`GET /documents/{doc_id}/analysis-runs/{run_id}` reads and validates a document
analysis run association.

## Active Frontend Callers And Consumers

| Frontend surface | Active relationship to runs |
|---|---|
| `WorkspaceView.vue` | Acquires the workspace-scoped `useAgentRun` store and mounts the persistent `AgentDrawer` |
| `AgentDrawer.vue` / `ChatComposer.vue` | Initialize the run/chat stores; provide the main chat action entry point, pause/cancel, approvals, and interaction responses |
| `ChatTranscript.vue` / `ChatRunCard.vue` | Render chat run projections; load full records; pause/resume/cancel, retry, continue, approve, and respond |
| `PlanningTab.vue` | Sends the `planning` goal template through assistant chat |
| `PostImportPlanningOffer.vue` | Sends planning with imported document IDs as run context |
| `DocTestsTab.vue` | Sends the `document_test_preparation` and `document_test_execution` workflow templates through assistant chat |
| `ImportDialog.vue` | Sends the validated folder-intake special action; edits and approves classification proposals |
| `DocumentsTab.vue` | Starts single/batch document-analysis workflow runs through the documents endpoints and polls the shared run record |
| `DashboardTab.vue`, `AnalysisTab.vue`, `PlanningTab.vue`, `DocTestsTab.vue`, `ImportDialog.vue`, `DocumentsTab.vue`, and `validation/ValidationTab.vue` | Share `useAgentRun`; relevant tabs subscribe to `workspace_changed` events and refresh affected data |

`useAgentRun.ts` is the sole frontend EventSource owner. It listens to status,
plan, task, graph, action, interaction, approval, activity, workflow, stage,
unit, checkpoint, revision, evidence, and `workspace_changed` events; it
refetches the authoritative run after event bursts and reloads history at
`stream_end`.

## Other Active Readers And Projections

- `backend/app/debug_service.py` and `backend/app/routes/debug_routes.py` list
  runs, load run details, and read the event stream for the local debug console.
- `backend/app/findings.py` loads a run when validating a finding's originating
  run reference.
- `backend/app/agent/actions.py` may load a source run when executing an action
  against an artifact produced by another run.
- `backend/app/agent/runner.py::notify_evidence_available(...)` scans recent
  runs and publishes targeted `evidence_available` events to live or affected
  workflow runs.
- `backend/app/workspaces.py` recognizes paths beneath `AgentRuns` as runtime
  persistence rather than ordinary workspace-definition writes.

## Clean-Slate Cutover Boundary

The target architecture starts only after the application data root's
`Workspaces/` directory is empty. Before enabling the target writers or
dispatch, operators must either delete disposable development workspaces or
move the entire pre-cutover workspace set outside the configured application
data root. Moving only `AgentRuns/` is insufficient because chats, artifacts,
workspace fields, and debug records also carry pre-cutover shapes.

The boundary is intentionally strict:

- No pre-cutover workspace, run, chat, artifact, or debug record is a supported
  target input.
- No legacy fixture directory, converter, record-shape inference, compatibility
  projection, or resume adapter will be added.
- Same-schema recovery tests create records with the writer under test and
  restart or resume those records within the same test.
- Target run creation persists `engine="workflow"` or `engine="action"` (and
  only a separately justified current engine from Phase 10). Target dispatch
  fails closed when the explicit engine is absent or unsupported.
- The existing workspace root must not be emptied during incremental phases.
  The destructive cleanout is an operational cutover prerequisite, not part of
  Phase 0 implementation.

## Migration Checkpoints Derived From The Inventory

- Phase 1 updated both store writers and every creation/continuation path;
  dispatch now requires `engine`, does not infer it while loading or projecting
  records, and has no retired v2 runner alias or compatibility module.
- Routing changes must preserve assistant-chat intent handling, pre-launch local
  workflow materialization, bounded-router fallback, generic-action fallback,
  and queued-command FIFO behavior.
- Runtime extraction must preserve the generic agent API and chat projection
  payloads because the frontend joins both surfaces by `run_id`.
- The Phase 3 exit gate proves shared budgets and controls across action,
  workflow, intake, document-test, and document-analysis runners, terminal
  crash handling before queued-command launch, and one direct provider-call
  path through `runtime/model_gateway.py`.
- Phase 9 moved both document-analysis caller families to the declared workflow:
  the Documents tab's two endpoints now start `documents_workflow_v1` command
  runs, and the audit graph reaches the same capabilities through
  `planning.context_ready`'s scoped `documents.analysis_generated` dependency. No
  `DocumentAnalysisRunner`, `document_analysis` engine, or `document_analysis` run
  kind remains, and the Documents tab's resumable-leaf-run controls were removed
  with the concept.
- Phase 10 decided both. `DocTestRunner`, the `doc_test` engine, and the
  `doc_test` run kind are deleted: standalone document-test execution is the
  declared `doc_tests_workflow_v1` graph, and both it and the audit graph bind
  Document Test units through one function. `IntakeRunner` is retained as a
  justified protocol runner and converted onto `RunRuntime`, the registered
  `intake.classification` worker, the declared `intake.classification` context
  preset, and a proposal-only `UnitPipeline` unit.
- Phase 12 retired v1. The generic creation endpoint's non-command path starts
  an `analysis_workflow_v1` command run through the registered `data_analysis`
  goal template; `_Runner`, `STAGES`, the v1 prompt/validator set,
  `agent/summary.py`, the `analysis` engine and run kind, the
  `discovery`/`domain`/`custom_analyses`/`context_notes` projections, and the
  frontend `startRun`/`AgentDiscovery` surface are deleted. `runner.py` is now a
  process layer that imports no compute or domain module.
- Phase 13 closed the migration. The remaining dead delegation helpers were
  removed, the workflow-definition default was deleted so a record without one
  fails closed, and three durable gates were added:
  `test_agent_v1_retirement.py`, `test_agent_final_boundaries.py`, and
  `test_agent_definition_of_done.py`.
- Phase 11 consolidated routing and dispatch. A request is classified once, in
  `agent/routing.py`, and the normalized route plus the selected engine are
  persisted before thread launch and projected by `store.run_summary(...)` as
  `route`. `initialize_known_workflow`, `route_unresolved_run`,
  `local_resolution`, `validate_route`, and the
  `partial_resolution`/`command_route`/`legacy_adoptions` run fields are
  deleted, not aliased. Eight workflow-owned generators left the action catalog,
  so no request can be claimed by both engines; the removal of
  `run_document_test` also closed the last unbudgeted document Q&A path.
  Compound cross-engine requests resolve to `clarification` instead of being
  split by a scheduler.
- Final cleanup must update the debug, findings, action-source, evidence-event,
  SSE, and workspace-path consumers as well as the visible drawer.
