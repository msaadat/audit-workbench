# RCM generation redesign: a staged pipeline, delivered in small steps

**Status:** steps 0 to 3 and step 5 are implemented (4-5 September 2026); step
4 is still design. Step 5, the cycle design after the APM, superseded 4a and
4b; **step 4 is now the next step to build**, on step 5's artifact.

This is the handoff for splitting the single RCM judgment turn into a
per-process, per-job pipeline, for moving the cycle-vouching evidence contract
out of planning, and for giving the cycle a place of its own between the
memorandum and the matrix. Each step lands on
its own, leaves `rcm_only` regenerating a matrix end to end, and is measured
against the previous step before the next one starts.

Where the implementation departed from what is written below, the step says so
in a **Landed as** note. Nothing in steps 2 to 4 has been revised against those
departures; read the notes before building on the step above them.

`docs/rcm-generation-quality.md` is the history this rests on: three rounds of
prompt and gate work on the same turn. Read its *Round 3* before touching the
worker; the failure mode it names, *the model's audit reasoning was sound and
the pipeline threw the work away*, is the one every step below is designed to
stop paying for.

Every file path below is under `backend/app/` unless it says otherwise. Every
claim about what the code does today was read from the code on 2 September
2026 at commit `3f93614`; where a step depends on a behaviour that should be
confirmed before building on it, the step says so.

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
risks considered". Commit `3f93614` demoted that gate to a warning. The run
that then passed shipped with six uncovered-theme warnings, one of them the
theme that had failed the run before it, which is the measure of what the gate
was actually testing.

Aggregate: 18 calls, 276k prompt tokens, 389k completion tokens, about 50
minutes of provider latency, roughly 40 percent of it on runs that committed
nothing. Four of six runs entered repair; every repair re-ran the evidence
call. The largest single call produced 65,853 output tokens in 314 s. Latency
tracked bundle size: the treasuryfull bundles were 100k characters against 50k
for the other two workspaces.

## What one run does today

| Concern | Where |
|---|---|
| Capability, readiness, one-unit expansion | `agent/capabilities/planning.py` (`_rcm_ready`, `_planning_rcm_ready`) |
| Dependencies | `agent/workflows/audit.py` `DEPENDENCIES["planning.rcm_ready"]` |
| Declared context | `agent/context/presets.py` preset `planning.rcm`; `agent/context/adapters.py` `rcm_scope` |
| Binding, unit input, post-commit warnings | `agent/audit_execution.py` `_bind_rcm` |
| Prompts, parsing, gates, repair | `agent/workers/planning.py` (`RCM_SYSTEM`, `RCM_SCHEMA_EVIDENCE_SYSTEM`, `run_rcm_worker`, `validate_rcm_proposal`) |
| Attribute contract | `cycle_linking.py` `validate_control_attribute`, `validate_required_comparisons` |
| Commit | `agent/executors/planning.py` `execute_rcm`, `match_rcm_revision` |
| Template | `templates/rcm.md`, 10.9k chars, overridable per workspace |
| Downstream consumer of the contract | `agent/capabilities/tests.py` `_cycle_attributes`, `agent/context/adapters.py` `_cycle_requirement_candidates`, `agent/workers/tests.py` `_refuse_uncovered_requirements`, `cycle_linking.required_comparisons_for` |

`planning.rcm_ready` expands to one unit. `_bind_rcm` resolves the preset (APM
up to 60k chars, template, documents 40k, table profiles and metadata and
small-table rows 40k, existing rows 40k, citation register) and hands the bundle
to `run_rcm_worker`. The judgment call asks for every row of every process at
once: risk, rating, control, control type, owner, criteria with citation refs
chosen from a numbered register, `control_attributes` with assertion and
`evidence_kind`, business cycle, and `operation`/`rcm_id` against the existing
rows. A second call authors `required_comparisons` for every
`transaction_cycle` attribute against the schema catalog carried on the unit
input. `validate_rcm_proposal` gates rows individually and the document as a
whole; one repair turn is scoped to failing rows unless a document-level error
forces a whole-document re-ask; survivors are quarantined on the last attempt.
`execute_rcm` commits rows one at a time under the APM parent hash.

Downstream, `tests.cycle_ruleset_proposed` hands the matrix's comparisons to
the linkage worker, which must answer each one in its exact operands; the
auditor approves the ruleset; `tests.generate` reads `required_comparisons_for`
to build cycle tests. That chain is why `documents.schemas_stamped` is a
prerequisite of the RCM and why the schema catalog is part of the RCM unit's
input hash.

## Where it strains

1. **One turn, four altitudes.** Risk enumeration is domain recall; control
   description is grounded reading; rating, assertion, type and evidence kind
   are closed-vocabulary classification; `operation`/`rcm_id` is bookkeeping.
   Fused, a defect in any one costs the whole output.
2. **Whole-matrix output.** The largest artifact in the system, produced in one
   completion. It drives the 314 s call, the empty-completion failure, the
   trailing-bracket class from round 3, and the cost of every whole-document
   repair.
3. **Document-level gates on a row-scoped repair.** A gate that asks the matrix
   to *add* something forces a whole-document re-ask of the biggest call. The
   theme gate is demoted; the valued-populations gate in
   `document_level_errors` still has this shape.
4. **The evidence contract is authored where the matrix knows least.** Two
   model authors write the same fact in sequence. Planning inherits the
   extraction pass as a prerequisite and the schema hash as an invalidation
   trigger.
5. **Identifier contracts.** Citation refs, document refs, row ids and
   operations are all things the model has to copy correctly, and each has been
   flaky. Each costs a rule in the prompt and a repair class in the gate.
6. **Narrative rules are prompt-only.** Recommendation 7 in the quality doc
   still has no deterministic check.

Two things are not the problem and are not changed: the model's audit
reasoning, and the executor, reconciliation and CAS commit, which recorded no
errors on any run.

## The target

The governing rule: **the model names things; local code finds things.**
Nothing the model outputs is an identifier into something else.

```
APM ──► cycle shape ─────────► one unit per step, in parallel ──► executor
          (steps, roles,          1  risks     (no engagement material)
           populations, themes)   2  controls  (step-scoped basis)
                                  3  attributes (closed vocabularies)
                                            │
                                            └─► cross-cutting unit per cycle, last

cycle shape + schemas_stamped ──► cycle bindings (fields only: anchor field,
                                  join keys, assertions, and the comparison that
                                  answers each transaction_cycle attribute,
                                  written back onto the row)
```

- **The cycle shape, after the APM.** One small structured call over the APM,
  the classified document types and the imported tables. Returns the cycle's
  steps in order, the document roles and the population table of each step,
  one cross-cutting bucket, and each planned theme assigned to a step. Stored
  in planning; auditor-editable; cached against the APM hash; drawn as a
  strip on its own page (step 5). It is what step 4 called the scope map,
  with the roles and populations on it.
- **Per bucket, small calls.** Risks first with no documents or profiles in
  view. Then controls against a locally selected slice of the basis. Then
  attributes. Each call carries only the template rules for the fields it
  writes.
- **Context per bucket is selected locally**, by the lexical selector and the
  table scorer that already exist.
- **Citations are resolved after the fact** from a verbatim quote, with the
  model's `[C…]` marker as an ungated hint.
- **Owner is a string check.** The role must occur in the supplied text.
- **Reconciliation is local.** `process` becomes a closed vocabulary of bucket
  names; `operation`/`rcm_id` leave the prompt.
- **The cross-cutting unit runs last** and adds only what no process owns.
- **Cycle vouching leaves the RCM.** `evidence_kind` stays on the row. The
  comparisons are authored once, downstream, and written back onto the rows so
  everything that reads `required_comparisons` today keeps reading it.
- **The cycle is one artifact in two layers.** Its shape (steps, roles,
  populations) is authored after the APM and needs nothing extracted; its
  bindings (fields) are authored after the schemas, by the ruleset stage that
  exists today, which no longer invents roles. The matrix sits between the
  two and depends only on the shape.

## The steps

Each step has an *invariant* (what must still work when it lands), a *measure*
(what to compare against the previous step on a treasuryfull and a procurement
regeneration), and a *rollback*. Steps 0 and 1 are independent of each other
and of the rest. Steps 2, 3 and 4 are sequential. Step 5 depends on step 1
only, is built next now that step 3 has landed, and replaces the first two
parts of step 4; step 4's per-bucket units are then built on step 5's
artifact.

---

### Step 0. Stop paying twice for failures

Independent of everything below and worth doing first.

#### 0a. Retry an unusable completion once at the gateway

`ModelGateway.complete` in `agent/runtime/model_gateway.py` raises
`ModelResponseUnusable` when the provider returns an empty message, with
`_unusable_completion_detail` naming the finish reason. Today that propagates
out of the worker with no rejection sidecar, so the unit fails and the next run
cannot seed from it. Run `f7eb12` is the case: 216 s, reason `error`, nothing
to repair, and the retry paid for a full fresh attempt.

- Wrap the `llm.chat` call inside `complete` in a loop of at most two tries.
  Retry only on `ModelResponseUnusable`; never on `Cancelled`, `LimitExceeded`
  or `LLMError` (the transport already retries HTTP-level failures up to
  `MAX_REQUEST_ATTEMPTS`).
- The retry goes through `_reserve_model_turn` and `_record_model_usage` like
  any turn, so it is budgeted and visible. Add `retry_reason: "unusable"` to
  the activity fields so the debug console can tell it from a repair.
- Keep the existing `attempt` number on both tries: the worker's attempt did
  not change, the provider's did.

Test: a gateway test with a stubbed `llm.chat` that returns an empty message
then a valid one; assert one result, two call records, one `retry_reason`.

#### 0b. Reasoning budget per worker kind

`llm._reasoning_parameters()` reads one global `LLM_REASONING`. Call
`c810f13` spent 56,944 of its 65,853 completion tokens reasoning.

- Add `reasoning: str | int | None = None` to `llm.chat`; when set it is
  passed to `_reasoning_parameters(override)` and wins over the environment.
  `LLM_REASONING=off` stays absolute: an override cannot turn reasoning on
  where the operator turned it off.
- In `ModelGateway.complete`, read
  `activity["context_metrics"]["worker_kind"]` and look it up in a table
  `REASONING_BY_WORKER_KIND` next to the gateway. Initial values:
  `rcm_attributes: "low"`, `rcm_evidence: "low"`, `rcm_scope: "low"`,
  `cycle_linkage: "medium"`; everything else absent, meaning the global
  setting. The step-2 and step-3 worker kinds are added as they appear.
- The table lives in code, not settings: it is a property of the prompt, and
  a prompt change should be reviewed with it.

