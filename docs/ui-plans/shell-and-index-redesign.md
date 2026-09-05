# Shell header and engagements index redesign: one bar, one list

**Status:** review pass and design proposed on 5 September 2026. **The header
is built** (steps 1, 2 and the header's share of step 4 — see "What was built
differently" at the end); the index (step 3) and the rest of the deletion pass
are not. Every claim about what the code did before that was read from the
working tree on top of commit `7904d1d` (the uncommitted changes there are the
built sources, planning and reporting pages and the assistant panel) and from
the running app against the four local engagements. The first section is the
review pass over the five earlier plans; the rest is the handoff for the two
surfaces those plans did not touch — the bar across the top of every page, and
the page the product opens on.

It uses the vocabulary of [`fieldwork-views-redesign.md`](fieldwork-views-redesign.md)
without restating it: the 36 px page header with one count sentence and one
primary, list rows with a dot and a meta line, cards at `--aw-radius-surface`,
no modals.

## Review pass: where the landed redesigns stand

| Plan | Its own status line | What the tree shows |
|---|---|---|
| [`engagement-record-redesign.md`](engagement-record-redesign.md) | "not yet implemented" | **Built.** The 32 px toolbar, the progress strip, the phase cards with `NEXT` and the stage rows are all in `EngagementRecordTab.vue`. The stale status line has since been corrected. |
| [`fieldwork-views-redesign.md`](fieldwork-views-redesign.md) | built | Built, with the six departures it records. Step 7 (delete dead CSS) is half done — see below. |
| [`cycle-design-evaluation.md`](cycle-design-evaluation.md) | built | Built; `cycle` sits between `apm` and `coverage` as step 5f decided. |
| [`sources-planning-reporting-redesign.md`](sources-planning-reporting-redesign.md) | all five pages built | Built. `ReportTab.vue` and `ReportReconcileDialog.vue` are deleted, `ReportView.vue` and `ApmView.vue` replace them. |
| [`assistant-panel-redesign.md`](assistant-panel-redesign.md) | built | Built. `AgentDrawer`, `ChatHistoryPanel` and `ConsoleView` are deleted; `/console` redirects. |

What the five left behind, found by reading the tree rather than the plans:

1. **Two headers, written twice.** `App.vue` draws a 56 px navy header and
   hides it on four route names (`WORKSPACE_ROUTES`); `WorkspaceView.vue` draws
   a 60 px navy header of its own with the same brand mark, the same gradient
   and the same `.brand` rules copied in. The allowlist has to be kept in step
   with `router.ts` by hand — the fieldwork plan's own "What was built
   differently" names it as an addition the plan implied but did not spell out.
   The two headers also disagree: the index shows an account menu with the
   user's name and `About`; the workspace shows a sign-out icon, no name, and
   six more icons.
2. **Three crumb bars and two label maps.** `AuditFileView.vue`,
   `WorkbenchView.vue` and `RcmRowView.vue` each draw the `.crumb` row, and the
   first two each keep a `SECTION_LABEL` map that restates the record's row
   labels. The maps and the pages already disagree: the crumb over the data
   tests says `Test programme` while the page's own `h1` says `Data tests`;
   the crumb over the document tests says `Document test results`.
3. **Dead CSS.** `WorkspaceView.vue` still carries the whole `.surface-switcher`
   block (60 lines) for the switcher the assistant plan removed; `HomeView.vue`
   carries `.portfolio-strip` (6 rules) and `.empty` for markup that no longer
   exists.
4. **Three components nothing imports** outside their tests:
   `agent/ActionRequired.vue`, `agent/AgentSummary.vue`,
   `ui/UiVerdictStatus.vue`.
5. **Two pages still on the old page header.** `AnalysisTab.vue` and
   `planning/ChainView.vue` use `UiPageHeader` (an `h2` at `--aw-text-xl`, no
   count sentence) — the only two pages not on the 36 px system. Neither was
   in a plan; they should be, in a later round.
   *Since closed by [`analysis-redesign.md`](analysis-redesign.md), which also
   found a third — `CycleTab.vue` used the same classes without the component —
   and deleted `UiPageHeader` once none was left.*
6. **Appearance and account controls are placed by accident.** Theme and
   presentation size exist only inside a workspace, so the index has no theme
   toggle; the workspace header has eight icon-only controls whose only label
   is a tooltip.
7. **The index describes a field nobody can fill.** Cards render
   `ws.description` but `NewEngagementDialog.vue` never sends one (it posts the
   name and, separately, the brief). The brief's `entity` and `period`, which
   the dialog does collect, appear nowhere on the index.
8. **Vertical spend.** On a work product page at a 900 px window the reader
   meets 60 px of header, 39 px of crumb bar and a 36 px page header — 135 px,
   15 % of the window, before the first row of the thing they opened.

Items 3 and 4 are a deletion pass and belong to step 4 below; items 1, 2, 6
and 8 are what the header redesign is for; item 7 is what the index redesign
is for; item 5 is noted for a later round.

## The design reference

- **Design canvas (pan, zoom, inspect, export PNG):**
  <https://claude.ai/code/artifact/861cf952-bdbc-4ceb-8937-1f4c981ac2da>
- **Exact markup**, one file per artboard, in [`shell-and-index/`](shell-and-index/).
  Generated from [`gen_shell.py`](shell-and-index/gen_shell.py) in the same
  folder; regenerate from the script rather than editing the HTML by hand.

| Artboard | File | What it shows |
|---|---|---|
| Header on a work product | [`Main.dc.html`](shell-and-index/Main.dc.html) | The 44 px header with the trail built in, over the risk and control matrix as built |
| Header states | [`HeaderStates.dc.html`](shell-and-index/HeaderStates.dc.html) | Index, record, work product, RCM row, attention, diagnostics, the two menus open, and the bar under 1,280 px |
| Engagements index | [`Engagements.dc.html`](shell-and-index/Engagements.dc.html) | The list, with the four local engagements' real data |
| Engagements index, empty | [`EngagementsEmpty.dc.html`](shell-and-index/EngagementsEmpty.dc.html) | Where the hero's copy goes |
| Alternate: compact cards | [`EngagementsCards.dc.html`](shell-and-index/EngagementsCards.dc.html) | The same fields as cards, for comparison |
| Alternate: light header | [`HeaderLight.dc.html`](shell-and-index/HeaderLight.dc.html) | The same anatomy on `--aw-panel` instead of navy |

All six are drawn at 1,440 px wide. The index rows carry what the API returned
on 5 September for `Procurement`, `TreasuryFull`, `Treasury` and `Expenses`:
the phase states from `/api/workspaces`, and the next step, open points,
counts and run totals from each engagement's `/engagement/record`. Like the
earlier mockups they use **literal hex values**; the implementation must use
the tokens in `frontend/src/style.css` (map at the end). Two artboards are
alternates rather than the proposal, and the sticky notes beside them say
what each trades.

## What is wrong today, per surface

**The header** (`App.vue` and `WorkspaceView.vue`)

1. It says where you are twice and where you can go nowhere. The left third —
   brand mark, wordmark, a rule, the eyebrow `Engagement` and the name — spends
   about 330 px on identity, and the name is not a link. The place you are in
   (the matrix, the row) is on a second bar below. There is no way to reach a
   different engagement except through the index.
2. The right cluster is ten controls: two labelled buttons and eight icons.
   Three of the icons are navigation (`Diagnostics`, `About`, `All
   workspaces`), two are appearance, one is sign-out; none is used more than
   once in a sitting, and each costs the same 32 px as `Assistant`, which is
   used constantly.
3. The crumb bar is a whole row for one link and one label. `← Engagement
   record` in teal is the only way back and the only thing on the row that
   does anything.
4. Below 1,280 px the wordmark hides but the eight icons do not compact, so the
   engagement name is what gives way.
5. The debug view keeps the global header *and* draws a 70 px band of its own
   (`← Engagement | LOCAL DIAGNOSTICS`, `Live`, `Clear`): three rows of chrome.

**The index** (`HomeView.vue`)

1. A hero of about 120 px explains the product to someone who has four
   engagements open. Its eyebrow says `Engagement index`, its title says `Your
   audit workspaces`, and the header tooltip says `All workspaces`; the rest of
   the product says *engagement*.
2. A card states the table count and the creation date. Neither is the
   question. The backend already computes, for the record page, the next step
   (`next.action`, `next.message`), how many open points there are, how many
   documents and tables were imported, and when the assistant last ran; the
   index shows none of it.
3. The strip says a phase needs attention but not why, and cards sit in
   creation order, so the one that wants you is wherever it happens to be.
4. The skeleton draws three 12 rem boxes for a list that has four cards; the
   kebab holds only `Delete`.

## The design, element by element

### 1. One header, 44 px, on every page

Drawn once, in `App.vue`, on every route including the debug view. Navy as
today (`--aw-navy-900` → `--aw-navy-950`), `--aw-shadow-sm`, padding `0 16px`,
gap 4 px between trail pieces and 8 px between right-cluster controls. Left to
right:

| Piece | Spec | Behaviour |
|---|---|---|
| Brand mark | 26 × 26, radius 7, the mint gradient as today, `pi-verified` | Link to the index. Always present. |
| Wordmark | `Audit Workbench`, 14 px 700, `--aw-on-dark` | **Only when the trail is empty** (index, login). Inside an engagement the trail is the identity. |
| Separator | `pi-chevron-right` 13 px at 32 % white | Between every two pieces. |
| Engagement crumb | Name 13 px 600 `--aw-on-navy` (`--aw-on-dark` when it is the current page), padding 4 px 8 px, radius 6; a 13 px `pi-chevron-down` beside it in `--aw-on-navy-muted` | **Split control.** The name opens the record — it replaces `← Engagement record`. The chevron opens the switcher (below). Hover fills 10 % white. Max width 280 px, ellipsis. |
| Section crumb | 13 px 600; `--aw-on-navy` as a link, `--aw-on-dark` as the current page | The record's own row label for the section, from one shared module (see frontend work). |
| Row crumb | `--aw-font-mono` 12.5 px 600 | Only on `/coverage/:rowId`. |

Right cluster, at most two labelled buttons and one kebab, all 30 px tall,
radius 7:

| Control | Spec | Notes |
|---|---|---|
| `Assistant` | Filled `--aw-teal`; `--aw-teal-600` while the panel is open; `--aw-warn` with `--aw-ink-strong` text while a run awaits approval, input or has failed; label `Assistant · working` while a run is live, `Assistant · needs you` in the amber state | Exactly the built toggle's states, kept. Inside an engagement only. |
| `Import` | Ghost: 9 % white fill, 18 % white border | Inside an engagement only. |
| Kebab | Ghost, 30 × 30 | Everywhere. Holds: `Presentation size` (checked state), `Theme · System` (cycles system → light → dark, label shows the current), a rule, `Diagnostics` (engagement only), `About Audit Workbench`, a rule, the signed-in address as a disabled row (multi-user only), `Sign out` (multi-user only). |

The `All workspaces` icon goes: the brand mark and the switcher's last entry
both reach the index.

**The switcher** (under the engagement crumb, 300 px, `--aw-panel`,
`--aw-radius-surface`, `--aw-shadow-md`): the eyebrow `ENGAGEMENTS`, one row
per engagement from the listing the index already loads — `pi-briefcase`, the
name, and a 64 px four-segment strip in the index's colours — the current one
pressed in `--aw-teal-soft`; a rule; `All engagements`; `New engagement` in
teal, which opens the same dialog the index opens. Rows sort as the index
sorts. The listing is fetched when the menu first opens and cached for the
session; the current engagement is drawn from the shell's own state so the
menu opens with at least one row.

**Under 1,280 px:** the two labelled buttons drop to icons with their labels
as `aria-label` and tooltip; the trail truncates its middle piece first (the
section) and the current piece last; the engagement crumb's max width falls
to 180 px. Nothing wraps.

**The debug view** keeps the header (trail `Procurement › Diagnostics`), drops
its own band, and puts `Live` and `Clear` on a 36 px page header row per the
shared system.

**Budget.** A work product page goes from 60 + 39 = 99 px of chrome to 44; the
record from 60 to 44; the index from 56 to 44; diagnostics from 56 + 70 to
44 + 36. The 36 px page header on every page is unchanged.

### 2. The engagements index

A page on the same 1,440 px `.page` container as today (`padding: 20px 32px
28px`), three things on it:

**Page header (36 px)** per the shared system: `h1` `Engagements`; the count
sentence `4 engagements · 3 need attention · 1 not started` (an engagement
"needs attention" when any phase is `attention`; "not started" when every
phase is; the sentence omits a zero count); `New engagement` as the one
primary. The hero goes. Its copy moves to the empty state, which already had
most of it, and the empty state keeps the one primary while the list is empty
(the header's button hides then, as today's hero button does).

**The list**, one card (`--aw-border`, `--aw-radius-surface`, `--aw-panel`,
overflow hidden), a header row on `--aw-raised` in `--aw-font-mono`
`--aw-text-2xs` uppercase, one row per engagement, `--aw-border` rules between
rows, padding `12px 16px`, gap 20 px, columns:

| Column | Width | Content |
|---|---|---|
| Engagement | `minmax(220px, 1.3fr)` | 28 px `pi-briefcase` tile in `--aw-teal-soft`; the name 14 px 600 `--aw-ink-strong`, a link to the record. |
| Where it stands | 228 px | `WorkspaceProgress` exactly as built — four segments, four labels, the same four colours, the `attention` label in `--aw-warn-ink`. |
| Next | `minmax(280px, 2fr)` | Line 1: the record's `next.action` as a link (`--aw-warn-ink` when the engagement needs attention, `--aw-teal` otherwise) then ` · ` and `next.message`, one line, ellipsis. Line 2: the open-points pill (`N open points`, warn tone, `--aw-text-2xs`) or `Nothing is blocking it` in `--aw-muted`. |
| Sources | 150 px | `8 documents` over `18 tables`, tabular. |
| Last activity | 150 px | The last run's date and time (`5 Sep, 05:28`) over `17 runs · 39 min` in `--aw-muted`. |
| Kebab | 30 px | `Open record`, `Open assistant` (the record with `?assistant=full`), a rule, `Delete engagement…` with today's confirm. |

The whole row opens the record; the `Next` link opens the destination the
record names (`nav.to(next.destination)`), which is the same jump the record's
`NEXT` row offers one click later. Rows sort engagements that need attention
first, then by last activity, newest first — the order the strip already
implies but the grid never applied. A search field (`Search engagements`) joins
the header row once there are more than eight; below that a list of eight
names needs no filter.

**Loading:** four skeleton rows at the row height, not three 12 rem boxes.
**Empty:** the card as today with the hero's lede folded into it
(`EngagementsEmpty.dc.html`).

**Words.** `workspace` → `engagement` in every label, tooltip and toast on
the index and in the header (`Delete engagement`, `All engagements`,
`Engagement not found`). Routes and API paths keep `workspace`; renaming a URL
buys nothing a reader can see.

**Alternate, not proposed:** `EngagementsCards.dc.html` draws the same fields
as 4-up cards. Cards read well up to six; the list's aligned `Next` and `Last
activity` columns are what make eight or twenty scannable, and the index is
the one surface where the count is expected to grow. If the auditor prefers
the cards, everything in "Backend and data" still applies.

## Backend and data

Nothing new for the header.

For the index, `workspaces.list_workspaces()` gains five fields per entry,
read from what the engagement record already derives:

| Field | Source | Shape |
|---|---|---|
| `counts` | the record's `counts` | `{documents, tables}` |
| `next` | the record's `next` | `{kind, action, message, destination}` or `null` |
| `open_point_count` | `len(record["open_points"])` | int |
| `last_at` | the record's `totals.last_at` | ISO timestamp or `null` |
| `runs` | the record's `totals.runs`, `totals.elapsed_ms` | `{count, elapsed_ms}` |

`_listed_progress(ws)` becomes `_listed_index(ws)` and returns `progress` plus
these, so a workspace whose record cannot be read still lists with the
fields it can state. The cost question is the one `engagement_progress.progress`
already answered: memoize the extra fields in the same revision-keyed cache
(`_cache`, keyed on `(root, revision)`), so the listing recomputes an
engagement exactly once per write and a listing that changed nothing costs
what it costs today. The record's own `_open_points` and `totals` are what the
record page computes on every open; the index computes them once per revision
instead.

`WorkspaceListItem` in `types.ts` gains the same five fields, optional, so the
list renders a row from a payload that lacks them.

Out of scope but noted: there is no `PATCH /api/workspaces/{id}` to rename an
engagement, so the kebab cannot offer `Rename` until there is.

## Frontend work, by file

- **New `components/shell/AppHeader.vue`**: the 44 px bar. Reads the trail and
  the right-cluster teleport target; draws brand, wordmark, crumbs, kebab.
  Owns the appearance and account items (moved from `WorkspaceView.vue` and
  `App.vue`). Renders the switcher popover (`UiOverflowMenu`-style
  `Popover`, not a modal).
- **New `composables/useShell.ts`**: module-level reactive state —
  `trail: Ref<Crumb[]>` (`{ label, to?, mono? }`), `engagement: Ref<{ id,
  name } | null>`, and `setTrail()` / `clearTrail()` that surfaces call in
  `onMounted` / `onUnmounted` (or a `watchEffect` on their route props).
  Nothing here knows about the agent.
- **`App.vue`**: `<AppHeader>` on every route; `WORKSPACE_ROUTES`, the
  `v-if`, the second `<header>` and the `.brand` / `.about-link` CSS go. An
  empty `<div id="shell-actions">` inside the header is the teleport target
  for the two engagement buttons.
- **`WorkspaceView.vue`**: the `<header class="workspace-header">` and all of
  its CSS go (including the dead `.surface-switcher` block). Sets
  `useShell().engagement` once the workspace loads. `<Teleport to="#shell-actions">`
  carries the `Assistant` toggle and `Import` button with their existing
  bindings to `useAgentRun`, so the header shows agent state without owning
  agent state. `showAccount` / `signOut` move to the header.
- **`useWorkspaceNavigation.ts`**: add `destinationLabel(destination)`,
  the one label map, taken from the record's row labels
  (`EngagementRecordTab.vue`'s `filed.label` vocabulary). `AuditFileView.vue`
  and `WorkbenchView.vue` delete their `SECTION_LABEL` maps and call it.
  Check the two disagreements first (`Test programme` / `Data tests`,
  `Document test results`) and pick one name per section that the page `h1`,
  the record row and the crumb all use.
- **`AuditFileView.vue`, `WorkbenchView.vue`, `RcmRowView.vue`**: delete the
  `<nav class="crumb">`; call `setTrail()` with their piece(s). `RcmRowView`'s
  print stylesheet drops `.crumb` from its hide list. `style.css` drops the
  `.crumb` block.
- **`DebugView.vue`**: delete `.debug-header`; `Live` and `Clear` move to a
  36 px page header row; sets the `Diagnostics` trail piece.
- **`HomeView.vue`**: hero deleted; page header; the list replaces the card
  grid; `WorkspaceProgress` reused unchanged; `NewEngagementDialog` unchanged;
  kebab items; sort; skeleton rows; `.portfolio-strip` and `.empty` CSS
  deleted. Rename `workspace` → `engagement` in copy.
- **`types.ts`**: `WorkspaceListItem` gains `counts?`, `next?`,
  `open_point_count?`, `last_at?`, `runs?`.
- **Backend `workspaces.py`**: `_listed_index`; **`engagement_progress.py`**:
  cache the extra fields beside `progress`.
- **Delete**: `agent/ActionRequired.vue`, `agent/AgentSummary.vue`,
  `ui/UiVerdictStatus.vue` and their tests, once a grep confirms no
  template reference (the sweep above checked imports only).

Tests to update: `AuditFileView.test.ts` and `WorkbenchView.test.ts` assert
`.crumb__cur` / `.crumb__back` — they become assertions on `useShell().trail`;
`useWorkspaceNavigation.test.ts` gains `destinationLabel`;
`WorkspaceProgress.test.ts` unchanged; new `AppHeader.test.ts` (wordmark only
with an empty trail; kebab contents per mode; the switcher lists the current
engagement before the listing arrives) and `HomeView.test.ts` (sort order,
count sentence, header button hidden while empty, row without the optional
fields still renders). Backend: `test_workspace_routes` (or wherever
`list_workspaces` is covered) asserts the five fields and that a workspace
whose record raises still lists.

## Order of work

Each step lands on its own.

1. **The header.** `useShell`, `AppHeader`, the teleport; delete the
   `WorkspaceView` header and `App.vue`'s allowlist; the three crumb bars
   become `setTrail` calls over one label module. The switcher can ship a
   step later — the crumb is a plain link until it does.
2. **The kebab** and the appearance controls on every page, including the
   index; the `Diagnostics` band.
3. **The index**: backend fields, then the list, sort, skeleton, words.
4. **The deletion pass**: the three unused components, `.surface-switcher`,
   `.portfolio-strip`, `.empty`, `.crumb`, and the stale status line in
   `engagement-record-redesign.md`.

## Token map for the mockups' hex values

Same as the fieldwork plan, plus:

| Value in the mockup | Token |
|---|---|
| `#0d2340` → `#07162b` header gradient | `--aw-navy-900` → `--aw-navy-950` |
| `#e6edf6` / `#8fa6c2` | `--aw-on-navy` / `--aw-on-navy-muted` |
| `#5eead4` → `#2dd4bf` brand mark | `--aw-mint` → `--aw-mint-600` |
| `#0f766e` / `#0d9488` Assistant fill / pressed | `--aw-teal` / `--aw-teal-600` |
| `rgba(255,255,255,0.09)` / `0.18` ghost button | as `WorkspaceView.vue` writes them today for `.p-button-secondary` on navy; give them names (`--aw-on-dark-wash`, `--aw-on-dark-line`) when the header moves |
| `rgba(255,255,255,0.32)` separator | `--aw-on-navy-muted` at 60 % is close; a named token is better |
| 44 px header, 30 px controls, 26 px mark | `2.75rem`, `1.875rem`, `1.625rem` — or one `--aw-shell-height` |
| 13 px / 12.5 px trail type | `--aw-text-sm`, `--aw-text-xs` in `--aw-font-mono` |
| 10.5 px mono uppercase header cells | `--aw-text-2xs` in `--aw-font-mono`, as the built grids do |

Icons are inline SVG stand-ins for the PrimeIcons the app uses; keep the
PrimeIcons (`pi-verified`, `pi-chevron-right`, `pi-chevron-down`,
`pi-briefcase`, `pi-sparkles`, `pi-upload`, `pi-ellipsis-v`).

## What the mockups assume

- **Last activity is the last agent run.** `totals.last_at` moves when a run
  finishes, not when a file is imported or a row is edited by hand. If the
  index should reflect manual work too, the manifest's write time is the other
  candidate; the record does not expose it today.
- **The count sentence's "need attention"** counts engagements with any
  phase in `attention`, which is how `WorkspaceProgress` colours them. Three
  of the four local engagements qualify, so the sentence is honest about the
  demo data even if it reads bleakly.
- **Run durations** (`39 min`, `1 h 8 min`) are `totals.elapsed_ms` rounded;
  the record page shows the same figure per stage.
- **`Expenses`'s next step** is a stage (`Analyse the imported documents`,
  action `Run`), not an open point; the row draws `Nothing is blocking it`
  from the record's `blocked_reason` being empty.
