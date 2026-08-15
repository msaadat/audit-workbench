import type { TriageCount } from '../components/ui/UiTriageCounts.vue'
import type { DataTest, TestConclusionState } from '../types'

/** The conclusion axis, as chips. It sits beside the outcome chips rather than
 *  replacing them: "which of these exceptions has nobody concluded on?" is two
 *  questions about the same row, and neither answers the other. */
const FACETS: ReadonlyArray<{ key: TestConclusionState; label: string; tone: TriageCount['tone'] }> = [
  { key: 'none', label: 'Not concluded', tone: 'warn' },
  { key: 'stale', label: 'Stale', tone: 'danger' },
  { key: 'agent', label: 'By agent', tone: 'info' },
  { key: 'auditor', label: 'By auditor', tone: 'ok' },
]

/**
 * Count one chip row over the states of whatever the other filters left. The
 * number on a chip is what clicking it would leave on screen, so it is taken
 * within the active outcome rather than across the engagement.
 */
export function conclusionCounts(states: Iterable<TestConclusionState>): TriageCount[] {
  const tally = new Map<TestConclusionState, number>()
  let total = 0
  for (const state of states) {
    tally.set(state, (tally.get(state) ?? 0) + 1)
    total += 1
  }
  return [
    { key: 'all', label: 'Any', value: total },
    ...FACETS.map(facet => ({ ...facet, value: tally.get(facet.key) ?? 0 })),
  ]
}

/**
 * Where a Data Test's conclusion stands. The doc-test summary derives the same
 * four states server-side, because a worklist row there is an item and the
 * conclusion it reports belongs to the test above it.
 *
 * The control conclusion alone decides whether one was recorded. Written
 * reasoning without it is not a conclusion — a run that narrates a result and
 * still reaches `no_conclusion` has left the sign-off outstanding, which is
 * exactly what "Not concluded" is meant to surface.
 */
export function dataTestConclusion(test: DataTest): TestConclusionState {
  if (test.control_conclusion === 'no_conclusion') return 'none'
  if (test.control_conclusion_stale) return 'stale'
  return test.control_conclusion_source === 'auditor' ? 'auditor' : 'agent'
}
