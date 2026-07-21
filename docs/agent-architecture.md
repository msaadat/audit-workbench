# Agent Architecture

## Purpose

The agent framework separates runtime orchestration from domain dependencies,
context selection, model prompting, and workspace mutation. The architecture
has two scheduling engines:

- `WorkflowRunner` executes declared capability dependency graphs.
- `ActionRunner` executes small model-generated DAGs of registered isolated
  actions.

Domain concepts such as APM generation, RCM generation, table analysis, and
document analysis are capabilities and workers, not specialized runners.

## Clean-Slate Cutover Assumption

The migration assumes there are no workspaces or agent runs that must survive
the cutover. Existing `Workspaces/` contents are disposable and must be removed
or moved outside the application data root before the new architecture is used.
The target reads and resumes only records created by the target schema; it does
not infer, convert, display, or resume v1, v2, v3, or protocol-specific legacy
run shapes.

This is a persistence clean slate, not permission to weaken durability. Runs
created by the target architecture still support crash recovery, proposal and
receipt reconciliation, approvals, interactions, pause/resume, queued commands,
and SSE replay. One explicit `engine` field selects the current scheduler. No
engine-schema version, historical compatibility classifier, or legacy adapter
package is required before the first release.

## System Flow

```text
Request
  -> Router
  -> WorkflowRunner or ActionRunner
  -> ContextResolver
  -> Worker
  -> proposal sidecar / approval
  -> Executor
  -> readiness reevaluation
  -> next unit or completion
```

## Components

| Component | Responsibility |
|---|---|
| `RunRuntime` | Persistence, events, budgets, checkpoints, pause/cancel, deadlines, approvals, and provider accounting |
| `WorkflowRunner` | Materialize and schedule declared capability dependency graphs |
| `ActionRunner` | Execute small model-generated DAGs of registered isolated actions |
| `Capability` | Specify an outcome: dependencies, readiness, units, context, worker, executor, and approval |
| `ContextResolver` | Build bounded context strictly from a capability's `ContextSpec` |
| `Worker` | Build prompts from the supplied context bundle and validate model output |
| `Executor` | Deterministically commit accepted proposals with CAS and receipts |
| `Router` | Resolve a request into requested outcomes, isolated action intent, clarification, or an unsupported response |

Runners use `RunRuntime` through composition. Domain-specific runners do not
inherit from each other.

## Capability Contract

A capability is a complete, hash-identified, auditable declaration of an
outcome.

```python
Capability(
    id="planning.apm_ready",
    depends_on=("planning.context_ready",),
    readiness="planning.apm_usable",
    expand_units="workflow.single_workspace",
    context="planning.apm_context",
    worker="planning.apm",
    executor="planning.commit_apm",
    approval="approval.artifact_change",
)
```

The capability declares what context is permitted. It does not gather context,
call the model, or mutate the workspace. All executable behaviors use stable
registry keys. Runtime callables are resolved from those keys and are not
serialized as identities. The capability identity is a hash of the normalized
declaration and the content/implementation hashes of its registered components;
manual `_v1` suffixes and component version counters are not required.
The initial target uses one persisted shape selected by explicit engine and does
not carry a schema-version field before release.

## Context Model

| Object | Meaning |
|---|---|
| `ContextSpec` | Declares permitted and required context sources, selection strategies, representations, privacy rules, and budgets |
| `ContextResolver` | Implements deterministic and bounded automatic selection from the declaration |
| `ContextManifest` | Records what was selected, omitted, truncated, and supplied |
| `ContextBundle` | Contains the bounded material supplied to a worker |

String presets such as `documents.policies` may provide concise declarations,
but they must compile into a normalized typed `ContextSpec`. Automatic
selection must use a named, registered strategy with a normalized configuration
and implementation hash rather than an opaque `auto` flag.

Automatic selectors are local, deterministic resolver components. They cannot
call `ModelGateway`, a provider model, or a network service. A selector may use
stable metadata, lexical scoring, or a hash-identified local embedding/index
whose model and index hashes participate in its identity. It must use
deterministic tie-breaking. Selection that requires provider-model judgment is
a separate worker capability with a persisted proposal, not a resolver strategy.

Each model call persists a local context manifest containing:

- Capability and unit identifiers.
- Context specification and resolver hashes.
- Selected source references and source hashes.
- Deterministic and automatically selected sources.
- Selection reasons and strategies.
- Omissions and truncations.
- Supplied character or token estimates.
- Privacy decisions and representations.

