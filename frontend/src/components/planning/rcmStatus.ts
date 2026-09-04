import { plural, pluralWord } from '../../format'
import type { FindingRollups, RcmCompletion, RcmRow, TestRollup } from '../../types'
import { portion } from '../ui/statusLanes'
import type {
  LaneState, ReviewChip, StatusAction, StatusChip, StatusDisclosure,
  StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'

/**
 * Where the engagement stands, as three answers rather than twenty-seven rows.
 *
 * The RCM grid says what is stored against each row. This says what is true of
 * the matrix as a whole — has the work run, does it support a conclusion, has
 * the conclusion been written up — and what is still outstanding in each case.
 *
 * Everything here is derived from payloads the page already holds: the planning
 * roll-ups, the finding index, and `GET /rcm/completion`. Nothing is fetched or
 * computed twice, and no lane invents a state the backend does not record.
 */

/** A conclusion that closes the row, as the backend defines it. */
const CONCLUDED = new Set(['effective', 'partially_effective', 'ineffective', 'not_applicable'])
/** Conclusions that put the control in the report rather than closing it. */
const ADVERSE = new Set(['ineffective', 'partially_effective'])

export type LaneKey = 'execution' | 'conclusion' | 'findings'

/**
 * Every filter the bar can put on the grid. Each is a predicate over one row,
 * so a filtered grid is always a subset of the same array the bar counted.
 */
export type RcmFilter =
  | 'no_test' | 'not_run' | 'blocked' | 'awaiting_review' | 'passed' | 'with_exceptions'
  | 'no_control'
  | 'effective' | 'partially_effective' | 'ineffective' | 'not_applicable' | 'no_conclusion'
  | 'missing_finding' | 'has_finding'
  | 'agent_concluded' | 'evidence_limit' | 'unreviewed_row'

/** What a lane's action asks for. `PlanningTab` owns how each is carried out. */
export type RcmActionKey =
  | 'generate_tests' | 'run_data_tests' | 'run_document_tests'
  | 'refresh_rollup' | 'draft_findings' | 'mark_reviewed'

interface Counts {
  rows: number
  /** Risks nothing is written against. A gap in the matrix, not in the work. */
  rowsWithoutControl: number
  rowsWithoutTests: string[]
  severeWithoutTests: number
  tests: number
  completed: number
  awaitingReview: number
  blocked: number
  pendingData: string[]
  pendingDocument: string[]
  passedRows: number
  exceptionRows: number
  concluded: number
  effective: number
  partiallyEffective: number
  ineffective: number
  notApplicable: number
  noConclusion: number
  adverse: number
  adverseCovered: number
  findings: number
  criticalFindings: number
  highFindings: number
  ceilingRows: number
  agentRows: number
  agentTests: number
  reviewedRows: number
  unreviewedRows: string[]
}

function testRollups(row: RcmRow): TestRollup[] {
  return row.execution_rollup.test_rollups ?? []
}

export function testCount(row: RcmRow): number {
  return row.execution_rollup.tests ?? row.test_refs.length
}

/** Executed, whatever the outcome. Mirrors the backend's `completed` tally. */
function hasRun(item: TestRollup): boolean {
  return String(item.status ?? '').startsWith('completed')
}

/**
 * A test the Run action can actually move. Blocked work needs evidence, and
 * work awaiting review has already run — neither is served by running it again.
 */
function isPending(item: TestRollup): boolean {
  return !hasRun(item) && item.status !== 'blocked' && item.status !== 'review_required'
}

/**
 * A risk with something written against it.
 *
 * The matrix can hold a risk nobody has named a control for, and the old grid
 * showed that as an empty cell among eleven other cells — indistinguishable
 * from a cell that had simply scrolled out of view. It is a gap in the matrix
 * itself rather than in the work done against it, which is why it is a filter
 * here and not a lane.
 */
function hasControl(row: RcmRow): boolean {
  return Boolean(String(row.control ?? '').trim())
}

function conclusionOf(row: RcmRow): string {
  return String(row.execution_rollup.control_conclusion ?? '') || 'no_conclusion'
}

function findingsFor(rollups: FindingRollups | undefined, id: string) {
  return rollups?.by_rcm[id] ?? []
}

function tally(
  rows: RcmRow[],
  findingRollups?: FindingRollups,
  completion?: RcmCompletion | null,
): Counts {
  const agentRows = new Set(
    (completion?.unreviewed_agent_conclusions ?? []).map(item => item.rcm_id),
  )
  // Findings are counted once even where one is written against several rows,
  // so the lane total agrees with the Findings tab rather than double-counting.
  const seenFindings = new Map<string, string>()
  const counts: Counts = {
    rows: rows.length,
    rowsWithoutControl: 0,
    rowsWithoutTests: [], severeWithoutTests: 0,
    tests: 0, completed: 0, awaitingReview: 0, blocked: 0,
    pendingData: [], pendingDocument: [],
    passedRows: 0, exceptionRows: 0,
    concluded: 0, effective: 0, partiallyEffective: 0, ineffective: 0,
    notApplicable: 0, noConclusion: 0,
    adverse: 0, adverseCovered: 0,
    findings: 0, criticalFindings: 0, highFindings: 0,
    ceilingRows: 0,
    agentRows: 0,
    agentTests: (completion?.unreviewed_agent_conclusions ?? []).length,
    reviewedRows: 0,
    unreviewedRows: [],
  }

  for (const row of rows) {
    const rollup = row.execution_rollup
    if (!hasControl(row)) counts.rowsWithoutControl += 1
    const total = testCount(row)
    counts.tests += total
    if (!total) {
      counts.rowsWithoutTests.push(row.id)
      if (row.risk_rating === 'critical' || row.risk_rating === 'high') counts.severeWithoutTests += 1
    }
    for (const item of testRollups(row)) {
      if (hasRun(item)) counts.completed += 1
      else if (item.status === 'blocked') counts.blocked += 1
      else if (item.status === 'review_required') counts.awaitingReview += 1
      if (isPending(item)) {
        (item.kind === 'datatest' ? counts.pendingData : counts.pendingDocument).push(item.test_id)
      }
    }
    if (total && (rollup.completed ?? 0) > 0) {
      if (rollup.exceptions) counts.exceptionRows += 1
      else counts.passedRows += 1
    }

    const conclusion = conclusionOf(row)
    if (CONCLUDED.has(conclusion)) counts.concluded += 1
    if (conclusion === 'effective') counts.effective += 1
    else if (conclusion === 'partially_effective') counts.partiallyEffective += 1
    else if (conclusion === 'ineffective') counts.ineffective += 1
    else if (conclusion === 'not_applicable') counts.notApplicable += 1
    else counts.noConclusion += 1

    const rowFindings = findingsFor(findingRollups, row.id)
    for (const finding of rowFindings) seenFindings.set(finding.id, finding.severity)
    if (ADVERSE.has(conclusion)) {
      counts.adverse += 1
      if (rowFindings.length) counts.adverseCovered += 1
    }

    // The row-level ceiling, not `completion.evidence_ceilings`: the backend
    // omits a limitation the auditor concluded over, because it is no longer
    // something to resolve. It is still a disclosure the reader is owed.
    if (rollup.evidence_ceiling) counts.ceilingRows += 1
    if (agentRows.has(row.id)) counts.agentRows += 1
    if (row.review_status === 'reviewed') counts.reviewedRows += 1
    else counts.unreviewedRows.push(row.id)
  }

  counts.findings = seenFindings.size
  for (const severity of seenFindings.values()) {
    if (severity === 'critical') counts.criticalFindings += 1
    else if (severity === 'high') counts.highFindings += 1
  }
  return counts
}

function executionLane(counts: Counts): StatusLane {
  const pending = counts.pendingData.length + counts.pendingDocument.length
  const chips: StatusChip[] = []
  const actions: StatusAction[] = []

  if (counts.rowsWithoutTests.length) {
    chips.push({
      key: 'no_test',
      label: `${plural(counts.rowsWithoutTests.length, 'risk')} with no test`,
      tone: 'bad',
    })
    actions.push({
      key: 'generate_tests',
      label: `Generate tests (${counts.rowsWithoutTests.length})`,
      tone: 'primary',
      ids: counts.rowsWithoutTests,
      needsAgent: true,
    })
  }
  if (pending) chips.push({ key: 'not_run', label: `${pending} not run`, tone: 'warn' })
  if (counts.blocked) chips.push({ key: 'blocked', label: `${counts.blocked} blocked`, tone: 'bad' })
  if (counts.awaitingReview) {
    chips.push({ key: 'awaiting_review', label: `${counts.awaitingReview} awaiting review`, tone: 'warn' })
  }
  if (counts.pendingData.length) {
    actions.push({
      key: 'run_data_tests',
      label: `Run ${plural(counts.pendingData.length, 'Data Test')}`,
      tone: counts.rowsWithoutTests.length ? 'ghost' : 'primary',
      ids: counts.pendingData,
    })
  }
  if (counts.pendingDocument.length) {
    actions.push({
      key: 'run_document_tests',
      label: `Run ${plural(counts.pendingDocument.length, 'Document Test')}`,
      tone: counts.rowsWithoutTests.length || counts.pendingData.length ? 'ghost' : 'primary',
      ids: counts.pendingDocument,
      needsAgent: true,
    })
  }

  // Nothing linked at all is a different sentence from nothing run: the risks
  // are uncovered, and no amount of running fixes that.
  if (!counts.tests) {
    return {
      key: 'execution', label: 'Execution',
      state: counts.rows ? 'alarm' : 'idle',
      value: '0',
      caption: counts.rows ? `of ${plural(counts.rows, 'risk')} have a test` : 'no risks recorded yet',
      segments: [],
      chips: counts.severeWithoutTests
        ? [{ key: 'no_test', label: `${counts.severeWithoutTests} critical or high`, tone: 'bad' }]
        : chips,
      actions: counts.rows
        ? [{
            key: 'generate_tests',
            label: `Generate planned tests (${counts.rows})`,
            tone: 'primary',
            ids: counts.rowsWithoutTests,
            needsAgent: true,
          }]
        : [],
      rest: counts.rows ? '' : 'Add a risk to begin',
    }
  }

  const done = !counts.rowsWithoutTests.length && !pending && !counts.blocked && !counts.awaitingReview
  if (done) {
    chips.length = 0
    if (counts.passedRows) {
      chips.push({ key: 'passed', label: `${counts.passedRows} passed`, tone: 'ok' })
    }
    if (counts.exceptionRows) {
      chips.push({ key: 'with_exceptions', label: `${counts.exceptionRows} with exceptions`, tone: 'bad' })
    }
  }

  return {
    key: 'execution', label: 'Execution',
    state: counts.rowsWithoutTests.length || counts.blocked ? 'alarm' : done ? 'done' : 'gap',
    value: String(counts.completed),
    total: String(counts.tests),
    caption: `of ${plural(counts.tests, 'test')} run`,
    segments: [
      { tone: 'ok', portion: portion(counts.completed, counts.tests) },
      { tone: 'warn', portion: portion(counts.awaitingReview, counts.tests) },
      { tone: 'bad', portion: portion(counts.blocked, counts.tests) },
    ],
    chips,
    actions: done ? [] : actions,
    rest: done ? 'All linked work has run' : '',
  }
}

function conclusionLane(counts: Counts): StatusLane {
  const chips: StatusChip[] = []
  if (counts.effective) chips.push({ key: 'effective', label: `${counts.effective} effective`, tone: 'ok' })
  if (counts.partiallyEffective) {
    chips.push({ key: 'partially_effective', label: `${counts.partiallyEffective} partially`, tone: 'warn' })
  }
  if (counts.ineffective) {
    chips.push({ key: 'ineffective', label: `${counts.ineffective} ineffective`, tone: 'bad' })
  }
  if (counts.notApplicable) {
    chips.push({ key: 'not_applicable', label: `${counts.notApplicable} not applicable`, tone: 'neutral' })
  }
  if (counts.noConclusion) {
    chips.push({ key: 'no_conclusion', label: `${counts.noConclusion} no conclusion`, tone: 'neutral' })
  }

  const state: LaneState = !counts.concluded ? 'idle' : counts.noConclusion ? 'gap' : 'done'
  const actions: StatusAction[] = state === 'gap'
    ? [{ key: 'refresh_rollup', label: 'Refresh roll-up', tone: 'ghost' }]
    : []

  return {
    key: 'conclusion', label: 'Control conclusion',
    state,
    value: String(counts.concluded),
    total: String(counts.rows),
    caption: `of ${counts.rows} ${pluralWord(counts.rows, 'control')} concluded`,
    // The mix is the point: "all concluded" and "all concluded ineffective"
    // are the same number and opposite engagements.
    segments: [
      { tone: 'ok', portion: portion(counts.effective, counts.rows) },
      { tone: 'warn', portion: portion(counts.partiallyEffective, counts.rows) },
      { tone: 'bad', portion: portion(counts.ineffective, counts.rows) },
      { tone: 'neutral', portion: portion(counts.notApplicable, counts.rows) },
    ],
    chips,
    actions,
    rest: state === 'done'
      ? 'Every row carries a conclusion'
      : state === 'idle' ? 'Conclusions follow execution' : '',
  }
}

function findingsLane(counts: Counts): StatusLane {
  const gap = counts.adverse - counts.adverseCovered

  if (!counts.adverse) {
    return {
      key: 'findings', label: 'Findings',
      state: 'idle',
      value: String(counts.findings),
      caption: counts.findings === 1 ? 'finding drafted' : 'findings drafted',
      segments: [],
      chips: counts.findings
        ? [{ key: 'has_finding', label: 'on rows with no adverse conclusion', tone: 'neutral' }]
        : [],
      actions: [],
      rest: 'Findings follow conclusions',
    }
  }

  if (gap > 0) {
    return {
      key: 'findings', label: 'Findings',
      state: 'alarm',
      value: String(counts.adverseCovered),
      total: String(counts.adverse),
      caption: `of ${counts.adverse} adverse ${pluralWord(counts.adverse, 'control')} ${
        counts.adverse === 1 ? 'has' : 'have'} a finding`,
      segments: [{ tone: 'bad', portion: portion(counts.adverseCovered, counts.adverse) }],
      chips: [
        { key: 'missing_finding', label: `${plural(gap, 'control')} with no finding`, tone: 'bad' },
        { key: 'has_finding', label: `${plural(counts.findings, 'finding')} drafted`, tone: 'neutral' },
      ],
      actions: [{ key: 'draft_findings', label: `Draft ${plural(gap, 'finding')}`, tone: 'warn', needsAgent: true }],
      rest: '',
    }
  }

  const chips: StatusChip[] = [{
    key: 'has_finding',
    label: `${counts.adverseCovered} of ${counts.adverse} adverse ${
      pluralWord(counts.adverse, 'control')} covered`,
    tone: 'neutral',
  }]
  if (counts.criticalFindings) {
    chips.push({ key: 'has_finding', label: `${counts.criticalFindings} critical`, tone: 'bad' })
  }
  if (counts.highFindings) {
    chips.push({ key: 'has_finding', label: `${counts.highFindings} high`, tone: 'warn' })
  }
  return {
    key: 'findings', label: 'Findings',
    state: 'done',
    value: String(counts.findings),
    caption: counts.findings === 1 ? 'finding drafted' : 'findings drafted',
    segments: [{ tone: 'ok', portion: 100 }],
    chips,
    actions: [],
    rest: 'Every adverse control is written up',
  }
}

/**
 * What qualifies the lanes above rather than blocking them. These never gate
 * completion, so they get a quiet strip and never a badge on a lane.
 */
function disclosuresFor(counts: Counts): StatusDisclosure[] {
  const items: StatusDisclosure[] = []
  if (counts.agentTests) {
    items.push({
      key: 'agent',
      mark: 'Agent',
      tone: 'agent',
      message: `${counts.agentTests} of ${counts.tests} test conclusions ${
        counts.agentTests === 1 ? 'was' : 'were'
      } set by the agent and never read by a person.`,
      filter: 'agent_concluded',
    })
  }
  if (counts.ceilingRows) {
    items.push({
      key: 'limit',
      mark: 'Limit',
      tone: 'warn',
      message: `${plural(counts.ceilingRows, 'conclusion')} ${
        counts.ceilingRows === 1 ? 'rests' : 'rest'
      } on evidence that cannot establish population compliance.`,
      filter: 'evidence_limit',
    })
  }
  // Sign-off only becomes a question once there is executed work to sign off.
  if (counts.completed) {
    items.push({
      key: 'review',
      mark: 'Sign-off',
      tone: 'muted',
      message: `${counts.reviewedRows} of ${counts.rows} ${
        pluralWord(counts.rows, 'row')} reviewed. The rest are still marked draft.`,
      filter: 'unreviewed_row',
      // Scoped to the rows the sentence counted, so signing off from here can
      // never reach a row the reader was not just told about. Dropped once
      // there is nothing left to sign, rather than left as a dead button.
      action: counts.unreviewedRows.length
        ? {
            key: 'mark_reviewed',
            label: `Mark ${plural(counts.unreviewedRows.length, 'row')} reviewed`,
            tone: 'ghost',
            ids: counts.unreviewedRows,
          }
        : undefined,
    })
  }
  return items
}

/**
 * The whole filter vocabulary, grouped by the axis each narrowing belongs to.
 *
 * The matrix never had one: its narrowings arrived as lane chips inside an
 * expander, so half of them were unreachable without opening a card first, and
 * the three disclosure strips each carried their own `Show rows` link to a
 * filter the menu did not know about. Declaring them all here is what lets the
 * review bar promote six and keep the rest one click away.
 *
 * Every count is a count of *rows*, because every predicate in `filterRows`
 * selects rows. The agent disclosure counts test conclusions, which is a
 * different population and deliberately not what the chip beside it reports:
 * a chip whose number cannot match the list it produces is the defect the
 * shared model exists to prevent.
 */
function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'execution',
      label: 'Execution',
      options: [
        { key: 'no_test', label: 'No test', value: counts.rowsWithoutTests.length, tone: 'bad' },
        { key: 'not_run', label: 'Not run', value: counts.pendingData.length + counts.pendingDocument.length, tone: 'warn' },
        { key: 'blocked', label: 'Blocked', value: counts.blocked, tone: 'bad' },
        { key: 'awaiting_review', label: 'Awaiting review', value: counts.awaitingReview, tone: 'warn' },
        { key: 'with_exceptions', label: 'With exceptions', value: counts.exceptionRows, tone: 'bad' },
        { key: 'passed', label: 'No exception', value: counts.passedRows, tone: 'ok' },
      ],
    },
    {
      key: 'matrix',
      label: 'The matrix itself',
      options: [
        { key: 'no_control', label: 'No control identified', value: counts.rowsWithoutControl, tone: 'warn' },
        { key: 'evidence_limit', label: 'Evidence limit', value: counts.ceilingRows, tone: 'warn' },
      ],
    },
    {
      key: 'conclusion',
      label: 'Control conclusion',
      options: [
        { key: 'effective', label: 'Effective', value: counts.effective, tone: 'ok' },
        { key: 'partially_effective', label: 'Partially effective', value: counts.partiallyEffective, tone: 'warn' },
        { key: 'ineffective', label: 'Ineffective', value: counts.ineffective, tone: 'bad' },
        { key: 'not_applicable', label: 'Not applicable', value: counts.notApplicable, tone: 'neutral' },
        { key: 'no_conclusion', label: 'Not concluded', value: counts.noConclusion, tone: 'warn' },
      ],
    },
    {
      key: 'findings',
      label: 'Findings',
      options: [
        { key: 'missing_finding', label: 'No finding written', value: counts.adverse - counts.adverseCovered, tone: 'bad' },
        { key: 'has_finding', label: 'Written up', value: counts.adverseCovered, tone: 'neutral' },
      ],
    },
    {
      key: 'signoff',
      label: 'Sign-off',
      options: [
        { key: 'agent_concluded', label: 'Agent-set, unread', value: counts.agentRows, tone: 'warn' },
        { key: 'unreviewed_row', label: 'Not reviewed', value: counts.unreviewedRows.length, tone: 'neutral' },
      ],
    },
  ]
}

