# Data Analysis Consolidation Plan

**Status:** Proposed; ready for implementation  
**Date:** 2026-07-28  
**Primary objective:** Replace the separate Analysis and Data Tests tabs with one
**Data Analysis** tab backed by one durable Data Test model and one local execution
path.

## 1. Executive decision

Audit Workbench will expose one user-facing **Data Analysis** tab.

Every saved data procedure will be a durable Data Test. Its scope is derived
from RCM linkage:

- `rcm_id == null` means the Data Test is **exploratory**;
- a valid `rcm_id` means the Data Test is an **RCM-linked audit test**.

There will be no persisted `purpose`, `scope`, or `is_exploratory` flag. API
responses may expose a derived `scope` value for display and filtering, but
`rcm_id` is the only source of truth. This prevents linkage and classification
from disagreeing.

The consolidation is not a cosmetic tab merge. The active `analyses` artifact
collection, saved-analysis CRUD, separate analysis execution contract, and
Analysis-tab editors will be retired after their definitions are migrated to
unlinked Data Tests. The exploratory data-analysis workflow will generate and
execute unlinked Data Tests through the same services used by auditor-authored
and RCM-linked tests.

The following distinctions remain deliberate:

- exploratory analysis generation and RCM test generation use different model
  workers and different context;
- both workers write the same durable Data Test artifact;
- every execution is local and deterministic after the definition exists;
- only a current, RCM-linked execution may satisfy RCM coverage or support a
  formal finding;
- any Data Test, exploratory or linked, may be pinned to the dashboard;
- linking, unlinking, or moving a test to another RCM row changes its audit
  meaning and therefore requires a new execution before it can count as current.

## 2. Current problem

The product currently presents two surfaces with nearly identical authoring and
execution affordances:

| Capability | Analysis | Data Tests |
| --- | --- | --- |
| Library analytics | Yes | Yes |
| Custom Polars/Python | Yes | Yes |
| Model-generated definitions | Yes | Yes |
| Local execution | Yes | Yes |
| Saved rerunnable definitions | Yes | Yes |
| Dashboard pinning | Yes | Yes |
| Unlinked exploratory work | Yes | Yes |
| RCM linkage | No | Yes |
| Durable exception result | Partial bounded result | Yes |
| Auditor disposition | No | Yes |
| Formal execution/finding chain | No | Yes |

The overlap is structural:

- `frontend/src/components/AnalysisTab.vue` exposes Library and Code creation;
- `frontend/src/components/DataTestsTab.vue` exposes Library analytics and
  Polars code creation;
- both use the analytics registry and guarded local sandbox;
- `workspace.analyses` and `workspace.data_tests` persist two representations of
  rerunnable data procedures;
- the exploratory workflow commits `workspace.analyses`, while the audit
  workflow commits `workspace.data_tests`;
- the Data Tests UI already labels an unlinked record “Exploratory,” so the
  separate Analysis tab no longer owns a unique user intent.

The durable Data Test contract is the stronger base. It already owns definition
validation, current execution metadata, dataset fingerprints, semantic issues,
bounded result frames, RCM linkage, dispositions, evidence, and finding
integration. Saved analyses should be migrated into that contract rather than
preserved as a second active model.

## 3. Product invariants

Implementation must preserve the following invariants.

### 3.1 One classification rule

```text
rcm_id is null       -> exploratory
rcm_id is valid      -> RCM-linked
anything else        -> invalid record
```

The derived scope must be computed in one backend helper and mirrored by one
frontend helper. Filters, labels, actions, RCM rollups, finding gates, dashboard
curation, and workflow readiness must use that rule.

### 3.2 One active durable collection

After migration, new data procedures are stored only under `DataTests/`.

`Workspace.analyses`, `Analyses/`, and `/api/workspaces/{id}/analyses` are not
valid active write targets. No compatibility layer may dual-write an Analysis
and a Data Test.

### 3.3 One execution service

All saved procedures execute through `backend/app/data_tests.py`:

- definition validation happens before save;
- execution is explicit and never implied by save;
- execution runs locally with Polars;
- current results are committed with parent-hash and workspace-revision guards;
- result frames remain bounded;
- source specifications and input datasets are fingerprinted;
- semantic diagnostics remain deterministic;
- a run error becomes a durable review-required result rather than a false pass.

An unsaved editor preview may continue to use a local stateless endpoint, but it
must be labelled **Preview** and must never count as a durable execution.

### 3.4 RCM linkage is the audit boundary

An exploratory Data Test:

- does not appear in RCM coverage;
- is not included by “Run all RCM Data Tests”;
- cannot supply a formal finding execution reference;
- cannot be selected by deterministic RCM dashboard curation;
- may still be run, edited, exported, and manually pinned;
- may be linked to an RCM row without cloning the definition.

An RCM-linked Data Test:

- appears in the linked RCM row’s `test_refs`;
- participates in fieldwork readiness and rollup;
- may support a finding only after a current, semantically valid execution;
- exposes criteria, disposition, conclusion, evidence, and finding actions.

### 3.5 Linkage changes invalidate audit currency

Changing `rcm_id` from null to a row, from one row to another, or from a row to
null must:

1. update both old and new `rcm[].test_refs`;
2. preserve the saved definition;
3. preserve the prior result file for display until the next run;
4. mark the definition `ready`;
5. mark the prior result stale for the new linkage;
6. prevent the prior result from satisfying RCM coverage or supporting a new
   finding;
7. require a new run to establish a current result under the new linkage.

The execution identity must therefore include the current `rcm_id`, including
null, in `source_sha1` or a dedicated `scope_sha1`. Comparing only engine,
tables, and spec is insufficient.

