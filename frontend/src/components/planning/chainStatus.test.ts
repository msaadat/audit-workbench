import { describe, expect, it } from 'vitest'

import {
  CHAIN_CHIPS, chainLinks, chainStatus, chainSummary, chainTone, isComplete, matchesFilter,
  ranked, visibleRows,
} from './chainStatus'
import type { ChainFilter } from './chainStatus'
import type { FindingRollups, RcmRow } from '../../types'

/**
 * The spine could answer its three questions about one risk and could not
 * answer them about the matrix. On the engagement this was built against,
 * thirty of thirty-two risks had no test at all, and the page had no way to
 * say so.
 */

function row(id: string, overrides: Record<string, unknown> = {}): RcmRow {
  return {
    id,
    risk: `Risk ${id}.`,
    control: `Control for ${id}`,
    process: 'Requisition initiation',
    criteria: '',
    criteria_refs: [],
    test_refs: [],
    finding_refs: [],
    evidence_refs: [],
    execution_rollup: { tests: 0, exceptions: 0, control_conclusion: '' },
    review_status: 'draft',
    ...overrides,
  } as unknown as RcmRow
}

const anchored = { criteria_refs: [{ id: 'c1', source_id: 'doc-1' }] }
const tested = { execution_rollup: { tests: 2, exceptions: 0, control_conclusion: 'effective' } }
const failing = { execution_rollup: { tests: 2, exceptions: 3, control_conclusion: 'ineffective' } }

const FINDING = { id: 'F-1', title: 'A finding', severity: 'medium' }
function rollups(map: Record<string, unknown[]>): FindingRollups {
  return { by_rcm: map, by_test: {}, by_procedure: {} } as unknown as FindingRollups
}

describe('how far a chain reaches', () => {
  it('reads its links off the records the payload already carries', () => {
    const links = chainLinks(row('RCM-1', { ...anchored, ...failing }), rollups({ 'RCM-1': [FINDING] }))
    expect(links).toEqual({ sources: 1, tests: 2, exceptions: 3, findings: 1, conclusion: 'ineffective' })
  })

  it('is complete only when it is sourced, tested, and written up if it owes it', () => {
    expect(isComplete(chainLinks(row('a', { ...anchored, ...tested })))).toBe(true)
    // Exceptions with no finding is the break this page exists to find.
    expect(isComplete(chainLinks(row('b', { ...anchored, ...failing })))).toBe(false)
    expect(isComplete(chainLinks(row('c', { ...anchored, ...failing }), rollups({ c: [FINDING] })))).toBe(true)
    expect(isComplete(chainLinks(row('d', tested)))).toBe(false)
    expect(isComplete(chainLinks(row('e', anchored)))).toBe(false)
  })

  /**
   * The dot says what the chain found, as every other list in the app reads
   * it; the meta line says how far it got. Reading one for the other is what
   * made thirty rows of `0 test 0 exc 0 find` unscannable.
   */
  it.each<[string, Record<string, unknown>, string, string]>([
    ['a risk nothing covers', {}, 'neutral', 'no source, no test'],
    ['a sourced risk nothing covers', anchored, 'neutral', 'sourced · no test covers it'],
    ['a clean chain', { ...anchored, ...tested }, 'ok', '2 tests · no exceptions'],
    ['exceptions with nothing written up', { ...anchored, ...failing }, 'bad', '2 tests · 3 exceptions · no finding'],
    ['a tested risk with no anchor', tested, 'ok', '2 tests · no exceptions · no source'],
  ])('says %s', (_name, overrides, tone, summary) => {
    const links = chainLinks(row('RCM-1', overrides))
    expect(chainTone(links)).toBe(tone)
    expect(chainSummary(links)).toBe(summary)
  })
})

describe('the three lanes are the three hops', () => {
  const rows = [
    row('RCM-1', { ...anchored, ...failing }),
    row('RCM-2', { ...anchored, ...tested }),
    row('RCM-3'),
  ]
  const index = rollups({ 'RCM-1': [FINDING] })

  it('counts what is sourced, what is covered, and what is written up', () => {
    const model = chainStatus(rows, index)
    const [sources, coverage, writeup] = model.lanes

    expect([sources.value, sources.total]).toEqual(['2', '3'])
    expect([coverage.value, coverage.total]).toEqual(['2', '3'])
    // Written up counts against the risks that owe a finding, not against all.
    expect([writeup.value, writeup.total]).toEqual(['1', '1'])
    expect(writeup.state).toBe('done')
    // `Run` and `Findings` are what `UiReviewBar` renames `execution` and
    // `findings` to; this page asks about coverage and write-up.
    expect(model.lanes.map(lane => lane.label)).toEqual(['Sourced', 'Tested', 'Written up'])
  })

  it('raises the alarm on an uncovered risk and on an unwritten exception', () => {
    const bare = chainStatus(rows, null)
    expect(bare.lanes[1].state).toBe('alarm')
    expect(bare.lanes[2].state).toBe('alarm')
    expect(bare.lanes[2].chips[0].key).toBe('missing_finding')
  })

  it('says nothing is owed when nothing found anything', () => {
    const lane = chainStatus([row('RCM-2', { ...anchored, ...tested })], null).lanes[2]
    expect(lane.state).toBe('idle')
    expect(lane.rest).toBe('No test found an exception')
  })
})

describe('narrowing the matrix', () => {
  const rows = [
    row('RCM-1', { ...anchored, ...failing }),
    row('RCM-2', { ...anchored, ...tested }),
    row('RCM-3'),
  ]

  it('composes across axes', () => {
    expect(visibleRows(rows, ['no_test'], null).map(item => item.id)).toEqual(['RCM-3'])
    expect(visibleRows(rows, ['sourced'], null).map(item => item.id)).toEqual(['RCM-1', 'RCM-2'])
    expect(visibleRows(rows, ['sourced', 'exceptions'], null).map(item => item.id)).toEqual(['RCM-1'])
  })

  it('names an exception with no finding, which is the gap', () => {
    expect(matchesFilter(rows[0], 'missing_finding', null)).toBe(true)
    expect(matchesFilter(rows[0], 'missing_finding', rollups({ 'RCM-1': [FINDING] }))).toBe(false)
  })

  it('offers every chip as a filter the page actually has', () => {
    const keys = new Set(
      chainStatus(rows, null).filters!.flatMap(group => group.options.map(option => option.key)),
    )
    for (const chip of CHAIN_CHIPS) expect(keys.has(chip.filter as ChainFilter)).toBe(true)
    expect(CHAIN_CHIPS.length).toBeLessThanOrEqual(6)
  })

  it('puts the deepest chains first', () => {
    const order = ranked(rows, rollups({ 'RCM-1': [FINDING] })).map(entry => entry.row.id)
    expect(order).toEqual(['RCM-1', 'RCM-2', 'RCM-3'])
  })
})
