# Dynamic cycle vouching: extraction and linkage contracts

This document locks the two contracts the dynamic cycle design hangs off. It
replaces the authored-pack model in `backend/app/cycle_registry/packs/` with a
per-workspace vocabulary the model induces and an auditor approves. The
deterministic pipeline below extraction — reduction, indexing, linking, role
binding, evaluation, rollup — is retained; only the source of the vocabulary
and of the cycle semantics moves.

The governing division of labour:

| Concern | Owner |
| --- | --- |
| What a document type is called | Closed global list |
| What fields a document type carries | LLM, induced per workspace from samples |
| Which field pairs join two documents | LLM proposes, code measures, auditor approves |
| Which field pairs must agree | LLM proposes, auditor approves |
| Whether a given item passes | Code, deterministically, no model call |

An LLM never decides an item's outcome. It authors rules; approved rules are
applied by code. Every result is replayable from the stored ruleset without a
model.

## Pass structure

Document type cannot come from intake. Intake classification is deliberately
filename-only with no document contents, as a stated privacy boundary
(`backend/app/intake.py`), and its `document_category` is coarser than a
document type. Type therefore requires page text, which makes this three
passes rather than two.

```
classify   page-1 text -> document_type from the closed list      all documents
induce     per type: 2-3 samples -> field schema; reconcile on conflict
extract    all documents, frozen schema as guidance + escape hatch
```

The samples used for induction are re-extracted in the third pass so the head
of each type is not extracted under a different contract from its tail.

## Contract 1: extraction

### 1.1 Classification (pass 1)

```json
{
  "document_id": "doc-0f21",
  "document_type": "purchase_order",
  "document_type_other": null,
  "confidence": "high",
  "rationale": "Header reads Purchase Order and states an order number."
}
```

`document_type` is drawn from the closed global list, which spans every
engagement area rather than one cycle: purchase order, goods receipt, vendor
invoice, payment voucher, salary slip, timesheet, payroll register, bank
confirmation, bank statement, payment instruction, air waybill, bill of lading,
delivery note, customs declaration, credit note, receipt, contract, and so on.
The list is additive and versioned; adding an entry is not a schema change.

`other` is always available and requires `document_type_other`. Without that
escape the Treasury gap reappears at the type level instead of the field level.
An `other` document is extracted and stored but cannot fill a role until its
type is promoted into the list.

### 1.2 Induced schema (pass 2)

One schema per document type per workspace.

```json
{
  "document_type": "vendor_invoice",
  "schema_version": 1,
  "schema_hash": "sha256:...",
  "derived_from": ["doc-0f21", "doc-13c8", "doc-2a70"],
  "reconciled": false,
  "fields": [
    {"name": "invoice_number",   "role": "identifier", "value_type": "identifier",
     "cardinality": "one",  "verbatim": true,  "confidence": "high"},
    {"name": "purchase_order_number", "role": "identifier", "value_type": "identifier",
     "cardinality": "one",  "verbatim": true,  "confidence": "high"},
    {"name": "invoice_date",     "role": "attribute",  "value_type": "date",
     "cardinality": "one",  "verbatim": true,  "confidence": "high"},
    {"name": "total_amount",     "role": "attribute",  "value_type": "number",
     "cardinality": "one",  "verbatim": true,  "confidence": "high"},
    {"name": "vendor_name",      "role": "party",      "value_type": "text",
     "cardinality": "many", "verbatim": true,  "confidence": "high"},
    {"name": "approval",         "role": "control",    "value_type": "text",
     "cardinality": "many", "verbatim": false, "confidence": "medium"}
  ]
}
```

Field `role` is the only part of the schema with downstream meaning:

| role | meaning |
| --- | --- |
| `identifier` | Candidate join key. Normalized as an identifier and indexed. |
| `party` | A named entity. Carries an `entry` ordinal; never a join key by default. |
| `attribute` | An ordinary stated value. Comparable, not joinable. |
| `control` | Evidence that a control step occurred. Eligible for presence assertions. |

`role: identifier` marks a field as *joinable*, not as a join key. Which
identifiers actually link is decided in Contract 2 and approved by a human.
This is the split that `edge_policy` conflates today: in
`cycle_registry/models.py` the linking decision is baked into the same
definition the extraction hash covers, so it can never be revised without
re-extracting. Here it is a separate, later, cheaper decision.

`verbatim: false` marks a value the document does not print — the role a party
plays, the decision a signature block represents. Those are exempt from the
citation requirement, which is otherwise unsatisfiable for them.

**Induction rules.**

- Two or three samples per type; more where the type is high volume. Spread the
  picks across any cheap heterogeneity signal (source folder, filename pattern,
  page count) rather than taking the first two in order.
- Schemas are **unioned**, never intersected. A field present in one sample and
  absent in the other is optional, not a disagreement. Intersecting discards a
  field the corpus really contains.
- Agreement is judged on the fields both samples marked confident, not on set
  equality. Exact equality almost never holds, which would send every type
  through reconciliation and make the fast path dead weight.
- A genuine conflict — same field name, incompatible `value_type` or `role` —
  triggers one reconciliation call whose only job is to choose. It may not
  invent fields absent from both samples.
- Schemas are visible and re-derivable but **not** gated on auditor approval.
  They are descriptive. Reviewer attention is spent on Contract 2, which is
  judgement.

### 1.3 Extracted records (pass 3)

Emitted per chunk, reduced per document by the existing reduction path.

```json
{
  "document_id": "doc-0f21",
  "document_type": "vendor_invoice",
  "schema_ref": {"document_type": "vendor_invoice", "schema_version": 1,
                 "schema_hash": "sha256:..."},
  "records": [
    {
      "record_id": "rec-8ac1",
      "fields": [
        {"name": "invoice_number", "entry": 1, "value": "INV-1042",
         "value_type": "identifier",
         "citation": {"page": 1, "excerpt": "Invoice No. INV-1042"}},
        {"name": "total_amount", "entry": 1, "value": "12,480.00",
         "value_type": "number", "currency": "USD",
         "citation": {"page": 1, "excerpt": "Total Due USD 12,480.00"}}
      ],
      "additional_fields": [
        {"name": "incoterm", "entry": 1, "value": "DAP",
         "value_type": "text",
         "citation": {"page": 1, "excerpt": "Incoterm: DAP"}}
      ]
    }
  ]
}
```

- **`normalized_value` is computed by code, never supplied by the model.** The
  model returns the value as printed; normalization is applied server-side by
  the existing conservative normalizer. This is the same lesson
  `CycleRegistry.bind_reference` already records: asking a model to transcribe a
  derived value adds no evidence and one failure mode.
- Every `verbatim` field requires a citation with page and excerpt. A value
  without one is rejected, not repaired.
- `additional_fields` is the escape hatch for facts the frozen schema has no
  room for. Without it this design reproduces the failure `cycle_registry/
  common.py` already documents: a fact the worker can see and cite but cannot
  place is dropped, or relabelled as an unrelated field.
- One document may reduce to several records. A document that carries no
  transaction record reduces to an empty record list under its schema, which is
  a truthful answer and must not be forced to produce one.

**Escape-rate monitoring.** The share of a type's documents emitting
`additional_fields`, and the frequency of each escaped field name, are computed
deterministically after extraction. A field escaping on a material share of the
type is evidence the induction samples were unrepresentative, and marks the
schema for re-derivation. This is the safety net for small-n induction: it
catches an unrepresentative sample without a human and without a model call.

## Contract 2: linkage proposal

One proposal per cycle. The model authors it from the induced schemas and its
own knowledge of how the documents relate; code measures it against the corpus;
the auditor approves or edits it; only then does it produce results.

```json
{
  "proposal_id": "lnk-4b2e",
  "cycle_label": "Procure to pay",
  "status": "proposed",
  "schema_refs": [
    {"document_type": "purchase_order", "schema_version": 1, "schema_hash": "sha256:..."},
    {"document_type": "vendor_invoice", "schema_version": 2, "schema_hash": "sha256:..."}
  ],

  "roles": [
    {"name": "purchase_order", "document_type": "purchase_order",
     "cardinality": "one", "required": true},
    {"name": "invoice", "document_type": "vendor_invoice",
     "cardinality": "one", "required": true},
    {"name": "goods_receipt", "document_type": "goods_receipt",
     "cardinality": "many", "required": false}
  ],

  "anchor": {"table": "expense_register", "column": "INVOICE_NO",
             "role": "invoice", "field": "invoice_number"},

  "join_keys": [
    {
      "id": "jk_po_number",
      "left":  {"role": "invoice",        "field": "purchase_order_number"},
      "right": {"role": "purchase_order", "field": "invoice_number"},
      "match": "normalized_equal",
      "rationale": "An invoice cites the purchase order it bills against.",
      "measured": {
        "left_documents": 182, "right_documents": 176,
        "matched_pairs": 171, "left_unmatched": 11,
        "fan_out_p50": 1, "fan_out_p95": 1, "fan_out_max": 3
      }
    }
  ],

  "assertions": [
    {
      "id": "as_total_agrees",
      "label": "Invoice total agrees to purchase order total",
      "left":  {"role": "invoice",        "field": "total_amount"},
      "right": {"role": "purchase_order", "field": "total_amount"},
      "operator": "numeric_within",
      "tolerance": {"absolute": 1.0, "percent": 0},
      "rationale": "The amount billed must be the amount ordered.",
      "measured": {"evaluable_items": 168, "unevaluable_items": 3}
    },
    {
      "id": "as_approval_present",
      "label": "Purchase order carries an approval",
      "left":  {"role": "purchase_order", "field": "approval"},
      "right": null,
      "operator": "present",
      "tolerance": null,
      "rationale": "Approval evidences that the authorization control operated."
    }
  ],

  "ruleset_hash": "sha256:...",
  "approved_by": null,
  "approved_at": null
}
```

