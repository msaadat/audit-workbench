# Cycle-linked vouching and grid plan

Status: Phase 0, the domain-neutral Phase 0.1 refactor, Phase 1, and Phase 2 are implemented as of 2026-08-08. Checkpoint A has passed automated and live procurement validation, including auditor review of the five regenerated voucher analyses. Phase 2 has passed its automated contract and production-build gates; Checkpoint B is now waiting on the required user-driven clean procurement rebuild. Phases 3 onward remain implementation-ready but must not begin until that confirmation. Procurement remains the first UX validation engagement, while the core contracts are proven independently with procure-to-pay and payroll packs. This is a clean target design with mandatory model-to-user regeneration checkpoints, not a legacy-migration plan.

## 1. Decision

The vouching grid is still the correct review surface:

- one row per selected transaction cycle;
- one column per audit assertion;
- one cell per transaction/assertion result; and
- drill-down from a cell to the supporting documents and citations.

The original plan started too late in the pipeline. The procurement workspace shows that the current cycle builder can produce the wrong document pack before the UI sees it. A wider grid would make those errors easier to scan, but would not make the testing reliable.

The implementation order is therefore:

1. create a typed transaction-evidence model backed by registered business-cycle packs;
2. generate stronger RCM control attributes and executable cycle-test definitions;
3. link and evaluate complete cycles deterministically;
4. expose the result as a grid; and
5. add column authoring, rollups, and working-paper output on top of the same canonical results.

The existing synthetic cycle tests may be deleted and regenerated, but only by the user through the existing product UX at the checkpoints in section 9. Implementation and automated tests must not reset, delete, or regenerate `Workspaces/procurement`. Do not add compatibility readers, migration heuristics, dual-write paths, endpoints, or UI states for its current schema. The implementing model tells the user when the disposable workspace must be regenerated and pauses until the user confirms it.

## 2. What the procurement workspace demonstrates

Procurement is the concrete failure case and first validation pack, not the
product ontology. Every defect below is translated into a domain-neutral rule
in section 3 and must also hold for payroll and future registered packs.

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
- the grid is primarily needed to review a wide result and to support future multi-item tests, not to solve the current one-item navigation alone.

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

### 3.1 Domain-neutral registry and typed evidence records

Replace broad document `document_type` values as role-binding keys with an
immutable registry of business-cycle packs. The core registry defines only the
shape and validation rules for normalizers, identifier kinds, field kinds,
record kinds, evidence strategies, and pack identity. It contains no
procurement record-name switch.

Built-in Phase 0.1 packs prove the boundary:

- `procure_to_pay` registers requisition, purchase-order, receipt, invoice, and
  payment-voucher evidence;
- `payroll` registers employment-contract, time-record, payroll-register,
  payslip, and bank-payment evidence; and
- `common` supplies shared non-linking entity identifiers, common fields, the
  non-bindable `common.other` record, and evidence strategies.

Domain IDs are namespaced. Procurement examples are
`procure_to_pay.purchase_order`,
`procure_to_pay.purchase_order_number`, and
`procure_to_pay.date.receipt`; payroll examples are `payroll.payslip`,
`payroll.payslip_number`, and `payroll.amount.net_pay`. Shared entity IDs use
the `common` namespace, such as `common.vendor_id` and `common.employee_id`.
Only identifier definitions with `edge_policy: transaction` can create graph
edges. The registry, never the model or a workspace payload, declares that
policy.

Every persisted registry-backed artifact carries an exact reference:

```yaml
registry:
  pack_id: procure_to_pay
  pack_version: 1
  definition_hash: sha256:...
```

The definition hash covers the expanded pack: the pack declaration and every
referenced normalizer, identifier, field, and record definition. Missing,
unknown, cross-pack, version-mismatched, or stale references fail closed. A
definition change requires a version/hash change and explicitly invalidates
dependent evidence and results. Failing closed is scoped to the artifact that
holds the stale reference: loading an engagement flags such a row as
`attributes_status: invalid` with its error, and never refuses to open the
workspace. The write paths stay strict, so the flag can only be cleared by
repairing the row through the ordinary editor — which must remain reachable for
that repair to be possible at all. Extension to another audit area means adding a
validated registered pack and runtime descriptors; it does not mean widening
TypeScript literal unions or adding a new branch to cycle-validation code.

The document-analysis response must return a `records` array. Each record has a
stable local `record_id`, one registered `record_kind`, the exact registry
reference, and the evidence that supports its classification. A normal
single-record file produces one record; a combined evidence pack may produce
several, all citing their own pages. If a record cannot be classified uniquely,
it returns `common.other` with candidate kinds and a review reason; the cycle
builder must not guess.

Every extracted typed value has this normalization envelope:

```yaml
raw_value: 29-Apr -2024
value: 2024-04-29
normalization_status: normalized
normalization_error: null
citation: ...
```

Improve the date normalizer for common human formats, but retain `raw_value` and an explicit `invalid` status whenever normalization still fails.

#### Chunk-to-record reduction

The transaction-evidence map worker runs on bounded source chunks, so it emits
`record_fragments`, not durable records. One fragment represents one candidate
record visible in that chunk and contains its registry reference, chunk ID/page
span, candidate record kind, classification evidence, citation-anchored typed
identifiers and fields, and no `record_id`. Field facts name `group`, `kind`, and
`attribute` explicitly. If a chunk contains two distinct values for a record
kind's primary identifier, the worker must emit two fragments; the map validator
rejects a fragment that blends them.

After every chunk proposal has settled, a deterministic document-local reducer builds records:

1. the selected pack defines an ordered set of primary identifier kinds for each record kind;
2. fragments with the same specific record kind and same exact typed primary identifier join one component;
3. fragments with different values of the same primary identifier kind never merge, even when they share a PO, payment, vendor, or another secondary identifier;
4. a fragment without a primary identifier joins a component only when it has a typed transaction identifier shared by exactly one component of a compatible record kind and has no contradictory identifier;
5. a fragment matching zero or several components remains in `unresolved_fragments` with `missing_identity` or `ambiguous_identity`; it contributes no comparable fact until auditor review assigns it; and
6. `record_id` is computed from the completed component only after this grouping, using the stable identity rule in section 3.7.

Within a component, identical normalized facts deduplicate while retaining
every citation. Differing facts remain separate and resolve as ambiguous.
`common.other` plus one specific record kind resolves to the specific kind; two
different specific kinds produce `record_kind_conflict` and the component is
not role-bindable. The reducer output is
`{registry, records, unresolved_fragments, conflicts}` and replaces the current
first-non-empty `document_type` plus union behavior. Children carry the same
exact registry reference as the reduction. Narrative map/reduction remains
separate.

