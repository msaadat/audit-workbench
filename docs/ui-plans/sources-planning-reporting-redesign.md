# Sources, planning and reporting views redesign: documents, tables, APM, findings, report

**Status:** design proposed on 5 September 2026, not yet built. This is the
handoff for rebuilding the five pages that bracket fieldwork — the two source
pages the engagement starts from (Documents, Source tables), the memorandum it
plans with (APM), and the two work products it ends on (Findings, Report) — on
the system the fieldwork views already use. Every claim about what the code
does today was read from the working tree at commit `fec0f23`; the uncommitted
changes in that tree touch the cycle strip only, which none of these pages
render.

It follows [`fieldwork-views-redesign.md`](fieldwork-views-redesign.md) and
reuses its vocabulary without restating it: the 36 px header with one count
sentence and one primary, the review bar whose chips are the filters, the
300 px list with a dot and a meta line, the verdict bar, and no modals. Where
this document says "per the shared system", section "The shared system" of
that plan is the specification.

## The design reference

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/b4905af1-87ae-4932-9c9d-cebebb255add>
- **Exact markup**, one file per artboard, in
  [`sources-planning-reporting/`](sources-planning-reporting/). Generated from
  [`gen_views.py`](sources-planning-reporting/gen_views.py) in the same
  folder, so every chip, meter, row and pill is drawn by one function;
  regenerate from the script rather than editing the HTML by hand.
- The canvas has a second page, *Assistant*, whose artboards belong to
  [`assistant-panel-redesign.md`](assistant-panel-redesign.md).

| Artboard | File | What it shows |
|---|---|---|
| Documents, preview | [`Main.dc.html`](sources-planning-reporting/Main.dc.html) | The list grouped by category, the one-row header, the viewer |
| Documents, analysis tab | [`DocumentAnalysis.dc.html`](sources-planning-reporting/DocumentAnalysis.dc.html) | Vocabulary, structured evidence, summary, notes, sources |
| Source tables | [`Tables.dc.html`](sources-planning-reporting/Tables.dc.html) | Files and joins, the verdict bar, the column profile with a Tested column |
| Findings register | [`Findings.dc.html`](sources-planning-reporting/Findings.dc.html) | The list by severity, the verdict bar with the stale strip, the narrative as a document |
| Audit planning memorandum | [`Apm.dc.html`](sources-planning-reporting/Apm.dc.html) | Outline, the memorandum as a document, provenance and what it feeds |
| Draft audit report | [`Report.dc.html`](sources-planning-reporting/Report.dc.html) | Outline with issue markers, the report with issues attached in place, the issues rail |

All six are drawn at 1440 px wide with the `Procurement` workspace's real data
as of 5 September: 8 documents, 6 tables and 12 agent-built joins, a 2,900-word
memorandum, 18 agent-drafted findings none of which names a risk, and a report
whose quality check returns 40 errors and 16 warnings. Like the fieldwork
mockups they use **literal hex values**; the implementation must use the
tokens in `frontend/src/style.css` (map at the end). Three values in the
mockups are illustrative rather than read from the workspace, and the section
"What the mockups assume" says which.

## What is wrong today, per view

**Documents** (`DocumentsTab.vue`, 1,175 lines)

1. Nothing on the page says whether the corpus is ready. Eight documents are
   analysed and none of the analyses has been read by a person, and the only
   place that fact appears is a `needs review` tag on the Analysis tab of one
   document at a time.
2. The rail spends a full row on a `Group by` select, then draws each evidence
   type as a sub-heading with a `9f` badge and a warning triangle whose meaning
   (`thin vocabulary`) is a tooltip. Filenames are bold; nothing else is said
   about a row.
3. One row above the viewer holds seven controls: three view tabs, the page
   pager, the `Original / Extracted text` toggle, `Find` and `Open original`.
4. The Analysis tab opens with four state tags in a row (`idle`, `complete`,
   `current`, `needs review`) and five buttons (`Text coverage only`,
   `Configure vision`, `Refresh`, `Revise vocabulary`, `Compare candidate`).
   The structured evidence the analysis produced is a `<details>` holding raw
   JSON.
5. Three modals (`Search document contents`, `Methodology knowledge packs`,
   `Vision model profile`) and a fourth for retyping (`DocumentTypeReview`).

