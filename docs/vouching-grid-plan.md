# Vouching as a grid: samples down, attributes across

**Status:** plan, not yet implemented. Follows the cycle-vouching capability
delivered in phases 1–4. Companion to
[cycle-vouching-shape-selection.md](cycle-vouching-shape-selection.md), which
covers a different open problem (*which* shape gets chosen); this one covers how
the chosen shape is authored and presented once samples get large.

## The problem

A vouching test is a schedule: one row per sample, one column per attribute
tested, a tick mark in each cell. That is how auditors have always worked and how
the result is reported ("2 of 12 vouchers lacked a receipt"). The workbench
currently stores exactly that data but presents and authors it in a shape that
does not survive a realistic sample.

Three separate explosions. The first two share a root cause; the third is
independent and multiplies whatever the other two produce.

**1. Test-count explosion (authoring).** `workers/tests.py` allows one vouch step
per document test, so every additional attribute the agent wants to test becomes
*another whole test* over the same population. Observed on `RCM-037B1B` in
`Workspaces/expenses`: four vouching tests — "Claim Submitter Vouch", "Claim
Approver Vouch", "Payment Voucher Preparer and Reviewer Vouch", "Claim Approver
and Submitter Vouch" — all anchored on the same 40-row population, all covering
the same 12 linked samples, differing only in which one or two comparisons they
carry. Each re-links the population, re-attaches the same documents, and produces
its own conclusion over the same samples. At 40 samples and a three-document
procurement cycle (PO / invoice / goods receipt note) with ~5 attributes per
document, the natural authoring drift is 10–15 tests where the engagement has
*one* procedure.

**2. Navigation explosion (review).** `DocTestItemList.vue` is a flat list, one
button per item, deliberately so — its comment records the assumption:

> Tests carry one or two items each, so grouping by test only added a level to
> click through without adding information.

That was true before cycle vouching. It is now false: one cycle test carries one
item *per linked sample*. `summary_payload` flattens every item of every test into
a single triage list, so the four tests above contribute 48 rows to a rail meant
for a handful, and the auditor can only see one sample's comparisons at a time in
`DocTestItemDetail.vue`. There is no view in which "did the approver match on all
12?" can be answered without 12 clicks.

**3. Per-document authoring (the multiplier).** A check names its role
explicitly, so a single audit assertion — "the amount agrees with the listing" —
must be written once per voucher type. Five assertions over a PO / invoice / GRN
cycle become fifteen checks that say the same thing three times, and a cycle that
happens to carry a fourth document type is simply not covered. This is what
§0 below addresses, and it is the change with the most leverage: it divides the
column count by the number of document types before the grid is ever drawn.

## What already exists (do not rebuild)

The grid is latent in the data model; this is a projection and an authoring
constraint, not a new engine.

- `build_cycle_vouching` copies **the same check templates into every item**
  (`checks=[dict(check) for check in check_templates]`). Every item of a cycle
  test therefore has the same checks in the same order — the columns are already
  uniform and stable.
- Cells already exist: each check carries `verdict` in
  `pending | match | mismatch | missing | invalid | ambiguous`, plus `expected`,
  `found`, `note`, and citation-anchored `evidence_refs`.
- Row state already exists: `_run_cycle_item` derives item
  `confirmed | exception | manual_review` from the check verdicts and
  `missing_roles`.
- Role membership already exists per item: `item["documents"]` carries
  `{document_id, role, document_type, matched_by}`, and `item["missing_roles"]`
  names required roles nothing filled. That is the "which voucher types are
  present" column group, already populated.
- Coverage against the population already exists: `spec["coverage"]` with
  `population / linked / unlinked / incomplete_roles`.

## Design

### 0. Generalized checks: quantify over roles

This is upstream of everything else, and it is what actually stops the column
count from multiplying by the number of voucher types.

Today a check names one role explicitly — `voucher.amount.total`,
`invoice.amount.total`, `purchase_order.amount.total` — so "the amount agrees
with the listing" has to be authored once per document type. Three roles × five
attributes is fifteen columns describing five audit assertions.

The mechanism to collapse that is already half-built. `WILDCARD = "*"` exists in
`doc_tests.py`, but `_parse_path` only honours it in the **key** position
(`voucher.amount.*` = any amount kind); the **role** position is an exact lookup,
`role_fields.get(role, ())`. Extend the wildcard to the role segment and one
check expresses the general assertion:

```
row.claim_amount   vs   *.amount.total        numeric_tolerance   quantifier: all
```

