# UI demo-readiness plan

A frontend UI/UX review of the workbench carried out on 2026-08-11 against `main` @ `5d69af1`,
covering all 14 routes and their dialogs at 1366, 1440 and 1920 px, in two states: the populated
`Workspaces/procurement` engagement and a fresh empty workspace on an isolated `WORKBENCH_DATA`
root. Full write-up with before/after mockups:
<https://claude.ai/code/artifact/a5158b45-bf05-40d6-9d9b-d134f8f77160>

**28 items.** Each one is independently shippable — no item depends on another except where an
item says so. Work them in any order; the running order in §10 is the recommended one.

## Verdict

The populated screens are strong. The RCM board, the evidence-linked findings editor, the
document viewer with page citations, and the agent transcript with its plan spine are all things
most audit tools do not have, and they look like it.

The gap is that a demo does not start there. It starts on an empty workspace, and the empty
workspace currently tells the audience that fieldwork is complete when nothing has run, offers to
draft an APM before any data exists, and shows a bare console with no route to the one action
that matters. Once data is loaded, three of the most-visited screens squeeze their content into
rails too narrow to read.

Nothing here needs re-architecting.

## Status legend

| Mark | Meaning |
|---|---|
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Done |
| `[-]` | Deliberately skipped — record why on the item |

Severity is fixed by the review and does not change as work proceeds:

- **Blocker** — an audience will see it and it undercuts the pitch.
- **Rough** — reads as unfinished.
- **Polish** — worth it if there is time.

## Progress

| Section | Items | Blocker | Rough | Polish | Done |
|---|---|---|---|---|---|
| 01 Opening and engagement setup | 5 | 3 | 1 | 1 | 4 |
| 02 File upload and import | 3 | 0 | 1 | 2 | 1 |
| 03 Planning — APM and RCM | 4 | 1 | 2 | 1 | 3 |
| 04 Data tests and document tests | 5 | 1 | 3 | 1 | 4 |
| 05 Findings and report | 4 | 1 | 2 | 1 | 3 |
| 06 Console and chat | 4 | 2 | 2 | 0 | 4 |
| 07 System-wide | 4 | 2 | 1 | 1 | 3 |
| **Total** | **29** | **10** | **12** | **7** | **22** |

**All 10 blockers and all 12 rough edges are done.** What remains is 7 polish
items: OP-5, IM-2, PL-4, TS-5, RP-4, SY-4, and the OP-0 "leave it alone" note.

*(29 rows below; OP-0 is a "keep this as it is" note, not a change.)*

## Changelog

Record what landed here so a later sitting can pick up without re-reading the diff.

### 2026-08-11 — Section 01 blockers

- **OP-1** `backend/app/dashboard.py` — `fieldwork_complete` now requires `fieldwork_started`, so
  an engagement with no tests no longer satisfies every gate vacuously. The not-started case gets
  its own summary, *"No tests have been planned yet."* Because `report_complete` and the
  `generate-report` suggested action both read that flag, the empty dashboard also stops offering
  to write the report. Two tests in `test_dashboard.py`.
- **OP-2** `ChatTranscript.vue`, `ConsoleThread.vue` — a `needsSources` branch on the empty
  transcript offers **Import files** through the shell's existing import dialog, using the same
  copy and icon as the audit-file dashboard's onboarding card. The "What's next?" nudge is
  suppressed under the same condition. `UiAdvancedSection` gained an optional `icon`.
- **OP-3** `backend/app/engagement.py`, `NewEngagementDialog.vue` — `plan_preview` now also
  returns `phases`, the twenty capabilities grouped by their domain into five auditor-facing
  phases with a one-line summary and a step count. `outcomes` is unchanged and still renders,
  behind an "All 20 steps" disclosure. An unmapped domain falls into a visible *Further steps*
  phase rather than being folded into an unrelated one; a test asserts that phase stays empty.

### 2026-08-11 — the remaining blockers

- **SY-2** `backend/app/profiler.py`, `DataTab.vue` — the dtype separator moved from markup (where
  the template compiler dropped it, rendering `JOB_TITLEString`) into CSS, and the profiler now
  reports `estimated_size_bytes` instead of megabytes rounded to 2 dp, which made every table under
  ~5 KB read "0 MB". New `frontend/src/format.ts` owns `fileSize`. `get_profile` caches on disk by
  table content alone, so the payload rename would have kept serving the old shape forever —
  `profiler.SCHEMA_VERSION` is now part of the cache key and stale entries prune on first write.