Because map fragments have no durable IDs, two chunks cannot independently create competing record hashes. A later primary identifier can absorb an earlier partial fragment only through the exact unique rule above; page adjacency or list order alone is never sufficient. Auditor fragment assignments are stored as reviewed overrides, included in the extraction hash, and revalidated on reanalysis.

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
    registry:
      pack_id: procure_to_pay
      pack_version: 1
      definition_hash: sha256:...
    required_record_kinds:
      - procure_to_pay.vendor_invoice
      - procure_to_pay.purchase_order
      - procure_to_pay.goods_receipt
      - procure_to_pay.payment_voucher
  - key: receipt_before_payment
    assertion: Cut-off
    requirement: Receipt occurs no later than payment.
    evidence_kind: transaction_cycle
    registry:
      pack_id: procure_to_pay
      pack_version: 1
      definition_hash: sha256:...
    required_record_kinds:
      - procure_to_pay.goods_receipt
      - procure_to_pay.payment_voucher
```

The RCM worker and validator must require unique attribute keys, the existing
assertion vocabulary, and a registered evidence strategy. Evidence strategy is
a discriminator: `transaction_cycle` requires an exact registry reference and at
least two unique bindable record kinds from that pack; `tabular_population`,
`document_content`, `manual_inspection`, `inquiry`, and `mixed` forbid record
kinds. A cycle is a link between records, so a requirement satisfied by a single
record kind is `document_content` or `tabular_population`, never a cycle whose
graph can only reach its own seed. The row's `business_cycle` is a projection of
the validated attributes: it is derived on every write, never required from the
caller, and never a separately editable field. This keeps non-cycle audit work independent of business-cycle
vocabulary. Required roles must still be logically supported by the control
wording or criteria. RCM generation must not create extra RCM rows merely to
represent attributes of the same risk/control.

Tests remain the sole executable source. They reference `RCM-ID:attribute_key`; the RCM row continues to reference tests through `test_refs`.

### 3.3 Canonical cycle-test definition

Add a distinct `cycle_vouch` Document Test variant. Its `definition` is the only executable definition. Remove cycle checks from `steps` and do not copy them into every item.

```yaml
schema_version: 2
kind: cycle_vouch
registry:
  pack_id: procure_to_pay
  pack_version: 1
  definition_hash: sha256:...
rcm_id: RCM-...
requirement_refs:
  - RCM-...:three_way_match
procedure_key: invoice-three-way-match
definition:
  population:
    candidate_id: CYCLE-CAND-...
    selection_reason: Best eligible population for the invoice-stage control requirement.
    table: invoice_data
    row_key:
      column: INVOICE_ID
      identifier_kind: procure_to_pay.internal_invoice_id
    cycle_keys:
      - column: VENDOR_INVOICE_NUMBER
        identifier_kind: procure_to_pay.vendor_invoice_number
      - column: PO_NUMBER_LINK
        identifier_kind: procure_to_pay.purchase_order_number
      - column: GRN_ID_LINK
        identifier_kind: procure_to_pay.goods_receipt_number
    selection:
      mode: evidence_linked
      assurance_scope: targeted_evidence_only
  roles:
    - role: vendor_invoice
      record_kind: procure_to_pay.vendor_invoice
      required: true
      cardinality: one
      reuse_across_items: exclusive
    - role: purchase_order
      record_kind: procure_to_pay.purchase_order
      required: true
      cardinality: one
      reuse_across_items: allowed
    - role: goods_receipt
      record_kind: procure_to_pay.goods_receipt
      required: true
      cardinality: one
      reuse_across_items: allowed
    - role: payment_voucher
      record_kind: procure_to_pay.payment_voucher
      required: true
      cardinality: one
      reuse_across_items: allowed
  assertions: []
