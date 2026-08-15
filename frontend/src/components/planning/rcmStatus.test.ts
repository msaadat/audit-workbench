import { describe, expect, it } from 'vitest'

import type { FindingRollups, RcmCompletion, RcmExecutionRollup, RcmRow, TestRollup } from '../../types'
import { filterRows, rcmStatus } from './rcmStatus'

function test(overrides: Partial<TestRollup> & Pick<TestRollup, 'test_id'>): TestRollup {
  return {
    kind: 'datatest', title: 'Test', executed_count: 0, exception_count: 0,
    open_exception_count: 0, evidence_count: 0, status: 'completed_no_exception',
    result_summary: '', conclusion: '', control_conclusion: 'no_conclusion',
    scope_limitations: '', finding_refs: [], ...overrides,
  } as TestRollup
}

function row(id: string, rollup: Partial<RcmExecutionRollup>, overrides: Partial<RcmRow> = {}): RcmRow {
  const tests = rollup.test_rollups ?? []
  return {
    id, semantic_id: id, created_by: 'agent', agent_run_id: null,
    process: 'Process', risk: 'Risk', risk_rating: 'high', business_cycle: '',
    control_attributes: [], control: '', control_type: '', control_owner: '',
    criteria: '', criteria_refs: [], test_refs: tests.map(item => item.test_id),
    execution_rollup: {
      tests: tests.length,
      completed: tests.filter(item => item.status.startsWith('completed')).length,
      exceptions: 0,
      test_rollups: tests,
      ...rollup,
    },
    finding_refs: [], evidence_refs: [], prepared_by: null, reviewed_by: null,
    review_status: 'draft', updated: '',
    ...overrides,
  } as RcmRow
}

function completionWith(overrides: Partial<RcmCompletion>): RcmCompletion {
  return {
    status: 'completed_with_open_items',
    coverage: {
      ok: true, issue_count: 0, rows_without_tests: [], unspecified_tests: [],
      invalid_test_parents: [], high_risks_without_executable_work: [],
      completed_without_durable_result: [], inconsistent_conclusions: [],
    },
    incomplete_outcomes: [], blank_conclusions: [], unreviewed_agent_conclusions: [],
    blocked_without_plan: [], rcm_without_conclusion: [], assurance_gaps: [],
    evidence_ceilings: [], pending_cycle_dispositions: [], untested_columns: [],
    undispositioned_analyses: 0, declined_analyses: 0,
    ...overrides,
  }
}

const lanes = (status: ReturnType<typeof rcmStatus>) =>
  Object.fromEntries(status.lanes.map(lane => [lane.key, lane]))

describe('execution lane', () => {
  it('reports uncovered risks rather than a run rate when nothing is linked', () => {
    const rows = [row('R1', {}), row('R2', {}, { risk_rating: 'low' })]
    const lane = lanes(rcmStatus(rows)).execution

    expect(lane.state).toBe('alarm')
    expect(`${lane.value} ${lane.caption}`).toBe('0 of 2 risks have a test')
    // The severe count is the part worth alarming about, and R2 is not it.
    expect(lane.chips[0].label).toBe('1 critical or high')
    expect(lane.actions[0]).toMatchObject({ key: 'generate_tests', ids: ['R1', 'R2'] })
  })

  it('scopes each run action to the tests that have not run, by kind', () => {
    const rows = [row('R1', {
      test_rollups: [
        test({ test_id: 'DAT-1', status: 'completed_no_exception' }),
        test({ test_id: 'DAT-2', status: 'draft' }),
        test({ test_id: 'DOC-1', kind: 'doctest', status: 'ready' }),
      ],
    })]
    const lane = lanes(rcmStatus(rows)).execution

    expect(lane.state).toBe('gap')
    expect(`${lane.value} ${lane.caption}`).toBe('1 of 3 tests run')
    expect(lane.actions).toEqual([
      { key: 'run_data_tests', label: 'Run 1 Data Test', tone: 'primary', ids: ['DAT-2'] },
      {
        key: 'run_document_tests', label: 'Run 1 Document Test', tone: 'ghost',
        ids: ['DOC-1'], needsAgent: true,
      },
    ])
  })

  it('keeps blocked and awaiting-review work out of the run action', () => {
    const rows = [row('R1', {
      test_rollups: [
        test({ test_id: 'DAT-1', status: 'blocked' }),
        test({ test_id: 'DOC-1', kind: 'doctest', status: 'review_required' }),
      ],
    })]
    const lane = lanes(rcmStatus(rows)).execution

    // Neither is served by running it again: one needs evidence, one needs a
    // person. Both are still counted, so the lane cannot read as finished.
    expect(lane.actions).toEqual([])
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 blocked', '1 awaiting review'])
    expect(lane.state).toBe('alarm')
  })

  it('collapses to a resting state once every linked test has run', () => {
    const rows = [
      row('R1', { exceptions: 3, test_rollups: [test({ test_id: 'DAT-1' })] }),
      row('R2', { exceptions: 0, test_rollups: [test({ test_id: 'DAT-2' })] }),
    ]
    const lane = lanes(rcmStatus(rows)).execution

    expect(lane.state).toBe('done')
    expect(lane.actions).toEqual([])
    expect(lane.rest).toBe('All linked work has run')
    expect(lane.chips.map(chip => chip.label)).toEqual(['1 passed', '1 with exceptions'])
  })
})

