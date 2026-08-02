# Test generation quality: what was wrong, what was fixed, what is left

**Status:** one round of changes has landed and has been evaluated against a
live regeneration. The round removed the defect class that mattered most —
predicates written against guessed column values — and introduced a new one in
its place. Recommendations 1 and 2 are the highest-value open items; the vouch
findings in *Cycle resolution* are new and belong with
`docs/cycle-vouching-shape-selection.md`.

This document records an auditor's review of generated tests, the root-cause
analysis of the generation turn that produced them, the criteria a good test has
to meet, and the measured result of the round. It is the handoff for the
remaining work. It is the test-generation counterpart of
`docs/rcm-generation-quality.md`, and it repeats that document's two transferable
lessons almost exactly — see *Lessons, again*.

## Context

`Workspaces/procurement` — Global Bank procurement audit. 70 RCM rows in
`Planning/RCM/` from agent run `20260802-111119-759a1f`, 26 of them honestly
marked `No control identified`. Four imported tables (requisitions 112, po_data
93, invoice_data 118, vendor_master_file 39) and eight documents.

Note for anyone re-deriving this: **`financial_approval_matrix` is a document,
not a table.** It does not appear in `workspace.table_names()`. Any test that
appears to reason about it is reasoning from document text.

### Round 0: the turn as it was

| Slot | Source | Size |
|---|---|---|
| System prompt | `GENERATE_SYSTEM` in `app/agent/workers/tests.py` | 5,724 chars |
| `documents` | 6 docs, `summary` representation | 26,050 chars (53.4%) |
| `other_rcm_rows` | 51 of 69 rows, truncated at budget | 16,351 chars (33.5%) |
| `table_schemas` | 4 tables, name + dtype only | 4,136 chars (8.5%) |
| `planning_context` | planning artifact | 2,143 chars (4.4%) |
| **`target_rcm_row`** | the only per-unit content | **828 chars (1.7%)** |
| `methodology` | `test_draft_methodology_candidates` | **0 items** |

13,779 prompt tokens, `nvidia/nemotron-3-ultra-550b-a55b:free`, temperature 0.
**98.3% of every prompt was invariant across the 70 units and re-sent each
time**, with `cached_tokens: 0` — 1,488,179 prompt tokens across 108 calls whose
sizes spanned only 371 tokens.

The analysis below is taken from the captured call payloads in
`Debug/LLMCalls/`, not inferred from the code. The captured calls showed two
things a code reading missed: the methodology slot resolving to zero items, and
the `AUDIT NOTES` block still arriving inside every document summary.

## Findings from the audit review

Verified by re-performing every test against the source tables with
`data_tests.compute`, which is non-mutating.

**Credit first, because it narrows the problem.** All 128 Polars steps executed
cleanly — no syntax errors, no unknown columns, no bad joins. The schema-only
validation in `_validate_generate_data_step` does its job. Coverage was near
total: 69 of 70 rows carried a test. Every defect below is methodological, not
mechanical.

### A. Predicates written against values the turn could not see

The single root cause of the worst defects. The turn received
`{"name": "VENDOR_STATUS", "dtype": "String", "type": "text"}` and nothing more;
`top_values` is stripped by `_LITERAL_PROFILE_FIELDS` as policy. So the model
guessed the vocabulary — and `VENDOR_STATUS` never takes the value `'Approved'`.
It is `Active` / `Under Review` / `Inactive`.

A wrong guess does not fail loudly. It fails silently in one of two directions,
and **both are reported to the auditor as a control conclusion**:

- **Saturated.** `VENDOR_STATUS != 'Approved'` matches every row. `DAT-866B843DA4`
  reported **93 of 93 POs** as exceptions and `DAT-F2D4151BA5` **112 of 112
  requisitions** — read plainly, "this control failed completely".
- **Vacuous.** `is_in(['approved'])` matches nothing, returns zero exceptions,
  and `compute()` concludes `control_conclusion: "effective"`. **The test
  certifies a control because its predicate can never fire.**

12 steps filtered on a literal absent from the column. The same control was
answered four times with contradictory results — 1, 1, 93, 93 — across four
rows in four different runs.

