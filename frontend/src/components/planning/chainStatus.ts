import { plural, pluralWord } from '../../format'
import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusChip, StatusFilterGroup, StatusLane, StatusModel, Tone,
} from '../ui/statusLanes'
import type { FindingRollups, FindingSummary, RcmRow } from '../../types'

/**
 * How far each risk's chain actually reaches, above the list of them.
 *
 * The chain is the one view organised by question rather than by artifact
 * kind: what is this criterion based on, what did we do about it, and what did
 * that show. The page drew that beautifully for one row at a time and could
 * not say a word about the set — so a reviewer could see that *this* row had
 * no test, and had no way to ask how many did.
 *
 * The three lanes are the chain itself. Sourced, tested, written up: the same
 * three questions the spine asks of one row, asked of all of them, and every
 * one of them is a coverage gap when it fails. `29 of 30 tested` is the
 * sentence this page exists to be able to say.
 *
 * Read entirely off the planning payload the view already holds.
 */

export type ChainFilter =
  | 'no_source' | 'sourced'
  | 'no_test' | 'tested'
  | 'exceptions' | 'clean'
  | 'missing_finding' | 'has_finding'
  | 'complete'

export interface ChainLinks {
  sources: number
  tests: number
  exceptions: number
  findings: number
  conclusion: string
}

export function chainLinks(row: RcmRow, rollups?: FindingRollups | null): ChainLinks {
  const rollup = row.execution_rollup ?? {}
  return {
    sources: row.criteria_refs?.length ?? 0,
    tests: rollup.tests ?? row.test_refs?.length ?? 0,
    exceptions: rollup.exceptions ?? 0,
    findings: findingsFor(row.id, rollups).length,
    conclusion: rollup.control_conclusion ?? '',
  }
}

/**
 * A finding points at the rows it was written against; a row does not point
 * back. `RcmRow.finding_refs` exists in the shape but nothing populates it, so
 * the server sends `finding_rollups.by_rcm` as the index to read instead.
 */
export function findingsFor(
  rcmId: string, rollups?: FindingRollups | null,
): FindingSummary[] {
  return rollups?.by_rcm?.[rcmId] ?? []
}

/** A chain is complete when it is sourced, tested, and written up if it owes it. */
export function isComplete(links: ChainLinks): boolean {
  if (!links.sources || !links.tests) return false
  return links.exceptions ? links.findings > 0 : true
}

/** The dot's tone: what the chain found, as every other list in the app reads it. */
export function chainTone(links: ChainLinks): Tone {
  if (links.exceptions) return 'bad'
  if (links.tests) return 'ok'
  return 'neutral'
}

/**
 * How far this chain reaches, in words.
 *
 * The rail said `1 src · 0 test · 0 exc · 0 find` — four abbreviations and four
 * figures, three of which are usually zero. Thirty rows of that is a table a
 * reader has to learn before it says anything. This says the one thing that
 * distinguishes the row, and says the break where there is one, because a
 * chain that stops is the reason to open it.
 */
export function chainSummary(links: ChainLinks): string {
  if (!links.tests) {
    return links.sources ? 'sourced · no test covers it' : 'no source, no test'
  }
  const parts = [`${plural(links.tests, 'test')}`]
  if (links.exceptions) {
    parts.push(`${plural(links.exceptions, 'exception')}`)
    parts.push(links.findings ? `${plural(links.findings, 'finding')}` : 'no finding')
  } else {
    parts.push('no exceptions')
  }
  if (!links.sources) parts.push('no source')
  return parts.join(' · ')
}

interface Counts {
  total: number
  sourced: number
  noSource: string[]
  tested: number
  noTest: string[]
  exceptions: number
  clean: number
  owedFindings: string[]
  written: number
  complete: number
}