- **CH-1** `backend/app/agent/narration.py` — `_blocker` falls through to the "needs your input"
  message when `humanize(code)` reduces to nothing, which is what produced *"… stopped: ."*;
  `context_note` no longer narrates a token count; `repair_note` no longer repeats the worker's
  validation message, which was surfacing JSON-schema text to auditors. Unit titles now read "Run
  document test" / "Run data test" rather than "Execute doctest" / "Execute datatest".
- **CH-2** `backend/app/agent/audit_execution.py` — milestone cards are titled by outcome, so
  "Fieldwork execution complete" above a body reporting 0 of 2 completed is now "Fieldwork ran —
  2 tests need you". The RCM and test-specification cards got the same treatment.
- **SY-1** new `backend/app/text.py` (`counted`, `plural_word`, `verb`) and the matching helpers in
  `frontend/src/format.ts`, then a sweep of all 216 sites. Two notes for whoever extends this:
  `text` is a common local variable name in this codebase, so import the helpers by name
  (`from .text import counted`) and never the module; and the swept strings are only regenerated
  when the artifact that holds them is regenerated (see the caveat below).
- **RP-1** `ReportTab.vue` — the sources panel names the statistics it shows instead of iterating
  every key, which is what serialised `risk_distribution` onto the screen as JSON under a
  capitalised "Rcm Rows". Risk distribution is a stacked bar, zero-valued warning figures are
  suppressed, traceability reads as finding titles with counts, and quality codes map to written
  headings instead of title-cased enum names.
- **TS-1** `DataTestList.vue`, `DocTestItemList.vue`, both tabs — the status chip moved from a flex
  sibling of the title to an eyebrow paired with the RCM reference, titles clamp to two lines, and
  the rails widened from `16rem` to `20rem`. Titles that ran to six lines now fit in two and cards
  are of uniform height.
- **PL-1** `planning/RcmGrid.vue` — `autoResize` removed from the editable cells and the summary
  columns clamped, so rows are a uniform 70 px instead of 130–190 px and eight fit where three and
  a half did. Both frozen columns get an opaque ground and an edge shadow, so the middle columns
  visibly scroll beneath them rather than stopping mid-word. Process moved under the id.

**Caveat for the demo.** Copy that is *computed on read* — phase summaries, blockers, readiness
reasons, test result headers — changed immediately for the existing `procurement` workspace. Copy
that is *persisted when an artifact is written* — milestone cards, agent chat messages, stored
`verdict_text` — still shows the wording captured at run time. Re-run the audit, or a single test,
to regenerate those.

Not caused by this work, but present on `main` @ `5d69af1` and worth fixing separately:
`test_rcm_execution.py::test_completion_uses_execution_and_outcome_gates` and
`test_rcm_central_e2e.py::test_synthetic_procurement_acceptance_from_population_to_preliminary_report`
both fail on a clean checkout.

### 2026-08-11 — the rough edges

- **OP-4** `HomeView.vue` — one primary action instead of two (the hero button is suppressed while
  the list is empty, so the empty state owns it), a sentence of positioning under the heading, and
  a labelled "Why this exists" link to `about.html` rather than an unlabelled ⓘ.
- **IM-1** `ImportDialog.vue`, `DocTestCreateDialog.vue` — PrimeVue 4's Dialog has no
  `focusOnShow`; it always moves focus, searching footer → header → default slot for `[autofocus]`
  and falling back to the header close button. The dropzone's pickers were plain `<label>`s, so
  there was nothing to find. They now carry `tabindex`, `autofocus`, and keyboard activation, and
  the document-test dialog's first Select is marked `autofocus`.
- **PL-2** `AuditFileView.vue`, `RcmGrid.vue`, `PlanningTab.vue` — the rail, the heading and the
  grid all say **RCM**, and the row count appears once, in the view toolbar.