### Why roles are named separately from document types

Roles are the addressing scheme; `document_type` only constrains what may fill
a role. Keeping them distinct allows two roles of the same type in one cycle —
an original and a revised invoice, two counterparty confirmations — which the
current implementation cannot express, because role binding matches on record
kind and raises when a `cardinality: one` role matches more than one record
(`backend/app/cycle_vouching.py`). Join keys and assertions address roles, never
document types.

### Join keys are not assertions

A join key builds the graph; an assertion tests it. Collapsing them is the one
structural mistake available here, because `invoice.amount == po.amount` reads
like a single statement while being two: *which* purchase order, and *does it
agree*. Join keys are applied by code across the whole corpus to build the
bounded identifier graph the existing BFS already walks. Assertions are
evaluated only on bound roles, only for sampled items.

### The `measured` block

Code computes `measured` before the auditor sees the proposal. It is not
model-supplied and is recomputed whenever the corpus or schemas change.

For a join key, fan-out is the load-bearing statistic. A key whose `fan_out_p95`
is 1 is a transaction identifier. A key whose `fan_out_p95` is in the hundreds
is an entity identifier — a vendor number, a bank account, a cost centre — and
approving it would collapse every unrelated transaction into one cluster. That
is the single most dangerous error in this design, and this is what makes it
observable to a reviewer who does not have to reason about it abstractly.

This replaces `edge_policy` with a measurement. Today the same judgement is
hardcoded per pack and is invisible until the graph is wrong.

`left_unmatched` is the coverage signal: a join key matching 171 of 182
invoices is sound; one matching 40 means the field is inconsistently named or
inconsistently present, and points back at the schema rather than the rule.

### Review lifecycle

```
proposed  ->  edited  ->  approved  ->  effective
                  \
                   ->  rejected
```

The auditor may edit any rule, delete rules, and add rules by hand. Approval
stamps `approved_by`, `approved_at`, and freezes `ruleset_hash` over the roles,
anchor, join keys, and assertions. Only an approved ruleset may produce results.

Review is at rule level, never at link level. A corpus yields thousands of
links and roughly ten rules; the rules are what an auditor can meaningfully sign
off, and rule-level approval is what makes the review step tractable at all.

## Provenance and fail-closed behaviour

`ruleset_hash` replaces the per-pack `definition_hash` as the provenance stamp,
and `schema_hash` replaces it for extraction. The split is deliberate: today a
single pack hash covers both the extraction vocabulary and the linking policy
(`edge_policy` sits inside `IdentifierKindDefinition.identity()` and therefore
inside the pack hash), so revising a linking decision invalidates every stored
extraction in the workspace. Under these contracts:

| Change | Consequence |
| --- | --- |
| Schema re-derived for one type | Re-extract that type only. Other types unaffected. |
| Join key or assertion edited | Re-link and re-evaluate. No model calls. |
| Document type list extended | Nothing invalidated; additive. |
| Ruleset approved anew | Prior results retained against their own `ruleset_hash`. |

Failing closed, in every case rather than reinterpreting under current
definitions:

- An extracted record whose `schema_hash` no longer matches the current schema
  for its type is excluded from evidence with a stated reason, not reinterpreted.
  Exclusion is per document; one stale analysis must not fail the workspace.
- An approved rule naming a field absent from the current schema is unusable and
  reports as such. It is never silently matched to a similar field.
- A result whose `ruleset_hash` differs from the effective ruleset is stale and
  is re-evaluated, never displayed as current.
- An unapproved proposal produces no results at all.

## Retained and replaced

Retained essentially unchanged, because they are already vocabulary-agnostic:

- Value normalization and the conservative identifier normalizer
- Fragment reduction into document-local records
- The identifier index and bounded BFS linking, rekeyed on approved join keys
- The six operators (`equal_exact`, `equal_normalized`, `numeric_within`,
  `date_on_or_before`, `date_within`, `present`)
- Assertion evaluation. `_fact_entries` already matches facts by plain string
  selectors off the record and never consults the registry.
- Role binding, item materialization, result rollup, grid projection

Replaced:

- `cycle_registry/packs/procure_to_pay.py`, `packs/payroll.py` — deleted
- Comparison recipes as code — the model proposes per workspace instead
- `DATE_LIFECYCLE` — an authoring-time guard only, consulted in exactly one
  place and never during evaluation. Direction is stated by the auditor when
  approving a date assertion.
- Per-pack `definition_hash` — replaced by `schema_hash` and `ruleset_hash`
- `edge_policy` — replaced by approved join keys with measured fan-out

## Resolved decisions

**One cycle, one effective ruleset per workspace.** A workspace holds at most one
approved ruleset, and it describes one cycle. This matches what item
materialization and rollup already assume, so nothing there needs to change.

Accepted cost: an engagement auditing two cycles — payroll and procure-to-pay in
the same workspace — can vouch only one of them. The other's documents are
extracted and stored but never bound to roles, and readiness must report that as
a degradation rather than passing silently. That reporting is the same
`_unvouched` path described under the capability graph, and it is what makes the
constraint visible rather than mysterious.

Storage still addresses rulesets by id (`CycleRulesets/<ruleset_id>.json`) rather
than collapsing to a single `ruleset.json`. The constraint is enforced at
approval — approving one supersedes the previous — not baked into the layout, so
lifting it later is a validation change rather than a migration. Superseded
rulesets are retained; results keep their own `ruleset_hash` and stay readable
against the ruleset that produced them.

**The document-type list is global.** It ships with the code as a single
versioned module, identical across deployments. Per-installation lists would
drift and would make an id mean different things in two places, which the
permanence rule cannot tolerate.

**An `other` document is retyped by the auditor, and that type is then real.**
See the section below, which this decision requires.

## Retyping and workspace-local types

Classification assigns `other` when no listed type fits. The auditor may then
retype that document to anything relevant — an existing entry from the global
list, or a new name they coin. The assigned type is not a label: it takes full
part in induction, roles, join keys, and assertions exactly as a shipped type
does.

**A coined type is workspace-local and prefixed `local.`** — `local.letter_of_
indemnity`. This is not the area prefix rejected under the design rules; that
prefix encoded a cycle, which a document does not belong to. This one encodes
*provenance*: who authored the id. It exists so a workspace's coined
`letter_of_indemnity` can never be silently conflated with a differently-meant
global entry added later, and so a listing can show at a glance which vocabulary
an engagement invented for itself.

**Retyping one document extends the workspace's effective type list, then
reclassifies the rest.** Retyping a single document and leaving forty similar
ones in `other` would fragment the corpus and defeat induction, which needs
several documents of a type. So retyping is two steps: the coined type joins the
workspace's effective list, and the classification pass reruns over the
*remaining* `other` documents with the extended list. Classification is page-1
text only, so this is cheap to repeat.

**A rerun never overwrites an auditor assignment.** Every document's type carries
`assigned_by: "model" | "auditor"`. Reclassification may change only
model-assigned types. An auditor assignment is a decision on the record and is
overwritten by nothing but another auditor assignment.

```json
{
  "document_id": "doc-3311",
  "document_type": "local.letter_of_indemnity",
  "document_type_other": null,
  "assigned_by": "auditor",
  "assigned_at": "2026-08-29T10:14:22Z",
  "previous_document_type": "other"
}
```

**Induction on a local type is unchanged, with one allowance.** Where only a
single document carries the type, the two-sample agreement check cannot run. Induce
from the one document, mark the schema `low_confidence`, and rely on the
escape-rate metric to catch it once more documents arrive. Do not block: a
one-off document with a real schema is still better evidence than an unclassified
one.

**Placement.** Retyping sits between classification and induction — before
extraction, which is the expensive pass, so a correction costs a page-1 reclassify
rather than a full re-extraction. It is **not** a blocking gate. An auditor who
never reviews the `other` bucket gets a working engagement in which those
documents cannot fill roles, and readiness says so.

