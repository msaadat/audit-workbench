import { describe, expect, it } from 'vitest'

import type { EngagementPhase, EngagementSection } from '../../types'
import { engagementStatus } from './engagementStatus'

function phase(
  id: EngagementPhase['id'],
  state: EngagementPhase['state'],
  counts: Record<string, number> = {},
  overrides: Partial<EngagementPhase> = {},
): EngagementPhase {
  return {
    id,
    label: id[0].toUpperCase() + id.slice(1),
    state,
    complete: state === 'complete',
    summary: '',
    counts,
    issues: [],
    target: { tab: id === 'fieldwork' ? 'doc-tests' : id, query: {} },
    sub: [],
    ...overrides,
  } as EngagementPhase
}

function section(
  id: string,
  state: EngagementSection['state'],
  overrides: Partial<EngagementSection> = {},
): EngagementSection {
  return { id, label: id, state, complete: state === 'complete', issues: [], ...overrides }
}

const rows = (phases: EngagementPhase[], sections?: Record<string, EngagementSection>) =>
  Object.fromEntries(engagementStatus(phases, sections).rows.map(row => [row.id, row]))

/** The live procurement file: everything gated complete. */
const live = () => [
  phase('planning', 'complete', { rcm_rows: 27, tests: 60 }, {
    sub: [
      { id: 'eda', label: 'EDA', state: 'complete', complete: true, target: { tab: 'analysis', query: {} } },
      { id: 'apm', label: 'APM', state: 'complete', complete: true, target: { tab: 'apm', query: {} } },
      { id: 'rcm', label: 'RCM', state: 'complete', complete: true, target: { tab: 'rcm', query: {} } },
    ],
  }),
  phase('fieldwork', 'complete', {
    data_tests: 39, document_tests: 21, exception_observations: 68,
    tests_linked: 60, tests_concluded: 60, unreviewed_agent_conclusions: 41,
  }),
  phase('report', 'complete', { findings: 35, quality_errors: 0, findings_awaiting_followup: 35 }),
]

describe('engagement position', () => {
  it('names the phase the file is sitting on, and how far along it is', () => {
    const model = engagementStatus([
      phase('planning', 'complete'), phase('fieldwork', 'in_progress'), phase('report', 'not_started'),
    ])
    expect(model.position).toBe('Fieldwork · 2 of 3')
    expect(model.arc).toEqual(['complete', 'in_progress', 'not_started'])
  })

  it('says the file is ready once every gate has passed', () => {
    expect(engagementStatus(live()).position).toBe('Ready for review')
  })

  it('says nothing has begun when nothing has', () => {
    const model = engagementStatus([
      phase('planning', 'not_started'), phase('fieldwork', 'not_started'), phase('report', 'not_started'),
    ])
    expect(model.position).toBe('Not started')
  })

  it('reports no position at all without phases, rather than an empty arc', () => {
    const model = engagementStatus([])
    expect(model.position).toBe('')
    expect(model.rows).toEqual([])
  })
})