**Source tables** (`DataTab.vue`)

1. No count sentence and no status. The engagement record's own report warns
   that `No data test evaluates 10 imported columns: po_data (5 of 11),
   staff_details (3 of 4), invoice_data (1 of 15) …`; the tables page does not
   show it, and the backend that computes the column names
   (`column_coverage.untested_columns`) exposes only the counts.
2. The health dot means "duplicate rows", which no table here has, and its
   meaning is a tooltip.
3. A join is named by concatenation (`invoice_data_po_data_joined_staff_details_joined`)
   and truncated at 18rem; the `left ⋈ right` line under it is the only
   readable part.
4. Four stat cards (Rows, Columns, Duplicate rows, In memory) above a
   `DataTable` restate the row's own `52×15`.
5. `Preview first 100 rows` is a 90 vw modal; rename is a modal; `Add join` is
   a modal.

**Findings** (`FindingsTab.vue`)

1. The page reads as a form. Title input, severity select, a 26rem markdown
   editor, two checkboxes, a textarea, three multiselects, two collapsed
   sections. The finding — the thing the report copies unchanged — is never
   shown as the document it is.
2. The verdict is spread over the lanes (`Support 0 / 18`), two disclosure rows
   with `Show rows` links, the `Auditor confirmed` checkbox at the bottom of
   the editor, and an evidence warning inside a collapsed section.
3. The one thing the file owes — every finding lacks a risk link, so the
   report excludes all 18 — has no action anywhere; the header offers
   `Finding template` and `Add manual finding`.
4. The report's context calls these 18 confirmed findings `draft`
   (`draft_findings_excluded`) while the register calls them confirmed. The
   two pages disagree in words about the same fact.
5. The finding template is a modal.

**APM** (`PlanningTab.vue`, `section="apm"`)

1. Two titles for one thing (`APM`, then `Audit planning memorandum`), each
   with its own toolbar: `Generate planning drafts` above, then `Hide sources`,
   `Export`, `Import`, `Template`, `Save APM`.
2. A 2,900-word document with eight sections and no outline; the reader
   scrolls a `MarkdownEditor`.
3. `Auditor edited` is the only attribution, though the document carries an
   agent run id; "drafted by the assistant, then edited" is the fact.
4. What the memorandum feeds is not on the page: the cycle records the APM
   hash it was derived from (`cycle.apm_sha1`), so the page could say whether
   the cycle and the 32 risks still match this text. It does not.
5. The template is a modal.

**Report** (`ReportTab.vue`)

1. The editor does not know the quality check exists. Fifty-six issues sit on a
   second view (`Sources & quality`) behind a `SelectButton`, and the two that
   are about the report itself (`Not labelled as a preliminary draft`, `Asserts
   a rating nothing supports`) are indistinguishable from the fifty-four that
   are the same three defects repeated per finding.
2. `generation_warnings` — the three sentences the generator wrote about its
   own limits — are not rendered anywhere.
3. `What this report draws on` says `Findings confirmed 0` and `Findings still
   in draft 18` about a register that shows 18 confirmed findings.
4. The reconcile step is a 78rem modal holding two 24-row textareas.
5. No outline for a 41,000-character document.

## The shared system, applied

The five rules of the fieldwork plan hold. The two components it built,
`UiReviewBar` and `UiVerdictBar`, are reused as they are; nothing here needs
a new primitive. What each page decides is in the two tables below.

### Header

| View | Count sentence | Primary | Secondaries | Kebab |
|---|---|---|---|---|
| Documents | `8 documents · 5 evidence · all analysed · 8 analyses to review` | `Analyse N` (warn) when eligible documents lack a current analysis, else `Add documents` | `Add documents`, `Analyse ▾` (outstanding / all again), `Identify N` (warn) when any is typed `other` | Search contents, Reindex search, Methodology knowledge, Vision profile |
| Source tables | `6 files · 12 joins · 10 columns no test evaluates` | `Add files` | `Add join` | Profile again, Export table |
| Findings | `18 findings · 1 critical · 8 high · 9 medium · none in the report` | `Confirm N` when any is unconfirmed, else `Add finding` | `Add finding`, `Draft from the RCM` | Generate all findings, Finding template, Export register |
| APM | `8 sections · 2,900 words · drafted by the assistant 1 Sep · edited by an auditor` | `Regenerate`; `Save` while editing | `Edit` / `Done`, `Export ▾` (Markdown / PDF) | Import, Template, Copy Markdown |
| Report | `18 findings drafted in · generated 3 Sep 12:01 · not edited since` | `Regenerate`; `Save` while editing | `Edit` / `Done`, `Check quality` | Editorial review, Template, Copy Markdown, Export PDF |