If a Data Test is already referenced by a formal finding, changing or clearing
its RCM link must fail with an actionable error until the finding references are
removed or reconciled. A mutation must not silently break an existing evidence
chain.

### 3.6 Model generation remains bounded and role-specific

The exploratory worker:

- receives declared table metadata, bounded profiles, value-free aggregates,
  relationship evidence, the analytics registry, and current unlinked tests;
- never receives table rows;
- proposes unlinked Data Tests only;
- cannot set `rcm_id`, auditor disposition, conclusion, or finding fields.

The RCM test worker:

- receives one RCM row plus its declared methodology, table metadata, and
  document inventory context;
- produces linked Data or Document Tests;
- continues to generate exception-oriented Polars steps for Data Tests.

These are two generation intents, not two artifact types.

### 3.7 Privacy remains unchanged

No consolidation work may relax the current row-level privacy boundary:

- analysis-definition model context remains metadata-only;
- RCM test-generation context remains bounded and row-free;
- table rows are available only to local Polars execution;
- durable context manifests remain content-free;
- result frames are local artifacts and are never added to model context by this
  plan.

## 4. Target information architecture

### 4.1 Navigation

Replace both current navigation entries with one entry:

```text
Data
  Documents
  Tables
  Query
  Data Analysis

Plan
  APM
  RCM

Fieldwork
  Document tests
```

`Data Analysis` lives in the Data group because it is the common home for
exploration and formal data testing. RCM and dashboard actions deep-link into
the RCM-linked view, so moving the navigation entry does not hide fieldwork.

The canonical query-string tab is:

```text
tab=data-analysis
```

Supported destination-owned query values are:

```text
test=<DAT-id>
scope=all|exploratory|rcm
create=analytics|validation|polars
rcm=<RCM-id>
```

Compatibility redirects:

- `tab=analysis` redirects to `tab=data-analysis&scope=exploratory`;
- `tab=data-tests` redirects to `tab=data-analysis`, retaining `test`, `create`,
  and `rcm`;
- a deep link with a concrete `test` takes precedence over the requested scope
  filter so the target is always visible;
- RCM actions use `scope=rcm` and pass the target `rcm` or `test`.

### 4.2 Page structure

The page uses one master-detail layout.

Header:

- title: **Data Analysis**;
- description: “Explore imported data or execute RCM-linked audit tests with
  durable local results”;
- primary action: **New analysis**;
- secondary action, when an RCM filter is active: **Run RCM tests**.

Scope switch:

- **All**;
- **Exploratory**;
- **RCM-linked**.

Filters:

- RCM row;
- engine;
- execution status;
- verdict;
- created by;
- text search over ID, title, objective, and table names.

Rail item:

- title;
- `Exploratory` or `RCM <id>` badge derived from `rcm_id`;
- engine badge;
- execution/staleness status;
- latest verdict when current;
- table or “All workspace tables” summary;
- created-by indicator.

The rail sorts by:

1. items requiring review;
2. stale linked tests;
3. ready linked tests;
4. completed linked tests;
5. exploratory tests;
6. most recently updated within each group.

### 4.3 Creation flow

The create menu contains:

- **Library analytic**;
- **Validation rules**;
- **Polars procedure**.

Every creation flow shows an optional **RCM row** selector at the top.

If no RCM row is selected:

- the dialog labels the record **Exploratory**;
- objective is required;
- criteria is optional and hidden under advanced fields;
- disposition and conclusion fields are absent;
- helper text states that the result will not count as audit coverage or support
  a formal finding.

If an RCM row is selected:

- the dialog labels the record **RCM-linked audit test**;
- objective and criteria are required;
- the parent risk and control are summarized above the definition;
- helper text states that saving validates the definition but execution is
  still required.

Newly created records are saved first and run only when the auditor selects
**Run**. The create dialog must not combine save and execution into one action.

### 4.4 Detail view

Common header actions:

- Save definition;
- Run;
- Export current result;
- Pin current result;
- Delete.

Common definition fields:

- title;
- objective;
- engine;
- table references where the engine requires them;
- executable specification;
- optional visualization configuration;
- created-by and generation provenance.

Exploratory-only presentation:

- prominent `Exploratory` badge;
- **Link to RCM** action;
- result section title **Exploratory result**;
- returned rows are labelled **Result rows**, not audit exceptions;
- no auditor disposition, control conclusion, Open RCM, or Draft finding action;
- a warning explains that pinning is presentational and does not create audit
  evidence.

RCM-linked presentation:

- prominent `RCM <id>` badge;
- **Open RCM** and **Change RCM link** actions;
- criteria, methodology, scope limitations, next action, disposition, and
  control conclusion;
- result section title **Durable test result**;
- returned rows are labelled **Exceptions**;
- **Draft finding** remains unavailable until the current execution is eligible;
- any stale-linkage or stale-dataset reason is shown before result content.

### 4.5 Linking and unlinking interaction

**Link to RCM** opens a confirmation dialog containing:

- selected RCM risk and control;
- the Data Test objective;
- the fact that the existing result will not count until re-run;
- validation issues that must be fixed before linking.

On success:

- the existing record is updated in place;
- the page switches to the RCM-linked scope;
- status becomes ready;
- **Run now** is the primary next action.

**Remove RCM link** requires confirmation and states that the test will become
exploratory and stop contributing to RCM coverage. The action is disabled with
an explanation when formal findings depend on the link.

Moving a test between RCM rows uses the same guarded transition and requires a
new execution.

### 4.6 Result terminology

The durable result JSON may continue to use `exception_count` for compatibility,
but the UI language depends on linkage:

| Stored field | Exploratory label | RCM-linked label |
| --- | --- | --- |
| `exception_frame` | Result rows | Exceptions |
| `exception_count` | Result row count | Exception count |
| `completed_with_exception` | Completed with result rows | Completed with exceptions |
| `completed_no_exception` | Completed with no result rows | Completed with no exceptions |
| `review_required` | Review required | Review required |

No derived label changes the stored result or RCM eligibility. Eligibility is a
separate deterministic predicate.

## 5. Target durable model

### 5.1 Data Test definition

The existing Data Test remains the authoritative record:

```json
{
  "id": "DAT-...",
  "semantic_id": "datatest:...",
  "rcm_id": null,
  "title": "Duplicate invoice identifiers",
  "objective": "Identify repeated invoice identifiers.",
  "criteria": "",
  "engine": "analytics",
  "table_refs": ["invoice_data"],
  "spec": {
    "test_id": "duplicates",
    "params": {
      "columns": ["invoice_no"]
    }
  },
  "viz": {
    "type": "table"
  },
  "steps": [],
  "methodology_refs": [],
  "status": "ready",
  "semantic_warnings": [],
  "last_run": null,
  "auditor_disposition": "pending",
  "evidence_refs": [],
  "created_by": "agent",
  "agent_run_id": "...",
  "workflow_parent_sha1": "...",
  "migration": null,
  "created": "...",
  "updated": "..."
}
```

Model changes:

- add a validated `viz` definition field so existing saved-analysis
  visualizations survive migration and dashboard pinning;
- add system-owned optional `migration` provenance containing only legacy ID,
  legacy semantic ID, and migration version;
- expose derived `scope: "exploratory" | "rcm"` in list/detail payloads;
- expose derived `result_current`, `result_stale_reasons`, and
  `finding_eligible`;
- do not add a persisted purpose or scope flag;
- keep `created_by` and `agent_run_id` as the authoritative author/generation
  provenance.

`migration` is not accepted from normal create or patch APIs.

### 5.2 Definition validation by scope

Common validation:

- title and objective are required;
- engine must be `analytics`, `validation`, or `polars`;
- analytics and validation require valid table references;
- Polars code must pass the sandbox validator;
- referenced tables and columns must exist;
- a definition must contain at least one executable analytic, rule, or step.

Additional RCM-linked validation:

- `rcm_id` must exist;
- criteria must be non-empty before the test may count as specified;
- every Polars step instruction must describe exception semantics;
- Polars output must be a DataFrame whose rows represent failed items;
- the current definition must pass semantic preflight;
- an invalid or incomplete linked definition may be stored as draft but is not
  executable or coverage-eligible.

Exploratory records may have blank criteria and may use broader informational
language. They still use safe Polars and bounded results.

### 5.3 Current result

Continue storing one replaceable current result under:

```text
DataTestResults/<DAT-id>/DTR-CURRENT.json
```

Add or enforce:

- `rcm_id` captured at execution, including null;
- a definition/source hash that includes `rcm_id`;
- dataset fingerprints for every frame the code could access;
- `scope_at_run: exploratory|rcm` as a derived snapshot for diagnostics;
- result eligibility computed from current definition, current linkage, current
  input fingerprints, semantic validity, and run status;
- no result history claim: the active model retains one current result, except
  older immutable files that remain referenced by findings.

Changing only narrative outcome fields does not invalidate execution. Changing
engine, table references, executable spec, or `rcm_id` does.

### 5.4 RCM and finding eligibility

Create one backend predicate, for example:

```python
data_tests.result_eligibility(workspace, item, result) -> {
    "current": bool,
    "rcm_eligible": bool,
    "finding_eligible": bool,
    "stale_reasons": list[str],
}
```

`rcm_eligible` requires:

- non-null valid `rcm_id`;
- executable non-draft definition;
- current result created under the same `rcm_id`;
- current source hash and dataset fingerprints;
- semantic validity;
- terminal completed status.

`finding_eligible` additionally requires:

- exceptions or another explicit supported finding basis;
- an auditor disposition that permits follow-up;
- no unresolved invalid-test classification;
- a valid immutable result hash.

`rcm_execution.py`, `findings.py`, dashboard curation, the Data Analysis UI, and
report-quality gates must all call this predicate rather than restating partial
logic.

## 6. API contract

### 6.1 Canonical resource routes

Keep Data Tests as the backend resource name. The user-facing tab is Data
Analysis, but every saved record remains a Data Test:

```text
GET    /api/workspaces/{workspace_id}/data-tests
POST   /api/workspaces/{workspace_id}/data-tests
GET    /api/workspaces/{workspace_id}/data-tests/{data_test_id}
PATCH  /api/workspaces/{workspace_id}/data-tests/{data_test_id}
DELETE /api/workspaces/{workspace_id}/data-tests/{data_test_id}
POST   /api/workspaces/{workspace_id}/data-tests/{data_test_id}/run
GET    /api/workspaces/{workspace_id}/data-tests/{data_test_id}/runs/{run_id}
GET    /api/workspaces/{workspace_id}/data-tests/{data_test_id}/runs/{run_id}/export
POST   /api/workspaces/{workspace_id}/data-tests/{data_test_id}/pin
POST   /api/workspaces/{workspace_id}/data-tests/run-all-rcm
```

Do not add a parallel `/data-analysis` resource namespace. The tab name is an
information-architecture label, while `data-tests` is the durable domain
resource.

### 6.2 List response

The list endpoint returns:

```json
{
  "items": [],
  "counts": {
    "all": 0,
    "exploratory": 0,
    "rcm": 0,
    "review_required": 0,
    "stale": 0
  }
}
```

