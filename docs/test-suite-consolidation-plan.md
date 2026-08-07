# Test Suite Consolidation Plan

## Objective

Reduce maintenance cost and failure noise in the backend test suite without
weakening the audit, persistence, recovery, or routing contracts that
make Audit Workbench safe to use locally.

This is a consolidation plan, not a coverage-reduction target. A test may be
removed only after its durable behaviour is protected by a clearer test at the
same or a more appropriate layer.

## Baseline

At the time of this assessment the backend suite contains 70 test modules,
1,129 test functions/classes, and 1,272 collected pytest cases. The full run
takes roughly four minutes on the current Windows development environment.

The main source of avoidable complexity is historical architecture testing:
many tests assert deleted symbols, exact source snippets, or the location of
implementation details. The core product contracts themselves are worth
retaining.

## Non-negotiable coverage

Do not remove these categories. They protect the product's material safety and
correctness properties.

- Durability: atomic writes, optimistic revision handling, parent hashes,
  linked-write rollback, receipt reconciliation, and proposal reuse after an
  interrupted commit.
- Workflow correctness: deterministic dependency closure, stable semantic unit
  identity, ordered all-settled execution, explicit engine dispatch, and
  fail-closed records.
- Audit semantics: RCM coverage, execution roll-up, observation/finding
  provenance, document-test disposition, working-paper/report reconciliation.
- Public behaviour: one representative API lifecycle for each persisted
  resource, plus the critical browser-facing projections.

## Consolidation principles

1. Prefer one behavioural test at the ownership boundary over several tests
   asserting call paths or source text.
2. Keep matrix breadth through parametrization when inputs have the same setup,
   execution path, and assertion shape.
3. Test pure routing, validation, and formatting functions directly; test the
   runner only for dispatch, persistence, controls, and terminal outcomes.
4. Keep one happy path and one failure/recovery path per workflow. Add more only
   for genuinely distinct risk classes.
5. Delete retired-architecture assertions after their surviving invariant is
   covered by an active contract test.
6. Do not use source-text assertions to police ordinary refactoring. Import
   boundary checks are allowed only for provider-call confinement, and
   domain-neutral runtime boundaries.

## Proposed work

### Phase 1 — Establish test tiers and ownership

Add pytest markers and document the intended run modes:

- `unit`: pure validation, routing, formatting, and registry tests.
- `contract`: persistence, runtime, worker, executor, and API contract
  tests.
- `integration`: one subsystem across durable storage or a mocked model.
- `e2e`: representative full workflow scenarios.
- `architecture`: temporary structural guards pending consolidation.

The default suite remains unchanged during this phase. CI should run all tiers;
local development can run `unit` and `contract` first.

Acceptance criteria:

- Every test module has an obvious owner and tier.
- The test command can select each tier without changing test behaviour.

### Phase 2 — Replace retirement tests with enduring contracts

Consolidate these migration-era modules before removing individual tests:

| Current module | Retain | Consolidate/remove |
| --- | --- | --- |
| `test_agent_v1_retirement.py` | Unsupported engines fail closed; no supported writer emits a retired record; dispatch accepts only the declared engines. | Assertions about deleted aliases, prompt names, modules, UI launchers, and source branches. |
| `test_agent_definition_of_done.py` | Explicit engine set; scheduler composition; declared workflow ownership; context presets are registered. | Historical helper names, compatibility aliases, and repeated checks already owned by composition tests. |
| `test_workflow_phase7_gate.py` | Registry/graph/execution-binding validation and frontend projection shape. | Source-text checks for transitional handlers and duplicated graph assertions. |
| `test_agent_capability_composition.py` | Authoritative graph partitioning, declaration identities, registry startup validation, and parallel-barrier eligibility. | Retired-module checks duplicated by the new engine/dispatch contract. |

Create a small `test_agent_architecture_contracts.py` with no more than six
tests. It should own the retained invariants above. Once it passes, remove the
duplicated migration-only assertions and delete empty historical modules.

Acceptance criteria:

- The active architecture remains fail-closed when an invalid engine, workflow,
  capability binding, or context preset is introduced.
- No test depends on the spelling of a removed private helper or prompt.

### Phase 3 — Reduce implementation-coupled checks

Review all uses of `inspect.getsource`, `hasattr`, and exact source-string
matching. Replace them with one of the following:

