# Engagement record redesign: the phased plan

**Status:** design agreed on 4 September 2026, not yet implemented. This is the
handoff for rebuilding the Record tab (`frontend/src/components/EngagementRecordTab.vue`)
around the five audit phases the backend already defines, with a phase progress
strip at the top. Every claim about what the code does today was read from the
code at commit `7b5d2cd`.

## The design reference

Two copies of the agreed mockup, and they are the spec:

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/ea2173d1-814f-4528-b7b1-35dfd12f31e3>
- **Exact markup:** [`docs/engagement-record-mockup.html`](engagement-record-mockup.html).
  Every element carries its geometry, colour and type as inline styles, so
  "what size is the phase numeral" is answered by opening the file, not by
  measuring a screenshot. It opens in a browser as a plain page.

The mockup is drawn at 1440 px wide with the data of the `Expenses` workspace as
it stood on 4 September: 3 of 12 work products filed, an analysis run in flight
at step 7 of 7, and the audit planning memorandum next.

The mockup uses **literal hex values**. The implementation must use the tokens
in `frontend/src/style.css`; the mapping is in the last section. Do not copy a
hex value into the component: the dark theme is nothing but token overrides,
and a literal breaks it.

## What is wrong with the view today

Read against the current screen, these are the five things the redesign removes.

1. **Two metaphors at once.** A timeline spine (dots, a time column) and a table
   (Time / Filed / What it did / Took). Nine of twelve rows show `—` in Time and
   `waits` in Took.
2. **The owed half dominates.** Nine "not yet" rows, each repeating `not yet` and
   `Waits for the …`, take three quarters of the screen. The three things the
   engagement actually holds get a quarter.
3. **Pills inside pills.** `Sources` plus `Documents 14` plus `Tables 24` stacked
   in one cell.
4. **The one action is buried.** The lead `Run` button sits in row four with no
   more weight than the rows around it.
5. **Four toolbar controls** for a page with one job.

## The design, element by element

Top to bottom. Sizes are CSS px at default density; the token column says what
to write.

### 1. Toolbar (32 px tall)

Left: the label `ENGAGEMENT RECORD` exactly as today (`--aw-text-xs`, 700,
0.1em tracking, uppercase, `--aw-muted`). Right, in order: the Concise / Full
density toggle exactly as today; **Refresh as an icon-only 30×30 outlined
button** (the word goes; the icon is enough beside a toggle that says what
the page is doing); the `Chain` link exactly as today.

The summary line that used to sit under the ledger (`3 work products held ·
21m of assistant time`) moves into the progress strip below.

### 2. Progress strip (new; one card, `--aw-radius-surface`, `--aw-border`, `--aw-panel`, padding 14 px 20 px)

Three rows, 8 px apart.

- **Sentence**, `--aw-text-base` 600 `--aw-ink-strong`:
  `{held} of {total} work products filed · {running} · {owed} to go`.
  The middle term is `1 running again` when a held stage is live, `1 running`
  when an owed one is, and is omitted when nothing is live. On the right in
  `--aw-text-sm` `--aw-muted`: `{elapsed} of assistant time · {runs} runs`
  from `totals`.
- **Segments.** A CSS grid with one column per phase, `grid-template-columns`
  built from the phase sizes (`2fr 1fr 3fr 4fr 2fr` for the default plan),
  6 px gap. Inside each column a flex row, 3 px gap, one 8 px-tall,
  3 px-radius segment per stage, in plan order. Colour by stage state:

  | State | Fill |
  |---|---|
  | held | `--aw-teal` |
  | live (running or queued), held or not | `--aw-info` |
  | the lead stage (first runnable) | `--aw-teal-line` |
  | owed, not live, not lead | `--aw-border` |

- **Labels.** The same grid; each phase title in `--aw-text-xs` 600
  `--aw-muted`, the current phase in `--aw-teal-strong`.

### 3. Live band (as today, tighter)