/**
 * The six narrowings worth a permanent chip on the matrix, in reading order:
 * what failed, what is owed a write-up, what the evidence cannot carry, what
 * has no control at all, what nobody has read, and what nobody has signed.
 */
export const RCM_CHIPS: ReviewChip[] = [
  { filter: 'ineffective', tone: 'bad', label: 'Ineffective' },
  { filter: 'missing_finding', tone: 'bad', label: 'Findings to draft' },
  { filter: 'evidence_limit', tone: 'warn', label: 'Evidence limits' },
  { filter: 'no_control', tone: 'warn', label: 'No control' },
  { filter: 'agent_concluded', tone: 'agent', label: 'Agent-set, unread' },
  { filter: 'unreviewed_row', tone: 'neutral', label: 'Unreviewed' },
]

/**
 * The count sentence beside the page title. What the matrix holds, rather than
 * what state it is in — the chips beside it answer that.
 */
export function rcmHeadline(rows: RcmRow[]): string {
  if (!rows.length) return 'no risks recorded yet'
  const counts = tally(rows)
  return [
    plural(rows.length, 'risk'),
    plural(rows.length - counts.rowsWithoutControl, 'control'),
    plural(counts.tests, 'test'),
  ].join(' · ')
}

export function rcmStatus(
  rows: RcmRow[],
  findingRollups?: FindingRollups,
  completion?: RcmCompletion | null,
): StatusModel {
  const counts = tally(rows, findingRollups, completion)
  return {
    lanes: [executionLane(counts), conclusionLane(counts), findingsLane(counts)],
    disclosures: disclosuresFor(counts),
    filters: filtersFor(counts),
  }
}

