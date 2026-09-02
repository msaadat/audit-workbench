# RCM generation redesign: a staged pipeline, delivered in small steps

**Status:** design, not yet implemented. This is the handoff for splitting the
single RCM judgment turn into a per-process, per-job pipeline, and for moving
the cycle-vouching evidence contract out of planning. It is written so that
each step lands on its own, leaves `rcm_only` regenerating a matrix end to end,
and can be measured against the previous step before the next one starts.

`docs/rcm-generation-quality.md` is the history this rests on: three rounds of
prompt and gate work on the same turn. Read its *Round 3* before touching the
worker; the failure mode it names, *the model's audit reasoning was sound and
the pipeline threw the work away*, is the one every step below is designed to
stop paying for.

## The evidence

Six agent runs in the local workspaces executed the RCM stage. All used
`deepseek/deepseek-v4-flash-0731` through OpenRouter, auto mode, cold matrix.

| Workspace | Run | Outcome | Duration | Calls | Prompt tok | Output tok | Rows |
|---|---|---|---|---|---|---|---|
| procurement | `20260901-172542-f7eb12` | failed | 3m 36s | 1 | 19,087 | 13,305 | 0 |
| procurement | `20260901-173149-1d05e1` | completed | 2m 37s | 1 | 18,812 | 23,911 | 27 |
| treasury | `20260901-163707-c3f1ef` | completed | 2m 34s | 3 | 34,194 | 35,679 | 12 |
| treasuryfull | `20260902-073148-2db4fb` | failed | 8m 38s | 4 | 75,650 | 107,073 | 0 |
| treasuryfull | `20260902-075954-183886` | failed | 11m 42s | 4 | 85,276 | 136,019 | 0 |
| treasuryfull | `20260902-130529-51572b` | completed | 5m 57s | 2 | 43,026 | 73,186 | 29 |

The three failures: the provider finished with reason `error` after 216 s and
13k reasoning tokens with no text; and two rejections by the APM theme-coverage
gate, of a 26-row and a 22-row matrix, over "Suspension avoidance." and "Fraud
risks considered". Commit `3f93614` has since demoted that gate to a warning.
The run that then passed shipped with six uncovered-theme warnings, one of them
the theme that had failed the run before it, which is the measure of what the
gate was actually testing.

Aggregate: 18 calls, 276k prompt tokens, 389k completion tokens, about 50
minutes of provider latency, roughly 40 percent of it on runs that committed
nothing. Four of six runs entered repair; every repair re-ran the evidence
call. The largest single call produced 65,853 output tokens in 314 s. Latency
tracked bundle size: the treasuryfull bundles were 100k characters against 50k
for the other two workspaces.

## What one run does today

`planning.rcm_ready` expands to one unit. `_bind_rcm` in
`agent/audit_execution.py` resolves the `planning.rcm` preset (APM up to 60k
chars, template 10.9k, documents 40k, table profiles and metadata and
small-table rows 40k, existing rows 40k, citation register) and hands the
bundle to `run_rcm_worker` in `agent/workers/planning.py`.

The judgment call, `RCM_SYSTEM` plus the whole template, asks for every row of
every process at once: risk, rating, control, control type, owner, criteria
with citation refs chosen from a numbered register, `control_attributes` with
assertion and `evidence_kind`, business cycle, and `operation`/`rcm_id` against
the existing rows. A second call, `RCM_SCHEMA_EVIDENCE_SYSTEM`, authors
`required_comparisons` for every `transaction_cycle` attribute against the
schema catalog carried on the unit input. `validate_rcm_proposal` gates rows
individually and the document as a whole; one repair turn is scoped to failing
rows unless a document-level error forces a whole-document re-ask; survivors
are quarantined on the last attempt. `execute_rcm` commits rows one at a time
under the APM parent hash.

Downstream, `tests.cycle_ruleset_proposed` hands the matrix's comparisons to
the linkage worker, which must answer each one in its exact operands; the
auditor approves the ruleset; `tests.generate` reads `required_comparisons_for`
to build cycle tests. That chain is why `documents.schemas_stamped`, the full
extraction pass, is a prerequisite of the RCM, and why the schema catalog is
part of the RCM unit's input hash.

## Where it strains