- **PL-3 / RP-3** `MarkdownEditor.vue` gained a `placeholder` prop (Crepe's untranslated "Please
  enter…" was showing through), and both the APM and the report show a `UiEmptyState` with a
  generate action instead of a blank editor. The APM's provenance label only renders once the
  document has content.
- **TS-2** `AnalysisTab.vue`, `analysis/classification.ts` — the same state now has the same name
  everywhere: *Clear* → **No exception**, *Unusual result* → **Need review**, *Execution issue* →
  **Blocked**, *Exception* → **Exceptions**. Only genuinely procedure-specific states (rerun
  required, informational) keep names of their own.
- **TS-3** `UiTriageCounts.vue` — chips with a zero count are hidden, so half the Analysis toolbar
  stopped being dead filters. The first chip ("all") and the active filter always survive, so a
  chip never vanishes from under the click that selected it.
- **TS-4** `FrameTable.vue` — one line per cell with an ellipsis and a `title`. A long step label
  in a narrow column was wrapping to four lines and setting the height of the whole row: exception
  rows went from 73 px to a uniform 31 px, so roughly ten fit where four did.
- **RP-2** `ReportTab.vue` — all 17 quality codes the backend emits map to written headings; a
  test-time check confirmed none is missing. The `text-transform: capitalize` that was turning
  those sentences back into Title Case is gone.
- **CH-3** `ChatTranscript.vue` — the transcript column is capped at `46rem` and centred, so prose
  no longer runs to ~190 characters per line on a presentation screen.
- **CH-4** `ChatHistoryPanel.vue`, `ChatComposer.vue`, `assistant_chats.py` — icon-only new-chat
  button, rename and delete moved into a per-row overflow menu on the active chat, titles clamped
  to two lines. The composer's mode menu now describes each mode and calls the second one "Ask
  first", matching the New engagement dialog. Chats started from a shortcut take the command's own
  short label, made unique within the workspace, instead of the generated prompt — which is why
  nine chats all read "Run all 28 Document Tests and preserve the results."
- **SY-3** en-GB throughout the UI and in the report and finding prompts. Identifiers, API fields
  and the routing phrase table keep their American spellings — routing already matched both, and
  changing a phrase list would break input matching.

---

## 01 · Opening and engagement setup

### `[x]` OP-1 — An empty engagement reports that fieldwork passed · **Blocker**

**Where** `backend/app/dashboard.py`

**Problem** `fieldwork_complete = completion["status"] == "completed"` is vacuously true when
there is nothing to complete, so a workspace with zero tests renders
*"All RCM tests passed deterministic execution and outcome gates."* in the console's Progress
rail. The same flag gates the `generate-report` suggested action, so the empty dashboard also
offers *"Generate the audit report — fieldwork is ready to be summarized in an evidence-linked
report."*

**Change** Require that fieldwork actually exists before it can be complete, and give the
not-started case its own summary.

**Done when** A workspace with no tables, documents, RCM rows or tests shows the Fieldwork phase
as `not_started` with a summary that does not claim any test passed, and the Report phase is not
`complete`. Covered by a backend test.

### `[x]` OP-2 — The first-run console has no way forward · **Blocker**

**Where** `frontend/src/components/agent/ChatTranscript.vue`,
`frontend/src/components/agent/ConsoleThread.vue`

**Problem** Creating an engagement lands on the console, whose empty state suggests *"Draft the
APM"* — which cannot succeed, because there is no data. The only route to importing is a small
secondary button in the top-right utility cluster, beside the debug and about icons. The
audit-file dashboard, meanwhile, has a proper "Bring in your audit files" hero. The two surfaces
disagree about what step one is.

**Change** When the workspace has no tables and no documents, the console's empty state becomes
the import call to action and the model-derived suggestions and guided-workflow chips are
suppressed until there is something to work on.

**Done when** A fresh workspace's console offers importing as its primary action; a workspace
with sources is unchanged.

### `[x]` OP-3 — The New engagement dialog lists 20 internal pipeline stages · **Blocker**

**Where** `backend/app/engagement.py`, `frontend/src/components/NewEngagementDialog.vue`

**Problem** "Here's what I'd do" renders `plan.outcomes` verbatim: twenty numbered capability
titles including *Table relationships*, *Join utility selection*, *Materialized joins*,
*Document chunk analysis*. These are engineering stage names. The list reads as a build log and
visually dwarfs the two fields beside it, which sit above a large empty gap.

**Change** Group the outcomes into the phases the rest of the app already uses and lead with the
outcome rather than the stage. Keep the flat list available behind a disclosure.

**Done when** The dialog shows five named phases with a one-line description each; the twenty
underlying steps remain reachable; `plan.outcomes` is unchanged for existing consumers.

### `[x]` OP-4 — The first screen does not say what the product is · **Rough**

**Where** `frontend/src/views/HomeView.vue`

**Problem** A fresh install opens on "Your audit workspaces" with two competing calls to action —
**New engagement** in the hero and **Create your first engagement** in the empty state — and no
sentence explaining what the workbench does. `frontend/public/about.html` makes the whole
argument and is reachable only through an unlabelled ⓘ icon.

**Change** Suppress the hero CTA while the list is empty, add one line of positioning under the
heading, and give the About page a visible label.

**Done when** The empty home screen has exactly one primary action and one sentence of context.

### `[ ]` OP-5 — Both engagement-creation buttons read as secondary · **Polish**

**Where** `frontend/src/components/NewEngagementDialog.vue`

**Problem** Both footer buttons are `size="small"` and, until a name is typed, both render as
disabled teal, so the primary path is not visually primary. The destination line beneath reads
"Requests and bounded result previews go to openrouter" — transport vocabulary in a setup dialog.

**Change** Make "Create and add files" full size and solid, demote "Create only" to a text
button, and reword the destination line to name the provider and model plainly.

**Done when** The dialog has one visually dominant action.

---

## 02 · File upload and import

### `[-]` OP-0 — Leave the import wizard alone · **Keep**

The numbered **Add files → Upload → Review → Complete** wizard, the full-window drop overlay, and
the editable routing proposals are all well judged, and the dropzone copy is the register the
rest of the app should adopt. Recorded here so it is not "tidied" by accident.

### `[x]` IM-1 — Opening the import dialog focuses the close button · **Rough**

**Where** `frontend/src/components/ImportDialog.vue`,
`frontend/src/components/doc-tests/DocTestCreateDialog.vue`

**Problem** Step 1 has no focusable content, so PrimeVue moves focus to the header ✕ and paints a
heavy focus ring on it. The first thing the eye lands on is a highlighted *close* control.

**Change** Give the dialog a real first focus target — `autofocus` on "Choose files" — or set
`:focusOnShow="false"` and focus the dropzone.

**Done when** Opening either dialog puts focus on something that advances the task.

### `[ ]` IM-2 — The wizard step rail is inert · **Polish**

**Where** `frontend/src/components/ImportDialog.vue`

**Problem** Steps 2–4 render at full contrast from the outset with no signal that they are ahead
rather than available, and completed steps are not distinct from pending ones on the way back.

**Change** Mute pending steps, mark completed ones, and let a completed step be clicked to return.

**Done when** The rail distinguishes done, current and pending at a glance.

---

## 03 · Planning — APM and RCM

### `[x]` PL-1 — The RCM grid clips the Control column mid-word · **Blocker**

**Where** `frontend/src/components/planning/RcmGrid.vue`,
`frontend/src/components/PlanningTab.vue`

**Problem** At 1440 px the Control column is cut off with no scrollbar and no ellipsis — text
simply stops: *"…intended to govern procurement activiti"*, *"documer"*. Row heights are driven
by the tallest cell, so rows run 130–190 px and only three and a half of nineteen are visible.
The row action icons overlap the cut.

**Change** Clamp Risk and Control to two lines with a real ellipsis, give the table its own
`overflow-x: auto`, fix the row height, demote Process under the ID, and use the same rating chip
the board uses. Full text opens in the existing RCM detail dialog.

**Done when** No cell is cut mid-word at 1366 px and roughly nine rows are visible.
Mockup MU5 in the review is the specification.

### `[x]` PL-2 — The nav says "Coverage", the page says "RCM" · **Rough**

**Where** `frontend/src/views/AuditFileView.vue`, `frontend/src/components/PlanningTab.vue`

**Problem** The rail entry is **Coverage**, the page heading is **RCM**, the grid heading is
**Risk & Control Matrix**, and the row count appears twice in one header band.

**Change** Use **RCM** in the rail and the heading; drop the duplicate count.

**Done when** One name and one row count per screen.

### `[x]` PL-3 — An untouched APM is labelled "Auditor edited" · **Rough**

**Where** `frontend/src/components/PlanningTab.vue`, `frontend/src/components/MarkdownEditor.vue`

**Problem** On a new engagement the APM sub-header reads *Auditor edited* over an empty editor
showing Milkdown's untranslated default placeholder *"Please enter…"*. Three stacked headers sit
above content that does not exist.

**Change** When the document is empty, replace the editor with an empty state carrying the
**Generate planning drafts** action, suppress the provenance tag, and collapse the double
heading. Set a real editor placeholder for the non-empty-document case.

**Done when** An empty APM shows an empty state, not a blank editor claiming provenance.

### `[ ]` PL-4 — The empty RCM board is four "None" labels · **Polish**

**Where** `frontend/src/components/PlanningTab.vue`

**Problem** Before planning has run the board shows four zero-count columns and the word "None"
four times across a blank canvas. Every other audit-file section has a proper empty state.

**Change** Reuse `UiEmptyState` with **Generate the RCM** and **Add risk**.

**Done when** The empty board matches the other sections.

---

## 04 · Data tests and document tests

### `[x]` TS-1 — The test rail gives titles ~17 characters per line · **Blocker**

**Where** `frontend/src/components/data-tests/DataTestList.vue`,
`frontend/src/components/DataTestsTab.vue`,
`frontend/src/components/DocTestsTab.vue`,
`frontend/src/components/doc-tests/DocTestItemList.vue`

**Problem** The rail is `16rem`. After list and card padding 219 px remain, and `.row-head` lays
the title out as a flex sibling of the status chip, which takes ~100 px — leaving the title about
113 px, roughly 17 characters. Titles wrap to six lines. The description below is clamped to two
lines; the title is not clamped at all, so the longest titles produce the tallest cards. The
detail pane beside it is often two-thirds empty.

**Change** Move the status chip onto its own line above the title as an eyebrow paired with the
RCM reference, clamp the title to two lines, and widen the rail to `20rem`. Same change in both
tabs.

**Done when** No rail card exceeds four lines total and cards are of uniform height.
Mockup MU1 in the review is the specification.

### `[x]` TS-2 — Two parallel test systems with different status vocabularies · **Rough**

**Where** `frontend/src/components/DataTestsTab.vue`, `frontend/src/components/AnalysisTab.vue`

**Problem** Data tests filter by *Exceptions · Need review · Blocked · No exception · Not run*.
Analysis procedures — a near-identical master–detail surface one click away — filter by
*Exception · Unusual result · Execution issues · Rerun required · Clear · Informational · Not
run*. Eight chips versus six for the same conceptual thing, with "Clear" and "No exception"
meaning the same state.

**Change** Converge on one vocabulary. Where Analysis genuinely has extra states, express them as
a secondary attribute rather than a peer chip.

**Done when** The same state has the same name on both surfaces.

### `[x]` TS-3 — Zero-count filter chips are always present · **Rough**

**Where** `frontend/src/components/DataTestsTab.vue`, `frontend/src/components/DocTestsTab.vue`,
`frontend/src/components/AnalysisTab.vue`

**Problem** Every chip renders whether or not it matches anything. On the Analysis tab that is
four dead chips out of eight.

**Change** Hide zero-count chips, keeping "All" and the active filter always visible.

**Done when** No chip reads `0` unless it is the active filter.

### `[x]` TS-4 — Exception tables waste vertical space · **Rough**

**Where** `frontend/src/components/data-tests/ExceptionExplorer.vue`

**Problem** The 43-row exception table renders at roughly 73 px per row, so four rows fill the
viewport. This is the evidence an auditor most wants to scan and it is the least dense thing on
the screen.

**Change** Apply the compact DataTable density used elsewhere (`--aw-row-height: 2.5rem`).

**Done when** At least ten exception rows are visible at 1366 px.

### `[ ]` TS-5 — The vouching grid is never reached by the demo data · **Polish**

**Where** `Workspaces/procurement`, `frontend/src/components/doc-tests/CycleVouchGrid.vue`

**Problem** All 27 document tests in the seeded engagement are `kind: "qa"`, so the cycle vouching
grid — a distinctive feature with its own tests and state module — cannot be shown.

**Change** Seed two or three vouching tests against the voucher documents already imported.

**Done when** The document-tests surface has at least one test that renders the grid.

---

## 05 · Findings and report

### `[x]` RP-1 — Raw JSON is rendered as a statistic · **Blocker**

**Where** `frontend/src/components/ReportTab.vue`

**Problem** The "Live report sources" panel iterates every key of `context.statistics` and prints
the value. One value is an object, so the panel displays
`{ "Critical": 1, "High": 15, "Medium": 3, "Low": 0 }` under the label "Risk Distribution". The
same loop applies `text-transform: capitalize` to snake_case keys, producing *"Rcm Rows"*, and
the header beside it reads *"1 warnings"*. Traceability lines print internal reference strings.

**Change** Replace the generic loop with an explicit list of the statistics worth showing, each
with a written label; render the risk distribution as a stacked bar; pluralise properly; show
traceability as named links.

**Done when** No serialised object appears on the panel and every label is written English.
Mockup MU4 in the review is the specification.

### `[x]` RP-2 — Quality issue titles are enum names · **Rough**

**Where** `frontend/src/components/ReportTab.vue`

**Problem** `issue.code.replaceAll('_',' ')` plus `capitalize` yields **Stale Evidence**,
**Broken Test Ref**, **Unsupported Finding**, **Finding Draft** — identifiers wearing title case,
and "Finding Draft" does not describe the problem.

**Change** Map codes to written headings.

**Done when** Every quality issue heading is a sentence about the problem.

### `[x]` RP-3 — An empty report opens a blank editor · **Rough**

**Where** `frontend/src/components/ReportTab.vue`

**Problem** Before generation the section shows a full-height empty editor with Milkdown's stock
*"Please enter…"* and a redundant "Report editor / Save" band under a heading that already says
"Draft audit report".

**Change** Empty state carrying **Generate report** and a line about what it draws on; collapse
the duplicate header band. Shares the placeholder fix with PL-3.

**Done when** An ungenerated report shows an empty state.

### `[ ]` RP-4 — Findings list wraps titles unboundedly · **Polish**

**Where** `frontend/src/components/FindingsTab.vue`

**Problem** Long finding titles render as six lines, pushing the sixth finding below the fold.
Every card also carries the word "Agent" as a footer, which is noise when all thirteen are
agent-drafted.

**Change** Clamp to three lines; show the source only when it differs from the majority.

**Done when** Every finding card is at most five lines.

---

## 06 · Console and chat

### `[x]` CH-1 — Broken and internal strings surface in the transcript · **Blocker**

**Where** `backend/app/agent/narration.py`

**Problem** Three leaks, all visible in the default `Procurement` chat:

1. *"Execute doctest — Vendor master periodic review stopped: ."* — the unmapped-code branch
   builds `"{title} stopped: {humanize(code)}."` and `humanize` returns an empty string for codes
   that reduce to nothing, leaving a sentence ending in a bare full stop.
2. *"…finding template for Eligible finding drafts (~2k tokens)"* — a token budget narrated to an
   auditor.
3. *"That draft didn't pass the quality check — the response must be a JSON object with a
   `finding` object — so I'm redoing it."* — a schema failure narrated to an auditor.

**Change** Fall through to the "needs your input" message when `humanize(code)` is empty; drop
token counts from user-facing narration; reword the schema-retry line.

**Done when** No transcript line ends in a bare punctuation mark or names a token count, a JSON
schema, or the word "doctest".

### `[x]` CH-2 — A stage headed "complete" reports zero completed and two failed · **Blocker**

**Where** `backend/app/agent/narration.py`, `frontend/src/components/agent/AgentMilestoneCard.vue`

**Problem** The milestone card reads *"Fieldwork execution complete — Completed 0 of 2 scheduled
fieldwork unit(s). 2 unit(s) failed or need auditor attention."* under a check icon. Headline,
icon and body contradict each other: "complete" means the stage finished executing, not that the
work succeeded.

**Change** Title the card by outcome rather than stage lifecycle. Reserve the check icon for
stages where nothing needs attention.

**Done when** No milestone card claims completion while its own body reports failures.

### `[x]` CH-3 — Transcript lines run 1,360 px wide at 1920 · **Rough**

**Where** `frontend/src/components/agent/ChatTranscript.vue`

**Problem** The console's middle column has no measure cap, so on a presentation screen prose
runs to roughly 190 characters per line against a comfortable 65–75.

**Change** Cap the transcript at `max-width: 46rem` and centre it in its column; leave milestone
cards, tables and artifacts full-width.

**Done when** Prose stays under about 90 characters per line at 1920 px.
Mockup MU6 in the review is the specification.

### `[x]` CH-4 — Chat rail and composer details · **Rough**

**Where** `frontend/src/components/agent/ChatHistoryPanel.vue`,
`frontend/src/components/agent/ChatComposer.vue`,
`frontend/src/composables/useAssistantChat.ts`

**Problem** Four things in the same 40 px of chrome: the **New chat** button wraps to two lines in
a 16.5 rem rail; **Rename** and the red delete icon sit in the same button group as it, so
creating and destroying are adjacent and equally weighted; the mode menu offers bare **Auto** /
**Ask** with no explanation and calls the second option "Ask" where the New engagement dialog
calls the identical option "Ask first"; and nine of eleven saved chats are named after the
command that started them, so the list repeats itself.

**Change** Icon-only new-chat button, rename and delete into a per-row overflow menu, one-line
descriptions on the mode menu with labels aligned to the dialog, and chat titles derived from the
first outcome rather than the prompt.

**Done when** The rail has one create action, per-row destructive actions, and no repeated titles.

---

## 07 · System-wide

### `[x]` SY-1 — 216 machine plurals · **Blocker**

**Where** `backend/app/` (170), `frontend/src/` (46)

**Problem** The `N item(s)` construction appears 216 times, concentrated where a demo dwells: the
progress rail ("0 RCM row(s) and 0 test(s)"), the transcript ("Recorded 99 exception(s) across 13
exception observation(s)"), and every test detail header ("43 exception row(s) across 2 step(s)").
Individually trivial; collectively the strongest signal that a program wrote the copy.

**Change** One `plural(n, singular, plural)` helper on each side, then a mechanical sweep. Can be
done file by file — this item is the natural candidate for splitting across several sittings.

**Done when** `grep -ro "(s)" backend/app frontend/src` returns nothing user-facing.

### `[x]` SY-2 — Column name and type render with no space between them · **Blocker**

**Where** `frontend/src/components/DataTab.vue`

**Problem** The table profile — the first screen after import — renders `JOB_TITLEString` and
`MAX_APPROVAL_AMOUNTInt64`. The separating space is a leading space inside a span and is dropped
at compile time. Two lines below, "In-memory size" reads **0 MB** for every small table because
the value is rounded to whole megabytes.

**Change** Put the gap in CSS rather than in markup; format sizes under 1 MB in KB.

**Done when** Name and type are visibly separated and no table reports 0 MB.

### `[x]` SY-3 — Mixed British and American spelling · **Rough**

**Where** `frontend/src/components/DocumentsTab.vue`, `frontend/src/components/AnalysisTab.vue`,
`backend/app/templates/`, prompt templates

**Problem** **Analyse with assistant** sits in the Workbench rail; **Analyze all** sits in the
Documents header one section above it. "Summarised" and "summarized" both appear in generated
report prose.

**Change** Pick one — the About page and most of the app lean en-GB — and add it to the prompt
templates so generated prose matches.

**Done when** One spelling of *analyse* and *summarise* across UI copy and templates.

### `[ ]` SY-4 — Dark mode is configured but not implemented · **Polish**

**Where** `frontend/src/main.ts`, `frontend/src/style.css`

**Problem** The PrimeVue preset declares `darkModeSelector: '.app-dark'`, but nothing applies that
class and `style.css` defines no dark token set. `about.html` is entirely dark.

**Change** Either add the dark token block and a toggle, or remove the dead option.

**Done when** Dark mode either works or is not advertised in the config.

---

## 10 · Recommended running order

Front-loads what an audience actually sees.

1. **Section 01** — OP-1, OP-2, OP-3. The first five minutes of every demo, and currently the
   weakest part.
2. **SY-1 and the copy items** — CH-1, RP-2, plus the string table in the review. Low risk, large
   perceived-quality jump.
3. **The cramped layouts** — TS-1, PL-1, TS-4. Mockups MU1 and MU5 are the specifications.
4. **The two rendering bugs** — RP-1, SY-2. One-line fixes with outsized visual impact.
5. **CH-2, CH-3, OP-4, OP-5** — milestone honesty, chat measure, and the home screen.
6. **If time remains** — TS-5 so the vouching grid can be shown, and decide whether to open the
   demo on `about.html`.
