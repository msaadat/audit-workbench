# RCM generation quality: what was wrong, what was fixed, what is left

**Status:** three rounds have landed. Round 1 (changes 1–5) fixed coverage and
risk wording; round 2 (changes 9–11) fixed the defects round 1 introduced. Round
3 — *Round 3: the pipeline, not the prose* below — rebuilt the evidence contract
and the repair loop after every RCM generation in the workspace began failing
outright. Rounds 1 and 2 were evaluated against live regenerations; round 3 is
evaluated against the two runs that failed, replayed as a fixture.

Recommendation 6 is now closed (round 3 split the turn, at the altitude that
mattered). Recommendation 7 is still the highest-value open item for the
*narrative* fields; round 3 closed its structural half. 8 is still deferred.

This document records an auditor's review of a generated RCM, the root-cause
analysis of the generation turn that produced it, the criteria a good RCM has to
meet, and the measured result of each round. It is the handoff for the remaining
work.

**Read *Round 2* before changing the template** — round 1's main lesson is that
adding a taxonomy to the prompt causes the model to leak it into fields, and
that banning a construction in one field relocates it to the next field rather
than removing it.

`docs/test-generation-quality.md` is the same treatment applied to
`tests.generate`, and it reproduced both of those lessons independently — which
makes them properties of this pipeline rather than of this artifact. It also
found RC1 below (a methodology channel declared but resolving to zero items)
repeating verbatim in a second capability, and adds a third lesson specific to
generated artifacts that are executed: the generating worker is workspace-free
by design, so a check at generation can never see what its own output does to
real data.

## Context

`Workspaces/procurement` — Global Bank procurement audit, 2025 annual plan.
Inputs: a procurement SOP extract, planning meeting minutes, and five tables
(requisitions 112 rows, po_data 93, invoice_data 118, vendor_master_file 39,
financial_approval_matrix 4).

The RCM under review: 15 rows in `Planning/RCM/`, all from agent run
`20260802-095835-01ac58`, all `review_status: draft`, `created_by: agent`,
`reviewed_by: null`. The generating call is captured at
`Debug/LLMCalls/call_3159c81681ba4f82854bc26cd117fb79.json` — the analysis below
is taken from that payload, not inferred.

### The turn as it was

| Slot | Source | Size |
|---|---|---|
| System prompt | `RCM_SYSTEM` in `app/agent/workers/planning.py` | 673 chars |
| `ACTIVE RCM TEMPLATE (verbatim)` | `app/templates/rcm.md` | 444 chars |
| `REVISED APM` | planning artifact | 13,373 chars |
| `CURRENT RCM TO REVISE` | — | empty (cold start) |
| documents | 2 docs, `summary` representation | 14,748 chars |
| table profiles | 5 tables, `table_profile` | 8,368 chars |
| methodology | `apm_methodology_candidates` | **0 items** |

18,962 prompt tokens, `nvidia/nemotron-3-ultra-550b-a55b:free`, temperature 0,
84.6s, 1,442 reasoning tokens. Total methodological instruction: 1,117
characters, 0.6% of the prompt.

## Findings from the audit review

Verified by re-performing the analytics against the source tables. The null
rates the RCM quotes (0.85% / 18.64% / 5.08% / 16.96% / 75%) are all
arithmetically correct — the defects are of interpretation and method.

### A. Risk statements contradicted by the data