describe('what each row draws', () => {
  it('rests finished phases, opens the one in flight, and holds the rest back', () => {
    const model = rows([
      phase('planning', 'complete'), phase('fieldwork', 'attention'), phase('report', 'not_started'),
    ])
    expect(model.planning.display).toBe('collapsed')
    expect(model.fieldwork.display).toBe('open')
    expect(model.report.display).toBe('pending')
  })

  it('opens the first phase while the file is still empty', () => {
    const model = rows([
      phase('planning', 'not_started'), phase('fieldwork', 'not_started'), phase('report', 'not_started'),
    ])
    expect(model.planning.display).toBe('open')
    // Nothing can be planned before there is anything to plan against.
    expect(model.planning.actions).toEqual([{ key: 'import', label: 'Import a folder', tone: 'primary' }])
  })

  it('keeps a resting phase carrying its totals and its sub-phases', () => {
    const model = rows(live()).planning
    expect(model.tail).toBe('27 rows · 60 tests')
    expect(model.chips.map(chip => [chip.label, chip.tone])).toEqual([
      ['EDA', 'ok'], ['APM', 'ok'], ['RCM', 'ok'],
    ])
    // Planning is a checklist, not a population; three chips beat a fraction.
    expect(model.figure).toBe('')
    expect(model.actions).toEqual([])
  })

  it('leads fieldwork with the same denominator the RCM bar uses', () => {
    const model = rows(live()).fieldwork
    expect(`${model.figure} ${model.caption}`).toBe('60 / 60 tests concluded')
    expect(model.tail).toBe('60 / 60 concluded')
    expect(model.segments.map(segment => Math.round(segment.portion))).toEqual([100, 0])
  })

  it('gives fieldwork a chip per section, counted by the backend', () => {
    const model = rows(live(), {
      'data-tests': section('data-tests', 'complete', { counts: { total: 39, concluded: 36 } }),
      'doc-tests': section('doc-tests', 'attention', { counts: { total: 21, concluded: 11 } }),
    }).fieldwork
    expect(model.chips.map(chip => `${chip.detail} ${chip.label}`))
      .toEqual(['36/39 Data', '11/21 Docs', '68 exceptions'])
    expect(model.chips.map(chip => chip.tone)).toEqual(['ok', 'bad', 'neutral'])
  })

  it('leaves out a section that has no tests rather than drawing 0/0', () => {
    const model = rows(live(), {
      'data-tests': section('data-tests', 'not_started', { counts: { total: 0, concluded: 0 } }),
      'doc-tests': section('doc-tests', 'complete', { counts: { total: 21, concluded: 21 } }),
    }).fieldwork
    expect(model.chips.map(chip => chip.label)).toEqual(['Docs', 'exceptions'])
  })

  it('says what fieldwork is waiting for when nothing has been planned', () => {
    const model = rows([phase('planning', 'complete'), phase('fieldwork', 'not_started')]).fieldwork
    expect(model.figure).toBe('')
    expect(model.caption).toBe('No tests have been planned yet.')
  })

  it('keeps the report row on its counts', () => {
    expect(rows(live()).report.tail).toBe('35 findings · 0 errors')
    expect(rows([phase('report', 'attention', { findings: 4, quality_errors: 2 })]).report.tail)
      .toBe('4 findings · 2 errors')
  })
})

describe('rail actions', () => {
  const sections = (overrides: Partial<EngagementSection>) => ({
    'data-tests': section('data-tests', 'attention', {
      counts: { total: 39, concluded: 36 }, ...overrides,
    }),
  })

  it('scopes the run to the tests that have never run', () => {
    const model = rows(live(), sections({ unrun_test_ids: ['DAT-1', 'DAT-2'] })).fieldwork
    expect(model.actions).toEqual([{
      key: 'run_data_tests', label: 'Run 2 tests', tone: 'primary', ids: ['DAT-1', 'DAT-2'],
    }])
  })

  it('treats a stale result as its own action, not as unrun', () => {
    const model = rows(live(), sections({
      unrun_test_ids: ['DAT-1'], stale_test_ids: ['DAT-9'],
    })).fieldwork
    expect(model.actions.map(action => [action.key, action.label, action.tone])).toEqual([
      ['run_data_tests', 'Run 1 test', 'primary'],
      ['rerun_stale', 'Re-run 1 stale test', 'ghost'],
    ])
  })

  it('offers nothing once every data test carries a current result', () => {
    expect(rows(live(), sections({})).fieldwork.actions).toEqual([])
  })
})

describe('disclosures', () => {
  it('names what the phase gates deliberately do not cover', () => {
    const model = engagementStatus(live())
    expect(model.disclosures.map(item => item.message)).toEqual([
      '35 of 35 findings have no root cause or management response.',
      '41 of 60 conclusions were set by the assistant and never read.',
    ])
    // A disclosure points at the page that can settle it.
    expect(model.disclosures.map(item => item.target.tab)).toEqual(['findings', 'rcm'])
  })

  it('agrees in number with itself when only one finding is owed', () => {
    const model = engagementStatus([
      phase('report', 'complete', { findings: 1, quality_errors: 0, findings_awaiting_followup: 1 }),
    ])
    expect(model.disclosures[0].message).toBe('1 of 1 finding has no root cause or management response.')
  })

  it('says nothing when there is nothing to qualify', () => {
    const model = engagementStatus([
      phase('fieldwork', 'complete', { tests_linked: 60, tests_concluded: 60, unreviewed_agent_conclusions: 0 }),
      phase('report', 'complete', { findings: 35, findings_awaiting_followup: 0 }),
    ])
    expect(model.disclosures).toEqual([])
  })
})