A third variant appeared only under re-performance: `DAT-73CAA7A0B8` and
`DAT-9DBDFB0520` compare `BUYER_ID` to `VERIFIED_BY_ID` / `SUPERVISOR_APPROVAL_ID`.
The code is correct Polars, but `BUYER_ID` is `B001–B006` and the approver ids
are `1003–1024`. Two segregation-of-duties tests across incompatible identifier
namespaces, both returning zero, both reading "effective".

*(An earlier pass of this review reported these as a column name passed as a
string literal. That was an artifact of the reviewing regex, not a defect in the
generated code. The namespace mismatch is the real fault.)*

### B. The validator cannot see any of it

`_validate_generate_data_step` runs the proposed code against
`_empty_schema_frames` — zero-row frames. It proves the code parses and that
column names resolve. It cannot detect a predicate that matches everything, one
that matches nothing, or a literal that never occurs.

The preset states the rationale in a comment: *"Data Test code is validated
against schema-only empty frames, so profiles are not needed to generate a valid
executable procedure."* That is true and beside the point. **Valid code is not a
valid test.**

### C. A threshold read from a document, frozen into code

`DAT-00C37422FA` filtered `> 10000000` as "the highest defined approval limit
(PKR 10,000,000 for CFO)" and flagged **73 of 112** requisitions. This is the
`RCM-8263DB` error from round 0 of the RCM work — the CEO row carries a null
`MAX_APPROVAL_AMOUNT` with `LIMIT_NOTES = "Above PKR 10,000,000"`, meaning
unlimited — now inherited into executable code where it produces a number.

### D. Document tests interrogated one document 73 times

73 of 82 doc tests were `qa`, 9 `vouch`. **All 82 read the same SOP extract.**
The three transaction vouchers appeared in one question step between them. The
shape-selection problem recorded in `docs/cycle-vouching-shape-selection.md`,
confirmed at scale, plus a second problem that document had not named: the
document-test population was a single policy document.

### E. Vouch tests with no coverage and no scope limitation

All 9 vouch tests resolved to **1 item**. The three-way-match tests carried
`missing_roles: ["purchase_order", "goods_receipt"]` — two of three required
roles unfilled. No test recorded a sample size, a population, or a scope
limitation anywhere.

### F. No method anywhere in the prompt

`GENERATE_SYSTEM` was 5,724 characters of contract: the JSON shape, the Polars
rules, the vouch plan schema, the field-path grammar. On *audit method* it said
essentially one thing — `objective: what the test establishes about the control`.
Nothing about population, sample size, what an exception is, when a test may
conclude on operating effectiveness, or what to do with a `No control
identified` row. The same imbalance the RCM had.

### G. Duplication the duplicate-avoidance channel did not prevent

25 of 70 rows carried a step duplicated on another row; 11 signature clusters
spanned two or more processes — while `other_rcm_rows` consumed a third of the
prompt to prevent exactly that.

### Against the answer key

| Exception | Round 0 |
|---|---|
| Self-verification `REQUESTER_ID = VERIFIED_BY_ID` (8) | found, n=9 (merged with the next) |
| `VERIFIED_BY_ID = APPROVED_BY_ID` (1) | found |
| PO-to-invoice variance (5) | found, exactly 5 |
| Invoice dated before GRN (5) | **missed entirely** |
| Duplicate vendor bank accounts (2 pairs) | found |
| PO to a non-`Active` vendor (1) | found — and also answered 93, twice |
| Paid with no GRN link (8) | found, triplicated |
| DoA breach candidate (1) | **mis-tested** — 73 false positives |

## Root causes in the generation turn

**RC1 — the methodology channel was wired but empty.** The `tests.generate`
preset declares a `methodology` source (5 items, 8,000 chars). It resolved to
zero items: `KnowledgePacks/` does not exist at either scope. **This is RC1 from
the RCM document, repeating verbatim in a second capability.**

**RC2 — column values are withheld from the turn by design, and nothing told the
model so.** The privacy layer withholds `top_values` deliberately. The prompt
never said the values were unavailable, so the model filled the gap by guessing
rather than by writing value-agnostic predicates. Finding A is entirely
downstream of this.

**RC3 — the quality gate proves syntax, and the conclusion machinery trusts it.**
Schema-only validation cannot see saturation or vacuity, and `compute()` maps
zero exceptions to `effective` and any exceptions to `ineffective` without
asking whether the predicate could have fired.