- **`RCM-8263DB` (rated *critical*) rests on a misread null.** It states the DoA
  matrix "does not reflect current transaction values" because the max approval
  is 10,000,000 against an invoice max of 120,000,000. The matrix's CEO row
  carries `MAX_APPROVAL_AMOUNT = null` with `LIMIT_NOTES = "Above PKR
  10,000,000"` — unlimited authority above 10m. The matrix does cover
  120,000,000. Its second pillar, "LIMIT_NOTES missing in 75% of rows", is the
  same artifact: the three rows without notes carry explicit numeric limits.
  The error originates upstream in `Planning/APM.md` ("Fact: Invoice amounts
  exceed highest approval limit") and was inherited unchallenged.
- **`RCM-3D4B2A` (rated *high*) treats workflow state as a control failure.** It
  cites `PO_NUMBER` missing in 16.96% of requisitions as incomplete mandatory
  fields. All 19 are `Rejected` (4), `Pending PO` (7), or `Approved` but not yet
  converted (8); all 93 `Completed` requisitions have a PO number. `PO_NUMBER`
  is a downstream field, not a PR-creation field. The row also asserts
  requisitions are raised "without proper verification" — `VERIFIED_BY_ID` and
  `VERIFIED_DATE` have zero nulls across 112 rows.
- **`RCM-82FD84` and `RCM-B2C3A8` double-count.** Both are built on
  `GRN_ID_LINK` missing in 18.64% of invoices, rated *high* and *critical*
  respectively.

### B. Most "controls" were recommendations, not controls

At least nine of fifteen rows describe a target state the row's own risk says
does not exist — "Finalized SOP specifies…", "Formal exception handling
procedure defines…", "Standardized onboarding checklist mandates…". An RCM
documents controls **in place** so they can be tested; a control that does not
exist is a design-gap finding. This is why `test_refs` was empty on 14 of 15
rows: there was nothing to test.

### C. Coverage gaps, each with live exceptions in the data

Fifteen rows covered the twelve APM processes, but the risk universe was built
from document-design review and missed the transaction-level risks a
procurement audit exists to address. Found in minutes of analysis:

| Missing risk | Exception present in the data |
|---|---|
| Segregation of duties | 8 requisitions where `REQUESTER_ID = VERIFIED_BY_ID`; 1 invoice where verifier = supervisor approver |
| PO-to-invoice variance | 5 invoices differ from the linked PO total; largest variance **72,000,000** |
| Receipt-before-invoice timing | 5 invoices dated before their GRN date |
| Duplicate vendor bank accounts | 2 account numbers each shared by 2 vendors |
| Purchases from non-approved vendors | 1 PO raised to a vendor not in `Active` status |
| Split purchasing / threshold avoidance | named as a planned test in `APM.md` but never carried into a row |
| Payment processing | 101 paid invoices entirely outside the RCM; 8 paid with no GRN link |
| Contract management, master-data change control, related-party | absent |

On the DoA row specifically: approver `1002` financially approved a requisition
of 99,348,150 while every other approval by that ID sits under 10,000,000,
consistent with a CFO-level limit — a candidate breach the *critical*-rated row
neither tests for nor records a scope limitation about (the data carries no
`EMPLOYEE_ID → DESIGNATION` bridge, so the row is not testable as written).

Scope creep in the other direction: `RCM-B770E4` (ERP upgrade project
governance) and `RCM-A9A712` (regulatory identification) are entity-level or
project-assurance risks with no procurement control to test.

### D. Field and classification defects

- Assertions misapplied: an ERP project plan tagged *Existence*; vendor KPI
  performance tagged *Accuracy*; PO issuance method tagged *Existence*.
- Control types wrong: `RCM-82FD84` and `RCM-B2C3A8` both tagged *detective*
  where the described mechanism (system-enforced blocking / matching) is
  preventive.
- `control_owner`, `criteria`, `criteria_refs`, `evidence_refs`, `prepared_by`,
  `reviewed_by` empty or null on all 15 rows. `working_papers.py:208` prints
  "Control owner: Not stated" fifteen times.
- Test coverage 1 in 15. The one linked test served a *high* row; both
  *critical* rows had none.

## Root causes in the generation turn

**RC1 — the methodology channel was wired but empty.** The `planning.rcm` preset
declares a `methodology` source (5 items, 8,000 chars). It resolved to zero
items: `KnowledgePacks/` does not exist at either scope. The slot designed to
carry a standard risk set was empty, so the model had no source of domain
knowledge and only knew what this engagement's documents said.

**RC2 — documents arrived as summary + AUDIT NOTES, so the turn transcribed
findings into rows.** `document_context.apm_document_context` composes
`DOCUMENT SUMMARY … AUDIT NOTES`, where the notes block is a numbered deficiency
list. The same notes also appear inside the APM under "Prior audit findings" —
the deficiency list was in the prompt twice, and it was the most row-shaped
content there. The result was a 1:1 transcription. **All 15 rows trace to an
input observation; none originated from procurement domain knowledge.**

| Input | Rows |
|---|---|
| SOP audit notes 1–7 | `E7A6A1`, `24BA39`, `1E3D6C`, `8263DB`, `7F445B`, `118311`, `483790` |
| Meeting audit notes | `B770E4`, `A9A712` |
| Table profile null rates | `3D4B2A`, `B2C3A8`, `82FD84` |
| SOP section + column presence | `0C5701`, `4F98C5`, `6C999B` |

**RC3 — small reference tables reach the model as statistics only.** The entire
view the model had of the 4-row approval matrix was:

```
MAX_APPROVAL_AMOUNT: distinct 3, max "10000000", min "1000000", nulls_pct 25.0
LIMIT_NOTES:         distinct 1, nulls_pct 75.0
```

The CEO row's `LIMIT_NOTES` value was never in the prompt. Given `max =
10,000,000` and a 25% null, "the matrix tops out at 10m" is the only available
reading — the model reported the profile accurately and the profile was the
wrong representation for a 4-row dimension table. This is also why data-derived
risks came out worded as null percentages: the statistic was the only evidence
available to quote.

**RC4 — the template defined a schema, not a methodology.** 444 characters. It
never stated what a risk statement is versus an observation, that risks come
from a process risk universe first, that the control column records controls
asserted to be in place, the assertion vocabulary, a rating rubric, the
preventive/detective test, one-risk-per-row, or any coverage floor. Its one
substantive line — "Do not claim that a control exists unless the planning basis
supports it" — was followed literally and produced the aspirational-control
problem, because there was no instruction for what to do instead.

**RC5 — the schema never asked for the fields that came back empty.**
`RCM_SYSTEM` requested exactly `operation, rcm_id, process, risk, risk_rating,
assertion, control, control_type`. `criteria` and `control_owner` were not
requested, so they were empty by construction — while `_RCM_ROW_CONTEXT_FIELDS`
reads them back on the next revision and the working paper renders them.

**RC6 — the quality gate checks types, not audit quality.**
`validate_rcm_proposal` enforces field presence, string types, the rating enum,
the operation enum, and `new_risk_reason` on creates. A 15-row findings list
passes cleanly; the gate never fired on this run.

## Round 1: what changed

1. **`backend/app/templates/rcm.md` rewritten** (444 → ~6,400 chars). Carries the
   methodology: one risk + one control per row; a two-pass method (enumerate the
   standard risk universe for the cycle from the model's own knowledge, *then*
   tailor to the engagement); ten domain-agnostic risk classes to check per
   process (authorization, segregation of duties, threshold circumvention,
   validity, completeness/accuracy, master data, matching, cut-off, monitoring
   and override, compliance); generic condition-independent risk wording with
   worked write/not-write pairs; "No control identified" instead of aspirational
   controls; field-by-field rules for rating, assertion, control type, criteria,
   and owner; and how to read table profiles.

   Knowledge packs were rejected as the mechanism — this is a generic agent and
   packs would have to be authored per audit area. Eliciting the model's own
   standard-risk knowledge is domain-agnostic and needs no per-area content.

2. **`RCM_SYSTEM` expanded** (`app/agent/workers/planning.py`). Now requests
   `criteria` and `control_owner`, and states the five non-negotiables: cover
   standard risks from own knowledge rather than only the observed ones; generic
   condition-independent wording with no percentages, counts, or column names;
   "No control identified" where none exists; profiles are not evidence; one
   risk and one control per row.

3. **User-message `INSTRUCTIONS` state the two-pass method** explicitly, so it is
   present in the turn even if a workspace overrides the template.

4. **`criteria` and `control_owner` now persist on revision**
   (`app/agent/executors/planning.py`). `RCM_OPTIONAL_ROW_FIELDS` is written only
   when the proposal supplies a non-empty value, so a rerun that cannot cite a
   criterion does not blank an earlier citation. `reconcile_rcm` deliberately
   still compares `RCM_ROW_FIELDS` only — the fields are written in one atomic
   `update_rcm`, so the required subset is a sufficient proxy, and including
   optional fields would produce false `not_applied` classifications.

5. **The RCM turn no longer receives the AUDIT NOTES block.**
   `apm_document_context` and `apm_document_candidates` take
   `include_audit_notes`, and `rcm_scope` passes `False`. Verified against the
   live workspace: the SOP summary supplied to the RCM turn drops 8,767 → 4,984
   chars and the meeting minutes 5,981 → 2,497, with the process description
   intact. **The APM turn is unchanged** and still receives both blocks —
   observations belong to the memorandum, which the RCM turn inherits as parent.

Tests added: `test_rcm_scope_supplies_the_process_description_without_the_audit_notes`
(`tests/test_agent_context_adapters.py`) and
`test_rcm_executor_writes_supplied_criteria_and_keeps_it_when_a_rerun_omits_it`
(`tests/test_agent_planning_executor.py`). Suite: 1155 passed, 2 failed — both
failures pre-exist on `main` and are unrelated
(`test_command_agent.py::test_full_audit_command_uses_documents_and_planning_templates`
and `test_rcm_execution.py::test_completion_uses_execution_and_outcome_gates`,
both the same `completed_with_open_items` vs `completed` assertion).

### One recommendation was changed during implementation

The original recommendation for RC3 was to send the rows of small reference
tables. **This is not permissible.** `_validate_privacy` in
`app/agent/context/presets.py` hard-rejects `allow_table_rows=True` for any
preset, and `ContextCandidate.__post_init__` rejects the representation
structurally — row-level table data cannot reach a provider, by design, at two
independent layers. `table_aggregate` does not help either: it strips literal
values through `_LITERAL_PROFILE_FIELDS`.

What landed instead is the honest fix within the invariant: the model is told
explicitly what a profile is and is not (a null percentage is not an exception
rate; a maximum is not a policy ceiling; never state a population fact or
conclude a control failure from a profile statistic), and the generic-wording
rule removes the incentive to quote statistics at all. A risk phrased "purchase
orders may be approved by officials below the required delegation level" cannot
carry the `RCM-8263DB` error, because it asserts nothing about the matrix's
contents.

**This has a limit worth knowing:** the turn still cannot see that the CEO row
means "unlimited". It can no longer state the opposite as fact, but a correct
positive reading of a small dimension table is out of reach for any model-facing
turn under the current privacy design. If that becomes necessary, it needs a
deterministic local pre-pass that reads such tables and emits a bounded,
reviewed *statement* (not the rows) into context — a design change, not a
prompt change.

## Round 1: measured result

The RCM was regenerated (agent run `20260802-105228-05fe01`, call
`Debug/LLMCalls/call_41c114959ba146bfb4e98ee07d7a7b86.json`) and reviewed against
the checklist at the end of this document. Template 6,671 chars in prompt, no
`AUDIT NOTES` in any supplied document, 20,158 prompt tokens → 10,931 completion
tokens, 288s, same free-tier model.

| Check | Round 0 | Round 1 |
|---|---|---|
| Rows / processes covered | 15 / 12 | **52 / 12** |
| Risks containing a `%` | 5 of 15 | **0 of 52** |
| Risks containing a column name or any digit | 5 of 15 | **0 of 52** |
| Aspirational "controls" that do not exist | ~9 of 15 | **0** (6 rows say `No control identified`) |
| `criteria` populated | 0 | **52 of 52** |
| `control_owner` populated | 0 | 42 of 52 |
| Entity-level scope creep | 3 rows | **0** |
| Duplicate pairs | 1 | 4 (of 52) |

**The coverage fix worked, through the model's own knowledge and with no risk
library.** Every gap listed in *Coverage gaps* above is now present: 5
segregation-of-duties rows, 3 threshold-circumvention rows, PO-to-invoice
variance, unapproved-vendor purchases, cut-off across four processes.
`RCM-0438DE` — bank-account changes not independently verified, enabling payment
diversion, rated critical — is the classic procurement fraud risk that was
missing, and nothing in the supplied material prompted it. `RCM-B836BA` reaches
AML/sanctions/KYC screening unprompted. The `RCM-8263DB` error class is gone: the
DoA row no longer asserts anything about what the matrix contains.

### The defects round 1 introduced or left

**D1 — controls assert system behaviour that is not evidenced, and some is
contradicted by the data.** A *new* failure mode, and worse in kind than the one
it replaced: round 0 said a control ought to exist; round 1 says one does. Nine
rows assert ERP enforcement absent from the SOP. Two are contradicted:

- `RCM-22DA16`: "system prevents same user ID from populating both" — 1
  requisition has verifier = approver, 8 have requester = verifier.
- `RCM-73FC2F`: "only active vendors selectable" — 1 PO was raised to a
  non-`Active` vendor.

Others (`RCM-D3229C` "enforces role-based access", `RCM-E9CB6C` "status tracking
prevents bypass", `RCM-55EE4C` "prevents out-of-sequence creation") are
unverifiable and unevidenced. The chain is *field exists → field is mandatory →
system enforces it*; the round-1 template asked for control mechanics and never
said what to do when the mechanics are unknown.

**D2 — the control column inherited the statistics the risk column shed.** 22 of
52 controls contain a percentage or column name; 15 use a `"…; but [deficiency]"`
clause. The numbers moved one field left rather than disappearing, because the
wording rules were scoped to `risk` alone. Two are wrong:

- `RCM-0E9E69`: "3.57% null rate in FIN_APPROVAL_DATE suggests gaps" — all 4
  nulls are `Rejected` requisitions, which correctly have no financial approval.
  The round-0 `RCM-3D4B2A` error surviving in a different field.
- `RCM-3D94B4`: "GRN_ID populated for all 93 POs and GRN_DATE for 91" —
  `GRN_DATE` has zero nulls across all 93. Fabricated.

**D3 — the assertion field was contaminated by the risk-class list.** 23 of 52
rows used a value outside the declared vocabulary — `Validity` (6), `Master
Data` (6), `Completeness and Accuracy` (5), `Monitoring and Override` (2),
`Cut-off and Sequence` (2), `Matching and Reconciliation` (2) — all lifted from
the template's own risk-class names. `Existence`, `Valuation`, and `Operational`
went unused. Cause: the ten risk classes and the eight assertions sat in the same
document sharing vocabulary, so the model merged the lists.

**D4 — control owners invented.** "IT Security / Procurement Systems
Administrator" owned 10 rows and appears nowhere in the SOP; nor do "Procurement
Manager", "Procurement Team Lead", or "Requester Department Head". The SOP names
only Requisitioning Department, Procurement Team, Financial Authorities, Vendor
Approval Committee, Legal, and Finance. "Leave empty rather than guessing" sat at
the end of a field list and was ignored on ~14 rows.

**D5 — cross-process duplication persists.** Four pairs, each a risk spanning two
processes filed under both: `RCM-E32CE6`/`RCM-BEC99E` (three-way match),
`RCM-1E7FCC`/`RCM-E235B2` (splitting to avoid approval thresholds),
`RCM-CB3F59`/`RCM-D01206` (vendor approved without committee),
`RCM-D519B4`/`RCM-4B1963` (vendor master data quality). The template forbids
duplicates but gives no rule for which process owns a cross-process risk.

### Two transferable lessons

1. **A taxonomy added to the prompt will leak into the fields.** D3 was caused by
   putting a ten-item risk-class list a page above an eight-item assertion list.
   Any list added for *thinking* has to be marked explicitly as not-a-field-value
   and kept lexically distinct from any enumeration a field accepts.
2. **Banning a construction in one field relocates it, it does not remove it.**
   D2 is the percentages moving from `risk` to `control`. A wording rule has to
   be scoped to every free-text field at once, or it just migrates.

## Round 2: what changed

Three template fixes plus a mirror into the system prompt. `rcm.md` is now ~9,200
chars.

9. **The two vocabularies are separated** (fixes D3). The ten risk classes became
   a numbered "risk themes" checklist, explicitly labelled *"a prompt for your
   thinking only … not assertions, not field values, and must never be copied
   into any field of a row"*. The `assertion` field became a closed enumeration
   spelled out verbatim, with the six leaked values named as prohibited and a ban
   on joining two assertions with "and".

10. **The wording and evidence rules extended to the control field** (fixes D1
    and D2, which are the same leak). Two new blocks in *Writing the control*:
    never assert that a system enforces, prevents, blocks, validates, or
    restricts unless the planning basis says so — *"a field existing in a table
    is evidence that a value is recorded, never evidence that it is controlled"*
    — with a worked write/not-write pair built from the actual `RCM-22DA16`
    failure, and the instruction to write "mechanism not confirmed in the
    planning basis" where the basis names a control but not how it works. Plus
    the risk wording rules restated as applying to `control` too, with the
    `"…; but [deficiency]"` pattern named and banned. `control_type` was also
    softened so that classifying a control preventive is not a licence to assert
    a mechanism.

11. **Empty-rather-than-guess became a hard rule** (fixes D4). `control_owner`
    now requires the role to appear *verbatim* in the planning basis, with the
    reasoning stated (an empty owner is a question for the client; an invented
    one is a false attribution that survives into the working paper) and the
    specific wrong inference blocked — a system-enforced control does not imply
    an IT owner.

The three hardest rules were also mirrored into `RCM_SYSTEM`
(`app/agent/workers/planning.py`), because a workspace can override the template
at `Workspaces/<id>/Templates/rcm.md` but cannot override the system prompt: the
no-invented-system-behaviour rule, the closed assertion list, and
optional-fields-stay-empty.

Suite after round 2: 1155 passed, 2 failed — the same two pre-existing failures.

**Round 2 has not yet been evaluated against a regeneration.** When it is, check
D1–D5 specifically; the round-1 numbers above are the baseline.

## Round 3: the pipeline, not the prose

Rounds 1 and 2 tuned what the prompt *said*. Round 3 was forced by a different
class of failure: after the transaction-cycle evidence contract was added to the
row schema, **every RCM generation that reached the model failed outright**.

### The evidence

Two runs completed the model call; both failed, and one was already a linked
retry of an earlier failure:

```
20260809-105349-fb0f80  failed  after 2 attempt(s): RCM row 11: Unsupported assertion operator 'equals'.
20260809-122429-3142de  failed  after 2 attempt(s): RCM row 8: ... 'equals'.; RCM row 12: ... 'greater_than_or_equal'.
```

The preserved rejection (`AgentRuns/20260809-122429-3142de/rejections/rcm.json`,
13 rows, 23,413 chars) is now
`backend/tests/fixtures/rcm_operator_rejection.json`. Replaying it establishes
the root causes:

| Fact | Measured |
|---|---|
| Comparison operators the model authored | 6 |
| Of those, valid | **0** |
| Comparisons also carrying an invented `operator_tolerance` key | 6 of 6 |
| Rows with no defect at all | 11 of 13 |
| Errors the repair turn was actually told about | 2 of 12 |
| Cost of run A attempt 1 | 29,661 reasoning tokens, 474s — discarded |

Four separate defects, none of them about audit judgment:

1. **The operator vocabulary was stated nowhere in the turn.** Not in
   `RCM_SYSTEM`, not in `rcm.md`. The system prompt spent 11,222 of its 17,076
   characters on the pack catalog — which record kinds and field selectors to
   copy — and zero on the six verbs that consume them. `tests.generate`
   documented them properly all along; the two prompts had drifted.
2. **The rejection named the wrong value and never a right one.** The message was
   `Unsupported assertion operator 'eq'.` Run A's trace is the proof that this
   cannot converge: attempt 1 wrote `eq`, was told `eq` was unsupported, and
   attempt 2 wrote `equals`.
3. **Errors were truncated below the count needed to fix them.** Each row
   aborted at its first bad comparison, so row 8's five defects were reported as
   one. With `max_repair_attempts=1`, a perfect repair of what was reported still
   failed.
4. **The prompt's own phrasing manufactured a defect.** It asked for "key, label,
   operator, left, optional right, and operator tolerance" — which reads as a
   field name, so the model wrote `operator_tolerance`. The key was silently
   ignored, so the tolerance was silently dropped, and nothing said so.

Note what *is* absent from that list: the model's audit reasoning. Eleven of
thirteen rows were substantively fine. This was a contract failure.

### What changed

12. **One operator table, read by the gate and both prompts**
    (`app/cycle_registry/operators.py`). Arity, operand type, tolerance shape,
    and direction as data. `cycle_vouching.OPERATORS` derives from it,
    `validate_assertions` is driven by it, and `prompts.operator_table()` renders
    it into both `planning.rcm` and `tests.generate`. The drift that caused this
    is now structurally impossible, and a parity test asserts it.
13. **Rejections teach.** `unsupported_operator_message` names the offending
    value, the complete legal set, and — for a recognizable near-miss — the
    operator probably meant *plus what the rename alone does not fix*. `equals`
    on an amount is told it may need `numeric_within` with a tolerance;
    `greater_than_or_equal` on dates is told to swap its operands. Deliberately
    **not** auto-applied: both are audit-design decisions, and the live payload
    proves it — `payment_after_receipt` written as `greater_than_or_equal` would
    become the opposite test under a naive rename.
14. **Every independent violation is reported, with a path.** Attributes and
    comparisons are validated independently and their errors collected;
    `CycleSchemaError` now carries `.errors`. Paths reach
    `control_attributes[1].required_comparisons[0].left.field`. The live fixture
    now yields 12 errors where it yielded 2. The repair policy was widened to 20
    errors / 4,000 chars to match.
15. **Closed key sets where placement carries meaning.** Control attributes,
    comparisons, operands, field selectors, registry references, and recipe
    references reject unknown keys, naming what they do accept — and record the
    unknown key *alongside* the object's other defects rather than instead of
    them. A `required_record_kinds` nested inside `registry` is now named as a
    misplacement rather than surfacing as a stale-reference complaint. Row-level
    extras are still dropped, not rejected: the workspace discards them anyway.
16. **Named comparison recipes** (`app/cycle_registry/recipes.py`). Fourteen
    named audit tests — three-way match, receipt-before-payment,
    approval-before-document, amount/quantity/party agreement, pay-period
    agreement — selected by id with record-kind bindings, expanded locally into
    canonical comparisons. Free-form DSL remains available as the reviewed
    fallback. A recipe is a shortcut through the *authoring*, never through the
    gate: its expansion is validated identically, and a test binds all 14 through
    the real validator.
17. **The turn is split by altitude** (closes recommendation 6, differently).
    `RCM_SYSTEM` decides risks, controls, and each attribute's evidence
    *strategy*, and never sees the pack catalog. `RCM_EVIDENCE_SYSTEM` authors
    contracts for the attributes that asked for one, and is the only prompt
    carrying the DSL and the catalog — and it is skipped entirely when no
    attribute needs it. In the live response only 4 of 28 attributes did.
    Measured: the judgment prompt fell from 17,076 to 4,929 characters.
18. **A declared record kind nothing reads is rejected at the RCM layer.** It was
    only caught at test generation, one capability downstream of the row that
    caused it. This found a real defect in the shipped payroll fixture.
19. **Repair is scoped to the rows that failed**, and merged locally over the
    rows that did not — carried through as the identical parsed objects, so a
    repair cannot reword a row it was not asked about. Previously any single row
    error regenerated the whole document, spending the one correction turn on
    rows with nothing wrong with them.
20. **Rows that will not repair are quarantined, not fatal.** On the last
    attempt, rows that still fail are set aside with their reasons and travel to
    the executor receipt; the rest commit. An empty survivor set is still a
    failure. This is the change that converts "validation errors are rarer" into
    "validation errors are not fatal" — the RCM is a set of independent rows, and
    the executor already commits them one at a time.

### The run that caught 21: validation has to be idempotent

`20260809-133225-658b03`, the first run on the new pipeline, is the useful one.
The generation side worked: **16 rows, recipes used on 3 of them, no repair turn,
gate passed first time**. It then failed at the commit step:

```
control_attributes[0].required_comparisons[2]: duplicate required comparison key 'total_amount_agreement'.
```

Nothing was wrong with the response. A row is validated more than once in its
life — the worker normalizes the proposal, the executor re-validates before
committing, the workspace re-validates on load — and change 16 expanded recipes
into `required_comparisons` while leaving `comparison_recipes` in place. The
second pass expanded them again and collided with its own first expansion.

21. **Expansion retires the recipe list.** `validate_control_attributes` now
    clears `comparison_recipes` and records what it expanded under
    `comparison_recipes_applied`, which is provenance and is never expanded.
    Validating an attribute twice, or three times, is now a no-op. Two latent
    versions of the same collision were closed with it: one recipe applied twice
    to an attribute (legitimate — an invoice agreeing to its order *and* its
    goods receipt) now qualifies its keys by binding, and comparison keys are
    enforced unique across the whole catalog at import.

Replayed against the contracts that run actually authored, all 16 rows validate
and re-validate identically, with the one hand-written comparison preserved
alongside the recipe expansion. No row had been committed, and the stale proposal
sidecar cannot be replayed because the worker implementation hash it is bound to
has changed.

### The run that caught 22 and 23: two brackets

`20260809-140906-19a740` failed with 13 attributes reporting *"declares evidence
kind 'transaction_cycle' but names no evidence contract"*. The debug log shows
only two model calls, **both `[agent:rcm]`** — the evidence pass never ran at all.

The judgment pass had returned a complete, valid **24-row** object followed by two
stray characters, `]}`, from the model closing brackets it had already closed.

22. **`json.loads` requires the entire string to be one value**, so the whole
    24-row draft was discarded — and because the rows never parsed, the evidence
    pass was skipped. Parsing now takes the first complete JSON object via
    `raw_decode` and ignores trailing surplus. Only surplus: a *truncated* object
    still fails, because reading one as complete would commit a matrix that
    silently stops halfway.
23. **The whole-document re-ask returned the model's response directly**, skipping
    the evidence pass. So even after the re-ask produced 17 clean rows, all 13 of
    their transaction-cycle attributes were left without the contract the
    judgment prompt had just told the model not to write. Every path that
    produces a document now goes through one `_contracted_document` helper.

Replayed through the fixed worker, that run's own judgment response yields **24
accepted rows on the first attempt, no repair turn**, with 6 cycle attributes
carrying validated contracts.

Worth noting what these two runs have in common with the original failure: in all
three the model's audit reasoning was sound and the pipeline threw the work away.
That is the failure mode this artifact is prone to, and it is worth checking for
first.

Suite after round 3: 1476 passed, 2 failed — the same two pre-existing failures
(`test_rcm_central_e2e`, `test_rcm_execution`), both unrelated to generation.

**Round 3 has not yet been evaluated against a live regeneration.** The gate,
the recipes, and the two-pass split are covered by
`backend/tests/test_rcm_evidence_contract.py` and the replayed fixture; what a
live run will show about the *narrative* criteria below is still open, and the
round-2 column of the table is still unfilled.

## Criteria for a good RCM

The standard the generated matrix should be measured against, and the basis for
any eval built later.

**Structure**
- One risk, one control, one row. No compound rows.
- No two rows describing the same underlying failure. Where they exist, they are
  merged, and they cannot carry different ratings.
- Every in-scope process from the APM process flow has at least one row.

**Risk statements**
- Generic and condition-independent: what could go wrong, whether or not it has.
- No percentages, counts, null rates, column names, table names, file names, or
  document ids.
- No embedded cause, and no pre-concluded deficiency.
- Not a restatement of a supplied observation or audit note.

**Coverage**
- Derived from the standard risk universe for the cycle, then tailored — not
  derived from the engagement's observations.
- The standard risk classes are considered for every process, and are dropped
  only when they genuinely do not apply, never because the material is silent.
- Transaction-level fraud and error risks are present, not only design risks:
  segregation of duties, threshold circumvention, duplicates, master-data
  change, validity of counterparties, matching, cut-off.
- No entity-level or project-assurance risks that carry no testable process
  control.

**Controls**
- Describe a control management asserts is in place, with its mechanics — who,
  over what population, how often, what happens on failure.
- "No control identified" where none exists; never a recommendation phrased as
  an operating control.
- Nothing about how the control will be tested.

**Fields**
- `assertion` reflects what the risk actually threatens; `Operational` where no
  financial-statement assertion applies, rather than forcing a fit.
- `control_type` judged on the control's mechanics, not the risk's severity.
- `risk_rating` follows the rubric; `critical` reserved for a single failure
  permitting material loss or fraud with no compensating control.
- `criteria` cites a real clause from the planning basis or is empty.
- `control_owner` names a role from the planning basis or is empty.

**Downstream**
- Every row is testable as written, or records the scope limitation that
  prevents it.
- No row advances past `draft` without a test and a named reviewer.

## Open recommendations

### 6. Split the turn into two passes — CLOSED by round 3, at a different seam

Round 3 split the turn, but not along the axis proposed here. Splitting *risk
enumeration* from *engagement tailoring* stayed deferred for the reason given
below: round 1's single-turn two-pass instruction lifted coverage from 15 rows to
52, so the structural guarantee was buying little.

What round 3 split instead was **judgment from mechanism**: the risk/control/
strategy pass, and the evidence-contract pass. That seam was carrying the actual
failures, and it also removed 11kB of pack catalog from the expensive call. The
original proposal below is retained because its reasoning still applies if a
coverage failure ever appears.



Pass 1 enumerates the risk universe from the process flow and the model's own
domain knowledge, with **no engagement observations supplied**. Pass 2 tailors
ratings, controls, and wording against the APM and the data. This structurally
prevents observation-to-row transcription rather than instructing against it,
which matters because change 1 relies on the model honouring a two-pass
instruction within a single turn — a weaker guarantee than two turns.

The worker/executor split makes this contained: a second `WorkerDefinition`
feeding its output into the existing one, or one worker invoked twice with
different bundles. The `planning.rcm` preset already has the source declarations
needed; pass 1 would use a subset (planning context, template, process flow
from the APM) with documents and profiles withheld.

**Round 1 lowered the priority of this.** The single-turn two-pass instruction
lifted coverage from 15 rows to 52 with every previously-missing risk class
present, so the structural guarantee is buying less than expected. The run also
cost 288s and 10,931 completion tokens for one pass; a second turn roughly
doubles that. Defer until a case appears where the single turn demonstrably
fails on coverage.

### 7. Add coverage and phrasing checks to `validate_rcm_proposal`

**Still the highest-value open item, now narrowed to the narrative fields.**
Round 3 built the deterministic gate for the *structured* half — the evidence
contract, the operator vocabulary, closed key sets, unread record kinds — and the
bullet below about the closed `assertion` list is done (it is a closed enum check
in `_validate_control_attribute`, reported with the legal set). What remains is
exactly the prose rules: the `%` and `ALL_CAPS_UNDERSCORE` rejection in `risk`
and `control`, the aspirational-control guard, the system-enforcement flag, and
deduplication.

The infrastructure round 3 added makes these cheaper than they were: errors are
collected per row rather than fail-fast, they carry paths, and a row that cannot
be repaired is quarantined instead of sinking the document — so a phrasing gate
can be strict without risking the whole matrix.

Rounds 1 and 2 have taken prompt-only fixes
about as far as they go: each round removed one defect class and the model found
an adjacent field to express it in. A deterministic gate does not move.

Cheap gates in `app/agent/workers/planning.py`, which already has the repair loop
(`max_repair_attempts=1`) to feed violations back:

- Reject `%` or an `ALL_CAPS_UNDERSCORE` token in **either** `risk` or
  `control`. Would have caught 22 round-1 rows. Scope it to both fields from the
  start — round 1's lesson is that a single-field rule migrates.
- ~~Reject an `assertion` outside the closed list.~~ **Done in round 3.** Pure
  enum check in `_validate_control_attribute`, reported with the legal set.
  Would have caught 23 round-1 rows.
- Reject a `control` opening with an aspirational construction ("Formal …
  defines", "Standardized … mandates", "Finalized … specifies") unless it is
  exactly `No control identified`. Round 2 shows 0 of these, so this is a
  regression guard rather than a fix.
- Flag a `control` asserting system enforcement ("system prevents", "ERP
  enforces", "only … selectable", "blocks") for auditor attention. Cannot be a
  hard reject — the assertion is sometimes legitimate — but it is exactly the
  D1 class and it should not pass silently.
- Warn, not reject, when a process named in the APM process flow has no row.

Deduplication (D5) also belongs here rather than in prompt text: compare
normalized `risk` strings pairwise and flag near-identical pairs across different
`process` values. Four round-1 pairs would have surfaced.

Note the trade-off: rejecting on a bare numeric literal will occasionally catch a
legitimate risk statement. Start with `%`, column-name tokens, and the assertion
enum, which have no legitimate use at all.

### 8. Reconsider the model for this step

`nvidia/nemotron-3-ultra-550b-a55b:free` at temperature 0 is doing a
judgment-heavy synthesis on a 20k-token prompt. Rounds 1 and 2 are necessary
regardless of model. Round 1 showed the model *can* do the domain reasoning —
coverage was good and unprompted risks like bank-detail diversion appeared — so
the remaining defects look more like instruction-following than capability.
Still worth a diff on a stronger model, but no longer the obvious lever.

Round 3 reinforces this and adds one correction. The operator failures were not a
capability problem — no model can guess a closed vocabulary that is stated
nowhere — so a stronger model would not have fixed them. The correction: a
provider-enforced JSON Schema is sometimes proposed as the answer here, and it is
**net-new work**, not a config flag. `app/llm.py` has no `response_format` or
`json_schema` path at all; only `tools` is wired (and does work against this
provider — `finish_reason: tool_calls` appears in the call log). Worth doing on
its own merits, but it would not have repaired an ambiguous contract.

## How to evaluate a re-run

Regenerate the RCM for `Workspaces/procurement` and check, against the criteria.
Baselines: round 0 = the original 15 rows, round 1 = the 52-row regeneration.

| # | Check | Round 0 | Round 1 | Round 2 |
|---|---|---|---|---|
| 1 | Risk statements containing a `%`, column name, or digit | 5 of 15 | 0 of 52 | ? |
| 2 | **Control** fields containing a `%` or column name | — | 22 of 52 | ? |
| 3 | Controls asserting unevidenced system enforcement | — | 9 of 52 | ? |
| 4 | `assertion` values outside the closed list | — | 23 of 52 | ? |
| 5 | `control_owner` naming a role absent from the SOP | — | ~14 of 52 | ? |
| 6 | Aspirational controls that do not exist | ~9 of 15 | 0 | ? |
| 7 | Duplicate pairs across processes | 1 of 15 | 4 of 52 | ? |
| 8 | Standard risk classes present (SoD, splitting, bank change, variance, unapproved vendor) | none | all | ? |
| 9 | DoA row asserts the matrix's contents | yes, wrongly | no | ? |

Two specific regressions to re-check by name, since both were contradicted by the
data in round 1: `RCM-22DA16` ("system prevents same user ID from populating
both" — 1 requisition has verifier = approver) and `RCM-73FC2F` ("only active
vendors selectable" — 1 PO to a non-`Active` vendor). Also `RCM-0E9E69`, which
read 4 `Rejected` requisitions as approval gaps.

The known exceptions in the data — listed in *Coverage gaps* above — are a ready
answer key for whether a generated row is testable and whether its test would
find anything.

## Verification commands

Row-level checks used across both rounds, against
`Workspaces/procurement/Planning/RCM/`:

```python
import json, glob, re
rows = [json.load(open(f)) for f in glob.glob("RCM-*.json")]
pct, caps = re.compile(r"\d+(\.\d+)?\s*%"), re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
ASSERTIONS = {"Existence", "Completeness", "Accuracy", "Authorization",
              "Valuation", "Cut-off", "Compliance", "Operational"}

print("risk  leaks:", [r["id"] for r in rows
                       if pct.search(r["risk"]) or caps.search(r["risk"])])
print("ctrl  leaks:", [r["id"] for r in rows
                       if pct.search(r["control"]) or caps.search(r["control"])])
print("bad assertions:", {r["assertion"] for r in rows} - ASSERTIONS)
print("no control:", [r["id"] for r in rows
                      if r["control"].strip().lower().startswith("no control identified")])
```

Factual claims in a `control` field must be re-performed against
`Workspaces/procurement/Data/*.xlsx` with polars — round 1 produced two
fabricated statistics that only a re-performance caught.
