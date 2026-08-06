# Cycle-linked vouching and grid plan

Status: revised against the current application and `Workspaces/procurement` on 2026-08-06. This is a clean target design, not a legacy-migration plan.

## 1. Decision

The vouching grid is still the correct review surface:

- one row per selected transaction cycle;
- one column per audit assertion;
- one cell per transaction/assertion result; and
- drill-down from a cell to the supporting documents and citations.

The original plan started too late in the pipeline. The procurement workspace shows that the current cycle builder can produce the wrong document pack before the UI sees it. A wider grid would make those errors easier to scan, but would not make the testing reliable.

The implementation order is therefore:

1. create a typed transaction-evidence and voucher-role model;
2. generate stronger RCM control attributes and executable cycle-test definitions;
3. link and evaluate complete cycles deterministically;
4. expose the result as a grid; and
5. add column authoring, rollups, and working-paper output on top of the same canonical results.

The existing synthetic cycle tests may be deleted and regenerated. Do not add compatibility readers, migration heuristics, or dual-write paths for their current schema.

## 2. What the procurement workspace demonstrates

The workspace contains four source populations:

| Table | Rows | Columns |
| --- | ---: | ---: |
| `invoice_data` | 118 | 15 |
| `po_data` | 93 | 16 |
| `requisitions` | 112 | 18 |
| `vendor_master_file` | 39 | 14 |

Five analyzed vouchers form one evidenced procure-to-pay cycle:

- purchase requisition;
- purchase order;
- goods receipt;
- vendor invoice; and
- payment voucher.

The current workspace also has 11 generated cycle-vouch tests containing 61 checks. Each test has only one linked item, while individual tests already contain between one and ten checks. This changes two assumptions in the earlier plan:

- current generation is already grouping many attributes into one test, so test-list fragmentation is not the immediate problem; and
- the grid is primarily needed to review a wide result and to support future multi-sample tests, not to solve the current one-item navigation alone.

The inspection exposed these correctness defects.

### 2.1 Coarse document types cannot identify cycle roles

The vendor invoice and payment voucher are both classified as `invoice`. The purchase requisition and purchase order are both classified as `purchase_order`. `build_cycle_vouching()` reduces `document_type -> role` to one dictionary entry, so the later declaration wins and both documents are assigned to the same role.

Consequences in the generated tests include:

- both invoice documents assigned as `payment_record`, leaving `vendor_invoice` missing;
- both purchase documents assigned as `purchase_requisition`, leaving `purchase_order` missing; and
- valid checks becoming `missing` even though the document is present.

Role identity must therefore be part of the extracted record, not inferred from a test-local many-to-one mapping of broad document types.

### 2.2 Direct anchor matching does not build a cycle

The current builder attaches only documents that contain the population row's selected anchor value. For example, an internal invoice ID links the vendor invoice but does not directly link the PO or GRN. Those documents are still connected through the invoice's PO and GRN identifiers.

Cycle construction must follow a bounded graph of typed transaction identifiers. It must not stop at documents that contain the seed value.

### 2.3 Not every identifier is a transaction key

The current anchor candidate logic treats every extracted identifier alike. In procurement this makes `BUYER_ID` look attractive because one buyer recurs across many PO rows, and vendor identifiers connect unrelated transactions. Joined tables also duplicate candidate populations.

Entity and organizational identifiers must never form transaction-cycle edges. Candidate scoring must also prefer an authoritative source population over a derived join unless the join is explicitly required to define the population.

### 2.4 Generated checks can be syntactically valid but semantically wrong

Examples found in the generated definitions include:

- comparing a row vendor ID with a document vendor name;
- using a unit-price path for item description or received quantity;
- using an approval path whose implicit default is the approval date when the test means approver or decision;
- a date-order check with its operands reversed;
- a string tolerance such as `0 days` where execution requires an integer or typed object; and
- paths for roles or fields that no selected document can supply.

Shape-only dotted-path validation is insufficient. The test-generation contract must be typed and validated against the selected population, role profiles, field attributes, and operator types before a test is committed.