describe('conclusion lane', () => {
  const rows = [
    row('R1', { control_conclusion: 'effective', test_rollups: [test({ test_id: 'T1' })] }),
    row('R2', { control_conclusion: 'ineffective', test_rollups: [test({ test_id: 'T2' })] }),
    row('R3', { control_conclusion: 'no_conclusion', test_rollups: [test({ test_id: 'T3' })] }),
  ]

  it('counts only conclusions that close the row', () => {
    const lane = lanes(rcmStatus(rows)).conclusion
    expect(`${lane.value} ${lane.caption}`).toBe('2 of 3 controls concluded')
    expect(lane.state).toBe('gap')
    expect(lane.actions).toEqual([{ key: 'refresh_rollup', label: 'Refresh roll-up', tone: 'ghost' }])
  })

  it('carries the mix in the meter, not just the total', () => {
    const lane = lanes(rcmStatus(rows)).conclusion
    // "All concluded" and "all concluded ineffective" are the same number.
    expect(lane.segments.map(segment => Math.round(segment.portion))).toEqual([33, 0, 33, 0])
  })

  it('rests once no row is missing a conclusion', () => {
    const lane = lanes(rcmStatus(rows.slice(0, 2))).conclusion
    expect(lane.state).toBe('done')
    expect(lane.rest).toBe('Every row carries a conclusion')
  })
})

describe('findings lane', () => {
  const rollups: FindingRollups = {
    by_rcm: {
      R2: [{ id: 'F-1', title: 'One', severity: 'critical' }],
      R3: [{ id: 'F-1', title: 'One', severity: 'critical' }],
    },
    by_test: {}, by_procedure: {},
  }

  it('measures coverage against adverse rows, not every row', () => {
    const rows = [
      row('R1', { control_conclusion: 'effective', test_rollups: [test({ test_id: 'T1' })] }),
      row('R2', { control_conclusion: 'ineffective', test_rollups: [test({ test_id: 'T2' })] }),
      row('R3', { control_conclusion: 'partially_effective', test_rollups: [test({ test_id: 'T3' })] }),
    ]
    const lane = lanes(rcmStatus(rows, rollups)).findings

    // R1 concluded effective is not a row missing a finding.
    expect(lane.state).toBe('done')
    expect(lane.rest).toBe('Every adverse control is written up')
    // One finding written against two rows is one finding.
    expect(lane.value).toBe('1')
  })

  it('asks for the gap when an adverse row has nothing written up', () => {
    const rows = [
      row('R2', { control_conclusion: 'ineffective', test_rollups: [test({ test_id: 'T2' })] }),
      row('R9', { control_conclusion: 'ineffective', test_rollups: [test({ test_id: 'T9' })] }),
    ]
    const lane = lanes(rcmStatus(rows, rollups)).findings

    expect(lane.state).toBe('alarm')
    expect(`${lane.value} ${lane.caption}`).toBe('1 of 2 adverse controls have a finding')
    expect(lane.actions).toEqual([
      { key: 'draft_findings', label: 'Draft 1 finding', tone: 'warn', needsAgent: true },
    ])
  })

  it('says nothing is owed before any conclusion is adverse', () => {
    const lane = lanes(rcmStatus([row('R1', { control_conclusion: 'effective' })])).findings
    expect(lane.state).toBe('idle')
    expect(lane.rest).toBe('Findings follow conclusions')
  })
})

