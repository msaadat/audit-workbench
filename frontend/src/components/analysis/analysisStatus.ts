import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusChip, StatusFilterGroup, StatusLane, StatusModel, Tone,
} from '../ui/statusLanes'
import type { AnalysisSummaryClassification, SavedAnalysis } from '../../types'
import { plural } from '../../format'

/**
 * Where the saved procedures stand, above the list of them.
 *
 * Three questions, in the order the work answers them: how much has executed,
 * how much of what executed still stands, and what anyone has done about what
 * it found. The middle one is the reason this module exists. The page used to
 * ask only the first, and answered the third nowhere at all:
 *
 *  - **Run** and **Current** were one number. A procedure whose definition had
 *    been edited since it ran classified as `stale`, which displaced the
 *    verdict, so an engagement whose twenty-three procedures were all stale
 *    reported "23 rerun required, 0 exceptions" over sixteen recorded
 *    exceptions. `classification` now answers only what a result concluded and
 *    `state` answers only whether it still stands.
 *  - **Answered** was invisible. `promotion` — carried into a data test against
 *    an RCM row, or declined with a reason — has been durable on every record
 *    since the promotion capability landed and never left the backend, so
 *    nothing could say that sixteen procedures held exceptions and not one had
 *    been answered for. A procedure computed outside the audit graph supports
 *    no finding until it becomes a test, which is what makes this a lane rather
 *    than a detail.
 *
 * Read entirely off the `SavedAnalysis` records the tab already holds.
 */

export type AnalysisFilter =
  | 'exception' | 'unusual' | 'errors' | 'clear' | 'informational' | 'not_run'
  | 'stale' | 'current'
  | 'unanswered' | 'promoted' | 'declined'

/** The dot, the chip and the label all take their tone from here. */
const CLASSIFICATION_TONE: Record<AnalysisSummaryClassification, Tone> = {
  exception: 'bad',
  execution_error: 'bad',
  unusual: 'warn',
  clear: 'ok',
  informational: 'neutral',
  not_run: 'neutral',
}

export function classificationTone(value: AnalysisSummaryClassification): Tone {
  return CLASSIFICATION_TONE[value] ?? 'neutral'
}

/** A procedure holds exceptions, so somebody owes it an answer. */
export function holdsExceptions(analysis: SavedAnalysis): boolean {
  return (analysis.last_result?.exception_count ?? 0) > 0
}

interface Counts {
  total: number
  run: number
  notRun: string[]
  stale: string[]
  current: number
  exception: number
  unusual: number
  errors: number
  clear: number
  informational: number
  flagging: number
  promoted: number
  declined: number
  unanswered: string[]
}

function tally(analyses: SavedAnalysis[]): Counts {
  const counts: Counts = {
    total: analyses.length,
    run: 0,
    notRun: [],
    stale: [],
    current: 0,
    exception: 0,
    unusual: 0,
    errors: 0,
    clear: 0,
    informational: 0,
    flagging: 0,
    promoted: 0,
    declined: 0,
    unanswered: [],
  }
  for (const analysis of analyses) {
    if (analysis.state === 'not_run') counts.notRun.push(analysis.id)
    else {
      counts.run += 1
      if (analysis.state === 'stale') counts.stale.push(analysis.id)
      else counts.current += 1
    }
    if (analysis.classification === 'exception') counts.exception += 1
    else if (analysis.classification === 'unusual') counts.unusual += 1
    else if (analysis.classification === 'execution_error') counts.errors += 1
    else if (analysis.classification === 'clear') counts.clear += 1
    else if (analysis.classification === 'informational') counts.informational += 1
    // Only a procedure that flagged rows can be promoted or declined, which is
    // the same population `analysis_promotion.candidates` draws from.
    if (!holdsExceptions(analysis)) continue
    counts.flagging += 1
    const state = analysis.promotion?.state
    if (state === 'promoted') counts.promoted += 1
    else if (state === 'declined') counts.declined += 1
    else counts.unanswered.push(analysis.id)
  }
  return counts
}