```

Role names are procedure-local aliases; `record_kind`, identifier kinds, and
field selectors resolve through the test's exact pack. The validator rejects a
role from another pack and rejects a field that its selected record kind does
not expose.

Supported selection modes and their structural assurance scope are explicit:

- `evidence_linked`: include every population row connected to at least one transaction document. This is targeted evidence selection, not sampling. Its derived `assurance_scope` is always `targeted_evidence_only`, even when every currently uploaded voucher passes or happens to cover the population.
- `sample`: materialize a deterministic sample from the stated population, including rows whose requested evidence is missing. Its derived `assurance_scope` is `sampled_population`. `method` is exactly `random | interval | stratified`; `size` is 1 through 500; `seed` is a required integer; and `stratify_by` is required only for `stratified` and must name a real column.

`assurance_scope` is computed by the domain service and cannot be supplied or upgraded by the model, route caller, or frontend. An evidence-linked test may prove and support an exception in a specific item, but cannot represent a population pass rate or control conclusion.

The materializer still caps a test at 500 items, but a large evidence-linked population is not a dead-end error. When more than 500 rows qualify, the builder raises a `selection_confirmation` proposal containing the eligible-row count and a ready-to-use deterministic sample suggestion (`random`, size 25, seed 42). It is raised rather than returned so no caller can mistake the proposal for a persisted test. The manual dialog lets the auditor confirm or adjust method/size/seed/stratum; an agent-generated test carries the same proposal through its normal approval and retains it on the durable record, so a cap-derived sample stays distinguishable from a freely chosen one. No test is persisted and no rows are silently truncated until the user confirms a sampled definition.

A cycle definition is generated against one transaction-evidence manifest and is only grounded in the evidence that manifest described. The proposal therefore carries its `context_manifest_sha256`, and the persistence service refuses a commit whose re-derived manifest no longer matches: the selection is regenerated rather than applied to facts that have since changed.

The first delivery admits transaction-level populations only: `row_key` must be non-null and unique. A line-level table with repeated transaction keys is rejected instead of silently taking one row. Grouped/aggregated populations require a later operand reducer contract.

Coverage reports `population_rows`, `selected_rows`, `rows_with_evidence`, `complete_cycles`, missing-role counts, selection basis, and assurance scope. It must not describe one evidenced row as assurance over the remaining population.

The stable test identity is based on `(rcm_id, kind, procedure_key, population.table, population.row_key)`, never the editable title. Two controls may use the same population without being merged. One RCM row may have two tests only when `procedure_key` and the population or lifecycle scope are materially different.

`cardinality` is the number of records that may fill the role within one item (`one | many`). `reuse_across_items` is independent and states whether the same record may support several population items (`exclusive | allowed`). A single PO, GRN, or consolidated payment can therefore be the one bound record in several invoice-grain items without becoming a collision. The generation validator derives or verifies these declarations against the population grain and control requirement; it must not infer cross-item exclusivity from `cardinality: one`.

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
2. attach directly matching evidence records on allowed transaction identifier kinds;
3. follow the allowed identifiers extracted from those records to a bounded transitive closure;
4. assign records by `record_kind` to declared roles while retaining their parent document IDs;
5. reject or flag role-cardinality conflicts instead of choosing by list order; and
6. retain every edge in `matched_by` so the auditor can see why a document joined the cycle.

Entity identifiers never create edges. Exact identifier disagreement remains a test result; it is not fuzzy-corrected to make linkage work. In the procurement sample, a PO-number typo should still be visible as a mismatch, while other exact invoice/GRN links can keep the cycle connected. Manual attach/detach operations act on a role binding (`document_id`, `record_id`, and `role`), then recompute missing roles and stale affected results; changing only a flat `document_ids` list is not sufficient.

Execution results are keyed by immutable assertion key. Each result stores the
pack `registry_definition_hash`, assertion hash, input hashes, verdict, bounded
display values, per-document comparisons, and evidence references. A result
whose registry hash is no longer current fails closed and becomes stale; it is
never reinterpreted against a newer pack. Items do not contain copies of
assertion definitions.

Keep deterministic evaluation separate from auditor disposition:

- `evaluation.state`: `not_run | passed | failed | incomplete | needs_review | stale`;
- assertion verdict: `match | mismatch | missing_evidence | invalid_extraction | ambiguous | not_run`;
- `disposition.state`: `pending | confirmed | exception`;
- `disposition.stale` retains but invalidates a prior sign-off after the definition or evidence changes; and
- aggregate evaluation is `failed` for a mismatch, `incomplete` for missing/invalid evidence, `needs_review` for ambiguity or role conflict, and `passed` only when every applicable assertion matches.

This separation aligns `doc_tests.executed` with machine execution and `doc_tests.dispositioned` with the auditor's decision.

### 3.7 Exact graph, identity, and limit rules

A cycle edge is keyed by the exact triple
`(registry_definition_hash, identifier_kind, normalized_value)`. Values from
different kinds or pack definitions never join even when their text is equal.
Each identifier kind owns a deterministic registered normalizer. The common
default performs Unicode NFKC normalization, trims leading/trailing whitespace,
collapses internal whitespace, and case-folds; punctuation and alphanumeric
characters are preserved unless that kind registers an additional demonstrably
safe rule. Thus `PO2024004` and `P02024004` remain different.

The linker uses breadth-first traversal with these hard limits:

- maximum six identifier edges from a population seed;
- maximum 25 evidence records in one cycle closure;
- maximum 100 traversed edges; and
- maximum 20 declared roles and 50 assertions in one test.

Crossing a limit produces `needs_review` with counts and the triggering identifier; the linker never returns a silently truncated cycle. These constants live in the cycle module, are returned by the metadata endpoint, and are shared by API validation, worker validation, and the frontend.

Collision rules are deterministic:

- a candidate whose row key is null or non-unique is rejected;
- if one record reaches several population items through a role with `reuse_across_items: allowed`, retain a normal many-to-one relationship fact on every binding, including the other item IDs; do not change evaluation state merely because the record is shared;
- if one record reaches several items through a role declared `reuse_across_items: exclusive`, mark those bindings as a cross-item collision and require review;
- repeated statements of the same normalized fact in one record collapse to one fact while retaining all citations;
- different facts matching a scalar selector are ambiguous; and
- more than one record within one item for a `cardinality: one` role is a role conflict, not a list-order choice.

Shared-record facts include role, record ID, identifier edge, related item IDs, and the declared reuse rule. Assertions may test whether a consolidated PO/payment allocation is valid; the linker itself does not convert an ordinary many-to-one relationship into a grey `needs_review` result. Generation also rejects a scalar amount-equality assertion between an item and a shared aggregate record unless the assertion declares an allocation or aggregation rule.

Stable identities and invalidation are also fixed:

- `record_id` hashes the parent document ID, registry definition hash, record kind, registry-selected primary identifier kind, and normalized primary identifier. The citation fallback is used only after auditor review deliberately accepts a standalone component with no registered primary identifier; such a record is not transaction-linkable without an explicit reviewed role override. Duplicate fallback identities are an extraction conflict. A corrected primary identifier or changed pack definition deliberately creates a new record identity.
- `item_id` hashes the test semantic ID, registry definition hash, population table, row-key identifier kind, and normalized row-key value. It survives source-row reordering; the table signature and frozen-row hash remain inputs that can make its results stale.
- an automatic role binding stores the record content hash and complete `matched_by` edge chain.
- a manual role override stores `(item_id, role, document_id, record_id)` plus the bound record hash. Reanalysis preserves it only when the same record identity and compatible record kind remain; otherwise the override is retained as stale for review and is never silently dropped or rebound.
- assertion result reuse requires the same assertion hash, frozen-row hash, bound-record hashes, and extraction hashes. Any changed dependency stales only the affected results and aggregate disposition.

### 3.8 Workflow state mapping

Cycle items do not persist the old overloaded `item.state`. Phase 0 introduces shared accessors and makes every scheduler/readiness path call them instead of inspecting `state` directly:

```text
cycle evaluation          execution pending   execution current
not_run                   yes                 no
stale                     yes                 no
passed                    no                  yes
failed                    no                  yes
incomplete                no                  yes
needs_review              no                  yes

