# Unified Assistant Chat Overhaul

> **Status (2026-07-16): proposed.** This is an implementation plan, not a
> description of current behavior. The current drawer still combines an
> in-memory Q&A transcript with one selected durable run.
>
> **Context update (2026-07-17):** the implemented local-model design no longer
> has document opt-in, PII masking, disclosure logs, or metadata-only structured
> context. References to those controls below are retained as historical design
> rationale. Current chats use bounded unmasked previews, automatic attached
> document context, source/version checks, and AI-activity provenance.

## Outcome

Replace the drawer's two disconnected message systems with durable,
workspace-scoped chats. A chat presents ordinary Q&A, action requests, agent
runs, follow-up commands, questions, approvals, citations, and local result
artifacts in one chronological transcript.

The overhaul is primarily a conversation and presentation layer over two
existing engines:

- `assistant.py` remains the read-only, metadata-safe Q&A tool loop.
- `agent/runner.py` and the schema-v2 command runner remain the only path for
  agent-managed workspace changes.

The chat service routes requests to those engines and records what happened;
it does not become a second agent runner or mutation authority.

## Problems to Solve

- Ordinary Q&A exists only in Vue memory and disappears on refresh.
- Each Q&A request is stateless, so pronouns and follow-up questions lack
  conversational context.
- Agent messages are stored per run and rendered separately from Q&A turns.
- The composer's normal Send and sparkle actions have different semantics that
  are difficult to discover.
- Run plans, summaries, controls, approvals, and messages compete for space in
  fixed sections outside the transcript.
- Tab-triggered actions start runs without leaving a clear conversational
  record of what the auditor requested.
- Edited Polars and rerun results mutate frontend objects only and revert after
  refresh.

## Product Principles

1. **One composer, one transcript.** The user should not have to decide whether
   a sentence belongs to “chat” or “agent messages.”
2. **Questions are safe by default.** Ambiguous language must not cause a
   workspace mutation.
3. **Runs remain durable and independently auditable.** A chat references run
   records; it does not copy or own their action ledger.
4. **One source of truth per fact.** Chat owns conversational entries and local
   Q&A artifacts. Run storage owns execution state, approvals, interactions,
   warnings, and summaries.
5. **Privacy constraints survive conversation history.** History must never
   become a way to send raw frames, tool results, run logs, or previously
   disclosed document text to a model under a broader context.
6. **Local-first remains literal.** Chat prompts, answers, citations, and
   artifact previews are stored only inside the workspace directory.
7. **Recovery favors safety over magic.** An interrupted Q&A request becomes a
   visible failed turn; it is not silently replayed against the model.

## Scope

### Included

- Multiple durable chats per workspace
- Stateful, bounded Q&A context
- `auto`, `ask`, and `act` message intents
- Safe intent routing and clarification
- Inline run cards and run interactions
- Active-run command queuing and linked follow-up runs
- Chat-scoped document attachments
- Durable citations, tool summaries, editable code, and bounded local results
- Tab and intake entry points routed through the active chat
- Rename, delete, and new-chat flows

### Not Included

- Importing existing run history into chats
- Rewriting or migrating legacy run files
- Message editing, regeneration, branching, or per-message deletion
- Cross-workspace chats or search across chat content
- Token streaming from the LLM
- Multiple simultaneously mutating runs in one workspace
- A second event log for chat; run progress continues to use run SSE

Existing runs remain readable through the existing run APIs. They are not
listed as historical chats. If an existing run is active during upgrade, the
drawer still surfaces it as a temporary workspace-level run card until it
finishes so approvals and cancellation cannot become inaccessible.

## Architecture Decisions

### Chat and run ownership

`AssistantChats` is the transcript index. `AgentRuns` remains the execution
ledger.

- A user action message stores its routing outcome and a run or command
  reference.
- A run card is hydrated from the referenced run record on chat read.
- Expanding a run card fetches the full run using the existing run API.
- Run progress, controls, approvals, and structured interactions continue to
  use the existing run endpoints and SSE stream.