Each item includes the derived fields from section 5.1. Filtering may remain
client-side initially because the collection is workspace-bounded, but the API
shape must not require a second analyses request.

### 6.3 Create and update behavior

Create:

- missing or blank `rcm_id` is normalized to null;
- a supplied `rcm_id` is validated and linked atomically;
- saving never executes;
- provenance fields remain server-owned;
- `scope` supplied by a client is rejected or ignored because it is derived.

Update:

- uses existing optimistic workspace revision handling;
- detects an `rcm_id` transition before mutation;
- rejects linkage changes that would break formal findings;
- synchronizes RCM `test_refs` in the same transaction;
- marks execution stale/ready when executable scope changes;
- returns the derived eligibility projection.

### 6.4 Export

Preserve Analysis-tab export capability by adding a Data Test result export:

- analytics exports the deterministic analytic result;
- validation exports rule summaries and bounded failures;
- Polars exports the current bounded summary/result frame;
- export uses the current saved definition and verifies its source identity;
- a stale result may be exported only with a filename and workbook note that
  identify it as stale;
- export is local and does not call the model.

### 6.5 Retired routes

Remove mutating saved-analysis routes after workspace migration:

```text
GET/POST/PATCH/DELETE /api/workspaces/{workspace_id}/analyses...
```

Keep the stateless analytics catalog and execution helpers because Data Test
authoring uses them:

```text
GET  /api/analytics
POST /api/workspaces/{workspace_id}/tables/{table}/analytics/{test_id}
```

The local `/run-python` endpoint may remain for unsaved previews and assistant
artifacts, but the Data Analysis UI must clearly distinguish preview from a
durable Data Test run.

## 7. Exploratory workflow target

### 7.1 New workflow identity

The persistence target and artifact references change materially, so new runs
must use a new workflow definition:

```text
workflow id: data_analysis_workflow_v1

data.relationships_inferred
-> data.joins_ready
-> data_analysis.tests_ready
-> data_analysis.executed
```

Goal-template routing remains:

```text
data_analysis -> data_analysis.executed
table_relationships -> data.joins_ready
```

Completed `analysis_workflow_v1` runs remain readable history. They are not
resumed or retried in place after cutover. A retry creates a new
`data_analysis_workflow_v1` run. Any live legacy workflow run encountered during
upgrade is marked interrupted with an actionable “restart data analysis” reason
rather than being dispatched into incompatible executors.

### 7.2 Worker

Replace the saved-analysis response contract with an exploratory Data Test
contract.

The worker returns one to four definitions containing:

- title;
- objective;
- engine: `analytics` or `polars`;
- engine-specific spec;
- optional visualization;
- stable semantic ID derived from target frame, engine, and canonical spec.

The worker does not return:

- `rcm_id`;
- status;
- disposition;
- result;
- evidence or finding references;
- provenance fields.

The executor forcibly supplies `rcm_id=None` even if a malformed proposal
contains a linkage field.

For model-generated Polars definitions:

- emit Data Test schema-version-2 steps directly;
- every step has a label, instruction, and safe code;
- code assigns a DataFrame to `result`;
- the prompt may describe the output as relevant rows rather than confirmed
  control exceptions because the test is exploratory;
- execution and UI apply the exploratory terminology rules.

### 7.3 Context

Rename or replace the current-analysis context source with current exploratory
Data Tests:

- include only `workspace.data_tests` where `rcm_id is None`;
- include definition metadata and canonical spec, never results or rows;
- include legacy-migrated definitions so generation does not duplicate them;
- continue supplying schema, bounded profile, value-free aggregates, analytics
  registry, and deterministic relationship evidence;
- preserve all current character, token, item, and table-scope bounds.

### 7.4 Executor

The definition executor:

- validates proposals through the Data Test service;
- assigns stable `DAT-...` IDs;
- writes `DataTests/`, not `Analyses/`;
- reconciles by semantic ID;
- preserves auditor-edited records unless overwrite was explicitly approved;
- emits artifact references `datatest:<id>`;
- records `workspace_changed.kind = "datatest"`.

The execution capability:

- expands only workflow-authored unlinked Data Tests in scope;
- calls `data_tests.compute` and `data_tests.commit_result`;
- persists the normal current Data Test result, not a second bounded
  `last_result` shape;
- does not run RCM-linked tests merely because they share a table;
- settles an invalid result as review-required without converting it into audit
  coverage.

### 7.5 Audit workflow

The audit workflow’s `tests.specified` worker and fieldwork executors remain
separate and continue to create RCM-linked Data Tests.

Required integration changes:

- shared creation and spec-validation helpers must accept explicit system-owned
  provenance from either workflow;
- audit readiness ignores unlinked exploratory tests;
- an exploratory test linked by the auditor becomes eligible for the audit
  workflow only after it is re-run;
- test generation must not overwrite a linked auditor-owned definition;
- forcing exploratory analysis must not regenerate or edit RCM-linked tests.

## 8. Workspace migration

### 8.1 Migration policy

Existing saved Analysis definitions are valuable and must be migrated. Existing
Analysis execution metadata is not sufficient to fabricate a durable Data Test
result because it may omit dataset fingerprints, the executable scope identity,
bounded exception frames, and the RCM scope snapshot.

Therefore:

- migrate definitions;
- preserve visualization and provenance;
- do not claim prior Analysis execution as a current Data Test run;
- set migrated tests to `ready`;
- require an explicit new Data Test run;
- keep dashboard tiles unchanged because they are independent copies;
- preserve legacy evidence anchors through a read-only archive when necessary.

### 8.2 Schema/version gate

Implement migration through the existing workspace artifact migration path under
the workspace write lock:

1. bump `SCHEMA_VERSION`;
2. detect an active `Analyses/.index.json` or legacy inline `analyses` collection;
3. build the complete migration projection without writing;
4. validate every target Data Test;
5. detect ID and semantic collisions;
6. stage all new Data Test files and the new index;
7. write a migration mapping;
8. rewrite supported references;
9. atomically publish the Data Test collection;
10. archive only legacy definitions needed for evidence compatibility;
11. remove the active Analyses index and unreferenced files;
12. update the workspace schema version last.

The migration must be idempotent after interruption. Reopening a workspace must
either finish the same deterministic mapping or recognize that each target
already exists.

### 8.3 Deterministic ID mapping

For every legacy Analysis ID:

```text
DAT-<first 10 uppercase hex characters of
     SHA1("legacy-analysis:" + legacy_analysis_id)>
```

If that ID already belongs to a record whose migration metadata names the same
legacy ID, reuse it. Any other collision is a migration error; do not silently
choose a random ID.

Use a unique migrated semantic ID:

```text
datatest:legacy-analysis:<lowercase legacy id>
```

Preserve the legacy semantic ID in system-owned migration metadata so the new
exploratory worker can de-duplicate against the canonical spec as well as the
migrated identity.

### 8.4 Field mapping

| Legacy Analysis | Data Test |
| --- | --- |
| `id` | deterministic new `DAT-...`; old ID stored in `migration` |
| `semantic_id` | stored in `migration.legacy_semantic_id` |
| `title` | `title` |
| `note` | `objective` when non-empty |
| blank `note` | objective `"Explore: <title>"` |
| `kind=analytics` | `engine=analytics` |
| `spec.test` | `spec.test_id` |
| `spec.params` | `spec.params` |
| analytics `table` | `table_refs=[table]` |
| `kind=python` | `engine=polars` |
| Python `spec.code` | one schema-version-2 Polars step |
| Python table label | no restrictive `table_refs`; code keeps access to workspace frames |
| `viz` | `viz` |
| `source=ai` | `created_by=agent` |
| `source=library|code` | `created_by=user`, unless existing provenance proves agent creation |
| `agent_run_id` | `agent_run_id` |
| `created` | `created` |
| `last_result` | not migrated as a current execution |
| any prior execution timestamp | optional diagnostic in `migration`, not execution evidence |
| no RCM field | `rcm_id=null` |

The migrated Polars step uses:

- label: legacy title;
- instruction: migrated objective;
- code: unchanged after sandbox validation;
- a deterministic step ID.

If legacy code fails current sandbox validation, migrate it as a visible draft
with a specific semantic warning. Do not drop the record and do not execute it.

### 8.5 Evidence compatibility

Some older workspaces may contain typed evidence anchors with
`source_kind="analysis"`.

Because a saved Analysis anchor identifies a definition rather than a
fingerprinted Data Test execution, it cannot be rewritten to a `datatest`
execution anchor without inventing evidence.

Migration must:

- identify every legacy Analysis referenced by an evidence anchor;
- copy its exact canonical JSON into
  `Legacy/AnalysisEvidence/<analysis-id>.json`;
- retain its original hash;
- teach the evidence resolver to read this directory only for existing
  `source_kind="analysis"` anchors;
- remove Analysis from new evidence-picker options;
- forbid creation of new Analysis anchors;
- mark the legacy anchor as definition-only support in report QA;
- allow the compatibility reader to be removed only after no workspace contains
  such anchors.

This archive is not an active collection: it is not listable, editable,
runnable, or supplied to a model.

### 8.6 Agent-run compatibility

Existing run records and sidecars may contain `analysis:<id>` artifact refs.
They remain historical records and are not rewritten.

At upgrade:

- completed legacy runs remain readable;
- paused, awaiting-approval, or interrupted `analysis_workflow_v1` runs are
  closed as interrupted with a migration reason;
- proposal and receipt sidecars remain under their original run;
- retry creates a new workflow run against migrated Data Tests;
- no new executor attempts to reconcile an `analysis:<id>` receipt against a
  `datatest:<id>` parent.

### 8.7 Migration verification

For each workspace, verify:

- migrated Data Test count equals legacy Analysis count, excluding only
  explicitly diagnosed collisions;
- every migrated record is unlinked;
- every migrated analytics spec canonicalizes;
- every migrated Polars spec is either valid or a visible draft with a warning;
- no current Data Test result was fabricated;
- every legacy evidence anchor resolves to the archive;
- dashboard tiles are byte-for-byte unchanged except normal workspace revision;
- `Analyses/.index.json` is no longer active;
- a second load performs no writes.

## 9. Backend implementation work packages

### WP0 — Pin contracts before mutation

- [ ] Add tests for `rcm_id`-derived scope.
- [ ] Add tests proving client-supplied scope cannot override linkage.
- [ ] Add tests for exploratory exclusion from RCM coverage and findings.
- [ ] Add tests for linkage-change result invalidation.
- [ ] Add tests for finding-dependent unlink rejection.
- [ ] Add a migration fixture containing analytics and Python analyses,
      visualizations, provenance, a pinned tile, and a legacy evidence anchor.
- [ ] Capture current analysis-workflow routing and privacy behavior.

**Gate:** the tests describe the target contract and fail only where the current
dual model is expected to change.

### WP1 — Strengthen the Data Test domain service

Touchpoints:

- `backend/app/data_tests.py`
- `backend/app/rcm_execution.py`
- `backend/app/findings.py`
- `backend/app/dashboard.py`
- `backend/app/workspaces.py`
- `backend/app/workspace_transactions.py`

Tasks:

- [ ] Add the single derived-scope helper.
- [ ] Add `viz` and system-owned migration provenance validation.
- [ ] Separate common definition validation from RCM-linked eligibility.
- [ ] Include `rcm_id` in execution/source identity.
- [ ] Add the shared result-eligibility predicate.
- [ ] Return current/stale/eligibility projections from `list_payload`.
- [ ] Mark a result stale and status ready on any linkage transition.
- [ ] Guard linkage changes used by formal findings.
- [ ] Keep RCM `test_refs` synchronized transactionally.
- [ ] Ensure table rename rewrites every Polars `spec.steps[].code`, not a
      retired single `spec.code` field.
- [ ] Ensure table deletion and join deletion mark affected exploratory and
      linked definitions stale consistently.
- [ ] Make RCM rollup call the shared eligibility predicate.
- [ ] Make finding support and dashboard curation call the same predicate.
- [ ] Keep `run_all_rcm_linked` restricted to non-null RCM IDs and skip drafts.

**Gate:** Data Tests alone can represent, execute, filter, link, unlink, roll up,
and pin both scopes without reading `workspace.analyses`.

### WP2 — Consolidate APIs

Touchpoints:

- `backend/app/routes/data_test_routes.py`
- `backend/app/routes/analyses_routes.py`
- `backend/app/routes/analysis_routes.py`
- `backend/app/routes/assistant_routes.py`
- `backend/app/main.py`

Tasks:

- [ ] Add derived list counts and eligibility fields.
- [ ] Add current result export.
- [ ] Enforce linkage transition errors through the patch route.
- [ ] Preserve stateless analytics metadata and preview routes.
- [ ] Update OpenAPI tags/descriptions to “Data Analysis” while keeping the
      `data-tests` resource path.
- [ ] Remove the saved-analysis router from `main.py` after migration is active.
- [ ] Delete saved-analysis mutating routes.
- [ ] Return actionable not-found/migrated errors for stale external requests if
      a temporary compatibility response is required.

**Gate:** the frontend needs only Data Test CRUD plus stateless authoring helpers.

### WP3 — Migrate workspace storage

Touchpoints:

- `backend/app/workspaces.py`
- `backend/tests/test_workspace_artifact_storage.py`
- `backend/tests/test_workspaces.py`
- new focused migration tests

Tasks:

- [ ] Bump the workspace schema version.
- [ ] Implement deterministic Analysis-to-Data-Test mapping.
- [ ] Stage and publish migration atomically.
- [ ] Persist the legacy-to-new ID mapping.
- [ ] Archive only evidence-referenced definitions.
- [ ] Stop hydrating `Workspace.analyses`.
- [ ] Remove `analyses` from `_ARTIFACT_COLLECTIONS`.
- [ ] Remove `analyses` from save, sync, table-rename, table-delete, and
      semantic-lookup paths.
- [ ] Update workspace summaries and mutation counters.
- [ ] Prove idempotence after simulated interruption at each publish boundary.

**Gate:** every supported workspace loads with one active data-procedure
collection and no definition loss.

### WP4 — Replace the exploratory workflow persistence target

Touchpoints:

- `backend/app/agent/workflows/analysis.py`
- `backend/app/agent/capabilities/analysis.py`
- `backend/app/agent/workers/analysis.py`
- `backend/app/agent/executors/analysis.py`
- `backend/app/agent/analysis_execution.py`
- `backend/app/agent/context/presets.py`
- analysis context adapters and selectors
- `backend/app/agent/routing.py`
- `backend/app/agent/workflow_dispatch.py`
- `backend/app/agent/store.py`

Tasks:

- [ ] Register `data_analysis_workflow_v1`.
- [ ] Register `data_analysis.tests_ready` and
      `data_analysis.executed`.
- [ ] Change current-definition context to unlinked Data Tests.
- [ ] Change the worker schema to Data Test definitions.
- [ ] Change executor refs from `analysis:` to `datatest:`.
- [ ] Commit definitions through Data Test validation and transactions.
- [ ] Execute through Data Test compute/commit.
- [ ] Preserve semantic de-duplication and auditor-edit protection.
- [ ] Keep relationship and join inference unchanged.
- [ ] Keep one bounded model turn per target frame.
- [ ] Update model budgets from the same resolved table scope.
- [ ] Route `data_analysis` goals only to the new workflow.
- [ ] Close live legacy workflow runs safely during migration.
- [ ] Update run projections, artifact labels, and UI stage labels.

**Gate:** “analyze these tables” creates and runs only unlinked Data Tests, and
the run uses the same result artifacts as manual Data Analysis execution.

### WP5 — Update isolated actions and assistant artifacts

Touchpoints:

- `backend/app/agent/actions.py`
- `backend/app/agent/action_runner.py`
- `backend/app/assistant_chats.py`
- `frontend/src/components/agent/ChatArtifactCard.vue`
- `frontend/src/components/agent/AgentTaskList.vue`

Tasks:

- [ ] Retarget “run this saved analysis” to an unlinked Data Test.
- [ ] Retarget “pin this analysis” to the Data Test pin endpoint.
- [ ] Save assistant-generated code/analytic artifacts as unlinked Data Tests.
- [ ] Require a title and objective when saving an artifact.
- [ ] Convert one-code artifacts into schema-version-2 Polars steps.
- [ ] Replace `analysis:<id>` action refs with `datatest:<id>`.
- [ ] Preserve query artifacts as queries; do not force them into Data Tests.
- [ ] Update action target resolution and artifact labels.
- [ ] Remove action access to `workspace.analyses`.

**Gate:** no active assistant or ActionRunner path can create or mutate a saved
Analysis.

### WP6 — Build the Data Analysis frontend