— "every voucher attached to this sample states a total that agrees with the
listing", whatever roles the cycle happens to contain. A three-document
procurement cycle and a single-voucher expense cycle are then the same test.

**A quantifier is required, not optional.** `_single_value` currently collapses
matches and returns `ambiguous` whenever they differ. That is correct for one
document stating two conflicting values, but wrong for three documents where one
disagrees — that is an exception with a named culprit, not an ambiguity. So a
check gains:

- `quantifier: "all"` — compare **each** match against the other side
  independently; the check fails if any match fails, and the failing
  `document_id` is recorded. This is the default for a wildcard role.
- `quantifier: "one"` — current behaviour, collapse-then-compare. Stays the
  default for an explicitly named role, so every existing test is unchanged.

`compare_values` stays exactly as it is — it compares two scalars. The quantifier
loops it and rolls the per-match outcomes up, storing them in the `comparisons`
array that already holds the two side objects and the outcome.

**One trap to close.** `_parse_path` does not validate the role segment at all —
any string parses. So `*.amount.total` is accepted today and resolves to nothing,
surfacing as a `missing` verdict: absent evidence rather than the unsupported
path it is. Same failure class as the `FIELD_GROUP_ATTRIBUTES` and
`VOUCHER_DOCUMENT_TYPES` bugs found during phases 1–3, and it should be closed
the same way — validate the role against the test's declared roles plus the
wildcard, and raise `PathError` otherwise. The generation validator already does
this (`validate_side` rejects undeclared roles); `build_cycle_vouching`, reachable
directly through `POST /doc-tests/build/cycle`, does not.

**The vacuous-truth trap.** `all` over zero matches is trivially true, so a
sample missing every voucher would pass silently. Zero matches must stay
`missing`, and role completeness must remain a separate assertion — `missing_roles`
already computes it per item and drives `manual_review`.

What a generalized cycle test looks like in full — five columns, not fifteen,
and the same five whether the cycle has one document or four:

| Assertion | Check |
|---|---|
| Amount agrees with listing | `row.amount` vs `*.amount.total`, `numeric_tolerance`, all |
| Payee agrees with listing | `row.vendor_name` vs `*.party.payee`, `fuzzy`, all |
| Every document names the transaction | `row.claim_id` vs `*.identifier.*`, `normalized`, any |
| Payment not before approval | `*.date.payment` vs `*.date.approval`, `date_order`, all |
| Receipt attached | `*.attachment.receipt.present`, `present`, all |

