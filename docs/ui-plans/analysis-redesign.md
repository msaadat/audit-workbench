# Analysis redesign: a memorandum and a register

**Status:** design proposed on 5 September 2026, **built** on 6 September —
see "What was built differently" at the end. Every claim about what the code
did before that was read from the working tree on top of commit `df308da` (the
uncommitted change there was the shell header) and from the running app against
the `Procurement` engagement's 23 saved procedures.

Analysis is the last work product still on the pre-redesign chrome. It was in
none of the five earlier plans; the shell plan's review pass named it and
`planning/ChainView.vue` as "the only two pages not on the 36 px system" and
left them for a later round. This is that round for Analysis.

It uses the vocabulary of [`fieldwork-views-redesign.md`](fieldwork-views-redesign.md)
and [`sources-planning-reporting-redesign.md`](sources-planning-reporting-redesign.md)
without restating it: the 36 px page header with one count sentence and one
primary, `UiReviewBar` whose chips are the filters, a 300 px list with a dot
and a meta line, `UiVerdictBar`, and `UiDocumentPage` + `UiMarkdownDocument`
for a long written work product.

## The claim in one line

The page already has two genuinely different things on it, and the app already
has a system for each. **Summary is the audit planning memorandum's page**;
**Procedures is Data tests' page.** Almost nothing here is new — it is the
existing components, applied.

## What is wrong today (`AnalysisTab.vue` and `components/analysis/`)

1. **The header is the ten-controls problem again.** `UiPageHeader` draws an
   `h2` at `--aw-text-xl` with no count sentence, and five equal buttons after
   it: `Analyse with assistant`, `Run outstanding (23)`, `Run all (23)`,
   `Library`, `Code`. Two of them run the same procedures; two open the same
   editor. Every other page in the file has one primary.
2. **Two screens behind a `SelectButton`.** `Summary` / `Procedures` is drawn
   as a form control, with a sentence of explanatory copy beside it that only
   appears on one of the two. The report page had exactly this and lost it in
   the sources plan; the RCM row page's tabs are the element that replaced it.
3. **The summary is not drawn as a document.** `AnalysisSummary` renders
   `MemoView` full-bleed across the whole width — 1,363 words at whatever
   measure the window gives it, no outline, no card, no rail, no provenance.
   The memorandum's own page solves each of those and is one component away.