cycle disposition         disposition current
pending                   no
confirmed, stale=false    yes
exception, stale=false    yes
confirmed/exception stale no
```

`incomplete` and `needs_review` mean deterministic execution finished; they do not masquerade as auditor disposition. The auditor still confirms or records an exception against the current definition. For the four existing kinds, the accessors preserve current behavior: `pending` is unexecuted; `agent_checked` is executed but not disposed; and `confirmed`, `exception`, or legacy `manual_review` are executed/disposed.

Update `_outstanding`, `unexecuted_items`, `document_test_units`, `already_checked`, executed/disposition readiness, force/retry behavior, `run_document_test`, summaries, and rollups to use these accessors. A current cycle evaluation expands no further execution unit; a pending/stale disposition expands or exposes auditor review, never another execution unit.

The standalone workflow is bumped to `doc_tests_workflow_v2` and declares `doc_tests.definitions_ready -> doc_tests.executed -> doc_tests.dispositioned`. Normal Run test requests may stop at `executed`; disposition is an auditor checkpoint that no model or deterministic executor can satisfy. The audit workflow may produce provisional rollups after execution, but `audit.verified` and any final control conclusion require current dispositions through the same accessor.

## 4. Candidate generation and test generation

### 4.1 Deterministic transaction-evidence manifest

Replace the current aggregate overlap list with a local manifest that describes:

- each evidence record's parent document ID, local record ID, exact registry reference, and exact `record_kind`;
- its available typed fields and normalization status, without relying on a document-type union;
- allowed transaction identifier kinds;
- original source-table candidates and explicitly justified join candidates;
- row-key and cycle-key column mappings;
- uniqueness, collision, matched-row, reachable-record, reachable-document, and reachable-role counts; and
- the complete cycle packs reached by each candidate, computed locally.

Column-to-identifier mapping is inferred locally from value overlap alone. Two
identifier kinds can legitimately hold the same literal value — a payment
voucher whose voucher number *is* the invoice's internal id — so a row-count tie
is broken first by how many evidence records carry each kind, then by token
overlap between the column name and the registered identifier id and label. That
last term reads the registry's own vocabulary and therefore holds for any pack;
it is not a domain switch. A column still tied on all three is omitted rather
than guessed. Dropping an ambiguous column silently is not acceptable when it is
a table's only non-null unique key: doing so removes that grain's population
entirely. Derived joins are never inferred, because §4.1 requires an
authoritative population wherever one exists.

Each candidate has a stable `candidate_id` derived from the selected pack's
definition hash, table signature, row key, and cycle-key mappings. Safety
validation runs before ranking. Eligible candidates are sorted by this tuple:
required-role coverage descending, authoritative source table before derived
join, complete-cycle count descending, linked-row count descending, collision
count ascending, row-key column position ascending, then table and row-key names
ascending. Column position precedes the lexical fallback because an exported
ledger leads with the key of its own grain; without it a purchase-order
population is labelled by whichever identifier column happens to sort first.
The complete tuple is included in the manifest.

Coverage counts describe the rows a test would select. A row that linked no
evidence is reported once as an unlinked row and never again under each role,
so `missing_role_counts` never restates population reach as a role failure.

The model chooses only among manifest candidates that already pass deterministic safety checks and returns the exact `candidate_id` plus `selection_reason`. It may choose a lower-ranked eligible candidate only when its lifecycle/population scope better matches the RCM requirement. The semantic validator verifies both fields; it never permits the model to invent a table/column-to-identifier-kind mapping.

### 4.2 Test-generation contract

For one RCM row, `tests.generate` receives its `control_attributes` and the transaction-evidence manifest. It returns the complete discriminated tests for that row.

For cycle tests it must:

- reference every covered RCM control attribute;
- choose one validated population candidate;
- group all compatible assertions sharing that population and lifecycle scope into one `cycle_vouch` test;
- use exact `record_kind` roles with within-item cardinality and cross-item reuse semantics;
- select fields only from the actual role profiles;
- state evidence-linked or sampled reach explicitly; and
- emit no narrative `steps[].checks` copy.

The semantic validator rejects:

- unsafe or non-unique row keys;
- entity identifiers used as cycle keys;
- derived joins when an equivalent source-table population exists;
- required roles not reachable in any candidate cycle;
- role cardinality/reuse declarations inconsistent with the population grain or observed relationship facts;
- an operand that compares unlike semantic types, such as vendor ID to vendor name;
- unavailable field kinds or attributes;
- implicit field attributes;
- reversed or untyped date operators;
- invalid tolerance shapes;
- duplicate assertion keys; and
- multiple proposed cycle tests for the same RCM procedure/population that could be one assertion set.

If the chosen evidence-linked candidate would exceed 500 items, semantic validation returns the deterministic `selection_confirmation` proposal rather than accepting the definition or failing with an authoring dead end.

This is a quality gate, not a migration layer. Once the new pipeline is live, the implementing model treats the old procurement records as unsuitable for validation, tells the user to perform Checkpoint B, and does not add a product compatibility path.

### 4.3 Kind integration and manual authoring

`cycle_vouch` is a fifth Document Test kind, not an internal alias for `vouching`. Phase 0 must enumerate and test every kind switch before vertical work begins. At minimum this includes:

- `doc_tests.KINDS`, normalization/hydration, create/update/load/list/meta, `execution_issues`, `evidence_blocked`, `result_rollup`, and `summary_payload`;
- `rcm_execution._specified`, `_executable`, test manifests, observation creation, and rollups;
- `capabilities.doc_tests` scope resolution, outstanding/readiness accessors, four-shape unit expansion, and shared audit/standalone binders;
- route request/response contracts and frontend API/types; and
- `DocTestsTab`, `DocTestCreateDialog`, `DocTestItemList`, `DocTestItemDetail`, and the new grid component.

Manual authoring survives with an explicit split:

- `vouching` remains the auditor-authored single-document/listing test. `build_vouching`, `/doc-tests/build/vouching`, and the current literal-comparison detail editor remain available for this kind.
- `prepare_evidence_aware_vouching` remains available only for that simple `vouching` kind. Its availability-biased selection is marked targeted and receives the same no-population-conclusion restriction as `evidence_linked` cycle tests.
- manual cycle authoring is replaced, not dropped. `DocTestCreateDialog` gains a distinct Cycle vouch shape that uses the deterministic candidate list, typed roles, selection basis, and typed assertions, then calls the same `cycle_vouching.create_test` service as generated tests.
- the old `build_cycle_vouching` implementation and `POST /doc-tests/build/cycle` contract are removed. Manual creation uses `POST /doc-tests/build/cycle-vouch` with the canonical typed request; there is no old-payload fallback.
- `/prepare-evidence-aware` and the item comparisons patch route reject `cycle_vouch`. Cycle columns change only through the canonical test-level assertions service from section 7; the existing comparisons patch remains scoped to simple `vouching`.

Generated and manual cycle tests therefore share definition validation, candidate identity, item construction, execution, and grid projection. No manual UX path writes `steps[].checks` or per-item cycle check copies.

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
assurance_scope: targeted_evidence_only
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
- return selection basis, assurance scope, and shared-record relationship facts;
- return bounded display values and evidence counts, never excerpts or complete document text;
- keep all per-document sub-results so generalized assertions are not collapsed to the first match;
- return `409 stale_definition` if item results were produced for a different definition and cannot be projected safely; and
- use `test_sha1` and the workspace revision for optimistic concurrency on later mutations.

The full item endpoint remains the source for citations, raw/normalized values, role-link paths, notes, and sign-off actions.

## 6. Frontend

The frontend keeps literal unions only for structural states and operators.
Pack, record, identifier, field, and evidence-kind IDs are strings constrained
by the descriptors returned from Document Test metadata. Authoring selects one
pack reference first, then derives every option from that exact descriptor;
saved IDs from one pack are never offered under another pack. Labels shown to
auditors come from descriptors rather than from procurement-specific switch
statements.

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

Keep `test_counts`, `tested_item_counts`, and assertion counts separate. A test with one failed item and nine mismatched assertions has one failed item, not ten exceptions.

### 6.2 Grid behavior

The grid provides:

- sticky transaction and status columns;
- horizontally scrollable assertion columns;
- compact icons/colors with accessible text labels;
- column-header summaries and filters;
- filters for evaluation, auditor disposition, missing role, and assertion verdict;
- a persistent Targeted evidence or Sampled population scope label;
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

Use the tested item as the exception-counting unit.

- `failed_items`: distinct items with at least one current mismatch;
- `incomplete_items`: distinct items with missing evidence or invalid extraction;
- `assertion_mismatches`: diagnostic count of mismatched cells;
- `confirmed_items`: items signed off confirmed against the current definition; and
- `open_exceptions`: distinct items signed off exception and not resolved.

Never add item exceptions and mismatched checks together. That is the current double-counting risk in RCM rollups.

The RCM execution rollup displays test-level coverage, item outcomes, selection basis, assurance scope, and auditor disposition separately. `_rollup_doctest` returns `conclusion_eligible` and `assurance_scope` rather than deriving a control conclusion from result counts alone.

For `targeted_evidence_only` tests, the restriction is structural:

- the test's `control_conclusion` is fixed at `no_conclusion` for automated reconciliation and cannot contribute a population passed/failed count;
- a clean targeted result cannot support effectiveness or a projected exception rate;
- a confirmed mismatch may create an item-specific targeted observation/finding candidate with its evidence, but its summary must not extrapolate beyond the tested item; and
- RCM completion and `audit.verified` treat it as supplemental item evidence, not as population coverage.

Only a `sampled_population` test with current evaluation and auditor disposition is eligible to support a population-level control conclusion, and the auditor still owns that conclusion. An unreviewed normalization failure or missing document never concludes that a control is ineffective.

The cycle working paper should render:

- population, selection basis, and assurance scope, with an explicit `Targeted evidence - not a sample` label where applicable;
- cycle coverage and missing-role limitations;
- the same assertion columns as the grid;
- one row per tested cycle;
- per-column counts;
- linked evidence/citations in a separate details section; and
- preparation/review identity and definition/source hashes.

The grid API, RCM rollup, and working paper must all consume `result_by_assertion`; none may reconstruct outcomes from narrative steps.

## 9. Manual regeneration checkpoints

`Workspaces/procurement` is a user-owned disposable UX workspace, not an automated fixture. No implementation command, test setup, migration, executor, or agent action may delete, rewrite, reanalyze, or regenerate it on the user's behalf. Automated coverage must use temporary workspaces or the compact procurement fixture created in Phase 0.

Regeneration notification is an implementation-conversation responsibility, not a product feature. Do not add backend status codes, recovery contracts, routes, banners, buttons, or frontend states solely to detect or explain this test-workspace regeneration.

At each checkpoint the implementing model pauses and tells the user in chat:

- what code/schema identity changed;
- which procurement artifacts are now unsuitable for the next validation step;
- exactly what to recreate or rerun through the UX that already exists at that point;
- what result the user should expect to inspect; and
- which implementation step is waiting for confirmation.

If the existing UX has no in-place regeneration path, the model instructs the user to recreate the disposable workspace through the normal Home and intake flows. It does not add a product action for this purpose. No later implementation step may assume regenerated procurement state until the user confirms completion.

### Checkpoint A - voucher reanalysis after Phase 1

Trigger: the voucher-record schema, identifier registry, normalizer, or voucher worker identity changes.

The implementing model tells the user to open `procurement` and re-run voucher analysis for the five voucher documents through the analysis UX or assistant command that already exists. If the current product cannot reanalyze them in place, the model instead tells the user to recreate the disposable workspace through the existing Home/intake flow. No new regeneration-only product action is added. The user reviews and accepts the resulting records through the UI.

Expected result:

- five distinct record kinds: purchase requisition, purchase order, goods receipt, vendor invoice, and payment voucher;
- each record exposes typed transaction identifiers and its classification evidence;
- fragment reduction shows no unexplained unresolved fragment or record-kind conflict for the five single-voucher sources;
- the receipt/requisition raw dates are either normalized or explicitly marked invalid; and
- no cycle test is run against the old analysis hashes.

Completion record (2026-08-06): Checkpoint A passed. The user re-ran and
reviewed the five voucher analyses through the existing UX. Their active
evidence uses procure-to-pay pack version 2 with definition hash
`sha256:e1853d61a0f8c97ec19166941e53057b0609a68232b796453c48afafe85e483c`,
contains exactly one current record for each of purchase requisition, purchase
order, goods receipt, vendor invoice, and payment voucher, and has no unresolved
fragments or record-kind conflicts. Exact bounded linkage reaches all five
records; entity identifiers remain non-linking; the `P0`/`PO` typo remains
visible without fuzzy correction; and the receipt, requisition, and payment
dates are normalized. No cycle test existed or ran against stale evidence.

The payment-voucher heading and voucher reference were visible but absent from
the PDF text layer. At the user's explicit request, the source was replaced
through the normal document service with a visually identical PDF containing an
extractable invisible text layer. Replacement changed the source hash,
invalidated the prior analysis, and refreshed extraction; the user then re-ran
and reviewed that analysis. The implementation did not regenerate or accept any
workspace analysis on the user's behalf.

The implementation does not perform this reanalysis. Phase 2 may be coded against automated fixtures, but live-workspace UX validation waits for the user's checkpoint confirmation.

### Checkpoint B - full clean regeneration after Phase 2

Trigger: the RCM `control_attributes` or cycle-test definition schema becomes authoritative. This invalidates the current RCM-derived chain, including linked Data Tests, question/review Document Tests, cycle tests, execution rollups, working papers, findings support status, and report inputs.

The canonical clean-break test is a full user-driven workspace regeneration through normal product UX. The implementer must tell the user to:

1. create a fresh procurement workspace, or delete/recreate the disposable one through the Home UX;
2. import the four original source data files and all original source documents through intake;
3. run and review document analysis, including the five voucher records;
4. generate planning context and APM;
5. generate and review the RCM with `control_attributes`; and
6. generate the complete RCM-linked test set, not only cycle tests.

The checkpoint handoff must use the actual labels present in the implemented UI and state which steps are assistant/agent commands. The implementation must not copy old RCM rows, test records, findings, reports, or generated workspace artifacts into the new workspace.

Expected result: test generation produces clean-schema tests for the new RCM IDs; compatible assertions are grouped per RCM procedure/population; and the procurement cycle tests show reachable, correctly classified roles before execution.

This full rebuild is the reset cascade. There is no in-place product migration requirement for the old synthetic RCM or its dependent artifacts.

Phase 2 handoff (2026-08-08; awaiting user confirmation): use these exact
current UI actions, and do not copy any generated artifact from the existing
workspace.

1. On Home, select **New engagement**, enter a fresh name such as
   `procurement-phase2`, then select **Create and add files**. Alternatively,
   for the disposable workspace only, use its menu action **Delete workspace**,
   confirm **Delete**, then recreate it with **New engagement**.
2. In **Import files and folders**, add the four original source data files and
   every original source document. Select **Import N files**, review the local
   classifications and names, select **Import N files** again to apply them,
   then select **Done**. Do not import an RCM export, test files, findings,
   reports, working papers, or anything from the old workspace.
3. Open **Documents** and select **Analyze all**. This starts the
   `analyze_documents` assistant/agent command. Wait for it to finish, open the
   analyses, and use **Save and mark reviewed** for the reviewed results,
   including the purchase requisition, purchase order, goods receipt, vendor
   invoice, and payment voucher records.
4. Open **Planning > APM** and select **Generate planning drafts**. This starts
   the `plan` assistant/agent command; the current command generates or
   reconciles planning context, the APM, the RCM, and its complete linked Data
   and Document Tests in dependency order. Review/approve its proposal items in
   the assistant Console, then review the APM in **APM**.
5. Open **Planning > RCM**. Review every generated row, open **RCM detail**, and
   verify the **Control attributes** entries have the intended assertion and
   evidence strategy. Transaction-cycle attributes must show the exact cycle
   pack and required record kinds. Use **Save RCM row** for auditor edits.
6. Still in **Planning > RCM**, select **Generate planned tests (N)** if that
   button appears. This starts the `tests.specified` assistant/agent command for
   every RCM row still lacking tests. In **Document Tests**, use **Prepare with
   assistant** only if a draft remains; that starts the
   `prepare_document_tests` assistant/agent command. Review the complete linked
   Data and Document Test set, and verify each **Cycle vouch** test shows a
   prevalidated population, reachable exact record-kind roles, grouped typed
   assertions, and either **Targeted evidence** or **Sampled population** scope.
   Stop before running any cycle test; execution belongs to Checkpoint C.

Expected inspection before confirmation: all tests use the new RCM IDs, no
cycle definition contains narrative `steps[].checks`, compatible assertions
are grouped by procedure/population, and no role or field is unreachable. Phase
3 remains blocked on the user's confirmation that this clean rebuild and review
are complete.

### Checkpoint C - cycle execution after Phase 3

Trigger: the new item builder and evaluator are available against the clean Phase 2 definitions.

The implementer tells the user which cycle tests to run through the normal test-execution UX. The user initiates execution and reviews the PO-number mismatch, extraction state, role bindings, matched-by chains, and auditor disposition controls. The implementation must not run or sign off the tests.

Expected result: evaluation produces current `result_by_assertion` entries, leaves auditor disposition pending, and distinguishes mismatch, missing evidence, invalid extraction, and ambiguity.

### Checkpoint D - grid review after Phase 5

Trigger: the grid frontend is available. No artifact regeneration is expected when the Phase 3 definition and result hashes remain current.

The implementing model tells the user to reload `procurement`, open a cycle test, exercise grid filters and horizontal review, open multi-result cells, drill into citations, and return to the preserved grid state. If implementation changes since Checkpoint C invalidated results, the model explains that in chat and asks the user to rerun only those tests through the existing UX.

### Checkpoint E - incremental assertion UX after Phase 6

Trigger: assertion-column authoring and pending-result execution are available.

The implementer tells the user to add or change an assertion through the UI or the target-specific assistant action, inspect the stale prior sign-off, initiate execution of the pending assertion, and re-sign off manually.

Expected result: unaffected cells retain their hashes/results, only the new or changed column runs, and no old disposition appears current.

### Checkpoint F - downstream regeneration after Phase 7

Trigger: assurance-aware RCM rollups and cycle working papers are available.

The user manually dispositions the tested cycle items and invokes the normal rollup, working-paper, and report-generation UX as applicable. The implementation does not generate or overwrite those artifacts. Existing report reconciliation rules continue to prevent silent replacement.

Expected result: the grid, item detail, RCM execution rollup, working paper, findings support status, and report inputs agree on tested-item counts, assurance scope, conclusion eligibility, and current hashes.

Any later implementation change that invalidates user-visible procurement artifacts adds a new checkpoint or explicitly reuses one above. The implementer never treats regeneration as an invisible setup step.

## 10. Implementation map and delivery phases

Primary touchpoints:

- `cycle_registry/models.py`, `registry.py`, `common.py`, and `packs/`:
  domain-neutral immutable definitions, validation, expanded definition hashes,
  runtime metadata, and registered business-cycle packs;
- `document_analysis.py`, document workflow reduction, `agent/workers/documents.py`, document routes, and `DocumentsTab.vue`: registry-backed evidence fragments, deterministic record reduction, reviewed-fragment assignment, kinds, typed identifiers, and normalization status;
- `cycle_vouching.py`: registry-reference enforcement, definition validation,
  candidate graph, item materialization, deterministic evaluation, and grid
  projection;
- `doc_tests.py` and `routes/doc_test_routes.py`: fifth-kind integration, retained simple-vouching builders, removal/replacement of the old cycle builder, generic persistence, role-aware mutations, disposition, and grid/assertion/manual-cycle routes;
- `agent/context/adapters.py`, `agent/workers/tests.py`, and `agent/executors/tests.py`: evidence manifest, generation contract, semantic validation, identity, and commit;
- `agent/workers/planning.py`, `agent/executors/planning.py`, `workspaces.py`, and Planning frontend types/components: RCM control attributes;
- `agent/workflows/doc_tests.py`, `agent/capabilities/doc_tests.py`, `doc_tests_execution.py`, `agent/executors/fieldwork.py`, and audit verification: state accessors, workflow-v2 disposition, and execution/readiness semantics;
- `rcm_execution.py` and working-paper assembly: assurance-scope eligibility, tested-item rollups, and the canonical grid result source; and
- `frontend/src/types.ts`, `DocTestsTab.vue`, `DocTestCreateDialog.vue`, `DocTestItemList.vue`, `DocTestItemDetail.vue`, and a new cycle-grid component: structural unions plus runtime pack descriptors, fifth-kind/manual authoring, test-first review, and drill-down.

Keep cycle-specific schema and compute out of the generic document-test persistence module so the clean rewrite does not further expand `doc_tests.py`.

### Phase 0 - lock the clean schemas and procurement fixture (completed)

- define `record_kind`, identifier-kind/cardinality registry, fragment/reduced-record contracts, normalized value envelope, RCM `control_attributes`, `cycle_vouch` definition, assurance scope, typed assertions, and the evaluation/disposition workflow accessors;
- enumerate every backend/frontend fifth-kind switch, the retained simple-vouching paths, the replacement manual-cycle path, and the `doc_tests_workflow_v2` graph;
- create a compact test fixture from the five-document procurement cycle; and
- add schema/validator tests before changing workers.

Exit condition met: the fixture expresses the complete requisition -> PO -> GRN
-> invoice -> payment cycle without broad-type aliases or dotted paths; Phase 0
tests prove cycle items expand exactly once for execution and remain pending only
for auditor disposition.

### Phase 0.1 - make the contract domain-neutral (completed)

- split immutable registry models and validation from domain pack definitions;
- move procurement names into the namespaced `procure_to_pay` pack and add the
  independent `payroll` pack as a second vertical contract fixture;
- introduce expanded pack version/hash references and require them on fragments,
  reductions, records, transaction-cycle RCM attributes, and tests; bind each
  assertion result to the exact definition hash;
- make evidence strategy a discriminator so non-cycle document, data, inquiry,
  inspection, and mixed work does not require record kinds;
- validate identifier edge policy, record bindability, field availability, and
  cross-pack references through the selected pack;
- replace procurement-specific frontend literal unions with generic IDs and
  runtime registry descriptors while retaining literal unions only for true
  structural state; and
- update the contract inventory and plan before extraction work begins.

Exit condition met: both procure-to-pay and payroll fixtures pass the same
generic validators; the registry core contains no procurement name switch;
stale and cross-pack references fail closed; and the metadata contract exposes
both packs for dynamic frontend authoring.

### Phase 1 - extraction and deterministic cycle evidence (completed)

- update document-analysis map extraction to select a registered pack and emit
  exact registry references, then implement deterministic record reduction and
  reviewed-fragment overrides against that pack;
- implement transaction-safe identifier indexing and bounded transitive cycle linkage;
- implement candidate scoring over authoritative populations; and
- stop at Checkpoint A, tell the user in chat what to reanalyze through the existing UX, and do not reanalyze the procurement workspace.

Exit condition met: automated fixtures reduce partial multi-chunk records
without first-kind/list-order behavior, yield five distinct roles in one
connected cycle, preserve normal shared PO/payment relationships, prevent
vendor/buyer edges, and leave the PO typo visible without breaking the whole
cycle. Checkpoint A's user-driven regeneration and auditor review are recorded
in section 9.

### Phase 2 - RCM and test generation (implemented; Checkpoint B pending)

- update the RCM worker, response schema, executor fields, hashes, and UI for
  discriminated `control_attributes`; only transaction-cycle attributes select
  a registry pack and record kinds;
- replace the cycle branch of `tests.generate` with the new definition contract;
- expand the transaction-evidence context manifest;
- integrate the fifth kind across persistence, RCM executability, workflow expansion, summaries, routes, and registry-driven frontend forms;
- retain simple manual `vouching`, replace manual cycle authoring with the canonical Cycle vouch dialog/route, and remove the old cycle payload/comparison paths;
- implement assurance-scope derivation and the large-population `selection_confirmation` proposal;
- implement the semantic quality gate and stable test identity; and
- stop at Checkpoint B with the full manual workspace-regeneration instructions.

Exit condition: generated and manually authored cycle tests use the same clean definition service, group compatible assertions per RCM procedure, use valid typed operands, and contain no unreachable roles or fields. Simple manual vouching remains usable, and populations above 500 produce a confirmable sample proposal. The implementer does not claim live procurement success until the user completes Checkpoint B.

Review remediation (2026-08-08). A code review of the Phase 2 implementation,
run against the regenerated procurement workspace, found and fixed:

- mapping inference discarded `invoice_data.INVOICE_ID` as ambiguous, because
  the payment voucher's own number is literally the invoice's internal id. That
  left the table with no viable row key and no invoice-grain population at all —
  the exact population section 3.3 uses as its worked example. Ties now resolve
  by evidence reach and then by registered naming;
- candidate ranking keyed `po_data` on `GRN_ID` by lexical fallback; row-key
  column position now precedes it;
- regeneration cleared a cycle test's structural fields, so `status` fell back
  to `draft` and the test silently left `definitions_ready`;
- `build_cycle_vouch_test` returned either a test or a sample proposal, which
  the generation executor consumed as a test; the proposal is now raised;
- a stale pack reference in one RCM row made the whole workspace unopenable;
- `business_cycle` had to be echoed by every caller, so an attribute-only edit
  through the API or the `edit_rcm_row` action failed;
- `missing_role_counts` counted unlinked rows against every role;
- the RCM prompt embedded the full registry (14 KB), biasing generation toward
  `transaction_cycle`; it now carries only the pack references a row may
  reference, plus explicit guidance to prefer `tabular_population` wherever the
  imported tables hold the fields;
- transaction-cycle attributes accepted a single record kind;
- the manual dialog authored presence assertions only; it now authors every
  registered operator with type-constrained operands and tolerances.

The regenerated RCM's own quality is a separate matter for Checkpoint B review:
all 28 rows carry exactly one attribute, and 13 use `transaction_cycle` against
zero `tabular_population`, several for requirements the imported tables can
answer across the whole population. The prompt and validator changes above
address the cause; the rows themselves need regenerating before that RCM is
accepted.

Automated exit record (2026-08-08): RCM persistence, imports/exports, planning
workers/executors, material hashes, and the Planning editor now use
discriminated `control_attributes`; the top-level assertion is not persisted.
Test-generation context carries one content-free, hash-identified transaction
evidence manifest. Generated and manual Cycle vouch definitions pass through
the same semantic validator and persistence service, while the retired
`mode: vouch`/`POST /doc-tests/build/cycle` path fails closed and simple manual
`vouching` remains available. Assurance scope is derived from selection mode,
stable test identity excludes editable title text, and evidence-linked reaches
above 500 return the confirmable random-25/seed-42 proposal without persisting
or truncating a test. Focused Phase 0-2, planning, context, generation, and
executor gates pass, and the Vue/TypeScript production build passes. The live
procurement exit condition remains deliberately unclaimed until the user
completes Checkpoint B above.

### Phase 3 - item builder and evaluator

- materialize selected population rows and complete role bindings;
- evaluate scalar and explicit role-set assertions;
- preserve per-document sub-results and evidence anchors;
- replace raw cycle `item.state` checks with the shared execution/disposition accessors across scheduler, binder, readiness, summary, and rollup paths;
- add `doc_tests_workflow_v2` disposition readiness and audit-verification gating;
- separate evaluation from auditor disposition; and
- implement result/input hashes and staleness; then stop at Checkpoint C without running or signing off procurement tests.

Exit condition: every procurement result is explainable as match, mismatch, missing evidence, invalid extraction, or ambiguity, with no role-collapse or first-match behavior. A current evaluation does not rerun, and a pending/stale auditor disposition does not satisfy final audit verification.

### Phase 4 - grid and summary APIs

- add the paged read-only grid projection;
- revise the engagement summary into discriminated test/item entries; and
- expose tested-item, coverage, assurance-scope, and assertion counts without double counting.

Exit condition: the grid is bounded, stable, read-only, and consistent with the item endpoint and result rollups; targeted evidence is visibly distinct from a sampled population.

### Phase 5 - grid frontend

- make cycle tests test-first and grid-first;
- add filters, summaries, sticky columns, per-document cell popovers, and drill-down;
- retain grid state across detail navigation; and
- add frontend unit and browser coverage; then give the user Checkpoint D grid-review instructions.

Exit condition: an auditor can scan the selected items, see whether they are targeted or sampled, identify the failed assertion, and reach the exact voucher citation without navigating nested rails.

### Phase 6 - incremental assertion authoring

- add the assertion mutation route and agent action;
- execute only new/stale assertion results; and
- enforce stale disposition and re-sign-off; then stop at Checkpoint E for user-driven authoring, execution, and sign-off.

Exit condition: adding a column neither destroys prior results nor leaves an old sign-off appearing current.

### Phase 7 - downstream outputs and manual regeneration handoff

- update RCM rollups, observation creation, audit verification, and working papers to use tested-item counts, assurance eligibility, and canonical assertion results;
- remove superseded cycle builders, dotted-path code, duplicated check storage, and old UI branches; and
- stop at Checkpoint F and tell the user in chat to disposition results and regenerate rollups, working papers, and report inputs through the existing UX.

Exit condition: no production path reads the old cycle schema; automated fixtures show grid/detail/RCM/working-paper agreement; targeted evidence can create item-specific exceptions but never a population control conclusion; and the user receives the exact Checkpoint F actions. The implementation never resets or regenerates the procurement workspace itself.

## 11. Required tests

### Backend

- procure-to-pay and payroll fixtures traverse the same fragment, reduction,
  RCM-attribute, definition, assertion, and item validators;
- pack IDs are namespaced, cross-pack definitions reject, and unknown or stale
  version/hash references fail closed;
- changing any expanded pack definition changes its identity and invalidates
  dependent artifacts rather than reinterpreting them;
- transaction-cycle RCM attributes require bindable record kinds from one exact
  pack, while non-cycle evidence strategies forbid record kinds and require no
  domain pack;
- fields and attributes resolve through the selected record kind's declared
  availability, including generalized multi-role operands;
- two formerly identical broad types resolve to distinct invoice/payment and requisition/PO roles;
- cycle closure follows invoice -> PO -> GRN/requisition transaction identifiers;
- graph edges require the same identifier kind and conservative kind-specific normalization;
- vendor, buyer, employee, department, and account identifiers never form edges;
- graph hop/record/edge limits fail visibly without truncation;
- an authoritative source table outranks an equivalent derived join;
- candidate ranking and lexical tie-breaking are deterministic, and a table is
  keyed on the identifier of its own grain rather than on whichever identifier
  column sorts first;
- a column whose values collide across two identifier kinds resolves by evidence
  reach and then by registered naming, is never dropped where it is a table's
  only viable row key, and is omitted only when tied on every signal;
- a transaction-cycle control attribute requires at least two record kinds;
- a stale pack reference flags its own RCM row and still loads the engagement;
- regeneration replaces a cycle definition without resetting the durable test's
  status or auditor links, and refuses a commit whose evidence manifest moved;
- multiple voucher records for a cardinality-one role produce ambiguity, not arbitrary selection;
- multiple voucher records in one combined document retain distinct roles and citations;
- a PO or consolidated payment reused across invoice-grain items is a normal shared binding when allowed, while an exclusive-role reuse is a collision;
- equality to a shared aggregate amount is rejected unless allocation/aggregation semantics are declared;
- chunk fragments with the same exact primary identity reduce to one record with all citations;
- fragments with different primary identities never merge merely because they share a PO/payment identifier;
- a primary-less fragment joins only one exact compatible component, while zero/multiple candidates remain unresolved;
- durable record IDs are computed after reduction and record-kind conflicts remain non-bindable;
- an exact identifier typo is a mismatch while other exact edges retain the cycle;
- raw-but-unparseable dates return `invalid_extraction`, not `missing_evidence`;
- date direction and typed tolerance validation are correct;
- row-vs-role-set assertions implement both role and entry quantifiers, with no vacuous pass;
- unavailable fields, unlike semantic types, implicit attributes, and unsafe keys reject generation;
- assertion definitions exist once and results are keyed by immutable assertion key;
- incremental assertion execution retains unaffected results and stales the prior disposition;
- manual role-aware attachment updates bindings, missing-role coverage, and affected result staleness;
- record and item identities survive reorder/reanalysis when their identity inputs are unchanged, while changed primary identifiers and bound-record hashes stale the correct artifacts;
- cycle execution expands once for `not_run/stale`, current incomplete/review evaluations do not rerun, and stale/pending dispositions cannot satisfy final verification;
- `random`, `interval`, and `stratified` are the only sampling methods, and a qualifying population above 500 returns a confirmable sample proposal instead of truncation or a dead-end error;
- evidence-linked and evidence-aware targeted tests are structurally ineligible for population control conclusions while retaining item-specific exception evidence;
- every fifth-kind switch accepts `cycle_vouch`, manual cycle creation uses the canonical service, and retained simple `vouching` routes reject cycle payloads;
- grid pagination and full-test column summaries agree;
- grid cells contain no excerpts and remain bounded with multiple evidence matches; and
- RCM failed-item and mismatch counts do not double count one item.

### Frontend

- registry metadata renders at least procure-to-pay and payroll descriptors
  without compile-time unions for either pack;
- switching packs constrains record, identifier, and field choices to the
  selected version/hash and never mixes IDs across packs;
- selecting a cycle test loads the grid without eagerly loading an item;
- selecting a row/cell opens the correct item and assertion context;
- closing detail restores filters, scroll position, and selected cell;
- generalized cells show all document sub-results;
- the grid and working-paper preview distinguish Targeted evidence from Sampled population;
- the create dialog preserves simple vouching, creates canonical cycle tests, and confirms/adjusts the large-population sample proposal;
- stale, incomplete, failed, and auditor-disposition states remain visually distinct; and
- non-cycle question/review worklists retain their item-first behavior.

### Regression gates

- document-question tests still execute through the registered document-Q&A worker;
- manually authored simple `vouching`/tracing tests still build, edit comparisons, execute, and render outside the cycle grid;
- standalone `doc_tests_workflow_v2` and audit workflow use the same cycle item/evaluation/disposition accessors and execution binding;
- test-generation proposal recovery remains hash-identified and does not rebill on an unchanged proposal;
- optimistic workspace revision and parent-hash conflicts fail closed;
- automated tests and implementation scripts never write, delete, reanalyze, or regenerate `Workspaces/procurement`; and
- the frontend production build passes in addition to the new unit/browser tests.

## 12. Non-goals

- no compatibility or migration layer for the current synthetic cycle schema;
- no model-defined, workspace-defined, or free-text record/identifier/field
  kinds; extensions are reviewed code-owned registry packs;
- no procurement switch in generic persistence, validation, workflow, or
  frontend type unions;
- no backend or frontend feature whose only purpose is notifying or performing regeneration of the procurement test workspace;
- no fuzzy auto-linking of transaction identifiers;
- no treating an allowed shared PO, GRN, or consolidated payment as a collision merely because it supports several items;
- no model execution for deterministic cycle comparisons;
- no global consolidation across RCM rows merely because anchors match;
- no set-to-set comparison until an explicit pairing contract is designed;
- no grouped line-item population until row operand reducers are explicit;
- no population control conclusion from evidence-linked or evidence-aware targeted selection;
- no removal of auditor-authored simple vouching or manual cycle authoring; each keeps the explicit path in section 4.3;
- no silent choice among conflicting documents or extracted facts; and
- no second source of executable assertions in narrative steps, item copies, UI-only state, or working-paper code.