describe('disclosures', () => {
  it('reports agent conclusions nobody has read', () => {
    const rows = [row('R1', { test_rollups: [test({ test_id: 'T1' }), test({ test_id: 'T2' })] })]
    const completion = completionWith({
      unreviewed_agent_conclusions: [{ rcm_id: 'R1', test_id: 'T1' }],
    })
    const [first] = rcmStatus(rows, undefined, completion).disclosures

    expect(first.mark).toBe('Agent')
    expect(first.message).toBe('1 of 2 test conclusions was set by the agent and never read by a person.')
  })

  it('discloses a ceiling the auditor concluded over, which the gate omits', () => {
    const rows = [row('R1', {
      control_conclusion: 'effective',
      evidence_ceiling: 'Design inquiry cannot establish population compliance.',
      evidence_ceiling_applied: false,
      test_rollups: [test({ test_id: 'T1' })],
    })]
    // The backend drops an auditor-owned ceiling from `evidence_ceilings`
    // because it is no longer something to resolve. It is still a disclosure.
    const status = rcmStatus(rows, undefined, completionWith({ evidence_ceilings: [] }))

    expect(status.disclosures.map(item => item.key)).toContain('limit')
  })

  it('holds the sign-off line back until there is executed work to sign off', () => {
    const untested = rcmStatus([row('R1', {})])
    expect(untested.disclosures.map(item => item.key)).not.toContain('review')

    const executed = rcmStatus([row('R1', { test_rollups: [test({ test_id: 'T1' })] })])
    expect(executed.disclosures.map(item => item.key)).toContain('review')
  })
})

describe('row filters', () => {
  const rows = [
    row('R1', {
      control_conclusion: 'effective', exceptions: 0,
      evidence_ceiling: 'Design inquiry only.',
      test_rollups: [test({ test_id: 'T1' })],
    }),
    row('R2', {
      control_conclusion: 'ineffective', exceptions: 4,
      test_rollups: [test({ test_id: 'T2', status: 'completed_with_exception' })],
    }),
    row('R3', { control_conclusion: 'no_conclusion', test_rollups: [test({ test_id: 'T3', status: 'draft' })] }),
    row('R4', {}, { review_status: 'reviewed' }),
  ]
  const rollups: FindingRollups = {
    by_rcm: { R2: [{ id: 'F-1', title: 'One', severity: 'high' }] },
    by_test: {}, by_procedure: {},
  }
  const completion = completionWith({
    unreviewed_agent_conclusions: [{ rcm_id: 'R2', test_id: 'T2' }],
  })
  const ids = (filter: Parameters<typeof filterRows>[1]) =>
    filterRows(rows, filter, rollups, completion).map(item => item.id)

  it('returns every row when nothing is filtered', () => {
    expect(ids(null)).toEqual(['R1', 'R2', 'R3', 'R4'])
  })

  it('selects the rows each lane counted', () => {
    expect(ids('no_test')).toEqual(['R4'])
    expect(ids('not_run')).toEqual(['R3'])
    expect(ids('passed')).toEqual(['R1'])
    expect(ids('with_exceptions')).toEqual(['R2'])
    expect(ids('effective')).toEqual(['R1'])
    expect(ids('no_conclusion')).toEqual(['R3', 'R4'])
  })

  it('selects the rows each disclosure counted', () => {
    expect(ids('agent_concluded')).toEqual(['R2'])
    expect(ids('evidence_limit')).toEqual(['R1'])
    expect(ids('unreviewed_row')).toEqual(['R1', 'R2', 'R3'])
  })

  it('treats an effective row without a finding as covered, not missing one', () => {
    expect(ids('has_finding')).toEqual(['R2'])
    expect(ids('missing_finding')).toEqual([])
  })
})