### 2.5 Extraction failure is not missing evidence

At least one receipt and several requisition dates retain readable raw values while their normalized values are null. The current evaluator reports those cases like an absent field. The new schema must preserve normalization status and distinguish:

- missing document or field;
- present raw evidence that failed normalization;
- multiple conflicting extracted values; and
- a genuine comparison mismatch.

### 2.6 Repeated anchors do not imply duplicate tests

Several procurement tests use the same anchor but cover different RCM rows and controls. One RCM row also legitimately has two cycle tests because approval is tested at different lifecycle points and against different populations.

Consolidation must be scoped to one RCM row and one coherent population/procedure. Never merge tests globally by anchor table and column.

## 3. Target domain model

### 3.1 Voucher record kinds and typed identifiers

Replace the broad voucher `document_type` as the role-binding key with a closed `record_kind` vocabulary. The initial procurement set is:

```text
purchase_requisition
purchase_order
goods_receipt
vendor_invoice
payment_voucher
other
```

The document-analysis response must return a `records` array. Each record has a stable local `record_id`, one `record_kind`, and the evidence that supports that classification. A normal single-voucher file produces one record; a combined pack may produce several, all citing their own pages. If a record cannot be classified uniquely, it returns `other` with candidate kinds and a review reason; the cycle builder must not guess. The kind registry is extensible for other business cycles, but an unknown kind fails closed until registered.

Identifiers also use a closed kind registry. The registry, not the model, declares whether a kind may create a cycle edge.

```text
Transaction-linking examples:
  requisition_number
  purchase_order_number
  goods_receipt_number
  vendor_invoice_number
  internal_invoice_id
  payment_voucher_number

Non-linking examples:
  vendor_id
  buyer_id
  employee_id
  department_id
  account_number
```

Every extracted typed value has this normalization envelope:

```yaml
raw_value: 29-Apr -2024
value: 2024-04-29
normalization_status: normalized
normalization_error: null
citation: ...
```

Improve the date normalizer for common human formats, but retain `raw_value` and an explicit `invalid` status whenever normalization still fails.

### 3.2 RCM control attributes

Keep one risk and one asserted control per RCM row, but replace the single top-level `assertion` string with canonical `control_attributes`. These describe what the control requires; they do not contain executable table names, columns, document paths, or expected values.

```yaml
id: RCM-...
business_cycle: procure_to_pay
risk: Invoices may be paid without evidence of receipt.
control: Invoices are matched to POs and GRNs before payment.
control_attributes:
  - key: three_way_match
    assertion: Existence
    requirement: A paid vendor invoice is supported by a PO and goods receipt.
    evidence_kind: transaction_cycle
    required_record_kinds:
      - vendor_invoice
      - purchase_order
      - goods_receipt
      - payment_voucher
  - key: receipt_before_payment
    assertion: Cut-off
    requirement: Receipt occurs no later than payment.
    evidence_kind: transaction_cycle
    required_record_kinds:
      - goods_receipt
      - payment_voucher
```

The RCM worker and validator must require unique attribute keys, the existing assertion vocabulary, a supported evidence kind, and evidence roles that are logically supported by the control wording or criteria. RCM generation must not create extra RCM rows merely to represent attributes of the same risk/control.

Tests remain the sole executable source. They reference `RCM-ID:attribute_key`; the RCM row continues to reference tests through `test_refs`.

### 3.3 Canonical cycle-test definition

Add a distinct `cycle_vouch` Document Test variant. Its `definition` is the only executable definition. Remove cycle checks from `steps` and do not copy them into every item.