4. **The memo's citations are anonymous.** `MemoView` splits the markdown at
   the ```` ```embed ```` fences and drops the real `ChartView` / `FrameTable`
   between two paragraphs. Nothing beside a rendered table says which procedure
   produced it, what it concluded, or how to open it.
5. **The rail is a title and a tooltip.** `AnalysisList` renders the title
   clamped to two lines and a status glyph whose only label is
   `v-tooltip.left`. `DataTestList` next door — the same kind of list, of the
   same kind of thing — carries a dot, the table, what failed and what is still
   open, in words.
6. **What a procedure found and whether it is still current are one field.**
   `analysis_state` returns a single `classification`, and `stale` is one of
   its values alongside `exception` and `clear`. So a procedure that recorded
   four breaches and has since had its definition changed classifies as
   `stale`, and the four breaches disappear: on `Procurement` today all 23
   procedures are stale, the triage row reads `23 Rerun required` and
   `0 Exceptions`, and the sixteen that recorded exceptions — including the
   43,200,000 invoice the memo calls the largest monetary exposure in the
   population — are not counted anywhere on the page.
7. **The detail pane opens on a bare text input.** `AnalysisPython`'s first
   element is an unlabelled `InputText` holding the title, then two `Tag`s,
   then `Run` / `Save` / `Export` / a red trash icon. Below it `AnalysisOutcome`
   restates the classification the rail already showed. A data test's detail
   opens on its id, its title as an `h2` and its objective, and states the
   outcome once.
8. **The disposition never reaches the page.** `analysis_promotion.py` records
   a durable `promotion` on each analysis — promoted to a data test against an
   RCM row, or declined with a reason — and its own docstring names the loss it
   exists to prevent: "an analysis found it, the planning memorandum named it
   the most significant analytic result of the engagement, and no test was ever
   written for it". `analysis_listing()` copies a fixed tuple of keys off the
   record and `promotion` is not among them, so the field never leaves the
   backend.

   In `Procurement` it is not empty: seventeen procedures hold exceptions and
   **all seventeen have been answered** — eleven carried into data tests
   against named RCM rows, six declined with a paragraph of recorded reasoning
   each ("an Inactive status is a legitimate, expected state of a vendor master
   record, not a control failure"). None of that reaches the page. The work was
   done, the file records it, and the surface an auditor would check is silent
   about it in both directions: it cannot show that a procedure was carried,
   and it could not show one that had not been.

Items 1–3 and 5–7 are a restyle onto components that already exist. Item 4 is
one new element. Items 6 and 8 are the two that are not cosmetic, and they are
what the page is actually for.

## The design reference

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/c3df03df-f54a-4784-bdd8-250461bef773>
- **Exact markup**, one file per artboard, in [`analysis/`](analysis/).
  Generated from [`gen_analysis.py`](analysis/gen_analysis.py), which imports
  its primitives from the two generators that drew the pages being borrowed
  from rather than copying them, so a chip here cannot drift from a chip there.
  Regenerate from the script rather than editing the HTML by hand.

| Artboard | File | What it shows |
|---|---|---|
| Summary | [`Main.dc.html`](analysis/Main.dc.html) | The memo on the memorandum's page: outline, document card, provenance rail |
| Procedures | [`Procedures.dc.html`](analysis/Procedures.dc.html) | The register on Data tests' page: review bar, list, verdict bar, result |
| Procedure states | [`ProcedureStates.dc.html`](analysis/ProcedureStates.dc.html) | The six things one procedure's verdict bar can say |
| Empty | [`Empty.dc.html`](analysis/Empty.dc.html) | Summary before it is written, and the page before anything has run |

All four are drawn at 1,440 px wide, on the 44 px shell bar from
[`shell-and-index-redesign.md`](shell-and-index-redesign.md). The data is what
the API returned on 5 September for `Procurement`: the 23 procedures with their
recorded verdicts, the memo's real prose and outline, and the three rows
`A-8BFCE4A3` actually flagged. Like the earlier mockups they use **literal hex
values**; the implementation must use the tokens in `frontend/src/style.css`.

## The design, element by element

### 1. One page header, two tabs

**Page header (36 px)** per the shared system: `h1` `Analysis`; the count
sentence `23 procedures · 16 exceptions · 1 to review · none current`; one
primary. The header is the page's, so it is the same on both tabs except for
the primary, which is the page's next act — `Run 23 outstanding` on Procedures,
`Regenerate` on Summary, `Write the summary` when there is none. Beside it:
`New procedure` as a split button (`Library test` / `Custom code`, replacing
the two separate buttons) and, on Procedures, `Analyse with assistant`.
`Run all` moves to the kebab: it re-executes work that is already current,
which is the rarer half of the pair, and the primary already names how much is
outstanding.

**Tabs** (`Summary` · `Procedures 23`) on the row under it, the underlined tab
element the RCM row page uses. The `SelectButton` and its explanatory sentence
go.

### 2. Summary — the memorandum's page

`UiDocumentPage` with `UiMarkdownDocument` inside it, exactly as `ApmView` uses
them: a 220 px sticky outline (`On this summary`), the document on a 96ch
measure in a card, and a 300 px provenance rail. The memo carries eleven
headings — four `##` sections and seven numbered `###` findings — so the
outline is real navigation the moment it is switched on; the
sections that report an exception take the outline's `marks` dot, which is what
`ReportView` already does with its problem sections.

Above the document, `UiVerdictBar` says who wrote it and what it rests on —
`Written by the assistant 1 Sep 17:17 · 1,363 words · 4 sections, 7 numbered
findings · cites 21 of 23 procedures` over `It describes the results recorded on 1 September` — and
the staleness banner becomes its warn strip. The `Message` component and the
loose `Written … · cites 21 results` line above the memo both go.

**The rail**, mirroring the APM's four cards:

| Card | Contents |
|---|---|
| `Results it cites` (21 of 23) | The cited procedures with their dot and what each found, three then a `18 more` link, and a total line: `Over 6 source tables · 195 rows · every join matched at 1.0` |
| `Not cited` (2) | The procedures the memo does not mention. Both found nothing here, so neither changes what it says — but a procedure that found something and is not cited would, and this is where that becomes visible |
| `Written` | Assistant, model, calls, committed revision — `ArtifactProvenance` as the APM reads it |
| `What this feeds` | The disposition, engagement-wide: how many procedures found exceptions and how many have been promoted to a test |

