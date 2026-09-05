# EDA pipeline: forward plan

**Status (2026-09-05):** E1, E4, and E5 are implemented. E2's alternate and
weak-route retention is implemented in the bounded EDA catalog; the older
durable-join selector remains separate. E3's value-domain half is complete;
its row-sample half was deliberately reverted. E6 and a within-group variance
primitive remain open. See [E5 validation](eda-e5-validation.md) for measurements
and limits; this is not a claim of live-provider validation.

**Scope:** `analysis_workflow_v1` only. Do not change the audit workflow,
document analysis, document tests, or the row-level privacy boundary.

**Measure of progress:** use `backend/tests/eda_answer_key.py` and
`backend/tests/test_eda_answer_key.py`. The scorer contains the executable
procurement answer key and pins the last measured `pro4` baseline: 19 of the 28
scoreable items. The independent issue descriptions remain in Appendix A of
`procurement-pipeline-review.md`.

## Objective

The pipeline must decide what is worth testing from measured evidence, rather
than discard relationships or candidate checks before it has evaluated their
audit significance. It must also retain an explainable record of every
assertion it considered, including one it could not test.

The implemented standalone EDA flow is:

```text
data.relationships_inferred   deterministic route diagnostics and orphan evidence
analysis.register_ready      local probes, safe domains, one bounded model reading
analysis.definitions_ready   direct floor specs; authored assertions batched per frame
analysis.inputs_ready        validate/load accepted analysis-owned alignment recipes
analysis.executed            local execution and bounded recorded results
analysis.summarized          bounded model summary
```

Probes may compute candidate alignments locally to measure them before selection.
Only accepted definitions persist a recipe; speculative alignments do not enter
the workspace's durable join collection. `analysis.reconciled` is still proposed
work under E6. Explicit durable-join requests retain their separate
`data.join_utility_ready` → `data.joins_ready` branch, and the audit graph keeps
its original branch and dependencies.

The model provides judgment, interpretation, and negative-space observations;
deterministic probes provide the default evidence-backed floor. A failed or
unhelpful reading must never silently remove a deterministic nomination.

## Current state

| Item | State | Contract that remains important |
| --- | --- | --- |
| E1 — deterministic probes | Complete | Sweep intra-frame pairs before cross-frame routes; emit executable nominations in the analytics library's vocabulary and rank them by measured yield. |
| E2 — relationship map | Implemented with bounds | Retain alternate keys and weak/zero-match evidence in the EDA catalog. Diagnose candidate routes; disclose catalog omissions. The explicit durable-join selector keeps its older arbitration. |
| E3 — safe context | Partly complete | Low-cardinality domains and format information are available. Do **not** reintroduce row samples without a separately reviewed privacy permission, cap, source-aware truncation, and a demonstrable need. |
| E4 — one reading turn | Complete | The assertion register is additive by default. The reading may decline a deterministic nomination only with a recorded reason. |
| E5 — joins after definitions | Implemented | Accepted definitions own reproducible alignment recipes; execution validates sources, keys and multiplicity without creating durable joins. |
| E6 — reconciliation | Open | A durable coverage artifact must classify every assertion's outcome. |
| Within-group variance | Open | Add a primitive for same-key/different-value analysis without treating every group as an exception. |
| Regression scorer | Complete | Any substantive change must be measured against the answer key; do not replace its signatures or pinned baseline to make a regression disappear. |

## Invariants

1. **No raw table rows go to the provider.** `allow_table_rows` stays denied.
   Value domains must be safe at the source-column level: joining a small
   dimension must not turn names, email addresses, or other prose attributes
   into a publishable category.
2. **Low match rate is evidence, not a rejection reason.** A relationship with
   many unmatched values may be the most important audit signal.
3. **Keep alternate routes until definitions establish which one is needed.**
   Do not choose one route per table pair by lexical order, convenience, or a
   generic score when multiple routes can answer different questions.
4. **Probe evidence is a floor.** Model judgment can add, prioritize, or give a
   reasoned decline; it cannot make evidence-backed work disappear without an
   auditable record.
5. **Definitions and execution remain local and rerunnable.** Continue using
   the existing durable analysis identity, local Polars execution, result
   provenance, and bounded exception handling.
6. **A saturated result is not automatically a useful finding.** Continue to
   use `analysis_results.uninformative_reason`; `referential` remains an
   intentional exception because a population-wide broken reference can be the
   finding.
7. **A clean or missing result is still an outcome.** Reconciliation must tell
   apart answered, unanswerable, declined-with-reason, failed, and
   never-attempted assertions.

## Next work

### E5 — implemented alignment contract

The deterministic catalog retains up to 32 routes per directed table pair and
192 candidate alignments, exploring up to two hops. It retains low/zero-match
diagnostics and alternate keys, including value-supported reference-shaped keys
whose names differ. An executable alignment must use compatible key types and
unique non-null right keys. Its ordered left joins preserve the root population;
an unmatched counterpart stays null. Bounds are disclosed in a run warning.

