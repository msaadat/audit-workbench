import { plural, pluralWord } from '../../format'
import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusChip, StatusDisclosure, StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'
import type { AuditFinding, DocTestSummaryEntry, DocTestSummaryPayload } from '../../types'

/**
 * Where the Document Tests stand, above the worklist.
 *
 * This page counts two different populations — 21 tests and 23 worklist items —
 * and mixing them is how a status bar ends up contradicting the chips beneath
 * it. The rule here is explicit: execution and findings are counted in worklist
 * entries, because that is what the list below shows; conclusions are counted
 * in tests, because `conclusion_state` is test-grain and repeated on every item
 * of the test.
 *
 * The lanes say how much has run, how much carries a settled conclusion, and
 * how much of what failed has been written up. The same tally also produces
 * `filters`, the page's whole narrowing vocabulary, so the filter menu and the
 * lane chips are the same numbers driving one active filter.
 */

export type DocTestFilter =
  | 'not_run' | 'awaiting_evidence' | 'needs_review' | 'exceptions' | 'confirmed'
  | 'no_call'
  | 'no_conclusion' | 'stale_conclusion' | 'agent_concluded' | 'auditor_concluded'
  | 'missing_finding' | 'has_finding'
  | 'evidence_request'

export type DocTestActionKey = 'run_tests' | 'draft_findings'

/**
 * An item the runner has read and nobody has answered for.
 *
 * Distinct from `not_run`, which is work that has not happened, and from
 * `needs_review`, which is a decision to defer — this is the silent middle,
 * and it is the commonest state on a page of results an agent produced.
 * A stale call counts here too: it stands on the record but not as current.
 */
function noCall(entry: DocTestSummaryEntry): boolean {
  if (entry.entry_type === 'cycle_test') return (entry.disposition_counts.pending ?? 0) > 0
  return entry.disposition.state === 'pending' || entry.disposition.stale
}

interface Counts {
  entries: number
  executed: number
  notRunTests: string[]
  notRunEntries: number
  awaitingEvidence: number
  needsReview: number
  exceptions: number
  confirmed: number
  noCall: number
  evidenceRequests: number
  tests: number
  concluded: number
  noConclusion: number
  staleConclusion: number
  agentConcluded: number
  auditorConcluded: number
  exceptionTests: number
  exceptionTestsCovered: number
  undrafted: string[]
}

function tally(payload: DocTestSummaryPayload | null, findings: AuditFinding[]): Counts {
  const entries = payload?.entries ?? []
  const drafted = new Set(findings.flatMap(finding => finding.test_refs))
  const counts: Counts = {
    entries: entries.length, executed: 0,
    notRunTests: [], notRunEntries: 0, awaitingEvidence: 0, needsReview: 0,
    exceptions: 0, confirmed: 0, noCall: 0, evidenceRequests: 0,
    tests: 0, concluded: 0, noConclusion: 0, staleConclusion: 0,
    agentConcluded: 0, auditorConcluded: 0,
    exceptionTests: 0, exceptionTestsCovered: 0, undrafted: [],
  }
  const notRun = new Set<string>()
  // Conclusion and findings are test-grain. Walking entries and folding into a
  // map by test id keeps a six-item test from counting its conclusion six times.
  const byTest = new Map<string, DocTestSummaryEntry>()

  for (const entry of entries) {
    if (entry.classification === 'not_run') {
      counts.notRunEntries += 1
      notRun.add(entry.test_id)
    } else counts.executed += 1
    if (entry.classification === 'awaiting_evidence') counts.awaitingEvidence += 1
    if (entry.classification === 'needs_review') counts.needsReview += 1
    if (entry.classification === 'exception') counts.exceptions += 1
    if (entry.classification === 'confirmed') counts.confirmed += 1
    if (noCall(entry)) counts.noCall += 1
    if (entry.entry_type === 'item' && entry.evidence_request_count) counts.evidenceRequests += 1
    if (!byTest.has(entry.test_id)) byTest.set(entry.test_id, entry)
  }
  counts.notRunTests = Array.from(notRun)

  for (const entry of byTest.values()) {
    counts.tests += 1
    if (entry.conclusion_state === 'none') counts.noConclusion += 1
    else counts.concluded += 1
    if (entry.conclusion_state === 'stale') counts.staleConclusion += 1
    if (entry.conclusion_state === 'agent') counts.agentConcluded += 1
    if (entry.conclusion_state === 'auditor') counts.auditorConcluded += 1

    if (entry.test_status === 'completed_with_exception') {
      counts.exceptionTests += 1
      if (drafted.has(entry.test_id)) counts.exceptionTestsCovered += 1
      // Drafting is per RCM row, so a test with no row has nothing to draft
      // against and is not counted as a finding the file is missing.
      else if (entry.rcm_id) counts.undrafted.push(entry.test_id)
    }
  }
  return counts
}