export function tally(rows: RcmRow[], rollups?: FindingRollups | null): Counts {
  const counts: Counts = {
    total: rows.length,
    sourced: 0,
    noSource: [],
    tested: 0,
    noTest: [],
    exceptions: 0,
    clean: 0,
    owedFindings: [],
    written: 0,
    complete: 0,
  }
  for (const row of rows) {
    const links = chainLinks(row, rollups)
    if (links.sources) counts.sourced += 1
    else counts.noSource.push(row.id)
    if (links.tests) counts.tested += 1
    else counts.noTest.push(row.id)
    if (links.exceptions) {
      counts.exceptions += 1
      if (links.findings) counts.written += 1
      else counts.owedFindings.push(row.id)
    } else if (links.tests) counts.clean += 1
    if (isComplete(links)) counts.complete += 1
  }
  return counts
}

export function matchesFilter(
  row: RcmRow, filter: ChainFilter, rollups?: FindingRollups | null,
): boolean {
  const links = chainLinks(row, rollups)
  switch (filter) {
    case 'no_source': return links.sources === 0
    case 'sourced': return links.sources > 0
    case 'no_test': return links.tests === 0
    case 'tested': return links.tests > 0
    case 'exceptions': return links.exceptions > 0
    case 'clean': return links.tests > 0 && links.exceptions === 0
    case 'missing_finding': return links.exceptions > 0 && links.findings === 0
    case 'has_finding': return links.findings > 0
    case 'complete': return isComplete(links)
    default: return true
  }
}

export function visibleRows(
  rows: RcmRow[], filters: readonly string[], rollups?: FindingRollups | null,
): RcmRow[] {
  if (!filters.length) return rows
  return rows.filter(row => filters.every(key => matchesFilter(row, key as ChainFilter, rollups)))
}

function sourceLane(counts: Counts): StatusLane {
  if (!counts.total) {
    return {
      key: 'sources', label: 'Sourced', state: 'idle',
      value: '0', caption: 'risks in the matrix',
      segments: [], chips: [], actions: [], rest: 'The matrix is empty',
    }
  }
  const chips: StatusChip[] = []
  if (counts.noSource.length) {
    chips.push({ key: 'no_source', label: `${counts.noSource.length} with no anchor`, tone: 'warn' })
  }
  return {
    key: 'sources', label: 'Sourced',
    state: counts.noSource.length ? 'gap' : 'done',
    value: String(counts.sourced),
    total: String(counts.total),
    caption: `of ${plural(counts.total, 'risk')} cite a document`,
    segments: [{ tone: 'ok', portion: portion(counts.sourced, counts.total) }],
    chips,
    actions: [],
    // A criterion with no anchor is not wrong, it is unverifiable: the prose
    // names a document nobody can open from here.
    rest: 'Every criterion cites a document you can open',
  }
}

/**
 * `Tested`, not `Run`: the tests ran on their own page, and what this one asks
 * is whether any of them reaches this risk. The lane keys avoid `execution`
 * and `findings` for the same reason — `UiReviewBar` renames those two to
 * `Run` and `Findings` for the pages where that is the question.
 */
function testLane(counts: Counts): StatusLane {
  if (!counts.total) {
    return {
      key: 'coverage', label: 'Tested', state: 'idle',
      value: '0', caption: 'risks covered',
      segments: [], chips: [], actions: [], rest: 'The matrix is empty',
    }
  }
  const chips: StatusChip[] = []
  if (counts.noTest.length) {
    chips.push({ key: 'no_test', label: `${counts.noTest.length} uncovered`, tone: 'bad' })
  }
  if (counts.exceptions) {
    chips.push({ key: 'exceptions', label: `${counts.exceptions} with exceptions`, tone: 'bad' })
  }
  if (counts.clean) {
    chips.push({ key: 'clean', label: `${counts.clean} clean`, tone: 'ok' })
  }
  return {
    key: 'coverage', label: 'Tested',
    state: counts.noTest.length ? 'alarm' : 'done',
    value: String(counts.tested),
    total: String(counts.total),
    caption: `of ${plural(counts.total, 'risk')} covered by a test`,
    segments: [
      { tone: 'bad', portion: portion(counts.exceptions, counts.total) },
      { tone: 'ok', portion: portion(counts.clean, counts.total) },
    ],
    chips,
    actions: [],
    rest: 'Every risk is covered',
  }
}