Test: gateway test asserting the request body carries
`reasoning.max_tokens == 2048` for `worker_kind: rcm_attributes` and nothing
extra for `worker_kind: apm`.

#### 0c. Not re-running the evidence call for unchanged rows

Superseded by step 1, which deletes the evidence call. Skip it if step 1 is
next.

**Invariant:** no prompt and no proposal shape changes.
**Measure:** failed runs per regeneration; reasoning tokens per call.
**Rollback:** revert.

**Landed as** (0a, 0b; 0c skipped as superseded):

- The retry could not wrap `llm.chat` alone: `ModelResponseUnusable` is raised
  at the *end* of `complete`, after metering and provenance, so the whole turn
  is the retryable unit. `complete` became a thin loop over `_complete_once`,
  which is the old body plus a `retry_reason` field. That is what the step
  wanted — the retry is reserved, metered and logged like any turn.
- `REASONING_BY_WORKER_KIND` is in `agent/runtime/reasoning.py`, not in the
  gateway module. `test_runtime_contracts_and_gateway_are_domain_neutral`
  asserts the gateway's source names no domain term, and a table of worker
  kinds is domain policy. The gateway now calls `reasoning_policy.budget_for`
  and knows nothing about which kinds exist.
- `rcm_evidence` is not in the table: step 1 deletes that call in the same
  change. `rcm_attributes` and `rcm_scope` are declared ahead of the calls that
  will send them, and are inert until steps 2 and 4.
- `LLM_REASONING` is validated even when an override will win it, so a
  misspelled setting is reported on every call rather than on whichever ones
  happen not to name a budget.

---

### Step 1. Cycle vouching out of the RCM

The largest single simplification of the RCM turn. After it, `run_rcm_worker`
makes one call and the RCM has no schema dependency. The row schema does not
change: `required_comparisons` remains the field downstream reads, and only its
author moves.

#### 1a. The RCM worker stops authoring contracts

In `agent/workers/planning.py`:

- Delete `RCM_SCHEMA_EVIDENCE_SYSTEM`, `_cycle_attribute_requests`,
  `_with_evidence_contracts`, `_merge_evidence_contracts` and
  `_downgraded_uncontracted`. `_contracted_document` becomes: parse rows,
  return `_rcm_document(request, attempt, rows)`. The whole-document re-ask in
  `_repaired_rcm` and the scoped repair both stop calling the evidence pass.
- `prompt_hash` becomes `_sha256_text(RCM_SYSTEM)`. Bump the schema hash
  string to `rcm-response:v5:...` so stale proposal sidecars are not reused.
- `RCM_SYSTEM`: the bullet beginning "A transaction_cycle attribute states
  evidence_kind and stops there" already says the right thing; shorten it to
  drop the clause about a separate step shown document schemas.
- `templates/rcm.md`: in the `control_attributes` field entry, replace the
  paragraph beginning "An attribute whose evidence strategy is
  `transaction_cycle` says so and stops there" with one sentence: the
  comparisons are decided later, against the engagement's documents, and are
  not written here. Delete the sentence in the `evidence_kind` entry that says
  `transaction_cycle` "selects an exact installed pack"; that has not been true
  since the pack registry was replaced.
- `_rcm_activity(request, "rcm")` is the only worker kind left.

#### 1b. The gate accepts an uncontracted cycle attribute

`cycle_linking.validate_control_attribute` currently routes every
`transaction_cycle` attribute through `validate_required_comparisons`, which
appends "is empty, so this attribute names no evidence contract" when the list
is missing or empty.

- Distinguish *absent* from *empty*. When `raw.get("required_comparisons") is
  None`, return the attribute without the key: it is uncontracted, which is now
  a legitimate state between the RCM and the cycle design. When it is present
  (a list, including an empty one), validate exactly as today. `schema_backed`
  keeps its definition (`required_comparisons is not None`), so every existing
  reader of that predicate now means "contracted".
- `workspaces._normalize_rcm_row` calls `validate_control_attributes` without
  a workspace on every load and every `update_rcm`, so this one change is also
  what lets committed rows sit uncontracted. `attributes_status` stays
  `valid`.
- Add a small predicate `cycle_linking.uncontracted(attribute)`:
  `evidence_kind == "transaction_cycle" and not schema_backed(attribute)`.

#### 1c. The RCM no longer depends on schemas

- `agent/workflows/audit.py`: `DEPENDENCIES["planning.rcm_ready"]` drops
  `documents.schemas_stamped`. Keep `documents.categorized` and
  `documents.types_classified`: the row still needs to know which record kinds
  exist to choose `evidence_kind`. Update the long comment above it.
  `tests.cycle_ruleset_proposed` keeps both of its edges.
- `agent/audit_execution.py` `_bind_rcm`: remove `schema_catalog` from
  `unit_input` and the comment explaining why it was there.
- `test_workflow_audit_definition.py` asserts the edge set; update it.

#### 1d. The ruleset stage asks about requirements, not comparisons

`agent/capabilities/tests.py`:

- `_cycle_attributes` currently requires `schema_backed(attribute)`. After 1b
  no attribute is schema-backed when the stage first runs, so the readiness
  would report `satisfied` and never propose. Key it on
  `evidence_kind == "transaction_cycle"` alone. Same in the row filter inside
  `_ruleset_units`.
- `_ruleset_ready` messages are unchanged. `invalidate_on=("rcm",)` stays:
  writing comparisons back (1f) changes the rows, but the next run's
  `_ruleset_units` returns `[]` when a ruleset exists, so this does not loop.
  Add a test for exactly that: propose, commit, re-expand, expect no unit.

`agent/context/adapters.py` `_cycle_requirement_candidates`:

- Emit one entry per uncontracted attribute:
  `{rcm_id, control_attribute, assertion, requirement, control, risk,
  process}`. Drop the `comparison`, `left`, `right` keys. Keep the source id
  `CYCLE_REQUIREMENT_SOURCE_ID` and the candidate's representation so the
  preset declaration is untouched.

`agent/workers/tests.py`:

- `LINKAGE_SYSTEM` gains a block: where requirements are supplied, return
  `coverage`, one entry per requirement, `{rcm_id, control_attribute,
  assertion_id}` naming the proposed assertion that answers it, or
  `{rcm_id, control_attribute, unsupported: true, reason}` where no field the
  schemas carry can express it. The existing paragraph "Where required
  comparisons are supplied, they are what the matrix has already decided..."
  is rewritten to say the requirement is stated in words and the worker
  chooses the operands. Remove the sentence about answering "in the matrix's
  own operands", which no longer has a referent.
- `_linkage_response_schema` parses `coverage` (optional; default `[]`).
- `validate_linkage_proposal`: replace `_refuse_uncovered_requirements` with
  `_require_every_requirement_answered`. For each supplied requirement whose
  `assertion` side is in scope (same in-scope rule as today, by document type
  of the roles), there must be exactly one coverage entry; a covering
  `assertion_id` must exist in `proposal["assertions"]`; an unsupported entry
  must carry a reason. Report all misses in one error string, as today.
- `_supplied_requirements` returns the new shape.

#### 1e. The ruleset executor writes the contract back onto the rows

`agent/executors/tests.py` `execute_cycle_ruleset`, inside `commit(fresh)`,
after `cycle_rulesets.save`:

- For each covered requirement, find the assertion; derive one comparison
  `{key: assertion.id, left: {document_type: roles[left.role].document_type,
  field: left.field}, right: same or None, rationale: assertion.requirement}`.
  Load the row with `fresh.rcm`, replace that attribute's
  `required_comparisons` with `[comparison]`, and call
  `fresh.update_rcm(rcm_id, {"control_attributes": attributes}, agent=True)`.
  `update_rcm` re-normalizes through `validate_control_attributes`, which is
  the same validator the old evidence pass satisfied, so a comparison naming a
  field the schema does not carry fails here rather than later.
- For each unsupported requirement, downgrade `evidence_kind` the way
  `_downgraded_uncontracted` did: `tabular_population` when
  `planning_workers._answering_table(row, tabular_answers(profiles))` finds a
  table, else `document_content`. Move that fallback into a shared helper,
  `cycle_linking.downgrade_uncontracted(workspace, row, attribute_key,
  reason) -> dict`, so the worker module stops owning it. Record the
  downgrade in the executor output as `downgraded: [{rcm_id, control_attribute,
  reason}]`.
- The ruleset unit's guarded parents are the `rcm:<id>` rows (see
  `_ruleset_units`). `mutate` checks parents before running the callback, so
  updating those rows inside the callback is allowed; the receipt's
  `postcondition_hashes` stays keyed on the ruleset hash.
- `_bind_cycle_ruleset.on_committed` in `agent/audit_execution.py` reads
  `downgraded` from the receipt and emits one `self.warn` per entry, next to
  the existing "await an auditor's approval" warning.

#### 1f. Degradation notes stay honest

`cycle_linking.unanswerable_cycle_requirements` filters on `schema_backed`, so
an uncontracted attribute would produce no note at test generation. Add a
branch: for each `uncontracted(attribute)`, note that the attribute declares
transaction-cycle evidence and no cycle design has run for it, so no cycle test
can be generated. `tests.generate` already falls back to a document question
test in that case (`_generate_cycle_candidate` returns nothing without an
approved ruleset), so behaviour is unchanged; only the note is new.

#### 1g. Tests

- `test_rcm_evidence_contract.py`: keep the contract validation cases; delete
  the two-pass replay cases (the fixture
  `tests/fixtures/rcm_operator_rejection.json` can stay as a validator
  fixture).
- `test_agent_planning_rcm_worker.py`: delete the evidence-pass and
  `_downgraded_uncontracted` cases; add: a `transaction_cycle` attribute with
  no comparisons is accepted; one with an empty list is rejected.
- `test_cycle_linkage_worker.py`: coverage parsing; every requirement must be
  answered or declared unsupported; an `assertion_id` that does not exist is
  rejected.
- New executor tests: a covered requirement gains one comparison whose
  operands match the assertion; an unsupported one is downgraded and reported;
  a comparison naming a field the schema lacks fails the commit.
- `test_agent_capabilities_tests.py`: `_cycle_attributes` finds uncontracted
  attributes; re-expansion after commit yields no unit.
- `test_rcm_central_e2e.py`: the planning template still produces the same
  number of cycle tests on the treasury fixture.

**Invariant:** the planning template produces cycle tests for the same rows
as before, because by the time `tests.specified` runs the rows carry
comparisons again. `rcm_only` produces a matrix with uncontracted cycle
attributes and no schema dependency.
**Measure:** RCM calls per run (expect 1); RCM prompt size; cycle tests on
treasuryfull equal to before; the ruleset stage's coverage list.
**Rollback:** revert; the row shape is unchanged.

**Landed as:**