**Promotion to the global list is out of band.** A `local.` type that proves
general is promoted by editing the shipped module in a later release. Promotion
never rewrites stored ids: a workspace that coined `local.letter_of_indemnity`
keeps it, and the global `letter_of_indemnity` is a distinct id. Reconciling the
two, if ever wanted, is an explicit migration, not an implicit rename.

**Recommended extension, not yet decided:** the same mechanism obviously
generalizes to correcting a *wrong* classification — a document the model called
`delivery_note` that is really a `goods_receipt`. The storage shape and the
`assigned_by` rule already support it and no new mechanism is needed. It is
called out separately because it widens the surface from "the `other` bucket" to
"every document", which is a UX decision rather than a contract one.

---

# Implementation plan

The contracts above pin data shapes only. This part covers what has to change to
carry them: storage, capability graph, workers, API, the RCM coupling, frontend,
migration, and tests.

## Blast radius

Non-test backend modules referencing the registry, packs, or cycle vouching:

```
agent/actions.py                  agent/executors/planning.py
agent/action_tools.py             agent/executors/tests.py
agent/audit_execution.py          agent/prompts.py
agent/capabilities/doc_tests.py   agent/workers/documents.py
agent/capabilities/tests.py       agent/workers/planning.py
agent/context/adapters.py         agent/workers/tests.py
agent/documents_execution.py      cycle_registry/*            (deleted/replaced)
agent/executors/documents.py      cycle_vouching.py
agent/executors/fieldwork.py      doc_tests.py
document_analysis.py              findings.py
rcm_execution.py                  routes/doc_test_routes.py
routes/document_routes.py         working_papers.py
workspaces.py
```

Frontend: 16 non-test files, including `types.ts`, `CycleAssertionDialog.vue`,
`CycleVouchGrid.vue`, `cycleGridState.ts`, `DocTestCreateDialog.vue`,
`RcmControlAttributesEditor.vue`, and `DocumentsTab.vue`.

## Storage layout

Follows the existing workspace conventions (`Planning/context.json`,
`Analyses/.results/`, `KnowledgePacks/`), written through `write_json_atomic`
and covered by `workspace_transactions.py`.

```
<workspace>/
  DocumentSchemas/
    <document_type>.json          current schema + version history
    .index.json                   type -> {schema_version, schema_hash, updated}
    .local_types.json             types this auditor coined for the engagement
  CycleRulesets/
    <ruleset_id>.json             proposal, edits, approval, ruleset_hash
    .index.json
```

Written directly with `write_json_atomic`, the way `methodology.py` and
`document_analysis.py` write their side stores — **not** through
`workspace_transactions.prepare_linked_write`, and not as artifact collections
on the `Workspace` object. Linked writes exist for a sidecar that must share a
workspace revision with an artifact mutation; saving a schema accompanies no
such mutation. The one place that will need a linked write is Phase 2a, where
classification stamps `document_type` onto the document artifact and any coined
type must land in the same revision.

The workspace's coined vocabulary lives beside the schemas because it *is*
document-type vocabulary: `effective_type_ids()` is the global list plus these.

Schemas are workspace-scoped, not owner-scoped. A schema is a description of
*this engagement's* documents; reusing one across engagements would reintroduce
exactly the staleness the pack model suffers from. Cross-engagement reuse, if
wanted later, is a copy-on-create, not a shared reference.

`.index.json` mirrors the existing directory-index pattern
(`workspaces.py:340`) so listing does not open every file.

## Capability graph

Three passes and one human gate become capabilities, so the workflow blocks on
them rather than discovering the gap at execution time. New and changed:

| Capability | Readiness | Units |
| --- | --- | --- |
| `documents.types_classified` *(new)* | every analyzable document has a `document_type`; `other` documents are reported, not blocking | one per document, page-1 text only; model-assigned types only on rerun |
| `documents.schemas_induced` *(new)* | every present type has a current schema | one per document type |

Both `documents.types_classified` and `documents.schemas_induced` run over the
whole document set, not the planning-scoped subset, and both become dependencies
of `planning.rcm_ready`. See *The RCM coupling*.

| `documents.analysis_generated` *(changed)* | now also requires a current schema for the document's type | per chunk, as today |
| `tests.cycle_ruleset_proposed` *(new)* | a proposal exists covering the required types | one per cycle |
| `tests.cycle_ruleset_approved` *(new, human gate)* | an approved ruleset exists with a current `ruleset_hash` | none — auditor action only |

The approval gate must be a real capability with readiness, never an executor
step. The codebase already keeps this separation for dispositions — a separate
auditor-only binder that never signs off — and rule approval belongs in the same
category. An agent must not be able to approve its own linkage rules.

`capabilities/tests.py::_unvouched` currently iterates `DEFAULT_REGISTRY` packs
to detect an engagement that extracted transaction evidence and then never
tie-matched it. It is rewritten against document types, and gains a second case:
evidence extracted with no proposal, and a proposal never approved. Both are
degradations that must be reported rather than passing silently — which is the
current Treasury failure.

## Workers

| Worker | Change |
| --- | --- |
| `workers/documents.py` `CLASSIFY_SYSTEM` *(new)* | page-1 text -> one label from the closed type list + confidence |
| `workers/documents.py` `INDUCE_SYSTEM` *(new)* | 2-3 sample extractions -> field schema; separate reconcile prompt for conflicts |
| `workers/documents.py` `VOUCHER_SYSTEM` *(rewritten)* | schema-guided extraction; `_pack_descriptor`/`_VOUCHER_REGISTRY_DESCRIPTORS` replaced by a schema descriptor; response schema enumerates the type's fields plus `additional_fields` |
| `workers/tests.py` `LINKAGE_SYSTEM` *(new)* | schemas + type list -> roles, join keys, assertions with rationale |
| `workers/planning.py` | RCM control-attribute prompt drops registry selectors (see below) |
| `agent/prompts.py::comparison_recipe_catalog` | deleted; recipes are no longer a code catalog |

The schema descriptor keeps the property `_pack_descriptor` established: the
schema text goes into the hashed prompt, so a schema change moves the execution
identity and no proposal built against an old schema can be reused under a new
one. That interlock is retained exactly; only its content changes.

`bind_reference`'s lesson carries over — the response supplies the document type
and field values; `schema_hash`, `schema_version`, and every `normalized_value`
are bound server-side.

## API surface

New:

```
GET    /workspaces/{id}/document-schemas
GET    /workspaces/{id}/document-schemas/{document_type}
POST   /workspaces/{id}/document-schemas/{document_type}/rederive
PATCH  /workspaces/{id}/documents/{document_id}/type      auditor retype
POST   /workspaces/{id}/documents/reclassify              rerun over remaining `other`
GET    /workspaces/{id}/document-types                    global list + workspace `local.` types
GET    /workspaces/{id}/cycle-rulesets
POST   /workspaces/{id}/cycle-rulesets                     propose
PATCH  /workspaces/{id}/documents/{document_id}/type      auditor retype
POST   /workspaces/{id}/documents/reclassify              rerun over remaining `other`
GET    /workspaces/{id}/document-types                    global list + workspace `local.` types
GET    /workspaces/{id}/cycle-rulesets/{ruleset_id}
PATCH  /workspaces/{id}/cycle-rulesets/{ruleset_id}        auditor edits
POST   /workspaces/{id}/cycle-rulesets/{ruleset_id}/measure   recompute measured
POST   /workspaces/{id}/cycle-rulesets/{ruleset_id}/approve
```

Changed:

- `POST /doc-tests/build/cycle-vouch` takes `ruleset_id` instead of a registry
  reference and role list.
- `GET /doc-tests/cycle-vouch/candidates` keeps `generate_cycle_candidates` and
  `infer_cycle_mappings`, rekeyed on approved join keys rather than pack
  identifier kinds.
- `GET /doc-tests/meta` stops serving pack descriptors and serves the type list
  plus current schemas.

`PATCH /doc-tests/{test_id}/assertions` and the existing SHA-guarded mutation
path are retained; assertion editing on a built test is unchanged.

## The RCM coupling — resolved by ordering

An earlier draft of this plan asserted that planning runs before documents are
analyzed, and built a trilemma on it. That premise is wrong. The authoritative
audit graph in `backend/app/agent/workflows/audit.py` declares:

```
planning.context_ready  <- ("documents.analysis_generated",)
planning.apm_ready      <- ("planning.context_ready",)
planning.rcm_ready      <- ("planning.apm_ready",)
tests.specified         <- ("planning.rcm_ready",)
```

Document analysis is a **dependency** of planning, not a successor. The RCM is
generated after documents have been read.

### The real constraint is scope, not order

`resolve_document_scope` (`capabilities/documents.py`) branches on
`document_scope_mode`. The standalone document workflow defaults to every
imported document including vouchers. The **audit** workflow uses
`"planning"` mode, which selects only `_planning_relevant` documents and caps
them at `MAX_SCOPE_DOCUMENTS = 12`. `_planning_relevant` reads
`PLANNING_DOCUMENT_CATEGORIES`, which is deliberately disjoint from
`VOUCHER_DOCUMENT_CATEGORIES` — a voucher is never planning material, and the
disjointness exists so a voucher analysis can never enter a planning prompt.

