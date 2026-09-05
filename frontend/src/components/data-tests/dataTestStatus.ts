import { plural, pluralWord } from '../../format'
import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusChip, StatusDisclosure, StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'
import type { AuditFinding, DataTest } from '../../types'

/**
 * Where the Data Tests stand, above the list of them.
 *
 * Three questions, in the order the work answers them: how much has run, how
 * much of it supports a conclusion, and who reached one — because a page of
 * green "no exception" results looks identical whether an auditor signed each
 * conclusion or an unattended run wrote them all.
 *
 * The same tally also produces `filters`, the page's whole narrowing
 * vocabulary. Deriving both here is what keeps the menu and the chips from
 * disagreeing: they are the same numbers, and there is one active filter
 * between them.
 *
 * Read entirely off the Data Test records the tab already holds.
 */

const CONCLUDED = new Set(['effective', 'partially_effective', 'ineffective', 'not_applicable'])

export type DataTestFilter =
  | 'not_run' | 'blocked' | 'awaiting_review' | 'stale_result' | 'passed' | 'with_exceptions'
  | 'effective' | 'partially_effective' | 'ineffective' | 'not_applicable' | 'no_conclusion'
  | 'stale_conclusion'
  | 'missing_finding' | 'has_finding'
  | 'agent_concluded' | 'auditor_concluded' | 'exploratory' | 'semantic_warning'

export type DataTestActionKey = 'run_tests' | 'rerun_stale' | 'draft_findings'

interface Counts {
  total: number
  run: number
  notRun: string[]
  blocked: number
  awaitingReview: number
  staleResults: string[]
  passed: number
  withExceptions: number
  concluded: number
  effective: number
  partiallyEffective: number
  ineffective: number
  notApplicable: number
  noConclusion: number
  staleConclusions: number
  agentConcluded: number
  auditorConcluded: number
  exploratory: number
  semanticWarnings: number
  /** RCM-linked exception tests — the ones a finding can be drafted against. */
  exceptionTests: number
  exceptionTestsCovered: number
  undrafted: string[]
  /** Exceptions on tests that support no RCM row, so no finding is owed. */
  exploratoryExceptions: number
}

function hasRun(test: DataTest): boolean {
  return Boolean(test.last_run)
}

/**
 * A test carrying a warning about what it actually measures.
 *
 * Two sources, one lane: what the definition looked wrong about when it was
 * saved, and what the run could not vouch for once it read the data. Neither
 * stops the test concluding, so this lane is the only place the page says so.
 */
function hasSemanticWarning(test: DataTest): boolean {
  return test.semantic_warnings.length > 0 || test.last_run?.semantic_valid === false
}

/**
 * A test the Run action can move. Blocked work needs something else first, and
 * a test awaiting review has already run — neither is served by running it.
 */
function isPending(test: DataTest): boolean {
  return !hasRun(test) && test.status !== 'blocked' && test.status !== 'review_required'
}