function executionLane(counts: Counts): StatusLane {
  if (!counts.entries) {
    return {
      key: 'execution', label: 'Execution', state: 'idle',
      value: '0', caption: 'items to work',
      segments: [], chips: [], actions: [],
      rest: 'Add a test to begin',
    }
  }
  const chips: StatusChip[] = []
  if (counts.notRunEntries) {
    chips.push({ key: 'not_run', label: `${counts.notRunEntries} not run`, tone: 'warn' })
  }
  // Evidence that never arrived is not fixed by running the test again, so it
  // is counted here but deliberately excluded from what the button runs.
  if (counts.awaitingEvidence) {
    chips.push({ key: 'awaiting_evidence', label: `${counts.awaitingEvidence} awaiting evidence`, tone: 'bad' })
  }
  if (counts.needsReview) {
    chips.push({ key: 'needs_review', label: `${counts.needsReview} need review`, tone: 'warn' })
  }
  const settled = !counts.notRunEntries && !counts.awaitingEvidence
  // Shown whether or not the lane has settled. A second row of outcome chips
  // used to carry these; it is gone, and how the calls fell is worth reading
  // while the rest of the worklist is still being worked.
  if (counts.confirmed) chips.push({ key: 'confirmed', label: `${counts.confirmed} confirmed`, tone: 'ok' })
  if (counts.exceptions) chips.push({ key: 'exceptions', label: `${counts.exceptions} exceptions`, tone: 'bad' })
  return {
    key: 'execution', label: 'Execution',
    state: counts.awaitingEvidence ? 'alarm' : counts.notRunEntries ? 'gap' : 'done',
    value: String(counts.executed),
    total: String(counts.entries),
    caption: `of ${counts.entries} ${pluralWord(counts.entries, 'item')} executed`,
    segments: [{ tone: 'ok', portion: portion(counts.executed, counts.entries) }],
    chips,
    actions: counts.notRunTests.length
      ? [{
          key: 'run_tests',
          label: `Run ${plural(counts.notRunTests.length, 'test')}`,
          tone: 'primary', ids: counts.notRunTests, needsAgent: true,
        }]
      : [],
    rest: settled ? 'Every item has been worked' : '',
  }
}

function conclusionLane(counts: Counts): StatusLane {
  if (!counts.tests || !counts.executed) {
    return {
      key: 'conclusion', label: 'Control conclusion', state: 'idle',
      value: '0', total: String(counts.tests), caption: `of ${counts.tests} concluded`,
      segments: [], chips: [], actions: [],
      rest: 'Conclusions follow execution',
    }
  }
  const chips: StatusChip[] = []
  if (counts.noConclusion) {
    chips.push({ key: 'no_conclusion', label: `${counts.noConclusion} no conclusion`, tone: 'neutral' })
  }
  if (counts.staleConclusion) {
    chips.push({ key: 'stale_conclusion', label: `${counts.staleConclusion} stale`, tone: 'bad' })
  }
  if (counts.agentConcluded) {
    chips.push({ key: 'agent_concluded', label: `${counts.agentConcluded} agent-set`, tone: 'warn' })
  }
  return {
    key: 'conclusion', label: 'Control conclusion',
    state: counts.staleConclusion ? 'alarm' : counts.noConclusion ? 'gap' : 'done',
    value: String(counts.concluded),
    total: String(counts.tests),
    caption: `of ${counts.tests} ${pluralWord(counts.tests, 'test')} concluded`,
    segments: [
      { tone: 'ok', portion: portion(counts.auditorConcluded, counts.tests) },
      { tone: 'warn', portion: portion(counts.agentConcluded, counts.tests) },
      { tone: 'bad', portion: portion(counts.staleConclusion, counts.tests) },
    ],
    chips,
    // Concluding is per test, in the test's own panel. No batch stands behind
    // it, so the lane points at the tests rather than offering a button.
    actions: [],
    rest: counts.noConclusion || counts.staleConclusion
      ? 'Recorded on each test once its items are dispositioned'
      : 'Every test carries a conclusion',
  }
}