So in an audit run, `documents.analysis_generated` can be satisfied having
analyzed **no voucher documents at all**. The edge exists; the voucher schemas
do not. That is the actual problem, and it is much smaller than a mis-ordered
graph.

### Resolution: classify and induce before the RCM, extract as today

Classification and induction are cheap in a way full extraction is not.
Classification reads page-1 text only. Induction reads two or three samples per
document type. Neither requires extracting the corpus. So the prerequisite the
RCM needs is affordable to add:

```
planning.rcm_ready  <- ("planning.apm_ready",
                        "documents.types_classified",
                        "documents.schemas_induced")
```

Both new capabilities run over the **whole** document set rather than the
planning-scoped subset, which is what makes voucher schemas available at RCM
time. `documents.analysis_generated` — the expensive extraction pass — keeps its
current scope and position; nothing about it changes.

The engagement order is preserved. This is *schema*-first, not ruleset-first: the
auditor still plans against documents they have only classified, not against a
ruleset that does not exist yet.

### What this buys

RCM control attributes keep **selector-exact** coverage, exactly as today. A
`transaction_cycle` attribute's `required_comparisons` name `{document_type,
field}` pairs validated against current schemas, in place of today's
`{record_kind, group.kind}` validated against a pack. The existing coverage
contract is unchanged in kind: a generated cycle test must cover every
comparison attached to each `requirement_ref`, and a selector the schemas cannot
express fails closed rather than being swapped for a related one.

No weakening to intent-level matching is needed. The earlier recommendation to
that effect is withdrawn.

`validate_control_attributes`, `_validate_control_attribute`,
`required_comparisons_for`, and `unanswerable_cycle_requirements`
(`cycle_vouching.py`) are retained and re-pointed from the registry to the
workspace schemas. `unanswerable_cycle_requirements` already covers the
staleness case: a schema re-derived after the RCM named one of its fields
leaves that comparison unanswerable, which is reported rather than silently
dropped.

### Consequence for `local.` types

Because retyping precedes induction and induction now precedes the RCM, a
workspace-local type coined by an auditor **is** available when the RCM is
written, and may be named in a `required_comparisons` entry like any other. The
open point raised against this is closed.

`rcm_execution.py`, `workers/planning.py`, `executors/planning.py`, and
`RcmControlAttributesEditor.vue` still change — they move from registry
selectors to schema fields — but the shape of what they express is the same.

## Frontend

New surface — the ruleset review screen — is the substantial piece, and it has
no analogue today:

- Roles and anchor, editable
- Join keys with their `measured` block. Fan-out is the primary display, not a
  detail: a reviewer approves a join key by reading its fan-out distribution and
  unmatched count. Surface `fan_out_p95` and `left_unmatched` at rule level.
- Assertions with operator, tolerance, rationale, and evaluable count
- Add / edit / delete on every rule, then a single approve action

Changed:

- `types.ts` — `KnowledgePack` untouched; registry types replaced by
  `DocumentSchema`, `CycleRuleset`, `JoinKey`, `RulesetAssertion`
- `CycleAssertionDialog.vue` — authors against schema fields, not pack
  descriptors
- `DocTestCreateDialog.vue` — cycle creation selects an approved ruleset
- `CycleVouchGrid.vue`, `cycleGridState.ts`, `DocTestItemDetail.vue` — role and
  field labels come from the ruleset and schemas
- `DocumentsTab.vue` — shows document type, schema, and escape-rate per type;
  hosts the `other` bucket review where an auditor retypes a document, coins a
  `local.` type, and triggers the reclassify-remaining rerun

## Migration

Existing workspaces hold voucher analyses stamped with a pack `definition_hash`
that no longer resolves.

- Legacy analyses remain **readable for display** but are never usable as cycle
  evidence. `document_analysis.py` already excludes per document with a stated
  reason rather than failing the workspace; add `legacy_pack_analysis` as an
  exclusion reason so the gap is visible instead of silent.
- Vouching a legacy workspace requires re-running classification, induction, and
  extraction. That is a real cost and must be an explicit, offered action rather
  than something that happens on load.
- No dual-run. Keeping both vocabularies alive doubles the validation surface
  for a transitional period that only ends when every workspace is re-extracted.

## Tests

- `backend/tests/fixtures/` procure-to-pay and payroll pack fixtures are replaced
  by fixture **workspaces** carrying induced schemas and an approved ruleset, so
  the deterministic chain is exercised end to end without model calls.
- Retain a treasury fixture specifically. The current design's failure is that
  treasury degrades silently; a fixture that would have caught it belongs in the
  suite permanently.
- Property test worth having: for a corpus and a join key, fan-out measured by
  the ruleset endpoint equals fan-out observed by the linker. If those two ever
  disagree, an auditor approved a rule on numbers the engine does not honour.
- `test_workflow_v2.py`, `test_planning.py`, and `test_agent_context_adapters.py`
  all construct registry references directly and need reworking.

## Sequencing

Dependency-ordered. Each phase leaves the tree working.

| Phase | Content | Gate |
| --- | --- | --- |
| 0 ✅ | Global document-type list frozen as `backend/app/document_types.py` (Appendix A) | 80 entries, self-validating on import |
| 1 ✅ | Storage: `document_schemas.py`, `cycle_rulesets.py`, indexes, coined types | 55 round-trip tests; full suite green |
| 2 ✅ | Classification pass: worker, capability, readiness, executor, type stored per document | Types present on a fixture workspace; full suite green |
| 2a ✅ | Retyping API, catalog endpoint, scoped re-examination run, `other` review dialog | An `other` document retyped through the UI; both suites green |
| 3 ✅ | Induction pass: sampling, union, reconcile, schema freeze, escape-rate metric | Schemas induced end to end on a fixture workspace; full suite green |
| 4 ✅ | Schema-guided extraction alongside the pack profile; `schema_hash` provenance; escape hatch | Records extracted against schemas end to end |
| 5 ✅ | Linkage proposal worker + `cycle_measurement` (fan-out, coverage, silence) | Proposals measured against the corpus |
| 6 ◐ | Ruleset review API and review screen done; agent-driven proposal capability outstanding | Auditor can approve a ruleset |
| 7 ✅ | `cycle_linking.py`: linking, role binding, evaluation, and the test lifecycle on approved rulesets | Cycle test builds, materializes, evaluates, and files end to end |
| 8 ✅ | RCM control attributes addressed by schema field; the two graph edges; coverage gate; authoring turn and editor re-pointed | Selector-exact coverage restored |
| 9 ✅ | `cycle_registry/` deleted, with the voucher profile, the recipes, and every branch that routed to them | Registry module removed |

Phases 1-4 are independent of 5-6 and can proceed in parallel with the ruleset
work once the contracts are fixed. Phase 9 must be last: the packs remain the
only working vocabulary until phase 7 proves the replacement.

## Built so far

| Module | Contents |
| --- | --- |
| `backend/app/document_types.py` | The closed global catalog: 79 classifiable types plus `other`, across nine areas. Validates itself on import. Owns `local.` coining, `validate`, and `prompt_catalog`. |
| `backend/app/document_schemas.py` | Induced schemas per workspace: field validation, meaning-only hashing, version-on-change, staleness (`is_current`), coined-type registry. |
| `backend/app/cycle_rulesets.py` | Roles, join keys, assertions: validation, reachability, approval and supersession, measurement that cannot move the hash. |
| `backend/tests/test_document_schemas.py` | 30 tests |
| `backend/app/document_classification.py` | Type assignment per document: `assigned_by` provenance, retyping, coined types, catalog-gated reclassification, the page-1 text the classifier reads. |
| `backend/tests/test_cycle_rulesets.py` | 25 tests |
| `backend/tests/test_document_classification.py` | 23 tests |
| `backend/app/routes/document_routes.py` | `GET /documents/types`, `GET /documents/unidentified`, `PATCH /documents/{id}/type`, `POST /documents/reclassify`. |
| `frontend/src/components/documents/DocumentTypeReview.vue` | The `other` bucket: retype to a listed type or coin one, then re-examine the rest. |
| `backend/tests/test_document_type_routes.py` | 13 tests |
| `frontend/.../DocumentTypeReview.test.ts` | 7 tests |
| `document_schemas.union_fields` / `induce` | Union never intersect, conflict detection, freeze, `low_confidence`. |
| `document_schemas.escape_rate` | Deterministic, model-free measure of how often extraction steps outside a schema. |
| `document_classification.sample_for_induction` | Stratified, deterministic sampling; 2 samples, 3 for a high-volume type. |
| `workers/documents.py` schema workers | `documents.schema_sample` reads one document's fields; `documents.schema_reconcile` settles a disputed one. |
| `backend/tests/test_schema_induction.py` | 21 tests |
| `backend/tests/test_workflow_schema_induction.py` | 6 end-to-end tests |
| `backend/tests/test_schema_escape_rate.py` | 7 tests |
| `workers/documents.py` `documents.analysis_structured` | Schema-guided extraction: fields named from the frozen schema, `additional_fields` escape hatch, citations required for anything the record states. |
| `backend/app/cycle_measurement.py` | Fan-out, coverage, and silence, computed from stored extractions. Never model-supplied. |
| `workers/tests.py` `tests.cycle_linkage` | Proposes roles, join keys, and assertions from the engagement's schemas. |
| `routes/doc_test_routes.py` | `/cycle-rulesets` propose, read, edit, measure, approve, reject. |
| `frontend/.../CycleRulesetReview.vue` | The review screen: rules with their rationale, fan-out per join key, concerns raised. |
| `backend/tests/test_structured_extraction.py` | 17 tests |
| `backend/tests/test_cycle_measurement.py` | 9 tests |
| `backend/app/cycle_linking.py` | The ruleset-native engine: structured evidence with content-addressed record identity, bounded breadth-first traversal of approved join keys, role binding, schema-typed operand resolution, and the build/materialize/evaluate lifecycle. |
| `cycle_vouching.ruleset_backed` | The single routing predicate. Every cycle entry point asks it; phase 9 deletes the other branch. |
| `backend/tests/test_cycle_linking.py` | 14 tests |
| `backend/tests/test_cycle_linking_end_to_end.py` | 20 tests |
| `backend/tests/test_cycle_linking_routes.py` | 4 tests |
| `frontend/.../DocTestCreateDialog.ruleset.test.ts` | 5 tests |
| `cycle_linking` control attributes | `required_comparisons` over `{document_type, field}`, validated against current schemas; `uncovered_comparisons` and `unanswerable_comparisons`; the degradation notes the stage reports. |
| `workers/planning.py` `RCM_SCHEMA_EVIDENCE_SYSTEM` | The authoring turn, shown this engagement's fields and asked what must agree. |
| `routes/document_routes.py` | `GET /documents/schemas` — the catalog a requirement may be written against. |
| `frontend/.../RcmControlAttributesEditor.vue` | Authors comparisons over document types and fields; the pack surface survives only where there are no schemas. |
| `backend/tests/test_rcm_schema_attributes.py` | 24 tests |
| `frontend/.../RcmControlAttributesEditor.schema.test.ts` | 6 tests |
| `backend/tests/test_cycle_vouching_retained.py` | 20 tests — the vocabulary-agnostic half, carried over unaltered |
| `backend/tests/test_cycle_treasury.py` | 5 tests — a cycle nobody shipped a pack for, kept permanently |
| `cycle_measurement.structured_records` | Reports `legacy_pack_analysis` and `stale_schema_reference` rather than skipping a document in silence. |
| `backend/tests/test_cycle_linkage_worker.py` | 13 tests |
| `backend/tests/test_cycle_ruleset_routes.py` | 11 tests |
| `frontend/.../CycleRulesetReview.test.ts` | 7 tests |

Invariants these encode, each with a test behind it:

- A join key may only address **identifier**-role fields. Joining on an amount
  would fuse unrelated transactions.
- An identifier field must have `value_type: identifier`, so a join key never
  compares `0042` against `42` under numeric rules.
- Every role must be **reachable** from the anchor by join keys, or it silently
  never binds.
- Re-inducing a schema to the same fields does **not** bump its version — the
  version is what extractions are stamped with.
- The schema hash covers meaning only; `derived_from`, timestamps, and field
  order do not move it.
- The ruleset hash covers rules only; `measured` is excluded, so a fan-out that
  shifted because documents arrived does not invalidate an approval.
- An approved ruleset is immutable, and approving one supersedes the previous.
- Approval revalidates against current schemas, names any field that has
  vanished, and refreshes `schema_refs` when a schema merely grew.

## Corrections implementation forced

Three things this plan got wrong, found by building it. Each is now what the
code does.

**Assignments live in `Documents/.types` sidecars, not on the document entry.**
The plan said classification "stamps `document_type` onto the document artifact"
and would need a linked write to share a revision. Both were wrong, for a reason
worth recording: capability readiness runs against whatever workspace handle its
caller holds, and that handle is routinely several revisions behind by the time a
stage is scheduled. A lazily hydrated artifact collection read from it reported a
document unclassified moments after it had been classified, so the capability
never settled and re-ran on every run — observed as a workspace handle at
revision 2 while disk was at 5. Every existing document capability reads sidecars
for exactly this reason. The sidecar also removes the awkwardness the plan
introduced: the document artifact is now untouched, so the parent guard means
what it means everywhere else — the source document was not replaced underneath
the commit — and the reconciler takes the same shape as the analysis one.

**The capability is sequential, not parallel.** Documents classify independently
of one another, which made the parallel barrier look right; a composition test
rejected it. The barrier is a claim about *commits*, not inputs, and every
assignment commits. Independence of inputs is not independence of commits.

**The reclassification sweep is gated on the catalog having changed.** The plan
described retyping as triggering a rerun "over the remaining `other` documents"
without saying what stops it. Nothing did, so every run re-classified the whole
bucket and the capability could never be reused. Each assignment now records the
`catalog_sha1` it was chosen against, and an `other` is revisited only when the
current catalog differs — which happens exactly when an auditor coins a type.

## Phase 2a notes

`POST /documents/reclassify` requests **only** `documents.types_classified`, not
the full document outcome set. Re-examining what a document *is* must not re-run
its analysis: the catalog has no bearing on what the map worker extracted. It
also refuses rather than starting a run that would do nothing, when every
`other` was already chosen from the current catalog.

`document_types.DocumentTypeError` is registered on the shared user-error
handler in `main.py` alongside `QueryError` and `SettingsError`, rather than
being made a `WorkspaceError`. An unknown or shadowing type id is the caller's
mistake, and keeping the exception out of the workspace hierarchy lets
`document_types` stay a leaf module the catalog can be read from without pulling
in workspace storage.

`document_analysis.inventory()` now carries each document's classification, read
from its sidecar rather than the document entry — so a listing taken from a
workspace handle a few revisions behind still shows the type actually assigned,
the same reason the store moved to sidecars in Phase 2.

The review dialog omits `other` from its picker: retyping a document to what it
already is changes nothing. Coined types are offered above the shipped
catalogue, since an engagement's own vocabulary is what an auditor is most
likely to reach for twice.

## Phase 3 notes

**Induction is two capabilities, not one, and the reason is a scheduler fact
worth writing down.** Units within a stage execute in sorted **id** order, never
declaration order (`workflow.py`, `workflow_runner.py`). A single capability
holding both the sample readings and the freeze that consumes them binds the
freeze *first* — `document_schema:x` sorts before `document_schema_sample:x:y`
because `:` precedes `_` — and reads back nothing. The first build did exactly
that and the freeze ran while its samples were still queued. `schemas_sampled`
→ `schemas_induced` makes the ordering a dependency edge the scheduler honours,
which is the same shape chunk analysis and its reduction already have.

**Agreement costs no model turn.** The freeze binder unions the sample readings
locally and calls `documents.schema_reconcile` only when two of them named one
field as two different things. Agreement is the common case, and it commits
through `commit_local`, so it keeps the same proposal-before-mutation,
reconciliation, and receipt guarantees a model-backed unit gets.

**Conflict is narrowly defined.** Only a differing `value_type` or `role` is a
conflict: those change what a field *is*. Cardinality merges (`many` wins — one
sample seeing two proves the type can carry two), `verbatim` merges (interpretive
wins, because demanding a quote for a value the document never prints is
unsatisfiable), and confidence takes the strongest reading. Everything else is
additive, per the union rule.

**The reconciliation result is re-unioned rather than patched.** Applying the
chosen readings back onto what each sample said and re-running the union keeps
one code path deciding what a schema is, so a reconciled schema is exactly the
schema those samples would have produced had they agreed.

**`input_payload` does not reach a binder.** The scheduler stores only `kind`,
`title`, `parent_refs`, and `input_sha1` on the unit. The freeze unit therefore
recovers both its samples and its document type from the parent refs it was
expanded against — those *are* the sample documents, which makes the recovery
exact rather than a re-derivation that could pick a different sample.

**A schema commits transactionally despite living in a side store.** The
executor goes through `mutate()`: that is what takes the write lock, re-checks
that the sample documents have not been replaced underneath, and publishes a
workspace revision, so the commit is an event rather than an invisible file
write. Its postcondition is the schema's own content hash — `parent_hashes`
projects workspace artifacts and has nothing to say about a side store.

**Escape rate is the real safety net for small-n induction**, and the reason two
samples is enough. Agreement between two documents says little about a
heterogeneous corpus, and is itself biased toward sparse schemas — two documents
agree most easily when both state little. What catches an unrepresentative
sample is not a better agreement check but the rate at which real extraction
finds facts the frozen schema cannot hold. It is deterministic, model-free, and
measures *documents* needing a field rather than occurrences, since breadth is
what distinguishes a missing type-level field from one document being unusual.
It has nothing to measure until Phase 4 emits `additional_fields`.

## Phases 4-6 notes

**Extraction routes by schema, not by flag day.** `analysis_profile` returns
`structured` wherever the engagement has induced a schema for the document's
type, and `voucher` otherwise. The pack profile stays live for anything without
a schema, which is what keeps a workspace analysable before — or without —
induction, and what lets phases 4-6 land while the registry still runs the
cycle. Phase 9 removes the fallback once Phase 7 has replaced what it feeds.

Corrected after the first real run: routing by schema **alone** was wrong, and
the intake category is the second half of the gate. See *Phase 9 notes — the
category gate*.

**The structured branch has to precede the single-chunk shortcut in reduction.**
The reduction takes a shortcut when a document produced exactly one chunk,
passing the raw chunk proposal straight through. A structured analysis needs its
schema stamp and its locally rendered summary whether it came from one chunk or
ten, so its branch runs first. This cost an hour to find and is invisible in any
multi-chunk test.

**The schema stamp is bound server-side at reduction.** The worker never echoes
it back. The interlock that proves an extraction used a particular schema is
stronger than an echoed stamp anyway: the schema descriptor travels in the unit
input, so a re-derived schema moves the unit's input hash and the chunks
re-expand rather than being reduced under fields they never saw.

**An empty `records` array is a complete answer.** A page of prose inside a
transaction document states no record, and treating that as a coverage gap would
report a hole the extraction did not actually have.

**Measurement is recomputed on read, not frozen into the rules.** A fan-out that
was true a hundred documents ago is not a fact worth approving, so the review
endpoints measure live. `set_measured` still exists for storing a snapshot, and
still cannot move the ruleset hash.

**`cycle_measurement.concerns` states observations, not refusals.** An
engagement can legitimately have a one-to-many cycle, and it is the auditor's
call. What must not happen is the number going unnoticed — so a join key whose
values reach many records, one matching less than half of what states it, and an
assertion nothing can evaluate are each surfaced with the reason in words.

**A record that never states a join key is not counted against it.** It is a
document the rule has nothing to say about, not evidence the rule is wrong.
Counting it as unmatched would make every partially-applicable rule look broken.

## Phase 7 notes

**The engine is a new module, not a rewrite of the old one.** `cycle_linking.py`
holds the ruleset-native half; `cycle_vouching.py` keeps the pack half and routes
to it on `definition.ruleset_id`. The alternative — translating a ruleset into
the shape the registry engine already consumes — means synthesising a pack, which
is the thing being deleted. Routing on the payload rather than on the workspace
matters: a workspace can hold an approved ruleset while an agent still proposes
pack definitions, and building the wrong one would silently discard the
definition the caller handed over.

**The retained half is imported, not copied.** The six operators, the
deterministic sampler, the citation catalogue, the evaluation rollup and the
cross-item reuse pass are `cycle_vouching`'s, aliased at the top of
`cycle_linking` with the reason recorded. They are private there only because
nothing outside needed them before. Copying them would have produced two
implementations of the comparison an auditor's stored verdict was made under,
which is exactly the drift a `result_sha1` cannot detect.

**Records, not documents, are the unit of traversal.** A voucher stating three
invoice lines is three records. Measurement counted documents and the linker
would have traversed records, so a fan-out an auditor approved could have been
exceeded in the engine on any multi-record document. `cycle_measurement` was
rekeyed onto record identity, and it now honours the join key's `match` mode
rather than always normalizing — so the parity property (`measure_join_key`
agrees with what the linker reaches) actually holds, and there is a test for it.

**A record's id is its content.** `REC-<sha256 of document, index, content>`. A
re-extraction that changes a value produces a different record, so a stored
verdict cannot inherit it by carrying the same id: staleness happens by
construction rather than by a check someone has to remember to run. The
extraction hash stays off the record and on the binding, so re-analysing a
document that produced an identical record does not churn the id.

**Hitting the hop limit keeps the record and stops expanding.** The registry
engine refused the whole traversal. That discards a complete cycle for the sake
of a record beyond it that nothing asked for. Records and edges still refuse,
because those bounds are about the size of the answer rather than its shape.

**A role bound to several records resolves when they agree.** The `many`
cardinality falls out of the existing scalar path: several bindings, one
distinct value, resolved. Where they disagree it is ambiguous, and picking one
would be inventing the answer the documents declined to give. Set-valued
operands — three receipts each carrying a quantity, compared to one order line —
are not in Contract 2 and are the known gap here.

**Assertions are not editable on a ruleset-backed test.** `mutate_cycle_assertions`
refuses and names the cycle rules review. Editing them on the test would produce
rules nobody approved, under a `ruleset_hash` that says otherwise.

**Grid attribution has to hash the rule, not the projection of it.** The grid
reads assertions through a display shape with a `source` discriminator the
ruleset does not have. Hashing that shape marked every current cell stale, so
`_grid_attribution` now takes the hashes the evaluator actually produced.

**The working paper names who approved the rules.** A cycle result is only as
authorised as the rules behind it, so the file carries the ruleset id, its hash,
and the auditor and date of approval, and every binding says which join key
reached it.

**Superseding rules reopens a test rather than invalidating it.** The
`ruleset_hash` pins exactly what ran, so stored results stay readable and stay
attributed. What changes is `status`: a test whose rules are no longer the
effective ones is `review_required` until it is regenerated. A ruleset *edited*
under a test is different — that fails closed, because the rules the test names
no longer exist in the form it names them.

## Phase 8 notes

**The edges bite, and that was worth measuring rather than assuming.** With a
voucher document present, a planning run now classifies it and induces its
schema before the RCM — confirmed by running one and reading the workspace
afterwards. The engagement order is unchanged and the expensive extraction pass
keeps its scope; what moved is that the vocabulary a requirement may address now
exists when the requirement is written.

**One attribute names one vocabulary.** `required_comparisons` puts an attribute
on the schema vocabulary, where `registry` and `comparison_recipes` are the keys
that do not belong; a pack attribute is the mirror image. Mixing is refused
either way round. The tests that used to prove a row *cannot* author comparisons
now prove it cannot author both, which is the invariant that survived the
change — the old one was a consequence of record kinds being decided later.

**Every unexpected key is reported, not the first.** A live row carried
`registry`, `required_record_kinds`, `label` and `operator_tolerance` at once.
Reporting one per pass means a model repairing it needs four turns, and it only
gets one look at each error.

**Validation is shape-only without a workspace and exact with one.** The
response validator has no engagement in hand; the commit does, and that is where
a field no schema states is refused. Hydration deliberately stays shape-only:
running schema lookups on every workspace load would pay disk on every read to
re-report something `unanswerable_cycle_requirements` already covers.

**Coverage is selector-exact, and the two failures are reported apart.** A field
nothing states is an evidence gap; a comparison no approved assertion answers is
a rules gap. They are repaired in different places, so the build refuses with
different messages and the stage reports them as separate notes. Operands may be
written either way round for a symmetric operator, because an equality stated in
the other order is the same requirement — but a different tolerance is a
different requirement, and a neighbouring field is a different test.

**`business_cycle` stays derived for a pack row and stated for a schema row.**
It was a projection of the pack id. A schema-backed row has no pack, so it keeps
the label the auditor gave it; echoing the old value for a pack row instead
would have let a stale cycle survive an attribute change, which is what the
"derived rather than echoed" test exists to catch.

**The catalog travels on the unit input, not in the prompt.** Same interlock as
schema-guided extraction: the prompt hash stays stable while the vocabulary
varies per workspace, and a re-derived schema moves the unit's input hash so the
matrix re-runs rather than leaving a requirement pointing at a field that moved.

## Phase 9 notes

**Deleted, in order, callers before callees.** The voucher analysis profile and
its worker family (~1,250 lines); the fragment reduction and the pack half of
`document_analysis`; the recipes, manifests, candidate generation and pack
authoring in `cycle_vouching` (~4,000 lines); then `cycle_registry/` itself
(2,077 lines). Around 5,000 lines of pack tests went with them. The suite was
green at every step, which is what made a deletion this size safe to attempt.

**`cycle_vouching` is now the vocabulary-agnostic half plus delegations.** Value
normalization, the six operators, the deterministic sampler, the citation
catalogue, the rollup, the grid, and the state words an auditor's dispositions
are recorded in — 6,192 lines down to 1,306. Roughly thirty modules import it by
name; collapsing it in place rather than relocating everything into
`cycle_linking` kept that surface stable while the pack vocabulary went.

**A transaction document with no schema is read as prose.** There is no third
profile to fall back to. It is still analysed, readable and citable, and it is
not cycle evidence — which is the honest description of a document nothing has
induced fields for, and it is what makes re-running classification and induction
an offered action rather than a silent precondition.

**`business_cycle` stopped being derived.** It was a projection of the pack a
transaction-cycle attribute named. With the rules held in the workspace there is
no pack and nothing to project from, so it is the label the matrix chose.

**Two comment/behaviour mismatches surfaced and were corrected.** The
conservative normalizer's docstring claimed `PO-2025/17` and `po 2025/17` were
the same reference; the code — correctly — treats punctuation as significant and
only folds case and whitespace. And `_downgraded_uncontracted` still keyed on
`registry`, so a schema-backed attribute that *had* been contracted was
downgraded to `document_content` on the next pass. Both are the kind of defect
that only shows up when the thing around them is removed.

**The treasury fixture is permanent.** The old design's failure was not that
treasury was unsupported — it was that treasury degraded *silently*: documents
read, no pack claiming them, no cycle test generated, run reports success. There
is still no treasury pack. There is a treasury ruleset, induced from the
engagement's own documents, and a suite that exercises it end to end.

**What a legacy workspace does now.** Its stored analyses stay readable and are
never counted as evidence; `structured_records` reports each one as
`legacy_pack_analysis` rather than skipping it, so the gap is stated instead of
inferred from a count. Vouching one requires re-running classification,
induction and extraction — a real cost, and an explicit action.

**The category gate, restored.** Found on the first real engagement run, not by
the suite. Before this redesign the structured profile was gated on the intake
category: `_voucher_document` asked whether `category` was in
`VOUCHER_DOCUMENT_CATEGORIES`, and nothing else was read under fields. Phase 4
replaced that question with "does this document's type have an induced schema"
and put no gate in front of induction, which expands over every classified type.
The category gate went dead — `VOUCHER_DOCUMENT_CATEGORIES` ended up referenced
nowhere outside `intake.py`.

On a procurement workspace the effect was immediate. Classification was right
about all three planning documents — an approval matrix *is* a
`delegation_of_authority`, minutes *are* `board_minutes` — and induction then
sampled both and invented fields for them.

The cost is not the wasted turns. A structured document's summary is rendered
from its records rather than written: the reduction takes no model turn at all
(`documents_execution.py`, the all-structured branch). The planning and APM
context selectors choose documents by exactly those categories and read the
analysis `summary`. Routing policy material to the structured profile therefore
replaces the narrative planning consumes with a record dump — a silent
degradation of planning input, from a classification that was correct.

Both questions now have to be answered yes:

- `document_classification.transaction_evidence` — classifiable documents whose
  intake category is transaction-level. `types_for_induction` is what induction
  expands over; `types_present` still reports every type the corpus carries,
  because that is a classification fact and reporting it is not the same as
  extracting against it.
- `analysis_profile` asks the category *and* the schema. A policy that shares a
  type with vouchers is not read under their fields.

Type says what a document **is**; category says whether this engagement holds it
as transaction evidence. Both are true of an approval matrix, and only one of
them was being asked.

The tests that were missing are in `test_document_classification.py` (the store
gate), `test_structured_extraction.py` (the profile pair — same document, same
schema, only the category differs), and `test_workflow_schema_induction.py` (a
voucher alongside an approval matrix and minutes, end to end). All four fail
with the gate removed.

**Two things the same run exposed, found by reading rather than by failing.**

*Schema provenance was positional.* `_bind_schema` recovered the contributing
documents as `sample_ids[:len(proposals)]`. A failed sample leaves no proposal,
so the readings are a *subsequence* of the samples, and that prefix names the
wrong documents the moment anything but the last one fails. It is not only
bookkeeping: `contributing` becomes the schema's `derived_from` **and** the
`expected_parents` guard, so replacing the document a schema was actually read
from would not conflict, while replacing one that failed would. `_schema_samples`
now returns `(document_id, proposal)` pairs.

*The freeze binder tolerates a partial sample set; the scheduler never lets it.*
`_bind_schema` returns `awaiting_confirmation` when no sample of a type could be
read, and unions whatever readings it got. Both paths are unreachable in a
single run: `_fold_stage` returns `failed` for a stage with any failed unit, and
`documents.schemas_induced ← documents.schemas_sampled` is not in
`_PARTIAL_DEPENDENCIES`, so one failed sample blocks **every** freeze unit — then
every chunk unit, then every analysis. The module comment above that map says
every edge in this graph is partial; the map lists three of six. Left open
deliberately: making the sample edge partial is narrow and matches the binder,
but making `analysis_chunks_ready ← schemas_induced` partial would silently
downgrade transaction evidence to prose, which is the failure mode this redesign
exists to remove. See *Still unspecified*.

## Still unspecified

Deliberately left open, and each needs a decision before its phase:

- Retry and repair budgets for the three new workers, and what a failed
  induction does — block the type, or fall back to unguided extraction.
- Whether escape-rate re-derivation is automatic or offered. Automatic
  re-derivation invalidates extractions mid-run.
- Concurrency: a schema re-derived while extraction is in flight. The existing
  execution-identity interlock should cover it, but it is untested for schemas.
- Whether retyping generalizes from the `other` bucket to correcting any
  classification. See *Retyping and workspace-local types*.

---

# Appendix A: the global document-type list (draft)

Phase 0 deliverable. This is the closed enum the classification pass selects
from, and the vocabulary role definitions constrain against.

## Design rules

**Flat namespace, no area prefix.** A document type belongs to the document, not
to a cycle. An air waybill appears in procurement, logistics, and trade finance;
an employment contract is payroll evidence and contract evidence both. Prefixing
ids by area (`p2p.purchase_order`) would rebuild the pack model in the type list.
Ids are globally unique and disambiguated by name where needed — `bank_statement`,
`vendor_statement`, `customer_statement` — and grouped by area only for display.

**Grain: a type is distinct when an auditor would expect a different set of
fields on it, or would give it a different role in a cycle.** Finer than that
fragments the corpus across near-synonyms; coarser than that makes roles
indistinguishable.

**Direction is part of identity.** `vendor_invoice` and `sales_invoice` are
separate types because payable and receivable are different cycles with
different roles, even where the paper looks similar.

**Jurisdictional variants are schema, not type.** A VAT or GST tax invoice is a
`vendor_invoice` or `sales_invoice` whose induced schema happens to carry tax
registration and tax-breakdown fields. Splitting it out invites a
discrimination the classifier cannot reliably make, and the induced-schema
design already absorbs the difference.

**Ids are permanent.** The list is additive. An id is never renamed or reused,
because extractions and approved rulesets store it. Deprecation marks an entry
inactive for new classification; it never rewrites history.

**Type is orthogonal to intake category.** A document may be
`category: contract, document_type: employment_contract`. Intake's category
governs routing, privacy, and whether the engagement holds the document as
transaction evidence at all; document type governs which fields it is read under
and which roles it may fill. Neither derives from the other, and both have to say
yes before a schema is induced — an approval matrix is genuinely a
`delegation_of_authority` and genuinely still policy.

## Classification rules

- Classify by what the document **is**, not what it is about. A purchase order
  attached to an email is a `purchase_order`; the email is `correspondence`.
- Where direction is ambiguous, resolve from the entity under audit: a demand
  for payment addressed **to** the entity is `vendor_invoice`; one issued **by**
  the entity is `sales_invoice`. If the document alone cannot settle it,
  classify `other` with a rationale rather than guessing.
- A document carrying several records — a voucher pack, a scanned bundle — takes
  the type of its **principal** record. The remaining records still reduce
  separately and keep their own fields.
- `other` requires `document_type_other` free text. It is extracted and stored
  but cannot fill a role until an auditor retypes it. Retyping may name an
  existing entry or coin a workspace-local `local.` type; either way the assigned
  type is fully effective from that point. See *Retyping and workspace-local
  types*.

## Procure to pay

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `purchase_requisition` | Purchase requisition | Internal request to buy, before a supplier is committed | PR, purchase request, requisition |
| `request_for_quotation` | Request for quotation | Solicitation issued to suppliers | RFQ, tender invitation, ITT |
| `vendor_quotation` | Vendor quotation | Supplier's priced offer, not yet an order | quote, bid, proposal, estimate |
| `purchase_order` | Purchase order | Entity's committed order to a supplier | PO, order confirmation |
| `goods_receipt` | Goods receipt | Internal record that goods were received and accepted | GRN, goods received note, receiving report |
| `delivery_note` | Delivery note | Supplier's document accompanying a shipment | despatch note, delivery challan, DN |
| `service_acceptance` | Service acceptance | Confirmation that a service was performed and accepted | service entry sheet, completion certificate, SES |
| `vendor_invoice` | Vendor invoice | Supplier's demand for payment addressed to the entity | supplier invoice, bill, tax invoice |
| `vendor_credit_note` | Vendor credit note | Supplier's reduction of an amount previously invoiced | credit memo |
| `vendor_debit_note` | Vendor debit note | Entity's charge back to a supplier | debit memo |
| `payment_voucher` | Payment voucher | Internal authorization to disburse against invoices | disbursement voucher, payment request |
| `remittance_advice` | Remittance advice | Notification of what a payment settles | payment advice |
| `vendor_statement` | Vendor statement | Supplier's periodic list of open items | supplier statement, statement of account |
| `vendor_master_form` | Vendor master change form | Request to create or amend supplier standing data | vendor onboarding form, supplier setup |

## Order to cash

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `sales_order` | Sales order | Customer's accepted order on the entity | customer order, SO |
| `proforma_invoice` | Proforma invoice | Advance invoice issued before supply, not a demand for payment | pro-forma |
| `sales_invoice` | Sales invoice | Entity's demand for payment issued to a customer | customer invoice, output invoice |
| `sales_credit_note` | Sales credit note | Entity's reduction of an amount previously billed | customer credit memo |
| `customer_receipt` | Customer receipt | Acknowledgement of cash received from a customer | official receipt, cash receipt |
| `customer_statement` | Customer statement | Entity's periodic list of open customer items | AR statement |
| `customer_master_form` | Customer master change form | Request to create or amend customer standing data | customer onboarding form |
| `credit_approval` | Credit approval | Decision granting or amending a customer credit limit | credit application, credit memo (limit) |

## Payroll and HR

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `employment_contract` | Employment contract | Agreement establishing employment terms | offer letter, appointment letter |
| `employee_master_form` | Employee master change form | Request to create or amend employee standing data | personnel action form, HR change form |
| `timesheet` | Timesheet | Record of hours worked by period | time record, attendance sheet, clock report |
| `leave_record` | Leave record | Record of absence taken or approved | leave application, absence record |
| `payslip` | Payslip | Individual statement of pay for one employee and period | pay stub, salary slip, wage slip |
| `payroll_register` | Payroll register | Entity-wide list of pay for one run | payroll summary, payroll journal |
| `payroll_bank_file` | Payroll bank file | Consolidated instruction to pay a payroll run | salary transfer list, bank upload file |
| `withholding_certificate` | Withholding tax certificate | Certificate of tax withheld from a payee | tax deduction certificate, Form 16, 1099 |
| `social_security_filing` | Social security filing | Statutory contribution return or receipt | pension filing, EPF/ESI return, payroll tax return |
| `expense_claim` | Expense claim | Employee's request for reimbursement | expense report, reimbursement claim |
| `travel_authorization` | Travel authorization | Approval of travel before it is incurred | travel request, trip approval |

## Treasury and banking

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `bank_statement` | Bank statement | Bank's periodic record of account movements | account statement, passbook |
| `bank_confirmation` | Bank confirmation | Bank's direct reply confirming balances or facilities | bank letter, standard confirmation |
| `payment_instruction` | Payment instruction | Entity's instruction to a bank to transfer funds | transfer request, wire instruction, RTGS/SWIFT request |
| `cheque` | Cheque | Negotiable instrument drawn on a bank account | check, demand draft |
| `bank_reconciliation` | Bank reconciliation | Working reconciling ledger to bank balance | bank rec |
| `petty_cash_voucher` | Petty cash voucher | Record of a small cash disbursement | cash voucher, IOU |
| `loan_agreement` | Loan agreement | Contract establishing borrowing terms | facility agreement, credit agreement |
| `loan_statement` | Loan statement | Lender's record of drawdowns, interest, and repayments | amortization schedule, facility statement |
| `fx_contract` | FX contract | Confirmation of a foreign-exchange deal | forward contract, FX deal ticket |
| `investment_confirmation` | Investment confirmation | Confirmation of a securities or deposit transaction | trade confirmation, deposit advice, contract note |
| `letter_of_credit` | Letter of credit | Bank undertaking to pay on documentary presentation | LC, documentary credit |
| `bank_guarantee` | Bank guarantee | Bank undertaking to pay on demand or default | performance bond, standby LC |
| `treasury_deal_ticket` | Treasury deal ticket | Internal record authorizing a treasury transaction | deal slip, dealing ticket |

## Inventory and logistics

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `material_requisition` | Material requisition | Internal request to issue stock | stores requisition, MR |
| `goods_issue_note` | Goods issue note | Record that stock left the store | GIN, issue slip |
| `stock_transfer_note` | Stock transfer note | Record of movement between locations | transfer note, STN |
| `stock_count_sheet` | Stock count sheet | Record of a physical inventory count | count tag, inventory sheet |
| `packing_list` | Packing list | Itemization of a shipment's contents | packing slip |
| `air_waybill` | Air waybill | Air carrier's contract and receipt for goods | AWB |
| `bill_of_lading` | Bill of lading | Sea or land carrier's contract and receipt for goods | BOL, consignment note, CMR |
| `customs_declaration` | Customs declaration | Filing to customs on import or export | bill of entry, SAD, shipping bill |
| `inspection_certificate` | Inspection certificate | Third-party attestation of quality or quantity | QC certificate, certificate of analysis |

## Fixed assets

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `capitalization_form` | Capitalization form | Record placing an asset into service | asset capitalization, WIP settlement |
| `asset_register_extract` | Asset register extract | Listing of assets and carrying values | FAR extract, asset listing |
| `asset_disposal_form` | Asset disposal form | Authorization and record of retirement or sale | disposal note, retirement form |
| `depreciation_schedule` | Depreciation schedule | Computation of periodic depreciation | depreciation run report |
| `asset_verification_sheet` | Asset verification sheet | Record of a physical asset inspection | asset count sheet |

## Financial reporting and general ledger

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `journal_entry` | Journal entry | Manual or adjusting posting with supporting narrative | JV, journal voucher, adjusting entry |
| `account_reconciliation` | Account reconciliation | Working reconciling a ledger account to support | balance sheet rec, GL rec |
| `trial_balance` | Trial balance | Listing of ledger balances at a date | TB |
| `general_ledger_extract` | General ledger extract | Transaction-level ledger listing | GL detail, account activity |
| `accrual_schedule` | Accrual schedule | Computation supporting an accrual or provision | provision schedule, accrual listing |
| `financial_statements` | Financial statements | Prepared primary statements and notes | FS, annual accounts |
| `intercompany_confirmation` | Intercompany confirmation | Agreement of balances between group entities | IC reconciliation, IC confirmation |

## Tax and statutory

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `tax_return` | Tax return | Filing submitted to a tax authority | VAT return, GST return, income tax return |
| `tax_assessment` | Tax assessment | Authority's determination of tax due | assessment order, notice of assessment |
| `tax_payment_receipt` | Tax payment receipt | Evidence of tax remitted | challan, payment confirmation |
| `statutory_filing` | Statutory filing | Non-tax regulatory submission | annual return, regulatory return |

## Governance and cross-cutting

| id | label | discriminator | common aliases |
| --- | --- | --- | --- |
| `contract` | Contract | Agreement not covered by a more specific type | agreement, MOU, SLA |
| `approval_form` | Approval form | Standalone authorization not embedded in another record | authorization form, sign-off sheet |
| `delegation_of_authority` | Delegation of authority | Schedule of who may approve what, to what limit | DOA, authority matrix, signature schedule |
| `board_minutes` | Board or committee minutes | Minuted decisions of a governing body | minutes, resolution |
| `insurance_policy` | Insurance policy | Cover note or policy schedule | policy schedule, cover note |
| `receipt` | Receipt | Generic acknowledgement of payment received | cash receipt, till receipt |
| `certificate` | Certificate | Attestation not covered by a more specific type | licence, registration certificate |
| `correspondence` | Correspondence | Letter, email, or memo used as evidence | email, letter, memo |
| `other` | Other | None of the above; requires `document_type_other` | — |

## Counts and coverage

Seventy-nine classifiable types across nine areas, plus `other` — eighty entries
in total. The areas exist for display and prompt grouping only; nothing in the
contracts keys off them.

Treasury, the area that fails silently today, has thirteen types and needs no new
mechanism — which is the test of whether this list is doing its job.

Implemented in `backend/app/document_types.py`, which is authoritative; this
appendix is its prose form. The module validates itself on import — id shape,
uniqueness, known area, non-empty discriminator, and no two entries reading the
same in a picker.

## Open points on the list

1. **`service_acceptance` vs `goods_receipt`.** Kept separate because service
   evidence rarely carries quantities and often carries a period instead. If
   classification confuses them in practice, merge rather than adding rules.
2. **`journal_entry` breadth.** It will absorb a wide range of supporting
   material. Watch its escape rate; a high one is the signal to split it.
3. **`correspondence` as a type and as an intake category.** The overlap is
   intentional but should be checked against `intake.DOCUMENT_CATEGORIES` so the
   two axes stay genuinely independent.
4. **Regional coverage.** Aliases lean Anglophone. Before freezing, review
   against the jurisdictions actually in scope; aliases are prompt text and cost
   nothing to extend.
