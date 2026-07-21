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
| `store.new_run(...)` | Explicit-engine `analysis`, `intake`, `doc_test`, or `document_analysis` protocol run | `runner.start_run(...)` |
| `store.new_command_run(...)` | Explicit-engine `workflow` command run, optionally routed to `action` before or during execution | `runner.start_command_run(...)` |

No route, chat service, or frontend component calls these writers directly.
They enter through the control surface in `backend/app/agent/runner.py`.
`BaseRunner`, `WorkflowRunner`, and the control functions in `runner.py` update
existing records and append events through `store.save_run(...)` and
`store.append_event(...)`.

## Run Creation And Continuation Callers

### Generic HTTP creation

`POST /api/workspaces/{workspace_id}/agent/runs` in
`backend/app/routes/agent_routes.py` is the public generic creation endpoint.

- A `command` object or `requested_outcomes` calls
  `runner.start_command_run(...)`.
- Otherwise it calls `runner.start_run(...)`; absent `kind` defaults to the
  legacy `analysis` run.
- The endpoint supplies `mode`, `context`, `parent_run_id`, requested outcomes,
  target references, and the current `missing_or_stale` refresh policy.

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
| Document-test API | `POST /api/workspaces/{workspace_id}/doc-tests/{test_id}/run` | `runner.start_run(..., kind="doc_test")` | `DocTestRunner` |
| Batch document analysis | `POST /api/workspaces/{workspace_id}/documents/analysis-runs` | `runner.start_run(..., kind="document_analysis")` | `DocumentAnalysisRunner` |
| Single-document analysis | `POST /api/workspaces/{workspace_id}/documents/{doc_id}/analysis-runs` | `runner.start_run(..., kind="document_analysis")` | `DocumentAnalysisRunner` |

The current `DocTestsTab.vue` does not call the specialized document-test
endpoint. Its Run and Prepare buttons send `document_testing` command actions
through assistant chat. The leaf endpoint and `DocTestRunner` remain active API
surfaces and have direct integration tests.

### Control-surface run creators

The following functions in `backend/app/agent/runner.py` can create additional
runs without a new top-level creation request:

- `retry_run(...)` starts a linked command run for a failed schema-v2/v3 audit
  run.
- `continue_audit(...)` starts a linked command run from deterministic
  `next_outcomes` on a run completed with open items.
- `steer(...)` starts a linked command or legacy-analysis follow-up when the
  addressed run is terminal, and queues commands when a command run is live.
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
`workflow_runner.initialize_known_workflow(...)` before launching it.
`workflow_runner._local_resolution(...)` resolves, in order:

1. Explicit `requested_outcomes`.
2. Known goal templates.
3. Recognized audit phrases.
4. Recognized isolated-action markers.

A locally recognized workflow is promoted to the schema-v3 capability graph
before the UI receives the new run. An unresolved command is handled after
launch by the bounded workflow router. A `generic_action` result delegates to
`ActionRunner`; workflow outcomes remain in `WorkflowRunner`; question or
unsupported results complete without mutation. `ActionRunner` invokes its
registered action interpreter when a generic action still needs a DAG.

### Worker-thread dispatch

`runner._execute(...)` loads the durable run and selects the current runner only
from its explicit `engine`:

| Persisted discriminator | Runner |
|---|---|
| `engine="intake"` | `IntakeRunner` |
| `engine="doc_test"` | `DocTestRunner` |
| `engine="document_analysis"` | `DocumentAnalysisRunner` |
| `engine="workflow"` | `WorkflowRunner` |
| `engine="action"` | `ActionRunner` |
| `engine="analysis"` | legacy `_Runner` analysis pipeline |

Missing or unsupported engines fail closed. No engine is inferred from `kind` or
`schema_version` while loading, resuming, or dispatching a run.

The four protocol values are explicit migration scaffolding for live schedulers,
not compatibility aliases. Document analysis migrates in Phase 9; Phase 10
decides the retained intake and document-test protocols; Phase 12 removes the
legacy analysis engine. The current runners all inherit, directly or indirectly,
from `BaseRunner`; composition through `RunRuntime` is introduced in later
phases.

## Active HTTP Run API

`backend/app/routes/agent_routes.py` exposes the shared agent API:

| Method and path suffix | Consumer behavior |
|---|---|
| `GET /api/agent/status` | Provider readiness shown by launch surfaces |
| `GET /api/agent/actions` | Registered action coverage |
| `POST /agent/runs` | Create command/workflow or specialized/legacy run |
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
| `DocTestsTab.vue` | Sends `document_testing` command actions through assistant chat |
| `ImportDialog.vue` | Sends the validated folder-intake special action; edits and approves classification proposals |
| `DocumentsTab.vue` | Directly starts single/batch document-analysis runs, polls records, and resumes partial analysis runs |
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

- Phase 1 must update both store writers and every creation/continuation path
  before dispatch can require `engine`.
- Routing changes must preserve assistant-chat intent handling, pre-launch local
  workflow materialization, bounded-router fallback, generic-action fallback,
  and queued-command FIFO behavior.
- Runtime extraction must preserve the generic agent API and chat projection
  payloads because the frontend joins both surfaces by `run_id`.
- Document analysis has direct UI/API callers in addition to audit-planning
  consumers; its Phase 9 migration must move both.
- Phase 10 must decide the current engine and scheduler disposition of both
  `IntakeRunner` and `DocTestRunner`; neither can be deleted based only on the
  current tab UX.
- Final cleanup must update the debug, findings, action-source, evidence-event,
  SSE, and workspace-path consumers as well as the visible drawer.