```yaml
schema_version: 2
kind: cycle_vouch
rcm_id: RCM-...
requirement_refs:
  - RCM-...:three_way_match
procedure_key: invoice-three-way-match
definition:
  population:
    table: invoice_data
    row_key:
      column: INVOICE_ID
      identifier_kind: internal_invoice_id
    cycle_keys:
      - column: VENDOR_INVOICE_NUMBER
        identifier_kind: vendor_invoice_number
      - column: PO_NUMBER_LINK
        identifier_kind: purchase_order_number
      - column: GRN_ID_LINK
        identifier_kind: goods_receipt_number
    selection:
      mode: evidence_linked
  roles:
    - role: vendor_invoice
      record_kind: vendor_invoice
      required: true
      cardinality: one
    - role: purchase_order
      record_kind: purchase_order
      required: true
      cardinality: one
    - role: goods_receipt
      record_kind: goods_receipt
      required: true
      cardinality: one
    - role: payment_voucher
      record_kind: payment_voucher
      required: true
      cardinality: one
  assertions: []
```

Supported selection modes are explicit:

- `evidence_linked`: include every population row connected to at least one transaction document; and
- `sample`: materialize a deterministic sample from the stated population, including rows whose requested evidence is missing. It requires `method`, `size`, and `seed`, plus `stratify_by` when stratified.

The first delivery admits transaction-level populations only: `row_key` must be non-null and unique. A line-level table with repeated transaction keys is rejected instead of silently taking one row. Grouped/aggregated populations require a later operand reducer contract.

Coverage reports `population_rows`, `selected_rows`, `rows_with_evidence`, `complete_cycles`, and missing-role counts. It must not describe one evidenced row as assurance over the remaining population.

The stable test identity is based on `(rcm_id, kind, procedure_key, population.table, population.row_key)`, never the editable title. Two controls may use the same population without being merged. One RCM row may have two tests only when `procedure_key` and the population or lifecycle scope are materially different.

### 3.4 Structured assertions instead of dotted paths

Replace dotted path strings with discriminated operands. No field attribute is implicit.

```yaml
key: invoice_amount_to_payment
label: Invoice amount agrees to payment
left:
  source: role
  role: vendor_invoice
  field:
    group: amounts
    kind: total
    attribute: value
right:
  source: role
  role: payment_voucher
  field:
    group: amounts
    kind: total
    attribute: value
operator: numeric_within
tolerance:
  absolute: 0.01
  percent: 0
```

Row operands use `source: row` plus a required `column`. Presence assertions still identify an explicit document field and attribute; for example, an approval presence check must select `approver`, `decision`, or `date` rather than relying on a group default.

Use typed, directional operator names:

```text
equal_exact
equal_normalized
numeric_within
date_on_or_before
date_within
present
```

The validator enforces operand/operator compatibility, numeric tolerance objects, integer day tolerances, known roles, real row columns, and fields available on the selected role profiles. Fuzzy matching is not allowed for transaction identifiers. A generated definition that fails this gate is repaired by the existing bounded worker repair turn and otherwise rejected; it is never committed as a runnable test.

### 3.5 Generalized document assertions

Do not use an unqualified `*` role. A generalized assertion names its applicable roles and its quantifiers explicitly.

```yaml
key: recorded_total_agrees
label: Recorded total agrees across applicable documents
left:
  source: row
  column: INVOICE_AMOUNT
right:
  source: roles
  roles:
    - purchase_order
    - vendor_invoice
    - payment_voucher
  field:
    group: amounts
    kind: total
    attribute: value
  entry_quantifier: one
operator: numeric_within
tolerance:
  absolute: 0.01
  percent: 0
role_quantifier: all
```

Semantics:

- `entry_quantifier` controls facts within one document: `one`, `any`, or `all`;
- `role_quantifier` controls outcomes across the named role documents: `all` or `any`;
- each document produces a visible sub-result, including missing and invalid extraction states;
- an empty role or fact set never passes vacuously; and
- the first implementation supports one scalar side and one role-set side. Set-to-set comparisons are out of scope until an explicit pairing rule exists.

This makes multiplicity within a document and multiplicity across documents independent and reviewable.

### 3.6 Materialized cycle items and results

The deterministic cycle linker builds item records from the canonical definition:

```yaml
id: ITEM-...
population_ref:
  table: invoice_data
  source_row: 17
  source_sha1: ...
frozen_row: {}
cycle_identifiers: []
role_bindings:
  - role: vendor_invoice
    document_id: ...
    record_id: ...
    matched_by: []
unassigned_records: []
missing_roles: []
result_by_assertion: {}
evaluation:
  state: not_run
  definition_sha1: ...
disposition:
  state: pending
  evaluated_definition_sha1: null
  stale: false
```