1. **One turn, four altitudes.** Risk enumeration is domain recall; control
   description is grounded reading; rating, assertion, type and evidence kind
   are closed-vocabulary classification; `operation`/`rcm_id` is bookkeeping.
   Fused, a defect in any one costs the whole output, and one repair turn has
   to fix all of them.
2. **Whole-matrix output.** The largest artifact in the system, produced in one
   completion. It drives the 314 s call, the empty-completion failure, the
   trailing-bracket class from round 3, and the cost of every whole-document
   repair.
3. **Document-level gates on a row-scoped repair.** A gate that asks the matrix
   to *add* something cannot be answered by the scoped repair, so it forces a
   whole-document re-ask of the biggest call. The theme gate is demoted; the
   valued-populations gate in `document_level_errors` still has this shape.
4. **The evidence contract is authored where the matrix knows least.** Two
   model authors write the same fact in sequence (the evidence call, then the
   linkage worker answering it). Planning inherits the extraction pass as a
   prerequisite and the schema hash as an invalidation trigger.
5. **Identifier contracts.** Citation refs, document refs, row ids and
   operations are all things the model has to copy correctly, and each has been
   flaky. Each costs a rule in the prompt and a repair class in the gate.
6. **Narrative rules are prompt-only.** Recommendation 7 in the quality doc
   (percent signs, column-name tokens, aspirational controls, system-enforcement
   claims, cross-process duplicates) still has no deterministic check.

Two things are not the problem and are not changed: the model's audit
reasoning, which was sound in every recorded rejection; and the executor,
reconciliation and CAS commit, which recorded no errors on any run.

## The target

The governing rule: **the model names things; local code finds things.**
Nothing the model outputs is an identifier into something else.

```
APM ──► stage 0: scope map ──► one unit per bucket, in parallel ──► local merge ──► executor
          (buckets, themes)       A  risks   (no engagement material)
                                  B  controls + attributes (bucket-scoped basis)
                                  C  attributes only, if B proves to leak
                                            │
                                            └─► cross-cutting unit per cycle, last

rows with evidence_kind = transaction_cycle ──► cycle design workflow (after schemas_stamped)
                                                  roles, join keys, assertions, and the
                                                  comparison that answers each attribute
```

- **Stage 0, the scope map.** One small structured call over the APM alone,
  seeded with the distinct `process` values of any existing rows. Returns
  business cycles, process buckets with a one-line description, one
  cross-cutting bucket per cycle, and each planned APM theme assigned to a
  bucket. Trivial gate. Stored beside the planning context; auditor-editable;
  cached against the APM hash.
- **Per bucket, small calls.** Risks first with no documents or profiles in
  view, so observation-to-row transcription is prevented by construction.
  Then controls with their attributes against a locally selected slice of the
  basis. Each call carries only the template rules for the fields it writes.
- **Context per bucket is selected locally.** The document selector already
  retrieves excerpts by a lexical query; per bucket the query is the bucket
  name, description and themes. The table scorer behind
  `untested_population_rows` already ranks tables against a row's words; it
  ranks them against the bucket the same way. APM sections whose heading
  matches a bucket are included; process-flow and fraud sections form a shared
  prefix. Small tables go to every bucket.
- **Citations are resolved after the fact.** The controls call writes
  `criteria` as a short verbatim quote or leaves it empty, and may add the
  `[C…]` marker it saw beside the sentence as an ungated hint. Local code
  matches the quote (case, whitespace and light paraphrase tolerant) against
  the supplied sentences, which carry their ids, and writes `criteria_refs`.
  Quote decides; a disagreeing id is corrected silently; when the quote
  matches several sentences the id disambiguates; when only the id resolves
  the ref is attached and flagged unverified; when nothing resolves the text
  stays with no ref and a flag. A wrong or missing id costs nothing.
- **Owner is a string check.** The role must occur in the supplied text.
- **Reconciliation is local.** `process` becomes a closed vocabulary of bucket
  names, so the semantic id (process slug plus risk slug) is stable enough for
  `match_rcm_revision` to carry the whole job; `operation`/`rcm_id` leave the
  prompt. Near-duplicate risks across buckets are detected locally before
  commit.
- **The cross-cutting unit runs last**, sees the accepted rows, and adds only
  what no process owns: override, monitoring, segregation across steps. That is
  what the treasuryfull matrix invented "Treasury operations" for.
