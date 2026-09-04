import { describe, expect, it } from 'vitest'

import type {
  AuditFinding, DocTestSummaryEntry, DocTestSummaryPayload,
} from '../../types'
import {
  DOC_TEST_CHIPS, docTestHeadline, docTestStatus, filterDocTestEntries,
} from './docTestStatus'

function item(
  testId: string, itemId: string, overrides: Partial<DocTestSummaryEntry> = {},
): DocTestSummaryEntry {
  return {
    entry_type: 'item', test_id: testId, test_title: testId, test_kind: 'qa',
    test_status: 'completed_no_exception', rcm_id: 'RCM-1',
    item_id: itemId, label: itemId, instruction: '', state: 'confirmed',
    classification: 'confirmed', conclusion_state: 'auditor',
    evaluation: {}, disposition: {}, question: '', response: '', runner_note: '',
    document_count: 1, citation_count: 1, evidence_count: 1,
    checks_total: 1, checks_matched: 1, checks_failed: 0,
    missing_document_types: [], image_only: false, evidence_request_count: 0,
    has_conflict: false, updated: '',
    ...overrides,
  } as DocTestSummaryEntry
}

function payload(entries: DocTestSummaryEntry[]): DocTestSummaryPayload {
  return { entry_counts: {}, test_counts: {}, tested_item_counts: {}, assertion_counts: {}, entries } as DocTestSummaryPayload
}

function finding(testRefs: string[]): AuditFinding {
  return { id: 'F-1', test_refs: testRefs } as AuditFinding
}

const lanes = (entries: DocTestSummaryEntry[], findings: AuditFinding[] = []) =>
  Object.fromEntries(docTestStatus(payload(entries), findings).lanes.map(lane => [lane.key, lane]))

describe('document test execution lane', () => {
  it('counts worklist items, which is the unit the list below shows', () => {
    const lane = lanes([
      item('DT-1', 'I-1'),
      item('DT-1', 'I-2'),
      item('DT-2', 'I-3', { classification: 'not_run' }),
    ]).execution

    // Three items across two tests: the lane says items, never tests.
    expect(`${lane.value} ${lane.caption}`).toBe('2 of 3 items executed')
    expect(lane.actions).toEqual([{
      key: 'run_tests', label: 'Run 1 test', tone: 'primary', ids: ['DT-2'], needsAgent: true,
    }])
  })

  it('keeps items awaiting evidence out of what the run would act on', () => {
    const lane = lanes([
      item('DT-1', 'I-1', { classification: 'awaiting_evidence' }),
    ]).execution

    // Running it again does not make the missing document arrive.
    expect(lane.state).toBe('alarm')
    expect(lane.actions).toEqual([])
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 awaiting evidence'])
  })

  it('rests once every item has been worked', () => {
    const lane = lanes([
      item('DT-1', 'I-1'),
      item('DT-2', 'I-2', { classification: 'exception', test_status: 'completed_with_exception' }),
    ]).execution

    expect(lane.state).toBe('done')
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 confirmed', '1 exceptions'])
    expect(lane.rest).toBe('Every item has been worked')
  })
})

describe('document test conclusion lane', () => {
  it('counts tests, not items, so a long test concludes once', () => {
    const lane = lanes([
      item('DT-1', 'I-1'), item('DT-1', 'I-2'), item('DT-1', 'I-3'),
      item('DT-2', 'I-4', { conclusion_state: 'none' }),
    ]).conclusion

    expect(`${lane.value} ${lane.caption}`).toBe('1 of 2 tests concluded')
    expect(lane.state).toBe('gap')
  })

  it('separates a conclusion the agent set from one an auditor signed', () => {
    const lane = lanes([
      item('DT-1', 'I-1'),
      item('DT-2', 'I-2', { conclusion_state: 'agent' }),
    ]).conclusion

    expect(lane.state).toBe('done')
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 agent-set'])
    expect(lane.segments.map(segment => Math.round(segment.portion))).toEqual([50, 50, 0])
  })

  it('raises a conclusion reached against evidence that has since moved', () => {
    const lane = lanes([item('DT-1', 'I-1', { conclusion_state: 'stale' })]).conclusion
    expect(lane.state).toBe('alarm')
  })
})

describe('document test findings lane', () => {
  it('measures write-up per exception test, however many items it has', () => {
    const entries = [
      item('DT-1', 'I-1', { classification: 'exception', test_status: 'completed_with_exception' }),
      item('DT-1', 'I-2', { classification: 'exception', test_status: 'completed_with_exception' }),
      item('DT-2', 'I-3', { classification: 'exception', test_status: 'completed_with_exception' }),
    ]
    const lane = lanes(entries, [finding(['DT-1'])]).findings

    expect(`${lane.value} ${lane.caption}`).toBe('1 of 2 exception tests written up')
    expect(lane.actions).toEqual([{
      key: 'draft_findings', label: 'Draft findings (1)', tone: 'warn',
      ids: ['DT-2'], needsAgent: true,
    }])
  })

  it('rests once every exception test is written up', () => {
    const lane = lanes(
      [item('DT-1', 'I-1', { classification: 'exception', test_status: 'completed_with_exception' })],
      [finding(['DT-1'])],
    ).findings

    expect(lane.state).toBe('done')
    expect(lane.rest).toBe('Every exception is written up')
  })
})

