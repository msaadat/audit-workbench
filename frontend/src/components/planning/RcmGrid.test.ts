import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { RcmRow } from '../../types'
import RcmGrid from './RcmGrid.vue'

vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ replace: vi.fn() }),
}))

/** A Select that keeps its options and reports a change, as PrimeVue's does. */
const SelectStub = {
  props: ['modelValue', 'options'],
  emits: ['update:modelValue', 'change'],
  template: '<select :data-options="options.join(\',\')" :value="modelValue"'
    + ' @change="$emit(\'update:modelValue\', $event.target.value);'
    + ' $emit(\'change\', { value: $event.target.value })">'
    + '<option v-for="option in options" :key="option" :value="option">{{ option }}</option>'
    + '</select>',
}

// The virtualised DataTable does not survive jsdom, and the cells are what is
// under test. The stub hands the rows to each Column rather than laying out a
// table: a column renders its own body slot per row, which is all a cell test
// needs and keeps the columns in template order.
const DataTableStub = {
  props: ['value'],
  provide() { return { gridRows: (this as unknown as { value: RcmRow[] }).value } },
  template: '<div class="table"><slot /></div>',
}
/** A Button that renders as one, so a row action can actually be clicked. */
const ButtonStub = {
  props: ['icon', 'label'],
  template: '<button :data-icon="icon">{{ label }}</button>',
}
const ColumnStub = {
  inject: ['gridRows'],
  template: '<div class="col"><span v-for="data in gridRows" :key="data.id" class="cell">'
    + '<slot name="body" :data="data" /></span></div>',
}

function row(overrides: Partial<RcmRow> = {}): RcmRow {
  return {
    id: 'R1', semantic_id: 'R1', created_by: 'agent', agent_run_id: null,
    process: '', risk: '', risk_rating: 'high', business_cycle: '',
    control_attributes: [], control: '', control_type: '', control_owner: '',
    criteria: '', criteria_refs: [], test_refs: [],
    execution_rollup: { tests: 0, completed: 0, exceptions: 0, test_rollups: [] },
    finding_refs: [], evidence_refs: [], prepared_by: null,
    review_status: 'draft', updated: '',
    ...overrides,
  } as unknown as RcmRow
}

function mountGrid(rows: RcmRow[]) {
  return mount(RcmGrid, {
    props: { rows },
    global: {
      stubs: {
        DataTable: DataTableStub, Column: ColumnStub, Select: SelectStub,
        Textarea: true, Tag: true, Button: ButtonStub,
      },
      directives: { tooltip: () => undefined },
    },
  })
}

describe('RcmGrid review column', () => {
  it('offers every review status the backend accepts, in order', () => {
    const wrapper = mountGrid([row()])
    const review = wrapper.findAll('select').at(-1)

    // Sign-off is binary. The retired middle states counted as neither signed
    // nor outstanding, so a row parked in one fell out of both tallies.
    expect(review?.attributes('data-options')).toBe('draft,reviewed')
    expect((review?.element as HTMLSelectElement).value).toBe('draft')
  })

  it('writes the row back on change, so sign-off does not need the dialog', async () => {
    const wrapper = mountGrid([row()])
    const review = wrapper.findAll('select').at(-1)!

    await review.setValue('reviewed')

    expect(wrapper.emitted('update')?.[0]).toEqual(['R1', { review_status: 'reviewed' }])
  })
})

describe('RcmGrid row actions', () => {
  // The working paper is the row's reviewable output. Reaching it only through
  // the detail dialog meant the matrix could list 27 rows and offer a way into
  // none of their papers.
  it('opens a row working paper from the row itself', async () => {
    const wrapper = mountGrid([row()])

    await wrapper.find('button[data-icon="pi pi-file"]').trigger('click')

    expect(wrapper.emitted('paper')?.[0]).toEqual([expect.objectContaining({ id: 'R1' })])
  })

  it('keeps the paper action distinct from opening the detail', async () => {
    const wrapper = mountGrid([row()])

    await wrapper.find('button[data-icon="pi pi-eye"]').trigger('click')

    expect(wrapper.emitted('open')?.[0]).toEqual([expect.objectContaining({ id: 'R1' })])
    expect(wrapper.emitted('paper')).toBeUndefined()
  })

  it('marks the rows carrying a cycle contract, which otherwise reads two levels down', () => {
    // Comparisons live under an attribute, and only under the transaction-cycle
    // strategy, so a matrix's whole cycle coverage is invisible without opening
    // every row in turn.
    const wrapper = mountGrid([row()])
    const count = (wrapper.vm as never as {
      cycleComparisons: (row: unknown) => number
    }).cycleComparisons

    expect(count({ control_attributes: [
      { evidence_kind: 'transaction_cycle', required_comparisons: [{ key: 'c1' }, { key: 'c2' }] },
      { evidence_kind: 'tabular_population' },
    ] })).toBe(2)
    // An inquiry attribute states no comparison, so the row carries no contract.
    expect(count({ control_attributes: [{ evidence_kind: 'inquiry' }] })).toBe(0)
    expect(count({ control_attributes: [] })).toBe(0)
  })
})