Cycle linkage is exact and local:

1. seed the graph from all declared `population.cycle_keys`, not only `row_key`;
2. attach directly matching voucher records on allowed transaction identifier kinds;
3. follow the allowed identifiers extracted from those records to a bounded transitive closure;
4. assign records by `record_kind` to declared roles while retaining their parent document IDs;
5. reject or flag role-cardinality conflicts instead of choosing by list order; and
6. retain every edge in `matched_by` so the auditor can see why a document joined the cycle.

Entity identifiers never create edges. Exact identifier disagreement remains a test result; it is not fuzzy-corrected to make linkage work. In the procurement sample, a PO-number typo should still be visible as a mismatch, while other exact invoice/GRN links can keep the cycle connected. Manual attach/detach operations act on a role binding (`document_id`, `record_id`, and `role`), then recompute missing roles and stale affected results; changing only a flat `document_ids` list is not sufficient.

Execution results are keyed by immutable assertion key. Each result stores the assertion hash, input hashes, verdict, bounded display values, per-document comparisons, and evidence references. Items do not contain copies of assertion definitions.

Keep deterministic evaluation separate from auditor disposition:

- `evaluation.state`: `not_run | passed | failed | incomplete | needs_review | stale`;
- assertion verdict: `match | mismatch | missing_evidence | invalid_extraction | ambiguous | not_run`;
- `disposition.state`: `pending | confirmed | exception`;
- `disposition.stale` retains but invalidates a prior sign-off after the definition or evidence changes; and
- aggregate evaluation is `failed` for a mismatch, `incomplete` for missing/invalid evidence, `needs_review` for ambiguity or role conflict, and `passed` only when every applicable assertion matches.

This separation aligns `doc_tests.executed` with machine execution and `doc_tests.dispositioned` with the auditor's decision.

## 4. Candidate generation and test generation

### 4.1 Deterministic transaction-evidence manifest

Replace the current aggregate overlap list with a local manifest that describes:

- each voucher record's parent document ID, local record ID, and exact `record_kind`;
- its available typed fields and normalization status, without relying on a document-type union;
- allowed transaction identifier kinds;
- original source-table candidates and explicitly justified join candidates;
- row-key and cycle-key column mappings;
- uniqueness, collision, matched-row, reachable-record, reachable-document, and reachable-role counts; and
- the complete cycle packs reached by each candidate, computed locally.

Anchor ranking must penalize repeated entity-like values and candidate collisions. The model chooses only among manifest candidates that already pass deterministic safety checks. It never invents a table/column-to-identifier-kind mapping.

### 4.2 Test-generation contract

For one RCM row, `tests.generate` receives its `control_attributes` and the transaction-evidence manifest. It returns the complete discriminated tests for that row.

For cycle tests it must:

- reference every covered RCM control attribute;
- choose one validated population candidate;
- group all compatible assertions sharing that population and lifecycle scope into one `cycle_vouch` test;
- use exact `record_kind` roles;
- select fields only from the actual role profiles;
- state evidence-linked or sampled reach explicitly; and
- emit no narrative `steps[].checks` copy.

The semantic validator rejects:

- unsafe or non-unique row keys;
- entity identifiers used as cycle keys;
- derived joins when an equivalent source-table population exists;
- required roles not reachable in any candidate cycle;
- an operand that compares unlike semantic types, such as vendor ID to vendor name;
- unavailable field kinds or attributes;
- implicit field attributes;
- reversed or untyped date operators;
- invalid tolerance shapes;
- duplicate assertion keys; and
- multiple proposed cycle tests for the same RCM procedure/population that could be one assertion set.

This is a quality gate, not a migration layer. Once the new pipeline is live, regenerate the procurement cycle tests from their RCM rows and discard the old records.

## 5. Grid API