- Run-internal messages are not copied into `chat.json`. Legacy planning
  interview messages and schema-v2 interactions are projected into transcript
  items with stable derived IDs and their original timestamps.

This avoids dual writes where a chat and a run can disagree about run status.

### Artifact storage

Do not embed every result frame in `chat.json`. A long chat containing many
200-row previews would make every atomic chat update increasingly expensive.
Store each Q&A artifact as an atomic sidecar and keep only its ID on the
message.

```text
Workspaces/<workspace-id>/AssistantChats/<chat-id>/
├─ chat.json
└─ artifacts/
   └─ <artifact-id>.json
```

Artifact sidecars may contain local row previews because they never leave the
machine. Model context is still built through `assistant._frame_for_model` and
must never read these frames back into a prompt.

### No chat event stream in the first release

Message submission is request/response. Run changes already have replayable
SSE. The frontend refreshes the active chat after a run event burst and when a
parent run ends, which also discovers a newly started queued-command child.
This keeps the first implementation from introducing a second cursor and
reconnection protocol.

## Durable Data Model

Add `backend/app/assistant_chats.py`. Follow the run store's atomic-write and
per-path lock patterns, but keep chat locks separate from run locks.

### Chat record

Illustrative schema (field names are normative; exact helper structure is not):

```json
{
  "schema_version": 1,
  "id": "chat_20260716_153012_a1b2c3",
  "workspace_id": "engagement-id",
  "title": "Review duplicate payments in Q2",
  "title_source": "auto",
  "created_at": "2026-07-16T10:30:12+00:00",
  "updated_at": "2026-07-16T10:34:08+00:00",
  "next_ordinal": 5,
  "composer_context": {
    "document_ids": ["DOC-123"]
  },
  "messages": []
}
```

- IDs must be generated by the backend and validated before path use.
- `title_source` is `auto` or `user`; automatic title updates never overwrite
  a manual rename.
- The first non-empty user message generates a local title: collapse
  whitespace, remove control characters, take at most 60 characters, and add
  an ellipsis only when truncated. No model call is used for titles.
- `updated_at` changes for transcript, title, context, and artifact edits so
  chat history sorts by actual recent activity.
- Missing referenced documents are retained in message history but removed
  from the active composer context and reported to the client.

### Chat message

```json
{
  "id": "msg_...",
  "ordinal": 3,
  "role": "user",
  "kind": "text",
  "content": "Pin that analysis and add a note",
  "created_at": "...",
  "request_id": "client-generated-uuid",
  "state": "complete",
  "requested_intent": "auto",
  "resolved_intent": "act",
  "reply_to_id": null,
  "document_context": null,
  "artifact_ids": [],
  "outcome": {
    "kind": "run_started",
    "run_id": "...",
    "command_id": "..."
  },
  "error": null
}
```

Messages use these constrained values:

- `role`: `user` or `assistant`
- `kind`: `text`, `clarification`, or `error`
- `state`: `pending`, `complete`, or `failed`
- `requested_intent`: `auto`, `ask`, or `act`
- `resolved_intent`: `ask`, `act`, `interaction_response`, or `clarify`

Assistant Q&A messages additionally hold `citation_ids`, `artifact_ids`, a
bounded `tool_trace`, and an immutable document-context snapshot. Tool traces
contain tool name, success/failure, and locally safe argument summaries; they
never contain full tool responses, model message arrays, artifact frames, or
run logs.

An action outcome is one of:

- `run_started`: a new run began
- `command_queued`: a command was queued on the active schema-v2 run
- `interaction_answered`: the message answered a pending free-text interaction
- `clarification_requested`: no execution occurred
- `answer`: a read-only Q&A response was appended

Run-derived transcript items are response projections, not stored chat
messages. Use stable IDs such as `run:<run-id>:interaction:<interaction-id>` or
`run:<run-id>:message:<index>`, and include a `derived: true` marker so the
frontend never tries to mutate them through chat APIs.