The APM and Report have no list, so no review bar; their verdict bar is the
provenance strip described under each.

### Review bar

| View | Chips → filter key | Meters |
|---|---|---|
| Documents | All · Not analysed → **new** `not_analysed` · Analysis to review → **new** `needs_review` · Needs attention → **new** `attention` · Not identified → **new** `unidentified` · Typed by the model → **new** `model_typed` (agent tone) · Thin vocabulary → **new** `thin_vocabulary` | `Read` (text extracted), `Analysed` (analysis current), `Reviewed` (analysis reviewed), each out of the document count |
| Source tables | All · Columns untested → **new** `untested` · Duplicate rows → **new** `duplicates` · Failed to load → **new** `broken` · No validation rules → **new** `no_rules` · Built by the assistant → **new** `agent_built` (agent tone) | `Profiled` (out of all tables), `Tested` (files with every column named by a test, out of files), `Validated` (files with a rule set, out of files) |
| Findings | All · Unconfirmed → `unconfirmed` · Not linked to a risk → `no_rcm_link` · Evidence moved → `evidence_warning` · Root cause pending → `cause_pending` · No management response → `no_response` · Drafted by the assistant → `agent_authored` | `Confirmed`, `Supported`, `Settled` — the three lanes `findingsStatus.ts` already emits |

The findings row names seven chips so that `Unconfirmed` leads when it is
non-zero; the bar's six-chip cap and zero-count rule keep it to six drawn. The
rest of the findings vocabulary (`confirmed`, `no_evidence`, `no_test_link`)
stays in the pressed chip's popover. Two new derivation modules,
`documentsStatus.ts` and `tablesStatus.ts`, produce the lane model
`UiReviewBar` takes; both are read entirely off payloads the pages already
load, except for the two additions under "Backend and data".

## Documents (`Main.dc.html`, `DocumentAnalysis.dc.html`)

Layout: grid `300px minmax(0,1fr)`, gap 14 px, both columns cards.

**List.** Header: search (`Search documents`) and a `Group ▾` link (by
category, by folder, by status) replacing the full-width select. Groups are
the categories in planning-first order — `policy`, `minutes`, `background`,
`evidence` — with the fieldwork group header (`--aw-canvas` fill, chevron,
name, count sentence: `5 documents · 5 types · typed by the model`). The
evidence sub-headings go; a row's meta line carries the type instead. Row:
9 px readiness dot (`ok` ready, `warn` attention, `info` processing,
`--aw-border-strong` not yet read), the filename at 13 px, and the meta line
`1 page · vendor invoice · to review`, with `to review` in `--aw-warn-ink`,
`attention` labels in `--aw-warn-ink`, and `failed` in `--aw-danger`. The
`9f` badge and the thin-vocabulary triangle are removed; the chip carries the
count and the Analysis tab carries the sentence. The deep-search link
(`Search inside documents for "…"`) stays at the foot of the list when the
search field has text, and its results replace the list in place; the
`Search document contents` modal is retired.

