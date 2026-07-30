import type { RcmRow } from '../../types'

/**
 * Where a risk stands, as a state of assurance rather than eleven columns of
 * cells. The RCM grid answers "what is stored against this row"; the board
 * answers "is this risk covered, and if not, whose move is it".
 *
 * The four states are exhaustive and mutually exclusive by construction —
 * `coverageState` returns exactly one for any row — so every row appears in
 * exactly one column of the board and the two views cannot disagree.
 */

export type CoverageState = 'no_coverage' | 'needs_you' | 'testing' | 'concluded'

export const COVERAGE_ORDER: CoverageState[] = [
  'no_coverage', 'testing', 'needs_you', 'concluded',
]

export const COVERAGE_LABELS: Record<CoverageState, string> = {
  no_coverage: 'No coverage',
  testing: 'Agent testing',
  needs_you: 'Needs you',
  concluded: 'Concluded',
}

const CONCLUDED = new Set(['effective', 'partially_effective', 'ineffective', 'not_applicable'])

export function testCount(row: RcmRow): number {
  return row.execution_rollup.tests ?? row.test_refs.length
}

/** True when a person, not the agent, owns the next move on this row. */
function pendingOnAuditor(row: RcmRow): boolean {
  const rollup = row.execution_rollup
  return Boolean(
    (rollup.review_required ?? 0)
    || (rollup.blocked ?? 0)
    || (rollup.open_exceptions ?? 0)
    || row.review_status === 'review_required',
  )
}

export function coverageState(row: RcmRow): CoverageState {
  // A risk with no test is uncovered whatever else is recorded against it —
  // that is the finding the board exists to make impossible to miss.
  if (!testCount(row)) return 'no_coverage'
  // Anything still waiting on a person outranks a recorded conclusion: a
  // concluded row with open exceptions is not finished work.
  if (pendingOnAuditor(row)) return 'needs_you'
  if (CONCLUDED.has(String(row.execution_rollup.control_conclusion ?? ''))) return 'concluded'
  return 'testing'
}

/** The agent's proposed next move, in the auditor's words. */
export function nextMove(row: RcmRow, state: CoverageState): string {
  const rollup = row.execution_rollup
  const total = testCount(row)
  if (state === 'no_coverage') return 'Agent can specify a test for this risk'
  if (state === 'needs_you') {
    const parts: string[] = []
    if (rollup.open_exceptions) parts.push(`${rollup.open_exceptions} exception(s) recorded`)
    if (rollup.review_required) parts.push(`${rollup.review_required} test(s) awaiting review`)
    if (rollup.blocked) parts.push(`${rollup.blocked} test(s) blocked`)
    if (!parts.length) parts.push('Row is marked for review')
    return parts.join(' · ')
  }
  if (state === 'concluded') {
    const conclusion = String(rollup.control_conclusion ?? '').replaceAll('_', ' ')
    const findings = rollup.findings ?? 0
    return findings ? `${conclusion} · ${findings} finding(s)` : conclusion
  }
  // Execution finished but no conclusion was recorded. The four states have no
  // column for that, and it is the agent's roll-up that owes it — so the row
  // stays here and the card says what is actually outstanding rather than
  // reporting progress that has already finished.
  const completed = rollup.completed ?? 0
  if (total && completed >= total) return 'Tests complete — awaiting roll-up conclusion'
  return `${completed} of ${total} test(s) complete`
}

export interface CoverageColumn {
  state: CoverageState
  label: string
  rows: RcmRow[]
  /** Critical/high rows in the column — the part worth alarming about. */
  severe: number
}

export function coverageColumns(rows: RcmRow[]): CoverageColumn[] {
  const grouped = new Map<CoverageState, RcmRow[]>(
    COVERAGE_ORDER.map(state => [state, [] as RcmRow[]]),
  )
  for (const row of rows) grouped.get(coverageState(row))!.push(row)
  return COVERAGE_ORDER.map(state => {
    const columnRows = grouped.get(state)!
    return {
      state,
      label: COVERAGE_LABELS[state],
      rows: columnRows,
      severe: columnRows.filter(row => ['critical', 'high'].includes(row.risk_rating)).length,
    }
  })
}