function findingLane(counts: Counts): StatusLane {
  if (!counts.exceptions) {
    return {
      key: 'writeup', label: 'Written up', state: 'idle',
      value: '0', caption: 'risks with exceptions',
      segments: [], chips: [], actions: [],
      rest: counts.tested ? 'No test found an exception' : 'Findings follow execution',
    }
  }
  const gap = counts.owedFindings.length
  return {
    key: 'writeup', label: 'Written up',
    state: gap ? 'alarm' : 'done',
    value: String(counts.written),
    total: String(counts.exceptions),
    caption: `of ${counts.exceptions} exception ${pluralWord(counts.exceptions, 'risk')} written up`,
    segments: [{ tone: gap ? 'bad' : 'ok', portion: portion(counts.written, counts.exceptions) }],
    chips: [
      ...(gap ? [{ key: 'missing_finding' as const, label: `${gap} with no finding`, tone: 'bad' as const }] : []),
      ...(counts.written ? [{ key: 'has_finding' as const, label: `${counts.written} written up`, tone: 'neutral' as const }] : []),
    ],
    actions: [],
    rest: 'Every exception is written up',
  }
}

function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'source',
      label: 'What it rests on',
      options: [
        { key: 'no_source', label: 'No cited source', value: counts.noSource.length, tone: 'warn' },
        { key: 'sourced', label: 'Cites a document', value: counts.sourced, tone: 'ok' },
      ],
    },
    {
      key: 'coverage',
      label: 'What was done about it',
      options: [
        { key: 'no_test', label: 'No test covers it', value: counts.noTest.length, tone: 'bad' },
        { key: 'exceptions', label: 'Found exceptions', value: counts.exceptions, tone: 'bad' },
        { key: 'clean', label: 'Tested, no exception', value: counts.clean, tone: 'ok' },
      ],
    },
    {
      key: 'writeup',
      label: 'What it became',
      options: [
        { key: 'missing_finding', label: 'Exceptions with no finding', value: counts.owedFindings.length, tone: 'bad' },
        { key: 'has_finding', label: 'Written up as a finding', value: counts.written, tone: 'neutral' },
        { key: 'complete', label: 'Chain complete', value: counts.complete, tone: 'ok' },
      ],
    },
  ]
}

export function chainStatus(rows: RcmRow[], rollups?: FindingRollups | null): StatusModel {
  const counts = tally(rows, rollups)
  return {
    lanes: [sourceLane(counts), testLane(counts), findingLane(counts)],
    disclosures: [],
    filters: filtersFor(counts),
  }
}

/**
 * The chips worth a permanent row, in the order the chain breaks: uncovered
 * first, because a risk nothing tests is the gap this page exists to find.
 */
export const CHAIN_CHIPS: ReviewChip[] = [
  { filter: 'no_test', tone: 'bad', label: 'No test' },
  { filter: 'missing_finding', tone: 'bad', label: 'No finding' },
  { filter: 'exceptions', tone: 'bad', label: 'Exceptions' },
  { filter: 'no_source', tone: 'warn', label: 'No source' },
  { filter: 'complete', tone: 'ok', label: 'Complete' },
]

/**
 * Deepest chains first: the rows worth reading are the ones that reached the
 * end, then the ones that broke late, then the ones that never started.
 */
export function ranked(rows: RcmRow[], rollups?: FindingRollups | null) {
  return rows
    .map(row => ({ row, links: chainLinks(row, rollups) }))
    .sort((left, right) => {
      const depth = (item: { links: ChainLinks }) =>
        (item.links.findings ? 8 : 0) + (item.links.exceptions ? 4 : 0)
        + (item.links.tests ? 2 : 0) + (item.links.sources ? 1 : 0)
      return depth(right) - depth(left) || left.row.id.localeCompare(right.row.id)
    })
}
