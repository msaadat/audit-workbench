# Agent-Driven Workspace Plan

> **Status (2026-07-12): implemented** on the `agent-mode` branch. Backend:
> `backend/app/agent/` (durable runs, staged runner, approvals, SSE) +
> `routes/agent_routes.py`. Frontend: `composables/useAgentRun.ts` +
> `components/agent/` (drawer, task list, approval cards, chat, summary).
> The standalone Ask AI flow was removed in the same change. Deviations from
> this draft: the LLM proposes work as structured JSON per stage and the
> orchestrator executes mutations (rather than exposing mutation tools to the
> model); queries/charts persist only as dashboard tiles (no separate saved
> query store); V1 summary lives on the run record.

## Vision

The workbench will support an optional LLM-backed data analyst agent that can
populate an entire audit workspace after the user uploads one or more files.
The agent will infer the likely audit domain and dataset roles, build relevant
workspace content, keep the user informed through visible tasks, and produce an
evidence-linked summary.

The user may optionally provide an audit objective, business context, period,
materiality, known risks, or table-role hints. Missing context is inferred from
the uploaded files and recorded as assumptions or limitations.

The agent will support two operating modes:

- **Auto mode:** executes validated agent actions without approval.
- **Permission mode:** performs read-only discovery automatically, but asks the
  user to review inferred joins, validation rules, and suggested tests before
  applying them.

The agent functionality will be composable inside this project so it can later
become part of a larger, unified audit-agent workbench without requiring a
formal handoff protocol between separate applications.

## Product Model

- The user uploads files, optionally supplies context, selects an operating
  mode, and clicks **Run Agent**.
- One agent run may populate the entire workspace: joins, validation rules,
  analyses, queries, pivots, charts, dashboard tiles, and a final summary.
- Uploaded source tables remain immutable.
- Agent-created items carry provenance and reconcile cleanly on reruns.
- Only one run may actively mutate a workspace at a time.
- Messages sent during a run steer that run or add tasks to its plan.
- Messages sent after completion create a linked follow-up run, preserving the
  completed run as an immutable record.

## Architecture

### Durable Agent Runs

Add a durable `AgentRun` domain model stored separately from the main workspace
definition, for example under:

```text
Workspaces/<workspace-id>/AgentRuns/<run-id>/
```

A run records:

- Run ID, workspace ID, parent run ID, mode, and optional context
- Status, timestamps, execution limits, and interruption details
- Inferred domain, table roles, assumptions, and confidence levels
- Adaptive task plan and task state transitions
- Conversation messages and steering instructions
- Approval proposals, decisions, and edited specifications
- Model disclosure records
- Tool calls, outcomes, errors, and retries
- Created or updated workspace artifact references
- Final structured findings and human-readable summary

Persist state after every meaningful transition. Writes should be atomic so an
unexpected process exit cannot corrupt a run record.

### Run State Machine

```text
queued -> discovering -> planning -> executing
       -> awaiting_approval -> executing
       -> verifying -> summarizing -> completed

Any active state -> paused | failed | cancelled
```

Tasks use states such as `queued`, `running`, `awaiting_approval`, `completed`,
`skipped`, and `failed`.

### Runner and Events

- Use a lightweight in-process durable runner to preserve the portable-zip
  deployment model.
- Permit one active run per workspace.
- Apply a small configurable global concurrency limit, defaulting to one, for
  LLM-heavy runs.
- Stream replayable run and workspace events to the frontend with server-sent
  events (SSE).
- Support pause, resume, cancel, retry, and recovery after backend restart.
- Do not introduce Redis, Celery, or another external service dependency.

### Typed Tool Registry

Refactor the existing assistant capabilities into a typed tool registry that
separates local computation from workspace mutation. Tools should validate all
arguments before execution and declare their model disclosure behavior.

Read and compute tools include:

- List and describe tables
- Profile schemas and columns
- Infer table roles and candidate relationships
- Evaluate join keys, cardinality, match rates, and row-count effects
- Run aggregate queries and cross-tabs
- Run library analytics
- Execute AST-guarded Polars
- Preview and verify validation-rule effects

Mutation tools include:

- Create validated joins
- Save or update validation rulesets
- Save library and custom analyses
- Save queries, pivots, and chart specifications
- Pin or reconcile dashboard tiles
- Save the final analyst summary

The orchestration layer, not the LLM, decides whether a mutation may execute or
must wait for approval.

## Adaptive Task Plan

