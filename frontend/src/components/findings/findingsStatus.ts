import { plural, pluralWord } from '../../format'
import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusChip, StatusDisclosure, StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'
import type { AuditFinding, FindingSeverity } from '../../types'

/** Worst first, which is the order the register is read in. */
export const SEVERITY_ORDER: FindingSeverity[] = ['critical', 'high', 'medium', 'low', 'info']

/**
 * Whether the findings are ready to report, in three answers.
 *
 * The rail lists findings and lets you search them; nothing on the page says
 * how many are agreed, how many are supported, and how many still owe work.
 * Those are separate questions and a finding can pass one while failing another
 * — the common case is a file of confirmed, fully evidenced findings where
 * every root cause is still open, which reads as finished until the report
 * refuses to draw a conclusion from it.
 *
 * Everything is read off the finding record. No new endpoint.
 */

export type FindingsFilter =
  | 'confirmed' | 'unconfirmed'
  | 'no_evidence' | 'no_rcm_link' | 'no_test_link' | 'evidence_warning'
  | 'cause_pending' | 'no_response'
  | 'agent_authored'

export type FindingsActionKey = 'generate_findings' | 'confirm_all'

interface Counts {
  total: number
  confirmed: number
  unconfirmed: string[]
  noEvidence: number
  noRcmLink: number
  noTestLink: number
  evidenceWarnings: number
  unsupported: number
  causePending: number
  responseMissing: number
  settled: number
  agentAuthored: number
  reportable: number
}

function hasText(value: unknown): boolean {
  return Boolean(String(value ?? '').trim())
}

/** A finding nothing in the file stands behind is not reportable. */
function unsupported(item: AuditFinding): boolean {
  return !item.evidence_refs.length
    || !item.rcm_refs.length
    || !item.test_refs.length
    || Boolean(item.evidence_warnings?.length)
}

/**
 * What the report can actually carry: agreed by an auditor, and traceable.
 *
 * Confirmation and support are separate answers — the file's live shape is
 * eighteen confirmed findings none of which names a risk — so neither alone
 * says whether a finding reaches the report.
 */
function reportable(item: AuditFinding): boolean {
  return item.auditor_confirmed && !unsupported(item)
}

/** Nothing further is owed on it: the cause is settled and the response is in. */
function settled(item: AuditFinding): boolean {
  return !item.cause_pending && hasText(item.management_response)
}

function tally(items: AuditFinding[]): Counts {
  const counts: Counts = {
    total: items.length,
    confirmed: 0, unconfirmed: [],
    noEvidence: 0, noRcmLink: 0, noTestLink: 0, evidenceWarnings: 0, unsupported: 0,
    causePending: 0, responseMissing: 0, settled: 0,
    agentAuthored: 0, reportable: 0,
  }
  for (const item of items) {
    if (item.auditor_confirmed) counts.confirmed += 1
    else counts.unconfirmed.push(item.id)
    if (!item.evidence_refs.length) counts.noEvidence += 1
    if (!item.rcm_refs.length) counts.noRcmLink += 1
    if (!item.test_refs.length) counts.noTestLink += 1
    if (item.evidence_warnings?.length) counts.evidenceWarnings += 1
    if (unsupported(item)) counts.unsupported += 1
    if (item.cause_pending) counts.causePending += 1
    if (!hasText(item.management_response)) counts.responseMissing += 1
    if (settled(item)) counts.settled += 1
    if (item.source === 'agent') counts.agentAuthored += 1
    if (reportable(item)) counts.reportable += 1
  }
  return counts
}

function confirmedLane(counts: Counts): StatusLane {
  if (!counts.total) {
    return {
      key: 'confirmed', label: 'Confirmed', state: 'idle',
      value: '0', caption: 'findings recorded',
      segments: [],
      chips: [],
      actions: [{
        key: 'generate_findings', label: 'Generate all findings',
        tone: 'primary', needsAgent: true,
      }],
      rest: '',
    }
  }
  const outstanding = counts.unconfirmed.length
  return {
    key: 'confirmed', label: 'Confirmed', state: outstanding ? 'gap' : 'done',
    value: String(counts.confirmed),
    total: String(counts.total),
    caption: `of ${counts.total} ${pluralWord(counts.total, 'finding')} confirmed for reporting`,
    segments: [{ tone: 'ok', portion: portion(counts.confirmed, counts.total) }],
    chips: [
      ...(counts.confirmed
        ? [{ key: 'confirmed' as const, label: `${counts.confirmed} confirmed`, tone: 'ok' as const }]
        : []),
      ...(outstanding
        ? [{ key: 'unconfirmed' as const, label: `${outstanding} unconfirmed`, tone: 'warn' as const }]
        : []),
    ],
    // The bulk confirm skips anything incomplete, so it is offered as what it
    // is — an attempt on the outstanding ones, not a guarantee about them.
    actions: outstanding
      ? [{ key: 'confirm_all', label: `Confirm ${outstanding}`, tone: 'primary', ids: counts.unconfirmed }]
      : [],
    rest: outstanding ? '' : 'Every finding is confirmed for reporting',
  }
}