Touchpoints:

- replace `frontend/src/components/DataTestsTab.vue` with a renamed or new
  `frontend/src/components/DataAnalysisTab.vue`;
- reuse/refactor `frontend/src/components/data-tests/AnalyticsTestAuthor.vue`;
- migrate useful editor and visualization pieces from
  `frontend/src/components/analysis/`;
- `frontend/src/views/WorkspaceView.vue`;
- `frontend/src/composables/useWorkspaceNavigation.ts`;
- `frontend/src/types.ts`;
- `frontend/src/components/DashboardTab.vue`;
- `frontend/src/components/PlanningTab.vue`;
- `frontend/src/components/ReportTab.vue`;
- `frontend/src/components/FindingsTab.vue`.

Tasks:

- [ ] Add the single Data Analysis navigation entry and panel.
- [ ] Add legacy query-string redirects.
- [ ] Add scope switch, counts, and granular filters.
- [ ] Show derived linkage badges and result currency.
- [ ] Restore Validation as a visible creation mode.
- [ ] Reuse one analytics catalog component.
- [ ] Reuse one Polars step editor.
- [ ] Preserve useful Analysis visualization, preview, and export affordances.
- [ ] Add guarded Link to RCM, Change RCM, and Remove RCM actions.
- [ ] Switch result terminology based on `rcm_id`.
- [ ] Hide formal audit controls for exploratory records.
- [ ] Disable finding actions when shared eligibility says false.
- [ ] Deep-link RCM and dashboard actions into the correct scope.
- [ ] Update workspace-changed subscriptions to `datatest`.
- [ ] Update empty states, tooltips, keyboard labels, and responsive layout.
- [ ] Remove `SavedAnalysis` frontend types.

**Gate:** there is one user-facing route for all saved data procedures and no
feature regression in authoring, running, result review, export, or pinning.

### WP7 — Remove the retired Analysis model

Delete only after WP1–WP6 gates pass:

- [ ] `frontend/src/components/AnalysisTab.vue`;
- [ ] retired files under `frontend/src/components/analysis/` after reusable
      pieces have moved;
- [ ] `backend/app/routes/analyses_routes.py`;
- [ ] saved-analysis CRUD in `Workspace`;
- [ ] saved-analysis payload helpers in `dashboard.py`;
- [ ] old analysis definition/execution executor contracts;
- [ ] old active workflow composition and dispatch registration;
- [ ] `SavedAnalysis` and `AnalysisLastResult` frontend interfaces;
- [ ] tests whose sole purpose was the retired model.

Then:

- [ ] replace deleted-model tests with consolidation and migration tests;
- [ ] run static searches for active `workspace.analyses`, `/analyses`, and
      `analysis:<id>` writes;
- [ ] retain only intentional uses of the word analysis, such as Data Analysis,
      document analysis, debug labels, historical runs, and legacy evidence.

**Gate:** no production write path, UI route, workflow executor, or current
artifact reference uses the retired saved-analysis model.

### WP8 — Documentation and rollout

- [ ] Update `README.md`.
- [ ] Update `AGENTS.md` product shape and architecture inventory.
- [ ] Update `docs/agent-architecture.md`.
- [ ] Update `docs/agent-runtime-active-surface-inventory.md`.
- [ ] Mark section 6.1 of `docs/rcm-central-workflow-plan.md` implemented and
      record the final Data Analysis label.
- [ ] Document the linkage-derived scope rule in API and user help.
- [ ] Add release notes explaining redirects and migrated definitions.
- [ ] Add a recovery note for interrupted legacy analysis runs.

**Gate:** architecture, product copy, and code describe the same one-model
system.

## 10. Test plan

### 10.1 Data Test service tests

Extend `backend/tests/test_data_tests.py` to prove:

- unlinked create returns derived exploratory scope;
- linked create returns derived RCM scope;
- a supplied contradictory scope cannot alter classification;
- both scopes execute through the same service;
- exploratory runs never enter RCM rollup;
- linking a previously run exploratory test marks the result stale;
- re-running after linking makes it eligible;
- unlinking removes RCM coverage immediately;
- moving between RCM rows updates both rows and requires re-run;
- finding-dependent linkage changes are rejected;
- table/spec/link changes invalidate currency;
- narrative conclusion changes do not invalidate execution;
- validation engine remains supported;
- Polars step table references are rewritten on table rename;
- dashboard pinning works for both scopes;
- automatic curation considers linked eligible results only.

### 10.2 Migration tests

Add a focused migration suite with fixtures for:

- library analytics analysis;
- hand-written Python analysis;
- agent-generated Python analysis;
- saved visualization;
- blank and nonblank notes;
- valid and now-invalid code;
- prior bounded `last_result`;
- pinned dashboard copy;
- legacy evidence anchor;
- duplicate titles;
- partial previous migration;
- deterministic ID collision;
- crash after staged files but before schema bump.

Assertions:

- every definition is represented exactly once;
- all migrated tests are unlinked;
- none has a fabricated current result;
- provenance and visualization survive;
- invalid code remains visible as draft;
- evidence archives retain original hashes;
- dashboard tiles do not change;
- repeat load is write-free.

### 10.3 Workflow tests

Replace or rewrite `backend/tests/test_workflow_analysis.py` to prove:

- the new graph and workflow hash are stable;
- deterministic relationship evidence is unchanged;
- joins are still materialized only under the existing evidence gate;
- the worker receives no rows;
- proposals create only unlinked Data Tests;
- executor refs are `datatest:`;
- definitions resume from sidecars without re-billing;
- executions use Data Test current results;
- auditor edits are preserved;
- force regeneration remains scoped;
- RCM-linked tests are never picked up by exploratory execution;
- model budgets and concurrency remain bounded;
- old live workflow runs do not dispatch into the new executors.

