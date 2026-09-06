# The Audit Workflow Graph

What each stage of an audit run actually does: what it waits for, what it is
shown, what it asks a model, what it writes back, and what it costs.

This is a reference for the **executable** audit lifecycle as the code declares
it today — `audit_workflow_v3`, 30 capabilities. It is derived from
`backend/app/agent/workflows/audit.py`, the grouped capability declarations
under `backend/app/agent/capabilities/`, the context presets in
`backend/app/agent/context/presets.py`, and the execution bindings in
`backend/app/agent/audit_execution.py`.

It is the **stage-level** companion to [agent-architecture.md](agent-architecture.md),
which states the contracts and boundaries the framework holds to. That document
gives each declared graph's structure; this one gives the audit graph's
behaviour, one capability at a time. Where the two disagree, the code wins.

- [1. Vocabulary](#1-vocabulary)
- [2. The graph](#2-the-graph)
- [3. From a request to a plan](#3-from-a-request-to-a-plan)
- [4. Scheduling](#4-scheduling)
- [5. What one unit does — the LLM call mechanism](#5-what-one-unit-does--the-llm-call-mechanism)
- [6. The provider call](#6-the-provider-call)
- [7. Context: what a stage is shown](#7-context-what-a-stage-is-shown)
- [8. Stage reference](#8-stage-reference)
- [9. Budgets](#9-budgets)
- [10. Approvals, gates, and modes](#10-approvals-gates-and-modes)
- [11. What lands on disk](#11-what-lands-on-disk)
- [12. Where to change what](#12-where-to-change-what)

---

## 1. Vocabulary

| Term | Meaning | Declared in |
| --- | --- | --- |
| **Capability** | One outcome the workflow can bring about (`planning.apm_ready`). Carries its dependencies, a readiness function, a unit expansion, a context declaration, a barrier, and an invalidation key. | `capabilities/*.py` |
| **Stage** | One capability's slot in a materialized run. Holds its units, its status, and the readiness snapshot taken before it ran. | `workflow.materialize` |
| **Unit** | The smallest thing that runs: one RCM row's tests, one document's category, one Q&A item against one document. Unit IDs are *semantic* (`test_generation:dt-014`), so re-expanding after a resume yields the same work. | `workflow.UnitSpec` |
| **Readiness** | A deterministic, model-free verdict on whether a capability's outcome already exists and is usable: `satisfied`, `missing`, `stale`, `blocked`, `review_required`. Existence and structural usability, plus — where the artifact carries a `workflow_parents` stamp — whether a declared parent has moved since it was committed. `stale` schedules work. | `workflow.Readiness` |
| **Barrier** | How a stage's units may run. `all_settled_then_validate` (default) runs them one at a time; `all_settled_parallel` fans them out. | `workflow.BARRIERS` |
| **Binding** | How a capability's units execute: a **pipeline binder** (context → worker → proposal → executor) or a **deterministic executor** (local computation, no model). Exactly one per capability. | `runtime.CapabilityExecution` |
| **Worker** | The only thing that talks to a model. Hash-identified by its prompt, response schema, and repair policy. Cannot reach a workspace, transaction, or run store. | `workers/model.py` |
| **Executor** | The only thing that mutates the workspace. Hash-identified by its id and concurrency mode. Cannot reach a worker or the gateway. | `executors/model.py` |

Two invariants hold the shape in place, and both have durable tests
(`test_agent_final_boundaries.py`):

- A workflow definition imports only graph primitives. Capabilities never
  schedule or persist. Workers never see a workspace. Executors never see a
  model. Context never calls a provider.
- There is exactly one provider call site in the whole agent:
  `runtime/model_gateway.py`.

---

## 2. The graph

`workflows/audit.py:DEPENDENCIES` is the single source of truth for the edges.
Capability modules attach behaviour to these IDs; they never restate an edge.

```text
                       sources.imported ───────────────┐
                              │                        │
  documents.text_ready        │                        │
        ├──► documents.categorized ──► documents.types_classified
        │            │                          │      │
        │            │                  documents.evidence_read
        │            │                          │
        │            │                  documents.schemas_stamped ──┐
        │            │                                              │
        └──► documents.analysis_chunks_ready                        │
                     │                                              │
             documents.analysis_generated ───┐                      │
                                             │                      │
  data.relationships_inferred                │                      │
        └──► data.join_utility_ready         │                      │
                 └──► data.joins_ready       │                      │
                          └──► analysis.register_ready              │
                                   └──► analysis.definitions_ready  │
                                            └──► analysis.executed  │
                                                     └──► analysis.summarized
                                             │                      │
                              planning.context_ready ◄──────────────┘ (sources +
                                             │                         generated
                                  planning.apm_ready                   analyses)
                                       │           │
                          planning.cycle_ready ◄───┤ (+ sources.imported,
                                       │           │   documents.types_classified)
                                       ▼           ▼
                                  planning.rcm_ready
                                    (+ documents.categorized,
                                       documents.types_classified)
                                       │
                     tests.cycle_ruleset_proposed
                        (+ planning.cycle_ready,
                           documents.schemas_stamped)
                                       │
                     tests.cycle_ruleset_approved   ← auditor gate in permission mode
                                       │
                                 tests.specified  (+ planning.rcm_ready)
                                       │
                          tests.promoted_from_analysis
                                       │
                              fieldwork.executed
                                       │
                                results.rolled_up
                       ┌───────────────┼───────────────┐
                       ▼               ▼               ▼
             findings.drafted   working_papers.   report.working_draft
                       │          generated        (+ planning.apm_ready)
                       └──────────────►│◄───────────────┘
                                       ▼
                                 audit.verified

  planning.change_assessed   (← apm_ready, rcm_ready; only ever asked for by name — §8)
```

The parallel branches after `results.rolled_up` are intentional; the graph is a
DAG, not a chain.

### Notable edges, and why they are there

- **`sources.imported` is the head, and the agent can never perform it.** It
  expands no units and resolves no worker; it exists so an engagement holding
  nothing reports planning as *waiting* rather than offering to write a
  memorandum about nothing.
- **`planning.context_ready → documents.analysis_generated`** grounds planning
  in generated document analyses rather than raw text. It is *not* a universal
  prerequisite: with no planning-relevant document in scope, every document
  capability's readiness is satisfied, no unit expands, and the audit runs
  unchanged.
- **`planning.cycle_ready` sits in front of the matrix, not behind the
  schemas.** The cycle shape is read out of the memorandum alone, so it costs no
  extraction; its step names become the vocabulary a matrix row's `process` is
  chosen from.
- **There is no schema edge into `planning.rcm_ready`.** A matrix row says a
  requirement needs linked source records and stops; *which* fields must agree
  is decided downstream by the cycle ruleset, where the induced schemas are in
  hand. That keeps a re-derived schema from invalidating the whole matrix.
- **`tests.specified` does not depend on `analysis.executed`**, and
  `planning.apm_ready` does not depend on the analysis branch. Making either an
  edge would drag the whole exploratory branch into every request that reaches
  fieldwork. Ordering is handled by declaration order in the registry instead.
- **Dashboard curation is not on the graph.** Arranging tiles changes how an
  engagement is read, not what it establishes; nothing downstream ever consumed
  it.
- **`planning.change_assessed` depends on the memorandum and the matrix**, and
  for most of this graph's life it could not. Materialization used to schedule
  a satisfied capability whenever a `depends_on` neighbour was scheduled, so
  naming the memorandum would have rewritten it before answering whether it
  needed rewriting; the capability declared no edges at all to escape that.
  Now that only a moved `invalidate_on` parent causes rework (§3 step 2), the
  edges say what they always meant: an assessment cannot run before the things
  it assesses exist, and a request for one on an unplanned engagement plans it
  first instead of reporting itself blocked and stopping. It is on no template
  and outside `FULL_AUDIT_OUTCOMES`; the steering loop requests it by name.

### Outcome sets

`workflows/audit.py:TEMPLATE_OUTCOMES` maps a goal template to the outcomes a
request asks for. The transitive closure of those outcomes is the plan.

| Template | Requested outcomes |
| --- | --- |
| `full_audit_working_draft` | `analysis.summarized`, `findings.drafted`, `working_papers.generated`, `report.working_draft`, `audit.verified` |
| `planning` | `planning.apm_ready`, `planning.rcm_ready`, `tests.specified` |
| `apm_only` | `planning.apm_ready` |
| `rcm_only` | `planning.rcm_ready` |
| `finding_draft` | `findings.drafted` |
| `document_test_preparation` | `tests.specified` |
| `report` | `report.working_draft`, `audit.verified` |

Running one named Document Test is deliberately absent: that is a request
against the standalone `doc_tests_workflow_v2` graph, which reaches the same
units through the same binder.

### Sibling graphs

Three other graphs run on the same scheduler and share declarations with this
one:

| Workflow | Chain |
| --- | --- |
| `analysis_workflow_v1` | `data.relationships_inferred` → `data.join_utility_ready` → `data.joins_ready`; `analysis.register_ready` → `analysis.definitions_ready` → `analysis.inputs_ready` → `analysis.executed` → `analysis.summarized` |
| `documents_workflow_v1` | `documents.text_ready` → `categorized` → `types_classified` → `evidence_read` → `schemas_stamped`; `analysis_chunks_ready` → `analysis_generated` → `analysis_reviewed` |
| `doc_tests_workflow_v2` | `doc_tests.definitions_ready` → `doc_tests.executed` → `doc_tests.dispositioned` |

The audit graph composes the document capabilities through
`CapabilityGroupView` (generation only — never auditor review) and the analysis
capabilities through `AuditAnalysisGroup` (everything except
`analysis.inputs_ready`, and with the audit graph's own edges substituted).
There is one implementation of each; the audit graph reuses it rather than
restating it.

Note the one deliberate edge difference: standalone analysis hangs
`analysis.register_ready` off `data.relationships_inferred`, while the audit
graph hangs it off `data.joins_ready`.

---

## 3. From a request to a plan

```text
assistant_chats._process_message
  └─ act intent
     └─ runner.start_command_run       one live run per workspace (AgentBusyError)
        ├─ store.new_command_run       creates run.json (store.new_run is intake-only)
        ├─ routing.resolve_route       classify ONCE; persist run["route"] + run["engine"]
        │    └─ routing.classify_command   pure, deterministic, no model turn:
        │         1. source == "loop"          -> the steering loop
        │         2. explicit requested_outcomes -> workflow
        │         3. a registered goal template  -> workflow
        │         4. a lifecycle phrase          -> workflow
        │         anything else — a sentence — -> the steering loop, which reads
        │                                         the workspace before deciding
        ├─ routing.install_resolution  materialize the graph, size the budgets
        └─ daemon thread ──► runner._run_engine ──► engine switch on run["engine"]
```

`resolve_route` always returns an engine. There is no pending route and no
router turn: the phrase tables that used to guess an outcome set from wording,
and the bounded router turn that guessed when they could not, decided nothing
across 45 recorded runs and are gone. A sentence is the loop's, and the loop
decides the same question with the workspace in front of it — and can ask.
`clarification` and `unsupported` routes are legacy only: nothing produces them
any more, and `finish_without_engine` exists to bring an already-persisted
record carrying one to a terminal status with a reply.

`routing.install_resolution` is where the plan comes from, and **it comes from
the registry, not from a model**:

1. Resolve the requested outcomes to a workflow definition
   (`workflow_for_outcomes` picks the *narrowest* registry that declares all of
   them).
2. `workflow.materialize(registry, workspace, outcomes, scope, generation_mode)`:
   - walk the transitive `depends_on` closure in topological order;
   - for each capability, run its deterministic `readiness()`;
   - under `reuse_existing`, **skip** any capability that is already satisfied
     and none of whose declared parents is being rewritten in this run,
     recording it in `reused_capabilities` with a `currency_status` of
     `current`, `unstamped`, or `not_assessed`;
   - otherwise call `expand_units()` and fan it into a stage, stamped with
     `scheduled_because`: `not_satisfied`, `stale`, `parent_rescheduled` (with
     the producers in `scheduled_because_refs`), or `forced`.

   **`depends_on` orders; `invalidate_on` invalidates.** They are different
   questions and the scheduler no longer conflates them. A capability's
   `invalidate_on` names the *bases* it reads; `workflows/audit.py:
   BASIS_PRODUCERS` maps each basis to the capabilities that write it; and a
   satisfied capability is redone only when one of those producers is scheduled
   in the same run, or when its own readiness returns `stale` — which it does by
   comparing the `workflow_parents` its executor stamped at commit against
   `parent_hashes` now. Depending is not reading: planning depends on the
   documents so it runs *after* them, and an imported document is a source
   rather than a parent, so it never restates the plan. Startup validation
   refuses an `invalidate_on` key absent from `BASIS_PRODUCERS` and a producer
   that is not a registered capability, so the field cannot go decorative again.
3. Reject the run if any stage exceeds `max_units_per_stage` (default 250).
   This raises inside `start_command_run` after the `queued` record has been
   saved and before the thread launches; `recover_orphans` later relabels that
   record `interrupted`.
4. Size the model budget from real counts (§9) and persist
   `run["workflow"]` with the definition id, definition hash, scope, resolved
   capabilities, `reused_capabilities`, a `state_at_resolution` readiness
   snapshot of every capability, stages, and a human-readable
   `workflow_explanation`. `WorkflowRunner.materialize` writes the same state
   under the same names; the split where the sibling-graph path said
   `reused_outcomes` and the audit route said `reused_capabilities` is gone.

`generation_mode` is `reuse_existing` unless the command says otherwise —
`workflow.command_generation_mode` reads `improve `, `regenerate`, `refresh `,
or `generate … again` out of the request text and returns `force`, which makes
materialization re-expand satisfied capabilities.

The scope resolved here (`target_refs`, `generation_mode`, `instruction`,
`tables`, `test_ids`, …) is durable on the run and is what every readiness and
expansion function reads. `_shared.target_scope` resolves `rcm:`, `datatest:`,
`doctest:`, `observation:`, `finding:`, and `document:` refs, and — importantly
— rolls anything below a row *up* to its row, so a stage that expands per row
still works when the auditor named one test.

---

## 4. Scheduling

`runtime/workflow_runner.py:WorkflowRunner` is domain-neutral: it receives the
capability registry, the execution bindings, a `RunRuntime`, and a
`UnitPipeline` by composition, and imports no audit module.

**Stages run strictly in dependency order.** Parallelism lives *inside* a stage.

For each stage, in materialized order:

1. `checkpoint()` — honours cancel/pause and the runtime deadline.
2. `_refresh()` — reload the workspace, project its revision on the run, and
   recompute the dynamic limits (§9). It also runs after `before_stage`, after
   every serialized unit, and after every stage.
3. `before_stage` — in permission mode, fire any scope checkpoint the stage
   declares (document scope, analysis scope); then the stage review, if the
   run asked for one.
4. Check dependencies. A dependency that was scheduled in this run and did not
   settle blocks the stage — **unless the edge is declared partial**.
5. `ensure_stage_units` — re-expand against the current workspace on every
   call (not only when the list is empty), merge by unit ID, refresh any unit
   still `queued`, and re-apply `max_units_per_stage`. A stage that grew past
   the cap mid-run fails the run here, not at routing.
6. **If there are no units, settle the stage from its own readiness alone**
   (`succeeded` if satisfied, else `blocked`). This is how an audit with no
   documents walks straight through the document capabilities, and how the
   permission-mode approval gate reports without acting.
7. Run the units through the capability's one binding.
8. `_refresh()`, fold the unit statuses into a stage status, emit
   `stage_summary`.

### Partial dependencies

`audit_execution._PARTIAL_DEPENDENCIES` lists edges that order work without
withholding it. The rule is: *a failure in the dependency must not destroy work
the dependent can still do.*

| Dependent | Partial on | Because |
| --- | --- | --- |
| `planning.context_ready` | `sources.imported`, `documents.analysis_generated` | An auditor may ask for a memorandum from a brief alone, before importing anything. |
| `planning.cycle_ready` | `sources.imported` | A step may legitimately have no imported population. |
| `tests.specified` | `tests.cycle_ruleset_approved` | Generation has always been able to proceed without a cycle — it writes document-question tests instead. Blocking here would withhold every test in the engagement, data tests included, to wait on an approval permission mode is not allowed to make. |
| `fieldwork.executed` | `tests.specified`, `tests.promoted_from_analysis` | One unsatisfiable promotion must not block every test that already exists. |
| `results.rolled_up` | `fieldwork.executed` | |
| `report.working_draft` | `findings.drafted` | |
| `audit.verified` | `working_papers.generated`, `report.working_draft` | |
| `documents.analysis_chunks_ready` | `documents.text_ready` only | One unextractable document must not withhold the others. The `documents.categorized` edge is **blocking**: a failed category unit withholds chunk analysis. |
| `documents.analysis_generated` | `documents.analysis_chunks_ready` | One unanalyzable document must not withhold the others. |
| `data.join_utility_ready` | `data.relationships_inferred` | Diagnosis is local and per pair. |
| `analysis.register_ready` / `definitions_ready` / `executed` / `summarized` | the previous analysis step | One procedure that would not execute must not withhold the memo. |

Deliberately **not** partial: `data.joins_ready` on `data.join_utility_ready`.
A pair whose utility gate never answered has nothing admitting it, and
materializing the join anyway would bypass the gate outright.

The table lives in `_PARTIAL_DEPENDENCIES`, apart from the edges in
`workflows/audit.py`; startup validation does not cross-check it, and only the
`tests.specified` entry has a test.

### Barriers

- **`all_settled_then_validate`** (the default, and every capability that
  commits): units run one at a time, and the workspace is reloaded between them
  so the next unit binds against what its predecessor committed. This is not a
  concession — for `documents.evidence_read` it *is* the mechanism, because a
  per-document read can only agree with its siblings about a vocabulary if it
  can see what they settled.
- **`all_settled_parallel`**: only for capabilities whose units are independent
  and commit nothing. Currently `tests.specified`,
  `tests.promoted_from_analysis`, and `documents.analysis_chunks_ready`.
  `workflow.stable_all_settled` fans them out under `max_llm_concurrency`,
  never fail-fast, and returns results in unit-ID order so the durable
  transcript is identical regardless of completion timing.

`tests.specified` is parallel for a hard reason: serialized, one unit per RCM
row, seventy turns at a minute each exhausts the run's deadline before the
stage completes.

### Recovery

`workflow.recovery` re-queues any unit left `running` by a crash. Because unit
IDs are semantic and proposals/receipts are sidecar-persisted, a resumed run
picks up at the next uncommitted unit without re-billing the provider.

---

## 5. What one unit does — the LLM call mechanism

`runtime/unit_pipeline.py:UnitPipeline.run` is the whole model-call lifecycle,
and it is the same for every pipeline-backed capability in every graph. The
binder supplies the domain parts; the pipeline owns the order.

```text
 1. context_provider()          resolve declared context → (ContextManifest, ContextBundle)
 2. persist_context_manifest    content-free manifest sidecar written BEFORE any model call
 3. build ProposalExecutionIdentity
       capability_definition_hash + unit_input_hash + context_manifest_hash
       + worker_definition_hash + model_profile_hash + input_modalities
       + prepared_media_hashes + media_policy_hash
 4. load_proposal(unit)         is there a persisted proposal with this exact identity?
       ├─ yes → reuse it. No provider call. No re-bill.
       └─ no  → 5
 5. workers.execute(...)        ──► ModelGateway.complete()   ← the only provider call
       ├─ response validated against the worker's response schema
       ├─ invalid → bounded repair: the response is quoted back with its
       │            validation errors (1 attempt for most workers, 2 for
       │            documents.evidence_read and tests.generate)
       └─ still invalid → persist a REJECTION sidecar and raise
 6. persist_proposal            exact-identity proposal sidecar, status "proposed"
 7. approval_provider(proposal) permission mode only; returns None → "approval_rejected"
       └─ accepted → proposal re-persisted with status "accepted"
 8. executor_id is None?        proposal-only unit — the proposal IS the durable
                                outcome (document chunk analyses, join utility,
                                the assertion-register reading turn, intake
                                classification). Return "proposed". No commit,
                                no receipt, and no readiness re-evaluation.
 9. executors.reconcile(...)    interrupted-commit check
       ├─ already_applied → synthesize a receipt, do not re-commit
       ├─ conflict        → raise UnitPipelineConflict
       └─ not_applied     → 10
10. executors.execute(...)      the ONE place the workspace is mutated
11. persist_receipt             hash-identified proof of the commit
12. readiness_provider()        re-evaluate the capability. Committed but not
                                satisfied → the unit fails.
```

Three properties matter for reading a run afterwards:

- **The manifest is written before the model call.** Whatever a turn was shown
  is recorded even if the turn crashed.
- **The proposal is written before approval or mutation.** A crash between
  generation and commit resumes from the sidecar. When any of the eight
  identity fields moves, the reuse is rejected with a named reason
  (`exact_context_changed`, `worker_definition_changed`,
  `unit_input_changed`, …) and the turn is paid for again — which is the point.
- **A rejected response is kept.** The final invalid response is persisted as a
  rejection sidecar and seeded back into an exact-identity retry, so the next
  attempt edits what the last one produced instead of starting over.
- **A fresh commit publishes a revision; a reconciled one need not.** Step 10
  mutates, so its receipt must show `workspace_revision_after >
  workspace_revision_before` — a receipt for a commit that left no trace is a
  receipt for nothing. Step 9 commits nothing: the work it describes already
  landed, possibly in an earlier run, so `before == after` is what happened and
  `reconciled: true` is how the receipt says so. Holding reconciliations to the
  execute-path rule failed twelve settled evidence readings on a forced run and
  blocked the schema stamp behind them.

### Repair, not retry

A repair happens *inside* one worker call. The unit's `attempts` counter stays
at 1 however many turns the response took; the worker's own count is recorded
on the proposal as `worker_attempts`. `WorkerAttempt` enforces the contract: the
first attempt may carry no guidance and no previous response; a repair attempt
must carry both.

Two details the lifecycle above elides:

- On the final attempt, a validator that can supply `error.partial` makes the
  worker return a *partial* result instead of raising. The pipeline persists it
  as an ordinary `proposed` proposal; the sidecar does not record that it was
  salvaged.
- `documents.schemas_stamped` commits through `UnitPipeline.commit_local`, which
  resolves no context and returns an empty `manifest_reference`, so that unit's
  record carries no `context_manifest`.

### Mixed-kind capabilities

`fieldwork.executed` is the one capability whose units are of several kinds, and
only one of them talks to a model. Its binder returns a `BoundUnitPipeline` for
document Q&A / LLM assessment / cycle vouching, and a `DeterministicUnitResult`
for data-test runs, deterministic document-test runs, and review units. That is
how a capability with mixed units still carries exactly one binding.

---

## 6. The provider call

`runtime/model_gateway.py:DefaultModelGateway.complete` is the only path from
the agent to a provider. Workers reach it through `BaseRunner._llm_content`;
the steering loop through `BaseRunner._llm_message`. A static test confines
direct provider calls *within `app/agent`* to this module; the report,
assistant, and document modules outside the agent package call `llm.chat` on
their own.

Per call it:

1. `checkpoint()` — cancel/pause/deadline.
2. Resolve the model profile (`text` or `vision`, selected by the worker's
   `required_model_capabilities`) and refuse if the configured profile lacks a
   required capability or is unconfigured.
3. Verify every prepared-media handle from the cache **before** reserving
   budget, so a preparation failure never spends model budget.
4. Estimate `request_characters`, `text_token_estimate` (chars / 4), and
   `image_token_estimate`, then **reserve turn and token budget through
   `RunRuntime` before calling**. An estimated overage never spends provider
   tokens.
5. Derive the `[agent:<stage>]` tag from the first line of the system prompt.
   This tag drives the UI stage label, per-worker accounting, streamed
   progress, and the "this is taking a while" heartbeat.
6. Hold a process-wide semaphore keyed on `provider:model`, capacity
   `AGENT_PROVIDER_MAX_CONCURRENCY` (default 4), shared by every run in the
   process.
7. Make the call inside a `debug_store.trace_context` carrying run id, stage,
   unit id, parent refs, document ids and artifact refs. The debug store
   records the **full** request and raw response in the workspace's
   `telemetry.db` (`llm_calls`), sanitised for secrets and image bodies only.
8. Reconcile actual token usage against the reservation, append **hash-only**
   provenance to the workspace's telemetry `activity_events`, and record the
   spend in the per-user usage ledger. `run.json` holds usage counters, not
   provenance.

An empty or unusable completion is retried exactly once *at this layer*: the
bounded repair loop corrects a response by quoting it back, and an empty
completion gives it nothing to quote. The retry is metered like any other turn
and is distinguishable in the debug console by carrying the same
`retry_number` with `retry_reason: "unusable"` (a repair carries a *higher*
`retry_number` and a different prompt). Two other retry layers exist around
it: `llm.chat` retries transport and rate-limit errors up to
`MAX_REQUEST_ATTEMPTS` (3) below the gateway, and the scheduler allows
`max_execution_attempts` (2) per unit above it.

### Registered workers

| Worker | Response | JSON | Repairs | Model caps | Semantic check |
| --- | --- | --- | --- | --- | --- |
| `planning.context` | `{context: {...}}` | yes | 1 | — | yes |
| `planning.apm` | Markdown memorandum | **no** | 1 | — | yes |
| `planning.cycle` | `{name, steps[], cross_cutting}` | yes | 1 | — | yes |
| `planning.rcm` | `{rows[], quarantined[]?}` | yes | 1 | — | yes |
| `planning.delta_review` | `{impact: none\|apm\|rcm\|both, summary, apm_changes[], rcm_changes[]}` | yes | 1 | — | yes |
| `tests.cycle_linkage` | `{roles[], join_keys[], assertions[]}` | yes | 1 | — | yes |
| `tests.generate` | `{tests[]}` | yes | **2** | — | yes |
| `fieldwork.document_qa` | `{answer, conclusion, control_conclusion, outcome, citations[]}` | yes | 1 | — | yes |
| `fieldwork.cycle_vouch` | `{cells[]: check_id, verdict, compared, reason}` | yes | 1 | — | yes |
| `reporting.finding` | Markdown draft (title / severity / narrative) | **no** | 1 | — | yes |
| `documents.category` | `{category, confidence, rationale}` | yes | 1 | — | no |
| `documents.classification` | document type assignment | yes | 1 | — | yes |
| `documents.evidence_read` | `{records[]: fields[], new_fields[]}` | yes | **2** | — | yes |
| `documents.analysis_chunk` | `{summary_markdown, audit_notes_markdown, citations[]}` | yes | 1 | — | yes |
| `documents.analysis_structured` | structured chunk analysis | yes | 1 | — | yes |
| `documents.analysis_visual_page` | page analysis | yes | 1 | **vision** | yes |
| `documents.analysis_reduction` | `{derived_text_markdown, summary_markdown, audit_notes_markdown}` | yes | 1 | — | yes |
| `analysis.join_utility` | join utility verdicts | yes | 1 | — | yes |
| `analysis.reading` | assertion register decisions | yes | 1 | — | yes |
| `analysis.definitions` | analysis specs | yes | 1 | — | yes |
| `analysis.summary` | EDA memo | yes | 1 | — | yes |
| `analysis.promotion` | RCM placement for a saved analysis | yes | 1 | — | yes |
| `intake.classification` | staged-file classification | yes | 1 | — | yes |

Two workers return Markdown rather than JSON, and both for the same measured
reason. The APM: constraining it to JSON produced a complete 16,000-character
memorandum filed under a key the model chose for itself, which then failed the
template check. The finding: a model that will not emit a newline inside a JSON
string delivers every section flattened onto one line, which parses as a single
heading with an empty body and fails every section check at once.

### Registered executors

All twenty-one audit-reachable executors use `parent_hashes` concurrency: they
guard the specific material parents they are about to overwrite and permit
unrelated workspace revisions to advance. (The alternative,
`workspace_revision`, is strict compare-and-swap; nothing in the audit graph
uses it.)

`planning.apm`, `planning.cycle`, `planning.context`, `planning.rcm`,
`planning.delta`, `tests.cycle_ruleset`, `tests.generate`, `fieldwork.document_qa`,
`fieldwork.cycle_vouch`, `reporting.finding`, `documents.category`,
`documents.classification`, `documents.read`, `documents.stamp`,
`documents.analysis`, `analysis.join`, `analysis.register`,
`analysis.definitions`, `analysis.execution`, `analysis.summary`,
`analysis.promotion`.

---

## 7. Context: what a stage is shown

Context is **declaration-only**. A capability names a registered preset; the
preset is authoritative. Auditor curation and explicit regeneration can change
which candidates are resolved *under* that policy, but cannot widen the policy.
Startup validation refuses a capability that names an unregistered preset.

```text
Capability.context ──► ContextPreset (presets.py)
                          ├─ sources[]: id, source_type, required, selector,
                          │             representations[], per-source budget
                          ├─ budget: max_items, max_characters (global ceiling)
                          └─ privacy: explicit per-content-class permissions

adapter scope fn ──► ContextScope (candidates per source id)
       │
       ▼
ContextResolver.resolve(workspace, capability, unit, scope)
       ├─ walk sources in DECLARATION ORDER
       ├─ required source with no candidates  → hard error
       ├─ optional source with no candidates  → omission record
       ├─ apply the selector (metadata | lexical | local embeddings — all local)
       ├─ apply per-source and global budgets; truncate or omit, and RECORD it
       └─ enforce privacy structurally
       │
       ├──► ContextManifest  content-free. Hashes, sizes, selections, omissions,
       │                     truncations, privacy decisions. Persisted.
       └──► ContextBundle    the actual content. Local-only. Never persisted,
                             never provenance.
```

The manifest/bundle split is the auditability boundary: the durable record says
*what* a turn was shown and how much of it, and never the words.

`execution_support.resolve_context` adds one thing on top: a source that had
candidates and admitted **none** of them raises a run warning
("had candidates but none fitted its budget, so the turn ran without it").
Degradation is acceptable; silent degradation is not — this is how a planning
turn came to describe populations it had never been shown.

### Context permissions

Named `ContextPrivacy` in code, and the name predates the reasoning. What these
permissions bound is not confidentiality but *what a turn is asked to compute
from*: a model handed 997 rows answers a counting question approximately,
expensively, and differently on the next run, where Polars answers it exactly
and repeatably — and what fits in the window is a truncated sample that reads
like a population. The default is to compute locally and send the result.

Permissions default to deny. A representation kind maps structurally to a
permission (`_REPRESENTATION_PRIVACY_FIELD`), so a declaration cannot admit
row-level content by renaming it.

`allow_table_rows` is denied everywhere and the model layer rejects the
`table_rows` representation before a bundle can reach a worker. Rows reach a
turn through exactly three narrower doors — each where the individual row *is*
the answer rather than an input to one, each its own permission, each capped:

| Permission | What it admits | Who declares it |
| --- | --- | --- |
| `allow_small_table_rows` | A whole table, only when the table is below the adapter's row-count ceiling — a 4-row approval matrix whose aggregate statistics cannot say what its one exceptional row contains. | `planning.rcm` |
| `allow_analysis_exception_rows` | The rows a saved exploratory procedure flagged, capped per procedure. | `analysis.summary` |
| `allow_datatest_exception_rows` | The rows a durable, RCM-linked Data Test flagged, capped by row count and serialized size in the adapter. | `reporting.finding_draft` |

The last two are separate on purpose, so widening one never silently widens the
other. Each projection reports what it left out: the Data Test projection
reports `rows_withheld`, so a truncated table cannot be drafted as a complete
population, and the analysis-exception projection reports `rows_supplied` and
`exception_count` and leaves the subtraction to the reader. That reporting,
rather than the cap alone, is the property worth having — a bounded sample
presented as the whole is worse than no sample.

**A column's value domain is the fourth door, row-*derived* rather than
row-level, and it rides under `allow_table_metadata`.** A generated Polars step is a predicate, so a turn
given names and dtypes alone has to guess what a status column holds — and a
wrong guess matches every row or none, both of which read as a control
conclusion. The complete value set is therefore supplied where it is a
*domain* rather than the rows restated. Three bounds decide that, and the third
was added after the first two were found not to hold:

- `MIN_CATEGORY_ROWS` (20) — the table has a population at all.
- `MIN_CATEGORY_REPETITION` (4) — each value recurs across it.
- `MAX_CATEGORY_LABEL_CHARACTERS` (32), applied only to values containing a
  space — the values read as labels rather than prose.

The first two were documented as making "an identifier, a name, or a free-text
field fail by construction", and they do not: free text written from a handful
of templates repeats often enough to pass. Measured across the engagements on
this machine, 551 columns disclosed their complete value set, among them a
treasury `DISCREPANCY_NOTE` whose three values are full exception sentences on
a 997-row table. Length is applied only to phrases because a long unbroken token
is a code, and codes are what the domain exists to supply —
`MM_PLACEMENT;MM_BORROWING;TBILL_PURCHASE` is forty characters and is exactly
the vocabulary a predicate has to name. The bound sits in a real gap rather than
on a tuned threshold: every genuine label domain measured tops out at 27
characters and the next value up is 44. It withholds 13 columns and keeps 538.

A domain is not a criterion, and the generation prompt says so, because the
turn that had `business_purpose: [..., "Ride to personal residence"]` filtered
on that literal — selecting the row it was shown rather than testing the
control.

`allow_document_schemas` is the deliberate middle term between those and
nothing. It admits the field names, roles and value types an induced schema
states — never a value any document printed — so a context permitted to see a
schema is not thereby permitted to see the text it was induced from. Two presets
declare it: `tests.cycle_linkage`, which writes the cycle rules against that
vocabulary, and `tests.generate`, which uses it in place of evidence-document
prose (see below). `allow_file_metadata` is used only by the intake sibling
graph.

The structural mapping constrains *declarations*. It is keyed on the
representation label an adapter attaches, and adapters choose labels: RCM
requirements travel as `planning_context`, observation, row, test, and
execution projections as `current_artifact` under `allow_document_text`. A
reviewer checking what a preset admits should read the adapter's scope
function as well as the preset.

### Preset inventory (audit graph)

Budgets below are `items / characters`.

| Preset | Global budget | Permissions | Sources |
| --- | --- | --- | --- |
| `planning.context` | 9 / 50k | planning_context, document_text | `current_planning_context` (opt, 1/10k), `planning_documents` (**req**, 8/40k, deterministic category rule, `summary` or `raw_pages`) |
| `planning.apm` | 47 / 96k | planning_context, template_text, document_text, table_metadata, table_profiles, analysis_summary, auditor_instruction | `planning_context` (req), `apm_template` (req), `current_apm` (opt, 32k), `analysis_summary` (opt, 24k), `population_summary` (opt), `table_metadata` (12/8k), `table_profiles` (12/16k), `documents` (lexical, 12/40k), `methodology` (lexical, 5/8k), `instruction` (opt) |
| `planning.cycle` | 26 / 86k | planning_context, document_text, table_metadata | `planning_context` (req), `current_apm` (**req, 60k**), `table_metadata` (24/16k). Names and shapes only — no document source is declared, so no evidence text can reach this turn. |
| `planning.rcm` | 255 / 138k | planning_context, template_text, document_text, table_metadata, table_profiles, **small_table_rows**, auditor_instruction | `planning_context`, three required templates (`rcm`, `rcm_controls`, `rcm_attributes`), `current_apm` (**req, 60k**), `current_rcm` (opt, 200/40k), `table_metadata`, `table_profiles`, `small_table_rows` (8/16k), `documents` (lexical), `methodology` (lexical), `instruction` |
| `planning.delta` | 210 / 120k | planning_context, document_text, auditor_instruction | `new_document_analyses` (**req**, 8/40k), `current_apm` (opt, 32k), `current_rcm` (opt, 200/40k), `planning_context` (req), `instruction` |
| `tests.cycle_linkage` | 2 / 88k | planning_context, **document_schemas** | `cycle_schemas` (req, 1 item carrying every induced type, 64k), `cycle_requirements` (opt, 24k). The schemas and what the matrix asks of them — nothing either was induced or drafted from. |
| `tests.generate` | 180 / 160k | planning_context, document_text, **document_schemas**, table_metadata, auditor_instruction | `planning_context` (req), `rcm_row` (req, 16k), `table_metadata` (lexical, 12/24k), `transaction_evidence` (req, 1/40k), `planning_documents` (**lexical_retained**, 8/20k — planning material only), `evidence_types` (opt, 1/4k — one item per document type, with how many records of it), `evidence_schemas` (opt, 1/32k, row-scoped), `methodology` (lexical), `instruction`. No profiles: test code is validated against schema-only empty frames. |
| `fieldwork.document_qa` | 66 / 44k | document_text | `qa_item` (req, 4k), `document_reading` (opt, 1/4k — one record's structured reading), `criteria_excerpt` (opt, 4/12k — the policy the answer is judged against), `document_pages` (opt, 60/26k — `raw_pages` when the auditor scoped pages, `excerpt` otherwise) |
| `fieldwork.cycle_vouch` | 1 / 40k | document_text | `cycle_item` (req) — the whole linked cycle and its pending checks as one candidate, because a comparison needs both sides |
| `reporting.finding_draft` | 7 / 44k | template_text, document_text, **datatest_exception_rows**, auditor_instruction | `observation`, `rcm_row`, `test`, `execution_result`, `finding_template` (all req), `exception_rows` (opt, 10k), `instruction` |
| `documents.category` | 1 / 6k | document_text | `document_category` (req) — the opening page |
| `documents.classification` | 1 / 6k | document_text | `document_classification` (req) |
| `documents.evidence_read` | 7 / 49k | document_text, document_images | `document_pages` (req, 48k), `document_page_images` (opt, 6) |
| `documents.analysis_chunk` | 2 / 34k | document_text | `document_metadata` (req), `document_chunk` (req, 32k) |
| `documents.analysis_structured` | 1 / 32k | document_text | `document_structured_chunk` (req) |
| `documents.analysis_visual_page` | 5 / 2k | document_text, **document_images** | `document_metadata`, `document_page_images` (req, 4 images) |
| `documents.analysis_reduction` | 201 / 62k | document_text | `document_metadata`, `chunk_analyses` (req, 200/60k) |
| `analysis.join_utility` | 13 / 44k | document_text, table_metadata, table_aggregates | `join_candidates` (req), `join_tables` |
| `analysis.reading` | 577 / 180k | table_metadata, table_profiles, table_aggregates, value_domains | `frame_map` (req, 224/60k), `nominations`, `relationship_map`, `join_hypotheses`, `value_domains`, `analytics_registry` (req) |
| `analysis.definitions` | 96 / 88k | table_metadata, table_profiles, table_aggregates, value_domains, auditor_instruction | `target_schema` (req), `target_profile`, `target_aggregates`, `related_frames`, `join_hypotheses`, `relationship_evidence`, `analytics_registry` (req), `lookup_candidates`, `probe_findings`, `value_domains`, `current_analyses`, `instruction` |
| `analysis.summary` | 250 / 170k | planning_context, table_metadata, table_profiles, analysis_results, **analysis_exception_rows** | `analysis_results` (req, 120/60k), `analysis_exceptions` (40/45k), `analysis_anomalies`, `coverage_gaps`, `table_joins`, `table_metadata`, `table_profiles`, `planning_context` |
| `analysis.promotion` | 105 / 80k | document_text, table_metadata, analysis_results | `promotion_subject` (req), `rcm_rows` (req, 80/48k), `table_metadata` |

A few of these budgets are load-bearing and were set from observed failures.
`planning.rcm`'s `current_apm` sits at 60k because at 32k a 53.5k-character
memorandum lost 21.5k of its tail — and the coverage gate, reading the same
truncated copy, enforced 7 of 14 themes while reporting nothing about the 7 it
could not see. `planning.apm`'s `current_apm` sits at 32k because text
truncates rather than drops: a revision turn that cannot see the end of what it
is revising rewrites it, silently.

---

## 8. Stage reference

Order below is the registry's declaration order, which is also the order a
full-audit closure schedules them in.

### `sources.imported` — Sources

| | |
| --- | --- |
| Depends on | — (graph head) |
| Readiness | Satisfied if the workspace holds **any** document or table. Either kind alone is a real engagement. |
| Units | **None, ever.** Importing is the auditor's act. |
| Binding | `_bind_unreachable` — registered so every capability has one, and raises if it is ever called |
| Context | none |
| Invalidated by | `sources` |

### `documents.text_ready` — Document content

| | |
| --- | --- |
| Depends on | — |
| Units | one per document (`document_text:<doc>`) |
| Binding | **deterministic** (`documents.extract`) — local text extraction |
| Context | none: no model sees this capability's inputs |
| Output | cached document text under `Documents/` |

### `documents.categorized` — Document classification

| | |
| --- | --- |
| Depends on | `documents.text_ready` |
| Units | one per uncategorized document (`document_category:<doc>`) |
| Binding | pipeline — worker `documents.category`, executor `documents.category` |
| Context | `documents.category` — the document's **opening page** only (1 item / 6k) |
| Input | the first page's raw text |
| Output | `{category, confidence: high\|medium\|low, rationale}` |
| Commit | category mirrored onto the shared `documents` collection |
| Barrier | sequential — independence of *inputs* is not independence of *commits*; two units landing at once would race on the shared collection |

What a document is *to this engagement* — planning material or transaction
evidence. It precedes the type because the type is only asked of evidence, and
because a category guessed from a filename put policy material under voucher
fields and left evidence out of scope entirely.

### `documents.types_classified` — Document types

| | |
| --- | --- |
| Depends on | `documents.categorized` |
| Units | one per evidence document (`document_classification:<doc>`) |
| Binding | pipeline — worker `documents.classification`, executor `documents.classification` |
| Context | `documents.classification` — opening page only (1 / 6k) |
| Output | a type from the closed global catalog |
| Barrier | sequential (shared collection commit) |

### `documents.evidence_read` — Evidence readings

| | |
| --- | --- |
| Depends on | `documents.types_classified` |
| Units | one per evidence document, keyed by type (`evidence_read:<type>:<doc>`) |
| Binding | pipeline — worker `documents.evidence_read`, executor `documents.read` |
| Context | `documents.evidence_read` — the document's pages (48k) plus up to 6 page images |
| Output | `{records[]: {fields[]}, new_fields[]}` — field values read against the type's accumulating master |
| Repairs | **2**, not the usual 1 |
| Barrier | **sequential, and here that is the mechanism.** A serialized unit sees its predecessor's work by rebinding against committed state; the parallel path binds every unit before running any of them. Per-document calls can only agree about a vocabulary if they are not independent — "make the read parallel and lock the master" is not an option, because the reads would not be *wrong* about the master, they would never have been shown it. |

The double repair allowance is earned: this worker's refusals are precise and
recoverable ("you returned 18 citations and not one field value"), and what a
lost read costs is not one document but its type's whole vocabulary, because a
type with an unread document is never stamped.

### `documents.schemas_stamped` — Document schemas

| | |
| --- | --- |
| Depends on | `documents.evidence_read` |
| Units | one per document type (`document_schema:<type>`) |
| Binding | pipeline binding with **no worker** — it commits through `UnitPipeline.commit_local` with executor `documents.stamp` |
| Context | none — no model turn. The stamp reads the finished master, calls `save_schema` once, and back-stamps the type's readings. |
| Output | one frozen schema per document type |

### `documents.analysis_chunks_ready` — Document chunk analysis

| | |
| --- | --- |
| Depends on | `documents.text_ready`, `documents.categorized` |
| Units | one per bounded source chunk (`document_chunk:<doc>:<chunk>`), in three kinds: `document_chunk_analysis`, `document_visual_page_analysis`, `document_structured_analysis` |
| Binding | pipeline, **proposal-only** (`executor_id=None`) — a chunk analysis is run-local; its durable home is the unit's proposal sidecar, not a workspace collection |
| Context | one preset per unit kind: `documents.analysis_chunk` / `documents.analysis_visual_page` / `documents.analysis_structured` |
| Output | `{summary_markdown, audit_notes_markdown, citations[]}` |
| Barrier | **parallel** — independent, and they commit nothing |

The category edge is not optional here. This pass excludes transaction
evidence *by category*, so a document whose category has not been read yet
would be chunked as prose and then read again as evidence — one document
analysed twice under two vocabularies.

### `documents.analysis_generated` — Document analysis

| | |
| --- | --- |
| Depends on | `documents.analysis_chunks_ready` |
| Units | one per document (`document_analysis:<doc>`) |
| Binding | pipeline — worker `documents.analysis_reduction`, executor `documents.analysis` |
| Context | `documents.analysis_reduction` — up to 200 chunk proposals (60k) plus document metadata |
| Output | `{derived_text_markdown, summary_markdown, audit_notes_markdown}` |
| Commit | `Documents/.analysis` sidecars under the document's material parent hash, stamped with run/unit/content provenance so an interrupted commit is reconciled rather than repeated |

Only the *reduced* analysis is an engagement artifact.
`documents.analysis_reviewed` — the auditor's own review — is on the document
graph and deliberately **not** on the audit graph: nothing the agent does
satisfies it, and an audit run must never wait on or imply it.

### The exploratory analysis branch

The audit graph schedules `data.relationships_inferred` →
`data.join_utility_ready` → `data.joins_ready` → `analysis.register_ready` →
`analysis.definitions_ready` → `analysis.executed` → `analysis.summarized`.
The full-audit outcome set requests the memo *before* the planning outcomes, so
the sequential scheduler completes it and the memo exists by the time planning
reads it — but neither planning capability *depends* on it.

| Capability | Binding | Worker / Executor | Notes |
| --- | --- | --- | --- |
| `data.relationships_inferred` | deterministic | — | Relationship facts are never model-generated: they come from the deterministic Polars diagnostics in `agent/joins.py`. |
| `data.join_utility_ready` | pipeline, **proposal-only** | `analysis.join_utility` / — | Gates which candidate joins are worth materializing. |
| `data.joins_ready` | deterministic | — / `analysis.join` | A join is applied automatically only on a single strong candidate. |
| `analysis.register_ready` | pipeline | `analysis.reading` / `analysis.register` | The one cross-cutting turn. Its **floor is a deterministic sweep**, so a run whose reading turn is skipped or fails still holds a complete, committable register. The reading turn itself is bound proposal-only; the adapter merges the proposal over the floor and writes the register through executor `analysis.register` in a separate commit. |
| `analysis.definitions_ready` | pipeline | `analysis.definitions` / `analysis.definitions` | Authors specs only for work the register could not already express. |
| `analysis.executed` | deterministic | — / `analysis.execution` | Local execution. Records a bounded `last_result` (shape, verdict, statistics, flagged-row count) — never result data. Flagged rows live in an evidence sidecar. |
| `analysis.summarized` | pipeline | `analysis.summary` / `analysis.summary` | The EDA memo. The one place `allow_analysis_exception_rows` is granted. |

Three analytics tests are excluded from autonomous proposal —
`period_compare`, `stratify`, `sampling` — because all three are *descriptive*:
they have no exception concept, so proposing one spends a definition turn and
an execution to produce a chart nothing downstream can promote or conclude
from.

### `planning.context_ready` — Planning context

| | |
| --- | --- |
| Depends on | `sources.imported`, `documents.analysis_generated` (both **partial**) |
| Readiness | satisfied when any planning-context field is non-empty, or interview answers exist |
| Units | one (`planning_context`) |
| Binding | pipeline — worker `planning.context`, executor `planning.context` |
| Context | `planning.context` — current context plus planning-relevant documents. Planning relevance is a **deterministic category rule**, not a model judgment and not a lexical score: at this point there is no stated objective to score against — producing one is what this capability is for. |
| Output | `{context: {...}}` — the engagement's objective, scope, period, and so on |

### `planning.apm_ready` — Audit planning memorandum

| | |
| --- | --- |
| Depends on | `planning.context_ready` |
| Readiness | non-empty markdown; `review_required` if it carries no `#` headings. Currency vs. changed sources is **not** assessed — the auditor decides when to force. |
| Units | one (`apm`), parent `planning:context` |
| Binding | pipeline — worker `planning.apm`, executor `planning.apm` |
| Context | `planning.apm` (47 / 96k) — planning context, the APM template, the current APM, the EDA memo, a population summary, table metadata and profiles, lexically-selected documents and methodology, and the auditor's instruction |
| Input | `{kind, input_sha1, parent_refs}` |
| Output | **Markdown**, not JSON |
| Guard | `expected_parents = parent_hashes(ws, ["planning:context"])` |
| Conflict | An auditor-edited APM is preserved: the unit goes `awaiting_confirmation` and the proposal is recorded under `run["planning_revisions"]` rather than overwriting |

Planning sees the EDA *memo*, never the flagged rows themselves — and with
embed directives already flattened to citations.

### `planning.cycle_ready` — Cycle design

| | |
| --- | --- |
| Depends on | `planning.apm_ready`, `sources.imported` (partial), `documents.types_classified` |
| Readiness | **the one planning capability that assesses currency**: a cycle whose `apm_sha1` no longer matches the memorandum is `missing`. The shape is a reading *of* the memorandum's process flow, and the matrix downstream takes its `process` vocabulary from it. An auditor's edit keeps the hash it was drafted against, so edits survive until the memorandum itself moves. |
| Units | one (`cycle`), parent `planning:apm` |
| Binding | pipeline — worker `planning.cycle`, executor `planning.cycle` |
| Context | `planning.cycle` (26 / 86k) — planning context, the **whole** memorandum (60k), table metadata. Names and shapes only. |
| Output | `{name, steps[]: {name, roles[], populations[], themes[]}, cross_cutting}` — roles are document types chosen from the types held, spelled exactly; populations are tables whose rows *are* that step |

### `planning.rcm_ready` — Risk and control matrix

| | |
| --- | --- |
| Depends on | `planning.apm_ready`, `planning.cycle_ready`, `documents.categorized`, `documents.types_classified` |
| Readiness | rows exist; `review_required` if any row lacks a risk or a control |
| Units | one (`rcm`), parents `planning:apm`, `planning:cycle` |
| Binding | pipeline — worker `planning.rcm`, executor `planning.rcm` |
| Context | `planning.rcm` (255 / 138k) — see §7 |
| Prompt | **three prompts in sequence** (risks, controls, attributes); the worker's `prompt_hash` covers all three, because the sequence is what decides what reaches the model |
| Output | `{rows[], quarantined[]?}` — rows the worker could not repair within its allowance are quarantined and recorded for the auditor rather than failing the run and discarding every correct row |
| Repairs | row-scoped: guidance is grouped per row, with a raised ceiling (20 errors / 4,000 characters) so a document with several bad rows does not have its errors dropped |

### `planning.change_assessed` — Change assessment

| | |
| --- | --- |
| Depends on | `planning.apm_ready`, `planning.rcm_ready` (see §2 — the edges went in once a scheduled dependency stopped being a reason to rewrite) |
| Readiness | `blocked` until the request names documents (`document:` refs); `missing` when no assessment exists for this exact basis; `satisfied` otherwise. The basis is a hash over the named documents' analyses, the memorandum, and the matrix rows, so changing any of the three re-asks the question. That the memorandum and matrix *exist* is the graph's job now, not a second check written out here. |
| Units | one (`change_assessment`), parents `document:<id>` for each named document, input `{document_ids, basis_sha1}` |
| Binding | pipeline — worker `planning.delta_review`, executor `planning.delta` |
| Context | `planning.delta` — the named documents' generated analyses (required), the current memorandum and matrix, the planning context, and the auditor's instruction |
| Output | `{impact: none\|apm\|rcm\|both, summary, apm_changes[], rcm_changes[]}`; the worker rejects an impact that disagrees with its own lists, an APM section that does not exist, or an RCM id not in the matrix |
| Commit | `Planning/.delta/<basis_sha1>.json`, guarded on the memorandum's parent hash. Nothing else moves: the assessment is what the auditor acts on, and the revision it recommends is a separate request. |

Off every template. The steering loop asks for it by name when an auditor asks
what new evidence changes.

### `tests.cycle_ruleset_proposed` — Cycle rules proposed for review

| | |
| --- | --- |
| Depends on | `planning.rcm_ready`, `planning.cycle_ready`, `documents.schemas_stamped` |
| Readiness | **satisfied where nothing asks** — an engagement whose matrix classifies no attribute as `transaction_cycle` needs no rules. Where the matrix asks and no schema has been induced: `review_required`. |
| Units | one, and only when the matrix asks and no ruleset exists |
| Binding | pipeline — worker `tests.cycle_linkage`, executor `tests.cycle_ruleset` |
| Context | `tests.cycle_linkage` — the induced schemas as **one atomic item** (a cycle is a statement about how the whole set relates, so a budget that admitted some of it would produce a proposal missing a role with nothing saying which), plus the matrix's requirements |
| Input | the schema hashes ride on the unit's `input_sha1`, so a re-derived schema re-proposes rather than leaving an auditor approving rules against a vocabulary that moved |
| Output | `{roles[], join_keys[], assertions[]}` |

This is the only stage that sees the matrix's requirements, the cycle's roles,
*and* the induced field vocabulary at once, which is why the evidence contract
is authored here.

### `tests.cycle_ruleset_approved` — Cycle rules made effective

| | |
| --- | --- |
| Depends on | `tests.cycle_ruleset_proposed` |
| Binding | **deterministic** (`tests.cycle_ruleset_approval`) |
| Context | none — and no worker. The judgement this gate exists for was made when the auditor chose the mode; re-asking a model to bless its own rules would add ceremony, not a check. |

The two run modes part here:

- **permission mode**: expands **no units**. The stage settles from its own
  readiness (`review_required`), the run carries on, and `tests.specified`
  proceeds on its partial edge to write document-question tests instead.
- **auto mode**: the auditor has delegated the run's approvals, so an
  unapproved proposal is work. One unit approves it and the cycle test becomes
  generatable.

### `tests.specified` — Executable test specifications

| | |
| --- | --- |
| Depends on | `planning.rcm_ready`, `tests.cycle_ruleset_approved` (**partial**) |
| Readiness | every scoped row has at least one executable test; a row declaring transaction-cycle evidence that still holds a pre-ruleset test is `missing` |
| Units | **one per RCM row** (`test_generation:<row>`) |
| Binding | pipeline — worker `tests.generate`, executor `tests.generate` |
| Context | `tests.generate` (180 / 160k) |
| Input | the RCM row — or `{row, regenerate_test_ids[]}` when the request named specific tests, which is part of the unit's input identity so a whole-row proposal is never reused as a single-test rewrite |
| Output | `{tests[]}` |
| Repairs | **2** |
| Barrier | **parallel** |

Naming a test *is* the instruction: it says the row is not settled whatever the
manifest reports, and which test is wrong — so neither the coverage gate nor
the auditor-draft gate applies, and `force` need not be asked for separately.

Every other RCM row used to be supplied here as duplicate avoidance and did not
achieve it: the projection carried the other rows' *risks* rather than the
tests already written for them, a unit cannot see what its siblings produce,
and it cost a third of the prompt. Deduplication needs a pass that can see every
generated test at once.

**Evidence documents stopped travelling as prose; the schema says what they
contain.** This is the one capability that read transaction-evidence prose for
something other than reading that document, and it was the largest document
consumer in the system: measured over 83 units in the shipped workspaces,
1,026,773 characters of evidence summaries and citations, 7k–15.5k per RCM row.
Those two fields were 83% of the projection's bytes and said the same thing once
per document — eighteen payment instructions describing what a payment
instruction is. The `documents` source now carries an evidence document's
identity and its `document_type`; `evidence_schemas` carries what a document of
that type *states*, once per type, through the same `schema_catalog` projection
`tests.cycle_linkage` uses. Planning material is untouched: a policy has no
induced schema, and its prose is the thing being reasoned about rather than one
sample of a population.

**And then they stopped travelling at all.** Withholding an evidence document's
prose was the first half; a document step naming a *type* rather than a list of
ids is the second, and it removes the identity items too. The workspace resolves
the type when the test runs, so listing eleven vouchers — or eighty-four — spent
the prompt on ids no step is allowed to write. `planning_documents` therefore
carries planning material only, and `evidence_types` says what the engagement
holds once per type: the type, how many documents carry it, how many *records*
they hold between them, and three sample ids. The record count is the number
that changes a decision, because a requirement written against a population
cannot be answered by a type carrying one record. See
[document-test-population-design.md](document-test-population-design.md).

The schema is always available. `documents.schemas_stamped` sits at index 11 of
this capability's dependency closure and `tests.specified` at 14, so no ordering
changed and no edge was added.

**A document step names one source.** Either `population`
— `{document_type, fields[], criteria_refs[], selection?}` — or `document_ids`,
never both: an item carrying both cannot be re-resolved without either dropping
the hand-picked ids or holding documents the type no longer reaches, and nothing
says which was meant. `document_ids` is now for a question about one *named
planning document*; evidence is named by type. The step's validation refuses a
type no supplied schema covers ("names a type this engagement holds no schema
for") and a field outside that schema — both being honest gaps stated from the
side they are actually on, where the old contract could only say
`missing_evidence`, and the model reached for "no documents record expense
categories" in a workspace holding twelve of them. `criteria_refs` name a
supplied planning document with an optional section, or `rcm:<id>#criteria`
where the criterion exists only in the matrix row.

**A Data Test is a predicate; a criterion is not.** `evidence_kind` on a control
attribute says where the requirement's *population* lives, not which test shape
answers it. A tabular attribute produces a Data Test where the requirement is
computable from the columns — an amount over a limit, an approval absent, two
columns that must agree — and a document question where it turns on reading the
criteria against a record. The rule exists because the closing instruction used
to read "a tabular attribute normally produces a Data Test", and a row whose
three attributes all said `tabular_population` produced a Polars step that
encoded the expense policy as an invented category whitelist and filtered on two
literals lifted from the value domains it had been shown. It validated cleanly
and established nothing.

**Scoped to the row, not the engagement.** Selection is the row's own naming
first — a `transaction_cycle` attribute states the types its comparisons read,
and that is the row saying it — then a weighted lexical score over the type's
name, its discriminator and its field labels, and only then every type. The
weighting mirrors `TABLE_NAME_WEIGHT`/`MIN_TABULAR_RELEVANCE` in the RCM worker
and exists for the reason that made those necessary: field labels are generic,
and unweighted overlap admitted six types out of six for half the rows measured.
The final fallback is deliberate — a row that needs documents and matched no
type by name is the row whose author could not say which record answers it, and
withholding the vocabulary there would have the turn invent a field.

**The document source retains what it cannot rank.** `documents.lexical`
*filters*: a candidate sharing no term with the query is dropped. That was right
while a document's summary was the thing being matched. Identity-only evidence
offers a title and a type, and filtering on that emptied the list — measured on
the expenses engagement, 50 of 60 evidence documents across six rows, leaving
every unit able to name a policy and not one voucher. This source therefore uses
`documents.lexical_retained`, which ranks the same candidates and keeps the
unmatched ones at the tail in source-ref order. It is the case `tables.lexical`
already answers, one noun over: a document the turn cannot see is a document no
step can name.

Retaining them was the repair; naming the type is the settlement. `evidence_types`
is one item selected by `documents.all`, so no selector scores it and a voucher
sharing no term with the row cannot be ranked out of the turn at all. The
retaining selector still governs `planning_documents`, where the same argument
holds for a policy the row does not happen to quote.

Scoping is what makes the substitution pay on an engagement whose documents do
*not* collapse into a handful of types. Measured through the resolver — what
actually reaches a turn after the per-source budgets, not the candidate pool —
summed over eight rows per workspace:

| workspace | evidence docs : types | before → after | | evidence supplied |
| --- | --- | --- | --- | --- |
| `expenses` | 12 : 1 | 197,615 → 87,368 | **−56%** | 80 → 80 |
| `treasury` | 8 : 4 | 150,448 → 75,201 | **−51%** | 64 → 64 |
| `treasuryfull` | 82 : 6 | 207,173 → 170,711 | **−18%** | 56 → **80** |
| `procurement` | 5 : 5 | 155,424 → 142,849 | −9% | 40 → 40 |

Two things the raw candidate pool hides and the resolved figure does not.
Treasuryfull saves least because its 12-item/26k source budget was *already*
truncating the prose, so the old turn was budget-limited rather than
document-limited — the redundancy was being paid for in coverage instead of
characters. And its evidence coverage goes up, from 56 documents to 80, because
the retaining selector admits what the filter dropped: the same budget now buys
more of the population and none of the repetition.

Procurement is close to a wash, and honestly so: where each document is its own
type there is no redundancy to collapse. The token saving is the secondary
benefit in any case. The worker keeps six of twelve candidates and ranks them on
identity alone — it never read the prose — so on treasuryfull the six it kept
were arbitrary representatives of 82 near-identical documents. A schema names
all 25 fields of `treasury_deal_ticket` with their roles; six sampled summaries
name the fields six particular tickets happened to fill.

### `tests.promoted_from_analysis` — Analyses placed in the matrix

| | |
| --- | --- |
| Depends on | `tests.specified` |
| Readiness | satisfied when nothing is pending — **including when no analysis ever ran**. Deliberately not scoped by table. |
| Units | one per candidate saved analysis (`analysis_promotion:<id>`) |
| Binding | pipeline — worker `analysis.promotion`, executor `analysis.promotion` |
| Context | `analysis.promotion` — the procedure and its result, the RCM rows (80/48k), table metadata |
| Barrier | **parallel** — each unit commits under its own analysis's parent hash |

It sits *after* generation so a promoted test is written against a matrix whose
own tests already exist, and *before* fieldwork so a promoted test is executed
with everything else — a procedure carried into a test and then never run has
not been carried anywhere.

### `fieldwork.executed` — Fieldwork execution

| | |
| --- | --- |
| Depends on | `tests.specified`, `tests.promoted_from_analysis` (both **partial**) |
| Readiness | reads the scoped test manifest: `missing` while anything is pending, `review_required` for non-executable or evidence-blocked tests |
| Units | **mixed kinds**, one binding |
| Context | per unit kind: `fieldwork.document_qa` for `document_qa_execution` and `document_llm_execution`, `fieldwork.cycle_vouch` for `cycle_vouch_execution` |

| Unit kind | Execution |
| --- | --- |
| `data_test_execution` | deterministic — `run_data_test` |
| `document_test_execution` | deterministic — `run_document_test` (also the shape used when a worklist is blocked on requested evidence, so the run records the block against the evidence request instead of pretending to test) |
| `document_qa_execution` / `document_llm_execution` | **pipeline** — worker `fieldwork.document_qa`, executor `fieldwork.document_qa`. One unit per unanswered *assessment unit*: an attached document, or one **record** of a resolved population. Output `{answer, conclusion, control_conclusion, outcome, citations[]}`, and the worker binds every citation to a page — or a reading field — it was actually supplied. |
| `cycle_vouch_execution` | **pipeline** — worker `fieldwork.cycle_vouch`, executor `fieldwork.cycle_vouch`. Output `{cells[]: check_id, verdict, compared, reason}`. |
| `document_test_review` | deterministic, settles immediately — only an auditor can dispose of it |

Every Document Test unit is bound by `doc_tests_execution.bind_document_test_unit`
and expanded by `capabilities.doc_tests.document_test_units` — the same two
functions the standalone `doc_tests_workflow_v2` graph uses. A worklist behaves
identically whichever graph scheduled it, and a Q&A test reaches the provider
only through the registered `fieldwork.document_qa` worker and its declared
context.

**The unit of assessment is the record.** A Q&A item that names a *population*
rather than a list of ids fans out one unit per record, not per document,
because a voucher pack's three line items are three transactions and assessing
them together lets two clean lines carry a third that is not. The record is in
the unit's identity and in its lineage (`record:<document>:<index>`), so a
re-run answers only what is unanswered and a receipt says which record it
settled. Answers are keyed `<document id>#<record index>`; a bare document id
still means the whole document, so every stored `qa_answers` map keeps
resolving. `capabilities.doc_tests.assessment_pairs` is the one place that
counts these, and the four budget sites read it rather than
`len(item["document_ids"])`.

**What one assessment reads.** For a population unit the primary evidence is the
record's own structured reading, projected to the fields the question names plus
the ones that identify it, with the citation the reading recorded for each —
a few hundred characters where a page excerpt was twenty-six thousand.
`criteria_excerpt` carries the policy the answer is judged against, and is never
cited as evidence about the record. Pages are fetched *only* where the reading is
silent about a named field, because silence about a field is not a value. A
citation may name a field, which the executor resolves to the page the reading
read it from — so an answer grounded in a field is grounded in a real page the
model was never shown.

Because the stage fans out one unit per assessment, `max_units_per_stage` is
raised from its 250 default to what the engagement's populations actually
resolve to (`assessment_pairs` plus headroom, in `routing` and in both
`_refresh_dynamic_limits`). A resolved population is bounded — by
`document_population.MAX_POPULATION_RECORDS` and by the auditor's own selection
— so refusing to schedule it would refuse the feature rather than bound it.

### `results.rolled_up` — Results and observations

| | |
| --- | --- |
| Depends on | `fieldwork.executed` (partial) |
| Units | one (`rollup`) |
| Binding | **deterministic** (`fieldwork.rollup`) |
| Output | each row's derived result and its observations, recomputed from current execution artifacts. Observation identities are keyed on `execution_ref`, so a repeated roll-up reuses rows rather than duplicating them. |

Roll-up is the first point that can see the fieldwork as a whole, and so the
only one that can warn about populations no executed data test makes a
statement about. A per-row conclusion cannot: every row concluded on the tests
it had.

### `findings.drafted` — Eligible finding drafts

| | |
| --- | --- |
| Depends on | `results.rolled_up` |
| Readiness | every eligible exception observation has a supported finding; `review_required` when a linked finding has support issues |
| Units | one per eligible exception observation (`finding:<obs>`), parents `observation:`, `rcm:`, and the execution ref |
| Binding | pipeline — worker `reporting.finding`, executor `reporting.finding` |
| Context | `reporting.finding_draft` — observation, RCM row, test, execution result, the firm's finding template, and (for a Data Test) the **flagged rows** |
| Output | **Markdown**: a title line, a severity line, and everything from the first `##` heading onward as the narrative — the prose copied into the report unchanged |

The narrative's sections are the firm's, so the template is required *context*
rather than a constant in the worker: a firm changes what a finding must say by
editing the template, not the code.

### `working_papers.generated` — RCM working papers

| | |
| --- | --- |
| Depends on | `results.rolled_up` |
| Units | one per RCM row (`working_paper:<row>`) |
| Binding | **deterministic** (`reporting.working_paper`) |
| Output | `WorkingPapers/<row>.json`, a pure projection of current RCM/execution state, parent-hash guarded |

### `report.working_draft` — Report working draft

| | |
| --- | --- |
| Depends on | `planning.apm_ready`, `results.rolled_up`, `findings.drafted` (partial) |
| Units | one (`report`) |
| Binding | **deterministic** (`reporting.report_draft`) — no worker, no model call |
| Output | the assembled draft. An auditor-edited draft is preserved and its regenerated candidate left for reconciliation, recorded as `awaiting_confirmation`. |

### `audit.verified` — Audit verification

| | |
| --- | --- |
| Depends on | `working_papers.generated`, `report.working_draft` (both partial) |
| Units | one (`verify`) |
| Binding | **deterministic** (`reporting.verify`), read-only |
| Output | the completion/quality/output outcome, recorded on the run. `succeeded` when complete, `blocked` with the completion status, report-quality error count, and output-gate count otherwise. |

---

## 9. Budgets

Backpressure is **budgetary, not queue-based**: per-workspace serialization,
`pending_commands` FIFO between runs, and per-run ceilings.

At route installation (`routing.install_resolution`):

```python
audit_turns    = 20 + 4*len(rcm) + 4*test_count + 2*qa_pairs + 2*eligible_findings
document_turns = 4 + chunks + 2*max(1, len(scoped_documents)) + preparation_turns
analysis_turns = 10 + 2*max(1, len(scoped_frames))

max_model_turns              = audit_turns + document_turns + analysis_turns
max_estimated_prompt_tokens  = max(existing, max_model_turns * 10_000)
max_completion_tokens        = max(existing, max_model_turns *  4_000)
max_units_per_stage          = 250        # a stage above this refuses to launch
max_llm_concurrency          = default_llm_concurrency()
max_compute_concurrency      = 2
max_execution_attempts       = 2
```

`AuditWorkflowExecution._refresh_dynamic_limits` recomputes a *different* sum
on every `_refresh()` (§4), grow-only, and adds vision allowances from the
real visual-unit count (`max_image_parts`, `max_prepared_image_bytes`,
`max_prepared_image_pixels`, and a prompt allowance of
`turns*10k + text_units*2k + visual_units*10,480`). Its document term is
`len(analysis_unit_specs) + max(1, documents)` with no base and no preparation
turns, and it carries no `analysis_turns` term at all; grow-only means the
installed value is never lowered, but analysis work that grows mid-run does
not raise it. This matters because the arithmetic depends on counts the run
itself creates: an RCM drafted mid-run changes how many test-generation turns
the budget must buy. The document, analysis, and doc-test execution adapters
each carry their own `_refresh_dynamic_limits` for their own graphs.

Two things the ceilings are not. They are not sent to the provider:
`max_completion_tokens` is checked cumulatively after each call, never passed
as `max_tokens`. And they are not the constraint that ends long runs in
practice — recorded runs use a small fraction of their turn ceilings; what
ends an 84-document engagement is the deadline.

`DefaultRunRuntime` owns the durable ledger, the dynamic limit updates, the
runtime deadline (extended by time spent blocked on the auditor), checkpoints,
live inbox draining, approval batches, and structured-interaction waits.
Offline auditor responses are persisted before wakeup and consumed on
same-schema restart. The deadline is an in-memory monotonic value seeded at
runner construction (3,600 s) and is not persisted: a resumed run, and every
child run a steering loop starts, gets a fresh one.

One budget subtlety worth knowing: `READ_REPAIR_ATTEMPTS = 2` lives in
`workflow.py` rather than beside the evidence-read worker, because two layers
must agree on it and neither may import the other — the worker *spends* the
attempts and `preparation_model_turns` has to *buy* them. A budget sized at one
turn per read that then spends three is precisely the failure the budget exists
to prevent.

---

## 10. Approvals, gates, and modes

**Auto mode** delegates the run's approvals. **Permission mode** asks.

- **Per-proposal approval.** A pipeline binding supplies an
  `approval_provider` only in permission mode. It builds proposal items, calls
  `request_approval`, and returns the accepted spec — or `None`, which settles
  the unit as `approval_rejected` with the proposal already durable.
- **Stage review.** A run whose context carries `review_each_stage` is asked
  before each stage runs (`audit_execution.stage_review`): **continue**,
  **skip**, or **stop**. An auto run never waits there.
- **Scope checkpoints.** In permission mode, the document and analysis stages
  fire a scope checkpoint through `before_stage` so the auditor can narrow what
  the branch will touch.
- **Auditor-edit preservation.** Executors reconcile rather than overwrite. An
  edited APM, cycle, planning context, or report yields
  `awaiting_confirmation` and a preserved candidate, never a silent overwrite.

Three outcomes are structurally **not** the agent's to settle:
`documents.analysis_reviewed`, `doc_tests.dispositioned`, and — in permission
mode — `tests.cycle_ruleset_approved`.

---

## 11. What lands on disk

```text
Workspaces/<id>/AgentRuns/<run_id>/
├── run.json          the durable record: route, engine, limits, workflow state
│                     (definition, definition_hash, scope, resolved_capabilities,
│                     state_at_resolution, stages[{id, capability, barrier,
│                     status, units[], readiness_before}], reused_capabilities,
│                     next_outcomes, workflow_explanation), plan, approvals,
│                     artifacts, milestones, narration, warnings, usage counters
├── contexts/<unit>.json    ContextManifest — content-free: hashes, sizes,
│                           selections, omissions, truncations, privacy decisions
├── proposals/<unit>.json   the model's proposal + its ProposalExecutionIdentity
│                           + worker_attempts + response/schema hashes
├── rejections/<unit>.json  the final invalid response verbatim, its validation
│                           errors, and the identity it was produced under
├── receipts/<unit>.json    ExecutorReceipt — proposal hash, concurrency mode,
│                           revision before/after, artifact refs,
│                           postcondition hashes, reconciled flag
├── sidecars/<sha1>.json    large or sensitive interaction/undo payloads,
│                           stored by content hash
└── conversation.json       steering-loop runs only: the loop's own model
                            conversation, rewritten after every turn
```

A unit record in `run.json` carries only *references* to these sidecars
(`context_manifest`, `proposal_sidecar`, `receipt_sidecar`,
`rejected_response_sidecar`) plus `input_sha1`, `parent_refs`, `status`,
`attempts`, `result_refs`, and timings — never content.

Sidecars are keyed by run, so proposal reuse (§5 step 4) can only happen when
the *same* run is resumed. A follow-up run started from `next_outcomes` is a
new run with an empty sidecar folder; what it avoids repeating, it avoids
through readiness, not through proposal identity.

The workspace's `telemetry.db` holds the rest, keyed by run: the replayable
event stream (`run_events`, indexed so reading forward from a cursor is a range
read; the UI consumes it as SSE from `agent_routes.py`, replayable by cursor or
`Last-Event-ID`), the hash-only model provenance (`activity_events`), the
stage restore points the scheduler's `stage_checkpoint` takes
(`checkpoints`, `checkpoint_files`), and the debug store's `llm_calls`, which
carry every provider request and raw response in full. The run record is
content-free; the workspace's telemetry is not.

---

## 12. Where to change what

| To change… | Edit | Consequence |
| --- | --- | --- |
| a dependency edge, or add a capability | `agent/workflows/audit.py` | changes `definition_hash()`; startup validation fails until the grouped modules partition the new graph exactly. An edge only *orders* work — it never causes a rewrite |
| what makes settled work be redone | that capability's `invalidate_on` in `agent/capabilities/<group>.py`, plus `BASIS_PRODUCERS` in `agent/workflows/<graph>.py` | changes `definition_hash()`; startup validation refuses a key with no producers and a producer that is not a capability. A capability whose producer runs is re-expanded with `scheduled_because: "parent_rescheduled"` |
| whether an artifact can report itself out of date | the `workflow_parents` its executor stamps at commit, plus `_shared.currency` in that capability's `readiness` | a moved parent becomes `stale`, which schedules the work and says so in the run explanation |
| what an outcome means / when it is done | that capability's `readiness` in `agent/capabilities/<group>.py` | changes what materialization skips |
| how work fans out | that capability's `expand_units` | changes unit IDs, so proposals stop being reused if the ID changes |
| what a stage is shown | the preset in `agent/context/presets.py` and the scope function in `agent/context/adapters.py` | moves the `context_manifest_hash`, which rejects persisted proposals and re-bills |
| a prompt or response schema | `agent/workers/<group>.py` | moves `worker_definition_hash`, which rejects persisted proposals and re-bills |
| how a commit is guarded | `agent/executors/<group>.py` | moves `executor_definition_hash` |
| which worker/executor a capability uses | `_PIPELINE_BINDERS` / `_DETERMINISTIC_BINDERS` in `agent/audit_execution.py` | |
| which edges tolerate an unsettled dependency | `_PARTIAL_DEPENDENCIES` in `agent/audit_execution.py` | |
| how a request maps to outcomes | `TEMPLATE_OUTCOMES` in `agent/workflows/audit.py` and `agent/routing.py` | |
| run budgets | `routing.install_resolution` and `AuditWorkflowExecution._refresh_dynamic_limits` | |
| scheduling itself | `agent/runtime/workflow_runner.py` | domain-neutral — it must not learn about audits |

Startup validation (`capabilities/__init__.py`) runs at import and refuses:
overlapping groups, a partition that does not cover the graph, a capability
whose declared edges disagree with the authoritative graph, a dependency cycle,
an `invalidate_on` key the graph maps to no producer, a producer that is not a
registered capability, an unregistered context preset, and — when executions are
supplied — a capability with no binding.

### Durable architectural gates

- `test_agent_final_boundaries.py` — workflow definitions import only graph
  primitives; capabilities never schedule or persist; workers cannot reach a
  workspace, transaction, or run store; executors cannot reach a worker or the
  gateway; context cannot call a provider; one provider call site. All of it
  is enforced by import and call-site scans of the source, so it sees imports,
  not private-attribute reaches or writes made through domain modules.
- `test_agent_runtime_import_boundaries.py` — `runtime/` imports no audit or
  product module; `WorkflowRunner` has no action inheritance or domain stage
  methods.
- `test_workflow_audit_definition.py` — `DEPENDENCIES` pinned edge for edge,
  template membership, topological closure, definition-hash stability.
- `test_agent_capability_composition.py` — groups partition the graph exactly
  once, the startup registry matches, declared identity fields are pinned,
  presets are registered, and only independent non-committing expansions
  declare the parallel barrier.
- `test_workflow_scheduler_golden.py` — golden scheduler behaviour against a
  **synthetic** DAG: closure order, readiness blocking, stable materialization,
  all-settled ordering, recovery, deterministic folding, binding validation. It
  never loads the audit registry or `_PARTIAL_DEPENDENCIES`.
