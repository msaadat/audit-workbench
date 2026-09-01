import { describe, expect, it } from 'vitest'

import type { AuditFinding, DataTest } from '../../types'
import { dataTestStatus, filterDataTests } from './dataTestStatus'

function test(id: string, overrides: Partial<DataTest> = {}): DataTest {
  return {
    id, semantic_id: id, rcm_id: 'RCM-1', title: id, objective: '',
    engine: 'analytics', table_refs: [], steps: [], spec: {},
    status: 'completed_no_exception', semantic_warnings: [],
    evaluation: { state: 'passed' }, exception_dispositions: [],
    conclusion_source: 'agent', control_conclusion: 'effective',
    control_conclusion_source: 'agent', control_conclusion_input_sha1: null,
    control_conclusion_stale: false, result_stale: false, semantic_review: null,
    last_run: { id: 'run-1' }, evidence_refs: [], finding_refs: [],
    exception_count: 0, open_exception_count: 0,
    result_summary: '', conclusion: '', scope_limitations: '', next_action: '',
    created_by: 'agent', agent_run_id: null, created: '', updated: '',
    ...overrides,
  } as DataTest
}

function finding(testRefs: string[]): AuditFinding {
  return { id: 'F-1', test_refs: testRefs } as AuditFinding
}

const lanes = (tests: DataTest[], findings: AuditFinding[] = []) =>
  Object.fromEntries(dataTestStatus(tests, findings).lanes.map(lane => [lane.key, lane]))

describe('data test execution lane', () => {
  it('scopes the run to the tests that have never run', () => {
    const lane = lanes([
      test('DAT-1'),
      test('DAT-2', { last_run: null, status: 'ready' }),
      test('DAT-3', { last_run: null, status: 'blocked' }),
    ]).execution

    expect(`${lane.value} ${lane.caption}`).toBe('1 of 3 tests run')
    // Blocked work needs something else first; running it again does nothing.
    expect(lane.actions).toEqual([{
      key: 'run_tests', label: 'Run 1 test', tone: 'primary', ids: ['DAT-2'],
    }])
    expect(lane.state).toBe('alarm')
  })

  it('treats a stale result as its own outstanding state, not as unrun', () => {
    const lane = lanes([test('DAT-1'), test('DAT-2', { result_stale: true })]).execution

    expect(`${lane.value} ${lane.caption}`).toBe('2 of 2 tests run')
    expect(lane.state).toBe('alarm')
    // The distribution is no longer withheld until the lane settles — the
    // outcome chip row that used to carry it above the list is gone.
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 stale', '2 no exception'])
    expect(lane.actions).toEqual([{
      key: 'rerun_stale', label: 'Re-run 1 stale test', tone: 'primary', ids: ['DAT-2'],
    }])
  })

  it('rests once every test carries a current result', () => {
    const lane = lanes([
      test('DAT-1'),
      test('DAT-2', { status: 'completed_with_exception', exception_count: 4 }),
    ]).execution

    expect(lane.state).toBe('done')
    expect(lane.actions).toEqual([])
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 no exception', '1 with exceptions'])
    expect(lane.rest).toBe('Every test has a current result')
  })
})

describe('data test conclusion lane', () => {
  it('counts conclusions and carries the mix, without offering a batch', () => {
    const lane = lanes([
      test('DAT-1', { control_conclusion: 'effective' }),
      test('DAT-2', { control_conclusion: 'ineffective' }),
      test('DAT-3', { control_conclusion: 'no_conclusion' }),
    ]).conclusion

    expect(`${lane.value} ${lane.caption}`).toBe('2 of 3 tests concluded')
    expect(lane.state).toBe('gap')
    expect(lane.actions).toEqual([])
    expect(lane.segments.map(segment => Math.round(segment.portion))).toEqual([33, 0, 33, 0])
  })

  it('raises a conclusion reached against evidence that has since moved', () => {
    const lane = lanes([test('DAT-1', { control_conclusion_stale: true })]).conclusion

    expect(lane.state).toBe('alarm')
    expect(lane.chips.map(chip => chip.label)).toContain('1 stale')
  })
})

