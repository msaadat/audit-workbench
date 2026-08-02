# RCM generation quality: what was wrong, what was fixed, what is left

**Status:** two rounds of changes have landed. Round 1 (changes 1–5) fixed
coverage and risk wording; round 2 (changes 9–11) fixed the defects round 1
introduced. Both rounds have been evaluated against a live regeneration.
Recommendation 7 is now the highest-value open item; 6 and 8 are deferred.

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

### 6. Split the turn into two passes (deferred after round 1)

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

**Now the highest-value open item.** Rounds 1 and 2 have taken prompt-only fixes
about as far as they go: each round removed one defect class and the model found
an adjacent field to express it in. A deterministic gate does not move.

Cheap gates in `app/agent/workers/planning.py`, which already has the repair loop
(`max_repair_attempts=1`) to feed violations back:

- Reject `%` or an `ALL_CAPS_UNDERSCORE` token in **either** `risk` or
  `control`. Would have caught 22 round-1 rows. Scope it to both fields from the
  start — round 1's lesson is that a single-field rule migrates.
- Reject an `assertion` outside the closed list. Pure enum check, no false
  positives, would have caught 23 round-1 rows. **Do this one first** — it is
  the cheapest and the highest-yield.
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