- **The kebab's `Theme · System`** cycles on click as the icon does today. A
  submenu with three radio rows is the other reading; the mockup keeps the
  cycle because it is one row.
- **The switcher's strips** reuse the listing's `progress`; the menu shows
  four segments including `Data`, exactly as the index rows do.
- **The signed-in row** reads `Signed in as [email]`; the real address comes
  from `useSession`.

## What was built differently

Steps 1, 2 and the header's share of step 4 landed on 5 September 2026. Four
departures from the design above, each deliberate:

1. **The chevron opens the engagement's own views, not other engagements.**
   The plan put an engagement switcher under the name — the index's listing,
   with each entry's progress strip. What shipped is `SectionSwitcher.vue`: the
   engagement record, then the work products in file order, then the sources
   and the bench, with the view you are on marked. The switcher the plan
   described answered a question the brand mark already answers in one click;
   the one that was actually missing is the surface rails, which went when the
   record became the index and left moving between two work products a trip
   back to the record. Reaching another engagement is the brand mark or the
   index. `New engagement` therefore does not appear in the header at all, and
   the header hosts no `NewEngagementDialog`.
2. **Page names, not the record's row labels.** The plan said the trail should
   use "the record's own row labels". It cannot: the record names the artifact
   a stage *filed* (`Test programme`, `Document test results`, `Fieldwork
   results`) and three of its rows can point at one page. `DESTINATION_LABEL`
   in `useWorkspaceNavigation.ts` is therefore a vocabulary of its own — each
   page's own `h1` — which is what resolves the two disagreements the review
   found: the trail now says `Data tests` and `Document tests`, matching the
   headings directly under it. The record's labels are untouched.