function tally(tests: DataTest[], findings: AuditFinding[]): Counts {
  const drafted = new Set(findings.flatMap(finding => finding.test_refs))
  const counts: Counts = {
    total: tests.length,
    run: 0, notRun: [], blocked: 0, awaitingReview: 0, staleResults: [],
    passed: 0, withExceptions: 0,
    concluded: 0, effective: 0, partiallyEffective: 0, ineffective: 0,
    notApplicable: 0, noConclusion: 0, staleConclusions: 0,
    agentConcluded: 0, auditorConcluded: 0, exploratory: 0, semanticWarnings: 0,
    exceptionTests: 0, exceptionTestsCovered: 0, undrafted: [], exploratoryExceptions: 0,
  }

  for (const test of tests) {
    if (hasRun(test)) counts.run += 1
    if (isPending(test)) counts.notRun.push(test.id)
    if (test.status === 'blocked') counts.blocked += 1
    if (test.status === 'review_required') counts.awaitingReview += 1
    if (test.result_stale) counts.staleResults.push(test.id)
    if (test.status === 'completed_no_exception') counts.passed += 1
    if (test.status === 'completed_with_exception') counts.withExceptions += 1

    const conclusion = String(test.control_conclusion ?? '') || 'no_conclusion'
    if (CONCLUDED.has(conclusion)) counts.concluded += 1
    if (conclusion === 'effective') counts.effective += 1
    else if (conclusion === 'partially_effective') counts.partiallyEffective += 1
    else if (conclusion === 'ineffective') counts.ineffective += 1
    else if (conclusion === 'not_applicable') counts.notApplicable += 1
    else counts.noConclusion += 1
    if (test.control_conclusion_stale) counts.staleConclusions += 1
    if (CONCLUDED.has(conclusion)) {
      if (test.control_conclusion_source === 'agent') counts.agentConcluded += 1
      else counts.auditorConcluded += 1
    }

    if (!test.rcm_id) counts.exploratory += 1
    if (hasSemanticWarning(test)) counts.semanticWarnings += 1

    if (test.status === 'completed_with_exception') {
      // Drafting is per RCM row, so an exploratory test has no row to draft
      // against. Counting it in the denominator would leave the lane
      // permanently short of a write-up that can never exist.
      if (!test.rcm_id) counts.exploratoryExceptions += 1
      else {
        counts.exceptionTests += 1
        if (drafted.has(test.id)) counts.exceptionTestsCovered += 1
        else counts.undrafted.push(test.id)
      }
    }
  }
  return counts
}