- **The requirements were never reaching the model.** `run_linkage_worker` sent
  `{schemas, tables}` and nothing else, while `LINKAGE_SYSTEM` promised the
  worker the matrix's comparisons and `_refuse_uncovered_requirements` refused
  it for not answering them. The context source resolved and was dropped on the
  floor. Fixed here — the payload now carries `requirements`, read through the
  same accessor the gate reads — but it predates this change, and any earlier
  measurement of that stage was taken against a worker answering questions it
  could not see.
- **1e's stated mechanism was wrong; the intent is implemented.** The step says
  `update_rcm` "re-normalizes through `validate_control_attributes`, which is
  the same validator the old evidence pass satisfied, so a comparison naming a
  field the schema does not carry fails here". It does not: `_normalize_rcm_row`
  calls that validator *without* a workspace, which checks shape only. Exactness
  lives in `execute_rcm`'s `_validated_rcm`, which passes one. The write-back
  therefore calls `validate_control_attributes(..., workspace=fresh)` itself
  before `update_rcm`, and a bad field fails the commit as the step intended.
- `downgrade_uncontracted(workspace, row, attribute_key)` takes no `reason`: a
  row has nowhere to put one, and the executor already reports it. It takes an
  optional precomputed `answers`, because the fallback profiles every table and
  a row with several unsupported attributes would otherwise do it per attribute.
  It reaches `_answering_table` and `tabular_answers` by a lazy import of
  `agent.workers.planning` — core importing an agent module, which
  `documents.py` already does at module level, but a smell worth naming.
- `cycle_linking.distinct_comparisons` lost its last caller with
  `_refuse_uncovered_requirements` and was deleted.
- **A residual window.** The write-back runs inside `commit(fresh)` after
  `cycle_rulesets.save`, as written. A process death between the two leaves a
  saved ruleset and uncontracted rows; `reconcile_cycle_ruleset` then reports
  `already_applied` and the stage settles. Not silent — 1f's note says the
  attribute has had no cycle design and is untested, and `tests.generate` falls
  back to a document question — but not repaired either. Reversing the order
  trades it for a different visible failure (rows contracted against assertions
  no saved ruleset contains), so it was left as the step specifies.
- **The frontend carried the old contract.** `RcmSchemaCycleAttribute` typed
  `required_comparisons` as required, and the editor wrote `[]` when switching
  an attribute to `transaction_cycle` — an empty contract, which the backend
  refuses. Both now produce and accept the uncontracted state.
- The closure reorders: `planning.rcm_ready` now precedes
  `documents.evidence_read` and `documents.schemas_stamped`. That is the point
  of the dropped edge — the matrix no longer waits on the extraction pass —
  and it moves the milestone the memorandum hands off to.

---

### Step 2. One call to two: rows, then attributes

Splits the classification job off the judgment turn. The persisted proposal
shape does not change, so the executor, approval flow and reconciliation are
untouched.

#### 2a. Two templates

Split `templates/rcm.md` into `rcm.md` (everything except the
`control_attributes` and `evidence_kind` field entries and the sentence in
"What a row is" about attributes) and a new `templates/rcm_attributes.md`
carrying exactly those. `templates_store.get_template(workspace, name)`
already serves any name and honours a workspace override at
`Templates/<name>.md`, so a firm that customised the attribute rules can
override the new file. Migration note for existing overrides: an override of
`rcm.md` that still contains the attribute section is harmless, because the
rows call ignores fields it does not ask for; say so in the template's
comment.

Preset `planning.rcm` in `agent/context/presets.py` gains a second template
source, `rcm_attributes_template`, same selector, `max_characters=8_000`.
`rcm_scope` in `adapters.py` supplies it the same way it supplies
`rcm_template`.

#### 2b. Two system prompts

In `agent/workers/planning.py`:

- `RCM_ROWS_SYSTEM` is `RCM_SYSTEM` with the four attribute bullets removed
  (the `control_attributes` shape, the enumeration rule, the `evidence_kind`
  table, the "No control identified still chooses evidence_kind" rule, and the
  transaction-cycle bullet). The row list in its first sentence drops
  `control_attributes`.
- `RCM_ATTRIBUTES_SYSTEM` carries those bullets and asks for
  `{attributes: [{row_index, control_attributes: [...]}]}`, one entry per
  supplied row, `row_index` copied exactly.
- `prompt_hash = _sha256_text(RCM_ROWS_SYSTEM + RCM_ATTRIBUTES_SYSTEM)`.

#### 2c. The worker sequence

`run_rcm_worker`, initial attempt:

1. `gateway.complete(RCM_ROWS_SYSTEM, _rcm_judgment_user(request), _rcm_activity(request, "rcm_rows"))`.
2. `rows = _parsed_rows(response)`; on parse failure return the raw response
   so the registry rejects it and the repair path handles it, as today.
3. `gateway.complete(RCM_ATTRIBUTES_SYSTEM, _attributes_user(request, rows), _rcm_activity(request, "rcm_attributes"))`, where the user message is
   `{"ROWS": [{row_index, process, risk, control, control_type}], "TABLES":
   table metadata (names and column names only), "DOCUMENT TYPES HELD":
   [names], "ACTIVE ATTRIBUTE TEMPLATE": rcm_attributes.md}`. Tables and
   document types come from the bundle's `table_metadata` source and from
   `document_classification` counts already computed for the schema catalog;
   no field vocabulary.
4. `_merge_attributes(rows, response)`: the same splice `_merge_evidence_contracts`
   performed, keyed on `row_index`; a row the response omits is left without
   attributes and fails the gate with the existing "missing control_attributes"
   error.
5. `_rcm_document(request, attempt, merged)`.

`_RCM_REQUIRED_FIELDS` is unchanged; it is checked on the merged rows.

#### 2d. Repair routes by error path

`_partition_rcm_rows` already collects per-row errors with paths. Tag each
failure with `stage: "attributes"` when every error for that row starts with
`RCM row N: control_attributes` and `stage: "rows"` otherwise. In
`_repaired_rcm`:

- Rows failing at `rows` go to the existing scoped call-1 repair (same
  `ROWS TO CORRECT` envelope, `RCM_ROWS_SYSTEM`), and then call 2 runs for
  exactly those rows and is merged.
- Rows failing only at `attributes` go to a scoped call-2 repair:
  `{"ATTRIBUTES TO CORRECT": [{row_index, row, current_attributes, errors}]}`
  under `RCM_ATTRIBUTES_SYSTEM`.
- Document-level errors: the one remaining check, `_asserts_agreement`, reads
  attribute requirement text, so the whole-document re-ask becomes a call-2
  re-ask over all rows with the error appended. Call 1 is never re-run for a
  document-level error again.

`_RCM_MAX_REPAIR_ATTEMPTS` stays 1. Quarantine on the last attempt is
unchanged.

#### 2e. Bookkeeping

- Schema hash `rcm-response:v6:...`.
- `base._rcm_row_progress`, which narrates streamed rows from the `rows`
  array, keeps working on call 1; call 2 streams nothing it recognises, which
  is fine.
- Worker kinds `rcm_rows` and `rcm_attributes` appear in the debug console;
  add `rcm_attributes: "low"` to the step-0b table.

#### 2f. Tests

`test_agent_planning_rcm_worker.py`: a fake gateway that answers call 1 and
call 2 in order; merged rows validate; a row omitted by call 2 fails with the
existing error; an attribute-only failure triggers a call-2 repair and no
call-1 repair; a narrative failure triggers call 1 then call 2 for the failing
rows only; the agreement document gate re-asks call 2 only. Template tests:
`rcm.md` no longer contains "evidence_kind"; `rcm_attributes.md` does.

**Invariant:** `validate_rcm_proposal` runs on the merged rows exactly as
before; `rcm_only` regenerates end to end; the proposal shape is byte-for-byte
the same schema.
**Measure:** output tokens of call 1 (expect roughly a third to leave it);
latency of the largest call; attribute-path errors per run and which call
repaired them.
**Rollback:** revert the worker and the template split; proposals from before
and after are the same shape.

**Landed as:**

- **`templates_store` does not serve any name.** 2a says it "already serves any
  name and honours a workspace override"; `TEMPLATE_NAMES` is a closed tuple and
  `_name` raises for anything outside it. `rcm_attributes` was added to it,
  which also makes the file editable through the existing
  `GET/PUT /templates/{name}` route — the override path the step wanted.
- **The document types had to come from somewhere.** 2c has the attributes call
  shown "document types held … from `document_classification` counts already
  computed for the schema catalog", but step 1 removed the schema catalog from
  the RCM unit input. New helper
  `document_classification.evidence_type_counts(workspace)` returns names and
  counts only, and `_bind_rcm` puts it on the unit input. It is hashed into
  `unit_input_hash`, so a newly classified document invalidates a persisted
  proposal — correct, because the attributes call's vocabulary moved.
- **A document-level error must not short-circuit the row repair.** 2d routes
  document-level errors to a whole-matrix call-2 re-ask, and read literally
  that skips the scoped call-1 repair — so a draft with both a bad rating and a
  missing agreement requirement would have the rating left unrepaired, on the
  one attempt available. Implemented as: the scoped rows repair runs for the
  rows that failed, *and* the attributes call is then asked over every row with
  the document errors appended. Call 1 is still never re-asked *for* a
  document-level error.
- 2d's stage rule needed a second prefix. A row the attributes call omits
  entirely fails as `RCM row N is missing control_attributes`, which does not
  match `RCM row N: control_attributes` — and that failure is call 2's to fix.
  `_failure_stage` matches both.
- `agent:rcm_attributes` is registered in `base.MODEL_WAIT_LABELS` and in
  `_model_template_context`, so the run says what it is doing and the activity
  record names which of the two templates a turn was written against.
- **The step-0b table went live.** `rcm_attributes` was declared there ahead of
  this step; the first thing it broke was every fixture, because
  `FakeAgentLLM.__call__` had no `reasoning` parameter. The fake now accepts and
  records it.
- Test fixtures: `conftest.FakeAgentLLM` gains a scripted `agent:rcm_attributes`
  default derived from the supplied rows, and the RCM worker suite's `_Gateway`
  answers the attributes call by echoing whatever the scripted rows carry. Both
  exist so a test that is not about attributes need not script them.
- **A rule left the rows prompt that belonged to it.** "Do not split one
  risk/control into extra rows merely because it has several attributes" lived
  *inside* the `control_attributes` bullet, and went out with the block — as did
  the template's "Keep all attributes of one risk/control on the same RCM row".
  Both are row-count rules. The first expenses run after the split named a
  different `process` on all twelve of its rows. Restored to `RCM_ROWS_SYSTEM`,
  together with a rule that `process` groups rows rather than labelling them,
  and the same in `rcm.md`'s `process` entry: 22 rows over 6 processes on the
  re-run, against 12 over 12 before it. Anything moved out of a fused prompt
  needs reading for rules that belong to the half being left behind.

