# Fieldwork views redesign: RCM, document tests, data tests

**Status:** design agreed on 4 September 2026, not yet implemented. This is the
handoff for rebuilding the three fieldwork views and their satellite surfaces
(the RCM row detail, the working paper, and the test definition editor) on one
shared system. Every claim about what the code does today was read from the
code at commit `0e56e15`.

It follows the same shape as [`engagement-record-redesign.md`](engagement-record-redesign.md),
and the two share a vocabulary: tokens, the slim header, one primary action.

## The design reference

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/199b9a8c-0408-45ca-91f4-5bc6d95c07ba>
- **Exact markup**, one file per artboard, in [`fieldwork-views/`](fieldwork-views/).
  Every element carries its geometry, colour and type inline; each opens in a
  browser as a plain page.

| Artboard | File | What it shows |
|---|---|---|
| Risk and control matrix | [`rcm.html`](fieldwork-views/rcm.html) | The grouped read-only grid with the row drawer open |
| Document tests | [`document-tests.html`](fieldwork-views/document-tests.html) | List, verdict bar, item detail |
| Data tests | [`data-tests.html`](fieldwork-views/data-tests.html) | List, verdict bar, exception explorer |
| RCM row, Definition tab | [`rcm-row.html`](fieldwork-views/rcm-row.html) | The routed page that replaces the detail modal |
| RCM row, Working paper tab | [`rcm-working-paper.html`](fieldwork-views/rcm-working-paper.html) | The paper as a document, replacing its modal |
| Test definition drawer | [`test-definition.html`](fieldwork-views/test-definition.html) | One drawer for New test and Edit definition |

All six are drawn at 1440 px wide with the `Procurement` workspace's real data as
of 4 September. Like the record mockup they use **literal hex values**; the
implementation must use the tokens in `frontend/src/style.css` (map at the end).
Two values in the mockups have no token yet and need one: the violet chip
border (`#d9ccf5`, proposed `--aw-accent-line`) and nothing else.

## What is wrong today, per view

**RCM** (`PlanningTab.vue` with `RcmGrid.vue`)

1. The matrix has twelve columns. The six that carry the verdicts (Test summary,
   Execution status, Exceptions, Conclusion, Findings, Review) sit past the
   right edge at 1440 px and are reached by horizontal scroll. A reader sees
   risk text and attribute text, and none of the conclusions.
2. Every cell is an inline editor: textareas with resize handles, reorder
   arrows, a rating select, a review select. The grid reads as a form.
3. Two titles for one thing (`RCM`, then `Risk and control matrix`).
4. Three notice rows with monospace tags (`Agent`, `Limit`, `Sign-off`) above
   the grid, each with its own `Show rows` link.
5. The frozen actions column is 14rem wide for three icons.
6. The row detail is a 1120 px modal, and the working paper is a 980 px modal
   over it. Neither has a URL a reviewer can send.

**Document tests** (`DocTestsTab.vue`)

1. Six header buttons plus a delete icon.
2. Four stacked bands before content: status lanes, the Agent notice, search,
   `Select all`.
3. The run result is stated four times on one screen: the list icon, the
   Assessment badge, `RUN RESULT` in the rail, and the `Result` section.
4. A 13rem rail with its own scrollbar holds the three disposition buttons,
   the conclusion select, the finding buttons and provenance.

**Data tests** (`DataTestsTab.vue`)

1. Two amber callouts compete: the stale-conclusion box and the exception card's
   own warning line.
2. The verdict is spread over the red card, the headline count, the rail's
   `Your conclusion`, and the ruling line of each reason group.
3. The rail scrolls separately, so `Accept` can be off-screen while the
   exception it accepts is on-screen.
4. Three candidates for the primary action: `Run`, `Accept`, `Draft findings`.
5. `New test` is a 56rem modal while `Edit definition` is a right drawer, for
   the same fields.

## The shared system

Five rules, applied to all three views. Build the two new components first
(section "Order of work"); each view is then mostly deletion.

### 1. Page header (36 px row)

`h1` title (`--aw-text-xl` 700, `--aw-ink-strong`, -0.01em) with a count
sentence beside it (`--aw-text-sm` `--aw-muted`, tabular): `24 risks · 22
controls · 37 tests`, `7 items · all run · 1 exception open`, `30 tests · all
run · 10 with open exceptions`. Right-aligned: at most one primary button, two
secondary outlined buttons, and the existing `UiOverflowMenu` kebab. Anything
else the header offers today goes into the kebab. The header no longer takes
lane actions from `statusActions()`; the review bar carries those counts and
the primary button is chosen per view (table below).