Add `GET /api/workspaces/{workspace_id}/doc-tests/{test_id}/grid` for `cycle_vouch` tests.

The endpoint is a projection over the canonical definition and item results. It does not perform execution or write state.

```yaml
test_id: DT-...
test_sha1: ...
definition_sha1: ...
title: ...
population: {}
coverage: {}
columns:
  - key: invoice_amount_to_payment
    label: Invoice amount agrees to payment
    operator: numeric_within
    applicable_roles:
      - vendor_invoice
      - payment_voucher
    counts:
      match: 0
      mismatch: 0
      missing_evidence: 0
      invalid_extraction: 0
      ambiguous: 0
      not_run: 0
rows:
  - item_id: ITEM-...
    label: INV2024004
    evaluation_state: failed
    disposition_state: pending
    roles_present: []
    missing_roles: []
    cells:
      invoice_amount_to_payment:
        verdict: mismatch
        display: 2,000,000 vs 2,100,000
        comparison_count: 1
        evidence_count: 2
        comparisons: []
page:
  offset: 0
  limit: 100
  total: 1
truncated: false
```

Requirements:

- support `offset` and `limit`, with a maximum page size of 200 rows;
- cap assertion columns at a validated definition limit rather than silently truncating them;
- compute column counts over the full test, not only the returned page;
- return bounded display values and evidence counts, never excerpts or complete document text;
- keep all per-document sub-results so generalized assertions are not collapsed to the first match;
- return `409 stale_definition` if item results were produced for a different definition and cannot be projected safely; and
- use `test_sha1` and the workspace revision for optimistic concurrency on later mutations.

The full item endpoint remains the source for citations, raw/normalized values, role-link paths, notes, and sign-off actions.

## 6. Frontend

### 6.1 Test-first navigation

For a `cycle_vouch` test:

1. the engagement worklist selects the test;
2. the main panel loads the grid, not the first item detail;
3. clicking a row or cell loads `DocTestItemDetail` on demand; and
4. closing the detail returns to the same grid scroll and filter state.

Use the available horizontal width for the grid. The item detail already contains its own action rail, so do not place it inside another permanently narrow master-detail rail. A drawer or full-width drill-down is preferable.

The summary API should expose discriminated entries rather than treating every record as the same shape:

- a cycle-test entry, classified from its aggregate evaluation/disposition and coverage;
- item entries for question/review tests where item-first triage is still appropriate.

Keep `test_counts`, `sample_counts`, and assertion counts separate. A test with one failed sample and nine mismatched assertions has one failed sample, not ten exceptions.

### 6.2 Grid behavior

The grid provides:

- sticky transaction and status columns;
- horizontally scrollable assertion columns;
- compact icons/colors with accessible text labels;
- column-header summaries and filters;
- filters for evaluation, auditor disposition, missing role, and assertion verdict;
- search over item label and frozen row display fields;
- a cell popover listing every per-document comparison; and
- a detail action that opens citations and the complete role-link path.

Do not show only the first resolved value. Ambiguity and per-document disagreement are part of the audit evidence.

The frontend currently has no unit-test runner. Add Vitest and Vue Test Utils for grid projection/state tests; keep the production build as a separate gate. Add a focused browser test for grid-to-detail navigation and state restoration.

## 7. Adding and changing assertion columns

Column authoring is useful after the canonical grid works, but it needs incremental execution semantics.

`POST /doc-tests/{test_id}/assertions` accepts a typed assertion, the expected `test_sha1`, and optional placement. The backend validates it exactly like generated assertions, assigns or verifies the immutable key, and updates the definition hash.

For every item:

- retain results whose assertion hash and input hashes are unchanged;
- create a `not_run` result only for the new or changed assertion;
- set the aggregate evaluation to `stale` until pending assertions run;
- retain the prior auditor disposition as history but mark it stale; and
- require re-sign-off against the new definition hash.

The executor runs pending assertion results rather than skipping a formerly finalized item or recomputing every column. The same mechanism handles document reanalysis and changed frozen population rows through input hashes.