### Measured on `expenses`, 4 September 2026

Same memorandum, same workspace, three runs. The first predates step 2 and had
`LLM_REASONING` unset; the second and third ran with `medium`, so the split is
not the only thing that changed between the first and the rest.

| | 1 call | 2 calls | 2 calls + prompt fix |
|---|---|---|---|
| rows | 15 | 12 | 22 |
| distinct processes | 10 | 12 | **6** |
| rows per process | 1.5 | 1.0 | **3.7** |
| control attributes | 35 | 18 | 29 |
| attributes per row | 2.33 | 1.50 | 1.32 |
| control_type | 7 prev / 8 det | 12 prev / 0 det | 14 prev / 8 det |
| "No control identified" | 47% | 58% | 41% |
| coverage warnings | — | 4 | **0** |
| output tokens | 35,594 | 21,357 | 19,382 |
| largest single call | 35,594 | 14,680 | 12,382 |
| cost | $0.0176 | $0.0071 | $0.0079 |

Output tokens fell 46% while the matrix grew by seven rows, and the largest
single completion by two thirds. Attributes per row is lower and is not
straightforwardly worse: the row set is finer, so each row states a narrower
requirement, and most single-attribute rows are genuinely single-requirement
controls ("receipts for every item of PKR 1,000 or more"). Worth watching on a
cycle whose controls are compound.

**`reasoning.max_tokens` is advisory, not a cap.** The attributes call spent
10,343 reasoning tokens against the 2,048 its `REASONING_BY_WORKER_KIND` entry
asked for; the rows call spent 12,916 against 8,192 in one run and 3,558 in
another. So step 0b's table shapes deliberation without bounding it, and no
conclusion about a call's quality should be drawn from its budget — the first
reading of the thin attribute enumeration above, that the `low` entry had
starved it, is disproved by these numbers.

---

### Step 3. Two calls to three: risks, then controls, then attributes

Splits domain recall from grounded reading, the seam the quality doc's
recommendation 6 always wanted. Introduces the local citation resolver, the
string owner check, and the deterministic wording gates.

#### 3a. Three templates

`rcm.md` splits again: `rcm.md` keeps "What a row is", "Building the risk
set", "Writing the risk", and the `process`, `risk_rating` and business-cycle
field entries; a new `rcm_controls.md` takes "Writing the control", "Reading
supplied table profiles", and the `control_type`, `criteria` and
`control_owner` entries. The `criteria` entry is rewritten: quote the clause
verbatim, at most about 300 characters, or leave it empty; optionally give the
`[C…]` marker seen beside it. Preset gains `rcm_controls_template`.

#### 3b. Call 1, risks

- `RCM_RISKS_SYSTEM`: the coverage and wording rules, one risk per row, the
  rating rubric pointer, `operation`/`rcm_id` for existing rows. Requests
  exactly `operation, rcm_id, process, risk, risk_rating, business_cycle`.
- User message: `ACTIVE RISK TEMPLATE`, `REVISED APM`, `EXISTING RISKS` as
  `[{rcm_id, process, risk, risk_rating}]` projected from `current_rcm`
  (nothing else of the row), `RESOLVED CONTEXT` restricted to
  `planning_context` and `methodology` via `_context_without_sources`, and the
  two-pass `INSTRUCTIONS`. Documents, profiles, small tables and metadata are
  in the bundle and are withheld from this message.
- Gate `_validate_risk_rows`: required fields; rating enum; operation and id
  as today; and the deterministic wording rules on `risk`:
  `_PERCENT = r"\d+(\.\d+)?\s*%"`, `_COLUMN_TOKEN =
  r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b"`, and a bare-digit rule `\d` reported
  as a warning rather than an error, because a legitimate risk statement can
  carry a number. Near-duplicate detection within the response: two rows
  whose normalized risk token sets have Jaccard overlap at or above 0.8 are
  reported as one error naming both indices, and the repair is asked to merge
  them.

#### 3c. Call 2, controls

- `RCM_CONTROLS_SYSTEM`: the control rules (management asserts, "No control
  identified", never assert system enforcement, mechanics not confirmed,
  wording rules apply to control), `control_type` rubric, owner verbatim rule,
  criteria as quote. Requests, per supplied `row_index`: `control,
  control_type, control_owner, criteria, criteria_hint`.
- User message: `ACTIVE CONTROL TEMPLATE`, `RISKS` as
  `[{row_index, process, risk, risk_rating}]` from the accepted call-1 rows,
  `RESOLVED CONTEXT` restricted to `documents`, `small_table_rows`,
  `table_profiles`, `table_metadata`. No citation register: the quote replaces
  it.
- Gate `_validate_control_fields`, per row:
  - `control` non-empty; `control_type` in the enum; wording rules on
    `control` (percent, column token).
  - Aspirational openers rejected unless the control is exactly "No control
    identified": a small regex over "Formal … defines", "Standardized …
    mandates", "Finalized … specifies", "should", "would".
  - System-enforcement phrasing ("system prevents", "ERP enforces", "only …
    selectable", "blocks", "validates") is a *flag*, not an error. Flags are
    collected on the proposal as `flags: [{row_index, kind, message}]` and
    become run warnings at commit.
  - `control_owner`: normalize (casefold, collapse whitespace) and require it
    to be a substring of the concatenated supplied document text plus the APM.
    Absent: error "control_owner 'X' does not appear in the planning basis;
    leave it empty rather than naming a role the basis does not." This is
    exact and cheap, and it is the D4 defect class from round 1.
  - `criteria`: resolved by `_resolve_criteria(quote, hint, request)` below.
    Never an error.

#### 3d. The citation resolver

`rcm_citation_sheet` already walks the supplied document items and finds every
`[C…]` marker in their text. Extend it into a sentence index: for each
document item, split the text on `_SENTENCE_END` (the same regex
`document_analysis.py` uses for audit notes), and for each sentence that ends
with a marker record `(normalized sentence, ref number, citation id)`. Then:

1. Normalize the quote: casefold, collapse whitespace, strip surrounding
   punctuation and quotation marks.
2. Exact: the normalized quote is a substring of a sentence, or the sentence
   of the quote. One hit: that ref and citation. Several hits: if `hint`
   matches one of them (casefold, the same folding `_validated_criteria_refs`
   applies), that one; otherwise the first in bundle order, with a flag
   `criteria_ambiguous`.
3. Fuzzy: no exact hit; take the sentence with the highest token-set overlap
   with the quote, accept at or above 0.6, same tie-break by hint.
4. Hint only: nothing matched but `hint` is a citation id present in the
   sheet; attach that sentence's ref with a flag `criteria_unverified`.
5. Nothing: `criteria_refs: []`, keep the quote as `criteria`, flag
   `criteria_unresolved`.

The output shape is the `[{document: ref, citations: [id]}]` the executor's
`_resolved_criteria_refs` already consumes, so `RCM_OPTIONAL_ROW_FIELDS` and
the executor are untouched. `_validated_criteria_refs` is deleted from the
gate; the model no longer writes refs.

`prompts.summary_document_name` and the `_CITATION_MARKER` regex stay as the
one recogniser of the marker's spelling.

#### 3e. Call 3, attributes

Unchanged from step 2, now fed the merged rows of calls 1 and 2.

#### 3f. Repair routing

Failure stages become `risks`, `controls`, `attributes`, decided by error path
prefix as in 2d, with the three scoped envelopes. A row that fails at `risks`
is re-asked at call 1, then flows through calls 2 and 3 for that row only. The
agreement document gate still re-asks call 3 only.

#### 3g. Proposal and receipt

`validate_rcm_proposal` returns `{rows, quarantined?, flags?}`. `_validated_rcm`
in the executor reads `rows` only and ignores the rest, so nothing changes
there; `_bind_rcm.on_committed` reads `flags` from
`outcome.receipt.output` and emits a warning per flag, in the same block that
emits the theme and population warnings today. `execute_rcm` copies `flags`
from the proposal into the receipt output.

#### 3h. Tests

Worker tests for each gate with one positive and one negative case; resolver
tests for the five outcomes in 3d using a two-document fixture where one
sentence is duplicated across documents (the hint tie-break) and one is
lightly paraphrased (the fuzzy branch); a repair-routing test for each of the
three stages; an executor test that `flags` reach the receipt.

**Invariant:** as step 2.
**Measure:** rows with a percent sign or column token (expect 0,
deterministically); owner strings absent from the basis (expect 0); criteria
resolution outcomes by branch; flags per run; coverage against the quality
doc's nine checks.
**Rollback:** revert the worker and template split.

**Landed as:**

- **3d's stated output shape was wrong, and it fails silently.** The step says
  the resolver emits `[{document: ref, citations: [id]}]`, "the shape the
  executor's `_resolved_criteria_refs` already consumes". It does not: the
  executor reads `{document_id, citation_id}` per anchor, and the bundle-order
  `ref` means nothing outside the turn that assembled it — which is exactly why
  the model stopped writing one. Emitted in the documented shape, the executor
  skipped every reference and the first `expenses` run of this step committed
  eight criteria and **zero** citations, with no error anywhere. The sentence
  index now carries `document_id` and the document name alongside the ref.
- **The sentence splitter had to change.** 3d reuses `document_analysis`'s
  `_SENTENCE_END`, which splits on `(?<=[.!?])\s+`. A citation marker follows
  its sentence's full stop ("… the requisition. [C4]"), so that split puts every
  sentence in one piece and every marker in the next, and no sentence carries
  the anchor written for it. It also splits on terminators alone, and a document
  summary is markdown whose lines mostly have none — so the file name and the
  heading were swallowed into the first clause and no quote of that clause
  matched. The index now splits on line breaks as well, and re-attaches a
  leading marker to the piece before it.
- Matching normalizes harder than the step says: markers are stripped from both
  quote and sentence, and each token is stripped of attached punctuation.
  Without it `requisition.` and `requisition` were different words and a quote
  copied a clause short fell under the 0.6 floor.
- `control_type` is validated against `{preventive, detective}` — 3c asks for
  the enum, and a procurement run had written the literal `"None"` on seven
  rows. An empty value is *correct* on a "No control identified" row and
  `"None"`/`"N/A"` are cleared to empty rather than refused: naming a kind for a
  control the row says does not exist asserts mechanics the basis never
  described, which is what every earlier run did.
- Repair routing (3f) takes the *earliest* stage any of a row's errors belongs
  to, and the row then flows forward through the calls after it. A row repaired
  at risks has its control and attributes restated, because both were written
  against a risk that has changed; repairing them against the old wording
  corrects nothing.
- `_STAGE_MARKERS` routes on error text rather than on a path prefix, because
  the gate reports in sentences. An error no marker claims routes to `risks` —
  the start of the sequence — so an unrecognised failure re-asks everything
  rather than reaching a call that cannot fix it.
- The risks call is given `planning_context` and `methodology` only, through a
  new `_context_from_sources`. What is withheld is withheld from the *message*,
  not from the manifest: the unit's context identity is unchanged and every
  resolved item is still recorded as supplied.
- **The controls call is not shown the memorandum, and that was tested.** 3c
  lists its context as the resolved sources only, so whether the APM belongs
  there is a question the step leaves open. Adding it looked right — the memo's
  process description is where a control environment is written down — and it
  moved every criterion onto the memo: nine of nine on one treasury
  regeneration were quoted verbatim from it, and the memo is generated planning
  prose carrying no `[C...]` anchors, so that engagement fell from ten of ten
  criteria citing a source to none. A criterion should rest on the policy the
  entity issued, not on this memorandum's paraphrase of it. Reverted.

### One run per variant cannot measure a prompt change

The memo variant also appeared to improve control identification and attribute
enumeration, and those readings do not survive. `risk_rating` is written by the
risks call, which was byte-identical across both variants — same prompt, same
context, same inputs — and the share of rows rated high or critical still moved
**-34, -14 and +9 points** between the two runs.

| | no memo | + memo | delta |
|---|---|---|---|
| expenses | 47% | 56% | +9 |
| treasury | 74% | 40% | **-34** |
| procurement | 68% | 54% | -14 |

That is the noise floor of a single regeneration, and it is wider than almost
every difference these comparisons were being read for. Only two kinds of claim
survive it: an effect verified by *inspection* rather than by a count — the
citation displacement was, by reading where each quote came from — and a
property a gate enforces on every row, which cannot vary. Step 3's deterministic
results are of the second kind and hold: five typos to none on procurement,
seven `"None"` control types to seven correct empties, and zero percentages,
column tokens and aspirational controls across all three engagements. Every
softer reading in this section needs repeated runs before it means anything, and
the measure in each step above should be read the same way.
- **Citations are capped upstream, not here.** On `expenses` only one of eight
  quoted criteria kept a citation. The resolver was right every time it was
  tested against the text the run was actually given; the *executor* drops the
  anchor, because `document_analysis` writes `[c…]` markers into its prose that
  it never registers as citation records — nine of sixteen unregistered in one
  document, six of fifteen in another — and `_resolved_criteria_refs` looks each
  one up in that register. `treasury`, whose analyses register what they write,
  resolved ten of ten. Worth noting which direction the error runs: the earlier
  path had the model pick an id out of a list and it tended to pick low-numbered
  ones, which happen to be the registered ones, so it *scored* better while
  citing sentences nothing had checked. This resolver picks the marker beside
  the quoted sentence and is more often right and more often dropped.

---

### Step 4. Stage 0 and parallelism

The structural change. Everything before it has already shrunk each call; this
makes the calls per-process and concurrent, and closes the `process`
vocabulary.

**4a and 4b are superseded by step 5.** The artifact 4a describes is step 5's
cycle shape with the roles and populations left off, and the capability 4b
adds is step 5's `planning.cycle_ready`. Read `planning["scope_map"]` as
`planning["cycle"]`, `planning:scope` as `planning:cycle`, `planning.scope_ready`
as `planning.cycle_ready` and *bucket* as *step* throughout 4c to 4g; the
closed `process` vocabulary 4e describes lands with step 5d, ahead of the
per-step units. 4a and 4b are kept below as written, for the reasoning.

#### 4a. The scope map artifact

Stored in `workspace.planning["scope_map"]`:

```json
{
  "cycles": [
    {
      "name": "Treasury dealing and settlement",
      "buckets": [
        {"name": "Treasury dealing", "description": "Front-office execution within limits and authorisation.", "themes": ["Ghost or unauthorised dealing."]},
        {"name": "Treasury confirmation", "description": "...", "themes": ["Confirmation"]},
        {"name": "Treasury settlement", "description": "...", "themes": ["Settlement", "Examine dual-control and SSI failures"]}
      ],
      "cross_cutting": {"name": "Treasury operations", "description": "Override, monitoring and segregation across the cycle.", "themes": ["Fraud risks considered"]}
    }
  ],
  "created_by": "agent",
  "agent_run_id": "…",
  "apm_sha1": "…",
  "updated": "…"
}
```

- `workspaces.update_planning` has a closed `allowed` set; add `scope_map`,
  with validation in a new `workspaces.validate_scope_map`: at least one
  cycle, at least one bucket per cycle, names unique per cycle after
  casefold, names at most 60 characters, every bucket has a description.
  Auditor edits go through the existing `PATCH /planning` route; a small
  editor in the planning UI is follow-up frontend work and not a blocker,
  because the map is readable and editable as JSON through that route today.
- `workspace_transactions.artifact_projection` gains `planning:scope`, the
  material projection of the map's cycles and buckets (not its provenance
  fields), so it can be a guarded parent.