**RC4 — documents arrived with the `AUDIT NOTES` block.** Only `rcm_scope`
passed `include_audit_notes=False`. The notes are a numbered deficiency list
whose entries each end in **"Follow-up: Obtain the master SOP document…"** — the
most test-shaped content in the turn. It came back as objectives that re-confirm
a known deficiency: `DT-B632E392`, *"Confirm the Procurement SOP does not define
monetary thresholds…"*. That is not evidence about a control.

**RC5 — the duplicate-avoidance channel could not do its job.**
`_TEST_DRAFT_OTHER_ROW_FIELDS` is `("id", "semantic_id", "risk")` — other rows'
*risks*, not their *tests*. A unit cannot see what its siblings produce. The
channel cost 33.5% of the prompt for a task that is structurally impossible
within one unit.

**RC6 — the capability ran serially against a fixed deadline.**
`DEFAULT_MAX_RUNTIME_SECONDS = 1800`; 70 units at ~80s each is ~93 minutes.
`max_llm_concurrency` was 4 but `max_concurrent_model_calls` was 1, and the sum
of call latencies equalled the wall clock. `tests.specified` did not declare
`barrier="all_settled_parallel"` while `documents.analysis_chunks_ready` did.
**Four of six runs died with "run time limit reached" after ~20 rows each.** The
60 data tests and 82 doc tests under review were the union of five partial runs.

## Round 1: what changed

1. **An execution-time reality gate** (`app/data_tests.py`). The generating
   worker has no workspace by design, so the check lives where the data already
   is. `_step_reality_issues` flags three things and routes them to
   `review_required` / `no_conclusion` instead of an effectiveness verdict:
   a step excepting ≥90% of its population (floor of 20 rows, so saturation stays
   a population-level signal); a step filtering on a literal absent from the
   category column it reads; and a column-to-column comparison whose two sides
   share no value. The empty string is excluded — finding no blanks is a blank
   check succeeding.

2. **Category vocabularies in the table metadata**
   (`test_generate_table_metadata_candidates`). Each category column now carries
   its complete value set, so the model does not have to guess. Two bounds keep
   this category metadata rather than row data, and both were earned:

   - **A value is a category only when many rows share it** — `MIN_CATEGORY_ROWS`
     20, `MIN_CATEGORY_REPETITION` 4. The existing guard test
     `test_test_generate_definition_context_has_schema_metadata_but_no_table_rows`
     caught the first version: on a 1-row table the "complete vocabulary" is
     literally the row. That guard is correct and still passes.
   - **Only a provably complete list is sent.** `profiler.TOP_VALUES` is 8 while
     `project_column_profile`'s `category_limit` is 30, so the pre-existing
     assistant path can hand a model a *truncated* vocabulary that looks
     complete — worse than none, because it licenses excluding a real value.
     Only a list holding one entry per distinct value is passed on. `BANK_NAME`
     (20 distinct, 8 retained) is correctly withheld.

   Routed through `assistant.table_metadata` rather than the profiler, because
   `test_planning_apm_preset_declares_all_current_adapter_sources` asserts that
   adapters never reach into `profiler` or `get_frame` directly.

3. **`tests.specified` declares `barrier="all_settled_parallel"`.** Verified safe
   before claiming it: one unit is one RCM row, the turn reads no sibling's
   output, and `commit_test_generation` guards exactly its own row's parent hash
   against a freshly read workspace, with binding and folding on the main thread.
   `Workspace.get_profile` was switched to `write_json_atomic` at the same time —
   its cache write was non-atomic, and making context resolution concurrent is
   what would have made that reachable.

4. **`other_rcm_rows` removed** end to end — preset source, scope wiring,
   adapter, projection constant, exports, payload key, and the "do not duplicate"
   clause.

5. **The `AUDIT NOTES` block no longer reaches the tests turn.**
   `document_test_document_candidates` takes `include_audit_notes` and
   `test_generate_scope` passes `False`, mirroring `rcm_scope`. The deficiency
   still reaches the turn through the RCM row driving the unit; what is gone is
   the pre-drawn conclusion and its follow-up.

