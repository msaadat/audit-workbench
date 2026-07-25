# Protocol Runner Decisions

## Purpose And Scope

[agent-architecture.md](agent-architecture.md) applies one strict test to every
runner that is not `WorkflowRunner` or `ActionRunner`:

> Retain a separate runner only when it requires a genuinely different
> scheduling and control protocol. Otherwise migrate its work into capabilities
> and workers.

File count, historical separation, and "it already works" are explicitly not
sufficient justification. This document is the required Phase 10 decision record
for the two runners that survived Phase 9: `IntakeRunner` and `DocTestRunner`.
It records what each runner's scheduling protocol actually is, measures it
against the workflow scheduler's own contract, and states the decision plus what
changed as a result.

Written 2026-07-25 for `P10.1` and `P10.4`. The implementations are `P10.2`,
`P10.3`, `P10.5`, and `P10.6`.

## The Measuring Stick

`WorkflowRunner` is not "a loop that runs things". It is a specific scheduling
protocol, and a runner fits it only if every one of these holds:

1. **Readiness is a deterministic function of the durable subject.**
   `workflow.materialize(...)` prunes a capability whose `readiness()` is already
   satisfied and expands the rest into semantic units. Both are evaluated against
   the `Workspace`.
2. **Work fans out into semantic units.** Unit IDs are stable across
   re-expansion, so a resumed run recomputes the same work set rather than
   replaying a recorded plan.
3. **Units are dependency-ordered across stages and independent within one.**
   Parallelism, all-settled folding, and re-expansion between stages are the
   scheduling capabilities the runner provides.
4. **Model work produces a proposal; mutation is a separate guarded commit.**
   `UnitPipeline` persists a content-free manifest, then a proposal, then
   optionally takes an approval, then commits through a registered executor under
   CAS or parent hashes, then persists a receipt.

A runner that satisfies all four gains something real by migrating. A runner that
satisfies none of them gains only indirection.

## Decision 1 — Intake: retain a thin protocol runner

**Decision:** retain `IntakeRunner` as a distinct protocol runner with the
explicit engine value `intake`, converted onto `RunRuntime`, `ModelGateway`, and
declared context/worker contracts.

### What the intake protocol actually is

One durable *batch*, staged under `Workspaces/<id>/Imports/<batch_id>/`, moving
through `uploading → classifying → awaiting_approval → applying → completed`.
The run joins that protocol partway through: the browser has already compared a
folder manifest (`intake.compare_manifest`), uploaded each staged file over its
own HTTP endpoint, and called `intake.complete_upload`. The run then classifies,
optionally asks the auditor to edit the proposed routing, applies, and verifies.

### Measured against the workflow contract

| Requirement | Intake | Verdict |
|---|---|---|
| Readiness from the durable subject | The authoritative state is the batch record under `Imports/`, not the `Workspace`. The batch is identified only by `context.batch_id`, and it is created and advanced by a separate upload protocol the run neither schedules nor can re-derive from workspace state. | **Fails** |
| Semantic unit fan-out | There is exactly one unit at every step. Classification is one bounded model turn over the whole batch payload (`intake.classification_payload_for_model`); application is one `intake.apply_batch(...)` call. | **Fails** |
| Dependency-ordered stages with intra-stage parallelism | The five steps are strictly linear with one unit each. Stage ordering, all-settled folding, and between-stage re-expansion have nothing to order. | **Fails** |
| Proposal, then guarded commit | Genuinely applicable — and the only part that was missing. See below. | **Partially applies** |

Fanning classification out per staged file would multiply provider turns by the
file count and lose the cross-file consistency the single payload gives the
model (duplicate detection, sibling naming, folder structure). That is a
regression dressed as an architecture win.

### Why `apply_batch` is not an executor commit

`intake.apply_batch(...)` copies staged files into the workspace and *creates*
the tables and documents. There is no pre-existing artifact whose parent hash a
CAS guard could pin — the artifacts do not exist until the call runs. Its
idempotency is structural instead: an item that already carries a `target_ref` is
skipped, an unchanged `sha1` at a known path resolves to the existing target, and
a `completed` batch returns immediately. Re-running a partially applied batch
therefore converges rather than duplicating. Recasting that as a registered
executor with revision CAS would mean rewriting `intake.py`'s persistence and
index bookkeeping — a workspace-schema change this migration explicitly excludes
from structural phases.