Agent authoring uses a target-specific `append_cycle_assertions` action. It is not a second way to define checks: the action returns the same typed assertion contract and calls the same validator and mutation service as the REST route.

## 8. Rollups, working papers, and RCM execution

Use the sample item as the exception-counting unit.

- `failed_samples`: distinct items with at least one current mismatch;
- `incomplete_samples`: distinct items with missing evidence or invalid extraction;
- `assertion_mismatches`: diagnostic count of mismatched cells;
- `confirmed_samples`: items signed off confirmed against the current definition; and
- `open_exceptions`: distinct items signed off exception and not resolved.

Never add item exceptions and mismatched checks together. That is the current double-counting risk in RCM rollups.

The RCM execution rollup should display test-level coverage, sample outcomes, and auditor disposition separately. It should not conclude that a control is ineffective from an unreviewed normalization failure or missing document.

The cycle working paper should render:

- population and selection basis;
- cycle coverage and missing-role limitations;
- the same assertion columns as the grid;
- one row per tested cycle;
- per-column counts;
- linked evidence/citations in a separate details section; and
- preparation/review identity and definition/source hashes.

The grid API, RCM rollup, and working paper must all consume `result_by_assertion`; none may reconstruct outcomes from narrative steps.

## 9. Implementation map and delivery phases

Primary touchpoints:

- `document_analysis.py` and `agent/workers/documents.py`: voucher records, kinds, typed identifiers, and normalization status;
- new `cycle_vouching.py`: identifier registry, definition validation, candidate graph, item materialization, deterministic evaluation, and grid projection;
- `doc_tests.py` and `routes/doc_test_routes.py`: generic persistence, role-aware item mutations, disposition, and grid/assertion routes;
- `agent/context/adapters.py`, `agent/workers/tests.py`, and `agent/executors/tests.py`: evidence manifest, generation contract, semantic validation, identity, and commit;
- `agent/workers/planning.py`, `agent/executors/planning.py`, `workspaces.py`, and Planning frontend types/components: RCM control attributes;
- `agent/capabilities/doc_tests.py`, `doc_tests_execution.py`, and `agent/executors/fieldwork.py`: execution/readiness semantics shared by both workflows;
- `rcm_execution.py` and working-paper assembly: sample-level rollups and the canonical grid result source; and
- `frontend/src/types.ts`, `DocTestsTab.vue`, `DocTestItemDetail.vue`, and a new cycle-grid component: test-first review and drill-down.

Keep cycle-specific schema and compute out of the generic document-test persistence module so the clean rewrite does not further expand `doc_tests.py`.

### Phase 0 - lock the clean schemas and procurement fixture

- define `record_kind`, identifier-kind registry, normalized value envelope, RCM `control_attributes`, `cycle_vouch` definition, item/result states, and typed assertions;
- create a compact test fixture from the five-document procurement cycle; and
- add schema/validator tests before changing workers.

Exit condition: the fixture expresses the complete requisition -> PO -> GRN -> invoice -> payment cycle without broad-type aliases or dotted paths.

### Phase 1 - extraction and deterministic cycle evidence

- update document-analysis extraction and validators;
- implement transaction-safe identifier indexing and bounded transitive cycle linkage;
- implement candidate scoring over authoritative populations; and
- reanalyze the five procurement vouchers.

Exit condition: the fixture yields five distinct roles in one connected cycle; vendor/buyer IDs cannot connect another row; the PO typo remains visible without breaking the whole cycle.

### Phase 2 - RCM and test generation

- update the RCM worker, response schema, executor fields, hashes, and UI types for `control_attributes`;
- replace the cycle branch of `tests.generate` with the new definition contract;
- expand the transaction-evidence context manifest; and
- implement the semantic quality gate and stable test identity.

Exit condition: regenerated procurement tests group compatible assertions per RCM procedure, use valid typed operands, and contain no unreachable roles or fields.

### Phase 3 - item builder and evaluator

- materialize selected population rows and complete role bindings;
- evaluate scalar and explicit role-set assertions;
- preserve per-document sub-results and evidence anchors;
- separate evaluation from auditor disposition; and
- implement result/input hashes and staleness.