| View | Primary | Secondaries | Kebab |
|---|---|---|---|
| RCM | `Draft N findings` (warn) when adverse rows lack a finding, else `Add risk` | `Add risk`, `Run tests ▾` (split: data / document / all) | Generate planning drafts, Generate all findings, Refresh roll-up, Export, Import |
| Document tests | `Prepare with assistant` | `Run ▾` (split: all / outstanding), `New test` | Cycle rules, Delete selected |
| Data tests | `Draft N findings` (warn) when any, else `Run all` | `Run all`, `New test` | Re-run stale, Delete selected |

### 2. Review bar (replaces `UiStatusLanes` and `UiFilterMenu`)

One card (`--aw-radius-surface`, `--aw-border`, `--aw-panel`, padding 10 px
14 px). Left: filter chips, 8 px apart. Right: three lane meters.

A **chip** is a pill (`--aw-radius-pill`, padding 4 px 11 px, `--aw-text-sm`
600) whose first token is the count in 700 tabular. Its tone comes from the
filter's tone in the existing status model:

| Tone | Border | Fill | Text |
|---|---|---|---|
| all (pressed) | `--aw-teal` | `--aw-teal-soft` | `--aw-teal-strong` |
| bad | `--aw-danger-line` | `--aw-danger-soft` | `--aw-danger-ink` |
| warn | `--aw-warn-line` | `--aw-warn-soft` | `--aw-warn-ink` |
| ok | `--aw-ok-line` | `--aw-ok-soft` | `--aw-ok` |
| agent | `--aw-accent-line` (new) | `--aw-accent-soft` | `--aw-accent` |
| neutral | `--aw-border` | `--aw-panel` | `--aw-ink-soft`, count in `--aw-ink-strong` |

Chips **are** the filters: the first chip is `All N` and is pressed by default;
clicking another chip replaces it (one axis at a time, which is the
`toggleFilter` rule already in `statusLanes.ts`). A pressed chip takes the
`all` treatment. Chips with a zero count are not drawn. The bar draws at most
six chips; the rest of the vocabulary stays reachable through the pressed
chip's popover, which is today's `UiFilterMenu` grouped list. Which six, per
view, in order:

| View | Chips → existing filter key |
|---|---|
| RCM | All · Ineffective → `ineffective` · Finding to draft → `missing_finding` · Evidence limits → `evidence_limit` · No control → **new** `no_control` (rows whose `control` is empty) · Agent-set, unread → `agent_concluded` · Unreviewed → `unreviewed_row` |
| Document tests | All · Exception → `exceptions` · Confirmed → `confirmed` · Agent-set, unread → `agent_concluded` · Call not recorded → **new** `no_call` (item disposition pending) · Needs review → `needs_review` |
| Data tests | All · Exceptions open → `with_exceptions` · Findings to draft → `missing_finding` · Measurement warnings → `semantic_warning` · Agent-set, unread → `agent_concluded` · No exception → `passed` |

The `Mark N rows reviewed` settle action (RCM only) is a small outlined button
after the `Unreviewed` chip, with the existing confirm dialog.

A **lane meter** is a label (`11px` 600 uppercase `--aw-muted`, tracking
0.06em) with the count in `--aw-ink-strong`, over a 64×4 px bar
(`--aw-radius-pill`). The bar paints the lane's segments left to right with the
segment tones the lane model already emits (`ok` `--aw-ok`, `warn` `--aw-warn`,
`bad` `--aw-danger`, remainder `--aw-border`). Labels: `Run`, `Concluded`,
`Findings`. The `Details` expander, the expanded three-column card, the
disclosure rows and their `Show rows` links are removed; every sentence they
carried is a chip.

### 3. Verdict bar (replaces the detail rail)

Inside the detail, directly under its header. A card (`--aw-raised` fill,
`--aw-border`, `--aw-radius-surface`, padding 12 px 16 px) in two columns:
`minmax(0,1fr) auto`.

Left column, two lines:
- **What the run found**, `--aw-text-base` 600 `--aw-ink-strong`, led by a 9 px
  status dot, followed by the count and date in `--aw-text-sm` 500 `--aw-muted`.
- **What is recorded**, `--aw-text-sm` `--aw-ink-soft`: the conclusion, who set
  it and whether a person has read it (agent authorship in `--aw-accent`).

