# Findings Consolidation

How related exception findings get recognised, proposed as one, and merged
under auditor control. Companion to
[audit-workflow-graph.md](audit-workflow-graph.md) (stage reference) and
[agent-architecture-review-2026-09.md](agent-architecture-review-2026-09.md)
(the review this design came out of).

- [1. Problem](#1-problem)
- [2. High-level design](#2-high-level-design)
- [3. Detailed implementation plan](#3-detailed-implementation-plan)
- [4. Open questions](#4-open-questions)

---

## 1. Problem

The procurement engagement has 18 drafted findings covering roughly 11
distinct issues. The same records appear under several titles:

| Records | Findings drafted |
| --- | --- |
| INV2024114, INV2024129, INV2024143 — paid before goods receipt | 3 |
| INV2024106, INV2024122, INV2024140 — invoice exceeds PO total | 2 |
| INV2024118, INV2024136 — invoice dated after receipt | 2 |
| INV2024150 / vendor V0977 — vendor not active | 3, at requisition, PO, and payment |

Two mechanisms produce this, and they are different problems.

**Intra-row duplicates.** Test generation wrote several tests for the same
control assertion on one RCM row. RCM-37662A carries three "payment before
goods receipt" tests, RCM-DE2F19 three "invoice over PO" tests, RCM-DDD0C1
three "vendor not active" tests. Each test becomes an observation
(`rcm_execution._rollup_datatest` writes one per test), each exception
observation becomes a finding unit (`capabilities/reporting._finding_units`).
The tests flag identical record sets. No judgment is involved: they are the
same measurement made three times.

**Cross-row, shared cause.** The vendor example. Requisition, purchase order,
and payment controls each fail on the same inactive vendor. These are
genuinely different control failures with one root cause. Whether the report
carries them as one finding with a shared cause, or three findings
cross-referenced, is an audit judgment.

Three facts about the current design make the duplication inevitable:

- The finding worker (`workers/reporting.run_finding_worker`) sees one
  observation, its row, its test, its execution result, and its exception
  rows. It cannot see sibling findings — the same unit-isolation limit the
  workflow doc records for test generation.
- `backend/app/data_test_redundancy.py` already computes the intra-row
  signal: post-run overlap of flagged records on an entity key, with
  `identical` / `subsumed_by` / `overlaps` relations and `DUP-` groups. It
  marks 9 of procurement's 30 tests `duplicate`. Neither the roll-up, the
  findings capability, nor the UI reads the mark.
- The report's only defence is an advisory `duplicate_finding` warning when
  two confirmed titles have a `SequenceMatcher` ratio of at least 0.75. It
  fires after the fact and merges nothing.

One prerequisite: in the procurement data every agent-drafted finding has an
empty `rcm_refs`, and all 14 RCM ids the observations point at no longer
exist after the matrix was regenerated. Grouping by RCM row is therefore not
a safe key today. Consolidation keys on test ids and entity ids.

---

## 2. High-level design

### 2.1 Principles

- **Deterministic where the evidence is deterministic.** Identical record
  sets are a fact the executed frames establish. No model is asked whether
  three tests that flagged the same three invoices are the same issue.
- **A model proposes, an auditor consolidates.** Cross-row grouping is a
  root-cause claim. The model is shown the deterministic overlaps and asked
  which of them are one finding; the auditor accepts, edits, or dismisses.
  Nothing on a finding changes until the auditor acts.
- **Mark, never delete.** An absorbed finding stays on disk as the record of
  what its control showed. It leaves the report, not the workspace. This is
  the redundancy module's stance, applied one level up.
- **Consolidate at the finding level, not the observation level, for
  cross-row groups.** The per-observation drafts are evidence of each control's
  result and may be wanted separately. Merging with consent preserves that
  choice; merging before drafting would remove it.

### 2.2 Three layers

```text
results.rolled_up ──► findings.drafted ──► findings.consolidated ──► report.working_draft
      │                     │                      │
      │ (1) collapse         │ one draft per        │ (2) one model turn over every
      │ intra-row dup        │ uncovered obs        │ draft: proposes groups
      │ observations         │                      │ (proposal-only)
      │ using redundancy     │                      │
      │ marks (no model)     │                      ▼
      │                      │             (3) Findings page: suggested groups
      │                      │                 accept → lead finding + absorbed
      │                      │                 dismiss → recorded against basis
      ▼                      ▼                      ▼
   observations           findings            report carries leads only
   covered_by             consolidation{}
```

**Layer 1 — collapse intra-row duplicates at roll-up.** `rcm_execution.rollup`
reads each data test's `redundancy` mark. When two tests on the same row
stand in an `identical` or `subsumed_by` relation with `confirmed`
confidence, one observation per group is kept as lead; the rest are written
with `covered_by: <lead observation id>`, outcome unchanged. Finding
expansion skips covered observations. No model turn, no approval; in
procurement this removes six of the seven surplus drafts before drafting.

**Layer 2 — `findings.consolidated`, a model pass that sees every draft.**
A new capability after `findings.drafted` and before `report.working_draft`.
One unit per engagement. Context is, per draft finding, its title, severity,
RCM process and control text, test title, and the flagged **entity ids only**
from the run's `exception_profile`, plus a deterministic overlap table the
adapter computes (every pair of findings sharing entity ids, with count and
Jaccard). The worker returns groups with a relation (`same_condition` or
`shared_cause`), a lead, a proposed title, a root-cause hypothesis, and a
rationale; the validator refuses a group the overlap table does not support.
The unit is proposal-only: the proposal is the durable suggestion set.

**Layer 3 — review on the Findings page.** A "Suggested consolidations"
panel lists each proposed group. Accept merges into the lead (refs unioned,
narrative redrafted by the existing finding worker from all member
observations); absorbed findings are marked, not deleted. Dismiss is recorded
against the group's basis hash so it is not re-raised until the findings
change. Manual grouping uses the same merge path. The report carries leads
only; absorbed findings appear under their lead as supporting procedures.

### 2.3 Data model

Observation (`Observations/<id>.json`), new optional field:

```json
"covered_by": "OBS-…"            // lead observation for an intra-row duplicate
```

Finding (`Findings/<id>.json`), new optional field:

```json
"consolidation": {
  "group_id": "CG-3F2A1C",
  "role": "lead" | "absorbed",
  "into": "F-…",                 // absorbed only
  "members": ["F-…", "F-…"],     // lead only; absorbed finding ids
  "relation": "same_condition" | "shared_cause",
  "basis_sha1": "…",             // the suggestion basis this decision answered
  "decided_by": "auditor" | "agent",
  "decided_at": "2026-…"
}
```

Consolidation suggestion set (proposal sidecar of the unit, and mirrored as
`Findings/.consolidation/<basis_sha1>.json` on accept/dismiss so the decision
outlives the run):

```json
{
  "basis_sha1": "…",
  "groups": [
    {
      "group_id": "CG-3F2A1C",
      "finding_ids": ["F-59EBEB", "F-9EFDB0", "F-F0C84A"],
      "lead_finding_id": "F-9EFDB0",
      "relation": "shared_cause",
      "proposed_title": "Transactions processed for vendors not in Active status",
      "root_cause_hypothesis": "Vendor master status is not enforced at requisition, PO, or payment release.",
      "rationale": "All three flag vendor V0977 / INV2024150; controls sit at successive stages of one cycle.",
      "shared_entities": {"VENDOR_ID": ["V0977"], "INVOICE_ID": ["INV2024150"]},
      "decision": null | "accepted" | "dismissed"
    }
  ],
  "singletons": ["F-0571DE", "…"]
}
```

The basis hash covers the sorted draft finding ids and each finding's
execution-result hashes, the same pattern `planning.change_assessed` uses, so
the suggestion is re-asked only when the finding set moves.

### 2.4 Graph placement

```text
findings.drafted ──► findings.consolidated ──► report.working_draft
                                         (partial edge: an unreviewed
                                          suggestion never withholds the report)
```

- Depends on `findings.drafted`. Readiness: `satisfied` when every group in
  the current-basis suggestion set has a decision, or no suggestion set is
  needed (fewer than two draft findings, or no overlaps); `review_required`
  when undecided groups exist; `missing` when no suggestion exists for the
  current basis.
- On no template by default. Added to `full_audit_working_draft` after
  `findings.drafted`, and requestable by name (`findings.consolidated`).
- Stage settles `review_required` when the auditor has not decided. The
  report proceeds on the partial edge and includes every undecided draft, as
  it does today, so nothing regresses when the review is skipped.

### 2.5 Privacy

The cross-finding pass needs record identifiers across tests. The existing
door `allow_datatest_exception_rows` admits whole rows to one finding draft; a
consolidation pass must not see rows, only keys. A new representation
`datatest_exception_keys` under a new permission
`allow_datatest_exception_keys`, capped per finding, admits the values of the
run's `entity_key` column and nothing else. Overlaps are computed locally in
the adapter and supplied as counts and shared ids.

### 2.6 What changes for the auditor

- Fewer drafts appear in the first place (layer 1).
- After drafting, the Findings page shows suggested groups with the shared
  records and a proposed combined title; accepting produces one finding whose
  Condition lists each instance by control stage and whose Root Cause states
  the shared cause.
- Nothing merges without a click. A dismissed suggestion stays dismissed
  until the findings change.
- The report's Key Findings table and Detailed Findings carry lead findings
  only; each lead lists its absorbed members under "Supporting procedures".

---

## 3. Detailed implementation plan

Five phases. Each ships on its own and is useful on its own. Phase 0 is a
prerequisite bug fix; phases 1 and 2 are backend; 3 is frontend; 4 is the
report.

### Phase 0 — make finding refs stable

Findings must carry the RCM and test refs the executor is documented to
derive, and consolidation must survive a regenerated matrix.

1. **Diagnose empty `rcm_refs`.** `executors/reporting.execute_finding`
   derives `rcm_refs=[observation.rcm_id]`; the procurement findings have
   `[]`. Reproduce with `test_agent_reporting_executor.py` against an
   observation whose row exists, and against one whose row was regenerated
   (id no longer in `workspace.rcm`). `findings._validate_links` *raises*
   on an unknown RCM ref rather than dropping it, so the empty refs were
   written empty at draft time or cleared afterwards; find which. Whatever
   the cause, a finding must keep the ref to a row that later disappears and
   carry an `evidence_warnings` entry, rather than losing it.
2. **Key on `semantic_id`.** RCM rows carry `semantic_id = rcm:<process>:<risk>`;
   a regenerated row keeps it. Add `rcm_semantic_refs` alongside `rcm_refs`
   on findings and observations, so process grouping in the report and in
   consolidation survives a redraft. Populate in the roll-up and the finding
   executor.
3. **Tests.** `test_rcm_execution.py`: observation carries the row's
   semantic id. `test_agent_reporting_executor.py`: a finding drafted against
   a row, then the row regenerated, still resolves a process.

### Phase 1 — collapse intra-row duplicates at roll-up

Files: `backend/app/rcm_execution.py`, `backend/app/data_test_redundancy.py`,
`backend/app/agent/capabilities/_shared.py`, `backend/app/findings.py`.

1. **Expose a per-row grouping helper** in `data_test_redundancy`:

   ```python
   def row_duplicate_groups(workspace, rcm_id) -> list[list[str]]:
       """Test ids on this row that flag the same records, lead first."""
   ```

   Built from the persisted `redundancy` marks (`annotate` already runs
   after every data-test run via `data_tests._redundancy_sweep`). A group is
   the connected set of tests on the row joined by `confirmed` `identical` or
   `subsumed_by` peers. Lead order: the subsuming test first, then the test
   with the earliest `created`, then id. A `subsumed_by` chain resolves to
   the outermost subsuming test.
2. **Write `covered_by` in `rcm_execution.rollup`.** After `_rollup_test` has
   run for every test on the row, look up the row's groups and, for each
   observation whose test is a non-lead member, set
   `observation["covered_by"] = <lead observation id>`; clear the field on
   every other observation of the row (a mark that was earned last run may
   not hold this run). Include `covered_by` in the material projection so
   the roll-up persists when only coverage changed.
3. **Skip covered observations in expansion.** `_shared.eligible_observations`
   adds `and not item.get("covered_by")`. `findings.observation_support_issues`
   adds an issue when a finding's source observation is now covered, so an
   already-drafted duplicate surfaces as a support issue rather than being
   silently kept.
4. **Redundancy sweep timing.** The sweep runs after each data-test run, but
   the roll-up may run before the last sweep on a resumed run. Call
   `data_test_redundancy.annotate(workspace, persist=False)` at the top of
   `rollup` when any data test's `redundancy.result_sha1` differs from its
   `last_run` result hash, and use the returned marks.
5. **Readiness and narration.** `findings.drafted` readiness details gain
   `covered: n`. The roll-up milestone (`audit_execution.milestone_projection`
   for `results.rolled_up`) says "n observations covered by a duplicate test
   on the same row".
6. **Tests.** `test_data_test_redundancy.py`: `row_duplicate_groups` on
   identical, subsumed chain, overlap-only (no group), and cross-row (no
   group). `test_rcm_execution.py`: three tests flagging the same rows on one
   row yield one uncovered observation; re-running after one test is
   retired clears `covered_by`. `test_agent_capabilities_tests.py` or a new
   `test_agent_capabilities_reporting.py`: `_finding_units` skips covered
   observations; a finding whose observation became covered reports a
   support issue.
7. **Expected effect on procurement:** 18 drafts become 12.

### Phase 2 — `findings.consolidated` capability

Files: `backend/app/agent/workflows/audit.py`,
`backend/app/agent/capabilities/reporting.py`,
`backend/app/agent/context/{model,presets,adapters}.py`,
`backend/app/agent/workers/reporting.py`,
`backend/app/agent/audit_execution.py`, `backend/app/findings.py`,
`backend/app/agent/operations.py`, `frontend/src/components/agent/capabilityLabels.ts`.

1. **Graph.** In `workflows/audit.py`:

   ```python
   "findings.consolidated": ("findings.drafted",),
   "report.working_draft": (
       "planning.apm_ready", "results.rolled_up",
       "findings.drafted", "findings.consolidated",
   ),
   ```

   Add `"findings.consolidated"` to `FULL_AUDIT_OUTCOMES` after
   `findings.drafted`. In `audit_execution._PARTIAL_DEPENDENCIES`:
   `"report.working_draft": {"findings.drafted", "findings.consolidated"}`.
   `definition_hash()` moves; `test_workflow_audit_definition.py` baseline
   edges are updated in the same change. `capabilityLabels.ts` gains the
   label "Finding consolidation".
2. **Basis and store.** New module `backend/app/finding_consolidation.py`
   (mirrors `planning_delta.py`):

   ```python
   FOLDER = ".consolidation"                         # Findings/.consolidation/
   def basis_sha1(workspace) -> str                  # sorted draft finding ids
                                                     # + each execution_refs' result_sha1
   def load(workspace, basis) -> dict | None
   def save(workspace, basis, suggestion, *, run_id) -> dict
   def decide(workspace, basis, group_id, decision) -> dict
   def draft_findings(workspace) -> list[dict]       # not absorbed, not confirmed? (see §4)
   ```
3. **Capability declaration** in `capabilities/reporting.py`:

   ```python
   def _consolidation_ready(workspace, scope) -> Readiness:
       drafts = finding_consolidation.draft_findings(workspace)
       if len(drafts) < 2: return Readiness("satisfied", details={"drafts": len(drafts)})
       basis = finding_consolidation.basis_sha1(workspace)
       suggestion = finding_consolidation.load(workspace, basis)
       if suggestion is None: return Readiness("missing", ("draft findings have not been reviewed for consolidation",))
       undecided = [g for g in suggestion["groups"] if not g.get("decision")]
       if undecided: return Readiness("review_required", (f"{len(undecided)} suggested consolidations await a decision",))
       return Readiness("satisfied", details={"groups": len(suggestion["groups"])})

   def _consolidation_units(workspace, scope) -> list[UnitSpec]:
       # one unit, only when the basis has no suggestion (or force)
       ...UnitSpec("finding_consolidation", "finding_consolidation",
                   "Review findings for consolidation",
                   tuple(f"finding:{f['id']}" for f in drafts),
                   {"basis_sha1": basis, "finding_ids": [...]})
   ```

   `Capability(..., context="reporting.finding_consolidation",
   invalidate_on=("finding",), produces=(), accepts_refs=("finding",))`.
   Register it in the reporting group's `CAPABILITY_IDS` and `_BUILDERS`.
4. **Privacy door.** `context/model.py`: add
   `allow_datatest_exception_keys: bool = False` beside
   `allow_datatest_exception_rows`, with the same "separate on purpose"
   comment. `context/presets.py` `_REPRESENTATION_PRIVACY_FIELD`: add
   `"datatest_exception_keys": "allow_datatest_exception_keys"`.
5. **Preset** `reporting.finding_consolidation` in `presets.py`:

   | Source | Required | Representation | Budget |
   | --- | --- | --- | --- |
   | `draft_findings` | yes | `current_artifact` (id, title, severity, process, control text, test title, entity_key) | 60 / 48k |
   | `finding_exception_keys` | yes | `datatest_exception_keys` (per finding: key column, up to 50 ids) | 60 / 24k |
   | `finding_overlaps` | yes | `current_artifact` (pairs: ids, shared count, Jaccard, shared ids up to 10) | 200 / 24k |
   | `instruction` | no | `planning_context` | 1 / 2k |

   Permissions: `document_text`, `datatest_exception_keys`,
   `auditor_instruction`. Global budget 320 / 100k.
6. **Adapter** `adapters.finding_consolidation_scope(workspace, *, instruction)`:
   - For each draft finding, load the execution result behind each
     `execution_refs` entry (`data_tests.load_result`), read
     `exception_profile.entity_key`, and take the distinct values of that
     column from `exception_frame`. Document-test findings contribute the
     document ids of their mismatched items as the key set under
     `DOCUMENT_ID`.
   - Compute pairwise overlaps on the same key name: shared count, Jaccard,
     up to 10 shared ids. Keep pairs with at least one shared id.
   - Also emit "same process" pairs (from `rcm_semantic_refs`, Phase 0) with
     zero shared ids, flagged `basis: "process"`, so the model can propose a
     `shared_cause` group that the auditor may still accept, but the validator
     can tell an entity-backed group from a process-only one.
   - Add a `context_reads` narration record ("read n draft findings and the
     records they flagged").
7. **Worker** `workers/reporting.py`, `CONSOLIDATION_WORKER_ID = "reporting.finding_consolidation"`:
   - System prompt tag `[agent:finding_consolidation]`. Asks for JSON:
     `{groups[]: {finding_ids, lead_finding_id, relation, proposed_title,
     root_cause_hypothesis, rationale, basis: "entity"|"process"},
     singletons[]}`.
   - Validator (`validate_consolidation_proposal`): every id is a supplied
     draft; every finding appears in exactly one group or in `singletons`;
     each group has at least two members and its lead is a member; a
     `same_condition` group must be `basis: "entity"` and every pair in it
     must appear in the overlap table with Jaccard ≥ 0.5; a `shared_cause`
     group must have every pair either in the overlap table or sharing a
     process; `rationale` non-empty. One repair. Semantic check on.
   - Register in `WORKERS`; add to
     `test_agent_worker_frozen_proposals.py`'s pinned identity list.
8. **Binder** `audit_execution._bind_finding_consolidation`, following
   `_bind_delta_review`: proposal-only (`executor_id=None`), approval kind
   `finding_consolidation` in permission mode, `on_committed` writes the
   proposal through `finding_consolidation.save` with every group
   `decision: null`, then narrates "Suggested n consolidations across m
   findings". Register in `_PIPELINE_BINDERS` with
   `{"worker": "reporting.finding_consolidation", "executor": None}`.
9. **Budget.** `routing._audit_model_turns` and
   `audit_execution._refresh_dynamic_limits` add `+ 1` turn when there are
   two or more draft findings.
10. **Milestone.** `milestone_projection` case for `findings.consolidated`:
    headline "Consolidation review", stats: groups suggested, entity-backed,
    process-only, undecided.
11. **Operations index.** `operations.py` gains the capability with
    `accepts_refs=("finding",)` so the loop's `plan_outcomes` can name it.
12. **Tests.** `test_agent_reporting_consolidation.py` (new): worker validator
    accepts a well-formed proposal, rejects an unknown id, a singleton group,
    a `same_condition` group without entity overlap, and a finding listed
    twice; adapter overlap table on two findings sharing two invoice ids;
    readiness across missing / review_required / satisfied; basis changes
    when a finding is confirmed or redrafted. `test_workflow_audit_definition.py`:
    edge and template updates. `test_agent_capability_composition.py`:
    identity pins for the new capability.

### Phase 3 — review mechanism on the Findings page

Files: `backend/app/findings.py`, `backend/app/routes/report_routes.py`,
`backend/app/agent/workers/reporting.py`, `backend/app/agent/context/adapters.py`,
`backend/app/agent/executors/reporting.py`, `frontend/src/types.ts`,
`frontend/src/components/FindingsTab.vue`, a new
`frontend/src/components/findings/ConsolidationPanel.vue`.

1. **API.**

   | Route | Does |
   | --- | --- |
   | `GET /findings/consolidation` | current basis, suggestion set (or null), each group with member summaries and shared entities |
   | `POST /findings/consolidation/refresh` | queue a `findings.consolidated` run via the assistant command path (same as "Generate all findings" does today for drafts) |
   | `POST /findings/consolidation/{group_id}/accept` | body `{lead_finding_id?, title?}`; merges; returns the lead finding |
   | `POST /findings/consolidation/{group_id}/dismiss` | records the decision |
   | `POST /findings/consolidate` | body `{finding_ids, lead_finding_id, relation, title?}`; manual group through the same merge |
   | `POST /findings/{id}/unconsolidate` | restores an absorbed finding to draft and removes it from its lead's members |

   Extend the `GET /findings` payload with `consolidation` per finding so the
   list can badge leads and hide absorbed by default.
2. **Merge** `findings.consolidate(workspace, *, finding_ids, lead_id,
   relation, group_id, basis, title=None, decided_by)`:
   - Refuse if any member is `auditor_confirmed` unless the caller passes
     `include_confirmed=True` (the UI asks first).
   - Lead: union `rcm_refs`, `rcm_semantic_refs`, `test_refs`,
     `execution_refs`, `evidence_refs` (dedupe by anchor id), `procedure_refs`;
     `severity` = max over members; `consolidation = {role: lead, members,
     relation, group_id, basis_sha1, decided_by, decided_at}`;
     `auditor_confirmed = False`; `cause_pending` as before.
   - Absorbed: `consolidation = {role: absorbed, into: lead_id, …}`,
     `auditor_confirmed = False`. Narrative untouched.
   - Record the decision on the suggestion set (`finding_consolidation.decide`)
     when `group_id` came from one; for a manual group, append a group with
     `decision: "accepted"` and `decided_by: "auditor"`.
   - `findings.support_issues`: an absorbed finding is never "unsupported"
     on account of being absorbed; a lead reports an issue if any member's
     source observation is no longer an exception.
   - `findings.update` patch allowlist: `consolidation` is not patchable;
     it changes only through the routes above.
3. **Redraft the lead narrative.** Extend the finding draft path to accept
   several observations:
   - `adapters.finding_draft_scope(workspace, observation_id, *, instruction,
     sibling_observation_ids=())`: when siblings are supplied, `observation`
     becomes a list projection, `execution_result` and `exception_rows` are
     supplied per observation (same caps, per member), and a new optional
     source `consolidation_brief` carries the group's relation, proposed title
     and root-cause hypothesis. Preset `reporting.finding_draft` gains that
     optional source; its permissions do not change.
   - `workers/reporting.run_finding_worker`: prompt states that when several
     observations are supplied the Condition section must list each instance
     by control stage and the Root Cause must state the shared cause; the
     validator additionally requires every member test id to be named in the
     narrative.
   - `executors/reporting.execute_finding`: when the target names a lead with
     members, write the narrative onto the lead and keep refs as unioned by
     `consolidate`; `finding_semantic_id` for a lead redraft is
     `finding:consolidated:<group_id>`.
   - Trigger: `accept` enqueues a `findings.drafted` run scoped to
     `finding:<lead_id>` (naming a finding already means "redraft it"); the
     binder passes `sibling_observation_ids` from the lead's members. Until
     the redraft lands the lead keeps its original narrative and shows a
     "narrative pending redraft" badge.
4. **UI.** `ConsolidationPanel.vue` above the findings list:
   - Header: basis state ("Suggestions current" / "Findings changed since
     last review — refresh"), a Refresh button (`/consolidation/refresh`),
     and counts.
   - One card per undecided group: relation chip (`same condition` /
     `shared cause`), member titles with severity, the shared records
     (entity key → ids), proposed title (editable), root-cause hypothesis,
     rationale. Lead selector defaults to the model's pick. Buttons: Accept,
     Dismiss.
   - Multi-select in the findings list gains "Consolidate selected…", which
     opens the same card in manual mode.
   - `FindingsTab.vue`: absorbed findings hidden behind a "Show absorbed (n)"
     toggle; a lead shows a "Consolidated: n procedures" badge and lists its
     members with links; an absorbed finding shows "Absorbed into F-…" with
     Restore. `confirmAll` skips absorbed findings.
   - `types.ts`: `FindingConsolidation`, `ConsolidationGroup`,
     `ConsolidationPayload`; `AuditFinding.consolidation?`.
5. **Tests.** `test_findings.py` (new or extend `test_report.py`):
   consolidate unions refs and takes max severity; unconsolidate restores;
   consolidating a confirmed finding is refused without the flag; support
   issues on leads. `test_agent_reporting_finding.py`: multi-observation
   draft names every member test; `consolidation_brief` reaches the prompt.
   Route tests for accept / dismiss / manual / restore.

### Phase 4 — the report

Files: `backend/app/report.py`, report template.

1. **Inclusion.** `supported` (`report.py` ~483) excludes findings whose
   `consolidation.role == "absorbed"`.
2. **Supporting procedures.** In Detailed Findings, after a lead's
   narrative, render "Supporting procedures" listing each absorbed member's
   title and test refs. In the Key Findings table a lead counts once.
3. **Quality checks.** Keep `duplicate_finding` as the last-resort title
   check. Add `unreviewed_consolidation` (advisory) when the current basis
   has undecided groups, and `absorbed_finding_confirmed` (blocking) if an
   absorbed finding is somehow confirmed.
4. **Working papers.** `reporting.working_paper` projections list a row's
   findings including absorbed ones with their `into` link, so the row's
   paper still shows what its own test found.
5. **Tests.** `test_report.py`: absorbed findings are excluded; lead renders
   its supporting procedures; the advisory check fires and clears.

### Phase 5 — roll-out and clean-up

- Run Phase 1 against the five local engagements and record the before/after
  draft counts in this document.
- Update [audit-workflow-graph.md](audit-workflow-graph.md): the graph, the
  stage reference (`findings.consolidated`), the worker table, the preset
  inventory, the privacy table (fourth door), the partial-dependency table,
  and `TEMPLATE_OUTCOMES`.
- Update `AGENTS.md` privacy boundary: three row-level doors become three
  plus one key-level door.
- Consider retiring the `SequenceMatcher` title check once the advisory
  consolidation check has run on real engagements.

### Estimated size

| Phase | Backend | Frontend | Tests |
| --- | --- | --- | --- |
| 0 | small | — | small |
| 1 | ~150 lines | — | ~120 |
| 2 | ~600 lines (module, capability, preset, adapter, worker, binder) | label only | ~250 |
| 3 | ~350 lines (merge, routes, multi-observation draft) | ~450 lines | ~200 |
| 4 | ~120 lines | — | ~80 |

---

## 4. Open questions

1. **Confirmed findings in a group.** Should the model be shown confirmed
   findings at all? Proposal: yes, as candidates, but the UI asks before
   merging a confirmed one, and the merged lead is always unconfirmed.
2. **Cycle-vouch observations.** One per dispositioned item today, so one
   cycle test with N exception items yields N findings. Layer 1 cannot
   collapse them (different records by design). Either the roll-up writes
   one observation per cycle test with the items as details, or layer 2 is
   allowed a third relation, `same_test`, that groups them without an entity
   overlap. The first is cleaner and is a roll-up change, not a
   consolidation change.
3. **Severity of a lead.** Max over members is the safe default. A
   `shared_cause` group spanning three control stages may warrant a higher
   rating than any member; that is the auditor's edit, not the merge's.
4. **Where the redraft runs.** Accept enqueues a scoped `findings.drafted`
   run, which is a full command run for one turn. If that feels heavy in
   the UI, the alternative is a registered action the steering loop's
   `ActionExecution` can run inline; the executor and worker are the same
   either way.
5. **Process-only groups.** Should the model be allowed to propose a
   `shared_cause` group with no shared entity at all (two different vendors
   failing the same status check at two stages)? The design says yes with
   `basis: "process"` visible to the auditor; the threshold can be tightened
   after the first engagements.