### What changed anyway (`P10.2`)

Retention is not a licence to keep a runner outside the target contracts. The
parts of the target architecture that *do* apply to a single-unit protocol were
adopted:

- **`RunRuntime` by composition.** `IntakeRunner` takes an explicit optional
  `runtime` constructor dependency, matching `ActionRunner` and `WorkflowRunner`.
- **A registered worker.** File classification moved out of the runner into
  `workers.intake:intake.classification`, with a hash-identified prompt, a
  hash-identified response schema, semantic validation, and the registry's
  bounded repair loop. The runner no longer owns a prompt.
- **A declared context spec.** The model sees only what the `intake.classification`
  preset declares, resolved by `ContextResolver` through the new `staged_files`
  source type and `file_metadata` representation. The privacy permission
  `allow_file_metadata` is deny-by-default like every other content class, and the
  content-free `ContextManifest` is persisted before the provider call.
- **A durable proposal before approval.** Classification runs as a *proposal-only*
  `UnitPipeline` unit (`executor_id=None`, the shape Phase 9 introduced for chunk
  analyses). A restart after the model call reuses the proposal sidecar instead of
  re-billing, and the auditor-edited accepted proposal is durable before
  `apply_batch` touches a file.

The human review of a staged manifest stays an explicit approval batch with
editable per-file specs, which is what `ImportDialog.vue` renders.

### Recorded consequence

Batch identity, local file operations, and idempotent application are unchanged.
The one behavior difference is that a provider error or an unusable response now
falls back to deterministic local routing *after* the registry's bounded repair
attempt rather than immediately — the same warning, one extra chance to succeed.

## Decision 2 — Document tests: migrate to `WorkflowRunner`

**Decision:** migrate. `DocTestRunner`, the `doc_test` run kind, and the
`doc_test` engine value are deleted. Standalone document-test execution is the
declared `doc_tests_workflow_v1` graph on the domain-neutral scheduler.

### What the document-test protocol actually was

`DocTestRunner` loaded one durable Document Test, created one task per item,
iterated the items in order, skipped items already `confirmed`/`exception`,
called `doc_tests.run_item(...)` for the rest, and rolled the results up. That is
fan-out over semantic items with skip-on-completion resume — the scheduler's own
description of itself.

### Measured against the workflow contract

| Requirement | Document tests | Verdict |
|---|---|---|
| Readiness from the durable subject | A Document Test is a durable workspace artifact. Structural executability is `doc_tests.execution_issues(...)`, execution is "no item is still pending", disposition is "every item carries the auditor's own decision". All three are deterministic reads. | **Fits** |
| Semantic unit fan-out | One unit per test, per item, or per item/document Q&A pair, keyed on IDs that are already stable. | **Fits** |
| Dependency-ordered stages | Definition readiness gates execution, which gates disposition. | **Fits** |
| Proposal, then guarded commit | Already true in the audit graph: Q&A answers go through the registered `fieldwork.document_qa` worker and executor. | **Fits** |

The decisive evidence is that the audit lifecycle *already* schedules document
tests this way. `fieldwork.executed` expands `document_test_execution`,
`document_qa_execution`, and `document_test_review` units and binds them through
`UnitPipeline` and the registered fieldwork executors. Retaining `DocTestRunner`
would have kept a second implementation of work the scheduler was already doing —
and a worse one, since the runner reached the provider through
`doc_tests.run_item(..., model_adapter=...)` rather than through a registered
worker with a declared context spec.

### The declared graph

```text
doc_tests.definitions_ready
-> doc_tests.executed
-> doc_tests.dispositioned
```

| Outcome | Persisted artifact | Readiness satisfied when |
|---|---|---|
| `doc_tests.definitions_ready` | The existing Document Test record | Every scoped test is structurally executable (`execution_issues` empty) |
| `doc_tests.executed` | The existing per-item result fields, evidence anchors, and Q&A answers | No scoped item is still `pending` |
| `doc_tests.dispositioned` | The existing per-item auditor state | Every scoped item is `confirmed` or `exception` |

