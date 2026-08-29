import { plural, pluralWord, verb } from '../../format'
import type { EngagementPhase, EngagementSection, NavigationTarget } from '../../types'
import { portion } from '../ui/statusLanes'
import type { Tone } from '../ui/statusLanes'

/**
 * Where the engagement stands, for the console rail.
 *
 * The page bars answer one surface's question across three lanes. This answers
 * the whole file's question down one 17rem column, so it is the same vocabulary
 * — a population, an arc across it, an action scoped to the gap — in a vertical
 * arrangement rather than `UiStatusLanes`' grid.
 *
 * Two rules keep it honest. It never recomputes a state the backend owns: the
 * ticks are `phase.state` exactly as `_engagement_state` set them, and a
 * disclosure qualifies a tick rather than withholding it. And it never counts a
 * population twice — every figure below is a count the backend already made and
 * put on the wire.
 */

export type PhaseState = EngagementPhase['state']

/**
 * How much of a row is drawn. Complete work collapses to its totals and opens
 * on click; the phase in flight is open; anything past it is a label waiting
 * its turn, because a card promising work that cannot start yet reads as a task.
 */
export type RowDisplay = 'collapsed' | 'open' | 'pending'

/** What a rail action asks for. `ConsoleView` owns how each is carried out. */
export type EngagementActionKey = 'import' | 'run_data_tests' | 'rerun_stale'

export interface PhaseChip {
  key: string
  label: string
  /** The fraction the chip leads with, where it has one: `36/39`. */
  detail: string
  tone: Tone
  target: NavigationTarget
}

export interface PhaseAction {
  key: EngagementActionKey
  label: string
  tone: 'primary' | 'ghost'
  /** The tests the action is scoped to, where it is scoped to any. */
  ids?: string[]
}

export interface PhaseRow {
  id: EngagementPhase['id']
  label: string
  state: PhaseState
  display: RowDisplay
  /** `47 / 60`, or empty where the phase has no countable population. */
  figure: string
  /** Completes the figure's sentence, or stands alone where there is none. */
  caption: string
  /** The totals a collapsed row still carries: `27 rows · 60 tests`. */
  tail: string
  /** Meter fill. Portions are percentages of the whole and need not total 100. */
  segments: Array<{ tone: Tone; portion: number }>
  chips: PhaseChip[]
  issues: string[]
  actions: PhaseAction[]
  target: NavigationTarget
}

/**
 * What qualifies the ticks rather than moving them. Each names a count the
 * phase gates deliberately do not cover, so a complete file cannot read as
 * finished when work of this kind is still owed.
 */
export interface EngagementDisclosure {
  key: string
  message: string
  target: NavigationTarget
}

export interface EngagementStatus {
  /** `Fieldwork · 2 of 3`, `Ready for review`, or `Not started`. */
  position: string
  /** One segment per phase, in order, for the arc under the header. */
  arc: PhaseState[]
  rows: PhaseRow[]
  disclosures: EngagementDisclosure[]
}

function count(phase: EngagementPhase, key: string): number {
  return phase.counts?.[key] ?? 0
}

function chipTone(state: PhaseState): Tone {
  if (state === 'complete') return 'ok'
  if (state === 'attention') return 'bad'
  if (state === 'in_progress') return 'warn'
  return 'neutral'
}

/** `27 rows · 60 tests` — the totals worth keeping on a one-line row. */
function joinTail(parts: string[]): string {
  return parts.filter(Boolean).join(' · ')
}

function planningRow(phase: EngagementPhase, display: RowDisplay): PhaseRow {
  const rows = count(phase, 'rcm_rows')
  const tests = count(phase, 'tests')
  return {
    id: phase.id,
    label: phase.label,
    state: phase.state,
    display,
    // Planning is a checklist, not a population: EDA, APM and RCM are each
    // done or not. A fraction over three would be a worse answer than the
    // three chips that say which one is outstanding.
    figure: '',
    caption: rows
      ? ''
      : 'No risks or controls are recorded yet.',
    tail: joinTail([
      rows ? plural(rows, 'row') : '',
      tests ? plural(tests, 'test') : '',
    ]),
    segments: [],
    chips: phase.sub.map(sub => ({
      key: sub.id,
      label: sub.label,
      detail: '',
      tone: chipTone(sub.state),
      target: sub.target,
    })),
    issues: phase.issues,
    actions: rows || tests ? [] : [{ key: 'import', label: 'Import a folder', tone: 'primary' }],
    target: phase.target,
  }
}

