# EDA pipeline: forward plan

**Status:** E1, E2, and E4 are complete. E3's value-domain half is complete;
its row-sample half was deliberately reverted. E5, E6, and a within-group
variance primitive remain open.

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

The target flow is:

```text
data.characterized       deterministic column metadata and safe value domains
data.relationship_map    deterministic, unpruned routes and orphan evidence
data.probes              deterministic ranked nominations
analysis.reading         one bounded model reading over the full map
analysis.definitions     model-authored executable definitions by assertion cluster
data.joins_ready         deterministic materialization of only required joins
analysis.executed        local execution
analysis.reconciled      deterministic assertion-register coverage
analysis.summarized      bounded model summary
```

The model provides judgment, interpretation, and negative-space observations;
deterministic probes provide the default evidence-backed floor. A failed or
unhelpful reading must never silently remove a deterministic nomination.

## Current state

| Item | State | Contract that remains important |
| --- | --- | --- |
| E1 — deterministic probes | Complete | Sweep intra-frame pairs before cross-frame routes; emit executable nominations in the analytics library's vocabulary and rank them by measured yield. |
| E2 — relationship map | Complete | Diagnose every viable route. `weak` is explanatory metadata, not an exclusion gate; orphan counts may themselves nominate `referential` work. |
| E3 — safe context | Partly complete | Low-cardinality domains and format information are available. Do **not** reintroduce row samples without a separately reviewed privacy permission, cap, source-aware truncation, and a demonstrable need. |
| E4 — one reading turn | Complete | The assertion register is additive by default. The reading may decline a deterministic nomination only with a recorded reason. |
| E5 — joins after definitions | Open | Definitions choose what must be computed; joins are then materialized for those accepted definitions. |
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

### E5 — materialize joins after definitions

This is the highest-value remaining change. The present scarcity of durable
joins forces routing to select one candidate before the system knows which
comparisons a definition needs. Separate a user-visible durable join from a
transient, reproducible alignment used to compute a proposed analysis.

Required behavior:

- Probes and the relationship map may retain multiple viable routes between the
  same table pair.
- An accepted definition declares its required source tables, columns, and
  route(s). Validate that declaration against the map before execution.
- Materialize only the routes required by accepted definitions, after definition
  selection. Equivalent route choices must be deterministic and explainable.
- Preserve analysis identity and staleness semantics when a required route or
  input table changes.
- Do not make a durable user-visible join merely to test a transient alignment.

The first acceptance scenario is the mixed `invoice_data.PO_NUMBER_LINK` field:
some values are PO identifiers and others are requisition identifiers. The
pipeline must be able to connect invoice-side evidence to `requisitions` without
losing the ordinary invoice-to-PO route. This is necessary to fully evaluate
A03/A04 and is a guard against the same class of loss in authority, SoD, and
master-data checks.

Before changing route arbitration, measure the current single-sided-definition
rejection rule. It previously removed plausible definitions simply because they
read one side of a joined frame; do not loosen or retain that rule on intuition.

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

- The probe sweep is fast on the current corpus but has not been characterized
  on very wide tables. Add deterministic bounds before assuming it scales to
  hundreds of columns.
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