Exit condition: every procurement result is explainable as match, mismatch, missing evidence, invalid extraction, or ambiguity, with no role-collapse or first-match behavior.

### Phase 4 - grid and summary APIs

- add the paged read-only grid projection;
- revise the engagement summary into discriminated test/item entries; and
- expose sample, coverage, and assertion counts without double counting.

Exit condition: the grid is bounded, stable, read-only, and consistent with the item endpoint and result rollups.

### Phase 5 - grid frontend

- make cycle tests test-first and grid-first;
- add filters, summaries, sticky columns, per-document cell popovers, and drill-down;
- retain grid state across detail navigation; and
- add frontend unit and browser coverage.

Exit condition: an auditor can scan the whole sample, identify the failed assertion, and reach the exact voucher citation without navigating nested rails.

### Phase 6 - incremental assertion authoring

- add the assertion mutation route and agent action;
- execute only new/stale assertion results; and
- enforce stale disposition and re-sign-off.

Exit condition: adding a column neither destroys prior results nor leaves an old sign-off appearing current.

### Phase 7 - downstream outputs and workspace reset

- update RCM rollups and working papers to use sample-level counts and canonical assertion results;
- remove superseded cycle builders, dotted-path code, duplicated check storage, and old UI branches; and
- delete and regenerate the synthetic procurement cycle-test records under the new schema.

Exit condition: no production path reads the old cycle schema, and grid, detail, RCM, and working papers agree on the same counts.

## 10. Required tests

### Backend

- two formerly identical broad types resolve to distinct invoice/payment and requisition/PO roles;
- cycle closure follows invoice -> PO -> GRN/requisition transaction identifiers;
- vendor, buyer, employee, department, and account identifiers never form edges;
- an authoritative source table outranks an equivalent derived join;
- multiple voucher records for a cardinality-one role produce ambiguity, not arbitrary selection;
- multiple voucher records in one combined document retain distinct roles and citations;
- an exact identifier typo is a mismatch while other exact edges retain the cycle;
- raw-but-unparseable dates return `invalid_extraction`, not `missing_evidence`;
- date direction and typed tolerance validation are correct;
- row-vs-role-set assertions implement both role and entry quantifiers, with no vacuous pass;
- unavailable fields, unlike semantic types, implicit attributes, and unsafe keys reject generation;
- assertion definitions exist once and results are keyed by immutable assertion key;
- incremental assertion execution retains unaffected results and stales the prior disposition;
- manual role-aware attachment updates bindings, missing-role coverage, and affected result staleness;
- grid pagination and full-test column summaries agree;
- grid cells contain no excerpts and remain bounded with multiple evidence matches; and
- RCM failed-sample and mismatch counts do not double count one item.

### Frontend

- selecting a cycle test loads the grid without eagerly loading an item;
- selecting a row/cell opens the correct item and assertion context;
- closing detail restores filters, scroll position, and selected cell;
- generalized cells show all document sub-results;
- stale, incomplete, failed, and auditor-disposition states remain visually distinct; and
- non-cycle question/review worklists retain their item-first behavior.

### Regression gates

- document-question tests still execute through the registered document-Q&A worker;
- standalone `doc_tests_workflow_v1` and audit workflow use the same cycle item/evaluation binding;
- test-generation proposal recovery remains hash-identified and does not rebill on an unchanged proposal;
- optimistic workspace revision and parent-hash conflicts fail closed; and
- the frontend production build passes in addition to the new unit/browser tests.

## 11. Non-goals

- no compatibility or migration layer for the current synthetic cycle schema;
- no fuzzy auto-linking of transaction identifiers;
- no model execution for deterministic cycle comparisons;
- no global consolidation across RCM rows merely because anchors match;
- no set-to-set comparison until an explicit pairing contract is designed;
- no grouped line-item population until row operand reducers are explicit;
- no silent choice among conflicting documents or extracted facts; and
- no second source of executable assertions in narrative steps, item copies, UI-only state, or working-paper code.