function findingsLane(counts: Counts): StatusLane {
  if (!counts.exceptionTests) {
    return {
      key: 'findings', label: 'Findings', state: 'idle',
      value: '0', caption: 'exceptions to write up',
      segments: [], chips: [], actions: [],
      rest: counts.executed ? 'No test found an exception' : 'Findings follow execution',
    }
  }
  const gap = counts.undrafted.length
  return {
    key: 'findings', label: 'Findings',
    state: gap ? 'alarm' : 'done',
    value: String(counts.exceptionTestsCovered),
    total: String(counts.exceptionTests),
    caption: `of ${counts.exceptionTests} exception ${
      pluralWord(counts.exceptionTests, 'test')} written up`,
    segments: [{
      tone: gap ? 'bad' : 'ok',
      portion: portion(counts.exceptionTestsCovered, counts.exceptionTests),
    }],
    chips: [
      ...(gap ? [{ key: 'missing_finding' as const, label: `${gap} with no finding`, tone: 'bad' as const }] : []),
      ...(counts.exceptionTestsCovered
        ? [{ key: 'has_finding' as const, label: `${counts.exceptionTestsCovered} written up`, tone: 'neutral' as const }]
        : []),
    ],
    actions: gap
      ? [{
          key: 'draft_findings', label: `Draft findings (${gap})`,
          tone: 'warn', ids: counts.undrafted, needsAgent: true,
        }]
      : [],
    rest: gap ? '' : 'Every exception is written up',
  }
}

/**
 * The whole filter vocabulary, grouped by axis. Execution is counted in
 * worklist items and conclusions in tests, exactly as the lanes count them, so
 * a menu row and the lane beside it can never report different numbers for the
 * same narrowing.
 */
function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'execution',
      label: 'Execution',
      options: [
        { key: 'not_run', label: 'Not run', value: counts.notRunEntries, tone: 'warn' },
        { key: 'awaiting_evidence', label: 'Awaiting evidence', value: counts.awaitingEvidence, tone: 'bad' },
        { key: 'needs_review', label: 'Need review', value: counts.needsReview, tone: 'warn' },
        { key: 'exceptions', label: 'Exceptions', value: counts.exceptions, tone: 'bad' },
        { key: 'confirmed', label: 'Confirmed', value: counts.confirmed, tone: 'ok' },
      ],
    },
    {
      key: 'call',
      label: 'Your call',
      options: [
        { key: 'no_call', label: 'Not recorded', value: counts.noCall, tone: 'neutral' },
      ],
    },
    {
      key: 'conclusion',
      label: 'Control conclusion',
      options: [
        { key: 'no_conclusion', label: 'Not concluded', value: counts.noConclusion, tone: 'warn' },
        { key: 'stale_conclusion', label: 'Stale conclusion', value: counts.staleConclusion, tone: 'bad' },
      ],
    },
    {
      key: 'source',
      label: 'Concluded by',
      options: [
        { key: 'agent_concluded', label: 'Agent', value: counts.agentConcluded, tone: 'warn' },
        { key: 'auditor_concluded', label: 'Auditor', value: counts.auditorConcluded, tone: 'ok' },
      ],
    },
    {
      key: 'findings',
      label: 'Findings',
      options: [
        { key: 'missing_finding', label: 'No finding written', value: counts.undrafted.length, tone: 'bad' },
        { key: 'has_finding', label: 'Written up', value: counts.exceptionTestsCovered, tone: 'neutral' },
      ],
    },
    {
      key: 'evidence',
      label: 'Evidence',
      options: [
        { key: 'evidence_request', label: 'Open request', value: counts.evidenceRequests, tone: 'warn' },
      ],
    },
  ]
}

