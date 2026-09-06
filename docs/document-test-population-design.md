# Document Tests over a typed population

**Status:** delivered (steps 1–4, plus the sampling model from step 5). Written
6 September 2026 against commit `1f5fc27`, from the `expenses` engagement and
run `20260906-105059-2ff1e3`; built the same day. The three open questions in
section 6 were answered before implementation and the answers are recorded
there.

**Where to read what.** The mechanics are now reference material and live in
[audit-workflow-graph.md](audit-workflow-graph.md): the `tests.generate` and
`fieldwork.document_qa` preset budgets and sources, the document-step contract,
the record-grained unit expansion, and the value-domain bound. Trust that file
where the two disagree. This one keeps what a reference cannot carry — the
engagement that failed, the questions that were open and how they were
answered, and where the build departed from the plan.

The unit of assessment is the **record**, not the document — question 1 below.
Everything in sections 2–4 that reads "one row per document" is one row per
*record* as built: a voucher pack holding three line items is three
assessments, three grid rows, and three separate auditor calls.

Every file path below is under `backend/app/` unless it says otherwise.

- [1. The problem, in one engagement](#1-the-problem-in-one-engagement)
- [2. High-level design](#2-high-level-design)
- [3. Implementation](#3-implementation)
- [4. UI](#4-ui)
- [5. Delivery steps](#5-delivery-steps)
- [6. Open questions — answered](#6-open-questions--answered)
- [7. What was built, and where it differs](#7-what-was-built-and-where-it-differs)

---

## 1. The problem, in one engagement

The `expenses` workspace holds 14 documents: two policy documents and twelve
employee expense payment vouchers, all twelve typed `payment_voucher` with a
23-field induced schema (`expense_category`, `expense_description`,
`expense_merchant`, `amount_paid`, `approved_by`, …) and a structured reading
per voucher.

RCM row `RCM-FF30C9` asks, among other things, that *the expense category and
description match the nature of the underlying transaction*. Test generation
produced `DT-7608F21B`, a Q&A test with two steps, both with an empty
`document_ids` and a `missing_evidence` line saying no documents record
expense categories. The executor blocked the test and raised two evidence
requests whose `missing_document_types` is the placeholder
`supporting_evidence`. The auditor sees a blocked test in an engagement that
holds twelve of exactly the document it asks for.

Three things stack up to produce that, and only the first is fixed:

1. **The selector dropped every voucher.** The `documents` source of the
   `tests.generate` preset ran `documents.lexical`, a filtering selector, over
   candidates that carry title and type only (evidence prose is withheld in
   favour of the schema). Nothing matched; all twelve were omitted with
   *"did not match the candidate"*. Commit `1f5fc27` (17:41, after the 10:50
   run) replaced it with `documents.lexical_retained`, citing this engagement.
2. **The worker cuts to six.** `workers/tests.py:_relevant_documents` keeps the
   six strongest lexical fits before the prompt is built. On an engagement
   with 84 vouchers the turn will never see the population it is asked to name.
3. **The test model has no population.** A Q&A item holds `document_ids`;
   fieldwork expands one model turn per item and document
   (`capabilities/doc_tests.py:document_test_units`); the roll-up counts items.
   A population exists only for cycle vouching (anchored on a table row, linked
   to documents through readings) and for the vouching kind's evidence-aware
   sample, neither of which applies to an `inquiry` attribute like this one.

The same pattern blocked `DT-548E3661` and `DT-72EEAAD5` in the same run.

---

## 2. High-level design

A document step names a document **type** and a **field**, and the criteria it
is tested against. The workspace resolves the documents. Nothing in the test
enumerates ids.

```text
Generation   "For every payment_voucher, check expense_description and
             expense_category against the approved expense classification
             SOP [document f408fb18a7 §4]."

Runner       resolve every payment_voucher in the workspace (12 today, 84 on
             the treasury engagement)
             one model call per document:
               in:  that document's reading JSON + the SOP excerpt + the question
               out: pass | fail | needs_review, one-line reason, citation

Result       one table, one row per document
               PV-2025-001   pass
               PV-2025-002   fail   "Alcohol" is excluded under SOP §4.2
               PV-2025-003   pass
               …
             test status from the table; RCM row conclusion from the test
```

Three properties make this hold up at 84 documents rather than 12:

- **The population is re-resolved every run.** A voucher imported after the
  test was written joins it on the next run; a voucher re-read against a
  changed schema re-runs.
- **The model reads the reading, not the pages.** The evidence-read stage has
  already extracted every field with a citation. A per-document call carries
  a few hundred characters of JSON instead of a page, and the answer cites the
  field. Pages are fetched only when the reading lacks the field.
- **Coverage is stated.** The test records whether it ran over all documents
  or a sample, so a partial run is never reported as full coverage.

This is a middle tier between the two shapes that exist today: *ask one named
document a question* and *vouch a transaction cycle across roles*.

---

## 3. Implementation

### 3.1 Test model (`doc_tests.py`)

A Q&A item gains a `population` block and the test gains a coverage record.
Everything else on the item is unchanged, so the existing per-document
answer store (`qa_answers[document_id]`) and item states carry over.

```json
{
  "id": "ITEM-C0F719EC",
  "label": "Verify expense classification",
  "question": "Does expense_description, read with expense_category, describe an expense the SOP permits?",
  "population": {
    "document_type": "payment_voucher",
    "fields": ["expense_category", "expense_description"],
    "selection": {"mode": "all"},
    "criteria_refs": [{"document_id": "f408fb18a7", "section": "4"}],
    "resolved_document_ids": ["40dbbb06cd", "5c7badb914", "..."],
    "resolved_at": "2026-09-06T…",
    "inputs_sha1": "…"
  },
  "document_ids": ["40dbbb06cd", "5c7badb914", "..."],
  "qa_answers": {"40dbbb06cd": {"outcome": "accepted", "answer": "…", "citations": []}}
}
```

- `document_ids` stays the executable list so every reader that iterates it
  keeps working; the population is what *produces* it. An item with a
  population and hand-attached ids is refused: one source per item.
- `selection` is `{"mode": "all"}` or
  `{"mode": "sample", "method": "random|interval|stratified", "size": n, "seed": s}`,
  reusing `cycle_vouching.SAMPLING_METHODS` and `sample_row_indices` over the
  resolved list.
- `inputs_sha1` fingerprints the resolved ids and their extraction hashes,
  following `cycle_vouching.ITEMS_INPUTS_KEY`. It is what tells a current
  population from one that has to be drawn again.
- `criteria_refs` name the policy document and section the question is judged
  against. They resolve to an excerpt at run time (see 3.4).
- `assurance_scope` on the test becomes `full_population` when every item's
  selection is `all`, else `sampled_population`; `doc_tests.assurance_scope`
  already derives this for cycle tests and extends here.

### 3.2 Worker contract (`workers/tests.py`)

A document step may carry `population` instead of `document_ids`:

```
_GENERATE_DOCUMENT_STEP_FIELDS += ("population",)
population = {document_type, fields[], criteria_refs[], selection?}
```

Validation, in `_validate_document_step`:

- exactly one of `document_ids` or `population`;
- `population.document_type` must be one of the types in the supplied
  `evidence_schemas`; anything else is
  *"names a type this engagement holds no schema for"*;
- each `population.fields` entry must be a field of that schema;
- `criteria_refs` must name a supplied planning document;
- `missing_evidence` is only accepted on a step with neither, and only when
  no supplied schema carries a field answering the requirement. Today the
  model reaches for `missing_evidence` whenever it cannot see documents; under
  this contract the schemas are always visible for the row's types, so the
  honest gap is *"no type carries this field"*, not *"no documents"*.

Prompt (`GENERATE_SYSTEM`, rule 2) changes from *name the documents of that
type in document_ids* to:

> `evidence_schemas` says what a document of each type states. Write the
> question against the schema's field names and name the **type** in
> `population.document_type`; the workspace supplies every document of that
> type at run time. Name in `criteria_refs` the policy or SOP document the
> answer is judged against. Do not list document ids.

`_relevant_documents` no longer applies to evidence documents (3.3).

### 3.3 Context (`context/adapters.py`, `context/presets.py`)

The `documents` source of `tests.generate` splits into two sources:

| Source | Content | Budget |
| --- | --- | --- |
| `planning_documents` | planning-category documents with summary, as today (`documents.lexical_retained`) | 8 / 20k |
| `evidence_types` | **one item per document type**: `{document_type, count, sample_ids[3], schema_ref}` | 1 / 4k |

The per-type item replaces twelve, or eighty-four, per-document identity
items with one line each, and pairs naturally with `evidence_schemas`, which is
already one item per type. `document_classification.evidence_type_counts`
already computes the counts. The six-document cut in the worker is deleted for
evidence and kept for planning material.

### 3.4 Executor (`executors/tests.py`)

`_document_items_from_steps` copies `population` onto the item.
`_commit_document_test` then resolves it inside the same parent-hash
transaction:

```python
documents = document_classification.documents_of_type(fresh, population["document_type"])
selected  = select(documents, population["selection"])          # all, or a seeded sample
item["document_ids"] = [d["id"] for d in selected]
item["population"] |= {"resolved_document_ids": ..., "inputs_sha1": population_inputs_sha1(fresh, selected)}
```

An evidence request is raised only when the type resolves to **zero**
documents, and it names the type:
`missing_document_types: ["payment_voucher"]`. `_generate_missing_evidence`
keeps its behaviour for steps that genuinely name absent evidence.

### 3.5 Execution (`capabilities/doc_tests.py`, `doc_tests_execution.py`, `executors/fieldwork.py`)

**Expansion.** `document_test_units` re-materializes each population item
before fanning out, the way `list_tests` rebuilds a cycle test's items through
`materialize_cycle_population`: if `inputs_sha1` differs from the fingerprint
computed now, the resolved list is redrawn and answers whose document left
the population are dropped. One `document_qa_execution` unit per
(item, document) as today; unit ids are already semantic on
`(test, item, document)`, so a re-run answers only what is unanswered.

**Per-document call.** The `fieldwork.document_qa` preset gains a third source,
and the adapter chooses representation by what the reading holds:

| Source | Candidate | Representation |
| --- | --- | --- |
| `qa_item` | the item (label, question, fields) | `current_artifact` |
| `document_reading` | this document's structured record from `cycle_measurement.structured_records`, projected to `population.fields` plus identifying fields (`voucher_id`, `claim_id`, `employee_name`) | `current_artifact` |
| `criteria_excerpt` | the section of the policy document named by `criteria_refs`, through `document_context` | `excerpt` |
| `document_pages` | **only** when a named field is absent from the reading | `excerpt` |

The worker prompt (`DOCUMENT_QA_SYSTEM`) is unchanged in shape: it returns
`{answer, outcome, control_conclusion, citations}` with `outcome` in
`accepted | exception | needs_manual_check`. A citation may now be a reading
field (`{"field": "expense_description", "citation": "c14"}`), which the
executor resolves to the page citation the reading already carries.

**Deterministic short-cut (optional, later).** A step whose `fields` and
question reduce to a comparison against a fixed set or another field runs
over the readings with no model call. Out of scope for the first delivery.

### 3.6 Roll-up and results (`doc_tests.py:result_rollup`, `rcm_execution`)

For a population item the roll-up reports, per item:

```
population: 84   assessed: 84   accepted: 79   exception: 4   needs_review: 1
selection: all   assurance_scope: full_population   inputs current: yes
```

The item's own state follows the existing rule: `agent_checked` when every
resolved document has a current answer, `exception` if any answer is an
exception the auditor has not overridden. The existing warning at
`results.rolled_up` about populations no test speaks about is extended to
document types: a type with a schema and no test over it is named.

### 3.7 Budgets

`qa_pairs` in `_refresh_dynamic_limits` already counts item × document pairs
and is recomputed before every stage, so an 84-document population buys its
turns without a formula change. Per-call size drops: a reading projection is a
few hundred characters where a page excerpt was 26k. The `max_units_per_stage`
cap (250) is reachable with three population items on a large engagement;
raise it for `fieldwork.executed` or expand one unit per item that fans out
internally under `stable_all_settled`. The latter keeps the stage count
honest and is preferred.

### 3.8 API

- `GET /doc-tests/{id}/grid` already exists for cycle tests
  (`routes/doc_test_routes.py:236`, `MAX_GRID_PAGE_SIZE = 200`). Extend it to
  population items: rows are documents, columns are the item's fields plus
  verdict, reason, citation; paginated, filterable by outcome.
- `POST /doc-tests/build/qa` accepts `population` as an alternative to
  `document_ids` so an auditor can author one by hand.
- `POST /doc-tests/{id}/items/{item}/resolve` redraws the population on demand
  (the same code path expansion uses).

---

## 4. UI

Today `DocTestsTab.vue` lists items one row each and `DocTestItemDetail.vue`
renders `qa_answers` as a stacked list of answer cards with a document chip
per attached id. Neither survives 84 documents: the item list stays one row,
which is right, but the detail becomes 84 stacked cards.

The pattern to reuse is the cycle vouch grid (`doc-tests/CycleVouchGrid.vue`):
a paged table with a fixed-width status column, selection driving a detail
pane, and disposition actions on the row.

### 4.1 Item list entry

One row per item, as now, with a population summary in place of the document
chips:

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ DT-7608F21B  Expense accuracy and classification verification    ● exception │
│   Q&A · RCM-FF30C9 · population: payment_voucher (84) · full population      │
│                                                                              │
│   ▸ Verify expense classification                    79 ✓   4 ✗   1 ?   84/84│
│   ▸ Verify amount against receipt                    84 ✓   0 ✗   0 ?   84/84│
└──────────────────────────────────────────────────────────────────────────────┘
```

The three counts are accepted, exception, needs review; the fraction is
assessed over resolved. A sampled item shows `sample 25 of 84` where a full
one shows `84/84`, and the badge reads *sampled* rather than *full population*.

### 4.2 Population grid (item detail)

Selecting an item opens the grid instead of the answer-card list:

```text
Verify expense classification                          population: payment_voucher
Does expense_description, read with expense_category, describe an expense the
SOP permits?                          criteria: Expense Policy §4 (f408fb18a7)

filter: [all ▾] [exception] [needs review] [accepted]      search ▢         ⟳ resolve

┌────┬────────────┬────────────┬──────────────┬─────────────────────────────┬──────────┬─────────────────────────────────────┬────────┐
│    │ voucher_id │ claim_id   │ employee     │ expense_description         │ category │ verdict · reason                    │ cite   │
├────┼────────────┼────────────┼──────────────┼─────────────────────────────┼──────────┼─────────────────────────────────────┼────────┤
│ ✗  │ PV-2025-002│ EXP-2025-002│ Bilal Ahmed │ Client dinner – wine        │ Meals    │ exception · alcohol excluded §4.2   │ p1 c14 │
│ ✓  │ PV-2025-001│ EXP-2025-001│ Ayesha Khan │ Client meeting travel       │ Transport│ accepted                            │ p1 c11 │
│ ?  │ PV-2025-017│ EXP-2025-017│ S. Malik    │ Misc.                       │ Other    │ needs review · description too vague│ p1 c12 │
│ ✓  │ PV-2025-003│ …          │             │                             │          │                                     │        │
│ …  │            │            │              │                             │          │                                     │        │
├────┴────────────┴────────────┴──────────────┴─────────────────────────────┴──────────┴─────────────────────────────────────┴────────┤
│ 84 documents · 79 accepted · 4 exceptions · 1 needs review           rows 1–50 of 84   ‹ 1 2 ›   [Confirm all accepted]           │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- Columns are the item's `fields` plus two identity fields chosen from the
  schema (`voucher_id`, `claim_id`) and the employee; the auditor can add any
  schema field from a column picker.
- Default sort puts exceptions first, then needs review, then accepted, so the
  84 rows read as *the 5 that matter, then the rest*.
- Clicking a row opens the existing per-document answer card in a side pane:
  the model's answer, the cited reading fields, and a *Open document at page*
  link. The card is what `DocTestItemDetail.vue` renders today, reused as a
  pane rather than a list.
- Row-level disposition (confirm / mark exception / needs review) works as on
  the cycle grid; *Confirm all accepted* settles the majority in one action
  and leaves the flagged rows for the auditor.
- A document that left the population since the last run shows greyed with
  *no longer payment_voucher*; one added since shows *not yet assessed* until
  the next run.

### 4.3 Coverage and staleness

A strip above the grid states what the numbers rest on:

```text
Full population · 84 of 84 payment_voucher assessed · readings current
Run 20260906-1430 · 84 model calls · 3 exceptions dispositioned, 1 open
```

When `inputs_sha1` no longer matches it reads *population changed: 2 new
documents, 1 re-read — run to update*, with the run button beside it.

### 4.4 Evidence request

When a type resolves to zero documents the test is blocked as now, but the
request card names the type and offers the two real remedies:

```text
⚠ payment_voucher: no documents of this type in the engagement
   Import vouchers, or change the population type ▾   [Import…] [Edit item]
```

### 4.5 Authoring

`DocTestDefinitionForm.vue`'s Q&A form gains a *Population* option beside the
document picker: a type select (from `document_types`), a multi-select of that
type's schema fields, a criteria-document picker, and *all / sample n*. The
form previews the count the population resolves to before saving.

---

## 5. Delivery steps

Each step lands on its own and is measured on the `expenses` row above.

1. **Model and resolve.** `population` on the item, resolution in the
   executor, `inputs_sha1`, evidence request naming the type. Hand-authored
   through the build endpoint. No prompt change yet.
2. **Execution over readings.** `document_reading` and `criteria_excerpt`
   sources in the QA preset; expansion re-materializes; roll-up counts.
   Measured: 12 calls, each under 2k characters, the fail row reasons cite a
   field.
3. **Generation.** Worker contract and prompt; `evidence_types` source;
   delete the evidence six-cut. Measured: `RCM-FF30C9` regenerates with two
   population steps and no evidence request.
4. **Grid UI.** Grid endpoint for population items; `DocTestsTab` entry
   summary; grid with side-pane detail; coverage strip.
5. **Sampling and the deterministic short-cut.** Only if a real engagement
   needs them.

## 6. Open questions — answered

- **Multi-record documents.** *The record.* A voucher pack's three line items
  are three transactions, and assessing the pack as one unit lets two clean
  lines carry a third that is not. `structured_records` has always been
  record-grained and the cycle engine has always traversed it that way; this
  makes document tests agree with both. Consequences, all built:
  `population.resolved_records` is the resolved list, an answer is keyed
  `<document id>#<record index>` (a bare document id still means the whole
  document, so every stored `qa_answers` map keeps resolving), execution fans
  out one unit per record with `record:<document>:<index>` in its lineage, and
  the grid is one row per record.
- **Criteria without a document.** *Allowed.* `criteria_refs` accepts
  `{"document_id", "section"}` or `rcm:<id>#criteria`, normalized to a typed
  ref. The RCM row's own `criteria` text hashes into `inputs_sha1`, so editing
  the row retires the answers judged against it.
- **Which fields identify a row.** *As proposed.*
  `document_population.identifier_fields` takes the schema's `identifier`-role
  fields, and falls back to the first two `verbatim` fields where the schema
  marks none.

## 7. What was built, and where it differs

Everything in sections 3 and 4 landed, with `document_population.py` as a new
module holding the population model rather than a fourth section of
`doc_tests.py`. Four deliberate differences from the design above:

- **Auditor calls are per record.** Section 4.2 asked for row-level
  disposition; that needed a store, so an item carries
  `record_dispositions` and its own one `disposition` is *folded* from them —
  any exception wins, then any needs-review, and an undecided row leaves the
  item pending. Every existing reader, rollup and RCM projection still reads
  the item's one disposition and none of them changed.
- **A departed record leaves the grid rather than greying in it.** Section 4.2
  wanted a greyed row saying *no longer payment_voucher*. Re-resolution drops
  it, and its answer and disposition with it, because an assessment of evidence
  the test no longer speaks about must not settle an item. What replaces the
  greyed row is the coverage strip: `unread_documents` names every document of
  the type that produced no current reading, which is the honest version of the
  same gap and covers the case a greyed row could not (a document that was
  never read at all).
- **The stage unit cap is raised, not worked around.** Section 3.7 preferred
  one unit per item fanning out internally. A resolved population is already
  bounded — by `MAX_POPULATION_RECORDS` and by the auditor's selection — so
  `max_units_per_stage` is raised to what the engagement's populations actually
  resolve to (`assessment_pairs` + headroom, in routing and in both
  `_refresh_dynamic_limits`). The stage count stays truthful and the run pays
  for exactly the assessments it makes.
- **The question is written about one record.** A consequence of question 1
  that the design did not anticipate: naming a population in the step led the
  generation turn to word the question about the population — measured on the
  first regenerated engagement, *"Do all payment vouchers carry every required
  claim field…"*, which each record's own answer can only guess at. The
  generation prompt now says the question is put to one record at a time and
  the population counts are computed from the answers; the assessment prompt
  says the same from the other side (*judge this record only*).
- **The generation turn had to be told when *not* to write a Data Test.**
  Measured on the regenerated engagement: `RCM-130E80` asks that claims stay
  "within the applicable policy categories", with criteria naming alcohol,
  personal expenditure and fines as non-reimbursable. All three of its
  attributes said `evidence_kind: tabular_population`, and the closing prompt
  rule said a tabular attribute normally produces a Data Test — so the turn
  wrote `DAT-C46F9873D7`, whose Polars step encodes the policy as an invented
  category whitelist and filters on two literals lifted from the column value
  domains it was shown. It validates cleanly and establishes nothing. The
  prompt now says a Data Test is a predicate over columns, that a requirement
  turning on the criteria is a document question over the type that carries the
  field however the attribute is typed, and that a column's `values` is a
  domain rather than a criterion or an answer.

  Two of that step's three literals came from the column value domains the
  context supplied, so the disclosure itself was the other half. A column's
  values reach the turn when the table has a population and each value recurs
  (`MIN_CATEGORY_ROWS`, `MIN_CATEGORY_REPETITION`), which was supposed to make
  "an identifier, a name, or a free-text field fail by construction" — and does
  not: free text written from a handful of templates repeats often enough to
  pass. Measured across the engagements on this machine, 551 columns disclosed
  their complete value set, among them a treasury `DISCREPANCY_NOTE` whose three
  sentences are exception descriptions. `_label_domain` is a third bound on
  shape rather than frequency: a value containing a space must be at most 32
  characters. Every genuine label domain measured — job titles, account names,
  departments, approval channels, counterparty types — tops out at 27, and the
  next value up is 44, so the bound sits in a real gap rather than on a tuned
  threshold. It withholds 13 columns and keeps 538. It does *not* separate
  `business_purpose` ("Ride to personal residence", 26) from `gl_account_name`
  ("Employee Travel and Expense", 27) — they are the same shape, and the prompt
  rule is what covers that one.
- **Sampling shipped with step 1.** The model needed `selection` anyway, and
  drawing it reuses `cycle_vouching._sample_row_indices` over a projected
  frame, so `random`, `interval` and `stratified` all work. The deterministic
  short-cut of 3.5 remains out of scope.