The reading selects an exact frame and role route. Its executor attaches the
catalog's recipe to the accepted definition; model text cannot replace that
recipe. Saved recipes include the root, ordered source/key mappings and aliases.
The reader accepts up to three hops for persisted recipes, while automatic
discovery currently explores two. Static Python dependencies and analytics
lookup dependencies join the recipe in the result fingerprint. Existing
unaligned analysis fingerprints and durable-join route identities are unchanged.

Rerun, preview, export, summary provenance, and promotion consume the saved
recipe. Promotion carries equivalent local Polars code into the Data Test, so
deleting the original analysis does not remove the test's input construction.
Source changes during generation or execution trigger parent conflicts instead
of committing an obsolete result. Multiplying or broken joins fail validation.

The mixed-identifier acceptance fixture preserves both invoice-to-PO and
invoice-to-requisition routes: 25 invoices remain the population, five are
comparable to requisitions, and one exceeds the requisition amount. This tests
the A03/A04 route prerequisite; it does not claim those answer-key items are now
fully answered on an existing workspace. The measured Procurement improvement
is the two-hop authority path, A10/A11, described in the validation note.

The single-sided-definition and saturation screens remain in force. Role-route
identity now includes the entire alignment path so, for example, requester and
approver authority checks cannot collapse merely because their last hop is the
same job-title lookup.

### Prompt size and workload

The reading frame map sends base schemas once, then compact join descriptors
and collision renames. Dependency-only descriptors cannot be selected as
assertion targets; missing source descriptors do not authorize invented columns.
Safe domains come from base columns once. Definition turns receive the target's
exact schema and base-table lookup context, without every speculative sibling's
full schema. Hypotheses appear once in each prompt. Durable nomination provenance
retains repeated-frame references, while the reading sees their count.

Full EDA saves one utility-gate call. Deterministic floor specs still need zero
definition calls, and the reading's maximum 12 authored assertions are grouped
into at most 12 frame-specific definition turns before repair/tool turns. The
reading context remains capped at 180,000 characters; compact descriptors and
nominations have larger item limits within that same total budget. These are
bounds, not expected usage. More eligible analyses can increase local probing,
definition work, and memo/tool work; prompt reduction alone does not establish
an end-to-end runtime improvement.

### E6 — reconcile the assertion register

Add `analysis.reconciled` after execution. It compares the durable assertion
register with definitions, execution results, and recorded declines.

For each assertion, emit one mutually exclusive state:

- `answered` — an eligible executed definition covers it;
- `unanswerable` — the data/model cannot express it, with the limitation;
- `declined_with_reason` — intentionally not pursued, with the reason;
- `failed` — definition or execution failed, with repair/retry state; or
- `never_attempted` — no later stage discharged it.

The artifact must be deterministic, revision-aware, and visible to planning and
reporting without exposing row values. It is coverage evidence, not an automated
audit conclusion. It must distinguish a test that was never written from a test
that ran cleanly, and a test that can only answer half of an assertion from one
that truly discharged it.

### Within-group variance primitive

Implement a library primitive for a repeated key whose associated values vary,
starting with item-description to unit-price analysis. The output must describe
group count, support, spread/distribution, and the threshold that makes a group
exceptional. A plain "values differ" predicate is not acceptable: ordinary
variation would flag every repeated item.

The initial acceptance scenario is A27. It should surface meaningful tail
spreads without claiming that all price variation is a control failure.

## Verification and guardrails

Run the focused answer-key tests while implementing, then score a fixture or
workspace with the scorer before claiming improved reach. Preserve the pinned
baseline and report both reach and regressions; a higher count does not justify
dropping a prior valid computation.

Exercise at least these cases:

- low-match and zero-match relationships create retained evidence and viable
  nominations rather than being silently pruned;
- multiple routes between the same tables can support distinct accepted
  definitions;
- deterministic nominations survive a failed reading turn and a model decline
  is stored with its reason;
- a route/input change invalidates only the affected definitions/results;
- saturation rejects vacuous comparisons while preserving legitimate saturated
  referential findings;
- sensitive dimension values never enter domain context merely because a join
  makes the column appear low-cardinality;
- reconciliation emits exactly one state per assertion and correctly exposes
  half-discharged work;
- the variance primitive is bounded on small populations and does not create
  universal exceptions; and
- the memo path is exercised on a live provider after its duplicate-tool-output
  and long-emission fixes.

## Open risks

- Expanded local probing is material: the six-table Procurement check took
  about 35 seconds. Catalog output is bounded, but column-pair diagnosis and
  probe cost have not been characterized on very wide tables. Do not assume
  output bounds imply constant compute or memory use.
- Direction matters. An inverted comparison can saturate a population; retain
  directional probe evidence rather than weakening the saturation guard.
- A single `analysis.reading` turn is a model failure point. The additive
  register and deterministic probes are the fallback; verify that behavior with
  a live provider.
- More candidate routes increase contention unless identity and arbitration are
  exact. Treat an unexplained arbitration change as a possible coverage
  regression.
- This work does not solve fieldwork lookup anchoring or row-wise population
  reachability. Keep those concerns separate rather than overloading E6.