Right column: the recording controls for that view (section per view).

When the recorded conclusion is stale, one amber strip is attached under the
card (`--aw-warn-soft`, top rule `--aw-warn-line`, `--aw-warn-ink`,
`--aw-text-sm`, padding 8 px 16 px) with a single sentence. Every other stale
message in the detail is removed.

### 4. List rows

The master list is 300 px wide. Its header holds the search field and, for
document tests, a `Select` toggle that reveals checkboxes. A row (padding 10 px
12 px, 3 px left rule) has a 9 px status dot, a one-line title (`--aw-text-sm`
+1 = 13 px; 600 when active, 500 otherwise) and one meta line (`11.5 px`
`--aw-muted`): kind and disposition for a document item, table and failure
count for a data test, with open counts in `--aw-danger` and warnings in
`--aw-warn-ink`. Active row: `--aw-teal-soft` fill, `--aw-teal` rule. The status
icon chip and its tooltip go; the dot and the meta line say the same.

### 5. No modals

Quick edits open in a right drawer (PrimeVue `Drawer`, the pattern
`Edit definition` already uses). Anything that reads as a document gets a
routed page with tabs. The RCM detail modal, the working paper modal, and the
two create dialogs are the four modals this retires. The two `confirm` dialogs
(delete, mark reviewed) stay.

## Risk and control matrix

### Grid (`rcm.html`)

Read-only. Grid template `96px minmax(0,1.2fr) minmax(0,1.2fr) 110px 130px
128px 84px 28px`, column gap 14 px, row padding 10 px 16 px, `--aw-border` top
rule per row. Header row: `--aw-raised`, `--aw-text-xs` 700 uppercase
`--aw-muted`. Columns:

1. **Risk**: the id without its `RCM-` prefix in `--aw-font-mono` 12 px 600,
   and under it the rating as a 7 px dot plus word (`--aw-danger` high,
   `--aw-warn` medium, `--aw-low` low, `--aw-danger-ink` critical).
2. **Statement**: `risk`, 13 px `--aw-ink`, clamped to two lines.
3. **Control**: `control`, 13 px `--aw-ink-soft`, clamped to two lines. Empty
   control renders `No control identified` in italic `--aw-warn-ink` with a
   12 px info icon.
4. **Tests**: `N tests · N exc`, the exception count in `--aw-danger` 600 when
   non-zero.
5. **Conclusion**: a pill in the conclusion's tone (`Effective` ok,
   `Partially effective` warn, `Ineffective` bad, `Not applicable` and
   `No conclusion` neutral).
6. **Finding**: the first finding as an 8 px-radius chip (`--aw-warn-soft`,
   `--aw-warn-line`, id in mono + severity), `—` in `--aw-border-strong` when
   none, `+N` after the chip when several.
7. **Review**: a 12 px dashed `--aw-border-strong` circle with `Draft`, or a
   filled `--aw-ok` check circle with `Reviewed`.
8. Chevron, `--aw-teal` on the selected row, `--aw-border-strong` otherwise.

Rows are **grouped by `process`** with a group header (`--aw-canvas` fill,
padding 7 px 16 px): chevron, the process name (13 px 600), and a count
sentence (`5 risks · 2 ineffective · 2 without a control`). Groups collapse.
The selected row takes `--aw-teal-soft`.

Removed: the `Risk and control matrix` sub-heading and its `Add risk` button
(now in the header), the inline editors, the frozen columns and their shadows,
`scrollHeight="60vh"`, the reorder arrows, the per-row eye / paper / trash
icons, the `Generate test` sparkle (it moves to the drawer's Tests section as
`Add test ▾` with `Generate` as an option).

### Row drawer (`rcm.html`, right side)

440 px wide, `--aw-shadow-lg`, opens on row click and on the `?rcm=` deep link.
Header: id in mono, rating dot, a `Working paper` link and close. Body (padding
14 px 16 px, 14 px gaps): Process and Rating side by side, Risk, Control,
Attributes (one card per attribute: assertion as an uppercase tag,
requirement text; `Add attribute` link), Tests (one line per linked test: dot,
title, open count; `Add test` link), and the conclusion card in its tone
(`Conclusion: Ineffective`, `Change`, the agent-authorship line in
`--aw-accent`, the finding line). Footer (`--aw-canvas` fill): the `Mark
reviewed` toggle, `Cancel`, `Save row` (primary). The drawer's header id links
to the row page for anything the drawer does not hold (control type and owner,
criteria and citations, the comparison editor, provenance, exception
observations).