Nothing the agent does satisfies `doc_tests.dispositioned`. Its units settle as
`awaiting_confirmation`, for the same reason `documents.analysis_reviewed` does:
a deterministic comparison or a cited answer is a candidate for auditor judgment,
not the judgment.

### One implementation, two graphs

The document-test unit kinds are bound in exactly one place —
`agent/doc_tests_execution.py` — and both compositions use it, following the
Phase 9 precedent where the audit composition binds the document capabilities
through `DocumentWorkflowExecution`. `audit_execution.py::_bind_execution` keeps
ownership of the datatest branch and the RCM-linked task/stage identity, and
delegates every document-test unit kind to the shared binder. There is one
`run_document_test` deterministic path, one `fieldwork.document_qa` worker, and
one `fieldwork.document_qa` executor across both graphs.

### Recorded deviations

**(a) No model call for a non-Q&A test.** `DocTestRunner` passed
`model_adapter=self.model_adapter` into `doc_tests.run_item(...)`, so a `qa`
Document Test reached `documents.document_chat` from inside the leaf runner. The
workflow expands a Q&A test into `document_qa_execution` units instead, so the
answer comes from the registered worker, the declared page context, and the
injected gateway. `executors.fieldwork.run_document_test(...)` raises rather than
making an unbudgeted provider call, which is now a contract the standalone path
shares with the audit path.

**(b) Scope with nothing named.** `DocTestRunner` was always given exactly one
`test_id`, and the live endpoint still names one. A request that names none falls
back to every test that still has outstanding work, bounded by
`MAX_SCOPE_TESTS`, and warns when it truncates. No scope interaction was added:
unlike the document library, there is no live caller that can produce an
ambiguous document-test scope.

**(c) The run-level rollup summary moved.** `run["doc_test"]["rollup"]` was a
leaf-runner projection. The rollup is recomputed from the workspace in the
workflow's finish projection and rendered into `summary_markdown`; the per-test
`status` transition that `doc_tests.result_rollup(...)` drives is unchanged and
still owned by `run_document_test`.

### The overlap left open here — settled by `P11.2A`

Phase 10 left the registered `run_document_test` action alongside the declared
`doc_tests.executed` outcome, because choosing between them is a routing
decision. `P11.2A` **removed the action** rather than narrowing it, for two
reasons:

- Executing a Document Test is a fan-out of per-item units, which is exactly
  what `doc_tests.executed` schedules through `bind_document_test_unit`. Keeping
  the action would have kept a second execution implementation of work the
  scheduler already does — the same argument that decided the migration above.
- Narrowing it to non-Q&A tests would still have left a duplicate deterministic
  comparison path, and the Q&A hole was not hypothetical: the action called
  `doc_tests.run_item` with no model adapter, so a Q&A worklist reached
  `documents.document_chat` outside the registered `fieldwork.document_qa`
  worker, its declared page context, and the run's model budget.

The `document_testing` goal template went with it. It conflated two
workflow-owned requests and is replaced by `document_test_preparation`
(`fieldwork.definitions_ready`) and `document_test_execution`
(`doc_tests.executed`); `DocTestsTab.vue` sends those, and the Run button names
the test through run context. Target-specific Document Test operations — create,
edit, delete, attach, detach, update comparisons, record a disposition — remain
registered actions.

## Consequences For The Target Schema

| Engine | Disposition after Phase 10 |
|---|---|
| `workflow` | Target scheduler |
| `action` | Target scheduler |
| `intake` | **Retained** by this decision record; a justified protocol engine in the target schema |
| `doc_test` | **Deleted** |
| `analysis` | **Deleted** in Phase 12 with the `_Runner` pipeline itself; exploratory analysis is the declared `analysis_workflow_v1` graph |

`P11.1` finalized the supported engine set against this table and `P12.2`
narrowed it to its final value:
`store.RUN_ENGINES == {workflow, action, intake}`. A record whose
engine is absent or outside that set fails closed in
`routing.dispatch_engine(...)`, and nothing infers an engine from `kind`,
`schema_version`, or record contents. A command run created before routing has
no engine at all; it carries `route.status == "pending"` until
`routing.resolve_pending_route(...)` selects one.