function supportLane(counts: Counts): StatusLane {
  const chips: StatusChip[] = []
  if (counts.noEvidence) {
    chips.push({ key: 'no_evidence', label: `${counts.noEvidence} without evidence`, tone: 'bad' })
  }
  if (counts.noRcmLink) {
    chips.push({ key: 'no_rcm_link', label: `${counts.noRcmLink} not linked to a risk`, tone: 'bad' })
  }
  if (counts.noTestLink) {
    chips.push({ key: 'no_test_link', label: `${counts.noTestLink} not linked to a test`, tone: 'warn' })
  }
  if (counts.evidenceWarnings) {
    chips.push({ key: 'evidence_warning', label: `${counts.evidenceWarnings} with stale evidence`, tone: 'bad' })
  }

  if (!counts.total) {
    return {
      key: 'support', label: 'Supported', state: 'idle',
      value: '0', caption: 'findings traced to the file',
      segments: [], chips: [], actions: [],
      rest: 'Support follows a drafted finding',
    }
  }
  const supported = counts.total - counts.unsupported
  return {
    key: 'support', label: 'Supported', state: counts.unsupported ? 'alarm' : 'done',
    value: String(supported),
    total: String(counts.total),
    caption: `of ${counts.total} traced to evidence, a risk and a test`,
    segments: [{
      tone: counts.unsupported ? 'bad' : 'ok',
      portion: portion(supported, counts.total),
    }],
    chips,
    // Linking is auditor work with no batch behind it, so the lane points at
    // the findings rather than offering a button that cannot do the job.
    actions: [],
    rest: counts.unsupported
      ? `${plural(counts.unsupported, 'finding')} ${
        counts.unsupported === 1 ? 'is' : 'are'} missing support — open each to link it`
      : 'Every finding is traceable to the file',
  }
}

function followUpLane(counts: Counts): StatusLane {
  if (!counts.total) {
    return {
      key: 'follow_up', label: 'Settled', state: 'idle',
      value: '0', caption: 'findings settled',
      segments: [], chips: [], actions: [],
      rest: 'Follow-up follows a drafted finding',
    }
  }
  const chips: StatusChip[] = []
  if (counts.causePending) {
    chips.push({ key: 'cause_pending', label: `${counts.causePending} root cause pending`, tone: 'warn' })
  }
  if (counts.responseMissing) {
    chips.push({ key: 'no_response', label: `${counts.responseMissing} awaiting management response`, tone: 'warn' })
  }
  const outstanding = counts.total - counts.settled
  return {
    key: 'follow_up', label: 'Settled', state: outstanding ? 'alarm' : 'done',
    value: String(counts.settled),
    total: String(counts.total),
    caption: `of ${counts.total} have a settled cause and a response`,
    segments: [{
      tone: outstanding ? 'warn' : 'ok',
      portion: portion(counts.settled, counts.total),
    }],
    chips,
    // Neither a root cause nor management's own words can be generated, so
    // this lane never offers to produce them.
    actions: [],
    rest: outstanding
      ? 'Recorded on each finding as the cause is established and the response arrives'
      : 'Nothing further is outstanding',
  }
}

function disclosuresFor(counts: Counts): StatusDisclosure[] {
  const items: StatusDisclosure[] = []
  if (counts.agentAuthored) {
    items.push({
      key: 'agent', mark: 'Agent', tone: 'agent',
      message: `${counts.agentAuthored} of ${counts.total} ${
        pluralWord(counts.total, 'finding')} ${
        counts.agentAuthored === 1 ? 'was' : 'were'
      } drafted by the assistant. The narrative is copied into the report unchanged.`,
      filter: 'agent_authored',
    })
  }
  if (counts.evidenceWarnings) {
    items.push({
      key: 'evidence', mark: 'Evidence', tone: 'warn',
      message: `${plural(counts.evidenceWarnings, 'finding')} ${
        counts.evidenceWarnings === 1 ? 'cites' : 'cite'
      } evidence that no longer matches its source.`,
      filter: 'evidence_warning',
    })
  }
  return items
}

/**
 * Every narrowing the register offers, grouped by the question it answers.
 *
 * Four axes, because they compose: a finding can be unconfirmed *and* missing
 * a risk *and* awaiting a response, and each is a different piece of work by a
 * different person. Within an axis they cannot — the review bar holds at most
 * one per group.
 */
