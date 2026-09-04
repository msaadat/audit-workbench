import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { FindingRollups, RcmRow } from '../../types'
import RcmGrid from './RcmGrid.vue'

function row(overrides: Partial<RcmRow> = {}): RcmRow {
  return {
    id: 'RCM-R1', semantic_id: 'R1', created_by: 'agent', agent_run_id: null,
    process: 'Requisition initiation', risk: 'A risk statement.', risk_rating: 'high',
    business_cycle: '', control_attributes: [], control: 'A control statement.',
    control_type: '', control_owner: '', criteria: '', criteria_refs: [], test_refs: [],
    execution_rollup: { tests: 1, completed: 1, exceptions: 0, test_rollups: [] },
    finding_refs: [], evidence_refs: [], prepared_by: null,
    review_status: 'draft', updated: '',
    ...overrides,
  } as unknown as RcmRow
}

function mountGrid(rows: RcmRow[], props: Record<string, unknown> = {}) {
  return mount(RcmGrid, { props: { rows, ...props } })
}

function cells(wrapper: ReturnType<typeof mountGrid>, index: number) {
  return wrapper.findAll('.row')[index].element.textContent?.replace(/\s+/g, ' ').trim() ?? ''
}

describe('RcmGrid', () => {
  it('reads rather than edits: no cell is a form control', () => {
    const wrapper = mountGrid([row()])

    // Twelve columns of textareas and selects made the matrix read as a form.
    // Editing is the drawer's job; the row is one control, which opens it.
    expect(wrapper.findAll('textarea')).toHaveLength(0)
    expect(wrapper.findAll('select')).toHaveLength(0)
    expect(wrapper.findAll('input')).toHaveLength(0)
  })

  it('opens the row it was clicked on', async () => {
    const wrapper = mountGrid([row()])

    await wrapper.find('.row').trigger('click')

    expect(wrapper.emitted('open')?.[0]).toEqual([expect.objectContaining({ id: 'RCM-R1' })])
  })

  it('carries the verdicts that used to sit past the right edge', () => {
    const wrapper = mountGrid([row({
      execution_rollup: {
        tests: 2, completed: 2, exceptions: 3, control_conclusion: 'ineffective', test_rollups: [],
      },
      review_status: 'reviewed',
    })], {
      findingRollups: {
        by_rcm: { 'RCM-R1': [{ id: 'F-1', severity: 'critical' }] },
      } as unknown as FindingRollups,
    })

    const text = cells(wrapper, 0)
    // The conclusion, the exception count, the finding and the sign-off all
    // read without a horizontal scroll.
    expect(text).toContain('2 tests · 3 exc')
    expect(text).toContain('Ineffective')
    expect(text).toContain('F-1critical')
    expect(text).toContain('Reviewed')
    // The `RCM-` prefix is on every row, so it identifies nothing.
    expect(wrapper.find('.row-id').text()).toBe('R1')
  })

  it('names a risk with nothing written against it', () => {
    const wrapper = mountGrid([row({ control: '   ' })])

    // An empty control cell was indistinguishable from one that had scrolled
    // out of view.
    expect(wrapper.find('.no-control').text()).toBe('No control identified')
  })

  it('says how many findings a row carries without listing them', () => {
    const wrapper = mountGrid([row()], {
      findingRollups: {
        by_rcm: {
          'RCM-R1': [
            { id: 'F-1', severity: 'high' },
            { id: 'F-2', severity: 'medium' },
          ],
        },
      } as unknown as FindingRollups,
    })

    expect(wrapper.find('.finding-chip').text()).toBe('F-1high')
    expect(wrapper.find('.findings .more').text()).toBe('+1')
  })
})

describe('RcmGrid groups', () => {
  const rows = [
    row({ id: 'RCM-A', execution_rollup: { control_conclusion: 'ineffective', tests: 1, completed: 1, test_rollups: [] } }),
    row({ id: 'RCM-B', control: '' }),
    row({ id: 'RCM-C', process: 'Purchase order' }),
  ]

  it('groups by process, in the order the matrix stores them', () => {
    const wrapper = mountGrid(rows)

    expect(wrapper.findAll('.group-name').map(node => node.text()))
      .toEqual(['Requisition initiation', 'Purchase order'])
  })

  it('says what is wrong with each group, and drops the clauses counting nothing', () => {
    const wrapper = mountGrid(rows)
    const summaries = wrapper.findAll('.group-summary').map(node => node.text())

    expect(summaries[0]).toBe('2 risks · 1 ineffective · 1 without a control')
    expect(summaries[1]).toBe('1 risk')
  })

  it('collapses a group without losing the ones beside it', async () => {
    const wrapper = mountGrid(rows)
    expect(wrapper.findAll('.row')).toHaveLength(3)

    await wrapper.findAll('.group')[0].trigger('click')

    expect(wrapper.findAll('.row')).toHaveLength(1)
    expect(wrapper.findAll('.group')).toHaveLength(2)
  })

  it('files a row with no process under one heading rather than none', () => {
    const wrapper = mountGrid([row({ process: '  ' })])

    expect(wrapper.find('.group-name').text()).toBe('Unassigned')
  })
})