- **Cycle vouching leaves the RCM.** `evidence_kind` stays on the row as the
  audit judgment. The comparisons, roles, join keys and assertions are authored
  once, in a separate workflow that runs after schemas are stamped, and the
  resulting comparisons are written back onto the rows so everything
  downstream keeps reading the field it reads today. The RCM depends on
  `documents.types_classified` only.

## The steps

Each step has an *invariant* (what must still work when it lands), a *measure*
(what to compare against the previous step, on a treasuryfull and a procurement
regeneration), and a *rollback* (a single revert). Order matters only where
stated; steps 0 and 1 are independent of each other and of the rest.

### Step 0. Stop paying twice for failures

Independent of everything below and worth doing first.

- **Retry an unusable completion once at the gateway.** `ModelResponseUnusable`
  in `agent/runtime/model_gateway.py` is raised for an empty message or a
  `length`/`error` finish; today it fails the unit with no rejection sidecar,
  so the next run cannot seed from it and pays a full fresh attempt. One retry
  of the same request before raising is the whole change. Retries are already
  attributed by `attempt` in the activity record.
- **Reasoning budget per worker kind.** `llm._reasoning_parameters()` reads
  one global `LLM_REASONING`. Let the gateway pass an override keyed by
  `activity.context_metrics.worker_kind`, with a lower ceiling for
  classification-shaped calls. The 57k-token reasoning trace on the 314 s call
  is spend, not quality.
- **Do not re-run the evidence call for unchanged rows.** Key contracts by a
  hash of (row process, risk, control, attribute key, requirement); a scoped
  repair that changed one row re-authors one row's contracts. Superseded by
  step 1, so skip it if step 1 is next.

Invariant: none of this changes any prompt or any proposal shape. Measure:
failed runs per regeneration, and reasoning tokens per call. Rollback: revert.

### Step 1. Cycle vouching out of the RCM

The largest single simplification of the RCM turn, and independent of the
splitting steps. After it, `run_rcm_worker` makes one call.

**RCM side.**

- Delete `_with_evidence_contracts`, `_merge_evidence_contracts`,
  `_downgraded_uncontracted`, `_cycle_attribute_requests` and
  `RCM_SCHEMA_EVIDENCE_SYSTEM` from `agent/workers/planning.py`.
  `_contracted_document` becomes `_rcm_document` over the parsed rows.
- `validate_control_attribute` in `cycle_linking.py` currently rejects a
  `transaction_cycle` attribute whose `required_comparisons` is empty. Make an
  absent list acceptable, meaning *not yet contracted*; keep rejecting a
  present-but-malformed one, and keep rejecting comparisons on any other kind.
  The workspace re-validates rows on load through the same function, so this
  is also what lets committed rows sit uncontracted until the cycle design
  runs.
- Remove `schema_catalog` from the RCM unit input in `_bind_rcm`, and remove
  `documents.schemas_stamped` from `planning.rcm_ready` in
  `agent/workflows/audit.py`. Keep `documents.categorized` and
  `documents.types_classified`: the row still needs to know which record
  kinds exist to choose `evidence_kind`. Strip the pack catalogue and the
  comparison paragraph from the template's `control_attributes` entry; the
  system prompt already tells the model to stop at the kind.

**Cycle-design side.** `tests.cycle_ruleset_proposed` already exists with the
right dependencies (`planning.rcm_ready`, `documents.schemas_stamped`), the
right worker (`tests.cycle_linkage`), and an optional requirements source.
Three changes make it the single author:

- `_cycle_requirement_candidates` in `agent/context/adapters.py` supplies the
  attribute's `requirement` text, assertion, row id and key, instead of
  comparisons that no longer exist at that point.
- `LINKAGE_SYSTEM` in `agent/workers/tests.py` asks, for each supplied
  requirement, which proposed assertion answers it, or `unsupported` with a
  one-line reason. `validate_linkage_proposal` checks every requirement is
  either covered by an assertion whose operands exist in the schemas, or
  declared unsupported.
- `execute_cycle_ruleset` in `agent/executors/tests.py` writes, for each
  covered requirement, a `required_comparisons` entry onto the RCM attribute
  derived from the assertion's operands (role → `document_type`, field), and
  for each unsupported one downgrades `evidence_kind` the way
  `_downgraded_uncontracted` does today, with a run warning naming the row.
  This is an RCM row update under the same CAS guard the RCM executor uses,
  and it is what keeps `required_comparisons_for`, `assertion_covers`,
  `uncovered_comparisons` and `tests.generate` unchanged.