**Detail header.** One 32 px row, because the viewer under it is what the
page is for: a neutral pill `Evidence · vendor invoice` (category and type;
`Not yet read` in `--aw-warn-ink` when the category is empty; the
classifier's confidence and rationale are its tooltip) and the filename as
`h2` at 15.2 px 600 clamped to one line. Nothing else: the page count, the
analysis date and the review state are on the list row's meta line and on
the `Mark reviewed` button's state, and the id, the hash and the imported
path are under `Technical details`. Right:
`Held as ▾` as an outlined select control (label in `--aw-muted`, value 600),
`Add to assistant` (outlined, paperclip), `Mark reviewed` (primary, which
becomes `Reviewed ✓` outlined once set), kebab (Re-analyse: refresh under the
current vocabulary / revise the vocabulary / with full visual coverage;
Re-extract text; Open original; Delete document). The imported path moves
under `Technical details`. `Read as` is the pill; it is no longer a second
labelled field. The detail card's padding drops to 12 px 16 px and its gap to
10 px on this page only.

**No verdict bar.** A document's two facts — when it was analysed and whether
a person has read the analysis — are one clause each, and both already sit
on the header's meta line and the list row's meta line; a bar restating them
was a band the viewer paid for. The two states that do need a sentence take a
strip under the tab row, in the fieldwork stale-strip form: when
`analysis_validity_state` is `stale`, `The analysis was made against an
earlier version of this file. Refresh it before relying on it.` with
`Refresh` as its action (the `coverage-warning` block in the Analysis tab is
removed); when `candidate_analysis_id` is set, `A refreshed analysis is
waiting.` in `--aw-info-soft` with `Compare`, which opens the comparison
inline in the Analysis tab as today.

**Tabs** under the bar, 13 px 600, 2 px `--aw-teal` underline: `Preview`,
`Analysis`, `Activity` with a count badge. The right end of the tab row holds
the viewer's own controls only while Preview is active: the `Original |
Extracted text` segmented control, `Find`, `Open original`. The page pager
stays inside the extracted-text view where the fieldwork plan's rule puts
paging (it belongs to whatever renders the document).

**Preview** is the browser's PDF viewer, the docx renderer or the text view,
as today, in a bordered 8 px-radius frame. Under it, `Technical details` and
`Where this came from` as chevron links.

**Analysis tab** (`DocumentAnalysis.dc.html`), sections with uppercase labels:

1. **Vocabulary**: one card — `Read as vendor invoice · 14 fields from 1
   document · none stated by two`, `14 fields` as a chevron link opening the
   field table, and when `thin`, the amber strip with the `thinReason`
   sentence and `Revise vocabulary` as its action. The four state tags go;
   the verdict bar says the same.
2. **Structured evidence**: `effective.records` rendered as a **sheet** in
   the same frame and at the same 640 px width as the file on the Preview
   tab, so the two read as the document and the machine's reading of it.
   The sheet has a header row (`RECORD 1 OF 1 · vendor_invoice` in mono
   uppercase `--aw-muted`, a `validated` pill, `read by the model` in
   `--aw-accent`) and a body that is the record's JSON with the words in the
   page's own type and the syntax drawn faintly: braces, quotes, colons and
   commas in `--aw-border-strong`; group keys (`"supplier"`, `"invoice"`,
   `"charges"`) in mono 12.5 px 600 `--aw-ink-strong` as the section
   headings; field keys in mono 12 px `--aw-muted` in a 200 px column;
   values at 13.5 px `--aw-ink`; a teal `p.N` citation chip at the row's
   end. Groups are the schema's own nesting where the schema nests, and one
   group named for the type where it is flat. The right of the section label
   reads `What the model read from the page, checked against the vendor
   invoice schema`. The raw-JSON `<details>` is retired.
3. **Summary**: when `summary_origin` is `structured_evidence`, the derived
   sentence in a `--aw-raised` card with `Derived from the structured evidence
   above; edit the fields, not the sentence.`; otherwise today's editor with
   its `Revert` link.
4. **Audit notes**: the editor, with the existing caution as its placeholder.
5. **Sources**: one row per citation, `[C1] p.1` in mono teal and the excerpt.

Footer (top rule): `Technical provenance` and `Where this came from` chevron
links; right, `Save notes` (outlined) and `Save and mark reviewed` (primary).
`Configure vision` moves to the header kebab as `Vision profile`, which opens
a right drawer; so do `Methodology knowledge` (a drawer listing packs) and the
retype flow (`Identify N` opens `DocumentTypeReview` as a 600 px drawer).

## Source tables (`Tables.dc.html`)

Layout as Documents.

**List.** Header: search (`Filter tables`), always shown. Groups `Files` (`6
tables · 195 rows · 4 with untested columns`) and `Joins` (`12 joins · all
built by the assistant`). Row: dot (`ok` every column tested, `warn` untested
columns or duplicate rows, `bad` load error, `--aw-border-strong` not
profiled), the name in `--aw-font-mono` 12 px, and the meta line `52 rows ·
15 columns · 1 column untested` with the count in `--aw-warn-ink`; a join's
meta line is the join sentence `invoice_data ⋈ po_data on PO_NUMBER_LINK =
PO_NUMBER · assistant` (authorship in `--aw-accent`), which is what the name
failed to say. The hover kebab goes; its actions are on the detail.

**Detail header.** Eyebrow `FILE · Invoice data.xlsx · imported 1 Sep 17:06`
(or `JOIN · left ⋈ right · built by the assistant`), `h2` the table name in
mono 16 px, then `Joined into 6 tables · profiled 5 Sep`. Right: `Replace
data` (outlined; files only), kebab (Rename, Remove, Download). Rename is an
inline edit of the `h2`, not a dialog.

**Verdict bar.** Line one: `52 rows · 15 columns · no duplicate rows · 5.1 KB
in memory · profiled 5 Sep`, dot `ok`, or `warn` with `N duplicate rows` in
`--aw-warn-ink`. Line two: `14 of 15 columns are evaluated by a data test.
DUE_DATE is evaluated by none — …`, the column names in `--aw-warn-ink` 600,
or `Every column is evaluated by at least one data test.` Right: `No
validation rules` in `--aw-muted` with `New rule set` (outlined), or `2 rule
sets · last run ok` with the verdict pill. A load error replaces the bar's
two lines with the error and `Replace data` as the primary.

**Tabs**: `Profile`, `Preview` with a `100 rows` badge, `Validation` with the
rule-set count, `Relationships` with the count of joins the table takes part
in. Preview mounts `FrameTable` inline at 100 rows and retires the modal.
Validation is today's `TableValidation` at full width. Relationships is the
join list for this table, with `Add join` opening the join builder as a
600 px right drawer (`JoinDialog.vue` retired).

**Profile body.** One line: `Statistics are computed on all 52 rows` (or `on
the first N rows` when `sampled`). Then the column table with header cells in
mono uppercase: Column (name in mono with the dtype in `--aw-muted` after
it), Type (pill: `id` neutral, `numeric` ok, `date` info, `categorical` warn),
Blank (64 px bar and the percentage), Distinct, Range / mean in mono, and a
new **Tested** column: `✓ 3 tests` in `--aw-ok` or `None` in `--aw-warn-ink`,
with the column name itself in `--aw-warn-ink` when none. The expander for top
values stays. The four stat cards are removed.

## Findings (`Findings.dc.html`)

Layout as Documents; the detail body is `minmax(0,1fr) 320px`, gap 22 px.

**List.** Header: search (`Search findings`) and a `Severity ▾` link. Rows are
grouped by severity (`Critical · 1 finding`, `High · 8 findings`, …), each
group collapsible. Row: dot in the severity tone (`critical`
`--aw-danger-ink`, `high` `--aw-danger`, `medium` `--aw-warn`, `low`
`--aw-low`, `info` `--aw-border-strong`), the title, and the meta line
`F-0571DE · no risk · cause pending` — id in mono, then the finding's open
items in their tones (`no risk`, `evidence moved` in `--aw-danger`; `cause
pending`, `no response` in `--aw-warn-ink`; `draft` in `--aw-muted` when
unconfirmed). The severity `Tag` and the `Agent` source line go.

**Detail header.** Eyebrow `F-0571DE · drafted by the assistant · 1 Sep 19:02`
(authorship in `--aw-accent`; `added by <name>` for manual findings), `h2`
the title (editable in place on click), then `Drafted from the exceptions of
one data test. The narrative is copied into the report unchanged.` Right:
the severity as a pill-shaped select in its tone (`Critical ▾`), `Save`
(primary, enabled when dirty), kebab (Remove, Regenerate from the test, Copy
Markdown).

**Verdict bar.** Line one: `Confirmed for reporting · by you, 3 Sep 11:54`
(dot `ok`) or `Not confirmed for reporting` (dot neutral) — `auditor_confirmed`
and `updated`. Line two: `In the report.` or `Left out of the report until it
is supported:` followed by the open items in their tones, the same list the
row's meta line carries, in full words. Right: `Withdraw confirmation` as a
link (or `Confirm for reporting` primary when unconfirmed) and `Link to a
risk ▾` (primary when `rcm_refs` is empty; otherwise the RCM chip). The
popover is today's RCM multiselect. The stale strip appears when
`evidence_warnings` is non-empty: `The test result this finding cites
(DAT-7A08FCD758, current run) has changed since the narrative was drafted.
Re-read the condition against the run, then re-affirm the evidence.` with
`Re-affirm` as its action (it rewrites `source_sha1` on the evidence ref,
which is what confirming the evidence means today). This is the only place
the evidence warning is stated.

**Body, left.** `Narrative` with `Edit` (pencil) at the right of the label.
Read mode renders the markdown as the document the report will copy: `h3`
sections at 15.2 px 600, body 14 px / 1.6, tables as bordered 8 px grids.
The `## Root Cause` section renders the amber strip `Pending auditor
follow-up. The report will carry this section empty until a cause is
recorded.` with `Record the cause` while `cause_pending` is true; recording
it switches to the editor at that section and clears the flag on save. `Edit`
swaps the whole narrative for `MarkdownEditor`. Then `Management response`
with `Record as received` at the right of the label: a `--aw-raised` card
reading `None received.` or the response text, the textarea appearing inline
on click. The two checkboxes are gone: confirmation is on the verdict bar,
cause pending is on the narrative.

**Body, right.** Three cards under uppercase labels. `Risk`: the RCM chip(s)
with the risk statement clamped to two lines, or, when none, a dashed
`--aw-danger-line` card on `--aw-danger-soft`: `Not linked to a risk. The
report cannot place this finding in a process until it names the row it
answers.` with `Choose a row`. `Tests`: one card per `test_refs` entry — the
test chip (id in mono, kind icon), an exception pill, the test title, and a
count line — with `Add` at the label. `Evidence`: one card per
`evidence_refs` entry — id in mono, a `changed` pill in warn tone when the
ref carries a warning, the source kind and id, and `Drafted against
<sha1[:8]>` — with `Add` at the label opening today's `evidence-picker`
list. Under the cards, `Where this came from` and the run id as chevron
links; the `ProvenanceRail` and the `Sources and provenance` section open in
place. The three multiselects (`RCM links`, `Test links`, `Execution sources`)
are replaced by these cards; execution refs are derived from the test refs on
save as the finding generator already does.

## Audit planning memorandum (`Apm.dc.html`)

Route unchanged (`/workspace/:id/apm`); the page stops being a mode of
`PlanningTab.vue` and becomes `views/ApmView.vue`.

**Header** per the table. One title. `Edit` toggles the document card into
`MarkdownEditor` at the same width; the primary becomes `Save` while editing
and the count sentence gains `· unsaved changes`.

**Provenance strip** (the verdict bar). Line one: `Drafted by the assistant
1 Sep 17:22 · from 3 documents and 7 tables · 1m 21s` (dot `ok`; from the
provenance payload's generation record). Line two: `Edited by an auditor
1 Sep 17:25. The cycle and the 32 risks in the matrix were derived from this
version, so a change here puts them out of date.` — `created_by`, `updated`,
and whether `cycle.apm_sha1` equals the hash of `apm_markdown`. No right
column. When the hashes differ the stale strip reads `The cycle was derived
from an earlier version of this memorandum. Regenerate the cycle, or the
risks will be planned against text that no longer exists.` with `Regenerate
cycle` as its action. When the memorandum is empty the strip is replaced by
today's empty state.

**Body**: grid `220px minmax(0,1fr) 300px`, gap 28 px. Left, sticky: `On
this memorandum` with one link per `h2` (2 px `--aw-teal` rule on the
current one), built from the rendered headings as the working paper tab
does. Middle: the memorandum in a white card at 760 px max width, padding
32 px 40 px, the eyebrow `AUDIT PLANNING MEMORANDUM · PROCUREMENT`, `h2`
21.6 px 700, `h3` 15.2 px 600, body 14 px / 1.6. Right, four cards at
`--aw-radius-control`: **Where this came from** — the documents the step
read (type badge, filename, size, `Open`), then `7 tables` and `4 other
sources` as chevron links, then the total; **Not supplied** — the withheld
tallies (`5 documents · outside this step's scope`, `1 other source · not
available`); **Generation** — step, model, calls, duration, committed
revision as a key/value list; **What this feeds** — the cycle (`4 steps ·
derived 5 Sep from this version`, green when current, amber with `derived
from an earlier version` when not) and the matrix (`32 risks · 5
processes`), each a link. The first three are today's `ProvenanceRail`
sections re-cut into cards; the fourth is new and reads from the planning
payload it already has.

The template opens in a 600 px right drawer. Import and Export keep their
file input and download.

## Draft audit report (`Report.dc.html`)

**Header** per the table; `Edit` and `Save` as on the APM.

**Verdict bar.** Line one: `2 issues with the report and 18 findings it
cannot include · checked 5 Sep 06:47` (dot `bad` when any error, `warn` when
only warnings, `ok` when clean, neutral when never checked: `Not yet checked
against the register`). The two numbers split `quality.issues` by whether an
issue's `refs` name a finding. Line two: `Generated by the assistant 3 Sep
12:01 from the register as it then stood. No auditor has read or edited it.`
(or `Edited by <name> on <date>`). Right: `Editorial review` and `Check again`,
outlined. The stale strip (warn) states the report-level facts the checks
found: `Fieldwork is still open: 32 risks have no test run and every finding
has lost its evidence since generation. The draft must be labelled
preliminary and cannot carry an overall rating.` — written from
`preliminary`, `completion.coverage.rows_without_tests` and the
`stale_evidence` count.

**Body**: grid `220px minmax(0,1fr) 320px`, gap 28 px. Left, sticky: `On
this report` from the `h2`/`h3` headings, with a 7 px `--aw-danger` dot on
any heading a report-level issue points at and an `excluded` badge on
`B. Detailed findings` while `draft_findings_excluded` is non-empty; the
detailed-findings entries collapse after three (`… 15 more`). Under it the
`Generated` card as on the working paper.

Middle: the report as a document card, and **the issues attached where they
apply**: a `--aw-danger-soft` strip with a 3 px `--aw-danger` rule under the
title (`Not labelled as a preliminary draft. Open fieldwork remains, so the
title page must say so.` with `Add the label`, which inserts the label the
generator would have) and under the `Audit Conclusion` heading (`Asserts a
Marginal rating. No overall rating can be given while fieldwork, evidence or
auditor judgment remains open; 32 risks have no test run.` with `Open
check`). Issue codes map to headings: `preliminary_label_missing` → title,
`report_rating_unsupported` → the conclusion, `report_arithmetic` and
`report_risk_arithmetic` → the summary table, `missing_limitations` → scope,
`finding_missing_from_report` → the detailed findings. The `Edit` toggle
swaps the card for `MarkdownEditor`; the strips stay above and below it.

Right, three cards. **Issues** (`56 · checked 5 Sep 06:47`): `About the
report · 2`, one row each with a red dot, the heading sentence from
`ISSUE_HEADINGS` and a link to its section; then `Findings it cannot include
· 18` with one sentence for the whole group (`Every finding in the register
is unsupported: none names a risk, and each cites a test result that has
moved since it was drafted.`) and **one row per finding** (`F-0571DE · no risk
· evidence moved`) rather than one per issue, linking to the finding. The
fifty-four per-finding issues collapse to eighteen rows because they are
eighteen facts. **Drawn from** (`3 Sep 12:01`): the risk-coverage bar and
legend as today, then `Risks in the matrix`, `Tests run`, `Findings
included`, `Excluded until supported` (warn), `Scope limitations recorded` as
a key/value list — `Findings still in draft` is renamed, because the register
does not call them drafts. **Generation notes** (`3`): `generation_warnings`
as sentences.

**Reconcile** is a mode of this page rather than a modal: when `generate`
returns `requires_reconcile`, the middle column becomes two document cards
side by side (`Current · edited by an auditor` and `Generated · new draft`),
the outline lists both, and the header carries `Keep current` and `Use
generated` (primary) until one is chosen. `ReportReconcileDialog.vue` is
retired. The template opens in a drawer.

## Backend and data

- **Untested column names.** `column_coverage.untested_columns` already names
  the columns per table; only `untested_column_summary` (counts) reaches the
  `rcm/completion` payload, deliberately, because that summary crosses into
  the report's model context. Add `GET /tables/{name}/coverage` returning
  `{columns: [{column, tests: [id…]}]}` for the auditor's view, computed from
  `tested_columns` with a per-column tally; the report's boundary is
  untouched. The Tested column, the tables verdict bar and the `untested`
  chip read from it.
- **Document review counts** need nothing new: `analysis_review_state`,
  `analysis_validity_state`, `classification.assigned_by` and the vocabulary's
  `thin` flag are all on payloads the page loads.
- **APM freshness** needs the hash of `apm_markdown` beside
  `cycle.apm_sha1`; compute it on the server and return it as
  `planning.apm_sha1` so the page compares two strings.
- **Report issue placement** is frontend-only: the code-to-heading map above.
- **Findings** need nothing new. `Re-affirm` uses the existing evidence
  confirmation on save.

## Frontend work, by file

- New `documents/documentsStatus.ts` and `tables/tablesStatus.ts` producing
  lanes, filters and chips for `UiReviewBar`; new `DOCUMENT_CHIPS`,
  `TABLE_CHIPS`, `FINDING_CHIPS` lists in the shape of `RCM_CHIPS`.
- `DocumentsTab.vue`: replace the rail tools and groups; add the verdict bar,
  the tab row, the field-card grid; move the three dialogs to drawers;
  retire the content-search dialog in favour of in-list results. Split the
  Analysis tab into `documents/DocumentAnalysisPanel.vue`.
- `DataTab.vue`: the list rows, the verdict bar, the tab row, the Tested
  column; `Preview` as a tab; rename inline; `JoinDialog.vue` into a
  `JoinDrawer.vue`.
- `FindingsTab.vue`: replace `UiStatusLanes` with `UiReviewBar`; the verdict
  bar; the narrative renderer (reuse `MarkdownView`); the three cards; delete
  the multiselects and checkboxes. `UiStatusLanes.vue` and `UiFilterMenu.vue`
  can then be deleted, which is the departure the fieldwork build recorded
  as pending.
- New `views/ApmView.vue` and `views/ReportView.vue` sharing a
  `DocumentPage` layout (outline, card, rail); `PlanningTab.vue` loses its
  `apm` branch; `ReportTab.vue` and `ReportReconcileDialog.vue` are retired.
  `AuditFileView.vue` mounts the two views.
- `ProvenanceRail.vue`: expose its three groups as slots or a `cards` prop so
  the APM rail can lay them out as cards without a second implementation.

Tests to update: `FindingsTab` (none exist today; add one), `findingsStatus.test.ts`
(the chip order), `DataTestsTab.test.ts` is untouched, `PlanningTab.test.ts`
(the `apm` branch moves), new `documentsStatus.test.ts` and
`tablesStatus.test.ts`, new `ApmView.test.ts` and `ReportView.test.ts`
asserting the outline is built from headings, the issue strips land on the
mapped headings, and the issues rail shows one row per finding.

## Order of work

1. Findings. The register is the page the fieldwork build could not finish
   without (it kept `UiStatusLanes` alive), and it needs no backend change.
2. Report, then APM, on the shared document layout.
3. Source tables, with the coverage route.
4. Documents, the largest file and the one with the most dialogs to retire.
5. Delete `UiStatusLanes`, `UiFilterMenu`, `ReportReconcileDialog`,
   `JoinDialog`, and dead CSS.

## What the mockups assume

Three values on the artboards are illustrative because the workspace does not
state them yet; everything else is read from the API on 5 September.

- The per-column test counts in the Tested column (`3 tests`, `1 test`) are
  placeholders; the backend names the untested column (`DUE_DATE`) but
  tallies nothing per column until the coverage route exists.
- The `2 exceptions on the current run · 55 requisitions` line on the test
  card of F-0571DE is written from the finding's own narrative; the test's run
  payload was not read.
- The `p.1` citation chips on every field of the structured evidence assume
  each record field carries a page; today only the summary citations do.

## Token map for the mockups' hex values

Same as the fieldwork plan, plus:

| Hex in the mockup | Token |
|---|---|
| `#d97706` | the report's high-risk band; no token today, matches `RISK_BANDS` in `ReportTab.vue` — give it one (`--aw-high`) or use `--aw-danger` |
| `#eab308` / `#854d0e` / `#fefce8` | `--aw-low` / `--aw-low-ink` / `--aw-low-soft` |
| `#525659` | the browser's PDF viewer chrome, not a token |
| 10.5 px mono uppercase | table header cells, `--aw-text-2xs` in `--aw-font-mono` |

Icons are inline SVG stand-ins for the PrimeIcons the tabs already use; keep
the PrimeIcons.