function executionLane(counts: Counts): StatusLane {
  if (!counts.total) {
    return {
      key: 'execution', label: 'Execution', state: 'idle',
      value: '0', caption: 'Data Tests defined',
      segments: [], chips: [], actions: [],
      rest: 'Add a test to begin',
    }
  }
  const chips: StatusChip[] = []
  const actions: StatusLane['actions'] = []
  if (counts.notRun.length) {
    chips.push({ key: 'not_run', label: `${counts.notRun.length} not run`, tone: 'warn' })
    actions.push({
      key: 'run_tests', label: `Run ${plural(counts.notRun.length, 'test')}`,
      tone: 'primary', ids: counts.notRun,
    })
  }
  if (counts.blocked) chips.push({ key: 'blocked', label: `${counts.blocked} blocked`, tone: 'bad' })
  if (counts.awaitingReview) {
    chips.push({ key: 'awaiting_review', label: `${counts.awaitingReview} awaiting review`, tone: 'warn' })
  }
  // A stale result describes a definition or a population that has since moved.
  // It ran, so it is not "not run"; it is no longer current, so it is not done.
  if (counts.staleResults.length) {
    chips.push({ key: 'stale_result', label: `${counts.staleResults.length} stale`, tone: 'bad' })
    actions.push({
      key: 'rerun_stale', label: `Re-run ${plural(counts.staleResults.length, 'stale test')}`,
      tone: counts.notRun.length ? 'ghost' : 'primary', ids: counts.staleResults,
    })
  }

  const settled = !counts.notRun.length && !counts.blocked
    && !counts.awaitingReview && !counts.staleResults.length
  // Shown whether or not the lane has settled. These used to wait for a quiet
  // lane because a second row of outcome chips was already saying them; that
  // row is gone, and "how did the results fall" is a question worth answering
  // while half the programme is still running.
  if (counts.passed) chips.push({ key: 'passed', label: `${counts.passed} no exception`, tone: 'ok' })
  if (counts.withExceptions) {
    chips.push({ key: 'with_exceptions', label: `${counts.withExceptions} with exceptions`, tone: 'bad' })
  }
  return {
    key: 'execution', label: 'Execution',
    state: counts.blocked || counts.staleResults.length ? 'alarm' : settled ? 'done' : 'gap',
    value: String(counts.run),
    total: String(counts.total),
    caption: `of ${plural(counts.total, 'test')} run`,
    segments: [
      { tone: 'ok', portion: portion(counts.run - counts.staleResults.length, counts.total) },
      { tone: 'bad', portion: portion(counts.staleResults.length, counts.total) },
    ],
    chips,
    actions,
    rest: settled ? 'Every test has a current result' : '',
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
  if (counts.staleConclusions) {
    chips.push({ key: 'stale_conclusion', label: `${counts.staleConclusions} stale`, tone: 'bad' })
  }

  if (!counts.total || !counts.run) {
    return {
      key: 'conclusion', label: 'Control conclusion', state: 'idle',
      value: '0', total: String(counts.total), caption: `of ${counts.total} concluded`,
      segments: [], chips: [], actions: [],
      rest: 'Conclusions follow execution',
    }
  }
  const outstanding = counts.noConclusion || counts.staleConclusions
  return {
    key: 'conclusion', label: 'Control conclusion',
    state: counts.staleConclusions ? 'alarm' : counts.noConclusion ? 'gap' : 'done',
    value: String(counts.concluded),
    total: String(counts.total),
    caption: `of ${counts.total} ${pluralWord(counts.total, 'test')} concluded`,
    segments: [
      { tone: 'ok', portion: portion(counts.effective, counts.total) },
      { tone: 'warn', portion: portion(counts.partiallyEffective, counts.total) },
      { tone: 'bad', portion: portion(counts.ineffective, counts.total) },
      { tone: 'neutral', portion: portion(counts.notApplicable, counts.total) },
    ],
    chips,
    // A conclusion is reached on the test, one at a time. There is no batch
    // behind it, so the lane points at the tests rather than offering a button.
    actions: [],
    rest: outstanding
      ? 'Recorded on each test once its result is settled'
      : 'Every test carries a conclusion',
  }
}

function findingsLane(counts: Counts): StatusLane {
  if (!counts.exceptionTests) {
    return {
      key: 'findings', label: 'Findings', state: 'idle',
      value: '0', caption: 'exceptions to write up',
      segments: [], chips: [], actions: [],
      rest: !counts.run
        ? 'Findings follow execution'
        : counts.exploratoryExceptions
          ? `Exceptions were found only by ${
            plural(counts.exploratoryExceptions, 'exploratory test')}, which support no RCM row`
          : 'No test found an exception',
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
 * The whole filter vocabulary, grouped by the axis each narrowing belongs to.
 *
 * The lanes carry the same keys as chips, but a lane can only show one axis
 * without misleading: "effective" and "set by the agent" are both true of the
 * same test, and a single row mixing them reads as a distribution that does
 * not add up. Grouping here is what let the two permanent chip rows above the
 * list go away without losing a filter.
 */
function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'execution',
      label: 'Execution',
      options: [
        { key: 'not_run', label: 'Not run', value: counts.notRun.length, tone: 'warn' },
        { key: 'blocked', label: 'Blocked', value: counts.blocked, tone: 'bad' },
        { key: 'awaiting_review', label: 'Awaiting review', value: counts.awaitingReview, tone: 'warn' },
        { key: 'stale_result', label: 'Stale result', value: counts.staleResults.length, tone: 'bad' },
        { key: 'with_exceptions', label: 'With exceptions', value: counts.withExceptions, tone: 'bad' },
        { key: 'passed', label: 'No exception', value: counts.passed, tone: 'ok' },
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
        { key: 'stale_conclusion', label: 'Stale conclusion', value: counts.staleConclusions, tone: 'bad' },
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
      key: 'scope',
      label: 'Scope',
      options: [
        { key: 'exploratory', label: 'Exploratory', value: counts.exploratory, tone: 'neutral' },
        { key: 'semantic_warning', label: 'Carries a warning', value: counts.semanticWarnings, tone: 'warn' },
      ],
    },
  ]
}

function disclosuresFor(counts: Counts): StatusDisclosure[] {
  const items: StatusDisclosure[] = []
  if (counts.agentConcluded) {
    items.push({
      key: 'agent', mark: 'Agent', tone: 'agent',
      message: `${counts.agentConcluded} of ${counts.concluded} ${
        pluralWord(counts.concluded, 'conclusion')} ${
        counts.agentConcluded === 1 ? 'was' : 'were'
      } set by the agent and never read by a person.`,
      filter: 'agent_concluded',
    })
  }
  if (counts.exploratory) {
    items.push({
      key: 'exploratory', mark: 'Scope', tone: 'muted',
      message: `${plural(counts.exploratory, 'test')} ${
        counts.exploratory === 1 ? 'is' : 'are'
      } exploratory and support no RCM row.`,
      filter: 'exploratory',
    })
  }
  if (counts.semanticWarnings) {
    items.push({
      key: 'semantic', mark: 'Check', tone: 'warn',
      message: `${plural(counts.semanticWarnings, 'test')} ${
        counts.semanticWarnings === 1 ? 'carries a warning' : 'carry warnings'
      } about what the test actually measures.`,
      filter: 'semantic_warning',
    })
  }
  return items
}

export function dataTestStatus(tests: DataTest[], findings: AuditFinding[] = []): StatusModel {
  const counts = tally(tests, findings)
  return {
    lanes: [executionLane(counts), conclusionLane(counts), findingsLane(counts)],
    disclosures: disclosuresFor(counts),
    filters: filtersFor(counts),
  }
}

/**
 * The six narrowings worth a permanent chip on this page, in reading order.
 *
 * They are the questions asked on arrival: what is still failing, what is owed
 * a write-up, what cannot be relied on, what nobody has read, and what came
 * back clean. Everything else in `filtersFor` stays one click behind the
 * pressed chip — the review bar's popover — rather than spending a row on a
 * distinction most engagements never need.
 */
export const DATA_TEST_CHIPS: ReviewChip[] = [
  { filter: 'with_exceptions', tone: 'bad', label: 'Exceptions open' },
  { filter: 'missing_finding', tone: 'bad', label: 'Findings to draft' },
  { filter: 'semantic_warning', tone: 'warn', label: 'Measurement warnings' },
  { filter: 'agent_concluded', tone: 'agent', label: 'Agent-set, unread' },
  { filter: 'passed', tone: 'ok', label: 'No exception' },
]


export const DATA_TEST_FILTER_LABELS: Record<DataTestFilter, string> = {
  not_run: 'tests not run',
  blocked: 'blocked tests',
  awaiting_review: 'tests awaiting review',
  stale_result: 'tests whose result is stale',
  passed: 'tests with no exception',
  with_exceptions: 'tests with exceptions',
  effective: 'effective',
  partially_effective: 'partially effective',
  ineffective: 'ineffective',
  not_applicable: 'not applicable',
  no_conclusion: 'no conclusion recorded',
  stale_conclusion: 'conclusions reached against evidence that moved',
  missing_finding: 'exception tests with no finding',
  has_finding: 'tests written up as a finding',
  agent_concluded: 'conclusions set by the agent',
  auditor_concluded: 'conclusions an auditor recorded',
  exploratory: 'exploratory tests',
  semantic_warning: 'tests carrying a warning',
}

/** Narrow the same list the lanes counted, so the two can never disagree. */
export function filterDataTests(
  tests: DataTest[], filter: DataTestFilter | null, findings: AuditFinding[] = [],
): DataTest[] {
  if (!filter) return tests
  const drafted = new Set(findings.flatMap(finding => finding.test_refs))
  return tests.filter(test => {
    const conclusion = String(test.control_conclusion ?? '') || 'no_conclusion'
    switch (filter) {
      case 'not_run': return isPending(test)
      case 'blocked': return test.status === 'blocked'
      case 'awaiting_review': return test.status === 'review_required'
      case 'stale_result': return test.result_stale
      case 'passed': return test.status === 'completed_no_exception'
      case 'with_exceptions': return test.status === 'completed_with_exception'
      case 'effective': return conclusion === 'effective'
      case 'partially_effective': return conclusion === 'partially_effective'
      case 'ineffective': return conclusion === 'ineffective'
      case 'not_applicable': return conclusion === 'not_applicable'
      case 'no_conclusion': return !CONCLUDED.has(conclusion)
      case 'stale_conclusion': return test.control_conclusion_stale
      case 'missing_finding':
        return test.status === 'completed_with_exception' && Boolean(test.rcm_id) && !drafted.has(test.id)
      case 'has_finding': return drafted.has(test.id)
      case 'agent_concluded':
        return test.control_conclusion_source === 'agent' && CONCLUDED.has(conclusion)
      case 'auditor_concluded':
        return test.control_conclusion_source !== 'agent' && CONCLUDED.has(conclusion)
      case 'exploratory': return !test.rcm_id
      case 'semantic_warning': return hasSemanticWarning(test)
      default: return true
    }
  })
}