6. **`GENERATE_SYSTEM` gained a methodology section** (5,724 → 8,916 chars),
   written as prose rather than an enumerated taxonomy, because the RCM round-1
   lesson is that a list added for thinking gets copied into fields. Four blocks:
   what a test is for (evidence that the control operated; not a restatement of
   the risk, not re-confirmation of a supplied conclusion; and for a
   `No control identified` row, test whether the exposure is present in the
   records); population; what an exception is, including that in-progress,
   cancelled and rejected records are missing downstream fields by design; and
   scope limitations, including that a threshold read from a document is only as
   complete as that document. Plus a paragraph on value-blindness, and one on
   vouch coverage.

Tests added: five in `tests/test_data_tests.py` (three defect classes plus two
asserting the gate does *not* fire on sound tests), three in
`tests/test_agent_context_adapters.py` (category values, truncated-vocabulary
withholding, audit-notes exclusion), and one in
`tests/test_agent_capability_composition.py` pinning the *set* of parallel
capabilities rather than just the new member. Suite: **1164 passed, 2 failed** —
the same two failures that pre-exist on `main`
(`test_command_agent.py::test_full_audit_command_uses_documents_and_planning_templates`
and `test_rcm_execution.py::test_completion_uses_execution_and_outcome_gates`).

## Round 1: measured result

Regenerated across runs `20260802-145254-64442e`, `152802-c475e2`, and
`162520-b1371b`. The entire test population is new: 75 data tests, 73 doc tests,
69 of 70 rows covered. **All 150 Polars steps execute cleanly.**

| Check | Round 0 | Round 1 |
|---|---|---|
| Steps filtering a value the column never holds | 10 | **0** |
| Column comparisons across incompatible id schemes | 2 | **0** |
| Contradictory answers to the same control | 1 / 1 / 93 / 93 | **0 / 0 / 1 / 1 / 1 / 1** |
| Vouch objectives stating limited reach | 0 of 9 | **13 of 13** |
| Distinct documents used in doc tests | 4 (SOP 82 of 82 tests) | **8, all 5 vouchers used** |
| Vouch share of document tests | 9 of 82 (11%) | 13 of 73 (18%) |
| Invoice-before-GRN exception (K4) | missed | **found, n=5** |
| Doc tests whose objective confirms an absence | 4 of 82 | 3 of 73 |
| Prompt tokens per call | 13,779 | **8,771 (−36%)** |
| `max_concurrent_model_calls` | 1 | **4** |
| Cross-row duplicated steps | 25 of 70 rows | **44 of 70 rows** |
| Steps returning ≥50% of their population | — | **21 of 150** |

**The value-blindness fix worked completely.** The guessed-literal class — the
most dangerous defect, because it silently produced both false failures and false
assurance — is gone. `VENDOR_STATUS`, `PAYMENT_STATUS`, `PO_STATUS`,
`GRN_STATUS`, `REQUISITION_STATUS`, `REQUESTER_DEPARTMENT` and `BUYER_ID` now
arrive with their complete vocabularies for +1.1k characters.

**Parallelism worked and was not sufficient.** Run `145254` executed 60 calls in
2,047s wall against 7,798s of summed latency — **3.8×**, essentially the whole
budget the limit allows. But per-call latency rose **80s → 130s**, and the run
still hit the deadline. Two plausible causes that this data cannot separate:
four concurrent requests against a free-tier endpoint queueing, and the longer
prompt inviting more reasoning. Effective throughput improved ~2.5× (34s/call
against 84s/call), leaving 70 rows at ~2,390s against an 1,800s budget.

## The defects round 1 introduced or left

**D1 — non-exception steps are counted as exceptions. The dominant defect of
this round.** 21 of 150 steps return ≥50% of their population, and the total
reported exception rows across all 75 tests is **1,751** against a real exception
count under 100. Three flavours, one cause:

- **Population steps.** `DAT-5E4F4AD80C` is a *good* segregation-of-duties test —
  its second step finds exactly 1 exception. Its first step, "select requisitions
  that have a financial approval recorded", returns 112, all counted. Reported
  total: 113.
- **Schema-absence steps.** `DAT-E05C535E39` selects all 39 vendors and labels
  each `"No AML/KYC field in schema"`. One design-gap observation reported as 39
  exceptions.
- **Scoping steps.** `invoices_over_10m` returns 73; the objective is "assess
  whether the matrix covers the observed transaction range", which is not an
  exception query at all.

All three trace to change 6. Asking for population statements and scope
limitations produced them — expressed as *steps*, and every step's row count is
an exception count. The gate caught 11 of these as saturated, so none reached an
auditor as a conclusion; the ones below 90% did not.