/** Whether one procedure is in the subset a filter key names. */
export function matchesFilter(analysis: SavedAnalysis, filter: AnalysisFilter): boolean {
  switch (filter) {
    case 'exception': return analysis.classification === 'exception'
    case 'unusual': return analysis.classification === 'unusual'
    case 'errors': return analysis.classification === 'execution_error'
    case 'clear': return analysis.classification === 'clear'
    case 'informational': return analysis.classification === 'informational'
    case 'not_run': return analysis.classification === 'not_run'
    case 'stale': return analysis.state === 'stale'
    case 'current': return analysis.state === 'current'
    case 'promoted': return analysis.promotion?.state === 'promoted'
    case 'declined': return analysis.promotion?.state === 'declined'
    case 'unanswered': return holdsExceptions(analysis) && !analysis.promotion
    default: return true
  }
}

/** Every narrowing in force, all of them, because they compose across axes. */
export function visibleAnalyses(
  analyses: SavedAnalysis[], filters: readonly string[],
): SavedAnalysis[] {
  if (!filters.length) return analyses
  return analyses.filter(
    analysis => filters.every(key => matchesFilter(analysis, key as AnalysisFilter)),
  )
}

function executionLane(counts: Counts): StatusLane {
  const outstanding = [...counts.notRun, ...counts.stale]
  if (!counts.total) {
    return {
      key: 'execution', label: 'Run', state: 'idle',
      value: '0', caption: 'procedures saved',
      segments: [], chips: [], actions: [],
      rest: 'Nothing has been written yet',
    }
  }
  return {
    key: 'execution', label: 'Run',
    state: counts.notRun.length ? 'gap' : 'done',
    value: String(counts.run),
    total: String(counts.total),
    caption: `of ${plural(counts.total, 'procedure')} executed`,
    segments: [{ tone: 'ok', portion: portion(counts.run, counts.total) }],
    chips: counts.notRun.length
      ? [{ key: 'not_run', label: `${counts.notRun.length} never run`, tone: 'neutral' }]
      : [],
    // One action for both gaps: a procedure that has never run and one whose
    // result no longer stands are the same thing to an auditor — neither
    // concludes anything about the data as it is now.
    actions: outstanding.length
      ? [{
          key: 'run_outstanding',
          label: `Run ${outstanding.length} outstanding`,
          tone: 'primary',
          ids: outstanding,
        }]
      : [],
    rest: 'Every procedure has executed',
  }
}

/**
 * Whether what executed still describes the data and the definitions as they
 * are now. Its own lane, because it is orthogonal to what each one found.
 */
function freshnessLane(counts: Counts): StatusLane {
  if (!counts.run) {
    return {
      key: 'freshness', label: 'Current', state: 'idle',
      value: '0', caption: 'results still standing',
      segments: [], chips: [], actions: [],
      rest: 'Freshness follows execution',
    }
  }
  const chips: StatusChip[] = []
  if (counts.stale.length) {
    chips.push({ key: 'stale', label: `${counts.stale.length} need a rerun`, tone: 'warn' })
  }
  return {
    key: 'freshness', label: 'Current',
    state: counts.stale.length ? 'gap' : 'done',
    value: String(counts.current),
    total: String(counts.total),
    caption: `of ${plural(counts.total, 'procedure')} still current`,
    segments: [{ tone: 'ok', portion: portion(counts.current, counts.total) }],
    chips,
    actions: [],
    rest: 'Every result describes the current data',
  }
}

/**
 * What has been done about what the procedures found.
 *
 * The lane counts only procedures that flagged rows: a clean procedure asks
 * nothing of anybody, and counting it as "answered" would report coverage that
 * was never decided.
 */
function dispositionLane(counts: Counts): StatusLane {
  if (!counts.flagging) {
    return {
      key: 'disposition', label: 'Answered', state: 'idle',
      value: '0', caption: 'procedures holding exceptions',
      segments: [], chips: [], actions: [],
      rest: counts.run ? 'No procedure flagged anything' : 'Answers follow execution',
    }
  }
  const answered = counts.promoted + counts.declined
  const gap = counts.unanswered.length
  const chips: StatusChip[] = []
  if (gap) chips.push({ key: 'unanswered', label: `${gap} not answered`, tone: 'bad' })
  if (counts.promoted) {
    chips.push({ key: 'promoted', label: `${counts.promoted} carried into a test`, tone: 'ok' })
  }
  if (counts.declined) {
    chips.push({ key: 'declined', label: `${counts.declined} declined`, tone: 'neutral' })
  }
  return {
    key: 'disposition', label: 'Answered',
    state: gap ? 'alarm' : 'done',
    value: String(answered),
    total: String(counts.flagging),
    caption: `of ${plural(counts.flagging, 'procedure')} holding exceptions answered`,
    segments: [
      { tone: 'ok', portion: portion(counts.promoted, counts.flagging) },
      { tone: 'neutral', portion: portion(counts.declined, counts.flagging) },
    ],
    chips,
    actions: gap
      ? [{
          key: 'answer_procedures',
          label: `Answer ${plural(gap, 'procedure')}`,
          tone: 'warn',
          ids: counts.unanswered,
          needsAgent: true,
        }]
      : [],
    rest: 'Every exception has been answered for',
  }
}