#### 4b. The scope worker and capability

- Worker `planning.scope` in `agent/workers/planning.py`. System prompt of a
  few hundred characters: name the in-scope processes of each business cycle
  as the memorandum names them, one line each, one cross-cutting bucket per
  cycle, and assign every supplied theme to exactly one bucket. Input: the
  APM, `planned_risk_themes(apm)` computed locally, and the distinct `process`
  values of existing rows as `EXISTING PROCESS NAMES` to be reused verbatim
  where they still apply. Output: the map above minus provenance. Validator:
  `validate_scope_map` plus: every supplied theme appears exactly once; a
  theme the model left out is assigned to the cross-cutting bucket locally
  rather than rejected; more than 12 buckets in a cycle is rejected. One
  repair. Worker kind `rcm_scope`, reasoning `low`.
- Preset `planning.scope`: `planning_context`, `current_apm`, `current_rcm`
  (the row projection reduced to `process`). Privacy: planning context,
  template text, nothing else.
- Executor `planning.scope`: `update_planning({"scope_map": ..., "agent_run_id": ..., "created_by": "agent"}, agent=True)`
  under `expected_parents={"planning:apm": ...}`; reconciler compares the
  stored map's material projection to the proposal.
- Capability `planning.scope_ready` in `agent/capabilities/planning.py`:
  readiness `satisfied` when a valid map exists whose `apm_sha1` equals the
  current APM hash, `missing` otherwise (an auditor-edited map keeps its
  `apm_sha1`, so edits survive until the APM changes); one unit via
  `_single("scope", "Map the processes in scope", "planning:apm")`;
  `invalidate_on=("planning:apm",)`. Add it to `CAPABILITY_IDS` and
  `_BUILDERS`; add `"planning.scope_ready": ("planning.apm_ready",)` to
  `DEPENDENCIES` and make `planning.rcm_ready` depend on it. The closure pulls
  it into every template that reaches the RCM, so `TEMPLATE_OUTCOMES` needs no
  change. Binder `_bind_scope` in `agent/audit_execution.py`, registered in
  the binder table beside `_bind_apm`.

#### 4c. Units per bucket

- `_rcm_units(workspace, scope)` replaces `_single("rcm", ...)`: for each
  cycle and bucket, `UnitSpec(f"rcm:{slug(cycle)}:{slug(bucket)}",
  "rcm_bucket", f"Draft matrix rows for {bucket}", ("planning:apm",
  "planning:scope"), {"cycle": cycle, "bucket": bucket, "description": ...,
  "themes": [...]})`. The runner persists only `kind`, `title`,
  `parent_refs` and `input_sha1` on the unit (see
  `workflow_runner.ensure_stage_units`), so `_bind_rcm` re-derives the bucket
  by parsing the unit id and looking it up in `planning["scope_map"]`; the
  `input_sha1` still moves when the bucket's definition changes.
- `planning.rcm_ready` declares `barrier="all_settled_parallel"`. Its
  executor commits with `expected_parents` only, and `mutate` skips the
  revision check when parents are given, so sibling commits under the same
  APM and scope hashes do not conflict. `tests.specified` already commits per
  row under this barrier and the executor contract at
  `executors/model.py` tolerates a revision newer than the snapshot, so the
  path exists; confirm with a two-bucket integration test before relying on
  it.
- `_validated_rcm` in `agent/executors/planning.py` requires
  `set(expected_parents) == {"planning:apm"}`; relax to
  `{"planning:apm", "planning:scope"}`. `RcmExecutorTarget` is unchanged.
- The adapter holds `self.ws` as shared state and `context_provider` reads it
  from the worker thread. Binding and folding stay on the main thread
  (`_run_parallel_pipeline_units` says so); a bucket's context may see a
  sibling's freshly committed rows, which only affects the `current_rcm`
  projection and is harmless. Do not read `self.ws` inside the executor.
- Readiness `_rcm_ready` becomes per bucket: `satisfied` when at least one
  row's `process` equals the bucket name, `missing` for buckets with none;
  the unit list is the missing buckets, so a rerun regenerates only what is
  absent, and `generation_mode` decides whether present buckets are
  re-expanded.

#### 4d. Bucket-scoped context

`rcm_scope(workspace, *, document_ids, bucket)` in `adapters.py`:

- `rcm_query` becomes the bucket name, description and theme names joined.
  The `documents` source's `documents.lexical` selector already ranks by
  `query_fields: ["rcm_query"]` (`resolver._lexical_matches`), so document
  selection is per bucket with no new selector. Lower the preset's document
  `item_limit` from 12 to 6 and `max_characters` from 40k to 20k in a new
  preset id `planning.rcm_bucket`, leaving `planning.rcm` for the
  cross-cutting unit.
- Tables: filter `apm_table_profile_candidates` and
  `apm_table_metadata_candidates` to the tables
  `planning_workers._answering_table`-style scoring ranks against the bucket
  tokens (`TABLE_NAME_WEIGHT`, `MIN_TABULAR_RELEVANCE` as they stand), plus
  every table when nothing scores, so an unmatched bucket degrades to today's
  behaviour rather than to nothing. Small tables are always all included.
- `current_rcm`: rows whose `process` equals the bucket name.
- APM: whole memo in this step, so the shared prefix is identical across
  units and provider caching applies. Slicing by heading match
  (`_section_bodies` already exists) is a later optimisation with its own
  measure.

#### 4e. The calls, per bucket

Calls 1 to 3 from step 3, with these changes to call 1:

- `THIS PROCESS`: name and description. `THEMES THIS PROCESS MUST OWN OR
  DECLINE`: the bucket's themes. Output may carry `declined_themes:
  [{theme, reason}]` beside `rows`.
- `process` on every row must equal the bucket name; anything else is a
  row error naming the allowed value. This is what closes the vocabulary.
- Gate: every listed theme is either declined or owned; ownership reuses
  `_theme_ownership` against the bucket's rows only. The theme was handed in
  by name, so lexical ownership is now a fair test at this scope, and the
  repair is one bucket's call 1. Declined themes go to the receipt and become
  warnings.
- `operation`/`rcm_id` leave the prompt. `EXISTING RISKS` for the bucket are
  still shown, with the instruction to reuse a risk's exact wording when it is
  the same risk. `match_rcm_revision` gains a near-duplicate branch: within
  the same `process`, a proposed risk whose normalized tokens overlap an
  existing agent-created row's at or above 0.8 is treated as an update of it.
  Auditor-owned rows are preserved by the existing `created_by != "agent"`
  rule.

#### 4f. The cross-cutting unit

Capability `planning.rcm_cross_ready`, depending on `planning.rcm_ready`,
`barrier="all_settled_then_validate"`, one unit per cycle, worker
`planning.rcm` with `unit_input.kind == "rcm_cross"`. Call 1 is shown the
accepted rows of the cycle as `ALREADY COVERED BY THE PROCESS ROWS` and the
cross-cutting themes, and asked only for risks no process row owns; calls 2
and 3 as usual. Readiness: `satisfied` when the map's cross-cutting entry
carries `reviewed_run_id`, which the executor sets even when the unit commits
zero rows (the worker returns `rows: []` with `none_needed: true`, which the
gate accepts only for this kind). The same unit runs the local dedup pass:
normalized pairwise comparison of `risk` across all rows of the cycle; exact
matches after normalization are reported as one warning per pair, and nothing
is deleted automatically in this step.

#### 4g. Tests

Scope worker and validator; map editing through `update_planning`;
`planning:scope` as a parent projection; unit expansion per bucket and the
id-to-bucket lookup; a two-bucket parallel run committing rows without
conflict; `process` closed-vocabulary error and repair; theme own-or-decline
gate; near-duplicate matching in `match_rcm_revision`; the cross-cutting unit
seeing sibling rows; degenerate map (one bucket) reproducing step 3's
behaviour.

**Invariant:** `rcm_only` and the planning template regenerate treasuryfull
and procurement end to end; proposals per unit are the same shape as today's
whole-document proposal restricted to one bucket, so the executor is
unchanged apart from the parent set.
**Measure:** wall-clock per regeneration; tokens per regeneration; largest
single call; rows per bucket; duplicate pairs; whether the treasuryfull
cross-cutting risks land in the cross-cutting unit rather than in every
bucket; how many buckets a rerun actually regenerates.
**Rollback:** the scope capability can stay while `_rcm_units` reverts to a
single unit and the parent set reverts to `{"planning:apm"}`; nothing
downstream reads the map.

---

### Step 5. Cycle design after the APM

Agreed on 5 September 2026 against the mockups in
[`ui-plans/cycle-design-evaluation.md`](ui-plans/cycle-design-evaluation.md);
the design canvas linked there is the reference for the page, and the
*Simplified view* page of it is what is built. Depends on step 1 only. Next in
build order, after step 3 and ahead of step 4.

After step 1 a matrix row says a requirement needs linked source records and
stops. The cycle those records form is then authored at
`tests.cycle_ruleset_proposed`, after the matrix and after the schemas, in one
turn that invents the roles and chooses the fields together. Two things are
wrong with that. The turn that names the roles is the one that knows least
about the process: the steps of the cycle are stated in the memorandum's
*Process flow and understanding* section, in order, and on `procurement` the
matrix's four `process` values are those four step names — the structure
exists and nothing in planning holds it. And the matrix has no vocabulary for
`process` at all; the prompt rule step 2 restored (*process groups the rows*)
is exhortation doing what an artifact should do by construction.

The constraint that shapes the step: the field half of a cycle needs the
induced schemas, the schemas need the evidence read, and step 1 took that
wait out from in front of the matrix on purpose. So the cycle is **one
artifact in two layers**. The *shape* — steps, the document roles each step
holds, the population table each step reads, the themes each step owns — is
authored after the APM from nothing extracted, and the matrix depends on it.
The *bindings* — the anchor field, the join keys, the assertions — stay where
they are authored today, after the schemas, but the roles are no longer the
model's to invent. One page draws both, and fills in as the engagement moves.

#### 5a. The artifact

`workspace.planning["cycle"]`:

```json
{
  "name": "Procure-to-pay",
  "steps": [
    {"name": "Requisition initiation and approval",
     "roles": [{"name": "requisition", "document_type": "purchase_requisition"}],
     "populations": [{"table": "requisitions"}],
     "themes": ["Authorisation against limits", "Segregation of duties"]},
    {"name": "Purchase order",
     "roles": [{"name": "order", "document_type": "purchase_order"}],
     "populations": [{"table": "po_data", "anchor": true}],
     "themes": ["..."]},
    {"name": "Goods receipt and inspection",
     "roles": [{"name": "receipt", "document_type": "goods_receipt"}],
     "populations": [{"table": "po_data", "columns": ["GRN_ID", "GRN_DATE", "GRN_STATUS"]}],
     "themes": ["..."]},
    {"name": "Invoice processing and payment",
     "roles": [{"name": "invoice", "document_type": "vendor_invoice"},
               {"name": "voucher", "document_type": "payment_voucher"}],
     "populations": [{"table": "invoice_data"}],
     "themes": ["..."]}
  ],
  "cross_cutting": {"name": "Procurement operations", "themes": ["Fraud risks considered"]},
  "created_by": "agent", "agent_run_id": "…", "apm_sha1": "…", "updated": "…"
}
```

- `workspaces.update_planning` gains `cycle` in its `allowed` set, with the
  same provenance rule as the APM: `agent_run_id` and `created_by` are the
  workbench's to write, and an auditor's edit of the cycle sets
  `created_by: "user"`. `apm_sha1` is kept across an auditor edit, so edits
  survive until the memorandum changes (as 4b specified). There is no
  separate confirmation state: an agent draft is `created_by: "agent"`, an
  auditor's edit is the confirmation, and readiness never waits on it — a
  permission-mode run must not block the matrix on a review the auditor may
  never make.
- `workspaces.validate_cycle`: a name; at least one step and at most 12;
  step names unique after casefold and at most 60 characters; every role name
  unique across the cycle and a `cycle_rulesets.valid_rule_id`; every role's
  `document_type` in `document_schemas.effective_type_ids` and not `other`;
  every population `table` in `workspace.table_names()` and a base table, not
  a join; at most one population in the cycle flagged `anchor`; every theme
  assigned to exactly one step or to `cross_cutting`. The validator runs on
  every `update_planning` and on every load of the page, the way
  `_normalize_rcm_row` does for rows.
- `workspace_transactions.artifact_projection` gains `planning:cycle`: the
  material projection of `name`, `steps` (name, roles, populations, themes)
  and `cross_cutting`, not the provenance fields. It is a guarded parent of
  the matrix and of the ruleset proposal (5d, 5e).
- Route: the existing `PATCH /planning` carries it, as 4a intended. A page
  edit is a PATCH of the whole `cycle` object.

#### 5b. The worker

Worker `planning.cycle` in `agent/workers/planning.py`, preset
`planning.cycle` in `agent/context/presets.py`, adapter `cycle_scope` in
`adapters.py`.

- Input, all on the unit input and none of it extracted: the APM;
  `planned_risk_themes(apm)`; `document_classification.evidence_type_counts`
  (names and counts, the same helper step 2 added for the attributes call);
  the base tables with their column names, from
  `workspace.get_frame(name).columns`, under `allow_table_metadata` — names
  only, no profile and no rows; `workspace.joins` as `(left, right, left_on,
  right_on)`; and, for reuse verbatim, the existing cycle's step names and the
  distinct `process` values of existing rows.
- System prompt of a few hundred characters: name the steps of the process
  as the memorandum names them, in the order it gives them; for each step,
  the document types that record it, chosen from the supplied list; the table
  whose rows are that step's population, chosen from the supplied list, or
  none, with the columns that hold it where the population lives on another
  step's table (the GRN case); flag the one population a cycle test would
  start from; name one cross-cutting bucket; assign every supplied theme to
  exactly one step or to the cross-cutting bucket. Output is the artifact
  minus provenance.
- Gate: `validate_cycle`, plus: a document type or table not in the supplied
  lists is an error naming the allowed values; a theme the model left out is
  assigned to the cross-cutting bucket locally rather than refused (as 4b
  said); a theme assigned twice is an error. One repair.
- Worker kind: the `rcm_scope` entry step 0b declared ahead of time is this
  call. Rename it `planning_cycle` in `REASONING_BY_WORKER_KIND`; reasoning
  `low`. Register `agent:planning_cycle` in `base.MODEL_WAIT_LABELS` as step 2
  did for the attributes call.
- Privacy: `allow_planning_context` and `allow_table_metadata`. No document
  text, no profiles, no small-table rows.

#### 5c. Capability, binder, executor

- `planning.cycle_ready` in `agent/capabilities/planning.py`, between
  `planning.apm_ready` and `planning.rcm_ready` in `CAPABILITY_IDS` and
  `_BUILDERS`. Readiness: `satisfied` when a valid cycle exists whose
  `apm_sha1` equals the current APM hash; `missing` otherwise; one unit via
  `_single("cycle", "Design the cycle", "planning:apm")`;
  `invalidate_on=("planning:apm",)`; `context="planning.cycle"`.
- `agent/workflows/audit.py`: `"planning.cycle_ready": ("planning.apm_ready",
  "documents.types_classified")`, and `planning.rcm_ready` gains the edge
  `planning.cycle_ready` (keeping `documents.categorized` and
  `documents.types_classified`, which the cycle now also implies; the comment
  above it is rewritten). `tests.cycle_ruleset_proposed` gains the same edge.
  `documents.types_classified` was already in front of the matrix, so the
  closure's order does not move; `TEMPLATE_OUTCOMES` is unchanged because
  every template that reaches the matrix pulls the cycle in.
  `test_workflow_audit_definition.py` asserts the edge set; update it.
- `engagement.phase_of_capability` files it under *Plan the engagement* by
  its `planning.` prefix with no change. `CAPABILITY_LABELS` in
  `frontend/src/components/agent/capabilityLabels.ts` gains
  `'planning.cycle_ready': 'Cycle design'`, and
  `test_plan_spine_capability_labels.py` holds the two in step.
- Binder `_bind_cycle` in `agent/audit_execution.py` beside `_bind_apm`, in
  the binder table. Executor `execute_cycle` and `reconcile_cycle` in
  `agent/executors/planning.py`, after `execute_apm`: commit is
  `fresh.update_planning({"cycle": ..., "created_by": "agent", "agent_run_id":
  ...}, agent=True)` under `expected_parents={"planning:apm": …}`; the same
  auditor-edit-preserved rule as the APM (`created_by == "user"` refuses an
  agent overwrite in permission mode, and `allow_auditor_overwrite` lifts it
  in auto mode); reconciliation compares the stored cycle's material
  projection to the proposal's. `on_committed` records
  `planning:cycle` as an artifact and counts it in `planning_changes`.

#### 5d. What the matrix takes from it

- `_bind_rcm` puts the cycle's step names and the cross-cutting name on the
  unit input as `PROCESS NAMES`, and the cycle's name as the row's
  `business_cycle`. Both are hashed into `unit_input_hash`, so a changed shape
  invalidates a persisted proposal — correct, because the vocabulary moved.
  `planning:cycle` joins `planning:apm` in the unit's `parent_refs`, and
  `_validated_rcm`'s parent-set check widens to `{"planning:apm",
  "planning:cycle"}` (4c widens it again).
- `RCM_RISKS_SYSTEM` (the call that writes `process` since step 3): the
  `process` rule step 2 restored becomes *choose `process` from PROCESS
  NAMES, spelled exactly*; the sentence about naming steps yourself where
  the basis does not goes, because the cycle already did. Same in
  `templates/rcm.md`'s `process` entry.
- `_validate_risk_rows`: a `process` outside the supplied names is a
  **flag** when this lands — `{row_index, kind: "process_outside_cycle",
  message}` in the proposal's `flags`, which step 3g already carries to the
  receipt and `_bind_rcm.on_committed` already emits as a run warning; it
  becomes a row error, routed to the risks stage and repaired in that scoped
  call, once one treasuryfull and one procurement regeneration have been
  read. The theme ownership gate is unchanged: the step's themes are shown as
  they are today, and 4e's per-step own-or-decline replaces it later.
- Nothing else in the matrix changes. Rows, attributes, the executor and the
  reconciliation are untouched.

#### 5e. The ruleset stage takes the roles

The stage keeps its place and both of its edges. What changes is that the
roles, the anchor's table and column, and the order of the cycle are inputs,
not outputs.

- `_bind_cycle_ruleset`: the cycle on the unit input, and `planning:cycle` in
  the unit's `parent_refs` beside the schema refs, so a reshaped cycle
  conflicts a stale proposal the way a re-derived schema does.
- `run_linkage_worker`: the payload gains `cycle: {steps: [{name, roles:
  [{name, document_type}], population: {table, anchor}}]}`. The schemas,
  tables and requirements travel as they do now.
- `LINKAGE_SYSTEM`: the paragraph that has the worker name the roles is
  replaced by one saying the roles are given, in the order the cycle runs,
  and are used as named; the anchor's role is given and the worker supplies
  the identifier field on it; join keys and assertions as today. The anchor
  in the response is `{role, field}` only — `table` and `column` are filled
  in by the executor from the shape before `cycle_rulesets.save`, which is
  the governing rule of this document applied: the model names things, local
  code finds things.
- `validate_linkage_proposal`: every role in the response is one the cycle
  declares, with the cycle's `document_type`; a role the cycle declares that
  the response omits is an error, unless it is listed under `unreachable:
  [{role, reason}]`, which the worker returns for a role whose document type
  induced no identifier field (the `purchase_requisition` case on
  `procurement`: fourteen fields, none of role `identifier`, so no join key
  can reach it and `cycle_rulesets.validate` would refuse the whole ruleset).
  `execute_cycle_ruleset` drops an unreachable role from the stored ruleset,
  reports it in the receipt output as `unreachable`, and
  `_bind_cycle_ruleset.on_committed` emits one warning per entry next to the
  downgrade warnings step 1e added. The shape keeps the role: it is still a
  step in the process, and the page draws it greyed with the reason.
- `cycle_rulesets.validate` records `cycle_sha1`, the hash of the shape's
  material projection, on the record beside `schema_refs`. It is not part of
  `ruleset_hash`, for the reason `measured` is not: an approved ruleset is
  what was approved.

#### 5f. The page

The *Simplified view* page of the design canvas is the specification; the
exact markup is `ui-plans/cycle-design/Main.dc.html` and
`CycleStrip.dc.html`, drawn from the `procurement` workspace.

- **Route.** A planning section `cycle` in
  `frontend/src/composables/useWorkspaceNavigation.ts`, between `apm` and
  `coverage`, answered in `views/AuditFileView.vue` by a new
  `components/planning/CycleTab.vue`. Breadcrumb *Engagement record /
  Planning / Cycle*.
- **Endpoint.** `GET /planning/cycle/graph` in `routes/planning_routes.py`,
  read-only, no model call:
  `{steps: [{name, documents: [{document_type, label, count, fields:
  [{name, role}]}], population: {table, rows, columns: [...], anchor,
  note}}], edges: [{kind, from: {step, node, field}, to: {step, node, field},
  rule_id}]}`. Assembled from the cycle, `evidence_type_counts`,
  `document_schemas.list_schemas`, `workspace.joins` between base tables, and
  `cycle_rulesets.effective` or else the latest proposed ruleset. `kind` is
  one of `join` (a join key), `assert` (an assertion with two operands),
  `anchor` (population column to role field), `table_join`. One-operand
  assertions are not edges; they are a `stated` mark on the field. Fields
  are listed only where they take part in an edge, in the order the edges
  leave or enter the node — the page shows the vocabulary of the rules, not
  the schema. Cached through `projection_cache` on the workspace root, the
  way the document-test listing is.
- **Layout.** One strip that scrolls to the right. One column per step, in
  order, with the step's name as a band across the top; a step with two
  roles spans two columns. The document node sits above the population node.
  A step without a population of its own shows a dashed placeholder naming
  the table and columns that hold its rows (from `populations[].columns`).
  Before the schemas exist, document nodes carry no fields and the strip
  shows the flow between steps, the populations with their columns and the
  table joins; the field edges appear when a ruleset does.
- **Arrows.** Orthogonal. An arrow leaves the right edge of its source field
  and enters the left edge of its target field. Between neighbouring columns
  it takes a vertical track in the gutter; an arrow that skips a column rides
  a horizontal bus above the documents (or below the populations), one lane
  per arrow, so it never crosses a node. Tracks and lanes are assigned to
  minimise crossings: start from list order and swap pairs while a swap
  reduces the count of segment intersections, riders swapped wholesale
  (both tracks and the lane) as well as singly. With rows ordered to follow
  the arrows the procurement strip routes with no crossing but the shared
  stub of a field that feeds two arrows. The layout is a pure module,
  `components/planning/cycleLayout.ts`, unit-tested on the procurement
  fixture for zero crossings; `CycleStrip.vue` renders HTML nodes over one
  SVG edge layer.
- **Chrome.** Header: the cycle's name, a count line (steps, document types,
  populations, and the ruleset's status), *Edit steps* and *Review rules*.
  No side panel, no status chips. *Edit steps* opens a `UiDefinitionDrawer`
  with the step list: reorder, rename, change a role's document type, assign
  a population, flag the anchor; save is one `PATCH /planning`. *Review
  rules* opens the existing `CycleRulesetReview` dialog; the document tests
  tab's *Cycle rules* action points at the same dialog and is left alone.
  Four kinds in the legend, and the line *only fields that take part in a
  relationship are shown*.
- **`PlanningPayload`** gains `cycle`; `types.ts` gains the graph shape.

#### 5g. Tests

- `test_planning.py`: `validate_cycle` positive and each negative; an
  auditor edit keeps `apm_sha1` and sets `created_by`; `planning:cycle`
  projection excludes provenance.
- New `test_agent_planning_cycle_worker.py`: a scripted response validates;
  a table or type outside the supplied lists is refused with the allowed
  values named; a missing theme lands in cross-cutting; a duplicated theme is
  refused; one repair.
- `test_agent_planning_executor.py`: commit under the APM parent; conflict
  when the APM moved; auditor edit preserved in permission mode and
  overwritten in auto mode; reconcile `already_applied`.
- `test_workflow_audit_definition.py`: the new edges.
  `test_plan_spine_capability_labels.py`: the new label.
- `test_agent_planning_rcm_worker.py`: a `process` outside PROCESS NAMES is
  flagged and the flag reaches the receipt (then, once promoted, refused and
  repaired in the risks call).
- `test_cycle_linkage_worker.py`: a role not in the cycle is refused; a
  declared role omitted without an `unreachable` entry is refused; the anchor
  table and column are filled from the shape. `test_cycle_ruleset_write_back.py`
  (or a sibling): an unreachable role is dropped from the stored ruleset and
  reported. `test_cycle_rulesets.py`: `cycle_sha1` stored and outside
  `ruleset_hash`.
- `test_cycle_linking_routes.py` or a new `test_planning_cycle_routes.py`:
  the graph endpoint on the treasury fixture — node and edge counts, only
  rule-bearing fields, `stated` marks, the pre-schema shape.
- Frontend: `cycleLayout.test.ts` asserts zero crossings on the procurement
  fixture and that a rider never enters a node's column between its
  vertical extent; `CycleTab.test.ts` renders the two states.
- `test_rcm_central_e2e.py`: the planning template on the treasury fixture
  still produces the same cycle tests, now with the cycle stage in the
  closure.

**Invariant:** `rcm_only` and the planning template regenerate treasuryfull
and procurement end to end with one more stage in the closure and no new
model call after the shape; the row schema, the ruleset schema (plus one
recorded hash) and the approval are unchanged; a workspace with no cycle
still generates document-question tests.
**Measure:** distinct `process` values per regeneration against the shape's
step count (expect equal); rows whose `process` is outside the shape (expect
0 once promoted to an error); roles the ruleset stage drops as unreachable,
with reason; ruleset proposals refused for a role the shape does not declare
(expect 0 after one repair); the cycle call's tokens and latency (expect it to
be the smallest call in the run).
**Rollback:** the capability can stay while `_bind_rcm` stops supplying
PROCESS NAMES and the linkage worker goes back to naming its roles; nothing
downstream reads the shape except through those two inputs. The page reads a
missing cycle as its empty state.

**Landed as:**

- **The validator could not live where 5a puts it.** 5b has the drafting gate
  run `workspaces.validate_cycle`, and
  `test_agent_final_boundaries.py::test_a_worker_cannot_reach_a_workspace_a_transaction_or_the_run_store`
  forbids `workers/` from importing `app.workspaces` at all — correctly, since a
  worker is a pure function of its supplied bundle. The structural half is now
  `planning_cycle.validate_cycle_shape`, a pure function taking the two
  vocabularies as arguments; `workspaces.validate_cycle` resolves them from the
  workspace and delegates. One validator, two callers: the turn checks a
  proposal against the lists it was handed, the commit checks it against what
  the engagement holds, and a shape that passes the first cannot surprise the
  second. It collects every problem rather than raising at the first, because
  the one repair should see all of them.
- **The cycle's provenance is inside the cycle, not on the planning object.**
  5c writes `update_planning({"cycle": ..., "created_by": "agent",
  "agent_run_id": ...})`, and those two keys are the *memorandum's*: setting
  them on a cycle commit clears the `created_by: "user"` marker that stops an
  agent run from overwriting an auditor's APM. The cycle carries its own, as 5a's
  JSON already shows, and `update_planning` stamps them.
- **Committing a cycle must not restamp the memorandum.** `planning["updated"]`
  is inside the `planning:apm` artifact projection, so writing the cycle moved
  the matrix's guarded parent and the matrix regenerated on every run that
  designed one. A cycle-only change now leaves `updated` alone. The fragility
  predates this — editing planning context has always restamped the APM — but
  the cycle is what made it bite.
- **`sources.imported` is an edge, and a partial one.** 5c omits it; the
  evaluation doc lists it. Both are right about something: the shape reads the
  imported tables to name a step's population, so the edge belongs — but a step
  *may* have no population, so an engagement with nothing imported must still
  get a matrix. It is in `_PARTIAL_DEPENDENCIES`, beside `planning.context_ready`
  and for the reason stated there.
- **Readiness assesses currency, which no other planning capability does.** The
  shape is a reading *of* the memorandum, so a rewritten memorandum leaves it
  describing a process the engagement no longer claims to audit — and the matrix
  takes its `process` vocabulary from it. `workspaces.planning_apm_sha1` is the
  memorandum's text and nothing else; `workflow_basis_sha1` is deliberately out,
  because a new voucher does not stop a process description being true.
- **`other` had to be filtered out of the offered types.** `evidence_type_counts`
  includes it, and `validate_cycle_shape` refuses a role that names it, so the
  turn was being handed a value it would then be refused for. `_bind_cycle`
  drops it: a document nothing could type records no identifiable position.
- **A satisfied matrix is not reused when the cycle materializes.** That is
  `workflow.materialize`'s general rule (`dependency_will_materialize`) and it is
  right — a matrix drafted before any cycle existed has a `process` vocabulary
  nothing checked. It does mean the first run that designs a cycle on an existing
  engagement also regenerates the matrix, once. Test fixtures that build a matrix
  by hand now stamp a shape (`conftest.stamp_planning_cycle`) rather than
  re-deriving one.
- **The page's colours had to be the app's.** The strip was written against
  PrimeVue's `--p-surface-*` tokens, which do not flip: `--p-surface-0` is
  `#ffffff` in both themes, so every node drew white with light text on it.
  Every colour is an `--aw-*` token now.