**D2 — segregation-of-duties coverage regressed out.** Nothing in round 1
compares `REQUESTER_ID` to `VERIFIED_BY_ID`, or `VERIFIED_BY_ID` to
`APPROVED_BY_ID`. **The 8 self-verification exceptions — the largest single
exception cluster in the data — are now untested.** Round 0 found them.
`REQUESTER_ID = FIN_APPROVED_BY_ID` (1) is covered.

**D3 — removing `other_rcm_rows` roughly doubled cross-row duplication**, 25 of
70 rows to 44 of 70, beyond what the increase in test count explains. The round-0
conclusion that the channel "was not preventing duplication" was wrong: it was
preventing some, expensively and partially. Deduplication now has no home at all.

**D4 — steps are still written as a pipeline.** Run `165332` died with
`NameError: name 'joined' is not defined` — step 2 referencing a variable bound
in step 1. Each step is a separate `sandbox.run` call with only the frames in
scope, and **the prompt never says so**. A unit failing validation three times
also takes the whole run with it.

**D5 — the DoA breach candidate is still not isolated.** Round 0 mis-tested it
(73 false positives). Round 1 is honest — `DAT-5BF4C0536D` states *"the data does
not map FIN_APPROVED_BY_ID to roles, so this test flags POs above the CFO limit
for further investigation"*, and the RCM row now records that the CEO tier has no
upper limit — but no test isolates approver `1002`'s single 99,348,150 approval
against their otherwise sub-10,000,000 pattern. Better to be honestly scoped than
confidently wrong; still not found.

## Cycle resolution: the vouch failures are not the model's

All 13 vouch tests resolve to 1 item and 22 of 49 roles are unfilled. Every
`document_types` value used is valid — there are no contract violations. Tracing
each unfilled role gives three causes, **none of them a generation defect**.
This extends `docs/cycle-vouching-shape-selection.md`: shape *selection*
improved this round; shape *resolution* is limited elsewhere.

1. **The linker cannot assemble a cycle.** Documents link by the *anchor row's*
   identifier only. `DT-9D2318DD` anchors on `invoice_data/INVOICE_ID` =
   `INV2024004`; only the invoice and the payment voucher carry that value. The
   purchase-order document carries `PO2024004 / REQ2024009 / GRN2024004`. So a
   three-way match anchored on an invoice **structurally cannot fill its PO and
   GRN roles** — even though the invoice document itself carries both
   identifiers, which a second hop would resolve. Six of the 13 fail this way.

2. **An OCR error silently breaks linking.** The GRN (`a453c5c52b`) and the
   payment voucher (`e648fb348f`) carry **`P02024004` with a digit zero**. The
   table and the PO document carry `PO2024004` with the letter O. They never
   match, so PO-anchored tests cannot reach the GRN either. Nothing surfaces
   this: the test simply reports missing evidence.

3. **A vocabulary gap.** `800a611b76` is the purchase *requisition* but
   extraction typed it `purchase_order`, and `VOUCHER_DOCUMENT_TYPES` has no
   requisition type. Tests wanting a requisition improvised `approval_record` or
   `other`; nothing is typed that way, so those roles never fill.

## Lessons, again

Both of `docs/rcm-generation-quality.md`'s transferable lessons reproduced here,
which is now evidence that they are properties of this pipeline rather than of
that artifact:

1. **An instruction lands in the field next door.** D1 is the population and
   scope-limitation instructions arriving as *steps* instead of as objective
   text. The RCM equivalent was percentages migrating from `risk` to `control`.
2. **Prompt-only fixes plateau, and a deterministic gate does not move.** The
   reality gate caught every round-0 defect it was built for *and* 11 instances
   of a defect class that did not exist when it was written. The prompt fixed one
   class and created another.

A third, specific to this capability:

3. **A generated artifact that is executed needs a check at execution, not only
   at generation.** The generating worker is workspace-free by design, so it can
   never see what its own output does to real data. Every high-severity finding
   in this document was found by running the tests, not by reading them.

## Criteria for a good test

**Structure**
- One test, one source, one question about one control.
- Every step is an exception query: `result` holds the rows that fail. A
  population, a scoping analysis, or a schema observation is not a step.
- Each step stands alone — it may not depend on a variable bound in another.

