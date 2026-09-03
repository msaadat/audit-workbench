# RCM generation redesign: a staged pipeline, delivered in small steps

**Status:** design, not yet implemented. This is the handoff for splitting the
single RCM judgment turn into a per-process, per-job pipeline, and for moving
the cycle-vouching evidence contract out of planning. Each step lands on its
own, leaves `rcm_only` regenerating a matrix end to end, and is measured
against the previous step before the next one starts.

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
APM ──► stage 0: scope map ──► one unit per bucket, in parallel ──► executor
          (buckets, themes)       1  risks     (no engagement material)
                                  2  controls  (bucket-scoped basis)
                                  3  attributes (closed vocabularies)
                                            │
                                            └─► cross-cutting unit per cycle, last

rows with evidence_kind = transaction_cycle ──► cycle design (after schemas_stamped)
                                                  roles, join keys, assertions, and the
                                                  comparison that answers each attribute,
                                                  written back onto the row
```

- **Stage 0, the scope map.** One small structured call over the APM alone.
  Returns cycles, process buckets with a one-line description, one
  cross-cutting bucket per cycle, and each planned theme assigned to a bucket.
  Stored in planning; auditor-editable; cached against the APM hash.
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

## The steps

Each step has an *invariant* (what must still work when it lands), a *measure*
(what to compare against the previous step on a treasuryfull and a procurement
regeneration), and a *rollback*. Steps 0 and 1 are independent of each other
and of the rest. Steps 2, 3 and 4 are sequential.

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

---

### Step 4. Stage 0 and parallelism

The structural change. Everything before it has already shrunk each call; this
makes the calls per-process and concurrent, and closes the `process`
vocabulary.

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