Use a deterministic stage skeleton to guarantee minimum coverage. After
discovery, the LLM creates dataset-specific tasks within those stages and may
add follow-up tasks when findings warrant deeper analysis.

1. Inspect uploaded tables and profiles.
2. Infer business domain, table roles, measures, dates, identifiers, and likely
   relationships.
3. Validate and create high-confidence joins.
4. Generate deterministic and LLM-advised validation rules.
5. Select relevant library analytics and propose custom Polars tests.
6. Build informative queries, pivots, and charts.
7. Curate dashboard items.
8. Verify significant results and reconcile populations.
9. Generate an evidence-linked final summary.

Auto mode starts execution after displaying the initial plan. Permission mode
uses the same plan but pauses at the agreed approval checkpoints. Users can
pause, cancel, redirect, or add instructions while the plan is running.

## Domain and Join Inference

The agent should infer likely domains such as sales, purchasing, payroll, or
payments from file names, table names, column names, datatypes, ranges,
low-cardinality labels, and aggregate profiles.

When multiple tables exist:

- Analyze each table independently first.
- Identify candidate keys and relationships.
- Test datatype compatibility, uniqueness, nulls, cardinality, match rates,
  unmatched populations, and row multiplication.
- Create a join automatically in auto mode only when evidence is strong.
- In permission mode, show the proposed tables, keys, join type, inferred
  relationship, match metrics, and expected row-count effect before creation.
- If confidence is insufficient, do not join. Record a warning and continue
  with independent analyses.

## Approval Policy

Read-only schema inspection, profiling, aggregate exploration, and draft
generation do not require approval.

Permission mode requires editable approval batches for:

- Inferred joins
- Validation rules
- Suggested library tests
- Suggested custom Polars tests

Each approval proposal should show:

- Rationale and risk addressed
- Exact configuration or code
- Tables and columns affected
- Expected output or impact
- Supporting inference and confidence
- Data disclosed to the model

The user can approve, reject, or edit a proposal. Closely related rules or
tests should be grouped into coherent batches rather than prompting for every
individual item.

Dashboard composition may proceed automatically from approved results because
tiles are reversible and do not affect analytical populations.

## Validation Boundary

For V1, the validation grid remains restricted to supported, typed validation
rule definitions:

- Deterministic inference and the LLM may propose configurations for existing
  rule types.
- Every rule must pass schema validation and a local preflight before saving.
- Unsupported or complex checks become editable sandboxed Polars analyses.
- New reusable validation rule types should be added deliberately to the
  registry with backend tests and UI metadata.

The final summary distinguishes standard validation results from exploratory
custom analyses.

## Privacy and Safety

Preserve the current metadata-only LLM boundary:

- The model may see filenames, table names, column names and types, row counts,
  null and distinct counts, ranges, aggregate results, and low-cardinality
  category labels.
- Local tools may inspect complete raw data to calculate profiles, joins,
  validations, and analyses.
- Raw rows, detailed exception listings, free-text transaction descriptions,
  and customer records never enter model context.
- The local browser may show complete results to the user.
- Each task records what workspace-derived information was disclosed.

Additional controls:

- Validate all tool arguments against typed schemas.
- Keep arbitrary computation inside the existing AST-guarded Polars sandbox.
- Apply fixed V1 limits for total runtime, LLM turns, task count, retries, and
  custom analyses.
- Detect repeated plans and tool loops.
- Use per-task timeouts and cancellation checks.
- On limit exhaustion, retain partial results and generate a resumable partial
  summary.

V1 will not expose user-selectable depth or cost profiles. Sensible limits will
be applied by default and tuned through testing.

## Source Data and Lineage

- Uploaded base tables are immutable.
- The agent may persist validated named joins.
- Filters, derived columns, normalization, and transformations remain inside
  saved query or Polars analysis specifications.
- The agent does not overwrite values, delete source rows, or create cleaned
  replacement datasets.
- Data-quality corrections appear as recommendations in the final summary.
- A future version may add explicit lineage-tracked derived tables.

## Provenance and Reruns

Every agent-created join, rule, analysis, query, and dashboard tile should
include:

- `agent_run_id`
- Creator and creation timestamp
- Stable semantic identifier
- Source rationale
- Relevant task and evidence references

Rerun behavior:

- Never overwrite or delete user-created work.
- Reconcile previous agent-owned outputs instead of creating duplicates.
- Update or replace agent-owned items when their underlying specification has
  materially changed.
- Treat manually edited agent outputs as user-owned.
- Flag obsolete agent outputs for review or cleanup.
- Keep completed runs immutable and link follow-up runs to their predecessors.