The worker may only use the resulting `ContextBundle`; it cannot retrieve
undeclared sources.

## Explicit Regeneration And Integrity

Generalized freshness assessment, invalidation watches, and automatic stale
propagation are deferred. The framework determines whether an outcome exists and
is structurally usable; it does not decide whether an existing audit artifact is
substantively current. A satisfied outcome therefore reports
`currency_status="not_assessed"` rather than claiming that it is current. The
auditor decides when changed sources or methodology warrant regeneration.

Workflow materialization has two generation modes:

- `reuse_existing` is the default. Existing structurally usable outcomes are
  reused, missing prerequisites are generated, and automatic context selectors
  do not rerun merely because workspace inputs changed.
- `force` is selected by an explicit improve, generate-again, regenerate, or
  refresh instruction. It rematerializes the requested outcome closure using
  context resolved at execution time.

This deferral does not weaken execution integrity. Uncommitted proposal sidecars
are reused only when their exact execution identity still matches the capability
definition, context policy, selected-source hashes, prompt, worker, response
schema, unit input, and proposal schema. Executors still enforce parent hashes,
CAS, auditor-edit preservation, receipts, and interrupted-commit reconciliation.
A changed target produces a conflict instead of a silent overwrite.

Pre-cutover runs and artifacts containing `stale`, `workflow_parent_sha1`, or
similar currency fields are unsupported and removed with the disposable
workspace root. Target scheduling does not create a change-domain registry,
propagate stale states, or automatically regenerate downstream artifacts.

## Worker Contract

A worker owns the model-facing behavior that is expected to change frequently:

- Context-to-message transformation.
- System and user prompts.
- Structured response schema.
- Response validation and bounded repair guidance.

A worker does not schedule units, persist run state, request arbitrary context,
or mutate the workspace. All provider calls go through `ModelGateway`, which
applies budgets, concurrency controls, retries, and provenance accounting.

## Executor Contract

An executor applies an accepted proposal deterministically. It owns:

- Parent-hash and CAS validation.
- Preservation of auditor-owned changes.
- Domain validation.
- Workspace mutation.
- Artifact references and receipts.

An executor cannot call the model or schedule additional work.

## WorkflowRunner

`WorkflowRunner` is domain-neutral. Given requested outcomes and a capability
registry, it:

1. Computes the transitive dependency closure.
2. Evaluates existence and structural readiness.
3. Expands semantic work units.
4. Schedules ready units in dependency order.
5. Runs independent units under bounded concurrency.
6. Resolves declared context and calls the selected worker.
7. Persists proposals before approval or commit.
8. Invokes the selected executor serially and conflict-aware.
9. Re-evaluates readiness and determines remaining outcomes.

The runner knows nothing about APMs, RCMs, analyses, documents, findings, or
reports.

### Audit Workflow

```text
planning.context_ready
  -> planning.apm_ready
     -> planning.rcm_ready
        -> planning.planned_tests_ready
           -> fieldwork.definitions_ready
              -> fieldwork.executed
                 -> results.rolled_up

results.rolled_up -> findings.drafted
results.rolled_up -> working_papers.generated
results.rolled_up -> dashboard.curated
planning.apm_ready -> report.working_draft
results.rolled_up -> report.working_draft
findings.drafted -> report.working_draft
working_papers.generated -> audit.verified
dashboard.curated -> audit.verified
report.working_draft -> audit.verified
```

This is the baseline graph migrated from the current registry in Phase 7; its
parallel branches are intentional. Phase 9 may change the graph definition hash
to add scoped
`documents.analysis_generated` dependencies where a capability declares them,
but document analysis is not a global prerequisite for every audit. The
authoritative executable audit lifecycle exists only in `workflows/audit.py`.

### Exploratory Analysis Workflow

```text
data.relationships_inferred
-> data.joins_ready
-> analysis.definitions_ready
-> analysis.executed
```

This workflow handles requests such as "review these two tables, infer relevant
joins, and perform useful analysis." It uses the same scheduler as the audit
workflow but a different requested outcome set.

### Document Analysis Workflow

```text
documents.text_ready
-> documents.analysis_chunks_ready
-> documents.analysis_generated
-> documents.analysis_reviewed
```