Role-specific checks remain available and are still the right tool when the
assertion genuinely concerns one document ("the PO was approved before its
date"). Generalization is the default, not the only mode.

### 1. Columns become a declared part of the spec

Today the column set is only implied by whatever checks the items happen to
carry. Add the canonical template list to the spec in `build_cycle_vouching`:

```python
test["spec"]["checks"] = check_templates      # canonical column definition
```

Three reasons this is worth the field rather than deriving columns from item 0:

- A grid needs a column order that does not depend on which item is first, and
  that survives an item whose checks were edited (`update_comparisons` is a
  public route).
- Column identity needs to be stable. `_normalize_check` defaults `field` to
  `"value"` and **does not enforce uniqueness**, so two checks can collide on the
  same label today. Assign each template a `key` (`c1`, `c2`, …) at build time,
  copy it onto every item's check, and reject duplicate `field` labels within one
  test — a schedule with two columns headed the same thing is not reviewable.
- With generalized checks a column is an *assertion*, not a document, so `field`
  becomes the column header an auditor reads ("Amount agrees with listing") and
  needs to be a declared label rather than an incidental one. Role-specific
  checks still group under their role via `_parse_path(check["right"])[0]`.

Guard the change with a fallback that derives templates from the first item, so
the twelve existing cycle tests in `Workspaces/expenses` keep rendering.

### 2. A lean grid projection, served separately

Do **not** build the grid client-side from `GET /doc-tests/{id}`. That response
carries, per check, both sides' `matches` with excerpts up to 400 characters plus
`evidence_refs`; at 40 samples × 20 checks it is on the order of a megabyte, and
the grid needs none of it.

Add `doc_tests.grid_payload(workspace, test)` and
`GET /api/workspaces/{ws}/doc-tests/{test_id}/grid`:

```jsonc
{
  "test_id": "DT-…",
  "anchor": {"table": "02_expense_claims", "key": "claim_id"},
  "coverage": {"population": 40, "linked": 12, "unlinked": 28, "incomplete_roles": 1},
  "roles": [{"role": "invoice", "required": true, "document_types": ["invoice"]}],
  "columns": [
    {"key": "c1", "field": "approver", "group": "voucher", "method": "exact",
     "left": "row.approver_name", "right": "voucher.party.approver"}
  ],
  "rows": [
    {"item_id": "ITEM-…", "label": "EXP-2025-003", "state": "exception",
     "classification": "exception", "missing_roles": [],
     "roles_present": {"invoice": "DOC-…", "goods_receipt": null},
     "cells": {"c1": {"verdict": "match", "left": "A. Rahman", "right": "A. Rahman",
                      "note": "", "evidence": 2,
                      // present only on a quantified check: which document failed
                      "per_document": [{"document_id": "DOC-…", "role": "invoice",
                                        "value": "A. Rahman", "verdict": "match"}]}}}
  ],
  "column_summary": {"c1": {"match": 11, "mismatch": 1, "missing": 0,
                            "ambiguous": 0, "invalid": 0, "pending": 0}}
}
```

`left`/`right` are the scalar compared values, truncated — never the excerpts.
Full evidence stays behind the existing per-item detail.

`column_summary` is not decoration: the per-attribute deviation count is the
reportable result of a vouching procedure, and nothing in the app computes it
today.

### 3. The grid view

New `frontend/src/components/doc-tests/DocTestGrid.vue`, PrimeVue `DataTable`
(already used by `FrameTable.vue`) with:

- Frozen first column: sample label (`item.label`, the anchor identifier), plus
  the row's overall state via `UiTestStatus`.
- A column group per role showing document presence, driven by `roles_present` /
  `missing_roles` — the PO/invoice/GRN completeness check, readable at a glance
  down the whole sample.
- One column per assertion, cell rendered as `UiVerdictStatus` (already maps
  verdicts to tick / cross / warning tones) with the compared values in the
  tooltip. A quantified cell that failed names the offending document there too —
  "invoice: 4,500 vs 4,050" — so the grid says *which* voucher broke, not merely
  that one did. Role-specific checks keep a `ColumnGroup` header per role.
- A footer row per column carrying `column_summary` — "11 ✓ / 1 ✗".
- `scrollable` with `virtualScrollerOptions` so 40+ samples do not mount 40+ rows
  of cells.
- Row or cell click selects the item and opens the **existing**
  `DocTestItemDetail.vue` beside or beneath the grid. The detail component is
  already correct for cycle items (roles, two-sided paths, citations); it is the
  navigation into it that is wrong, so it should not be touched.

`DocTestsTab.vue` picks the view by shape: `spec.shape === "cycle"` renders the
grid, everything else keeps the existing rail. Both keep `UiTriageCounts` and the
search box; search filters grid rows by label.

### 4. Stop the summary rail from flattening cycle items

`summary_payload`'s docstring states the assumption that is now false ("very few
items each"). Emit **one summary row per cycle test**, carrying its counts, in
place of one row per item — so a 40-sample cycle test occupies one line in the
engagement-level triage list and links into its grid, while non-cycle tests keep
their current per-item rows. Without this, fixing the grid still leaves the tab
that leads to it unusable.

### 5. Authoring: one cycle test per population, many columns

The grid only pays off if the agent stops splitting attributes across tests.

- **Prompt** (`workers/tests.py`, `GENERATE_SYSTEM`): state that a vouch step is a
  *schedule* — every attribute tested on the same sample set is another check in
  the same step, never another test. The current wording ("a vouch test has
  exactly one [step]") constrains steps per test but says nothing about tests per
  population, which is the axis that actually multiplied. Add the generalization
  rule with it: write the assertion once against `*` and let it apply to every
  document in the cycle; name a role only when the assertion is genuinely about
  that one document. Give the five-row table above as the worked example — the
  model has consistently done better with a shared vocabulary it can copy than
  with a rule it has to apply.
- **Validator** (`_validate_generate_cycle_step` and the proposal-level rule):
  reject a proposal containing two vouch tests with the same
  `(anchor_table, anchor_key)`; the repair turn merges their checks. This is the
  same class of deterministic guard as the existing one-vouch-step-per-test rule
  and the `FIELD_GROUP_ATTRIBUTES` vocabulary check.
- **Executor** (`executors/tests.py`, `_commit_document_test`): when a semantic id
  resolves to an existing cycle test over the same anchor, extend its columns
  rather than creating a sibling.
- **Manual route:** `POST /doc-tests/{id}/checks` to append a column to an
  existing cycle test — appends to `spec["checks"]` and to every item, verdict
  `pending`, leaving prior results intact so re-running only fills the new column.
  Auditors add attributes as fieldwork proceeds; requiring a rebuild would discard
  their sign-offs.

## Phasing

| Phase | Scope | Done when |
|---|---|---|
| 0 | Role wildcard + `quantifier` + role validation in `_parse_path` | one check expresses "all vouchers agree with the listing"; `*.foo.bar` against an undeclared role raises instead of resolving to `missing` |
| 1 | `spec["checks"]` + stable column keys + duplicate-`field` rejection, with derive-from-item-0 fallback | existing expenses tests still load; new builds carry canonical columns |
| 2 | `grid_payload` + `GET …/grid` | payload for a 12-sample test is < 50 KB and carries no excerpts |
| 3 | `DocTestGrid.vue` + shape-based switch in `DocTestsTab.vue` | 12 samples × N attributes visible without scrolling into a second screen; click opens existing detail |
| 4 | `summary_payload` aggregation for cycle tests | one rail row per cycle test |
| 5 | Authoring consolidation (prompt, validator, executor, append route) | a generation run on `RCM-037B1B` produces **one** vouching test whose columns are generalized assertions |

Phase 0 is the substantive backend change and is worth doing first — it decides
what a column *is*, and the grid built on per-role columns would have to be
rebuilt after it. Phases 1–4 are presentation and are independently shippable
against the tests already in the workspace. Phase 5 changes agent behaviour and
should land last, so its output can be inspected in the shape it is meant for.

## Testing

- Quantified checks, against the procurement three-document fixture in
  `test_cycle_vouching.py`: `all` passes when every document agrees; fails and
  names the offending `document_id` when one disagrees; reports `missing` rather
  than passing vacuously when no document carries the field; `any` passes on a
  single match. An explicitly named role keeps `quantifier: "one"` semantics, and
  every existing assertion in that file passes unchanged.
- `*` against a role the test did not declare raises `PathError` rather than
  resolving to `missing`.
- `build_cycle_vouching` assigns unique column keys; duplicate `field` labels
  raise; the procurement fixture produces the expected column set.
- `grid_payload`: column order matches `spec["checks"]`; a row missing a required
  role reports it; `column_summary` totals equal the per-item verdicts; no
  excerpt text appears anywhere in the payload (assert on size and on absence of
  a known excerpt substring).
- Backward compatibility: a cycle test saved before phase 1 (no `spec["checks"]`)
  still yields a grid.
- Frontend: typecheck plus a render of a 40-row × 15-column fixture to confirm
  virtual scrolling and frozen column behaviour.
- Validator: a proposal with two vouch tests on one anchor is rejected with an
  error naming both, and the repair turn's merged output validates.

## Non-goals

- **Sample selection.** The MVP premise stands: samples are supplied and
  *recognized* by identifier match, not drawn. A grid over a drawn sample would
  need selection method, seed, and population strata recorded in the spec — a
  separate piece of work.
- **Editing values in the grid.** Cells are read-only; sign-off stays in the item
  detail, where the citation and the auditor's note live together.
- **Replacing the item detail.** It is correct; only the way in is being changed.

## Open questions

1. **Does a failing quantified cell make one exception or several?** "Amount
   agrees" failing on the invoice but not the PO is one deviation for sampling
   purposes but two facts for the file. The grid shows one cell; `exception_count`
   currently counts items. Suggest keeping the item as the unit of deviation —
   that is what a sample size is computed against — and recording the per-document
   detail inside the cell.
2. **Wildcard on both sides.** `*.date.payment` vs `*.date.approval` is a cross
   product once more than one document carries both. Restrict it to a pairwise
   comparison within the same document, which is what "paid after it was
   approved" actually means, and require explicit roles for genuine
   document-to-document comparisons.
3. **Column count ceiling.** Generalization takes the expected width from ~18 to
   ~6, which removes most of this concern. If role-specific checks still push it
   wide, prefer horizontal scroll over per-role tabs until an engagement says
   otherwise.
4. **Uncovered rows.** 28 of 40 population rows have no document and become no
   item at all. Should they appear as greyed rows in the grid, making the
   coverage gap visible in the same place as the results, or stay a number in
   `scope_limitations`? Showing them is more honest but triples the row count at
   the observed link rate.
