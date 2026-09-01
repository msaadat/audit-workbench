import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { DataTest, DataTestResult } from '../../types'
import DataTestResultPanel from './DataTestResultPanel.vue'

function test(overrides: Partial<DataTest> = {}): DataTest {
  return {
    id: 'DAT-1',
    title: 'Approval screen',
    status: 'completed_with_exception',
    exception_count: 4,
    open_exception_count: 4,
    semantic_warnings: [],
    exception_dispositions: [],
    conclusion_source: 'none',
    control_conclusion: 'no_conclusion',
    control_conclusion_source: 'none',
    control_conclusion_input_sha1: null,
    control_conclusion_stale: false,
    result_stale: false,
    semantic_review: null,
    evaluation: {
      state: 'failed',
      note: '4 exception rows.',
      exception_count: 4,
      reasons: [{ label: 'Pending approval', rows: 4, records: 4, columns: [] }],
      suggested_control_conclusion: 'ineffective',
      input_sha1: 'sha1:input',
      ran_at: '2026-08-15T10:00:00+00:00',
    },
    ...overrides,
  } as DataTest
}

function result(overrides: Partial<DataTestResult> = {}): DataTestResult {
  return {
    exception_frame: { columns: ['invoice_no', '_reason'], dtypes: ['Int64', 'String'], rows: [] },
    exception_profile: null,
    statistics: [],
    step_results: [],
    semantic_issues: [],
    verdict_text: '4 exception rows.',
    exception_count: 4,
    status: 'completed_with_exception',
    ...overrides,
  } as unknown as DataTestResult
}

function mountPanel(t: DataTest, r: DataTestResult | null) {
  return mount(DataTestResultPanel, {
    props: { test: t, result: r },
    global: { stubs: { FrameTable: true, UiTestStatus: true, Message: { template: '<div><slot /></div>' } } },
  })
}

describe('DataTestResultPanel', () => {
  it('states what the run found separately from what still stands', () => {
    const wrapper = mountPanel(test(), result())

    expect(wrapper.find('.two-readings').text()).toContain('The run found exceptions: 4 exception rows')
    expect(wrapper.find('.two-readings').text()).toContain('4 still open')
  })

  it('keeps the run’s count when every group has been accepted', () => {
    const wrapper = mountPanel(test({ open_exception_count: 0 }), result())

    // The run found four. That does not stop being true because they were
    // accepted, so both numbers stay on the page.
    expect(wrapper.find('.two-readings').text()).toContain('4 exception rows')
    expect(wrapper.find('.two-readings').text()).toContain('none stand against the control')
  })

  it('only offers rulings on groups the backend holds', () => {
    // A migrated record can carry a richer profile on its stored result than
    // the evaluation knows about. Offering those extra labels would be an
    // action the API refuses, so the evaluation is what decides.
    const wrapper = mountPanel(
      test(),
      result({
        exception_profile: {
          entity_key: 'invoice_no',
          record_count: 4,
          row_count: 4,
          population: 10,
          population_table: 'tx',
          reason_source: 'predicate',
          reasons: [
            { label: 'Pending approval', rows: 3, records: 3, columns: ['status'] },
            { label: 'A group the evaluation does not have', rows: 1, records: 1, columns: [] },
          ],
        },
      }),
    )

    const labels = wrapper.findAll('.reason-label').map(node => node.text())
    expect(labels).toEqual(['Pending approval'])
    // The richer profile still enriches what it legitimately describes.
    expect(wrapper.text()).toContain('of 10')
  })

  it('warns when the result no longer describes the current basis', () => {
    const wrapper = mountPanel(test({ result_stale: true }), result())

    expect(wrapper.text()).toContain('This result is out of date')
    expect(wrapper.find('.two-readings').attributes('data-stale')).toBe('true')
  })

  it('shows what the runner could not vouch for without holding the test back', () => {
    const wrapper = mountPanel(
      test(),
      result({ semantic_issues: ['The step cannot match the rows it describes.'] }),
    )

    expect(wrapper.text()).toContain('cannot match the rows it describes')
    // The warning qualifies the result; nothing is being asked of the reader,
    // and the conclusion below is theirs to reach either way.
    expect(wrapper.text()).not.toContain('produced no usable evidence')
    expect(wrapper.find('.review').exists()).toBe(false)
  })

  it('asks for a review only when the run produced nothing to read', () => {
    const wrapper = mountPanel(
      test({
        status: 'review_required',
        evaluation: { ...test().evaluation, state: 'inconclusive' },
      }),
      result({ semantic_issues: ['Step failed to execute: no such column.'] }),
    )

    expect(wrapper.text()).toContain('This run produced no usable evidence')
    expect(wrapper.text()).toContain('I have reviewed this and it does not invalidate the result')
  })
})