### Row page (`rcm-row.html`)

Route `/workspace/:id/coverage/:rowId`, breadcrumb `Engagement record / Risk
and control matrix / RCM-FBB3A7`. Replace the `?rcm=` query with the param and
keep a redirect for old links.

Header (white, bottom rule): id, rating dot, process; right: previous / next row
(`1 of 24`), `Add test ▾`, `Save row` (primary), kebab (Remove row, Export).
Under it the risk statement as the page title (19 px 600, max 900 px), then
tabs (13 px 600, 2 px `--aw-teal` underline on the active one): **Definition**,
**Attributes** with count, **Tests** with count and open exceptions in a red
badge, **Working paper**, **Where this came from**.

Definition tab body: grid `minmax(0,1fr) 380px`, gap 20 px. Left: Process,
Rating, Control type, Control owner in a four-column row; Risk; Control;
Criteria with its citation pills (`--aw-teal-line` border, doc name + mono page)
under it; Attributes as a compact table (assertion select, evidence strategy
select, requirement, remove). Right column, top to bottom: the conclusion card
(`Accept and mark reviewed` as one button when the conclusion is agent-set),
the Tests card (one card per linked test: id, kind tag, open count, title,
assurance line), the Finding card, and the Reviewed toggle.

Attributes tab: today's `RcmControlAttributesEditor` including the
transaction-cycle comparison editor, at full width. Tests tab: the linked-test
cards at full width with `Open test`. Where this came from: today's
`ProvenanceRail` plus the exception observations list.

### Working paper tab (`rcm-working-paper.html`)

Same page header; `Copy ▾` (Markdown / HTML), `Export PDF`, `Regenerate`
(primary) replace `Add test` and `Save row` while this tab is active. Body: grid
`220px minmax(0,1fr)`, gap 32 px. Left, sticky: `On this paper` with one link
per `h2` in the rendered markdown (2 px left rule on the current one) and a
`Generated` card (date, source run, whether a person has read it). Right: the
paper in a white card at 760 px max width, padding 32 px 40 px, `h2` 21.6 px
700, section `h3` 15.2 px 600, body 14 px / 1.6. The existing markdown renderer
is kept; the section list is built from its headings.

### Backend and data

Nothing new is needed for the grid, drawer or page: `process`, `control_type`,
`control_owner`, `criteria`, the rollups, conclusions, findings and
`review_status` are all in the planning payload already. Two additions:

- The `no_control` filter is frontend-only (`rcmStatus.ts`).
- A row-page route needs the planning payload to be loadable for one row, or
  the page loads the whole payload and selects; the latter is fine at 24 rows
  and is what the modal does today.

## Document tests (`document-tests.html`)

Header per the table above. Review bar per section 2. Layout: grid
`300px minmax(0,1fr)`, gap 14 px, both columns cards.