function disclosuresFor(counts: Counts): StatusDisclosure[] {
  const items: StatusDisclosure[] = []
  if (counts.agentConcluded) {
    items.push({
      key: 'agent', mark: 'Agent', tone: 'agent',
      message: `${counts.agentConcluded} of ${counts.tests} test ${
        pluralWord(counts.tests, 'conclusion')} ${
        counts.agentConcluded === 1 ? 'was' : 'were'
      } set by the agent and never read by a person.`,
      filter: 'agent_concluded',
    })
  }
  if (counts.evidenceRequests) {
    items.push({
      key: 'evidence', mark: 'Evidence', tone: 'muted',
      message: `${plural(counts.evidenceRequests, 'item')} ${
        counts.evidenceRequests === 1 ? 'has' : 'have'
      } an open evidence request against the client.`,
      filter: 'evidence_request',
    })
  }
  return items
}

export function docTestStatus(
  payload: DocTestSummaryPayload | null, findings: AuditFinding[] = [],
): StatusModel {
  const counts = tally(payload, findings)
  return {
    lanes: [executionLane(counts), conclusionLane(counts), findingsLane(counts)],
    disclosures: disclosuresFor(counts),
    filters: filtersFor(counts),
  }
}

/**
 * The six narrowings worth a permanent chip on this page, in reading order.
 *
 * `no_call` is the one the old bar could not state: a worklist of items the
 * runner settled and nobody answered for looks finished in every other count
 * on this page.
 */
export const DOC_TEST_CHIPS: ReviewChip[] = [
  { filter: 'exceptions', tone: 'bad', label: 'Exception' },
  { filter: 'confirmed', tone: 'ok', label: 'Confirmed' },
  { filter: 'agent_concluded', tone: 'agent', label: 'Agent-set, unread' },
  { filter: 'no_call', tone: 'neutral', label: 'Call not recorded' },
  { filter: 'needs_review', tone: 'warn', label: 'Needs review' },
]


export const DOC_TEST_FILTER_LABELS: Record<DocTestFilter, string> = {
  not_run: 'items not run',
  awaiting_evidence: 'items awaiting evidence',
  needs_review: 'items needing review',
  exceptions: 'items concluded as exceptions',
  confirmed: 'items confirmed',
  no_call: 'items no auditor has answered for',
  no_conclusion: 'items whose test has no conclusion',
  stale_conclusion: 'items whose conclusion was reached against evidence that moved',
  agent_concluded: 'items whose conclusion the agent set',
  auditor_concluded: 'items whose conclusion an auditor recorded',
  missing_finding: 'exception tests with no finding',
  has_finding: 'items written up as a finding',
  evidence_request: 'items with an open evidence request',
}

/**
 * Narrow the same entries the lanes counted. Conclusion filters are test-grain
 * and select every item of a matching test, which is what the worklist shows.
 */
export function filterDocTestEntries(
  entries: DocTestSummaryEntry[], filter: DocTestFilter | null, findings: AuditFinding[] = [],
): DocTestSummaryEntry[] {
  if (!filter) return entries
  const drafted = new Set(findings.flatMap(finding => finding.test_refs))
  return entries.filter(entry => {
    switch (filter) {
      case 'not_run': return entry.classification === 'not_run'
      case 'awaiting_evidence': return entry.classification === 'awaiting_evidence'
      case 'needs_review': return entry.classification === 'needs_review'
      case 'exceptions': return entry.classification === 'exception'
      case 'confirmed': return entry.classification === 'confirmed'
      case 'no_call': return noCall(entry)
      case 'no_conclusion': return entry.conclusion_state === 'none'
      case 'stale_conclusion': return entry.conclusion_state === 'stale'
      case 'agent_concluded': return entry.conclusion_state === 'agent'
      case 'auditor_concluded': return entry.conclusion_state === 'auditor'
      case 'missing_finding':
        return entry.test_status === 'completed_with_exception'
          && Boolean(entry.rcm_id) && !drafted.has(entry.test_id)
      case 'has_finding': return drafted.has(entry.test_id)
      case 'evidence_request':
        return entry.entry_type === 'item' && entry.evidence_request_count > 0
      default: return true
    }
  })
}