function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'reporting',
      label: 'Reporting',
      options: [
        { key: 'unconfirmed', label: 'Not confirmed', value: counts.unconfirmed.length, tone: 'warn' },
        { key: 'confirmed', label: 'Confirmed', value: counts.confirmed, tone: 'ok' },
      ],
    },
    {
      key: 'support',
      label: 'Support',
      options: [
        { key: 'no_rcm_link', label: 'Not linked to a risk', value: counts.noRcmLink, tone: 'bad' },
        { key: 'no_evidence', label: 'No evidence', value: counts.noEvidence, tone: 'bad' },
        { key: 'evidence_warning', label: 'Evidence moved', value: counts.evidenceWarnings, tone: 'bad' },
        { key: 'no_test_link', label: 'Not linked to a test', value: counts.noTestLink, tone: 'warn' },
      ],
    },
    {
      key: 'follow_up',
      label: 'Follow-up',
      options: [
        { key: 'cause_pending', label: 'Root cause pending', value: counts.causePending, tone: 'warn' },
        { key: 'no_response', label: 'No management response', value: counts.responseMissing, tone: 'warn' },
      ],
    },
    {
      key: 'authorship',
      label: 'Authorship',
      options: [
        { key: 'agent_authored', label: 'Drafted by the assistant', value: counts.agentAuthored, tone: 'neutral' },
      ],
    },
  ]
}

export function findingsStatus(items: AuditFinding[]): StatusModel {
  const counts = tally(items)
  return {
    lanes: [confirmedLane(counts), supportLane(counts), followUpLane(counts)],
    disclosures: disclosuresFor(counts),
    filters: filtersFor(counts),
  }
}

/**
 * The seven narrowings worth naming on the register, in reading order.
 *
 * Seven names for six slots: `Unconfirmed` leads the row when anything is
 * unconfirmed, and drops out of it entirely when nothing is — which is how a
 * file whose whole register is agreed still shows what that register is
 * missing. The bar draws only the chips that count something, so the rest of
 * the vocabulary stays behind the pressed chip.
 */
export const FINDING_CHIPS: ReviewChip[] = [
  { filter: 'unconfirmed', tone: 'warn', label: 'Unconfirmed' },
  { filter: 'no_rcm_link', tone: 'bad', label: 'Not linked to a risk' },
  { filter: 'no_evidence', tone: 'bad', label: 'No evidence' },
  { filter: 'evidence_warning', tone: 'bad', label: 'Evidence moved' },
  { filter: 'cause_pending', tone: 'warn', label: 'Root cause pending' },
  { filter: 'no_response', tone: 'warn', label: 'No management response' },
  { filter: 'agent_authored', tone: 'agent', label: 'Drafted by the assistant' },
]

/**
 * What one finding still owes, in the order it is owed.
 *
 * The list row carries the short words and the verdict bar the full ones, from
 * one derivation: a row that says `no risk` and a bar that says the finding is
 * ready would be the same contradiction the old page shipped, where a green
 * status chip sat above an unlinked draft.
 */
export interface FindingOpenItem {
  key: FindingsFilter
  /** For the list's meta line, where the row has one line to spend. */
  short: string
  /** For the verdict bar, which is a sentence. */
  label: string
  tone: 'bad' | 'warn'
}

export function openItems(item: AuditFinding): FindingOpenItem[] {
  const items: FindingOpenItem[] = []
  if (!item.rcm_refs.length) {
    items.push({ key: 'no_rcm_link', short: 'no risk', label: 'not linked to a risk', tone: 'bad' })
  }
  if (!item.evidence_refs.length) {
    items.push({ key: 'no_evidence', short: 'no evidence', label: 'no evidence anchor', tone: 'bad' })
  }
  if (item.evidence_warnings?.length) {
    items.push({
      key: 'evidence_warning', short: 'evidence moved',
      label: 'evidence changed since drafting', tone: 'bad',
    })
  }
  if (!item.test_refs.length) {
    items.push({ key: 'no_test_link', short: 'no test', label: 'not linked to a test', tone: 'warn' })
  }
  if (item.cause_pending) {
    items.push({ key: 'cause_pending', short: 'cause pending', label: 'root cause pending', tone: 'warn' })
  }
  if (!hasText(item.management_response)) {
    items.push({ key: 'no_response', short: 'no response', label: 'no management response', tone: 'warn' })
  }
  return items
}

export const FINDINGS_FILTER_LABELS: Record<FindingsFilter, string> = {
  confirmed: 'findings confirmed for reporting',
  unconfirmed: 'findings not yet confirmed',
  no_evidence: 'findings without evidence',
  no_rcm_link: 'findings not linked to a risk',
  no_test_link: 'findings not linked to a test',
  evidence_warning: 'findings citing stale evidence',
  cause_pending: 'findings with the root cause pending',
  no_response: 'findings awaiting a management response',
  agent_authored: 'findings drafted by the assistant',
}

/** Narrow the same list the lanes counted, so the two can never disagree. */
export function filterFindings(
  items: AuditFinding[], filter: FindingsFilter | null,
): AuditFinding[] {
  if (!filter) return items
  return items.filter(item => {
    switch (filter) {
      case 'confirmed': return item.auditor_confirmed
      case 'unconfirmed': return !item.auditor_confirmed
      case 'no_evidence': return !item.evidence_refs.length
      case 'no_rcm_link': return !item.rcm_refs.length
      case 'no_test_link': return !item.test_refs.length
      case 'evidence_warning': return Boolean(item.evidence_warnings?.length)
      case 'cause_pending': return item.cause_pending
      case 'no_response': return !hasText(item.management_response)
      case 'agent_authored': return item.source === 'agent'
      default: return true
    }
  })
}