**Scope**
- The objective names the population: which records, in what state, over what
  period.
- A data test runs the whole population; narrowing is stated and justified.
- Where the data cannot answer the question, the objective says so instead of
  substituting a proxy.

**Predicates**
- Filter only on values known to occur in the column.
- Both sides of a comparison hold the same kind of identifier.
- An absent value is not an exception until workflow state has been excluded.
- A threshold read from a document is not encoded as the whole rule.

**Exceptions and conclusions**
- An exception contradicts the control having operated — not merely a large,
  unusual, or incomplete record.
- A test that could not have fired never concludes "effective".
- A test that excepts its whole population never concludes "ineffective" without
  review.

**Document and vouch tests**
- Mode chosen from what the risk is about, not from what is easiest to write.
- A vouch test states that its reach is limited to transactions holding
  evidence, and marks a role `required` only where the test is meaningless
  without it.
- A question step reads the document that carries the answer, not whichever
  document is largest.

**Coverage**
- Every known transaction-level risk class has a test that would find its
  exceptions: segregation of duties, threshold circumvention, matching, cut-off,
  master data, counterparty validity.
- No two rows carry the same test.

## Open recommendations

### 1. Separate population from exceptions in the step contract

**Highest-value open item, and it fixes D1, D4 and part of D5 at once.** The
prompt cannot fix this: it has already been told, and the instruction is what
produced the steps. Let a step declare what it is — scoping or exception-bearing
— and count only the latter toward `exception_count` and the control conclusion.
If steps could also chain, `joined = …` in step 1 followed by its use in step 2
becomes legal rather than a run-ending validation failure.

This is a change to `_run_polars_steps`, the step schema in
`_validate_generate_data_step`, and the prompt's description of `result` —
contained, and all three already exist.

### 2. Tell the model each step runs independently

One sentence, and it prevents a whole-run abort. Independent of 1, and worth
doing even if 1 lands, because a scoping step and a chained step are different
things.

### 3. Fix voucher identifier normalisation, and consider a second hop

The `O`/`0` confusion (Cycle resolution 2) silently defeats cycle linking and is
invisible downstream. Normalising identifiers before indexing is cheap. The
second hop — letting a linked document's own cross-references reach further
documents — is a design change, but without it a three-way match anchored on an
invoice cannot ever assemble, which is the shape auditors most want.

Also worth deciding whether `VOUCHER_DOCUMENT_TYPES` should carry a requisition
type, since extraction currently types a requisition as a `purchase_order`.

### 4. Restore segregation-of-duties coverage

D2 is the most serious audit consequence of this round: a real, eight-instance
exception cluster went from tested to untested. Worth understanding why before
adding an instruction — the round-0 test existed, so something in round 1's
changes displaced it.

### 5. Deduplicate after generation, not during it

D3 confirms the channel was doing partial work; removing it doubled duplication.
The replacement belongs where all generated tests are visible at once — compare
normalized step signatures across rows and flag near-identical pairs, the same
shape as the RCM document's recommendation 7. A per-unit prompt cannot do this.

### 6. Close the run-budget gap

Concurrency took 70 rows from ~5,600s to ~2,390s against an 1,800s deadline.
Options, roughly in order of preference: raise `DEFAULT_MAX_RUNTIME_SECONDS` for
row-per-unit capabilities; find out whether the 80s → 130s latency rise is
free-tier queueing (if so, a paid endpoint changes the arithmetic and nothing
else needs to); or accept a full generation as a resumable two-run job, which it
already effectively is.

### 7. Fill the methodology channel, or remove it

RC1 is unresolved and now known to affect two capabilities. The
`tests.generate` preset still declares a `methodology` source that resolves to
zero items on every engagement, because `KnowledgePacks/` does not exist. Either
author packs, or drop the declaration so the turn's inputs are what they appear
to be. The RCM work chose to elicit the model's own knowledge instead of
authoring packs; the same choice is available here.

### 8. A `test.md` template channel

Deferred, deliberately. The RCM fix worked largely by moving methodology into an
auditor-editable template, and test generation has no equivalent — all guidance
lives in a hardcoded system prompt. But recommendations 1 and 2 are *contract*
changes, and putting methodology in a template before the contract is right
would bake the wrong shape into an auditor-facing file. Do this after 1.

## How to evaluate a re-run