describe('document test disclosures', () => {
  it('reports agent conclusions and open evidence requests, but not selection breadth', () => {
    const cycle = {
      entry_type: 'cycle_test', test_id: 'DT-CYCLE', title: 'Cycle', test_kind: 'cycle_vouch',
      test_status: 'completed_with_exception', rcm_id: 'RCM-1', classification: 'needs_review',
      conclusion_state: 'agent', item_count: 1, tested_item_count: 1,
      evaluation_counts: {}, disposition_counts: {}, assertion_columns: 1,
      assertion_counts: {}, coverage: {}, selection_basis: 'evidence_linked',
      assurance_scope: 'targeted_evidence_only', assurance_label: 'Targeted',
      requirement_refs: [], updated: '',
    } as unknown as DocTestSummaryEntry

    const model = docTestStatus(payload([
      item('DT-1', 'I-1', { evidence_request_count: 2 }),
      cycle,
    ]))

    expect(model.disclosures.map(entry => entry.key)).toEqual(['agent', 'evidence'])
    expect(model.disclosures[0].message).toContain('1 of 2 test conclusions was set by the agent')
  })

  it('says nothing when there is nothing to qualify', () => {
    expect(docTestStatus(payload([item('DT-1', 'I-1')])).disclosures).toEqual([])
  })
})

describe('document test filters', () => {
  const entries = [
    item('DT-1', 'I-1'),
    item('DT-2', 'I-2', { classification: 'exception', test_status: 'completed_with_exception' }),
    item('DT-3', 'I-3', { classification: 'not_run', conclusion_state: 'none' }),
    item('DT-4', 'I-4', { conclusion_state: 'agent', evidence_request_count: 1 }),
  ]
  const ids = (filter: Parameters<typeof filterDocTestEntries>[1]) =>
    filterDocTestEntries(entries, filter, [finding(['DT-2'])])
      .map(entry => (entry.entry_type === 'item' ? entry.item_id : entry.test_id))

  it('returns every entry when nothing is filtered', () => {
    expect(ids(null)).toEqual(['I-1', 'I-2', 'I-3', 'I-4'])
  })

  it('selects the entries each count stands for', () => {
    expect(ids('not_run')).toEqual(['I-3'])
    expect(ids('exceptions')).toEqual(['I-2'])
    expect(ids('no_conclusion')).toEqual(['I-3'])
    expect(ids('agent_concluded')).toEqual(['I-4'])
    expect(ids('evidence_request')).toEqual(['I-4'])
    expect(ids('has_finding')).toEqual(['I-2'])
  })

  it('selects every item of a test when the filter is test-grain', () => {
    const many = [
      item('DT-9', 'I-A', { conclusion_state: 'agent' }),
      item('DT-9', 'I-B', { conclusion_state: 'agent' }),
      item('DT-8', 'I-C'),
    ]
    expect(filterDocTestEntries(many, 'agent_concluded')).toHaveLength(2)
  })
})

describe('the call nobody has recorded', () => {
  const entries = [
    item('DT-1', 'I-1', { disposition: { state: 'confirmed', stale: false } } as never),
    item('DT-2', 'I-2', { disposition: { state: 'pending', stale: false } } as never),
    // A call made against evidence that has since moved stands on the record
    // but not as a current one, so it is owed again.
    item('DT-3', 'I-3', { disposition: { state: 'confirmed', stale: true } } as never),
  ]

  it('counts and selects the items no auditor has answered for', () => {
    const options = docTestStatus(payload(entries)).filters
      ?.find(group => group.key === 'call')?.options ?? []

    expect(options[0]).toMatchObject({ key: 'no_call', value: 2 })
    expect(filterDocTestEntries(entries, 'no_call').map(entry =>
      (entry.entry_type === 'item' ? entry.item_id : entry.test_id))).toEqual(['I-2', 'I-3'])
  })

  it('names a chip for a filter the page actually counts', () => {
    const groups = docTestStatus(payload(entries)).filters ?? []
    const known = new Set(groups.flatMap(group => group.options.map(option => option.key)))

    for (const chip of DOC_TEST_CHIPS) expect(known.has(chip.filter)).toBe(true)
    // Five, plus the `All` chip the bar draws itself, is the six-chip cap.
    expect(DOC_TEST_CHIPS).toHaveLength(5)
  })
})

describe('docTestHeadline', () => {
  it('answers how much there is, how much ran, and how much is open', () => {
    expect(docTestHeadline(null)).toBe('no items yet')
    expect(docTestHeadline(payload([item('DT-1', 'I-1'), item('DT-1', 'I-2')])))
      .toBe('2 items · all run · no exceptions open')
    expect(docTestHeadline(payload([
      item('DT-1', 'I-1', { classification: 'exception' }),
      item('DT-2', 'I-2', { classification: 'not_run' }),
    ]))).toBe('2 items · 1 of 2 run · 1 exception open')
  })
})