Only while a run is in flight. One 8 px-radius row (`--aw-info-line` border,
`--aw-info-soft`), padding 9 px 14 px: spinner, the headline in `--aw-info`
600 `--aw-text-sm`, the state line `Running · step 7 of 7 · 13m so far` in
`--aw-ink-soft`, then a 4 px progress bar that fills the remaining width
(`--aw-info-line` track, `--aw-info` fill, `settled / total` of the workflow's
stages, hidden when the workflow has one stage), then the `Watch it` button as
today. The waiting-for-approval variant keeps today's amber treatment.

An `open_point` next step (a review debt) keeps its amber band, drawn in the
same slot below the live band. The `stage` next step no longer gets a band:
the current phase header says it (see 4).

### 4. Phase sections

One card per phase, in `PLAN_PHASES` order, 12 px apart. Each has a header and,
when expanded, one row per stage. Three states:

| State | Card | Header | Numeral | Expanded by default |
|---|---|---|---|---|
| **done** (every stage held) | `--aw-border` solid, `--aw-panel` | `--aw-raised` | 22 px filled circle, `--aw-teal`, white numeral | no |
| **current** (holds the lead stage, or any live stage; else the first phase with an owed stage) | `--aw-teal-line` solid | `--aw-teal-soft` | 22 px circle, 2 px `--aw-teal` ring, white fill, teal numeral | yes |
| **later** | `--aw-border-strong` **dashed**, transparent background | none | 22 px circle, 1.5 px dashed `--aw-border-strong` ring, `--aw-muted` numeral | no |

Header anatomy (padding 9 px 16 px, 12 px gaps, all one line): numeral · title
(`--aw-text-base` 600 `--aw-ink-strong`; `--aw-ink-soft` on a later phase) ·
state text (`2 of 2 filed` in `--aw-teal-strong` 600; `0 of 3 filed` in
`--aw-muted`; `4 stages · after planning` in `--aw-muted`) · on the current
phase a `NEXT` badge (`--aw-teal` fill, white, 11 px 700 uppercase, pill) · a
1 px vertical rule and the stage names joined by ` · ` on a collapsed phase ·
right-aligned: the phase's total elapsed time (sum of its stages' history) on a
done phase, `Nothing is blocking it.` on the current phase when its lead stage
is runnable, a chevron on a collapsed phase.

Clicking a header toggles the phase. The choice lives in component state for
the visit; it is not persisted. Refreshing the record keeps the user's toggles.

The `NOW` divider line and the `9 stages have not run` note are gone; the phase
states say the same thing.

### 5. Stage rows

Grid `16px minmax(0, 1fr) auto 120px`, column gap 14 px, padding 11 px 16 px,
`--aw-border` top rule, vertically centred. Cells:

1. **Status dot**, centred: 10 px `--aw-teal` disc when held; `--aw-warn` when
   held with `completed_with_issues` or `needs_review`; 10 px white disc with a
   2 px `--aw-teal` ring for the lead stage; 9 px white disc with a 1.5 px
   dashed `--aw-border-strong` ring when owed; `--aw-info` disc with the
   existing pulse animation when running.
2. **Name cell**, a flex row, 10 px gap, `min-width: 0`: the stage icon
   (16 px, `--aw-teal`; `--aw-muted` when owed) · the work product label
   (`--aw-text-base` 600 `--aw-ink-strong`, a `RouterLink` where it has a
   destination; `--aw-muted` when owed) · the count as a bare number
   (`--aw-text-sm` 700 `--aw-teal`, tabular) · the row's sentence
   (`--aw-text-sm` `--aw-ink-soft`, ellipsised) · then, after a 1 px rule, the
   doors: artifact doors as today (`Documents 14`, `Tables 24`), tool doors as
   today (dashed, wrench, `Query`).
   The pill around the work product is gone. The label is the link.