**The citation card** is the one new element. Where the memo has an `embed`
fence, `MemoView` renders the result inside a bordered card whose header is the
procedure's dot, its id in mono, its title, what it concluded, and `Open`. The
prose already names the ids in brackets; this makes them the thing you click.

### 3. Procedures — Data tests' page

**`UiReviewBar`.** Chips: `23 All procedures`, `16 Exceptions`,
`1 Need review`, `6 No exception`, `23 Rerun required`. Meters:
`RUN 23/23`, `CURRENT 0/23`, `ANSWERED 0/16` — the same three-lane shape as
Data tests' `Run · Concluded · Findings`, saying the same kind of thing: what
has executed, what still stands, and what has been carried into the audit file.
`UiTriageCounts` and the separate `toolbar` search row go; the search moves
into the list panel's head, where Data tests keeps it.

**The list**, `DataTestList`'s row: a dot in the tone of what the procedure
*found*, the title, and a meta line that leads with the outcome
(`3 of 52 failed · invoice_data_po_data_joined`) because a joined frame's name
is 38 characters and would push the ranking fact off the end. Rows sort by
outcome. One addition over the data-test row: a small amber `pi-refresh` at the
row's right end for a result that is no longer current — a third state a data
test does not have, and one that must not compete for the meta line.

**The open procedure**, in Data tests' order:

| Element | Contents |
|---|---|
| Detail header | `A-8BFCE4A3 · Assistant test · invoice_data_po_data_joined` as the eyebrow, the title as an `h2`, the `note` as the objective, `Export` and a kebab on the right. The bare title input goes — a title is edited from the definition, like a test's |
| `UiVerdictBar` | *Found*: `3 of 52 rows failed 5.8% · run 1 Sep, 17:16`. *Recorded*: `Returned rows are read as exceptions. Recorded by an unattended run. No auditor has read it.` Actions: the outcome policy as the `Change`-shaped control, and `Run`. The stale strip carries the rerun sentence. `AnalysisOutcome` is replaced by this |
| `What it tests` | The spec as a sentence — `compare_columns · INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT over invoice_data_po_data_joined` — with `Edit definition`. Replaces the collapsed `Parameters` accordion and the free-standing `Change test` row |
| `Exception rows · 3 of 52` | The flagged rows, with `Open a row for the other 19 fields of the record.` and `Export the full result` |
| Footer row | `Answered for`: the disposition — the test and the row it was carried into, or the reason it was declined. The row that closes the record, as `Finding` does on a data test |