function fieldworkRow(
  phase: EngagementPhase,
  sections: Record<string, EngagementSection>,
  display: RowDisplay,
): PhaseRow {
  const linked = count(phase, 'tests_linked')
  const concluded = count(phase, 'tests_concluded')
  const exceptions = count(phase, 'exception_observations')

  const chips: PhaseChip[] = []
  for (const key of ['data-tests', 'doc-tests'] as const) {
    const section = sections[key]
    if (!section?.counts?.total) continue
    chips.push({
      key,
      label: key === 'data-tests' ? 'Data' : 'Docs',
      detail: `${section.counts.concluded ?? 0}/${section.counts.total}`,
      tone: chipTone(section.state),
      target: { tab: key, query: {} },
    })
  }
  if (exceptions) {
    chips.push({
      key: 'exceptions',
      label: pluralWord(exceptions, 'exception'),
      detail: String(exceptions),
      tone: 'neutral',
      target: { tab: 'findings', query: {} },
    })
  }

  // Both populations are deterministic work the rail can run itself. Blocked or
  // review-bound tests are deliberately not here: running them again does not
  // move them, which is the same rule the Data tests lane applies.
  const data = sections['data-tests']
  const unrun = data?.unrun_test_ids ?? []
  const stale = data?.stale_test_ids ?? []
  const actions: PhaseAction[] = []
  if (unrun.length) {
    actions.push({
      key: 'run_data_tests',
      label: `Run ${plural(unrun.length, 'test')}`,
      tone: 'primary',
      ids: unrun,
    })
  }
  if (stale.length) {
    actions.push({
      key: 'rerun_stale',
      label: `Re-run ${plural(stale.length, 'stale test')}`,
      tone: unrun.length ? 'ghost' : 'primary',
      ids: stale,
    })
  }

  return {
    id: phase.id,
    label: phase.label,
    state: phase.state,
    display,
    figure: linked ? `${concluded} / ${linked}` : '',
    // The RCM bar's denominator, so the rail and the page it links to never
    // disagree about how much fieldwork is left.
    caption: linked ? 'tests concluded' : 'No tests have been planned yet.',
    tail: linked ? `${concluded} / ${linked} concluded` : '',
    segments: [
      { tone: 'ok', portion: portion(concluded, linked) },
      { tone: 'warn', portion: portion(linked - concluded, linked) },
    ],
    chips,
    issues: phase.issues,
    actions,
    target: phase.target,
  }
}

function reportRow(phase: EngagementPhase, display: RowDisplay): PhaseRow {
  const findings = count(phase, 'findings')
  const errors = count(phase, 'quality_errors')
  return {
    id: phase.id,
    label: phase.label,
    state: phase.state,
    display,
    figure: '',
    caption: phase.state === 'not_started'
      ? 'Not drafted. Opens when the fieldwork gates pass.'
      : '',
    tail: joinTail([
      findings ? plural(findings, 'finding') : '',
      errors ? plural(errors, 'error') : findings ? '0 errors' : '',
    ]),
    segments: [],
    chips: [],
    issues: phase.issues,
    actions: [],
    target: phase.target,
  }
}

/**
 * The phase the engagement is actually sitting on: the first that is not
 * complete. Everything before it rests, everything after it waits.
 */
function currentIndex(phases: EngagementPhase[]): number {
  const index = phases.findIndex(phase => phase.state !== 'complete')
  return index === -1 ? phases.length : index
}

function disclosuresFor(phases: EngagementPhase[]): EngagementDisclosure[] {
  const items: EngagementDisclosure[] = []
  const fieldwork = phases.find(phase => phase.id === 'fieldwork')
  const report = phases.find(phase => phase.id === 'report')

  if (report) {
    const owed = count(report, 'findings_awaiting_followup')
    const findings = count(report, 'findings')
    if (owed) {
      items.push({
        key: 'followup',
        message: `${owed} of ${plural(findings, 'finding')} ${
          verb(owed, 'has', 'have')} no root cause or management response.`,
        target: { tab: 'findings', query: {} },
      })
    }
  }
  if (fieldwork) {
    const unread = count(fieldwork, 'unreviewed_agent_conclusions')
    const linked = count(fieldwork, 'tests_linked')
    if (unread) {
      items.push({
        key: 'agent',
        message: `${unread} of ${linked} ${pluralWord(linked, 'conclusion')} ${
          verb(unread, 'was', 'were')} set by the assistant and never read.`,
        target: { tab: 'rcm', query: {} },
      })
    }
  }
  return items
}

export function engagementStatus(
  phases: EngagementPhase[],
  sections: Record<string, EngagementSection> = {},
): EngagementStatus {
  const current = currentIndex(phases)
  const rows = phases.map((phase, index) => {
    const display: RowDisplay = index < current
      ? 'collapsed'
      : index === current ? 'open' : 'pending'
    if (phase.id === 'planning') return planningRow(phase, display)
    if (phase.id === 'fieldwork') return fieldworkRow(phase, sections, display)
    return reportRow(phase, display)
  })

  const started = phases.some(phase => phase.state !== 'not_started')
  const position = !phases.length
    ? ''
    : current === phases.length
      ? 'Ready for review'
      : started
        ? `${phases[current].label} · ${current + 1} of ${phases.length}`
        : 'Not started'

  return {
    position,
    arc: phases.map(phase => phase.state),
    rows,
    disclosures: disclosuresFor(phases),
  }
}