### Artifact sidecar

Start from the current `AssistantArtifact` payload and add:

- `chat_id`, `message_id`, `created_at`, and `updated_at`
- `revision` for optimistic updates
- `last_run_at` and `last_error`
- the bounded `frame`, `total_rows`, `stdout`, editable `code`, `spec`, and
  `viz`

Only allow client updates to `code` and supported presentation fields. Frames,
row counts, stdout, and execution errors are server-owned rerun results.
Reject stale writes with HTTP 409 when `revision` does not match.

### Run and queued-command linkage

Add nullable `chat_id` and `source_message_id` to newly created run records and
run summaries. Add the same fields to schema-v2 queued commands. Hydration must
default both fields to `None`, preserving legacy readability.

When a queued command starts its child run, propagate those two fields. This
lets chat hydration replace a queued placeholder with the child run card after
the parent stream ends without copying run state into the chat.

Deleting a chat does not rewrite linked run files. Dangling chat linkage is
valid and ignored by chat listing.

## API Design

Add a router under `/api/workspaces/{workspace_id}/assistant/chats`.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/chats` | Newest-updated chat summaries; support bounded `limit` and cursor |
| `POST` | `/chats` | Create an empty `New chat` record and return it |
| `GET` | `/chats/{chat_id}` | Load transcript, artifact summaries, and compact hydrated run projections |
| `PATCH` | `/chats/{chat_id}` | Rename or update chat-scoped document attachments |
| `DELETE` | `/chats/{chat_id}` | Delete only the chat directory and artifact sidecars |
| `POST` | `/chats/{chat_id}/messages` | Append and route one message |
| `PATCH` | `/chats/{chat_id}/artifacts/{artifact_id}` | Persist an allowed edit with expected revision |
| `POST` | `/chats/{chat_id}/artifacts/{artifact_id}/rerun` | Execute edited code locally and atomically persist the result |

The message request contains:

```json
{
  "content": "Prepare the report from the current findings",
  "intent": "auto",
  "mode": "permission",
  "request_id": "browser-generated-uuid",
  "goal_template": null,
  "source": "composer"
}
```

- `source` is `composer`, `shortcut`, `tab_button`, or `folder_intake`.
- `goal_template` is accepted only with `act` and must be one of the command
  runner's registered templates.
- The server reads document IDs from the chat's composer context rather than
  trusting a separate unsynchronized frontend store, then snapshots them onto
  the user and assistant messages.
- Return `{ outcome, chat }` so the submitting client can render the durable
  state immediately. A run SSE connection begins when the outcome identifies
  a run.

Keep `POST /assistant`, `POST /run-python`, and direct agent-run creation
available temporarily for compatibility and tests. New drawer and tab code
must use the chat APIs. Remove the compatibility paths only in a later cleanup
after all callers are migrated.

### Provider readiness

The unified drawer must load both existing profiles instead of treating agent
status as the status of all chat:

- Read-only Q&A and the fallback classifier use the `assistant` profile.
- Command interpretation/execution uses the `agent` profile, which may have
  `AGENT_PROVIDER`/`AGENT_MODEL` overrides.
- If Q&A is configured but the agent override is not, questions remain
  available and action requests produce a durable configuration error.
- If only the agent profile is configured, explicit action shortcuts remain
  available. Ambiguous `auto` messages cannot use the classifier and must ask
  the user to choose Ask or Act; they must not guess an action.

Expose these as separate capability flags in the chat store. Do not disable
the entire composer merely because one profile is unavailable.

### Idempotency and failures

`request_id` is required and unique within a chat.

1. Under the chat lock, append a pending user message and reserve the request
   ID and ordinal.
2. Release the file lock before classification, LLM calls, or run startup.
3. Serialize message processing per chat with an in-memory processing lock.
4. Reacquire the file lock to append/finalize the assistant response and
   outcome.
5. A duplicate request ID returns the existing outcome and never calls the
   model or runner again.

Do not hold a filesystem lock during network or computation work. If the
backend restarts with a pending message, the next load marks it failed with an
“interrupted before completion” error. The user may retry as a new request.

Concurrent Q&A in different chats is allowed. Workspace mutation concurrency
is still enforced by the runner. If run startup races with another action,
reload the active run and queue on it only when it is schema-v2; otherwise
return a durable clarification/error rather than bypassing the busy guard.

## Message Routing

### Routing precedence

For each message, apply this order:

1. If a chat-visible active run has a pending free-text planning interview or
   schema-v2 clarification, consume the message as its answer. Approval,
   target-choice, and confirmation interactions still require their typed
   inline controls.
2. Honor explicit `ask` or `act` intent. Shortcuts and tab buttons always send
   `act`.
3. For `auto`, apply deterministic high-confidence rules.
4. Use the constrained classifier only when deterministic routing is
   inconclusive.
5. If classification is malformed, low-confidence, or depends on an unclear
   pronoun, append a clarification response and do nothing.

The deterministic fast path also avoids paying for a separate classifier call
on obvious questions and actions.

### Deterministic rules

Clear read-only questions include requests to explain, summarize, compare,
inspect, count, calculate, or answer without words that request persistence or
workspace change.

Clear actions include requests to create, edit, update, delete, remove, save,
pin, rerun, execute, import, generate, prepare, preserve, reconcile, approve,
or run an audit procedure. These are routing hints only; the command runner's
typed action registry and approval policy still decide what may execute.

Do not build a broad regex that pretends to understand every sentence. Rules
should cover obvious imperatives and explicit UI actions; everything else goes
to the classifier or clarification.

### Classifier contract

The classifier returns strict JSON:

```json
{
  "intent": "ask | act | clarify",
  "confidence": "high | medium | low",
  "clarification": null
}
```

It receives only:

- the current user-authored text;
- up to two preceding eligible user/assistant text turns;
- compact identifiers and titles for artifacts or runs in the immediately
  preceding response; and
- whether a schema-v2 run is active.

It never receives frames, tool payloads, source rows, run logs, document text,
or the workspace artifact index. Only `high` confidence may route to `act`.
`medium` or `low` action classifications become clarifications. A clear
question may safely fall back to `ask`.

For phrases such as “pin that” or “update it,” attach a `context_refs` list to
the command only when the immediately preceding transcript item has exactly
one compatible artifact. Otherwise ask what the user means. The command
runner remains responsible for resolving and validating the target.

### Action behavior

- With no active workspace run, create a schema-v2 command run using the saved
  Auto/Ask-before-changes mode.
- During an active schema-v2 run, append a linked queued command. General chat
  must not mutate the active action graph in place.
- When that run finishes, the existing queue launcher creates a child run and
  propagates chat/message linkage.
- When a legacy run is active, ordinary Q&A remains available, but inferred
  actions receive a clear “wait, pause, or cancel the current run” response;
  do not reinterpret them as legacy steering.
- After a terminal run, a new action creates a linked follow-up only when the
  immediately preceding relevant run belongs to the same chat. Otherwise it
  starts a new root command run.

Auto mode still executes only actions that pass the command runner's typed
validation, optimistic preconditions, and risk policy. Ask-before-changes uses
the existing approval/interaction gates. Chat classification grants no new
mutation authority.

## Conversational Q&A Context

Refactor `assistant.ask` to accept an already filtered list of prior text
turns. It must still construct tool messages internally and keep
`_frame_for_model` as the only path for computed frame results sent to the
model.

Use a bounded, contiguous history window:

- at most 8 prior user/assistant messages;
- at most 12,000 total characters;
- at most 2,000 characters from any one prior message; and
- only completed read-only Q&A text, never action messages, run-derived items,
  clarifications, errors, citations, traces, or artifact content.

Budgets should be named constants with unit tests. Add the current question
after applying the history budget so it is never truncated.

### Document-context eligibility

Every Q&A turn snapshots:

- normalized attached document IDs;
- the disclosed document source hashes/versions;
- whether Document AI was enabled;
- whether PII masking was enabled; and
- the disclosure manifest already returned by `assistant.ask`.

A prior document-grounded user/assistant pair is eligible only when:

1. Document AI is currently enabled.
2. The current attached document ID set exactly matches the prior set.
3. PII-masking mode matches.
4. Each referenced document still has the same source hash/version.

If any check fails, stop the contiguous history window at that boundary. Do
not silently include only half of a prior question/answer pair. Non-document
turns are eligible only for a current non-document request. This conservative
rule prevents an old quoted answer from crossing disclosure scopes.

Citations remain stored immutable evidence anchors. If their source document
is later removed, render the citation as unavailable rather than deleting or
silently retargeting it.

## Transcript and Drawer Experience

### Layout

- The drawer header shows the active chat title, a menu button, model/run
  status, and collapse control.
- The body is one scrollable transcript.
- The composer is fixed to the bottom and has one Send button.
- Auto/Ask-before-changes is a compact composer setting and remains shared by
  all assistant entry points.
- Document chips are chat-scoped and restored after reload.
- While routing, show a transient local status such as “Checking request.” Do
  not persist this label as a message.

Opening a chat restores the last read position when practical. New messages
auto-scroll only when the user is already near the bottom; never pull the user
away while they are reading an older run.

### Empty state

Replace the large launch form with shortcuts:

- Full audit
- Planning
- Data analysis
- Document testing
- Report

Each shortcut submits a visible user action message with `intent: act` and the
corresponding goal template. The transcript must show what was requested; the
shortcut must not start an invisible side path.

### Chat history

The title menu opens a compact history panel with New chat, Rename, and Delete.
Remember the active chat ID per workspace in local storage. On load, use the
remembered chat if it still exists, otherwise the most recently updated chat,
otherwise create a new chat.

Deleting a chat requires confirmation and explains that runs and workspace
artifacts remain. If a linked run is active, the global active-run card remains
visible in the drawer until terminal so controls and pending attention are not
orphaned; it is no longer part of chat history.

### Message rendering

- User and assistant text render as ordinary bubbles.
- Citations render on the assistant answer and open the existing evidence
  anchor dialog.
- Tool traces are collapsed by default.
- Artifacts render below their answer using the existing chart, table, code,
  save-analysis, and pin controls.
- Errors are durable inline responses with a Retry action.
- Planning interview questions and their answers render as normal bubbles.
- Approvals, choices, conflicts, and confirmations render as typed attention
  cards at their chronological position.

### Inline run cards

Every action outcome renders a compact card containing:

- run/command title and mode;
- status and current activity;
- completed/total/failed/blocked counts;
- queued position or pending-attention indicator; and
- final one-line outcome.

Only one run card needs to be expanded at a time. Expansion fetches the full
run and reuses `AgentActionList`, `AgentTaskList`, `AgentApprovalCard`,
`AgentInteractionCard`, and `AgentSummary`. Controls remain pause, resume,
cancel, and the applicable interaction actions. Warnings, artifact references,
logs/traces, and final summary live inside the expanded card instead of in
separate drawer sections.

The active workspace run can belong to a different chat. In that case the
header shows that work is active elsewhere; read-only Q&A still works and new
actions are visibly queued against that run.

## Frontend State

Add `useAssistantChat.ts`, using the same module-scope-per-workspace pattern as
`useAgentRun.ts`. It owns:

- chat summaries and active chat ID;
- the hydrated active transcript;
- composer busy/error state;
- chat-scoped document attachments;
- create, switch, rename, delete, send, artifact edit, and artifact rerun; and
- refresh scheduling after run events.

Keep `useAgentRun.ts` as the execution/SSE store, but change it from “one
selected run is the drawer” to:

- the workspace's active run summary;
- full run details keyed by run ID;
- one expanded run ID;
- the one live SSE connection permitted by current concurrency; and
- run controls and workspace-change notifications.

Replace `AgentChat.vue` and the sectioned body of `AgentDrawer.vue` with small
components such as `ChatTranscript`, `ChatComposer`, `ChatHistoryPanel`,
`ChatRunCard`, and `ChatArtifactCard`. Reuse existing specialized run and
artifact components rather than duplicating their behavior.

Replace `useAssistantContext.ts`; document selection becomes the active chat's
persisted composer context. “Add to assistant” from `DocumentsTab.vue` creates
or reuses the active chat, attaches the document, opens the drawer, and does
not itself send document text to a model.

## External Entry Points

All assistant-triggering UI paths must create or reuse the active chat and call
the unified message endpoint:

- empty-state goal shortcuts;
- Planning “Generate planning drafts”;
- document-test prepare/run actions;
- post-import planning offer;
- folder intake classification/apply; and
- future report editorial actions.

Use `intent: act`, an explicit `source`, and a human-readable message. Folder
intake may still use its specialized runner, but the run must receive
`chat_id/source_message_id` and appear inline. Manual non-assistant buttons
continue to call their direct domain APIs.

## Security and Privacy Review

- Validate chat, message, artifact, run, and document identifiers before path
  access; never concatenate unchecked IDs into filesystem paths.
- Chat deletion must resolve the target and verify it remains below the
  workspace's `AssistantChats` directory before recursive removal.
- Persisted chat content is local engagement data and is removed with the
  workspace.
- The classifier sees user text and bounded eligible assistant text only.
- Q&A history contains text only and is filtered by document disclosure scope.
- Artifact frames, stdout, code execution results, tool responses, run
  sidecars, exception rows, and run logs are never added to model history.
- Editable code continues through the AST-guarded Polars sandbox.
- The command orchestrator, not the classifier or chat service, authorizes
  mutations.
- Document disclosures continue to write the existing disclosure and AI
  activity logs for each request; conversational reuse must also create an
  activity record describing the reused document IDs and hashes.

## Implementation Sequence

### Phase 1: Durable chat and Q&A vertical slice

- Add chat storage, validation, locking, atomic writes, summaries, and CRUD.
- Add message idempotency and interrupted-pending recovery.
- Persist current Q&A answers, citations, traces, and artifact sidecars.
- Add bounded eligible conversation history to `assistant.ask`.
- Add backend tests for privacy boundaries before enabling history in the UI.

Acceptance: two consecutive questions can use safe context and survive a page
reload; no run behavior changes yet.

### Phase 2: Unified message routing and run linkage

- Add explicit intents, deterministic routing, constrained classification, and
  clarification outcomes.
- Add `chat_id/source_message_id` to new runs, summaries, and queued commands.
- Route actions through schema-v2 runs and active-run command queuing.
- Project run interviews/interactions into chat reads.
- Handle parent completion and queued child discovery.

Acceptance: one endpoint safely answers a question, starts an action, queues a
follow-up, or asks for clarification with durable outcomes.

### Phase 3: Transcript-first drawer

- Add `useAssistantChat.ts` and adapt `useAgentRun.ts` for inline cards.
- Replace the split drawer layout with header/history, transcript, and composer.
- Add empty-state shortcuts and compact mode setting.
- Render run cards, attention cards, summaries, citations, and artifacts.
- Preserve scroll position and narrow/mobile drawer behavior.

Acceptance: refresh and chat switching reproduce the same visible transcript,
and all active-run controls remain usable.

### Phase 4: Artifact editing and entry-point migration

- Add optimistic artifact edits and server-owned rerun persistence.
- Preserve save-analysis and pin behavior against restored artifacts.
- Migrate planning, document testing, post-import, document attachment, and
  folder-intake assistant entry points.
- Stop new drawer code from calling legacy assistant/run-creation paths.

Acceptance: tab-triggered work appears in the active transcript and edited
Polars/rerun output remains after reload.

### Phase 5: Hardening and cleanup

- Exercise corrupted chat files, stale artifact revisions, duplicate sends,
  process interruption, run-start races, chat deletion, and missing documents.
- Verify active legacy-run visibility during upgrade.
- Add performance fixtures for long chats and many completed run cards.
- Remove obsolete frontend split-turn state and run-history UI.
- Retain backend compatibility endpoints until a later explicit removal.

## Test Plan

### Storage and API

- Chat CRUD, title generation, manual-title preservation, sort order, and
  workspace isolation
- Invalid/path-traversal IDs and safe recursive deletion
- Duplicate `request_id` handling before, during, and after completion
- Pending-message recovery after simulated restart
- Artifact sidecar hydration, revision conflicts, and missing/corrupt sidecars
- Chat deletion leaves runs and workspace artifacts unchanged

### Routing and execution

- Clear questions, clear actions, explicit intent overrides, ambiguous prompts,
  malformed classifier JSON, and low-confidence action output
- No mutation or run start on clarification
- Auto and Ask-before-changes behavior under existing action policies
- Active schema-v2 queueing, queued child linkage, and cross-chat action queueing
- Active legacy-run action refusal without blocking ordinary Q&A
- Pending free-text interviews consume the next message before classification
- Typed approvals and choices cannot be accidentally answered by ordinary text
- Run-start race falls back safely without duplicate runs

### Context and privacy

- Follow-up Q&A receives only bounded prior text
- Current question is never truncated by history budgeting
- Raw rows, artifact frames, stdout, tool results, code results, run messages,
  and sidecars never reach `llm.chat`
- Document history is included only for exact document IDs, source versions,
  masking mode, and enabled disclosure
- History stops at a disclosure-scope boundary and never includes half a turn
- Deleted/replaced documents make prior citations unavailable and prior text
  ineligible
- Classifier prompt obeys its narrower input contract
- Assistant/agent profile combinations enable only their supported operations

### Frontend

- Reload, chat switching, new/rename/delete, and remembered active chat
- One-send composer behavior, Enter/Shift+Enter, disabled/busy states, and retry
- Near-bottom auto-scroll versus preserved reading position
- Inline run progress follows SSE and the correct card refreshes
- Parent completion discovers and subscribes to the queued child run
- Pending approvals/interactions remain prominent and actionable
- Artifact code edit, rerun, save-analysis, and pin after reload
- Document attachment persistence and missing-document warnings
- Tab-triggered and intake runs appear in the correct chat
- Narrow drawer and mobile layout

Run the full backend suite and `npm run build`. Add at least one end-to-end
manual scenario that mixes Q&A, a document-grounded follow-up, an action in
permission mode, an approval, a queued command, and a restored artifact.

## Release Acceptance Criteria

- Ordinary Q&A and its artifacts survive refresh and chat switching.
- Natural follow-up questions work within a tested bounded context window.
- Document-derived history never crosses its disclosure scope.
- The composer has one primary send action and ambiguous text cannot mutate the
  workspace.
- Q&A and action availability reflect their separate provider profiles without
  unnecessarily disabling the other capability.
- Clear actions start or queue schema-v2 commands under existing safeguards.
- Every new run started by assistant UI has chat/message provenance and renders
  inline.
- Run cards expose progress, attention, controls, warnings, and final outcome
  without a separate run-history mode.
- Planning questions and responses appear chronologically without duplicate
  internal-message rendering.
- Edited Polars and rerun results restore exactly as last persisted locally.
- Deleting a chat removes only its transcript/artifact sidecars.
- Existing run files remain readable and are not imported or rewritten.
- Backend tests pass and the frontend TypeScript/Vite build succeeds.

## Resolved Assumptions

- The initial chat list is empty even when legacy runs exist.
- Only one agent run may actively mutate a workspace, matching the runner.
- Chats store auditor prompts and locally displayed aggregate/preview results;
  they remain engagement data under the workspace directory.
- Active chat selection is a browser preference, not shared workspace state.
- Chat history is not an audit evidence ledger. Formal evidence continues to
  use typed anchors, findings, working papers, reports, disclosure logs, and
  run records.
