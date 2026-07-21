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
| `Capability` | Specify an outcome: dependencies, readiness, units, context, worker, executor, approval, and invalidation |
| `ContextResolver` | Build bounded context strictly from a capability's `ContextSpec` |
| `Worker` | Build prompts from the supplied context bundle and validate model output |
| `Executor` | Deterministically commit accepted proposals with CAS and receipts |
| `Router` | Resolve a request into requested outcomes, isolated action intent, clarification, or an unsupported response |

Runners use `RunRuntime` through composition. Domain-specific runners do not
inherit from each other.

## Capability Contract

A capability is a complete, versioned, auditable declaration of an outcome.

```python
Capability(
    id="planning.apm_ready",
    version=2,
    depends_on=("planning.context_ready",),
    readiness="apm_current",
    expand_units="single_workspace_unit",
    context=ContextSpec(
        sources=(
            "planning.structured_context",
            "documents.policies",
            AutoSelect(
                preset="documents.audit_relevant",
                strategy="document_relevance_v1",
                max_items=6,
            ),
            "methodology.relevant_sections",
        ),
        max_characters=60_000,
        allow_document_text=True,
        allow_table_rows=False,
    ),
    worker="apm_v2",
    executor="commit_apm_v1",
    approval="artifact_change",
    invalidate_on=("planning:context", "documents", "methodology"),
)
```

The capability declares what context is permitted. It does not gather context,
call the model, or mutate the workspace.

## Context Model

| Object | Meaning |
|---|---|
| `ContextSpec` | Declares permitted and required context sources, selection strategies, representations, privacy rules, and budgets |
| `ContextResolver` | Implements deterministic and bounded automatic selection from the declaration |
| `ContextManifest` | Records what was selected, omitted, truncated, and supplied |
| `ContextBundle` | Contains the bounded material supplied to a worker |

String presets such as `documents.policies` may provide concise declarations,
but they must compile into a normalized typed `ContextSpec`. Automatic
selection must use a named, versioned strategy rather than an opaque `auto`
flag.

Each model call persists a local context manifest containing:

- Capability and unit identifiers.
- Context specification and resolver versions.
- Selected source references and source hashes.
- Deterministic and automatically selected sources.
- Selection reasons and strategies.
- Omissions and truncations.
- Supplied character or token estimates.
- Privacy decisions and representations.

The worker may only use the resulting `ContextBundle`; it cannot retrieve
undeclared sources.

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
2. Evaluates readiness and staleness.
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
planning.sources_selected
-> documents.analysis_generated
-> planning.context_ready
-> planning.apm_ready
-> planning.rcm_ready
-> planning.planned_tests_ready
-> fieldwork.definitions_ready
-> fieldwork.executed
-> results.rolled_up
-> findings.drafted
-> working_papers.generated
-> dashboard.curated
-> report.working_draft
-> audit.verified
```

The authoritative audit lifecycle exists only in `workflows/audit.py`.

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

## Persistence And Recovery

Runs continue to persist under `Workspaces/<id>/AgentRuns/<run_id>/`. A run
records:

- Materialized capability stages and semantic units, or an action DAG.
- Unit and action transitions.
- Context manifests.
- Proposal sidecars.
- Approval and interaction records.
- Executor receipts.
- Definition, prompt, worker, context-policy, and source hashes.
- Provider usage and hash-only provenance.

Proposals are persisted before commit. On recovery, the runner reuses valid
sidecars, re-evaluates readiness, reconciles interrupted commits, and resumes
only incomplete units. Changing a dependency, context policy, prompt version,
worker version, or source artifact makes affected output deterministically
stale.

## Suggested Package Structure

```text
agent/
|- runtime/
|  |- run_runtime.py
|  |- workflow_runner.py
|  |- action_runner.py
|  |- model_gateway.py
|  `- interactions.py
|- routing.py
|- workflows/
|  |- audit.py
|  |- analysis.py
|  `- documents.py
|- capabilities/
|  |- model.py
|  |- planning.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
|- context/
|  |- resolver.py
|  |- presets.py
|  `- manifest.py
|- workers/
|  |- planning.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
|- executors/
|  |- planning.py
|  |- analysis.py
|  |- documents.py
|  `- reporting.py
`- actions/
   |- catalog.py
   |- planner.py
   `- executors.py