export const FILTER_LABELS: Record<RcmFilter, string> = {
  no_test: 'risks with no test',
  not_run: 'tests not run',
  blocked: 'blocked tests',
  awaiting_review: 'tests awaiting review',
  passed: 'controls with no exception',
  with_exceptions: 'controls with exceptions',
  no_control: 'risks with no control identified',
  effective: 'effective',
  partially_effective: 'partially effective',
  ineffective: 'ineffective',
  not_applicable: 'not applicable',
  no_conclusion: 'no conclusion recorded',
  missing_finding: 'adverse controls with no finding',
  has_finding: 'controls with a finding',
  agent_concluded: 'agent conclusions not reviewed',
  evidence_limit: 'conclusions with a stated evidence limit',
  unreviewed_row: 'rows not yet reviewed',
}

/**
 * Narrow the same array the bar counted. Filtering is a view over the grid, so
 * a filter that matches nothing yields an empty grid rather than the whole one.
 */
export function filterRows(
  rows: RcmRow[],
  filter: RcmFilter | null,
  findingRollups?: FindingRollups,
  completion?: RcmCompletion | null,
): RcmRow[] {
  if (!filter) return rows
  const agentRows = new Set(
    (completion?.unreviewed_agent_conclusions ?? []).map(item => item.rcm_id),
  )
  return rows.filter(row => {
    const rollup = row.execution_rollup
    const conclusion = conclusionOf(row)
    const items = testRollups(row)
    switch (filter) {
      case 'no_test': return testCount(row) === 0
      case 'not_run': return items.some(isPending)
      case 'blocked': return items.some(item => item.status === 'blocked')
      case 'awaiting_review': return items.some(item => item.status === 'review_required')
      case 'passed': return Boolean((rollup.completed ?? 0) && !rollup.exceptions)
      case 'with_exceptions': return Boolean((rollup.completed ?? 0) && rollup.exceptions)
      case 'no_control': return !hasControl(row)
      case 'effective': return conclusion === 'effective'
      case 'partially_effective': return conclusion === 'partially_effective'
      case 'ineffective': return conclusion === 'ineffective'
      case 'not_applicable': return conclusion === 'not_applicable'
      case 'no_conclusion': return !CONCLUDED.has(conclusion)
      case 'missing_finding':
        return ADVERSE.has(conclusion) && !findingsFor(findingRollups, row.id).length
      case 'has_finding': return findingsFor(findingRollups, row.id).length > 0
      case 'agent_concluded': return agentRows.has(row.id)
      case 'evidence_limit': return Boolean(rollup.evidence_ceiling)
      case 'unreviewed_row': return row.review_status !== 'reviewed'
      default: return true
    }
  })
}