- **A note above a field row moves the arrow that enters it.** Endpoints are
  computed from a row's index, so the unbound note, the "no schema" line and the
  hidden-field count are all rendered *after* the field list, and counted into
  the node's height so the population below does not overlap it.
- The anchor's `table` is filled from the shape inside the guarded commit
  rather than from the unit input: `ExecutorRequest` carries no `unit_input`,
  and reading `fresh.planning["cycle"]` under the `planning:cycle` parent guard
  is the stronger version of what 5e intends.
- **The anchor's `column` is not the shape's to give, and deriving it wrote a
  column that did not exist.** 5e says the executor fills both `table` and
  `column`; the shape names a *table*, and only names columns in the borrowed
  case, so the first implementation fell back to the role's document field. On
  `treasuryfull` that stored `deal_reference` as a column of `04_deals`, which
  carries `DEAL_ID` — and nothing refused it, because `_validate_anchor` checked
  only that the column was a non-empty string. A cycle anchored on a column that
  does not exist matches no population row, which reads downstream as an
  engagement whose records simply do not link. Now: the shape owns the table,
  the response owns the column (the turn is shown every column of every table),
  the borrowed case still takes the shape's named column, and
  `cycle_rulesets.validate` refuses a column the table does not carry — checked
  as a pair, so a ruleset naming a table this engagement never imported is still
  readable rather than becoming a workspace nobody can open.
