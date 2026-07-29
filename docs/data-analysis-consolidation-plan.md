# Data Analysis Consolidation Plan

**Status:** Proposed; ready for implementation  
**Date:** 2026-07-28  
**Primary objective:** Replace the separate Analysis and former Data Tests tabs with one
**Data Analysis** tab backed by one durable Data Analysis model and one local execution
path.

## 1. Executive decision

Audit Workbench will expose one user-facing **Data Analysis** tab.

Every saved data procedure will be a durable Data Analysis record. Its scope is derived
from RCM linkage:

- `rcm_id == null` means the record is **exploratory**;
- a valid `rcm_id` means the record is an **Audit Test**.

There will be no persisted `purpose`, `scope`, or `is_exploratory` flag. API
responses may expose a derived `scope` value for display and filtering, but
`rcm_id` is the only source of truth. This prevents linkage and classification
from disagreeing.

The consolidation is not a cosmetic tab merge. The active `analyses` artifact
collection, saved-analysis CRUD, separate analysis execution contract, and
Analysis-tab editors are retired outright. The exploratory data-analysis
workflow will generate and execute unlinked records through the same services
used by auditor-authored Audit Tests.

The consolidation also renames the surviving durable model. Today that model is
named "Data Test" throughout the stack — `backend/app/data_tests.py`,
`DataTests/`, `workspace.data_tests`, `datatest:<id>`, `DataTestsTab.vue`. The
consolidated product name is **Data Analysis**, so the rename is part of this
work and is delivered first, mechanically, as WP-1 in section 9. Every
target-state name in this document is the post-rename name; section 2, which
describes the current problem, uses the current names.

This plan assumes a **clean cutover**. Pre-consolidation workspaces are not
migrated, and no legacy definition, result, evidence anchor, or run record is
carried forward. See section 8.

The following distinctions remain deliberate:

- exploratory analysis generation and RCM test generation use different model
  workers and different context;
- both workers write the same durable Data Analysis artifact;
- every execution is local and deterministic after the definition exists;
- only a current Audit Test execution may satisfy RCM coverage or support a
  formal finding;
- any Data Analysis record, exploratory or Audit Test, may be pinned to the dashboard;
- linking, unlinking, or moving a test to another RCM row changes its audit
  meaning and therefore requires a new execution before it can count as current.

## 2. Current problem

The product currently presents two surfaces with nearly identical authoring and
execution affordances:

| Capability | Analysis tab | Data Tests tab |
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
integration. It survives the consolidation, renamed to Data Analysis. Saved
analyses are retired rather than preserved as a second active model.

## 3. Product invariants

Implementation must preserve the following invariants.

### 3.1 One classification rule

```text
rcm_id is null       -> Exploratory Analysis
rcm_id is valid      -> Audit Test
anything else        -> invalid record
```

The derived scope must be computed in one backend helper and mirrored by one
frontend helper. Filters, labels, actions, RCM rollups, finding gates, dashboard
curation, and workflow readiness must use that rule.

### 3.2 One active durable collection

After cutover, data procedures are stored only under `DataAnalysis/`.

`Workspace.analyses`, `Analyses/`, and `/api/workspaces/{id}/analyses` are not
valid active write targets. No compatibility layer may dual-write an Analysis
and a Data Analysis record.

### 3.3 One execution service

All saved procedures execute through `backend/app/data_analysis.py`:

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

An exploratory Data Analysis record:

- does not appear in RCM coverage;
- is not included by “Run all Audit Tests”;
- cannot supply a formal finding execution reference;
- cannot be selected by deterministic RCM dashboard curation;
- may still be run, edited, exported, and manually pinned;
- may be linked to an RCM row without cloning the definition.

An Audit Test:

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

If a Data Analysis record is already referenced by a formal finding, changing or clearing
its RCM link must fail with an actionable error until the finding references are
removed or reconciled. A mutation must not silently break an existing evidence
chain.

### 3.6 Model generation remains bounded and role-specific

The exploratory worker:

- receives declared table metadata, bounded profiles, value-free aggregates,
  relationship evidence, the analytics registry, and current unlinked records;
- never receives table rows;
- proposes unlinked Data Analysis records only;
- cannot set `rcm_id`, auditor disposition, conclusion, or finding fields.

It may separately propose validation rules for the Data tab's profiling
workflow. Those rules are not Data Analysis records, do not appear in the Data
Analysis tab, and never participate in Data Analysis execution or audit
coverage.

The RCM test worker:

- receives one RCM row plus its declared methodology, table metadata, and
  document inventory context;