## Frontend Experience

### Workspace Controls

Add the following to the workspace header:

- **Run Agent**
- Auto/Permission segmented mode control
- Pause/resume control
- Cancel control
- Agent drawer toggle and current run status

The launch interaction accepts optional context but remains genuinely
one-click when no context is provided.

### Persistent Agent Drawer

Add a persistent right-side drawer available while the user moves among tabs.
It contains:

- Conversation and steering composer
- Adaptive task list and current activity
- Approval cards
- Assumptions, confidence indicators, and warnings
- Tool and evidence trace at an appropriate level of detail
- Run history
- Final and partial summaries

The standalone **Ask AI/Ask LLM** flow will be removed. Conversational
analysis, editable Polars code, artifacts, save, rerun, and pin actions will be
integrated into the Agent drawer. Existing manual Library and Code workflows
remain available.

### Live Workspace Updates

Emit typed workspace-change events when the agent creates or updates joins,
rules, analyses, queries, or dashboard tiles. Relevant tabs should refresh
without a full-page reload, briefly highlight changed items, and provide an
**Open** action from the corresponding task event.

Successful earlier tasks remain visible if a later task fails. The run can
retry, skip, or continue from the failure rather than rolling back unrelated
completed work.

## Final Summary

Produce both a saved human-readable analyst summary and structured findings
usable by other modules in a future unified audit-agent workbench.

The summary includes:

- Inferred audit domain and scope
- Tables analyzed and relationships used
- User context and agent assumptions
- Data limitations and unresolved ambiguities
- Join evidence and population reconciliation
- Validation outcomes
- Significant analytical findings and risk ratings
- Supporting artifact identifiers
- Unresolved questions
- Recommended audit follow-ups

Every substantive claim links to a saved validation result, analysis, query,
chart, or dashboard tile. The summary clearly distinguishes observed facts
from LLM interpretation. It reports analytical findings for auditor review and
does not issue an audit opinion or assurance conclusion.

## Implementation Sequence

### Phase 1: Durable Execution Foundation

- Add agent run, task, event, message, and approval models.
- Add atomic run persistence and state transitions.
- Add the in-process runner, concurrency guard, pause/cancel support, and SSE.
- Add restart recovery and replayable event cursors.

### Phase 2: First Vertical Slice

- Add Run Agent and the persistent Agent drawer.
- Discover tables and infer a domain.
- Generate and display an adaptive task plan.
- Propose validation rules.
- Support permission-mode review and auto-mode application.
- Persist task provenance and create a basic final summary.

This phase establishes one complete path from uploaded data to visible,
approved workspace content.

### Phase 3: Joins and Validation Coverage

- Add candidate relationship inference and local join diagnostics.
- Add auto-mode join creation and permission-mode join approval.
- Add deterministic rule recommendation logic.
- Add rule preflight, batching, editing, and verification.

### Phase 4: Analyses and Visual Exploration

- Integrate the analytics registry with agent task planning.
- Add custom Polars proposal and execution flows.
- Add permission checkpoints for suggested tests.
- Create saved queries, cross-tabs, charts, and analyses with provenance.

### Phase 5: Dashboard and Summary

- Add dashboard curation and duplicate prevention.
- Verify material findings and reconcile reported populations.
- Generate evidence-linked human and structured summaries.
- Add linked follow-up runs and rerun reconciliation.
- Remove the old standalone Ask LLM interface.

### Phase 6: Hardening

- Test privacy disclosures and raw-row withholding.
- Test interruption, restart, retry, cancellation, and SSE reconnection.
- Test reruns against mixed user-owned and agent-owned workspace content.
- Test join cardinality and row-multiplication safeguards.
- Add end-to-end fixtures for sales, payments, purchasing, and ambiguous data.
- Tune default execution limits and dashboard curation behavior.

## V1 Acceptance Criteria

- A user can upload files and start an auto or permission-mode run without
  providing context.
- The agent infers and displays a likely domain, assumptions, and task plan.
- Tasks and corresponding tabs update live.
- Auto mode can create validated joins, rules, analyses, queries, charts, and
  dashboard tiles.
- Permission mode gates joins, rules, and suggested tests with editable review.
- The agent never mutates uploaded source tables.
- No raw rows enter LLM context.
- Runs survive refreshes and expose interruption recovery.
- Reruns preserve user work and do not create duplicate agent outputs.
- The final summary links findings to persistent evidence.
- The existing standalone Ask LLM flow is fully replaced by the Agent drawer.