- **Reachability had to move into the repair loop.** A role the response
  declares that no join key reaches fails `cycle_rulesets.validate` at commit —
  after the turn has succeeded and outside any repair — so it costs the whole
  run. It cost one: a `treasuryfull` proposal declared `broker_ack`, wrote no
  join key reaching it, and the stage failed with nothing to show for two model
  calls. `validate_linkage_proposal` now runs the same walk, and the repair has
  two honest ways out: add the join key, or declare the role `unreachable`. The
  re-run took the second, which is what 5e's mechanism is for.


### Measured on `procurement` and `treasuryfull`, 5 September 2026

`rcm_only` on each, then `tests.cycle_ruleset_approved` on `treasuryfull`. Same
model and settings as every earlier measurement in this document
(`deepseek/deepseek-v4-flash-0731`, `LLM_REASONING=medium`), auto mode, over the
matrix each engagement already had.

| | procurement | treasuryfull |
|---|---|---|
| cycle steps + cross-cutting | 4 + 1 | 3 + 1 |
| distinct `process` values written | **5** | **4** |
| rows the turn wrote outside the shape | **0** | **0** |
| rows committed | 32 (4 created, 28 updated) | 30 (1 created, 25 updated) |
| calls | 4 | 4 |
| prompt / output tokens | 34,977 / 52,593 | 55,909 / 38,244 |
| cycle call output tokens | **9,040 — smallest** | **5,619 — smallest** |
| cycle call latency | 48.7 s — smallest | 48.0 s |
| largest call | rcm_risks, 82.0 s | rcm_controls, 72.6 s |
| repairs | 0 | 0 |

**The closed vocabulary holds.** Every row either turn wrote named a step the
shape declares. On both engagements the shape reused the matrix's existing
process names verbatim, which is what `EXISTING PROCESS NAMES` is for — and on
`treasuryfull` it also *reduced* four to three, folding "Treasury operations"
into the cross-cutting bucket, which is the arrangement 4a's own example
predicted for this cycle.

**The cycle call is the smallest in the run on both**, as the step expected.

**`business_cycle` is now uniform** on `procurement` (one value, the cycle's
name) and has two values on `treasuryfull` — for a reason worth stating.

#### What a warm regeneration cannot show

`treasuryfull` finished with one row outside the shape and two `business_cycle`
values, and neither is the turn's doing: four rows from a 2 September run were
not re-proposed, and an omitted row keeps what it had (*"Omission never deletes
an existing row"*). One of the four carries `process: "Treasury operations"`,
which the new shape does not name.

So the measure as written — *distinct `process` values per regeneration against
the shape's step count (expect equal)* — cannot be read off a warm engagement at
all. It held on `procurement` by luck, because the shape happened to reuse all
four old names. What is actually enforced, and what both runs show, is the
narrower claim: **no row the turn writes names a process outside the shape.**

The stale rows are also outside `process_outside_cycle`'s reach, and promoting
that flag to a row error (5d) would not touch them: the flag is computed on
*proposed* rows. Reconciling a row whose process the cycle no longer names is a
`match_rcm_revision` question and belongs with step 4, not with the gate.

#### The ruleset stage, on `treasuryfull`

Two model calls: the first refused by the gate, the second accepted — *0 after
one repair*, as the step predicted, though it took two attempts to get there
and both of the defects it caught were mine (see the *Landed as* notes above).

- **Roles are the shape's.** Five of the six the cycle declares came back with
  the cycle's own names and types. The ruleset this replaced — proposed under
  the old contract, on the same engagement — had invented five different names
  (`internal_deal_ticket`, `fx_counterparty_confirmation`, …). The shape and the
  rules now agree by construction rather than by luck, which is the whole point
  of 5e.
- **`unreachable` fired for real, and reported.** `broker_ack` was dropped from
  the rules with its reason — *"broker_confirmation fields only an identifier
  (note_number) that is not shared with any reachable role"* — and the run
  warned about it. The shape keeps the step. This is the
  `purchase_requisition` case the evaluation doc predicted, arriving on a
  different role in a different engagement.
- **The anchor bound correctly**: `04_deals` from the shape, `DEAL_ID` from the
  turn.
- `cycle_sha1` recorded on the ruleset and outside `ruleset_hash`.
- Write-back: 3 attributes contracted, 3 downgraded with reasons.

#### Not measured

Nothing here is a claim about quality. Every number above is one run, and this
document's own *One run per variant cannot measure a prompt change* applies: only
the gate-enforced properties — rows outside the shape, roles outside the cycle,
the anchor's column against its table — are safe to read from a single
regeneration. Token and latency figures are recorded so a later run has
something to sit beside, not because a 4-call run is now known to cost more than
a 3-call one.

---

## What each step buys

| After step | Calls per cold run | Largest output | What a rejection costs | Schema dependency |
|---|---|---|---|---|
| today | 2 to 4 | 66k tokens | the matrix and a re-run evidence call | RCM invalidated by re-derived schemas |
| 1 | 1 | unchanged | the matrix | none on the RCM |
| 2 | 2 | roughly two thirds | rows or attributes, not both | none |
| 3 | 3 | roughly half | one job's rows | none |
| 5 ✅ | 4 (one small shape call) | unchanged | the shape alone, or one job's rows | none; the cycle's field half stays after the schemas |
| 4 | 1 + 3 per step + 3 per cycle, parallel | a step's rows | one job of one step | none |

## What is deliberately not changed

- The privacy boundary. No step requests table rows; bucket context is a
  narrower selection of what the preset already admits, resolved by the same
  resolver under the same `ContextPrivacy`.
- `execute_rcm`, `reconcile_rcm`, per-row approval in permission mode, and the
  receipt shape. Step 4 widens the parent set and adds a near-duplicate branch
  to `match_rcm_revision`; nothing else.
- The row schema. `required_comparisons` remains the field downstream reads;
  only its author moves.
- The ruleset approval. Proposing is still not approving; the auto-mode
  delegation in `cycle_rulesets.approve` is orthogonal.
- Join-key fan-out measurement, which `docs/agentic-vouching-plan.md` names as
  the property that must not be lost; the ruleset path is untouched.

## How to evaluate each step

Regenerate `treasuryfull` and `procurement` with `rcm_only` after each step and
record, from `AgentRuns/<run>/` and `telemetry.db`: calls, prompt and output
tokens per call, largest call latency, repair count by stage, rows committed,
rows quarantined, flags and warnings. Then run the row-level checks in the
*Verification commands* section of `docs/rcm-generation-quality.md` and fill
the next column of its table. A step that regresses any of the quality doc's
nine checks does not proceed to the next.