- produces Audit Tests or Document Tests;
- continues to generate exception-oriented Polars steps for Data Analysis.

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
the Audit Tests view, so moving the navigation entry does not hide fieldwork.

The canonical query-string tab is:

```text
tab=data-analysis
```

Supported destination-owned query values are:

```text
test=<DAT-id>
scope=all|exploratory|audit
create=analytics|polars
rcm=<RCM-id>
```

All four must be registered in `ownedKeys` in
`frontend/src/composables/useWorkspaceNavigation.ts`:

```ts
'data-analysis': ['test', 'create', 'rcm', 'scope'],
```

Without the `scope` entry, `cleanWorkspaceQuery` strips it and every redirect
below loses its scope on arrival.

Compatibility redirects map the two tab keys that exist today. The old Analysis
tab was exploratory-only, so it maps to the exploratory scope; the old Data
Tests tab carried both scopes, so it maps to the unfiltered view:

- `tab=analysis` redirects to `tab=data-analysis&scope=exploratory`;
- `tab=data-tests` redirects to `tab=data-analysis`, retaining `test`,
  `create`, and `rcm`;
- a deep link with a concrete `test` takes precedence over the requested scope
  filter so the target is always visible;
- RCM actions use `scope=audit` and pass the target `rcm` or `test`.

Both redirects reuse the tab-key normalizer that already exists in
`frontend/src/views/WorkspaceView.vue` for `validation -> data`. Do not
introduce a second redirect mechanism.

### 4.2 Page structure

The page uses one master-detail layout.

Header:

- title: **Data Analysis**;
- description: “Explore imported data or execute Audit Tests with
  durable local results”;
- primary action: **New analysis**;
- secondary action, when an RCM filter is active: **Run Audit Tests**.

Scope switch:

- **All**;
- **Exploratory**;
- **Audit Tests**.

Filters:

- RCM row;
- engine;
- execution status;
- verdict;
- created by;
- text search over ID, title, objective, and table names.

Rail item:

- title;
- `Exploratory` or `Audit Test · RCM <id>` badge derived from `rcm_id`;
- engine badge;
- execution/staleness status;
- latest verdict when current;
- table or “All workspace tables” summary;
- created-by indicator.

The rail sorts by:

1. items requiring review;
2. stale Audit Tests;
3. ready Audit Tests;
4. completed Audit Tests;
5. exploratory tests;
6. most recently updated within each group.

### 4.3 Creation flow

The create menu contains:

- **Library analytic**;
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

- the dialog labels the record **Audit Test**;
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

Audit Test presentation:

- prominent `Audit Test · RCM <id>` badge;
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
- the Data Analysis objective;
- the fact that the existing result will not count until re-run;
- definition issues that must be fixed before linking.

On success:

- the existing record is updated in place;
- the page switches to the Audit Tests scope;
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

| Stored field | Exploratory label | Audit Test label |
| --- | --- | --- |
| `exception_frame` | Result rows | Exceptions |
| `exception_count` | Result row count | Exception count |
| `completed_with_exception` | Completed with result rows | Completed with exceptions |
| `completed_no_exception` | Completed with no result rows | Completed with no exceptions |
| `review_required` | Review required | Review required |

No derived label changes the stored result or RCM eligibility. Eligibility is a
separate deterministic predicate.

## 5. Target durable model

### 5.1 Data Analysis definition

The existing durable record remains authoritative, renamed and with one added
field. The full shape, including the outcome fields section 4.4 binds to:

```json
{
  "id": "DAT-...",
  "semantic_id": "data_analysis:...",
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
  "conclusion": "",
  "control_conclusion": "no_conclusion",
  "result_summary": "",
  "scope_limitations": "",
  "next_action": "",
  "exception_count": 0,
  "open_exception_count": 0,
  "finding_refs": [],
  "last_run": null,
  "auditor_disposition": "pending",
  "evidence_refs": [],
  "created_by": "agent",
  "agent_run_id": "...",
  "workflow_parent_sha1": "...",
  "created": "...",
  "updated": "..."
}
```

Model changes:

- add a validated `viz` definition field so a saved visualization survives
  re-runs and dashboard pinning;
- expose derived `scope: "exploratory" | "audit"` in list/detail payloads;
- expose derived `result_current`, `result_stale_reasons`, and
  `finding_eligible`;
- do not add a persisted purpose or scope flag;
- keep `created_by` and `agent_run_id` as the authoritative author/generation
  provenance;
- keep every existing outcome field above; the consolidation does not narrow
  the record.

There is no `migration` provenance field. Clean cutover means there is no
legacy identity to record.

### 5.2 Definition validation by scope

Common validation:

- title and objective are required;
- engine must be `analytics` or `polars`;
- analytics requires valid table references;
- Polars code must pass the sandbox validator;
- referenced tables and columns must exist;
- a definition must contain at least one executable analytic or Polars step.

Additional Audit Test validation:

- `rcm_id` must exist;
- criteria must be non-empty before the test may count as specified;
- every Polars step instruction must describe exception semantics;
- Polars output must be a DataFrame whose rows represent failed items;
- the current definition must pass semantic preflight;
- an invalid or incomplete linked definition may be stored as draft but is not
  executable or coverage-eligible.

Exploratory records may have blank criteria and may use broader informational
language. They still use safe Polars and bounded results.

Rule-based validation is not a Data Analysis engine. It remains a Data-tab
ruleset capability backed by `backend/app/validation.py`,
`backend/app/routes/validation_routes.py`, `workspace.rulesets`, and
`frontend/src/components/validation/TableValidation.vue`, none of which this
plan touches. The current durable service carries a third `validation` engine
that duplicates that capability inside the module being consolidated; it has no
producer — neither the Data Tests UI nor any agent worker emits it, and it is
reachable only by a direct API POST. It is removed in WP1. Dashboard tile kind
`validation`, which the Data tab's own pin path produces, is unaffected and
stays.

### 5.3 Current result

Continue storing one replaceable current result under:

```text
DataAnalysisResults/<DAT-id>/DAR-CURRENT.json
```

Already present, to be confirmed rather than added:

- `rcm_id` captured at execution, including null;
- dataset fingerprints for every frame the code could access.

Actual changes:

- fold `rcm_id` into the definition/source hash. `source_sha1` currently hashes
  only `{engine, table_refs, spec}`, which is why a linkage change silently
  reuses an execution under a different audit meaning. This one line is the
  substance of invariant 3.5;
- add `scope_at_run: exploratory|audit` as a derived snapshot for diagnostics;
- compute result eligibility from current definition, current linkage, current
  input fingerprints, semantic validity, and run status;
- make no result history claim: the active model retains one current result,
  except older immutable files that remain referenced by findings.

Changing only narrative outcome fields does not invalidate execution. Changing
engine, table references, executable spec, or `rcm_id` does.

### 5.4 RCM and finding eligibility

Create one backend predicate, for example:

```python
data_analysis.result_eligibility(workspace, item, result) -> {
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

Use `data-analysis` as the canonical durable resource route, served from
`backend/app/routes/data_analysis_routes.py` (renamed from
`data_test_routes.py` in WP-1). Every saved record is a Data Analysis record:

```text
GET    /api/workspaces/{workspace_id}/data-analysis
POST   /api/workspaces/{workspace_id}/data-analysis
GET    /api/workspaces/{workspace_id}/data-analysis/{analysis_id}
PATCH  /api/workspaces/{workspace_id}/data-analysis/{analysis_id}
DELETE /api/workspaces/{workspace_id}/data-analysis/{analysis_id}
POST   /api/workspaces/{workspace_id}/data-analysis/{analysis_id}/run
GET    /api/workspaces/{workspace_id}/data-analysis/{analysis_id}/runs/{run_id}
GET    /api/workspaces/{workspace_id}/data-analysis/{analysis_id}/runs/{run_id}/export
POST   /api/workspaces/{workspace_id}/data-analysis/{analysis_id}/pin
POST   /api/workspaces/{workspace_id}/data-analysis/run-all-audit
```

`data-analysis` is the durable namespace. `analysis` is **not** available for
it: `backend/app/routes/analysis_routes.py` is the stateless router — table
schema, preview, profile, explore queries, the analytics catalog, and Excel
export — and section 6.5 retains it unchanged. The two have different lifetimes
and must not share a name or a module.

### 6.2 List response

The list endpoint returns:

```json
{
  "items": [],
  "counts": {
    "all": 0,
    "exploratory": 0,
    "audit": 0,
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

Preserve export capability by adding a Data Analysis result export:

- analytics exports the deterministic analytic result;
- Polars exports the current bounded summary/result frame;
- export uses the current saved definition and verifies its source identity;
- a stale result may be exported only with a filename and workbook note that
  identify it as stale;
- export is local and does not call the model.

### 6.5 Retired routes

Delete `backend/app/routes/analyses_routes.py` outright and drop its router
import and mount from `backend/app/main.py`:

```text
GET/POST/PATCH/DELETE /api/workspaces/{workspace_id}/analyses...
```

Keep `backend/app/routes/analysis_routes.py` unchanged. Its stateless analytics
catalog and execution helpers are what Data Analysis authoring uses:

```text
GET  /api/analytics
POST /api/workspaces/{workspace_id}/tables/{table}/analytics/{test_id}
```

Validation routes under `backend/app/routes/validation_routes.py` are also
unchanged and out of scope.

The local `/run-python` endpoint may remain for unsaved previews and assistant
artifacts, but the Data Analysis UI must clearly distinguish preview from a
durable Data Analysis run.

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

`analysis_workflow_v1` is retired with the workspaces that contained its runs.
Under clean cutover there is no legacy run history to keep readable, resume, or
close as interrupted, and no executor needs to recognize an `analysis:<id>`
receipt.

### 7.2 Worker

Replace the saved-analysis response contract with an exploratory Data Analysis
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

- emit Data Analysis schema-version-2 steps directly;
- every step has a label, instruction, and safe code;
- code assigns a DataFrame to `result`;
- the prompt may describe the output as relevant rows rather than confirmed
  control exceptions because the test is exploratory;
- execution and UI apply the exploratory terminology rules.

### 7.3 Context

Rename or replace the current-analysis context source with current exploratory
Data Analysis records:

- include only `workspace.data_analysis` where `rcm_id is None`;
- include definition metadata and canonical spec, never results or rows;
- continue supplying schema, bounded profile, value-free aggregates, analytics
  registry, and deterministic relationship evidence;
- preserve all current character, token, item, and table-scope bounds.

### 7.4 Executor

The definition executor:

- validates proposals through the Data Analysis service;
- assigns stable `DAT-...` IDs;
- writes `DataAnalysis/`, not `Analyses/`;
- reconciles by semantic ID;
- preserves auditor-edited records unless overwrite was explicitly approved;
- emits artifact references `data_analysis:<id>`;
- records `workspace_changed.kind = "data_analysis"`.

The execution capability:

- expands only workflow-authored unlinked Data Analysis records in scope;
- calls `data_analysis.compute` and `data_analysis.commit_result`;
- persists the normal current Data Analysis result, not a second bounded
  `last_result` shape;
- does not run Audit Tests merely because they share a table;
- settles an invalid result as review-required without converting it into audit
  coverage.

### 7.5 Audit workflow

The audit workflow’s `tests.specified` worker and fieldwork executors remain
separate and continue to create Audit Tests.

Required integration changes:

- shared creation and spec-validation helpers must accept explicit system-owned
  provenance from either workflow;
- audit readiness ignores unlinked exploratory tests;
- an exploratory test linked by the auditor becomes eligible for the audit
  workflow only after it is re-run;
- test generation must not overwrite a linked auditor-owned definition;
- forcing exploratory analysis must not regenerate or edit Audit Tests.

## 8. Cutover policy

### 8.1 No workspace migration

This consolidation is a clean cutover. Pre-consolidation workspaces are not
migrated and are not supported by the consolidated build.

- no saved Analysis definition is carried forward;
- no legacy Analysis execution, bounded `last_result`, or dashboard-tile
  history is carried forward;
- no legacy evidence anchor is carried forward or archived;
- no `analysis_workflow_v1` run record, proposal, or receipt sidecar is carried
  forward;
- there is no compatibility reader, no `Legacy/` archive, no ID mapping, and no
  migration provenance anywhere in the target model.

A workspace produced before the cutover must be recreated. Release notes must
say so plainly.

### 8.2 Schema version gate

Clean cutover still requires one deliberate change, because the existing
migration path is unsafe to leave in place while the schema version moves.

`_migrate_artifacts` in `backend/app/workspaces.py` is a single v1-to-v4
inline-to-sidecar migration, not a versioned chain. It is guarded only by
`schema_version >= SCHEMA_VERSION`, and it rebuilds every collection by reading
that collection out of `workspace.json`. A v4 `workspace.json` holds only
`_MANIFEST_FIELDS` — no collections and no `planning`. So bumping
`SCHEMA_VERSION` without touching that function makes every existing v4
workspace re-enter the loop, find nothing, and rewrite each `.index.json` as
`{"ids": []}` while resetting `Planning/context.json` and `Planning/APM.md` to
defaults. The artifact files survive on disk but become unreachable.

The correct clean-cutover behavior is a hard, actionable stop:

1. bump `SCHEMA_VERSION`;
2. change `_migrate_artifacts` to raise `WorkspaceError` for any workspace
   below `SCHEMA_VERSION`, with a message naming the workspace and instructing
   the user to recreate it;
3. write nothing on the rejection path — the workspace directory must be
   byte-identical after a refused open.

Step 2 is not optional and must land in the same change as step 1.

## 9. Backend implementation work packages

### WP-1 — Rename Data Test to Data Analysis

Mechanical, behaviour-free, and delivered on its own before any other work
package. Every other section of this plan is written against the post-rename
names.

| Current | Target |
| --- | --- |
| `backend/app/data_tests.py` | `backend/app/data_analysis.py` |
| `backend/app/routes/data_test_routes.py` | `backend/app/routes/data_analysis_routes.py` |
| `/api/workspaces/{id}/data-tests` | `/api/workspaces/{id}/data-analysis` |
| `.../data-tests/run-all-rcm` | `.../data-analysis/run-all-audit` |
| `_ARTIFACT_COLLECTIONS["data_tests"] = ("DataTests", "id")` | `["data_analysis"] = ("DataAnalysis", "id")` |
| `Workspace.data_tests` | `Workspace.data_analysis` |
| `DataTestResults/` | `DataAnalysisResults/` |
| `CURRENT_RESULT_ID = "DTR-CURRENT"` | `"DAR-CURRENT"` |
| `DAT-` record ID prefix | unchanged |
| `semantic_id` prefix `datatest:` | `data_analysis:` |
| artifact and evidence ref `datatest:<id>` | `data_analysis:<id>` |
| `rcm[].test_refs` values written by `_link` | `data_analysis:<id>` |
| `tile.data_test_id` | `tile.data_analysis_id` |
| `tile.result_ref` `datatest:<id>:<run>` | `data_analysis:<id>:<run>` |
| `SOURCE_KINDS` member `"datatest"` | `"data_analysis"` |
| `findings` kind set `{"datatest", "doctest"}` | `{"data_analysis", "doctest"}` |
| `frontend/src/components/DataTestsTab.vue` | `DataAnalysisTab.vue` |
| `components/data-tests/AnalyticsTestAuthor.vue` | `components/data-analysis/AnalyticsAuthor.vue` |
| `DataTest*`, `DataTestEngine`, `DataTestStep` types | `DataAnalysis*` |
| tab key `data-tests` | `data-analysis` |
| agent action `create_data_test` | `create_data_analysis` |
| capabilities `analysis.definitions_ready`, `analysis.executed` | `data_analysis.tests_ready`, `data_analysis.executed` |
| workflow `analysis_workflow_v1` | `data_analysis_workflow_v1` |

Scope is roughly 520 references across `backend/app` and `frontend/src`.

Two coupling constraints, both of which break evidence resolution if split
across commits:

- `_link` writes the ref into `rcm[].test_refs` and `commit_result` writes
  `tile.result_ref`; both must change in the same commit as `SOURCE_KINDS`;
- the capability, workflow, and action renames must land together with the
  registry that dispatches them.

**Gate:** the full backend suite and the frontend production build pass with no
behaviour change, and `rg -n "datatest:|data_tests|DataTests|DataTestResults"
backend/app frontend/src` returns nothing.

### WP0 — Pin contracts before mutation

- [ ] Add tests for `rcm_id`-derived scope.
- [ ] Add tests proving client-supplied scope cannot override linkage.
- [ ] Add tests for exploratory exclusion from RCM coverage and findings.
- [ ] Add tests for linkage-change result invalidation.
- [ ] Add tests for finding-dependent unlink rejection.
- [ ] Capture current analysis-workflow routing and privacy behavior.

**Gate:** the tests describe the target contract and fail only where the current
dual model is expected to change.

### WP1 — Strengthen the Data Analysis domain service

Touchpoints:

- `backend/app/data_analysis.py`
- `backend/app/rcm_execution.py`
- `backend/app/findings.py`
- `backend/app/evidence.py`
- `backend/app/dashboard.py`
- `backend/app/workspaces.py`
- `backend/app/workspace_transactions.py`

Tasks:

- [ ] Add the single derived-scope helper.
- [ ] Add `viz` definition validation.
- [ ] Separate common definition validation from Audit Test eligibility.
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
- [ ] Keep `run_all_audit` restricted to non-null RCM IDs and skip drafts.

Remove the orphaned `validation` engine (section 5.2):

- [ ] Reduce `ENGINES` to `{"analytics", "polars"}`.
- [ ] Delete the `validation` branch in `_validate_spec`.
- [ ] Delete the `validation` branch in `_run_engine` and its null-column
      special case.
- [ ] Drop the `"validation"` entry from the pin route's engine-to-tile-kind
      map, leaving dashboard tile kind `validation` intact for the Data tab.
- [ ] Drop the `validation` import from `data_analysis.py`.
- [ ] Narrow the frontend engine type to `'analytics' | 'polars'`.

Retire the Analysis evidence source kind (clean cutover, section 8.1):

- [ ] Remove `"analysis"` from `evidence.SOURCE_KINDS`.
- [ ] Delete the `source_kind == "analysis"` branch in `findings.artifact`; it
      reads `workspace.analyses`, which WP3 removes.
- [ ] Change `evidence.legacy_anchor` to raise instead of defaulting an
      unrecognized ref to `kind="analysis"`, which would otherwise keep minting
      anchors for a source kind that no longer resolves.

**Gate:** Data Analysis records alone can represent, execute, filter, link, unlink, roll up,
and pin both scopes without reading `workspace.analyses`.

### WP2 — Consolidate APIs

Touchpoints:

- `backend/app/routes/data_analysis_routes.py` — the durable CRUD router
- `backend/app/routes/analyses_routes.py` — deleted
- `backend/app/routes/analysis_routes.py` — unchanged, stateless helpers only
- `backend/app/routes/assistant_routes.py`
- `backend/app/main.py`

Tasks:

- [ ] Add derived list counts and eligibility fields.
- [ ] Add current result export.
- [ ] Enforce linkage transition errors through the patch route.
- [ ] Reject `engine=validation` with an actionable error.
- [ ] Leave `analysis_routes.py` and `validation_routes.py` untouched; they own
      the stateless authoring helpers and the Data-tab ruleset capability.
- [ ] Update OpenAPI tags and descriptions for the `data-analysis` resource
      path.
- [ ] Delete `analyses_routes.py` and drop its import and mount from
      `main.py`.

**Gate:** the frontend needs only Data Analysis CRUD plus stateless authoring helpers.

### WP3 — Retire the Analyses collection and gate the schema version

Touchpoints:

- `backend/app/workspaces.py`
- `backend/tests/test_workspace_artifact_storage.py`
- `backend/tests/test_workspaces.py`

Tasks:

- [ ] Bump `SCHEMA_VERSION`.
- [ ] Convert `_migrate_artifacts` from a migrating path into a rejecting gate:
      any workspace below `SCHEMA_VERSION` raises an actionable
      "recreate this workspace" error and writes nothing. See section 8.2 —
      bumping the version without this change blanks every artifact index in
      every existing workspace.
- [ ] Stop hydrating `Workspace.analyses`.
- [ ] Remove `analyses` from `_ARTIFACT_COLLECTIONS`.
- [ ] Remove `analyses` from save, sync, table-rename, table-delete, and
      semantic-lookup paths.
- [ ] Remove saved-analysis counts from workspace summaries and mutation
      counters.

**Gate:** a current-version workspace loads with one active data-procedure
collection; an older workspace is refused without being modified.


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

The workflow identity, capability IDs, and ref prefix are renamed in WP-1. This
package changes what they persist.

- [ ] Change current-definition context to unlinked Data Analysis records.
- [ ] Change the worker schema to Data Analysis definitions.
- [ ] Commit definitions through Data Analysis validation and transactions.
- [ ] Execute through Data Analysis compute/commit.
- [ ] Preserve semantic de-duplication and auditor-edit protection.
- [ ] Keep relationship and join inference unchanged.
- [ ] Keep one bounded model turn per target frame.
- [ ] Update model budgets from the same resolved table scope.
- [ ] Route `data_analysis` goals only to the new workflow.
- [ ] Update run projections, artifact labels, and UI stage labels.

**Gate:** “analyze these tables” creates and runs only unlinked Data Analysis records, and
the run uses the same result artifacts as manual Data Analysis execution.

### WP5 — Update isolated actions and assistant artifacts

Touchpoints:

- `backend/app/agent/actions.py`
- `backend/app/agent/action_runner.py`
- `backend/app/assistant_chats.py`
- `frontend/src/components/agent/ChatArtifactCard.vue`
- `frontend/src/components/agent/AgentTaskList.vue`

Tasks:

- [ ] Retarget “run this saved analysis” to an unlinked Data Analysis record.
- [ ] Retarget “pin this analysis” to the Data Analysis pin endpoint.
- [ ] Save assistant-generated code/analytic artifacts as unlinked Data Analysis records.
- [ ] Require a title and objective when saving an artifact.
- [ ] Convert one-code artifacts into schema-version-2 Polars steps.
- [ ] Replace active `analysis:<id>` action refs with `data_analysis:<id>`.
- [ ] Preserve query artifacts as queries; do not force them into Data Analysis.
- [ ] Update action target resolution and artifact labels.
- [ ] Remove action access to `workspace.analyses`.

**Gate:** no active assistant or ActionRunner path can create or mutate a saved
Analysis.

### WP6 — Build the Data Analysis frontend

Touchpoints:

- `frontend/src/components/DataAnalysisTab.vue` (renamed from
  `DataTestsTab.vue` in WP-1) becomes the combined surface; `AnalysisTab.vue`
  contributes its reusable pieces and is then deleted in WP7;
- reuse/refactor `frontend/src/components/data-analysis/AnalyticsAuthor.vue`
  (renamed from `data-tests/AnalyticsTestAuthor.vue` in WP-1);
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
- [ ] Register `'data-analysis': ['test', 'create', 'rcm', 'scope']` in
      `ownedKeys`; without `scope`, `cleanWorkspaceQuery` strips it and the
      redirects below lose their scope on arrival.
- [ ] Add the `tab=analysis` and `tab=data-tests` redirects from section 4.1,
      reusing the existing `WorkspaceView.vue` tab-key normalizer.
- [ ] Add scope switch, counts, and granular filters.
- [ ] Show derived linkage badges and result currency.
- [ ] Do not expose Validation as a Data Analysis creation mode; keep its
      profiling workflow in the Data tab.
- [ ] Reuse one analytics catalog component.
- [ ] Reuse one Polars step editor.
- [ ] Preserve useful Analysis visualization, preview, and export affordances.
- [ ] Add guarded Link to RCM, Change RCM, and Remove RCM actions.
- [ ] Switch result terminology based on `rcm_id`.
- [ ] Hide formal audit controls for exploratory records.
- [ ] Disable finding actions when shared eligibility says false.
- [ ] Deep-link RCM and dashboard actions into the correct scope.
- [ ] Update workspace-changed subscriptions to `data_analysis`.
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

`backend/app/routes/data_test_routes.py` is **not** on this list. It is renamed
to `data_analysis_routes.py` in WP-1 and retained as the durable CRUD router.

Then:

- [ ] replace deleted-model tests with consolidation tests;
- [ ] run static searches for any remaining `workspace.analyses`, `/analyses`,
      `analysis:<id>`, `data_tests`, or `datatest:<id>` reference;
- [ ] confirm every match is zero. Clean cutover leaves no historical
      compatibility surface: all active artifacts use `data_analysis:<id>`.

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
- [ ] Document the Data Test to Data Analysis rename, including the changed
      route, collection directory, and artifact ref prefix.
- [ ] Add release notes stating plainly that pre-consolidation workspaces are
      not migrated and must be recreated, and explaining the tab redirects.

**Gate:** architecture, product copy, and code describe the same one-model
system.

## 10. Test plan

### 10.1 Data Analysis service tests

Extend `backend/tests/test_data_analysis.py` to prove:

- unlinked create returns derived exploratory scope;
- linked create returns derived Audit Test scope;
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
- `engine=validation` is rejected by create and update, and absent from the
  creation controls;
- Polars step table references are rewritten on table rename;
- dashboard pinning works for both scopes;
- automatic curation considers linked eligible results only.

### 10.2 Schema gate tests

Clean cutover replaces the migration suite with one focused test in
`backend/tests/test_workspace_artifact_storage.py`:

- opening a workspace whose `schema_version` is below `SCHEMA_VERSION` raises an
  actionable error naming the workspace;
- the refused workspace directory is byte-identical afterwards — in particular
  every `.index.json`, `Planning/context.json`, and `Planning/APM.md` is
  untouched;
- opening a current-version workspace performs no migration work.

### 10.3 Workflow tests

Replace or rewrite `backend/tests/test_workflow_analysis.py` to prove:

- the new graph and workflow hash are stable;
- deterministic relationship evidence is unchanged;
- joins are still materialized only under the existing evidence gate;
- the worker receives no rows;
- proposals create only unlinked Data Analysis records;
- executor refs are `data_analysis:`;
- definitions resume from sidecars without re-billing;
- executions use Data Analysis current results;
- auditor edits are preserved;
- force regeneration remains scoped;
- Audit Tests are never picked up by exploratory execution;
- model budgets and concurrency remain bounded.

Keep audit workflow tests proving RCM generation still creates Audit Tests.

### 10.4 API tests

Cover:

- combined list counts;
- derived scope;
- create/update/run/result/export/pin;
- optimistic concurrency during linkage changes;
- linkage errors for missing RCM and broken findings;
- removed saved-analysis routes;
- the durable resource is served at `/data-analysis`, not `/analysis`;
- stateless analytics, preview, and validation routes still work;
- legacy tab redirects are frontend behavior, not duplicate APIs.

### 10.5 Evidence and report tests

Cover:

- exploratory results cannot satisfy formal finding execution refs;
- linked current results can;
- stale linkage results cannot;
- `analysis` is not a valid `source_kind` and is rejected on create;
- `legacy_anchor` raises on an unrecognized ref instead of defaulting it to an
  Analysis anchor;
- RCM working papers include only eligible linked results.

### 10.6 Frontend verification

At minimum:

- TypeScript typecheck through the existing production build script;
- production Vite build;
- manual desktop verification at wide and narrow breakpoints;
- keyboard navigation through scope switch, rail, create menu, and RCM dialogs;
- `tab=analysis` redirects to `tab=data-analysis&scope=exploratory`;
- `tab=data-tests&test=...` redirects to `tab=data-analysis&test=...`;
- RCM deep link to an Audit Test;
- creation and execution of both Data Analysis engines;
- link, re-run, unlink, and blocked-unlink flows;
- exploratory versus Audit Test terminology;
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
rg -n "data_tests|DataTests|data-tests|datatest:" backend/app frontend/src
rg -n "SavedAnalysis|AnalysisLastResult|DataTestEngine" frontend/src backend/app
```

Under clean cutover every one of these must return zero matches, with exactly
one documented exception: the `'data-tests'` string literal in the frontend tab
redirect from section 4.1. There is no other historical-compatibility surface to
exempt. Active references use `data_analysis`, `DataAnalysis/`,
`DataAnalysisResults/`, and the `/data-analysis` route; `analysis_routes.py` and
`validation_routes.py` remain as stateless helpers and are the only surviving
uses of those words.

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
2. Confirm the Data Analysis record appears in Data Analysis under Audit Tests.
3. Confirm model-generated Polars steps are visible.
4. Run locally.
5. Confirm exceptions, semantic diagnostics, disposition, and RCM rollup.

### Scenario D — Exploratory workflow

1. Ask the assistant to infer joins and analyze selected tables.
2. Confirm deterministic relationship handling.
3. Confirm generated definitions are unlinked Data Analysis records.
4. Confirm their results use `DataAnalysisResults/`.
5. Confirm no `Analyses/` artifact is created.

### Scenario E — Pre-cutover workspace refusal

1. Open a pre-consolidation workspace.
2. Confirm the open is refused with an actionable message naming the workspace
   and instructing the user to recreate it.
3. Confirm the workspace directory is unchanged on disk, including every
   `.index.json` and the Planning artifacts.
4. Create a fresh workspace and confirm normal operation.

### Scenario F — Evidence protection

1. Create a formal finding supported by a current Audit Test result.
2. Attempt to remove or change the test’s RCM link.
3. Confirm the mutation is rejected with the dependent finding identified.
4. Reconcile the finding, then repeat the linkage change successfully.

## 12. Definition of done

The consolidation is complete only when:

- one Data Analysis tab is visible;
- all saved data procedures are Data Analysis records;
- `rcm_id` alone determines exploratory versus Audit Test scope;
- the same local execution path serves both scopes;
- linkage changes cannot reuse an execution under a different audit meaning;
- exploratory definitions and results cannot satisfy RCM or finding gates;
- exploratory workflow output lands in `DataAnalysis/`;
- RCM test-generation output continues to land in `DataAnalysis/`;
- the Data Test naming is gone from every active path — module, route,
  collection, result directory, semantic ID, artifact ref, component, and type;
- rule-based validation is unchanged and remains a Data-tab ruleset capability;
- a pre-cutover workspace is refused without being modified;
- dashboard pinning, visualization, and export are preserved;
- old navigation links redirect correctly;
- no active saved-analysis CRUD, storage, workflow executor, or frontend model
  remains;
- all backend tests and the frontend production build (including its TypeScript
  typecheck) pass;
- architecture and product documentation describe the consolidated system.

## 13. Recommended delivery sequence

Deliver in five reviewable changes:

1. **Rename**
   - WP-1 alone;
   - no behaviour change, no new tests, no product change;
   - reviewable as a pure diff of names.

2. **Domain foundation**
   - WP0–WP3;
   - no navigation change yet;
   - prove the Data Analysis model and the schema gate independently.

3. **Workflow convergence**
   - WP4–WP5;
   - new exploratory runs create Data Analysis records;
   - old Analysis UI may temporarily be read-only during this change.

4. **Data Analysis UI cutover**
   - WP6;
   - one navigation entry and redirects;
   - full authoring, execution, linking, export, and pinning.

5. **Retirement and documentation**
   - WP7–WP8;
   - delete the old active model only after all prior gates pass;
   - run the full regression and static-boundary suite.

Keep the rename out of every other delivery. A rename mixed with behaviour
changes is not reviewable, and the rename is the one change that touches every
file this plan mentions.

Do not combine the schema gate, workflow persistence change, and old-model
deletion into one unreviewable change. Each delivery must leave workspace
storage valid and must have an explicit rollback boundary.