3. **Surfaces publish through `useTrail` / `useEngagementCrumb`, not
   `setTrail` / `clearTrail`.** The plan's shape had each surface clear on
   unmount. Vue runs `onUnmounted` as a post-render effect, so on a route
   change the incoming surface has already published by the time the outgoing
   one is torn down: an unguarded clear wiped its successor's trail and the bar
   went blank on every move between two work products. The two composables
   claim the slot on publish and only clear a slot they still hold.
   `useShell.test.ts` holds that hand-off.
4. **`Import` is not in the header.** The plan gave the right cluster two
   labelled buttons; it has one. Importing is something an engagement needs a
   few times and then never again, and it is already offered everywhere it is
   actually wanted — the record's Sources row, `Add documents` and `Add files`
   on the two pages that hold them, the assistant's own prompt, and a drop
   anywhere on the window. `Assistant` is the only control the bar carries, and
   the ghost-button rules that dressed `Import` for the navy went with it.
5. **`--aw-on-accent` on the filled toggle.** The built `Assistant` toggle
   used `--aw-on-dark` (white) on `--aw-teal`, which is white on `#5eead4` once
   the dark palette flips the accent. The bar is navy in both themes but the
   accent is not, so the filled and the amber states both take the token
   written for text on a solid accent fill.

Also landed with them: `--aw-shell-height` / `--aw-shell-control` and the four
`--aw-on-dark-*` washes as tokens; the `.crumb` block and `WorkspaceView`'s
`.surface-switcher` deleted; the print stylesheet's `.workspace-header` becomes
`.app-shell`; `main`'s workspace class keyed on the `/workspace/` path prefix
rather than on the route allowlist.

Still open from this plan: step 3 (the index, and the five backend listing
fields it needs), and the rest of step 4 — `agent/ActionRequired.vue`,
`agent/AgentSummary.vue`, `ui/UiVerdictStatus.vue` and `HomeView.vue`'s
`.portfolio-strip` / `.empty` CSS.
