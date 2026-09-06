# Agent Architecture Review — September 2026

A high-level review of the audit agent as it stands at commit `f10fae1`,
made by reading [audit-workflow-graph.md](audit-workflow-graph.md) against
the code it describes and against the five local engagements on disk
(`Workspaces/Users/local/Workspaces/*`: 70 persisted runs, 1,033 provider
calls, 823 context manifests).

The documentation drift found during the review has been folded into
`audit-workflow-graph.md` directly. This file holds everything else: what the
architecture gets right, where the risk actually sits, what the recorded runs
show, and what to change first.

- [1. Verdict](#1-verdict)
- [2. What holds up](#2-what-holds-up)
- [3. Findings](#3-findings)
- [4. What the recorded runs show](#4-what-the-recorded-runs-show)
- [5. Recommendations](#5-recommendations)
- [6. Smaller items](#6-smaller-items)

---

## 1. Verdict

The framework layer — graph primitives, materialization, the domain-neutral
scheduler, the unit pipeline, the context declaration model — is well made,
and the workflow-graph document describes it accurately. The risk is not
there. It sits in three places the document does not foreground:

1. **Invalidation is implicit.** The rule that decides when existing work is
   redone is a side effect of materialization order, not a declaration, and
   the codebase has already had to route around it.
2. **The architecture lives in the adapters.** The glue between the graph and
   the workspace is roughly seven times the size of the scheduler, is split
   across three execution adapters that share one workspace object by manual
   assignment, and is where every schema change lands.
3. **Several durability mechanisms have never been exercised.** Proposal
   reuse, permission-mode gates, and in-place resume do not appear in any
   recorded run. The mechanisms are paid for on every edit; their benefit has
   not yet been collected.

## 2. What holds up

- **Scheduler and pipeline.** `WorkflowRunner`, `UnitPipeline`,
  `workflow.materialize`, and the composition validator do what the document
  says. Semantic unit IDs, manifest-before-call and proposal-before-commit
  ordering, all-settled fan-out in stable order, and the twelve-step unit
  lifecycle all check out against the code.
- **Startup validation is real.** Group overlap, partition coverage, edge
  agreement with the authoritative graph, acyclicity, preset registration,
  and binding coverage all fail at import.
- **Declared privacy holds.** Every `allow_*` defaults to deny;
  `allow_table_rows` is rejected at the spec, selection, and bundle-item
  layers; the three narrower row doors are on exactly the three presets the
  document names. All 823 manifests on disk are content-free: none holds a
  string over 200 characters.
- **The preset inventory is exact.** All 22 documented presets match the live
  registry on budgets, permissions, sources, selectors, and required flags.
- **Boundary tests exist and pass their own terms.** Workers cannot import a
  workspace, executors cannot import a worker, `runtime/` imports no audit
  module, and there is one `llm.chat` call site under `app/agent`.

## 3. Findings

Ordered by how much they matter.

### 3.1 Invalidation is implicit and cascades

`workflow.materialize` reschedules any *satisfied* capability whose dependency
is being materialized in the same run (`dependency_will_materialize`,
`backend/app/agent/workflow.py`). Every planning capability expands one unit
unconditionally (`_shared.single_unit`). Together that means: import one
document, ask for any lifecycle outcome, and `documents.text_ready` goes
missing, which schedules `categorized`, which schedules the analysis chain,
which schedules `planning.context_ready`, then `apm_ready`, `cycle_ready`, and
`rcm_ready`, each redrafted, with the auditor-edit conflict path
(`awaiting_confirmation`) as the only backstop. An unedited memorandum is
silently rewritten.

`Capability.invalidate_on` is declared on every capability and read nowhere
except `capability_definition_hash`. The field that looks like the invalidation
model is documentation.

The codebase has already collided with this. `planning.change_assessed`
declares **no edges** precisely so that asking "does this new evidence change
the memorandum?" does not rewrite the memorandum first; its comment in
`workflows/audit.py` says so. A capability that must omit its true
dependencies to be usable is the signal that the invalidation model is
missing, not that the capability is odd.

### 3.2 The architecture lives in the adapters, not the framework

| Layer | Lines |
| --- | --- |
| scheduler, pipeline, graph primitives (`runtime/*.py`, `workflow.py`) | ~2,700 |
| `audit_execution.py` + `analysis_execution.py` + `documents_execution.py` | ~6,900 |
| `context/adapters.py` | ~3,800 |
| `context/presets.py` | ~2,150 |

`audit_execution.py` alone is four things: ~900 lines of milestone narration
that switches on nine capability IDs, fourteen `_bind_*` methods that each
redo refresh, scope, parent-hash, and conflict handling in their own shape,
the partial-dependency table and stage review, and a 300-line composition
function that instantiates three adapters and wires four binder tables.

Specific coupling smells:

- Three adapters (`AuditWorkflowExecution`, `AnalysisWorkflowExecution`,
  `DocumentWorkflowExecution`) share one workspace by manual
  `adapter.ws = subject` assignment in `before_stage` and again inside every
  binder.
- `AuditAnalysisGroup` (`capabilities/__init__.py`) rewrites each analysis
  capability's `depends_on` with `dataclasses.replace` and wraps its readiness
  and expansion to inject a hidden `_legacy_analysis: True` scope key that
  `capabilities/analysis.py` and `analysis_execution.py` branch on. The
  document's "one implementation, reused" is true syntactically; behaviourally
  there are two, selected by an undocumented flag.
- `tests.promoted_from_analysis` is declared in the tests group but bound by
  the analysis adapter.
- Milestone routing in `build_audit_workflow_runner` hard-codes a five-ID set
  that duplicates `UNNARRATED_CAPABILITIES`.
- The `fieldwork.executed` binding identity names worker
  `fieldwork.document_qa` and three deterministic kinds; the cycle-vouch worker
  and executor are absent from the hash, so a change to the vouch path leaves
  the persisted binding identity unchanged.
- `adapters.py` imports eleven top-level app modules plus three lazy
  in-function imports to dodge cycles, reads raw workspace dicts directly,
  re-projects the same RCM row four different ways, and hand-builds the
  planning dict five times. Every such edit moves a `context_manifest_hash`,
  which rejects persisted proposals and re-bills.
- Capabilities do not write, but the boundary is convention: `reporting.py`
  reads `WorkingPapers/<id>.json` from the filesystem directly, `_shared.py`
  calls the private `workspace._table_signature` and lazily imports the private
  `findings._test_rcm_id`, and capabilities freely import domain services that
  persist. The boundary test bans imports; it cannot see these.
- Readiness is "existence and structural usability only" for planning and
  reporting. `capabilities/documents.py` (1,730 lines) also owns chunking,
  visual-page routing, media specs, and budget sizing; `tests.py`'s
  `_specified_ready` computes a five-way diagnosis of unvouched types, which is
  a report, not a verdict.

### 3.3 Real runs contradict the serialization rationale

The document defends the sequential barrier on `documents.evidence_read` as
"the mechanism": a per-document read can only agree with its siblings about a
vocabulary if it can see what they settled. On the 84-document engagement
(`treasuryfull`) both full document runs died with `run time limit reached`
inside `evidence_read`, leaving 69 and then 43 units queued. The auditor
restarted each as a fresh run with a narrower scope.

Document types have independent vocabularies (one master per type), so the
mechanism only requires serialization *within* a type. Serial within type and
parallel across types keeps the argument and fits the deadline.

Related: stages of 231 and 245 units appear in recorded runs, close to the
250-unit cap that `ensure_stage_units` re-applies mid-run, where it fails the
whole run rather than refusing at routing.

### 3.4 Proposal reuse has never fired

Across 70 runs and 1,033 model calls there is not one `proposal_reused` or
`proposal_reuse_rejected` event in any `telemetry.db`. Sidecars are keyed per
run; a run that fails is abandoned and a new one started from `next_outcomes`,
and a new run has an empty sidecar folder. In practice, recovery is readiness
skipping per-document units, not proposal identity.

The eight-field `ProposalExecutionIdentity` costs a re-bill every time a
prompt, preset, adapter projection, or model profile changes, and the codebase
is edited daily. It has delivered no saved call. Either "Continue" should
resume the same run in place, or sidecar identity should be scoped to
workspace and unit rather than run, or the machinery should be simplified.

### 3.5 Partial dependencies are declared in the wrong place

Edges are in `workflows/audit.py`. Whether an edge is partial is a separate
table, `_PARTIAL_DEPENDENCIES` in `audit_execution.py`, keyed by capability,
not cross-checked at startup, and covered by one test out of fourteen entries.
The document's own rule ("a failure in the dependency must not destroy work
the dependent can still do") is already violated: `documents.analysis_chunks_ready`
is partial on `text_ready` but blocking on `categorized`, so one failed
category unit withholds all chunk analysis.

A recorded run (`treasuryfull/20260902-132206-5ac436`, on an earlier graph
revision) shows the cost of the pattern: one failed ruleset proposal blocked
29 rows of test generation and, downstream, 17 promotions.

### 3.6 Budgets are not the binding constraint; the deadline is

Recorded runs use a small fraction of their ceilings (one used 103 of 852
turns). Meanwhile:

- `document_turns` has three formulas: the document in one place, the route
  installer in another, and the audit refresh in a third with no base and no
  preparation term.
- The audit refresh drops `analysis_turns` entirely; grow-only hides the
  shrink but analysis growth mid-run never raises the budget.
- Grow-only is forever: a budget inflated by a transient count never comes
  down, and the installer takes `max(existing, …)` for token limits.
- The deadline is an in-memory value seeded at construction, never persisted.
  A resume gets a fresh 3,600 s; every child run of a steering loop gets its
  own, and the loop adds the child's wall time to its own deadline, so a loop
  request's total wall bound is effectively open.
- Four retry layers stack: `llm.chat` transport retries (3), the gateway's
  unusable-completion retry (1), worker repairs (1–2), and unit attempts (2).
- Hitting the unit attempt cap raises `LimitExceeded`, which ends the whole
  run as `failed`. That contradicts the all-settled philosophy stated for
  every other failure.
- `LimitExceeded` ends a workflow run as `failed` but a loop run as
  `completed_with_open_items`.
- Estimated and actual tokens are checked against the same cap, and the
  post-call check raises before returning content, discarding a paid-for
  completion.

### 3.7 Durability has thread-safety gaps

- **Admission race.** `start_command_run` reads `live_handles()` and `_launch`
  inserts later with no lock spanning both; two concurrent requests for one
  workspace can both pass the one-live-run check.
- **Lost update on `run.json`.** `submit_approval_response` and
  `submit_interaction_response` load-mutate-save the record from the API
  thread while the worker thread holds in-memory authority; the worker's next
  `save()` overwrites whatever the API wrote except what reached it through
  the handle.
- **Unlocked mutation.** In the parallel path `_reference_recorder` sets a
  unit field outside `_state_lock` before calling `save()`, while another
  thread may be serializing `self.run` under the lock.
- **Cancel latency.** Cancel and deadline are observed only at checkpoints. A
  call blocked on the provider semaphore or inside `llm.chat` is
  uninterruptible, and the heartbeat timer starts before semaphore
  acquisition, so a queued call is reported and billed as latency.
- **Pause then cancel.** `cancel_run` sets `resume` to unblock a pause; the
  checkpoint loop exits without re-checking `cancel`, flips status to
  `executing`, and raises only at the next checkpoint.
- **Recovery is lazy.** `recover_orphans` runs only when someone starts or
  resumes a run or hits two routes, not at process start.

### 3.8 "Content is never persisted" is true of the run and false of the workspace

The run record and its sidecars are content-free apart from proposals and
rejections. But the debug store writes every provider request and raw response
into the workspace's `telemetry.db` (`llm_calls`), sanitised only for secrets
and image bodies. That is why the treasury engagement's database is 58 MB.
Nothing leaves the machine, so this is not a provider-privacy issue; it is a
local-footprint and a "what is durable" issue the privacy story should state.

The one-provider-call-site test scans `app/agent` only. `app/report.py`,
`app/assistant.py`, and `app/documents.py` call `llm.chat` directly, and
`document_context.get_document_context`, which `adapters.py` calls, lives in
that unguarded layer.

The structural privacy mapping constrains declarations, not adapters. Adapters
choose the representation label: RCM requirements travel as
`planning_context`, observation/row/test/execution projections as
`current_artifact` under `allow_document_text`. `allow_document_text` is
effectively "any JSON projection", and four analysis presets declare it with
no source that needs it.

### 3.9 Unexercised branches

- All 70 recorded runs are **auto** mode. The single permission-mode run was
  cancelled at its first stage. Per-proposal approval, scope checkpoints, and
  the `tests.cycle_ruleset_approved` gate as a permission-mode no-op have no
  recorded execution.
- No run was resumed in place (`resume_run`), so `workflow.recovery` and
  receipt reconciliation have only their unit tests.
- `documents.policies` is a registered preset with no consumer.

## 4. What the recorded runs show

Five engagements, all on one provider and model
(`openrouter` / `deepseek-v4-flash`), 70 runs between 1 and 6 September 2026.

**Run outcomes**

| Status | Runs |
| --- | --- |
| completed | 36 |
| completed_with_failures | 12 |
| completed_with_open_items | 8 |
| failed | 8 |
| cancelled | 7 |

**Spend by stage** (share of 7.97 M prompt tokens)

| Stage | Calls | Prompt share | Mean latency |
| --- | --- | --- | --- |
| test_generate | 168 | 24% | 46 s |
| analysis_promotion | 84 | 16% | 19 s |
| finding | 112 | 14% | 13 s |
| loop | 34 | 7% | 7 s |
| analysis_definitions | 23 | 6% | 22 s |
| rcm | 19 | 4% | 113 s |
| cycle_linkage | 17 | 4% | 130 s |
| document_evidence_read | 110 | 4% | 38 s |
| analysis_summary | 6 | 4% | 129 s |

**Unit failure modes** (top causes across all runs)

| Cause | Units |
| --- | --- |
| blocked by an unsettled dependency (`tests.specified`, `tests.promoted_from_analysis`) | 70 |
| `reporting.finding` invalid after 2 attempts (empty Condition section, missing title line) | 15 |
| `tests.generate` invalid after 3 attempts (population/grain, disallowed code) | 7 |
| evidence-blocked document test (no document supplied) | 7 |
| empty completion (`finish_reason: stop`, empty message) | 8 |
| `planning.rcm` invalid after 2 attempts (theme no row owns) | 2 |
| `tests.cycle_linkage` invalid after 2 attempts (comparisons unanswered) | 2 |
| executor code bug (`ExecutorReconciliation` keyword), since fixed | 2 |

Readings:

- Validator exhaustion, not transport, is the dominant failure. The
  rejection-and-repair design is doing real work; the finding worker's two
  failure modes are both format contracts the template check enforces.
- Empty completions are a recurring provider behaviour and the gateway's
  single unusable-retry does not always absorb them.
- The graph changed repeatedly across the five days (five distinct
  `definition_hash` values on audit runs), and `workflows/audit.py` has 29
  commits since 22 July. Every change re-keys persisted proposals, which is
  consistent with reuse never firing.
- The two deadline failures are both `documents.evidence_read` on the
  84-document engagement (§3.3).

## 5. Recommendations

In order.

1. **Make invalidation explicit.** Have `materialize` consume `invalidate_on`
   (or edge-level annotations) instead of the "dependency will materialize"
   cascade, so a satisfied capability is redone only when something it
   declares it reads has moved. Then either delete `planning.change_assessed`'s
   edge-less workaround or document it as the intended shape.
2. **Move partial flags into `DEPENDENCIES`.** An edge should say whether it
   is partial where it is declared, validated at startup with the rest of the
   graph, and tested per edge. Fix the `analysis_chunks_ready → categorized`
   edge while there.
3. **Decide what proposal identity is for.** Wire "Continue" to resume the
   same run, or scope sidecars to workspace and unit, or drop the identity
   fields that are not earning their re-bill.
4. **Fit `evidence_read` to the deadline.** Serial within a document type,
   parallel across types. Persist the deadline, or size it from unit counts.
5. **Consolidate the execution adapters.** One adapter per run, one `ws`, one
   `_refresh_dynamic_limits`, one budget formula, binders that share the
   refresh/scope/parent-hash prelude. Fold the cycle-vouch worker into the
   fieldwork binding hash.
6. **Fix the run-record write path.** API-side responses should go through
   the live handle's inbox when one exists, never a parallel load-save. Take
   the admission check and insert under one lock. Make a unit's attempt cap
   fail the unit, not the run.
7. **State the telemetry footprint** in the privacy section and cap or rotate
   `llm_calls`.
8. **Exercise permission mode and in-place resume** in an integration test
   against a real workspace before either is relied on.

## 6. Smaller items

- `CapabilityGroupView`'s docstring says the audit graph declares "three of the
  four" document capabilities; it declares seven of eight.
- Stale comments still describe a pending route (`runner.py`, `store.py`) and
  list "the action-graph scheduler" as an engine; `store.new_command_run`
  still seeds action-graph limits and `run_summary` counts tasks and actions.
- `routing.py` imports every workflow, `doc_tests`, and the capability
  packages, and holds all four budget formulas; `runner.py` hosts 115 lines of
  evidence-request matching. Neither is what its name says.
- Routing materializes the full closure and loads every doc-test file on the
  request thread.
- Per-worker usage accounting and the UI stage label derive from parsing
  `[agent:…]` out of the system prompt's first line.
- The unit pipeline reaches into `gateway.run["model_profiles"]` and
  `runtime.run["parent_run_id"]` by duck-typed `getattr`; the worker registry
  mutates shared `gateway.context` attributes around each call.
- Proposal-only units skip readiness re-evaluation, so a proposal-only
  capability is never checked for "committed but not satisfied".
- `commit_local` returns an empty manifest reference, so `schemas_stamped`
  units carry no `context_manifest`.
- The `checkpoints` / `checkpoint_files` restore points in `telemetry.db`
  (16,820 file rows on one engagement) are not mentioned in either
  architecture document.