Document map and reduce operations are separate unit types with separate
workers. The runner owns chunk fan-out, concurrency, progress, resumption, and
reduce ordering. A generated analysis and an auditor-reviewed analysis are
distinct outcomes.

## ActionRunner

`ActionRunner` handles bounded imperative requests that do not represent
durable workflow outcomes. Examples include:

- Attach a document and rerun a test.
- Create a saved analysis and pin it to the dashboard.
- Rename an artifact.
- Remove a validation rule.
- Reconcile a specific report edit.

A bounded planner may generate an action DAG from registered action types. The
action ledger validates dependencies, targets, preconditions, idempotency, and
receipts. It contains no audit lifecycle policy.

## Routing

Routing first uses deterministic templates and phrase mappings. A bounded
router worker handles unresolved commands without performing any work.

| Request | Route |
|---|---|
| Prepare the RCM | `WorkflowRunner` requesting `planning.rcm_ready` |
| Analyze these two tables | `WorkflowRunner` requesting `analysis.executed` |
| Analyze these documents | `WorkflowRunner` requesting `documents.analysis_generated` |
| Complete the audit | `WorkflowRunner` requesting `audit.verified` |
| Attach this file to that test | `ActionRunner` |
| Pin this analysis | `ActionRunner` |

The routing rule is:

> Requests for durable outcomes use `WorkflowRunner`. Requests for specific
> operations on artifacts use `ActionRunner`.

Apply that rule using this precedence:

1. An explicit registered outcome, goal template, or lifecycle-wide completion
   request routes to `WorkflowRunner`.
2. Improve, generate again, regenerate, or refresh a workflow-owned deliverable
   routes to `WorkflowRunner` with the relevant outcome and `force` mode.
3. Explicit CRUD, attachment, pinning, manual edits, or execution of one
   identified existing test routes to `ActionRunner`.
4. Scope-wide execution such as declared RCM fieldwork routes to
   `WorkflowRunner`; target-specific reruns remain actions.
5. A compound request that genuinely requires both engines is clarified or
   split into separately persisted queued runs. Neither scheduler calls the
   other.

An artifact name alone does not decide the engine: "regenerate the APM" is a
workflow request, while "replace this APM paragraph" is an action. The action
catalog may contain bounded model-backed operations such as running one named
document test, but it cannot generate or refresh an artifact family owned by a
registered workflow outcome.

## Persistence And Recovery

Target runs persist under `Workspaces/<id>/AgentRuns/<run_id>/`. A run records:

- Materialized capability stages and semantic units, or an action DAG.
- Unit and action transitions.
- Context manifests.
- Proposal sidecars.
- Approval and interaction records.
- Executor receipts.
- Exact proposal execution identity.
- Definition, prompt, worker, context-policy, selector, and source hashes.
- `reuse_existing` or `force` generation mode.
- Provider usage and hash-only provenance.

Proposals are persisted before commit. On recovery, the runner reuses sidecars
only when their exact execution identities remain valid, reconciles interrupted
commits, and resumes only incomplete units. It does not assess committed-artifact
currency as part of recovery.

## Package Structure Authority