Keep audit workflow tests proving RCM generation still creates linked tests.

### 10.4 API tests

Cover:

- combined list counts;
- derived scope;
- create/update/run/result/export/pin;
- optimistic concurrency during linkage changes;
- validation errors for missing RCM and broken findings;
- removed saved-analysis routes;
- stateless analytics and preview routes still work;
- legacy tab redirects are frontend behavior, not duplicate APIs.

### 10.5 Evidence and report tests

Cover:

- exploratory results cannot satisfy formal finding execution refs;
- linked current results can;
- stale linkage results cannot;
- legacy Analysis anchors still resolve read-only;
- new Analysis anchors are rejected;
- report QA distinguishes legacy definition-only support;
- RCM working papers include only eligible linked results.

### 10.6 Frontend verification

At minimum:

- TypeScript typecheck through the existing production build script;
- production Vite build;
- manual desktop verification at wide and narrow breakpoints;
- keyboard navigation through scope switch, rail, create menu, and RCM dialogs;
- legacy `tab=analysis` redirect;
- legacy `tab=data-tests&test=...` redirect;
- RCM deep link to a linked test;
- creation and execution of all three engines;
- link, re-run, unlink, and blocked-unlink flows;
- exploratory versus linked terminology;
- dashboard pin navigation;
- finding-action eligibility.

### 10.7 Full regression commands

Run the repository’s established equivalents of:

```bash
cd backend
pytest

cd ../frontend
npm run build
```

Also run focused static searches:

```bash
rg -n "workspace\\.analyses|/analyses|analysis:<" backend/app frontend/src
rg -n "AnalysisTab|SavedAnalysis|AnalysisLastResult" frontend/src backend/app
```

Every remaining match must be documented as Data Analysis terminology,
document analysis, historical compatibility, or legacy evidence.

## 11. End-to-end acceptance scenarios

### Scenario A — Manual exploration

1. Import a table.
2. Open Data Analysis.
3. Create a library analytic without an RCM link.
4. Save; confirm status is ready and scope is exploratory.
5. Run; confirm a durable local result.
6. Pin it.
7. Confirm it does not appear in RCM coverage and cannot draft a finding.

### Scenario B — Promote exploration into fieldwork

1. Open the exploratory test from Scenario A.
2. Link it to an RCM row.
3. Confirm the prior result is visibly stale and does not count.
4. Add/confirm criteria.
5. Re-run.
6. Confirm RCM coverage, result rollup, Open RCM, and eligible finding actions.
7. Confirm the same `DAT-...` ID was retained.

### Scenario C — RCM-generated test

1. Generate tests for an RCM row.
2. Confirm the Data Test appears in Data Analysis under RCM-linked.
3. Confirm model-generated Polars steps are visible.
4. Run locally.
5. Confirm exceptions, semantic diagnostics, disposition, and RCM rollup.

### Scenario D — Exploratory workflow

1. Ask the assistant to infer joins and analyze selected tables.
2. Confirm deterministic relationship handling.
3. Confirm generated definitions are unlinked Data Tests.
4. Confirm their results use `DataTestResults/`.
5. Confirm no `Analyses/` artifact is created.

### Scenario E — Migration

1. Open a pre-consolidation workspace containing saved analyses.
2. Confirm each appears as an exploratory Data Test.
3. Confirm titles, code/analytic specs, visualizations, and provenance.
4. Confirm old bounded results are not misrepresented as current executions.
5. Run one migrated definition successfully.
6. Restart and confirm the migration does not run again.

### Scenario F — Evidence protection

1. Create a formal finding supported by a linked current Data Test result.
2. Attempt to remove or change the test’s RCM link.
3. Confirm the mutation is rejected with the dependent finding identified.
4. Reconcile the finding, then repeat the linkage change successfully.

## 12. Definition of done

The consolidation is complete only when:

- one Data Analysis tab is visible;
- all saved data procedures are Data Tests;
- `rcm_id` alone determines exploratory versus RCM-linked scope;
- the same local execution path serves both scopes;
- linkage changes cannot reuse an execution under a different audit meaning;
- exploratory definitions and results cannot satisfy RCM or finding gates;
- exploratory workflow output lands in `DataTests/`;
- RCM test-generation output continues to land in `DataTests/`;
- migrated saved Analysis definitions remain usable;
- no prior Analysis execution is fabricated into stronger evidence;
- dashboard pinning, validation authoring, visualization, and export are
  preserved;
- old navigation links redirect correctly;
- no active saved-analysis CRUD, storage, workflow executor, or frontend model
  remains;
- all backend tests and the frontend production build (including its TypeScript
  typecheck) pass;
- architecture and product documentation describe the consolidated system.

## 13. Recommended delivery sequence

Deliver in four reviewable changes:

1. **Domain and migration foundation**
   - WP0–WP3;
   - no navigation change yet;
   - prove the Data Test model and migration independently.

2. **Workflow convergence**
   - WP4–WP5;
   - new exploratory runs create Data Tests;
   - old Analysis UI may temporarily be read-only during this change.

3. **Data Analysis UI cutover**
   - WP6;
   - one navigation entry and redirects;
   - full authoring, execution, linking, export, and pinning.

4. **Retirement and documentation**
   - WP7–WP8;
   - delete the old active model only after all prior gates pass;
   - run the full regression and static-boundary suite.

Do not combine the migration, workflow persistence change, and old-model
deletion into one unreviewable change. Each delivery must leave workspace
storage valid and must have an explicit rollback boundary.