3. **Meta cell**, right of centre: the chips (`3 flags` amber, `4 attempts`
   neutral, exactly today's `.sig` chips) then `10:54 · 6m 54s` in
   `--aw-text-sm` `--aw-muted` tabular. On a waiting row this cell holds the
   dependency in italic `--aw-muted`: `after the memorandum`.
4. **Action cell** (120 px, right-aligned): the `Run` button on the lead stage
   (primary, `SplitButton` when the stage has alternates, otherwise `Button`);
   a `Import more` outlined secondary on Sources; a chevron on a held row that
   is foldable; empty otherwise.

Wording changes on owed rows: the sentence is the stage's imperative headline
(`Build the risk and control matrix`), and the dependency moves to the meta
cell as `after the memorandum` (lower-case, italic) instead of the sentence
`Waits for the memorandum.` under a `not yet` pill. Derive it from
`blocked_reason` by dropping the leading `Waits for` and the trailing full stop;
fall back to the raw reason where it does not start that way.

Everything a held row shows when opened (the summary paragraph, the tally, the
highlights, open points, the attempts list, `Running again`) is unchanged. The
Concise / Full toggle keeps governing it.

### 6. Footer

One line, `--aw-text-xs` `--aw-muted`, tabular: `14 runs · 13 attempts at 3
stages`, and on the right `1 run filed nothing` when there is such a run.
`work_products` and `elapsed_ms` moved up into the strip.

### 7. Responsive

Keep the existing `@container (max-width: 56rem)` rule's intent: below it the
meta cell drops under the name cell and the action cell stays on the first
line. The strip labels may wrap to two lines; the segments must not.

## Backend: a phase on every stage

The record payload (`backend/app/engagement_record.py`, `record()` and
`_stages()`) has no phase today. `plan_phases()` in `backend/app/engagement.py`
already groups capabilities into phases by the capability id's domain, the part
before the first dot, through `_PHASE_OF_DOMAIN`.

1. **Fix the mapping gap.** `_PHASE_OF_DOMAIN` has no entry for `doc_tests`, so
   `doc_tests.executed` (the *Document test results* row) falls into the
   `other` / *Further steps* catch-all. Add `"doc_tests": "fieldwork"`. The
   existing test in `backend/tests/test_engagement_brief.py` (around line 87)
   asserts *Further steps* stays empty for the default template, but it walks
   `plan_outcomes()`, not the record spine, so add a record-level assertion
   as well (step 4 below).
2. **Add `phase` to each stage** in `_stages()`: the phase id for the
   capability's domain, `other` where unmapped. Factor the domain lookup out of
   `plan_phases()` so the two cannot drift.
3. **Add `phases` to the payload**: `[{id, title, summary}]` from
   `PLAN_PHASES`, filtered to the phases that have at least one stage, in
   `PLAN_PHASES` order. The frontend draws sections from this list, never from
   a copy of the titles.
4. **Tests** in `backend/tests/test_engagement_record.py`: every stage's
   `phase` is the id of an entry in `phases`; the default spine puts no stage in
   `other`; `doc_tests.executed` is in `fieldwork`; `phases` keeps
   `PLAN_PHASES` order.

The default spine grouped, for checking the result:

| Phase | Stages (capability ids) |
|---|---|
| Understand the data | `sources.imported`, `analysis.executed` |
| Read the documents | `documents.analysis_generated` |
| Plan the engagement | `planning.apm_ready`, `planning.rcm_ready`, `tests.specified` |
| Do the fieldwork | `doc_tests.executed`, `fieldwork.executed`, `results.rolled_up`, `findings.drafted` |
| Write it up | `report.working_draft`, `audit.verified` |

Note that `_SPINE` order and `_positions()` order stay the row order inside a
phase; the phase only groups.

## Frontend: `EngagementRecordTab.vue`

Keep everything in the `<script>` that reads the run in flight (`liveStages`,
`liveState`, `live`, `soleRunning`, `activityLine`), the density preference,
`chips`, `foldable`, `start` and the toast handling. What changes:

1. `frontend/src/types.ts`: `phase: string` on `EngagementStage`;
   `phases: Array<{ id: string; title: string; summary: string }>` on
   `EngagementRecordPayload`.
2. **Grouping.** A computed `groups` maps `payload.phases` to
   `{ phase, stages, state, held, total, elapsedMs }` where `state` is
   `done | current | later` by the rule in section 4. Exactly one group is
   `current`; if nothing is owed, none is.
3. **Collapse state.** `openPhases = ref<Set<string>>` seeded from the group
   states on first load and whenever the current phase changes id; a header
   click toggles.
4. **Strip.** A computed list of `{ phase, segments: Array<'held'|'live'|'lead'|'owed'> }`
   and the `grid-template-columns` string from segment counts.
5. **Template.** Replace the `<ol class="ledger">` with the phase sections.
   Delete the `.head`, `.nowline`, `.tm` and `.gut` markup and CSS; delete the
   `stage`-kind `.brief` under the ledger; move the summary footer's first two
   numbers into the strip. Row markup follows section 5; most of the existing
   `.say` body (`.dsc`, `.left`, `.again`, `.tally`, `.hl`, `.open`, `.tries`,
   `.attempts`) is reused as is.
6. **Tests** in `frontend/src/components/EngagementRecordTab.test.ts`: a
   payload with the table above renders five sections; only *Plan the
   engagement* is open; its header carries `NEXT` and the lead row the only
   `Run` button; a later phase header lists its stage names; the strip renders
   twelve segments with the right classes; a live stage colours its segment
   and the band shows the step count; the density toggle still folds and
   unfolds a held row's body; clicking a done phase header opens it.

## Order of work

Each step lands on its own and leaves the tab working.

1. Backend phase mapping fix, `phase` and `phases` in the payload, tests.
2. Types and the grouping computed, rendering today's rows inside phase
   sections with headers (no row redesign yet). This is the step to screenshot
   against the mockup.
3. Row redesign (section 5) and the removal of the header, gutter, now line and
   `stage` band.
4. Progress strip and the footer move.
5. Delete the dead CSS and the `.card` pill styles; run the frontend tests and
   the container-width check at 1366 px.

## Token map for the mockup's hex values

| Hex in the mockup | Token |
|---|---|
| `#f6f8fb` | `--aw-canvas` |
| `#ffffff` | `--aw-panel` (and `--aw-on-accent` on a teal fill) |
| `#eef2f7` | `--aw-raised` |
| `#dce5ee` | `--aw-border` |
| `#c5d2e0` | `--aw-border-strong` |
| `#07162b` | `--aw-ink-strong` |
| `#0d2340` | `--aw-ink` |
| `#46576d` | `--aw-ink-soft` |
| `#5a6a81` | `--aw-muted` |
| `#627081` | `--aw-muted-strong` |
| `#0f766e` | `--aw-teal` |
| `#0b625c` | `--aw-teal-strong` |
| `#e7f7f4` | `--aw-teal-soft` |
| `#a7ded8` | `--aw-teal-line` |
| `#b45309` | `--aw-warn` |
| `#8a4308` | `--aw-warn-ink` |
| `#fdf1e3` | `--aw-warn-soft` |
| `#f0cf9f` | `--aw-warn-line` |
| `#1d4ed8` | `--aw-info` |
| `#eff6ff` | `--aw-info-soft` |
| `#bfdbfe` | `--aw-info-line` |
| 11.5 px | `--aw-text-xs` |
| 12.8 px | `--aw-text-sm` |
| 14 px | `--aw-text-base` |
| 8 px radius | `--aw-radius-control` |
| 12 px radius | `--aw-radius-surface` |
| 999 px radius | `--aw-radius-pill` |

Icons in the mockup are inline SVG stand-ins for the PrimeIcons the tab already
uses (`FILED_ICONS`); keep the PrimeIcons.