function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'outcome',
      label: 'What it concluded',
      options: [
        { key: 'exception', label: 'Exceptions', value: counts.exception, tone: 'bad' },
        { key: 'unusual', label: 'Need review', value: counts.unusual, tone: 'warn' },
        { key: 'errors', label: 'Blocked', value: counts.errors, tone: 'bad' },
        { key: 'clear', label: 'No exception', value: counts.clear, tone: 'ok' },
        { key: 'informational', label: 'Informational', value: counts.informational, tone: 'neutral' },
        { key: 'not_run', label: 'Not run', value: counts.notRun.length, tone: 'neutral' },
      ],
    },
    {
      key: 'freshness',
      label: 'Whether it still stands',
      options: [
        { key: 'stale', label: 'Rerun required', value: counts.stale.length, tone: 'warn' },
        { key: 'current', label: 'Current', value: counts.current, tone: 'ok' },
      ],
    },
    {
      key: 'disposition',
      label: 'What was done about it',
      options: [
        { key: 'unanswered', label: 'Not answered', value: counts.unanswered.length, tone: 'bad' },
        { key: 'promoted', label: 'Carried into a test', value: counts.promoted, tone: 'ok' },
        { key: 'declined', label: 'Declined', value: counts.declined, tone: 'neutral' },
      ],
    },
  ]
}

export function analysisStatus(analyses: SavedAnalysis[]): StatusModel {
  const counts = tally(analyses)
  return {
    lanes: [executionLane(counts), freshnessLane(counts), dispositionLane(counts)],
    disclosures: [],
    filters: filtersFor(counts),
  }
}

/**
 * What the procedure found, in the fewest words that stay true.
 *
 * The row and the verdict bar both lead with this. `verdict_text` is the
 * server's full sentence — "3 rows of 52 breach INVOICE_AMOUNT <=
 * PO_TOTAL_AMOUNT" — which belongs in the detail pane, not in a 300 px row
 * beside a 38-character joined frame name.
 *
 * "No exceptions" and "no exceptions over 49 of 52 rows compared" are
 * different statements, so a procedure that could not evaluate its whole
 * population says so rather than implying it covered everything.
 */
export function foundSummary(analysis: SavedAnalysis): string {
  const result = analysis.last_result
  if (!result) return 'not run'
  if (analysis.classification === 'execution_error') return 'could not run'
  const flagged = result.exception_count ?? 0
  const population = result.population ?? null
  if (flagged > 0) {
    const verb = analysis.classification === 'exception' ? 'failed' : 'flagged'
    return population ? `${flagged} of ${population} ${verb}` : `${flagged} ${verb}`
  }
  if (analysis.classification === 'informational') {
    return `${plural(result.row_count ?? 0, 'result row')}`
  }
  const tested = result.tested ?? null
  return tested && population && tested < population
    ? `no exceptions · ${tested} of ${population} compared`
    : 'no exceptions'
}

/**
 * The chips worth a permanent row, in reading order: what failed, what wants a
 * look, what could not run, what no longer stands, what nobody has answered
 * for, and what came back clean. Everything else stays one click behind the
 * pressed chip.
 */
export const ANALYSIS_CHIPS: ReviewChip[] = [
  { filter: 'exception', tone: 'bad', label: 'Exceptions' },
  { filter: 'errors', tone: 'bad', label: 'Blocked' },
  { filter: 'unanswered', tone: 'bad', label: 'Not answered' },
  { filter: 'unusual', tone: 'warn', label: 'Need review' },
  { filter: 'stale', tone: 'warn', label: 'Rerun required' },
  { filter: 'clear', tone: 'ok', label: 'No exception' },
]
