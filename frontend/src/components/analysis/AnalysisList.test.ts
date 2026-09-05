import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import AnalysisList from './AnalysisList.vue'
import type { AnalysisLastResult, SavedAnalysis } from '../../types'

/**
 * The row was the title clamped to two lines and a status glyph whose only
 * label was a tooltip, so finding the procedure that failed meant hovering
 * thirty of them. It carries what a data test's row carries now, plus the one
 * state a data test does not have.
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

const failing: Partial<AnalysisLastResult> = {
  exception_count: 3, population: 52, tested: 52, verdict: 'fail',
}

function render(items: SavedAnalysis[], selectedId: string | null = null) {
  return mount(AnalysisList, {
    props: { items, selectedId },
    global: { directives: { tooltip: {} } },
  })
}

describe('AnalysisList', () => {
  it('leads the meta line with what it found, not with the frame', () => {
    // A joined frame here is `invoice_data_po_data_joined` — 27 characters
    // that would push the ranking fact off the end of a 300px column.
    const wrapper = render([analysis({
      classification: 'exception',
      last_result: failing as AnalysisLastResult,
    })])

    expect(wrapper.find('.meta').text())
      .toBe('3 of 52 failed · invoice_data_po_data_joined')
    expect(wrapper.find('.dot').attributes('data-tone')).toBe('bad')
  })

  /**
   * The regression this list exists for: a stale exception is still an
   * exception. The rail used to render it amber and call it "Rerun required",
   * and the three failures were invisible on the page.
   */
  it('shows a stale result as what it found, and marks it separately', () => {
    const wrapper = render([analysis({
      state: 'stale',
      classification: 'exception',
      last_result: failing as AnalysisLastResult,
    })])

    expect(wrapper.find('.dot').attributes('data-tone')).toBe('bad')
    expect(wrapper.find('.meta').text()).toContain('3 of 52 failed')
    expect(wrapper.find('.mark').attributes('aria-label')).toBe('Rerun required')
  })

  it('leaves a current result unmarked, because standing is the ordinary case', () => {
    const wrapper = render([analysis({ last_result: { exception_count: 0 } as AnalysisLastResult })])
    expect(wrapper.find('.mark').exists()).toBe(false)
    expect(wrapper.find('.dot').attributes('data-tone')).toBe('ok')
  })

  it('marks the open procedure and says so to a screen reader', () => {
    const wrapper = render([analysis(), analysis({ id: 'A-2', title: 'Second' })], 'A-2')
    const rows = wrapper.findAll('.row')

    expect(rows[1].classes()).toContain('active')
    expect(rows[1].attributes('aria-selected')).toBe('true')
    expect(rows[0].attributes('aria-selected')).toBe('false')
  })

  it('says a narrowing matched nothing rather than showing an empty column', () => {
    expect(render([]).find('.empty').text()).toBe('No procedure matches this view.')
  })
})