describe('data test findings lane', () => {
  it('measures write-up against exception tests only', () => {
    const tests = [
      test('DAT-1'),
      test('DAT-2', { status: 'completed_with_exception' }),
      test('DAT-3', { status: 'completed_with_exception' }),
    ]
    const lane = lanes(tests, [finding(['DAT-2'])]).findings

    // A test with no exception is not a test missing a finding.
    expect(`${lane.value} ${lane.caption}`).toBe('1 of 2 exception tests written up')
    expect(lane.actions).toEqual([{
      key: 'draft_findings', label: 'Draft findings (1)', tone: 'warn',
      ids: ['DAT-3'], needsAgent: true,
    }])
  })

  it('leaves an exploratory exception out of the count and says why', () => {
    // Counting it would leave the lane short of a write-up that can never
    // exist, because drafting is per RCM row and this test has none.
    const lane = lanes([
      test('DAT-9', { status: 'completed_with_exception', rcm_id: null }),
    ]).findings

    expect(lane.state).toBe('idle')
    expect(lane.actions).toEqual([])
    expect(lane.rest)
      .toBe('Exceptions were found only by 1 exploratory test, which support no RCM row')
  })
})

describe('data test disclosures', () => {
  it('reports who reached the conclusions, and what supports no RCM row', () => {
    const model = dataTestStatus([
      test('DAT-1'),
      test('DAT-2', { control_conclusion_source: 'auditor' }),
      test('DAT-3', { rcm_id: null, control_conclusion: 'no_conclusion' }),
      test('DAT-4', { semantic_warnings: ['Counts rows, not records.'] }),
    ])

    expect(model.disclosures.map(item => item.key)).toEqual(['agent', 'exploratory', 'semantic'])
    expect(model.disclosures[0].message).toContain('2 of 3 conclusions were set by the agent')
  })

  it('counts a run the runner could not vouch for as a warning, not a block', () => {
    // The warning no longer shows up as a status, so this lane is the only
    // place the page says the run carried one.
    const model = dataTestStatus([
      test('DAT-1'),
      test('DAT-2', { last_run: { id: 'run-2', semantic_valid: false } } as Partial<DataTest>),
    ])

    const warned = model.disclosures.find(item => item.key === 'semantic')
    expect(warned?.message).toContain('1 test')
    expect(filterDataTests([test('DAT-1'), test('DAT-2', {
      last_run: { id: 'run-2', semantic_valid: false },
    } as Partial<DataTest>)], 'semantic_warning').map(item => item.id)).toEqual(['DAT-2'])
  })

  it('does not call an unconcluded test an agent conclusion', () => {
    const model = dataTestStatus([test('DAT-1', { control_conclusion: 'no_conclusion' })])
    expect(model.disclosures.map(item => item.key)).not.toContain('agent')
  })
})

describe('data test filters', () => {
  const tests = [
    test('DAT-1'),
    test('DAT-2', { status: 'completed_with_exception', control_conclusion: 'ineffective' }),
    test('DAT-3', { last_run: null, status: 'ready', control_conclusion: 'no_conclusion' }),
    test('DAT-4', { result_stale: true, rcm_id: null }),
  ]
  const findings = [finding(['DAT-2'])]
  const ids = (filter: Parameters<typeof filterDataTests>[1]) =>
    filterDataTests(tests, filter, findings).map(item => item.id)

  it('returns every test when nothing is filtered', () => {
    expect(ids(null)).toEqual(['DAT-1', 'DAT-2', 'DAT-3', 'DAT-4'])
  })

  it('selects the tests each count stands for', () => {
    expect(ids('not_run')).toEqual(['DAT-3'])
    expect(ids('stale_result')).toEqual(['DAT-4'])
    expect(ids('with_exceptions')).toEqual(['DAT-2'])
    expect(ids('no_conclusion')).toEqual(['DAT-3'])
    expect(ids('exploratory')).toEqual(['DAT-4'])
    expect(ids('has_finding')).toEqual(['DAT-2'])
    expect(ids('missing_finding')).toEqual([])
  })
})
