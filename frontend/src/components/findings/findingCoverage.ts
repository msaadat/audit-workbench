import type { AuditFinding, AuditObservation } from '../../types'

/**
 * Which exception tests are already written up, and by which finding.
 *
 * Both test tabs ask this to draw their "no finding written" count and to
 * offer the button that clears it, and the answer is not "is this test id in
 * some finding's `test_refs`". A row's duplicate test flags exactly the
 * records its lead test flags, so the roll-up marks the duplicate's
 * observation `covered_by` the lead's and finding drafting skips it — one
 * exception, one write-up. The lead's finding names only the lead's test, so
 * counting `test_refs` alone left the duplicate permanently owing a finding
 * that the workflow would never draft: the tab offered a button, the run
 * answered "nothing needed doing", and pressing it again changed nothing.
 *
 * So coverage is derived from the observations, which is where the fact lives:
 * a duplicate is spoken for once the observation covering it has a finding.
 * Read fresh on every call rather than taken from the finding's own
 * `covered_test_refs`, which records the same relation for a reader of the
 * file but is written at draft time and not rewritten when a later roll-up
 * changes which tests are duplicates.
 *
 * Requiring the lead's finding to exist is what keeps the count actionable: a
 * covered duplicate whose lead is still undrafted does owe a write-up, and the
 * run scoped to that row will produce it.
 */
export interface FindingCoverage {
  /** Whether this test's exception is written up — by its own finding or its lead's. */
  spokenFor(testId: string): boolean
  /** The findings that speak for this test, its own first. */
  forTest(testId: string): AuditFinding[]
}

export function findingCoverage(
  findings: AuditFinding[] = [],
  observations: AuditObservation[] = [],
): FindingCoverage {
  const direct = new Map<string, AuditFinding[]>()
  for (const finding of findings) {
    for (const testId of finding.test_refs ?? []) {
      const held = direct.get(testId)
      if (held) held.push(finding)
      else direct.set(testId, [finding])
    }
  }
  const bySourceObservation = new Map<string, AuditFinding>()
  for (const finding of findings) {
    const source = finding.source_observation_id
    if (source && !bySourceObservation.has(source)) bySourceObservation.set(source, finding)
  }
  // The lead's finding, for each test whose own observation a duplicate on the
  // same row stands in front of.
  const viaLead = new Map<string, AuditFinding>()
  for (const observation of observations) {
    const lead = observation.covered_by
    const testId = observation.test_id
    if (!lead || !testId) continue
    const finding = bySourceObservation.get(lead)
    if (finding) viaLead.set(testId, finding)
  }
  return {
    spokenFor: testId => direct.has(testId) || viaLead.has(testId),
    forTest: testId => {
      const own = direct.get(testId) ?? []
      const lead = viaLead.get(testId)
      return lead && !own.includes(lead) ? [...own, lead] : own
    },
  }
}
