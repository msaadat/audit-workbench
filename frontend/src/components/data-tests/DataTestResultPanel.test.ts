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
    next_action: '',
    scope_limitations: '',
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
    summary_frame: null,
    statistics: [],
    step_results: [],
    semantic_issues: [],
    verdict_text: '4 exception rows.',
    exception_count: 4,
    status: 'completed_with_exception',
    ...overrides,
  } as unknown as DataTestResult
}

function mountPanel(t: DataTest, r: DataTestResult | null, props: Record<string, unknown> = {}) {
  return mount(DataTestResultPanel, {
    props: { test: t, result: r, ...props },
    global: {
      stubs: {
        FrameTable: true,
        UiTestStatus: true,
        ProvenanceRail: { template: '<div class="rail" />' },
      },
    },
  })
}

describe('DataTestResultPanel', () => {
  it('states the run’s outcome nowhere, because the verdict bar states it once', () => {
    const wrapper = mountPanel(test(), result())

    // The status chip, the headline, the two-readings line and the statistics
    // tiles were four renderings of one fact. The panel now carries only the
    // evidence behind it.
    expect(wrapper.text()).not.toContain('4 exception rows.')
    expect(wrapper.text()).not.toContain('The run found exceptions')
    expect(wrapper.find('.reason-card').exists()).toBe(true)
  })

  it('leaves staleness to the verdict bar rather than banner-ing it again', () => {
    const wrapper = mountPanel(
      test({ result_stale: true, control_conclusion_stale: true }), result(),
    )

    expect(wrapper.text()).not.toContain('out of date')
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

    expect(wrapper.findAll('.reason-label').map(node => node.text())).toEqual(['Pending approval'])
  })

  it('shows what the runner could not vouch for without holding the test back', () => {
    const wrapper = mountPanel(
      test(),
      result({ semantic_issues: ['The step cannot match the rows it describes.'] }),
    )

    expect(wrapper.text()).toContain('cannot match the rows it describes')
    // The warning qualifies the result; nothing is being asked of the reader,
    // and the conclusion above is theirs to reach either way.
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

describe('DataTestResultPanel disclosures', () => {
  it('folds the machinery into one row of links and opens each in place', async () => {
    const wrapper = mountPanel(
      test(),
      result({
        step_results: [{
          step_id: 'S1', step_label: 'Approval present', status: 'completed_with_exception',
          exception_count: 4, error: null,
        }],
        summary_frame: { columns: ['n'], dtypes: ['Int64'], rows: [[4]] },
      }),
      { workspaceId: 'WS-1' },
    )
    const links = wrapper.findAll('.disclosure-link')

    expect(links.map(node => node.text()))
      .toEqual(['Checks that ran · 1', 'Summary output', 'Where this came from'])
    expect(wrapper.find('.steps').exists()).toBe(false)

    await links[0].trigger('click')
    expect(wrapper.find('.steps').text()).toContain('Approval present')

    await links[2].trigger('click')
    // Provenance is a question asked once per engagement, not a permanent rail.
    expect(wrapper.find('.rail').exists()).toBe(true)
  })

  it('offers no link to a block that has nothing behind it', () => {
    const wrapper = mountPanel(test(), result())

    expect(wrapper.findAll('.disclosure-link')).toHaveLength(0)
  })
})