- a direct behavioural test;
- an import-boundary test based on parsed imports; or
- a registry/contract validation test.

Retain source/import checks only for these high-value boundaries:

- only `ModelGateway` may reach the provider transport;
- workers cannot mutate workspaces or read the run store;
- executors cannot call workers or the model gateway;
- the domain-neutral runtime cannot import audit/product domains;
- context resolution cannot expose table rows or call a provider.

Likely primary targets are `test_workflow_v2.py`, `test_agent_runtime_contracts.py`,
`test_command_agent.py`, and the retirement/definition-of-done suites.

Acceptance criteria:

- No routine refactor fails because a method was renamed or moved.
- Model-call confinement checks remain explicit and executable.

### Phase 4 — Consolidate workflow tests by risk class

Keep the scheduler golden suite as the owner of generic scheduler behaviour:

- dependency closure and stable materialization;
- deterministic all-settled ordering and failure isolation;
- recovery/requeue semantics;
- generation-mode behaviour; and
- exact one-binding validation.

Then reduce duplicate scheduler assertions embedded in audit, analysis, document,
and document-test workflow suites. Each workflow should retain:

1. one scoped happy path;
2. one partial/blocked path;
3. one interrupted-commit or proposal-reuse path; and
4. its unique evidence rule.

Examples of unique rules that must remain:

- analysis: joins are evidence-supported and model context excludes table rows;
- documents: chunk reduction uses only persisted chunk proposals and visual
  evidence is bounded;
- document tests: agent execution and auditor disposition remain separate;
- audit: exception observations, findings, and report/working-paper provenance
  reconcile correctly.

Acceptance criteria:

- The generic scheduler is tested once in `test_workflow_scheduler_golden.py`.
- Domain suites test domain outcomes, not duplicate scheduler internals.

### Phase 5 — Parameterize repeated matrices

Parameterize, rather than duplicate, tests that have identical setup and only
vary inputs or expected normalized output:

- route phrase to workflow/outcome mappings;
- invalid action/worker/executor payload shapes;
- engine/status fail-closed cases;
- narration status labels and wording variants;
- catalogue aliases and canonicalization variants.

Do not parameterize tests that require different setup, different persistence
boundaries, or different user-visible recovery behaviour; those are distinct
scenarios even if their final assertion looks similar.

Acceptance criteria:

- Parameter names make failing cases self-explanatory.
- Each parametrized table maps to one concrete public or domain contract.

### Phase 6 — Right-size API and narration coverage

API tests should retain one lifecycle per persisted resource and focused tests
for distinct rules such as revision headers, exports, upload handling, and
authorization/mode gates. Do not repeat generic CRUD assertions through both
the API and workflow layers.

For narration, retain tests for terminal status, blocker/actionability,
bounded transcript projection, chat deletion/orphan handling, and the next-step
projection. Consolidate near-identical wording/status mappings into
parameterized pure-function cases.

Acceptance criteria:

- Public endpoints retain meaningful request/response coverage.
- Text changes do not require editing many tests unless they alter a documented
  user-facing promise.

## Candidate target size

Aim for roughly 800–950 collected cases after consolidation. This is a planning
range, not a success metric: retaining a redundant-looking test is correct when
it protects a distinct durability or audit-semantic risk.

## Change protocol

For each proposed removal or merge:

1. Identify the invariant and its current owning tests.
2. Add or strengthen the surviving behavioural/contract test first.
3. Run the affected module, then the relevant tier, then the full suite.
4. Record the removed tests and the surviving owner in the pull request.
5. Remove only after the replacement test demonstrates the same failure mode.

No test should be removed merely because it is slow, awkward, or currently
failing. Flaky tests must first be repaired or quarantined with a tracked issue;
the Windows sidecar path-length failures are an example of a product defect,
not removable test noise.

## Recommended implementation order

1. Finish the current import/composition repair so the suite collects again.
2. Add markers and produce a collection report by tier.
3. Create `test_agent_architecture_contracts.py` and consolidate the
   retirement/definition-of-done/phase-gate tests.
4. Replace low-value source-text assertions with durable boundary tests.
5. Parameterize routing, validation, and narration matrices.
6. Reassess timing and flakiness after each phase; do not batch-delete tests.