```

Modules are grouped by responsibility and then by domain. Avoid creating one
file or runner per capability unless its size independently justifies it.

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
- Capability, context-policy, prompt, worker, and source hashes participate in
  readiness.
- Provider provenance stores hashes and metrics, not row-level data or document
  text.

## Migration Direction

The final target has two scheduling engines: `WorkflowRunner` and
`ActionRunner`. Existing domain runners such as the legacy analysis runner,
`DocumentAnalysisRunner`, and `DocTestRunner` migrate into capabilities,
workers, and executors unless they demonstrably require a different scheduling
protocol. During migration, existing runners may remain as thin compatibility
adapters over the shared implementations, but duplicate execution logic is not
retained.

## Handoff Context

This section records the analysis and decisions that led to the target
architecture. It is intended to make this document sufficient context for a
new implementation thread.

### Current Implementation

The repository currently contains three generations of general agent planning:

| Generation | Implementation | Plan source | Work unit | Scheduler |
|---|---|---|---|---|
| v1 | `_Runner` in `agent/runner.py` | Hard-coded stages | Task | Straight-line stage calls |
| v2 | `CommandRunner` | Model-generated action DAG | Action | Priority loop over the action ledger |
| v3 | `WorkflowRunner` | Capability registry closure and readiness | Semantic unit | Dependency-ordered stages with bounded parallelism |

The production audit path is v3. `WorkflowRunner` inherits from
`CommandRunner` and currently reuses planning context, quality checks, proposal
formatting, approvals, and RCM matching through that inheritance. This sharing
is intentional, but the inheritance also exposes the entire v2 scheduler and
its obsolete full-audit logic to v3.

`CommandRunner` remains useful for isolated mutations, including attaching a
document, pinning a dashboard tile, renaming an artifact, or composing a small
bounded series of registered actions. It should not plan or enforce a complete
audit lifecycle.

The small intake, document-test, and document-analysis runners currently have
their own durable run behavior. They are not simply copies of the general graph
runners. The target architecture nevertheless applies a strict test: retain a
separate runner only when it requires a genuinely different scheduling and
control protocol. Otherwise migrate its work into capabilities and workers.

### Confirmed Vestigial Areas

The main dead path is the v2 full-audit orchestration in
`agent/command_runner.py`, including:

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
before the v2 command interpreter runs.

The audit lifecycle is currently encoded in three places:

- `ledger.AUDIT_LIFECYCLE_STAGES`.
- `CommandRunner.ORCHESTRATED_FULL_AUDIT_ACTION_TYPES` and associated methods.
- `audit_capabilities.build_registry()`.

`build_registry()` is the current authoritative implementation. The target
moves the authoritative declaration to `workflows/audit.py` and removes the
other two lifecycle encodings.

There is also concrete document-analysis duplication. Both
`DocumentAnalysisRunner` and `CommandRunner._ensure_planning_analysis` implement
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
- Automatic context selection must be named, bounded, versioned, and recorded.
  A bare `auto` selector is insufficiently auditable.
- Model generation and deterministic mutation are separate. Workers return
  validated proposals; executors commit them.

### Context Auditability Requirements

The capability declaration is intended to be the primary auditable and
manually editable specification. It must identify:

- Dependencies and invalidation sources.
- Readiness and unit expansion functions.
- Permitted context presets and representations.
- Required versus optional context.
- Deterministic selectors and bounded automatic selectors.
- Context size and item budgets.
- Privacy permissions, especially the prohibition on sending table rows.
- Worker, prompt, response schema, and executor versions.
- Approval policy and output contract.

The normalized `ContextSpec` and its hash must be persisted. Each execution
also persists a `ContextManifest` showing the sources actually selected,
selection reasons, source hashes, omissions, truncations, privacy decisions,
and supplied size. Provider provenance remains hash-only and does not contain
document text or row-level table data.

### Recommended Migration Sequence

1. Add characterization tests for local routing, v3 outcome materialization,
   isolated actions, and resumption of persisted v2 runs.
2. Remove the v2 full-audit methods, branches, prompt clauses, and action-budget
   reservations while retaining generic action behavior.
3. Remove `AUDIT_LIFECYCLE_STAGES`, lifecycle normalization, and the
   `audit_lifecycle` argument from the action ledger.
4. Introduce `RunRuntime`, `ContextSpec`, `ContextResolver`, `ContextManifest`,
   worker, executor, and capability interfaces without moving all behavior at
   once.
5. Extract planning context and quality behavior from `CommandRunner`; make
   `WorkflowRunner` use the new interfaces directly instead of inheriting from
   `CommandRunner`.
6. Move the audit dependency declaration into `workflows/audit.py` and migrate
   v3 stage handlers into grouped capability, worker, and executor modules.
7. Add the exploratory analysis workflow and route the existing `data_analysis`
   intent to `analysis.executed`.
8. Replace both document-analysis implementations with map and reduce
   capabilities using shared workers and persistence executors.
9. Migrate or wrap document-test and intake runs according to whether their
   scheduling protocols fit `WorkflowRunner`.
10. Move the v1 `_Runner` to an explicit compatibility module, migrate remaining
    callers, and delete it in a separate change when no supported entry point
    depends on it.

Do not combine v1 retirement with the initial v2/v3 cleanup. Removing duplicate
full-audit orchestration is lower risk and should land first.

### Compatibility And Recovery

All `kind="audit"` runs currently dispatch through `WorkflowRunner`. A persisted
v2 broad-audit command without workflow state can therefore be rematerialized
as a v3 capability workflow on resume. Characterization tests must lock this
behavior before deleting compatibility branches.

Persisted v2 generic action runs must continue through `ActionRunner` with their
existing action ledger, preconditions, receipts, and interactions. Existing v3
runs must retain semantic unit IDs, proposal-sidecar reuse, parent-hash checks,
and deterministic readiness behavior.

Compatibility adapters may translate old run projections for the UI, but new
domain logic must not be added to those adapters.

### Verification Criteria

The migration is complete when:

- The audit lifecycle has one declaration.
- `WorkflowRunner` contains no APM-, RCM-, document-, analysis-, or report-
  specific handlers.
- `WorkflowRunner` does not inherit from `ActionRunner` or `CommandRunner`.
- The action ledger has no audit lifecycle normalization.
- Workers cannot access undeclared context or write workspace state.
- Executors cannot call the model.
- Document analysis has one map/reduce implementation.
- Generic table analysis and full-audit requests route to different declared
  outcome sets while using the same workflow scheduler.
- Existing privacy boundaries remain enforced.
- Backend tests and the frontend build pass.

At the time this handoff section was written, the architecture had only been
documented. No framework implementation changes had been made as part of this
architecture discussion.
