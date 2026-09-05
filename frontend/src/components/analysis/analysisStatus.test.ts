import { describe, expect, it } from 'vitest'

import {
  ANALYSIS_CHIPS, analysisStatus, classificationTone, foundSummary, holdsExceptions,
  matchesFilter, visibleAnalyses,
} from './analysisStatus'
import type { AnalysisFilter } from './analysisStatus'
import type { AnalysisLastResult, SavedAnalysis } from '../../types'

/**
 * The two axes this page is for.
 *
 * What a procedure found and whether that still stands were one field. All
 * twenty-three procedures in the engagement this was found in were stale, so
 * the page reported "23 rerun required, 0 exceptions" over sixteen recorded
 * exceptions — including the largest monetary exposure in the population. And
 * the answer to "and then what" was durable on every record and shown nowhere.
 */

function analysis(overrides: Partial<SavedAnalysis> = {}): SavedAnalysis {
  return {
    id: 'A-1', title: 'Invoiced amount exceeds the linked PO total',
    kind: 'analytics', table: 'invoice_data_po_data_joined', note: '',
    viz: {} as SavedAnalysis['viz'], source: 'ai', created: '',
    state: 'current', classification: 'clear',
    ...overrides,
  } as SavedAnalysis
}

function result(overrides: Partial<AnalysisLastResult> = {}): AnalysisLastResult {
  return {
    run_id: 'run-1', executed_at: '2026-09-01T17:16:12+00:00', status: 'ok', error: null,
    verdict: 'fail', verdict_text: '', row_count: 3, column_count: 3, stat_count: 0, stats: [],
    exception_count: 3, exception_rows_retained: 3,
    population: 52, tested: 52, exception_rate: 0.0577,
    ...overrides,
  } as AnalysisLastResult
}

describe('what a procedure found, and whether that still stands', () => {
  const stale = analysis({
    id: 'A-STALE', state: 'stale', classification: 'exception', last_result: result(),
  })

  it('keeps the verdict of a result that is no longer current', () => {
    expect(classificationTone(stale.classification)).toBe('bad')
    expect(foundSummary(stale)).toBe('3 of 52 failed')
    // Both are true of the same procedure, and neither answers the other.
    expect(matchesFilter(stale, 'exception')).toBe(true)
    expect(matchesFilter(stale, 'stale')).toBe(true)
  })

  it('counts the exception and the rerun separately', () => {
    const model = analysisStatus([stale, analysis({ id: 'A-CLEAN' })])
    const outcome = model.filters!.find(group => group.key === 'outcome')!
    const freshness = model.filters!.find(group => group.key === 'freshness')!

    expect(outcome.options.find(o => o.key === 'exception')!.value).toBe(1)
    expect(freshness.options.find(o => o.key === 'stale')!.value).toBe(1)
    // The outcome axis still partitions the register.
    expect(outcome.options.reduce((total, o) => total + o.value, 0)).toBe(2)
  })

  it('runs everything with no current result, never-run and stale alike', () => {
    const model = analysisStatus([stale, analysis({ id: 'A-NEW', classification: 'not_run', state: 'not_run' })])
    const run = model.lanes.find(lane => lane.key === 'execution')!

    expect(run.value).toBe('1')
    expect(run.total).toBe('2')
    expect(run.actions[0].ids).toEqual(['A-NEW', 'A-STALE'])
  })

  it('composes narrowings across axes and replaces within one', () => {
    const clean = analysis({ id: 'A-CLEAN', state: 'stale' })
    const list = [stale, clean]

    expect(visibleAnalyses(list, ['stale']).map(item => item.id)).toEqual(['A-STALE', 'A-CLEAN'])
    expect(visibleAnalyses(list, ['stale', 'exception']).map(item => item.id)).toEqual(['A-STALE'])
  })
})

describe('what was done about what it found', () => {
  const owed = analysis({ id: 'A-OWED', classification: 'exception', last_result: result() })
  const carried = analysis({
    id: 'A-CARRIED',
    classification: 'exception',
    last_result: result(),
    promotion: { state: 'promoted', result_sha1: 'x', decided_at: '', test_id: 'DAT-1', rcm_id: 'RCM-1' },
  })
  const declined = analysis({
    id: 'A-DECLINED',
    classification: 'exception',
    last_result: result(),
    promotion: { state: 'declined', result_sha1: 'x', decided_at: '', reason: 'Not a control test.' },
  })

  it('counts only procedures that flagged something', () => {
    // A clean procedure asks nothing of anybody, so counting it as answered
    // would report a decision nobody made.
    const model = analysisStatus([owed, carried, declined, analysis({ id: 'A-CLEAN' })])
    const lane = model.lanes.find(lane => lane.key === 'disposition')!

    expect(lane.value).toBe('2')
    expect(lane.total).toBe('3')
    expect(lane.state).toBe('alarm')
    expect(lane.actions[0].ids).toEqual(['A-OWED'])
  })

  it('is settled when every procedure holding exceptions has an answer', () => {
    const lane = analysisStatus([carried, declined]).lanes.find(l => l.key === 'disposition')!
    expect(lane.state).toBe('done')
    expect(lane.actions).toEqual([])
  })

  it('has nothing to answer for when nothing flagged', () => {
    const lane = analysisStatus([analysis()]).lanes.find(l => l.key === 'disposition')!
    expect(lane.state).toBe('idle')
    expect(holdsExceptions(analysis())).toBe(false)
  })

  it('narrows to the unanswered', () => {
    expect(matchesFilter(owed, 'unanswered')).toBe(true)
    expect(matchesFilter(carried, 'unanswered')).toBe(false)
    expect(matchesFilter(carried, 'promoted')).toBe(true)
    expect(matchesFilter(declined, 'declined')).toBe(true)
  })
})

describe('what the row says it found', () => {
  it.each<[string, Partial<SavedAnalysis>, string]>([
    ['never run', { classification: 'not_run', state: 'not_run' }, 'not run'],
    [
      'a broken definition',
      { classification: 'execution_error', last_result: result({ status: 'error', error: 'boom' }) },
      'could not run',
    ],
    [
      'a clean run over the whole population',
      { last_result: result({ exception_count: 0, verdict: 'ok' }) },
      'no exceptions',
    ],
    [
      'a clean run that could not compare everything',
      { last_result: result({ exception_count: 0, verdict: 'ok', population: 52, tested: 49 }) },
      'no exceptions · 49 of 52 compared',
    ],
    [
      'something worth a look',
      { classification: 'unusual', last_result: result({ exception_count: 1, verdict: 'warn' }) },
      '1 of 52 flagged',
    ],
  ])('says %s', (_name, overrides, expected) => {
    expect(foundSummary(analysis(overrides))).toBe(expected)
  })
})

describe('the permanent chip row', () => {
  it('names filters the page actually offers', () => {
    const keys = new Set(
      analysisStatus([]).filters!.flatMap(group => group.options.map(option => option.key)),
    )
    for (const chip of ANALYSIS_CHIPS) {
      expect(keys.has(chip.filter as AnalysisFilter)).toBe(true)
    }
    // Six is the review bar's cap beside the `All` chip.
    expect(ANALYSIS_CHIPS.length).toBeLessThanOrEqual(6)
  })
})