The authoritative target package tree and its incremental creation order are in
[Section 7 of the implementation plan](agent-architecture-implementation-plan.md#7-authoritative-package-migration).
This architecture document defines responsibilities and import boundaries; it
does not maintain a second file-by-file tree. Modules are grouped by
responsibility and then by domain. Avoid creating one file or runner per
capability unless its size independently justifies it.

## Architectural Invariants

- Runtime modules cannot import audit-specific implementations.
- Workers cannot schedule work, mutate workspace state, or gather undeclared
  context.
- Executors cannot call the model.
- Capabilities cannot perform work directly.
- `ContextResolver` cannot exceed declared source, privacy, representation, or
  size policies.
- The audit lifecycle is declared in one place.
- Every model call goes through `ModelGateway`.
- Every proposal is persisted before commit.
- Every commit uses parent hashes or CAS and produces a receipt.
- Proposal sidecars are reused only when their exact execution identity matches.
- Existing committed outcomes are reused unless the auditor explicitly selects
  `force`; their currency is not assessed by the framework.
- Provider provenance stores hashes and metrics, not row-level data or document
  text.

## Migration Direction

The final target has two scheduling engines: `WorkflowRunner` and
`ActionRunner`. Existing domain runners such as the legacy analysis runner,
`DocumentAnalysisRunner`, and `DocTestRunner` migrate into capabilities,
workers, and executors unless they demonstrably require a different scheduling
protocol. Temporary delegation may keep active callers working between commits,
but it does not read legacy persisted records and is deleted in the same phase
that migrates the last live caller. Duplicate execution logic is not retained.

## Handoff Context

This section records the analysis and decisions that led to the target
architecture. It is intended to make this document sufficient context for a
new implementation thread.

### Current Implementation

The repository currently contains three generations of general agent planning:

| Generation | Implementation | Plan source | Work unit | Scheduler |
|---|---|---|---|---|
| v1 | `_Runner` in `agent/runner.py` | Hard-coded stages | Task | Straight-line stage calls |
| v2 | `ActionRunner` | Model-generated action DAG | Action | Priority loop over the action ledger |
| v3 | `WorkflowRunner` | Capability registry closure and readiness | Semantic unit | Dependency-ordered stages with bounded parallelism |

The current audit path is v3. `WorkflowRunner` inherits from
`ActionRunner` and currently reuses planning context, quality checks, proposal
formatting, approvals, and RCM matching through that inheritance. This sharing
is intentional, but the inheritance also exposes the entire v2 scheduler and
its obsolete full-audit logic to v3.

`ActionRunner` remains useful for isolated mutations, including attaching a
document, pinning a dashboard tile, renaming an artifact, or composing a small
bounded series of registered actions. It should not plan or enforce a complete
audit lifecycle.

The small intake, document-test, and document-analysis runners currently have
their own durable run behavior. They are not simply copies of the general graph
runners. The target architecture nevertheless applies a strict test: retain a
separate runner only when it requires a genuinely different scheduling and
control protocol. Otherwise migrate its work into capabilities and workers.

### Phase 1 Deletion Boundary

The v2 full-audit orchestration has been deleted from
`agent/action_runner.py`. The deletion gate covers:

- `_prepare_planning`.
- `_ensure_full_audit_stages`.
- `_validate_full_audit_action_graph`.
- `ORCHESTRATED_FULL_AUDIT_ACTION_TYPES`.
- Full-audit action-budget reserves.
- Full-audit branches in adaptive graph expansion and completion summaries.
- Interpreter and planner prompt clauses concerning `prepared_planning`, the
  execution manifest, and terminal stages owned by the orchestrator.

This machinery existed to force a model-generated action graph through a safe
audit lifecycle. V3 replaced that responsibility with deterministic capability
dependencies. Local routing catches known full-audit phrases and goal templates
before the action interpreter runs, and `ActionRunner` retains a defensive
fail-closed guard for malformed records and bounded-router misses.

The audit lifecycle was encoded in two places at the Phase 1 boundary:

- `ledger.AUDIT_LIFECYCLE_STAGES`.
- `audit_capabilities.build_registry()`.

`build_registry()` is the current authoritative implementation. The action
ledger no longer accepts or invokes lifecycle normalization; its unreachable
constants and enforcement helper remain for the next Phase 2 deletion task.
The target later moves the authoritative declaration to `workflows/audit.py`.

There is also concrete document-analysis duplication. Both
`DocumentAnalysisRunner` and `ActionRunner._ensure_planning_analysis` implement
document extraction, chunk map calls, reduction, validation, and persistence.
The replacement must have one implementation expressed as document-analysis
capabilities, workers, and executors.

### Decisions From The Architecture Discussion

- Runner names represent scheduling algorithms, not audit artifacts. There
  should not be an `APMRunner` or `RCMRunner`.
- APM, RCM, planned tests, exploratory analysis, document analysis, findings,
  and reporting are capabilities executed by the generic `WorkflowRunner`.
- `ActionRunner` is retained for imperative artifact operations and bounded
  model-generated action DAGs.
- `WorkflowRunner` and `ActionRunner` share `RunRuntime` services through
  composition; neither inherits from the other.
- Generic data analysis is an outcome workflow, not merely an isolated action.
  A request to inspect tables, infer joins, and perform useful analyses requests
  `analysis.executed`.
- RCM-linked audit testing requests audit capabilities such as
  `fieldwork.definitions_ready` and `fieldwork.executed` rather than the generic
  analysis workflow alone.
- Document analysis is a capability workflow. Standalone document-analysis UI
  operations and audit planning should request the same underlying outcomes.
- Generated document analysis and auditor-reviewed document analysis are
  separate outcomes so generated summaries are not silently treated as
  approved evidence.
- Dependencies are declarative. An RCM worker does not call an APM worker or
  check whether it should run APM work. The scheduler resolves and enforces the
  dependency closure.
- Context policy is part of the capability definition. Workers cannot silently
  retrieve additional context.
- Automatic context selection must be named, bounded, hash-identified, and
  recorded.
  A bare `auto` selector is insufficiently auditable.
- Model generation and deterministic mutation are separate. Workers return
  validated proposals; executors commit them.

### Context Auditability Requirements

The capability declaration is intended to be the primary auditable and
manually editable specification. It must identify:

- Dependencies, existence/structural-readiness functions, and unit expansion.
- Permitted context presets and representations.
- Required versus optional context.
- Deterministic selectors and bounded automatic selectors.
- Context size and item budgets.
- Privacy permissions, especially the prohibition on sending table rows.
- Worker, prompt, response-schema, and executor hashes.
- Approval policy and output contract.

The normalized `ContextSpec` and its hash must be persisted. Each execution
also persists a `ContextManifest` showing the sources actually selected,
selection reasons, source hashes, omissions, truncations, privacy decisions,
and supplied size. Provider provenance remains hash-only and does not contain
document text or row-level table data.

### Recommended Migration Sequence

1. Add characterization tests for active routing, outcome materialization,
   isolated actions, and same-schema crash recovery.
2. Rename `CommandRunner` to `ActionRunner`, update all live imports, and remove
   the full-audit methods, branches, prompt clauses, and action-budget
   reservations without a persisted-run alias.
3. Remove `AUDIT_LIFECYCLE_STAGES`, lifecycle normalization, and the
   `audit_lifecycle` argument from the action ledger.
4. Introduce `RunRuntime`, `ContextSpec`, `ContextResolver`, `ContextManifest`,
   worker, executor, and capability interfaces without moving all behavior at
   once.
5. Extract planning context and quality behavior from `ActionRunner`; make
   `WorkflowRunner` use the new interfaces directly instead of inheriting from
   `ActionRunner`.
6. Move the audit dependency declaration into `workflows/audit.py` and migrate
   v3 stage handlers into grouped capability, worker, and executor modules.
7. Add the exploratory analysis workflow and route the existing `data_analysis`
   intent to `analysis.executed`.
8. Replace both document-analysis implementations with map and reduce
   capabilities using shared workers and persistence executors.
9. Migrate or retain document-test and intake protocols according to whether
   their scheduling protocols fit `WorkflowRunner`.
10. Migrate every live v1 caller and delete `_Runner`, its fixed stages, and its
    projections. No historical reader or resume path is retained.

Do not combine v1 retirement with the initial v2/v3 cleanup. Removing duplicate
full-audit orchestration is lower risk and should land first.

### Cutover And Recovery Boundary

The cutover begins with an empty application workspace root. Old `run.json`,
action ledgers, workflow stages, chats, debug records, and UI history are outside
the supported input contract and may be deleted with the disposable workspaces.
The application does not contain engine inference, schema translation,
historical run-card projection, or resume paths for those shapes.

Recovery tests begin with a run created by the target schema and exercise every
interruption boundary in that same schema. Source hashes, proposal sidecars,
parent checks, receipts, approvals, and interactions remain durable because they
protect current work from crashes and duplicate side effects, not because they
support pre-cutover records.

### Verification Criteria

The migration is complete when:

- The audit lifecycle has one declaration.
- `WorkflowRunner` contains no APM-, RCM-, document-, analysis-, or report-
  specific handlers.
- `WorkflowRunner` does not inherit from `ActionRunner`.
- The action ledger has no audit lifecycle normalization.
- No legacy run reader, conversion path, compatibility classifier, or
  compatibility package remains.
- Workers cannot access undeclared context or write workspace state.
- Executors cannot call the model.
- New workflow scheduling reuses existing structurally usable outcomes, labels
  their currency as not assessed, and regenerates them only on explicit force.
- Document analysis has one map/reduce implementation.
- Generic table analysis and full-audit requests route to different declared
  outcome sets while using the same workflow scheduler.
- Existing privacy boundaries remain enforced.
- Backend tests and the frontend build pass.

At the time this handoff section was written, the architecture had only been
documented. No framework implementation changes had been made as part of this
architecture discussion.