**The six states** a procedure's verdict bar can be in are on
`ProcedureStates.dc.html`: found exceptions and current; found exceptions and
rerun required; found nothing (saying what it covered — "no exceptions over 49
of 52 rows" is a different statement from "no exceptions"); worth a look;
could not run (the error *is* the verdict, and the action is `Edit definition`,
not a `Run` that will fail again); never run.

### 4. Empty

Two states, not three. With procedures but no memo, the Summary tab carries one
empty state and the header's primary becomes `Write the summary`. With nothing
at all, the tabs do not appear — there is nothing for a summary to summarise —
and the count sentence says `No procedures yet`.

## Backend and data

Two changes, both small, and item 6 is the one that matters.

**1. Separate what it found from whether it is current.** Smaller than it
looks, because the second axis is already computed and already on the wire.
`analysis_result_state()` returns `not_run` / `current` / `stale`, and
`analysis_state()` puts it on every payload as `state`. The whole loss is two
lines in `_classification()`:

```python
if state == "stale":
    return "stale"        # ← discards the verdict the result recorded
```

Delete them. `classification` then answers only what the result concluded —
`exception`, `unusual`, `clear`, `informational`, `execution_error`,
`not_run` — and `state` keeps answering whether it still stands. Nothing new is
computed and nothing new is sent; the frontend already receives both.

`CLASSIFICATION_BUCKETS` loses `stale`; `analyses_summary_payload()`'s `counts`
gains `stale` as a count over the same 23 rather than as a bucket that excludes
them, so `exception + unusual + clear + informational + errors + not_run` sums
to the register. `OUTSTANDING` in `classification.ts` becomes `not_run` plus
`state === 'stale'`, which is the same set `Run outstanding` already targets.

**2. Expose the disposition.** It gains `promotion` — the
record `analysis_promotion.disposition()` already returns, which is `null` when
the stamped `result_sha1` no longer matches, so a procedure rewritten since it
was declined correctly reads as unanswered again. The listing also gains
nothing else; `candidates()` and `declined()` already exist for the counts.

`SavedAnalysis` in `types.ts` already carries `state`; it gains
`promotion?: { state: 'promoted' | 'declined'; test_id?: string; rcm_id?: string; reason?: string; decided_at: string } | null`.
`AnalysisSummaryPayload.counts` gains `stale` alongside the rest.

Out of scope but noted: nothing in the API says which memo section cites which
procedure. `AnalysisMemo` carries `cited_analysis_ids` as a flat list, and
`MemoView` splits the markdown at the `embed` fences without tracking which
heading each fence sits under. Naming the section on the `Cited by` row means
either walking back to the nearest preceding heading in the client, or the memo
endpoint returning the citations grouped by section — the second is the honest
place for it, since the backend already writes the memo.

## Frontend work, by file

- **`AnalysisTab.vue`**: `UiPageHeader` → the 36 px `page-head` with the count
  sentence and one primary; the `SelectButton` → the tab row; `Library` and
  `Code` → one split `New procedure`; `Run all` → the kebab. `UiTriageCounts`
  and the `toolbar` block are deleted.
- **New `components/analysis/AnalysisReviewBar.vue`** or, better, call
  `UiReviewBar` directly with an `ANALYSIS_CHIPS` constant beside
  `DATA_TEST_CHIPS` and lanes built from the new counts. Prefer the second:
  the fieldwork pages share the component, not a wrapper each.
- **`components/analysis/AnalysisList.vue`**: rewritten as `DataTestList`'s
  row — dot, title, meta line, plus the not-current marker. The two-line clamp,
  the status glyph and its tooltip go.
- **`components/analysis/AnalysisSummary.vue`**: `UiDocumentPage` +
  `UiVerdictBar` + the four rail cards. The `Message` banner and the
  `summary-head` line go.
- **`components/analysis/MemoView.vue`**: prose segments render through
  `UiMarkdownDocument` so the headings become addressable ids the outline can
  reach; each embed renders inside the citation card.
- **`components/analysis/AnalysisPython.vue` and `AnalysisLibrary.vue`**: the
  shared head becomes the `detail-head` shape (eyebrow, `h2`, objective,
  actions) and `AnalysisOutcome` is replaced by `UiVerdictBar`. The title input
  moves into the definition editor.
- **Delete `components/analysis/AnalysisOutcome.vue`** and its stats strip once
  `UiVerdictBar` carries the same figures.
- **`components/analysis/classification.ts`**: `stale` leaves `META` and
  `BUCKET_CLASSIFICATIONS`; `OUTSTANDING` is rewritten against `current`.
- **`types.ts`**, **`analysis_payloads.py`**, **`analysis_results.py`** as
  above — the last of those is the one-line deletion in `_classification()`.

Tests to update: `analysis` has no component test today, which is part of why
item 6 survived. New `AnalysisList.test.ts` (the row states, including a
current result that failed and a stale one that failed reading differently);
`analysisClassification.test.ts` (the split enum, and that the buckets sum to
the register); `AnalysisSummary.test.ts` (outline entries from the memo, a
citation card per embed, the stale strip). Backend:
`test_analysis_results` asserts a stale result keeps the verdict it recorded, and that
`counts` sums to the register; a listing test asserts `promotion` round-trips
and is `null` once the result changes.

## Order of work

Each step lands on its own.

1. **The two axes.** Backend `classification` / `current`, the frontend enum,
   the review bar. This is the one that changes what the page *says*, and it is
   worth landing before any restyle.
2. **Procedures.** The page header and tabs, the list row, the detail head and
   verdict bar, the definition card, the footer row.
3. **Summary.** `UiDocumentPage`, the rail cards, the citation card.
4. **The disposition.** `promotion` in the listing, the `Answered` meter, the
   footer row's `Promote to a test`.
5. **`planning/ChainView.vue`**, the other page the shell plan's review pass
   left on `UiPageHeader`. Built with the rest — see below.

## Token map for the mockups' hex values

As the fieldwork and shell plans, plus:

| Value in the mockup | Token |
|---|---|
| `#eef2f7` verdict-bar fill | `--aw-raised` |
| `#b42318` / `#7f1d1d` failure ink | `--aw-danger` / `--aw-danger-ink` |
| `#b45309` / `#8a4308` the rerun marker | `--aw-warn` / `--aw-warn-ink` |
| 760 px document card | `max(1.25rem, calc((100% - 96ch) / 2))` padding, as `UiMarkdownDocument` writes it |
| 220 / 300 px outline and rail | `UiDocumentPage`'s `13.75rem` and `railWidth` |
| 300 px list panel | `UiMasterDetail` / the fieldwork `list-panel` |
| 10.5 px mono uppercase table heads | `--aw-text-2xs` in `--aw-font-mono` |

Icons are inline SVG stand-ins for the PrimeIcons the app uses; keep the
PrimeIcons (`pi-refresh` for the not-current marker, `pi-shield` for
`Promote to a test`, `pi-play`, `pi-sparkles`, `pi-plus`).

## What the mockups assume

- **The mixed triage is the honest post-rerun state.** All 23 procedures in
  `Procurement` are stale right now, so the built page's own counts are
  `23 Rerun required` and nothing else. The chips on `Procedures.dc.html` are
  each procedure's *recorded* verdict — 16 fail, 1 warn, 6 ok — which is what
  the split in step 1 makes visible without running anything. The
  `CURRENT 0/23` meter and the amber marker on every row are the current state,
  drawn truthfully.
- **`Read as exceptions` is `outcome_policy`.** Every procedure in this
  workspace has an empty `outcome_policy`, so the control shows its default.
  Whether it belongs in the verdict bar as the analogue of a data test's
  recorded conclusion, or stays a `SelectButton` in the definition, is the one
  placement worth a second opinion.
- **A procedure has no auditor sign-off.** A data test's verdict bar offers
  `Accept conclusion`; an analysis has nothing equivalent, and the mockups do
  not invent one. `Promote to a test` is the act that gives it one, which is
  why it closes the record instead.
- **`Cited by` names one section.** A procedure cited from two sections would
  need the row to carry both; the artboard draws the single-section case.
- **Two tabs, not two doors.** The alternative is to split Summary and
  Procedures into two record rows with their own destinations, as the sources
  plan did to the report. It is not drawn: the record has one `Analysis
  library` row, the two screens read the same loaded list, and a memo with no
  procedures under it is not a work product. If the auditor prefers two doors,
  everything above still applies to each half.

## What was built differently

Built on 6 September 2026, in the order above. Seven departures, each
deliberate:

1. **The count sentence is not there.** The design gave the page header a
   sentence — `23 procedures · 16 exceptions · 1 to review · none current`.
   Data tests does not have one and the fieldwork plan's own header does not
   either: the review bar directly below states every count the page has, so a
   sentence beside the title restates numbers the reader is about to be shown.
   The header is the title, `New procedure`, one primary, and the kebab.
2. **The rail states figures, not sentences.** The `Not cited` card explained
   itself in three lines ("None of these found anything, so their absence
   changes nothing the summary says — but a procedure that found something and
   is not cited would"). The rows and the count say it; the explanation is in
   this document, where it belongs. What survives in prose is one clause in the
   verdict bar, because "two are not cited" and "one that found something is
   not cited" are different facts and the band has to say which.
3. **`ANSWERED` counts what has been answered, and it is not zero.** The
   artboards drew `0/16` and a footer reading "Not yet". `Procurement` reads
   `17/17`: the promotion turn had run, and the six declines carry reasoning
   nobody could read. The lane counts only procedures that flagged rows — a
   clean procedure asks nothing of anybody, and counting it as answered would
   report a decision nobody made.
4. **No `Promote to a test` button.** Promotion is a fitting turn the assistant
   makes over every unanswered procedure at once (`analysis_promotion.candidates`),
   not a per-row commit this page can make. The footer states the disposition;
   the page's kebab asks for the ones still owed, through the same assistant
   path as every other workflow request.
5. **`Cited by` is not on the footer.** It needs the memo, which is a request
   the Procedures tab otherwise never makes, and the link already exists in the
   other direction: every citation in the summary opens the procedure. If it is
   wanted, the memo endpoint should return its citations grouped by section
   rather than the client walking back to the nearest heading.
6. **The engagement-level summary endpoint is no longer called.**
   `/analyses/summary` was a second answer to a question the records already
   answer. `analysisStatus` derives every count from the listing the tab holds,
   as `dataTestStatus` does next door, so the page makes one request and the
   chips cannot disagree with the rows.
7. **Two shared pieces came out rather than being copied.** The underlined tab
   row moved to `.ui-tabs` / `.ui-tab` in `style.css` (the RCM row page now uses
   it and dropped its own copy), and the heading split `UiMarkdownDocument`
   did inline moved to `components/ui/markdownBlocks.ts`, because the memo is a
   document rendered in pieces and has to number its duplicate headings across
   the whole of itself — `markdownBlocks.test.ts` holds that.

Also landed: `formatExecutedAt` now uses the same `stamp` the memorandum and
the report use, instead of a bare `toLocaleString` that gave the analysis pages
a date format nothing else in the engagement wrote; the verdict bar's second
line no longer restates the first (it said "This procedure concluded that the
population contains exceptions" under "16 of 20 rows failed"); and the title
input moved from the top of the pane into the definition, where renaming is one
edit among the edits that change what a procedure does.

**Files.** New: `components/analysis/analysisStatus.ts`, `AnalysisHead.vue`,
`AnalysisVerdict.vue`, `AnalysisFooter.vue`, `components/ui/markdownBlocks.ts`.
Deleted: `components/analysis/AnalysisOutcome.vue`. Rewritten: `AnalysisTab.vue`,
`AnalysisList.vue`, `AnalysisSummary.vue`, `MemoView.vue`, and the two editors'
heads. Backend: `_classification` lost its two-line short-circuit,
`CLASSIFICATION_BUCKETS` and `SUMMARY_CLASSES` lost `stale`,
`analyses_summary_payload` gained `stale`/`current` as cross-cutting counts, and
`analysis_listing` gained `promotion`.

**Tests.** New: `analysisStatus.test.ts` (14), `AnalysisList.test.ts` (5),
`AnalysisSummary.test.ts` (6), `markdownBlocks.test.ts` (3), plus backend
`test_a_stale_result_still_reports_what_it_concluded` and
`test_the_answer_reaches_the_page_that_has_to_show_it`.

### Step 5 · Chain

The other page on `UiPageHeader`, done with the rest. Same treatment, and one
thing that is not a restyle.

**What was wrong.** An `h2` and a 62ch lede explaining what a chain is. A rail
of 2-line risk cards under four mono counters — `1 src · 0 test · 0 exc ·
0 find` — an abbreviation table the reader has to learn, three of whose figures
read zero on most rows, so thirty of them said nothing about which to open. No
search. And the spine's second hop restated the risk and the control that the
detail's own header should have been carrying, so both were on screen twice.

**What it could not say.** The chain is the one view organised by question
rather than by artifact kind, and it asked its three questions of one row at a
time. A reviewer could see that *this* risk had no test and had no way to ask
how many did. In `Procurement` that number is **32 of 32** — not one risk in
the matrix is covered by a test — and in `TreasuryFull`, **none of the 19 risks
holding exceptions has a finding**. Neither fact was reachable from this page,
or from any page.

**What replaced it.** The spine's three hops are the review bar's three lanes,
asked of every row: `SOURCED 6/30 · TESTED 29/30 · WRITTEN UP 0/19`. The chips
are the breaks — `No test`, `No finding`, `Exceptions`, `No source`,
`Complete` — and they compose across axes, because "an uncovered risk that
cites no source" is two questions and neither answers the other. The rail
becomes the fieldwork row: a dot for what the chain found, the risk's first
sentence, and one line saying how far it reached (`2 tests · 2 exceptions ·
no finding`). The detail gains the head the spine was standing in for, and the
criterion hop goes with it. `chainStatus.ts` holds the derivation, as
`dataTestStatus.ts` does next door.

Two departures worth naming: the lanes are keyed `sources` / `coverage` /
`writeup` rather than `execution` / `findings`, because `UiReviewBar` renames
those two to `Run` and `Findings` and this page asks about coverage and
write-up — the tests ran on their own page. And the row's title lost its
120-character cap: the row clamps to two lines in CSS, so cutting the text as
well truncated it twice, once at a width the layout had never measured.

**Files.** New: `components/planning/chainStatus.ts` and its test (14).
Rewritten: `ChainView.vue`; its existing test keeps every assertion it had,
against the new markup, plus one for the empty state.

### And the component itself

With Analysis and Chain moved, `UiPageHeader` had one caller left — `CycleTab`,
which used its *classes* rather than the component and so appeared in no
survey. Its header was already the right shape in everything but the `h2` and
the class names, so it moved to `.page-head` with the rest, and the component,
its `.ui-page-header` block in `style.css`, and a stale stub in
`EngagementRecordTab.test.ts` are deleted. Nothing in the app draws a page
header any other way now.

**Still open:** nothing from this plan. Every work product is on the 36 px
system.