Invariant: the full planning template still produces cycle tests for the same
rows it does today, because by the time `tests.specified` runs the rows carry
comparisons again; `rcm_only` produces a matrix with uncontracted cycle
attributes and no schema dependency. Measure: RCM calls per run (expect 1),
RCM prompt size, and the same number of cycle tests as before on treasuryfull.
Rollback: revert; the row shape is unchanged.

Tests to move: `test_rcm_evidence_contract.py` covers the contract
validation and stays; the two-pass worker tests in
`test_agent_planning_rcm_worker.py` are deleted; add executor tests that a
covered requirement gains comparisons and an unsupported one is downgraded.

### Step 2. One call to two: rows, then attributes

Splits the classification job off the judgment turn. The persisted proposal
shape does not change, so the executor, approval flow and reconciliation are
untouched.

- **Call 1** is `RCM_SYSTEM` without the attribute rules and the template
  without its `control_attributes` and `evidence_kind` entries. Rows come back
  with everything except `control_attributes`. Slice the template by its `##`
  and list headings with the section parsing `templates_store` already owns,
  so a workspace override of `rcm.md` keeps working.
- **Call 2**, `RCM_ATTRIBUTES_SYSTEM`, receives the accepted rows as
  (index, process, risk, control, control type), the imported tables' column
  names, and the names of the document types held. It returns
  `{attributes: [{row_index, control_attributes: [...]}]}`, merged locally the
  way `_merge_evidence_contracts` merged contracts. It carries the attribute
  rules, the closed assertion list and the evidence-kind decision table, and
  nothing else.
- **Repair routes by error path.** Errors under `control_attributes[` go to a
  scoped call-2 repair for those rows; everything else goes to the existing
  scoped call-1 repair, after which call 2 runs again for the corrected rows
  only. `_RCM_MAX_REPAIR_ATTEMPTS` stays at 1 per call.
- `prompt_hash` covers both system prompts, as it does today for the two
  prompts it has.

Invariant: `validate_rcm_proposal` runs on the merged rows exactly as before;
`rcm_only` regenerates end to end. Measure: output tokens of call 1 (expect the
attribute share to leave it, roughly a third), latency of the largest call,
attribute-path errors per run. Rollback: revert the worker; proposals from
before and after are the same shape.

### Step 3. Two calls to three: risks, then controls, then attributes

Splits domain recall from grounded reading, which is the seam the quality doc's
recommendation 6 always wanted and never got.

- **Call 1, risks.** Message built from the APM, the risk slice of the
  template (the two-pass method, the theme checklist, the wording rules), and
  the existing rows as (process, risk, rating) marked *already covered*. The
  documents, table profiles and small-table rows are still resolved into the
  bundle but withheld from this message; `_context_without_sources` already
  filters by source id. Output per row: `operation`, `rcm_id`, `process`,
  `risk`, `risk_rating`, `business_cycle`. Reconciliation stays model-driven
  at this step; it moves to code in step 4 when `process` becomes closed.
- **Call 2, controls.** Accepted risks as (index, process, risk) plus the
  documents, small tables and citation register; the control slice of the
  template. Output per row: `control`, `control_type`, `control_owner`,
  `criteria` as a verbatim quote, optional `[C…]` hint. Introduce the local
  citation resolver here and delete `_validated_criteria_refs` as a gate; keep
  its output field. Owner becomes a string-containment check.
- **Call 3, attributes.** Unchanged from step 2.
- **Deterministic narrative gates land here**, on the two small outputs
  where a repair is cheap: percent signs and `ALL_CAPS_UNDERSCORE` tokens in
  `risk` or `control`, aspirational openers in `control`, a system-enforcement
  flag for the auditor, and the rating enum. This closes recommendation 7.

Invariant: as step 2. Measure: rows containing a percent sign or column token
(expect 0, deterministically), owner strings absent from the basis (expect 0),
unresolved criteria quotes, and coverage against the quality doc's checklist.
Rollback: revert the worker.

### Step 4. Stage 0 and parallelism

The structural change. Everything before it has already shrunk each call;
this makes the calls per-process and concurrent, and closes the `process`
vocabulary.