Regenerate for `Workspaces/procurement` and check against the criteria above.
Baselines: round 0 = the 60/82 tests from runs `112213`–`132847`, round 1 = the
75/73 tests from runs `145254`–`162520`.

| # | Check | Round 0 | Round 1 | Next |
|---|---|---|---|---|
| 1 | Steps filtering an absent literal | 10 | 0 | ? |
| 2 | Comparisons across id namespaces | 2 | 0 | ? |
| 3 | Steps returning ≥50% of their population | — | 21 of 150 | ? |
| 4 | Total reported exception rows | — | 1,751 | ? |
| 5 | Cross-row duplicated steps | 25 of 70 | 44 of 70 | ? |
| 6 | Vouch objectives stating limited reach | 0 of 9 | 13 of 13 | ? |
| 7 | Vouch roles filled | — | 27 of 49 | ? |
| 8 | Self-verification exceptions covered | yes | **no** | ? |
| 9 | Runs lost to the time limit | 4 of 6 | 2 of 3 | ? |
| 10 | Prompt tokens per call | 13,779 | 8,771 | ? |

The known exceptions in the data are the answer key. A generated test that
targets one should find it; one that cannot is mis-scoped or untestable as
written.

| Exception | Count | Round 0 | Round 1 |
|---|---|---|---|
| `REQUESTER_ID = VERIFIED_BY_ID` | 8 | found | **missed** |
| `VERIFIED_BY_ID = APPROVED_BY_ID` | 1 | found | **missed** |
| `REQUESTER_ID = FIN_APPROVED_BY_ID` | 1 | — | found |
| Invoice differs from linked PO total | 5 | found | found |
| Invoice dated before its GRN | 5 | **missed** | found |
| Vendor bank accounts shared by two vendors | 2 pairs | found | found |
| PO to a non-`Active` vendor | 1 | found + two 93s | found |
| Paid invoices with no `GRN_ID_LINK` | 8 | found | found |
| DoA breach candidate (approver `1002`) | 1 | mis-tested | not isolated |

Two traps that have caught every round so far: the CEO row's null
`MAX_APPROVAL_AMOUNT` means *unlimited*, not missing; and the 19 requisitions
with no `PO_NUMBER` are `Rejected`, `Pending PO`, or `Approved`-not-yet-converted
— workflow state, not missing data. The same holds for the 4 null
`FIN_APPROVAL_DATE` values, all `Rejected`.

## Verification commands

Re-performance is the only reliable review. `data_tests.compute` does not mutate
the workspace, so the whole population can be re-run safely:

```python
import glob, json, sys
sys.path.insert(0, "backend")
from app.workspaces import load_workspace
from app import data_tests

ws = load_workspace("procurement")
for path in sorted(glob.glob("Workspaces/procurement/DataTests/*.json")):
    item = json.load(open(path))
    result = data_tests.compute(ws, item["id"])
    flagged = [
        issue for issue in result["semantic_issues"]
        if any(key in issue for key in
               ("mis-specified predicate", "can never match",
                "cannot match the rows it describes", "share no value"))
    ]
    if flagged or result["verdict"] == "error":
        print(item["id"], item["rcm_id"], result["status"], flagged)
```

To recover the prompt a run actually sent, match on `correlation.run_id` in
`Workspaces/procurement/Debug/LLMCalls/`; `request.messages[1]` holds the user
payload and `messages[0]` the system prompt. **Do not infer the prompt from the
code** — the captured calls showed an empty methodology slot and a surviving
`AUDIT NOTES` block that a code reading missed, and one captured payload turned
out to be a repair attempt carrying its own error suffix.

To measure the resolved context without running a model:

```python
from app.agent.context.adapters import test_generate_scope
from app.agent.context.resolver import ContextResolver
from app.agent.capabilities.tests import capabilities

scope = test_generate_scope(ws, "RCM-635EFB",
                            document_ids=[d["id"] for d in ws.documents])
unit = {"id": "u1", "kind": "test_generation", "parent_refs": ["rcm:RCM-635EFB"]}
manifest, bundle = ContextResolver().resolve(ws, capabilities()[0], unit, scope)
```

Run outcomes and concurrency are in `AgentRuns/<run_id>/run.json` —
`usage.max_concurrent_model_calls`, `usage.model_usage_by_worker`, and `error`.