**List header**: search (`Search items and answers`) and a `Select` link that
toggles checkboxes on item rows. While any row is ticked, the **bulk sign-off
bar** (today's four buttons) replaces the verdict bar of the open item; it
returns when the selection clears.

**Detail header**: eyebrow `CITED Q&A · ITEM-B2A1D367` (kind label, then the id
in mono), `h2` title 17.6 px 600, `Test: <test title>` in `--aw-ink-soft`.
Right: the RCM chip (`--aw-teal-line` border, map icon) and `Run test`
(outlined). The `Not linked to an RCM row` warning stays where the chip would
be.

**Verdict bar**: left line `The run found an exception · 1 open · run 4 Sep
09:12` (dot in the run tone) and under it `Your call is not recorded. Agree
with the run, or record a different one.` (or `You confirmed this on 3 Sep`,
with the reason quoted). Right: the three dispositions as **one segmented
control** (`--aw-border-strong` outline, 8 px radius, dividers): `Confirm` in
`--aw-ok`, `Exception` in `--aw-danger`, `Needs review` in `--aw-warn-ink`; the
current call takes its tone's soft fill. `Add a reason` and `Clear my call`
become links under the segmented control once a call exists. Cycle dispositions
stay binary (no `Needs review` segment).

**Body sections**, each an uppercase `--aw-text-xs` 700 `--aw-muted` label:
Procedure (the question at 14 px / 1.5, `Question as put to the model` as a
chevron link), Assessment (one card per document: doc icon, name, page links,
verdict pill right; 3 px left rule in the verdict tone; the answer under),
Transaction cycle and Assertion results for cycle items as today, Comparisons
and Attributes for vouching and attribute tests as today, Evidence (attached
docs as teal pills, `Attach a document` link, evidence requests and gap callouts
as today). **The Result section is removed**; its exceptions count is on the
verdict bar and its coverage sentence moves under the Assessment label for
cycle items.

**Footer row** (top rule, two columns): Conclusion select with `Record your
call first.` while no call exists, `Save` once changed; Finding chip with
`Regenerate`, or `Generate finding`. Provenance moves behind a `Where this came
from` chevron link beside them.

The cycle vouch grid keeps its own layout and is out of scope, except that its
inert `Cycle rules` button must either be bound (`@openRules`) or removed.

## Data tests (`data-tests.html`)

Header and review bar per the shared system. Layout as document tests; the
list header stacks search and the RCM row select (only when more than one RCM
row is linked, as today).

**List row meta**: `invoice_data · 2 of 52 failed · 2 open`, with `warning`
appended in `--aw-warn-ink` when the test carries a semantic warning.

**Detail header**: id in mono (`DAT-CD3E85FCE7`), title, the objective in
`--aw-ink-soft` 13 px / 1.45; right: RCM chip, `Edit definition`, `Run`, all
small outlined. The rail is gone.

**Verdict bar**: first line `2 of 52 records failed` with the rate in
`--aw-danger` (`--aw-muted` under 10%), `· 2 still open · run 4 Sep 09:26`.
Second line: `Concluded Ineffective by an unattended run. No auditor has read
it.` (authorship in `--aw-accent`), or `Concluded Effective by <name> on
<date>`. Right: `Accept conclusion` (primary) when the recorded conclusion is
agent-set or stale, and `Change ▾`, which opens a popover with the five-way
select and the note textarea (today's rail form) and `Save`. The stale strip
under the bar reads `The conclusion was recorded against an earlier run.
Accepting re-affirms it against this one.` All four of today's stale surfaces
collapse into that strip and the dimmed reading line goes.

**Why they failed**: label with `1 reason · 2 rows still open` right-aligned.
One card per reason (3 px left rule in the ruling tone): name and row count,
the ruling line under it (`Not ruled on.` plus the moved-evidence sentence in
`--aw-warn-ink`, or the quoted note), and the three rulings as a segmented
control (`Accept` ok, `Confirm exception` danger, `Needs review` warn).
`Clear` becomes a link inside the card once ruled. The accept-with-reason form
opens inline under the card as today.

**Exception table**: today's `FrameTable`, plus a **Ruling** column on the
right that shows the row's reason-group ruling (`Open`, `Accepted`, `Exception`,
`Needs review`). Header cells in mono uppercase. Footer note as today.

**Disclosures**: `Checks that ran · N`, `Summary output`, `Where this came
from` as three chevron links on one line, each opening its block in place.

**Footer row**: Finding, with `None yet. Two exception rows are still open.`
or the finding chip, and `Generate finding` / `Regenerate` on the right.

## Test definition drawer (`test-definition.html`)

600 px right drawer, `--aw-shadow-lg`, the page dimmed behind. Serves both
`New test` (empty) and `Edit definition` (filled); `DataTestCreateDialog.vue`
is retired and its authoring components (`AnalyticsTestAuthor`,
`PolarsStepEditor`, `AnalyticsCatalog`) mount inside the drawer.

Header: eyebrow `DEFINITION · <id>` (or `NEW TEST`), the title, close. Body
sections in this order, each with an uppercase label:

1. **Analytic**: the selected analytic as a teal strip (icon, label, one-line
   description, `Change`) or the catalog when none is chosen; `Write Polars code
   instead` link underneath, swapping the section for the step editor.
2. **Parameters**: the analytic's parameter grid, two columns; Table first.
3. **Counts as coverage for**: the RCM row select showing id and risk; the
   exploratory note under it in `--aw-muted`.
4. **Title and objective**: two inputs; `Criteria` behind a chevron link.

Missing required values outline their control in `--aw-warn-line` in place;
the footer no longer carries a `blocker` sentence. Footer (`--aw-canvas` fill):
the consequence sentence `Changing the definition marks the recorded conclusion
out of date.` in `--aw-warn-ink` when editing, then `Cancel`, `Save only`,
`Save and run` (primary; `Create and run` on a new test).

`DocTestCreateDialog.vue` moves into the same drawer with its **Test shape**
picker as section 1 and its per-shape scope fields as sections 2 onward; the
two-step stepper goes.

## Frontend work, by file

- New `ui/UiReviewBar.vue` (chips + meters) taking the same lane model
  `UiStatusLanes` takes today plus a `chips` list of `{filter, tone}`; new
  `ui/UiVerdictBar.vue` (two-line left column, slotted right column, optional
  stale strip). `UiStatusLanes.vue` and `UiFilterMenu.vue` are deleted once the
  three views have moved.
- `statusLanes.ts`: `statusActions()` stops feeding the header; keep it for the
  kebab and the review bar's settle action.
- `rcmStatus.ts`: add `no_control`. `docTestStatus.ts`: add `no_call`.
- `PlanningTab.vue`: replace `RcmGrid` usage and the detail dialog with the
  grouped grid, the drawer, and a `RouterLink` to the row page. New
  `views/RcmRowView.vue` for the page and its tabs; the working paper dialog
  content becomes its tab. `router.ts`: add `coverage/:rowId`.
- `RcmGrid.vue`: rewrite as read-only rows with a group header; no `DataTable`.
- `DocTestsTab.vue`, `DocTestItemDetail.vue`: remove the rail, the toolbar
  band and the Result block; add the verdict bar and footer row.
- `DataTestsTab.vue`, `DataTestResultPanel.vue`, `ExceptionExplorer.vue`:
  remove the rail and the stale messages; add the verdict bar, the segmented
  rulings, the Ruling column, the disclosure links.
- `DataTestCreateDialog.vue`, `DocTestCreateDialog.vue`: fold into
  `TestDefinitionDrawer.vue`.

Tests to update: `PlanningTab.test.ts`, `RcmGrid.test.ts`, `rcmStatus.test.ts`,
`DocTestsTab.test.ts`, `DocTestItemDetail.test.ts`, `docTestStatus.test.ts`,
`DataTestsTab.test.ts`, `DataTestResultPanel.test.ts`,
`ExceptionExplorer.test.ts`, `dataTestStatus.test.ts`, `UiStatusLanes.test.ts`
(replaced by a review-bar test). Assert per view: six chips at most, zero-count
chips absent, one pressed chip, the meters' segment widths, the verdict bar's
two lines, and that the run result appears exactly once in the detail.

## Order of work

Each step lands on its own.

1. `UiReviewBar` and `UiVerdictBar`, with tests, behind no view yet.
2. Data tests: header, review bar, verdict bar, list meta, rulings, Ruling
   column, disclosures. It is the view with the most duplicated verdicts, so
   the win is largest.
3. Document tests: the same, plus the footer row and the bulk bar swap.
4. RCM grid and drawer, `no_control`, header actions.
5. RCM row page with tabs and the working paper tab; route and redirect.
6. Test definition drawer; retire both create dialogs.
7. Delete `UiStatusLanes`, `UiFilterMenu`, the RCM detail and paper dialogs,
   and dead CSS.

## Token map for the mockups' hex values

Same as the record plan, plus:

| Hex in the mockup | Token |
|---|---|
| `#7c3aed` | `--aw-accent` |
| `#f5f0ff` | `--aw-accent-soft` |
| `#d9ccf5` | `--aw-accent-line` (new; `color-mix(in srgb, var(--aw-accent) 28%, var(--aw-panel))`) |
| `#147d55` / `#e5f6ee` / `#9fd9be` | `--aw-ok` / `--aw-ok-soft` / `--aw-ok-line` |
| `#b42318` / `#7f1d1d` / `#fdecea` / `#f2b8b2` | `--aw-danger` / `--aw-danger-ink` / `--aw-danger-soft` / `--aw-danger-line` |
| `#b45309` / `#8a4308` / `#fdf1e3` / `#f0cf9f` | `--aw-warn` / `--aw-warn-ink` / `--aw-warn-soft` / `--aw-warn-line` |
| `JetBrains Mono` | `--aw-font-mono` |
| 21.6 px | `--aw-text-xl` |
| 17.6 px | `--aw-text-lg` |
| 15.2 px | `--aw-text-md` |
| 13 px | `--aw-text-base` minus one; use `--aw-text-base` |
| `0 8px 20px … 0 24px 60px` | `--aw-shadow-lg` |

Icons are inline SVG stand-ins for the PrimeIcons the tabs already use; keep
the PrimeIcons.