- **New planning artifact and capability.** `planning.scope_ready`, depending
  on `planning.apm_ready`, invalidated on `planning:apm`, with worker
  `planning.scope` over the APM and the existing rows' distinct `process`
  values. Output: cycles, buckets with description, a cross-cutting bucket per
  cycle, themes assigned to buckets (`planned_risk_themes` supplies the
  candidates). Stored next to the planning context and editable through
  `planning_routes.py`. `planning.rcm_ready` gains it as a dependency.
- **Units per bucket.** `_rcm_ready`'s unit expansion in
  `agent/capabilities/planning.py` returns one `UnitSpec` per bucket with the
  bucket as `input_payload`, and the capability declares
  `barrier="all_settled_parallel"`, which the runner already fans out under
  `max_llm_concurrency` (default 4). `_bind_rcm` reads the bucket from
  `unit_input` and builds `rcm_scope` for it: `excerpt_query` from the bucket,
  table candidates filtered by the existing overlap scorer, APM sections by
  heading match with the whole memo as fallback, existing rows filtered by
  process, small tables always. Calls 1 to 3 from step 3 run per unit.
- **Cross-cutting unit after the buckets.** A second capability,
  `planning.rcm_cross_ready`, depending on `planning.rcm_ready`, one unit per
  cycle, shown the accepted rows and asked only for what no process owns.
  A separate capability rather than a parent-ref trick, so ordering is the
  graph's and not the scheduler's.
- **Closed `process`.** Gate each row's `process` against the bucket names.
  With that stable, drop `operation`/`rcm_id` from call 1; the executor's
  semantic-id match already handles rows without an explicit id, and
  auditor-owned rows are already preserved.
- **Local dedup.** Normalized pairwise comparison of `risk` across buckets
  before commit, reported as a warning first and merged on exact match.
- **Themes as input.** Each bucket's call 1 lists its assigned themes to own
  or decline with a reason. `unowned_themes` and `weakly_owned_themes` stay
  as post-commit warnings.
- **Degenerate maps.** An APM that yields one bucket runs as one process unit
  plus one cross-cutting unit, which is step 3's behaviour.

Invariant: `rcm_only` and the planning template regenerate treasuryfull and
procurement end to end; proposals per unit are the same shape as today's
whole-document proposal restricted to one bucket, so the executor is unchanged.
Measure: wall-clock per regeneration, tokens per regeneration, largest single
call, rows per bucket, duplicate pairs, and whether the treasuryfull
cross-cutting risks land in the cross-cutting unit rather than in every bucket.
Rollback: the scope capability can stay while `_rcm_ready` reverts to a single
unit; nothing downstream reads the map.

## What each step buys

| After step | Calls per cold run | Largest output | What a rejection costs | Schema dependency |
|---|---|---|---|---|
| today | 2 to 4 | 66k tokens | the matrix and a re-run evidence call | RCM invalidated by re-derived schemas |
| 1 | 1 | unchanged | the matrix | none on the RCM |
| 2 | 2 | roughly two thirds | rows or attributes, not both | none |
| 3 | 3 | roughly half | one job's rows | none |
| 4 | 1 + 3 per bucket + 3 per cycle, parallel | a bucket's rows | one job of one bucket | none |

## What is deliberately not changed

- The privacy boundary. No step requests table rows; bucket context is a
  narrower selection of what the preset already admits.
- `execute_rcm`, `reconcile_rcm`, `match_rcm_revision`, per-row approval in
  permission mode, and the receipt shape.
- The row schema. `required_comparisons` remains the field downstream reads;
  only its author moves.
- The ruleset approval. Proposing is still not approving; the auto-mode
  delegation in `cycle_rulesets.approve` is orthogonal.
- Join-key fan-out measurement, which `docs/agentic-vouching-plan.md` names as
  the property that must not be lost; the ruleset path is untouched.

## How to evaluate each step

Regenerate `treasuryfull` and `procurement` with `rcm_only` after each step and
record, from `AgentRuns/<run>/` and `telemetry.db`: calls, prompt and output
tokens per call, largest call latency, repair count, rows committed, rows
quarantined, warnings. Then run the row-level checks in the *Verification
commands* section of `docs/rcm-generation-quality.md` and fill the next column
of its table. A step that regresses any of the quality doc's nine checks does
not proceed to the next.
