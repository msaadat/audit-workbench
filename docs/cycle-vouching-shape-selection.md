# Cycle vouching: shape selection is not dependable

**Status:** open problem, deferred. The cycle-vouching capability itself is
complete and working (phases 1–3); this document records the one part that is
not dependable, so it is not rediscovered from scratch.

## The problem in one sentence

`tests.generate` decides *by model discretion* whether an RCM row gets a
question test or a cycle-vouching test, and it frequently chooses the question
even when transaction evidence is available and a cycle test is what the row
needs.

## What was observed

Five live generation runs against `Workspaces/expenses` (agent profile
`nvidia/nemotron-3-ultra-550b-a55b:free`):

| Row | Outcome |
|---|---|
| `RCM-5F81CC` | `qa` test — no vouch step attempted |
| `RCM-5C741E` | `qa` test — no vouch step attempted |
| `RCM-6A6564` | blocked before generation (see *Unrelated blocker* below) |
| `RCM-9ED79B` | blocked before generation (same) |
| `RCM-037B1B` | **two cycle vouching tests** — correct shape |

The proposal sidecars for the two `qa` runs record **no validation errors and no
repair attempts**, which means the model never emitted a vouch step for the
validator to reject. This is selection, not contract compliance.

## The contract is not the problem

A controlled diagnostic — the same `GENERATE_SYSTEM` prompt, the same expenses
material, plus an explicit instruction to return a vouch test — produced a
structurally valid cycle plan on the first attempt: correct `anchor_table`,
`anchor_key`, `document_roles`, and paths on both sides of every check.

So the model *can* write the shape. It does not *choose* it.

Two prompt-level attempts to steer it were made and are still in the prompt.
Neither is sufficient on its own:

1. Explicit mode-selection guidance ("a row about whether a control *operated*
   is a vouch test whenever transaction evidence is available").
2. A `transaction_evidence` block in the generation payload naming which
   supplied documents carry extracted structured records.

The one successful run came after both were added, but a single success is not
evidence they work — the two failures preceded them and were not retried.

## Proposed fix: decide it in the capability, enforce it in the validator

Prompt wording should not be what decides whether an audit performs vouching.
The rest of this codebase decides this class of question with declared,
inspectable rules (`_planning_relevant`, `PLANNING_DOCUMENT_TERMS`,
`FIELD_GROUP_ATTRIBUTES`), and shape selection belongs in the same category.

Sketch of the intended approach:

1. **A deterministic possibility test, computed from data rather than prose.**
   For each `(table, column)`, count how many of its normalized values appear in
   the identifiers already extracted by the voucher analysis profile
   (`doc_tests.voucher_field_index`). A column that links at least a small
   threshold of rows is a cycle *anchor candidate*. A workspace with no analyzed
   evidence, or evidence matching nothing, yields no candidates — so no cycle
   test can be required of anyone, and this stays inert on engagements it does
   not apply to.

   This also removes a second guess: the candidates carry the anchor table, the
   anchor key, and the extracted `document_type` values, so the model is handed
   them instead of inventing them.

2. **A requirement on the unit.** `capabilities/tests.py::_generation_units`
   includes the requirement in the unit's `input_payload`, so `input_sha1`
   changes when the requirement changes and the unit re-expands.

   *Plumbing note:* the scheduler stores only `kind`, `title`, `parent_refs`,
   and `input_sha1` on the unit dict — **the `UnitSpec.input_payload` does not
   reach the binder** (`runtime/workflow_runner.py`, `_materialize`). So
   `audit_execution._bind_test_generate` has to recompute the requirement from
   the workspace and pass it through `unit_input`, the way
   `documents_execution._map_for_unit` recomputes chunk specs. Confirm this
   before designing around passing the payload through.

3. **Enforcement in the semantic validator.** If a cycle test is required for
   the row and the proposal contains no vouch test, that is a validation error.
   The existing bounded repair loop then gives the model one corrective turn,
   with the anchor candidates in the error message.

The open design question is step 1's scope: whether the requirement applies to
every row that *can* link, or only to rows whose risk concerns operating
effectiveness. Requiring it everywhere would put a meaningless cycle test on a
row about, say, missing SOP governance metadata. Gating on the row's prose is
possible (there is precedent in `intake.PLANNING_DOCUMENT_TERMS`) but is a
keyword heuristic and should be recognised as one.

## Unrelated blocker found along the way

Two of the five runs never reached generation:

```
Document Test 'DT-5F9C479D' has final-result items and cannot be re-specified.
```

A row whose document test already has final-result items cannot be regenerated,
so an existing completed `qa` test **cannot be converted into a cycle test**
without an auditor clearing it first. This is pre-existing behaviour, not caused
by the cycle work, but it directly limits adopting cycle vouching on any
engagement that has already run fieldwork.

## What works today regardless

- Auditors can author cycle tests directly: `POST /doc-tests/build/cycle`.
- When the agent *does* emit a vouch step, everything downstream is deterministic
  and tested: linking by extracted identifier, path resolution, comparison,
  citation-anchored evidence, and coverage reporting against the population.
- Every vocabulary the model previously had to guess is now shared and validated
  at authoring time: `doc_tests.FIELD_GROUPS`, `doc_tests.FIELD_GROUP_ATTRIBUTES`,
  `doc_tests.METHODS`, and `document_analysis.VOUCHER_DOCUMENT_TYPES`. Each of
  those three was found by inspecting real model output, and each would
  otherwise have surfaced as an empty result rather than an error.
