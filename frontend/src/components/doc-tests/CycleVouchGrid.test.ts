import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import type { CycleVouchGridPayload } from '../../types'
import CycleVouchGrid from './CycleVouchGrid.vue'

const payload: CycleVouchGridPayload = {
  test_id: 'DT-CYCLE',
  test_sha1: 'sha1:test',
  control_conclusion: 'no_conclusion',
  rcm_id: 'RCM-1',
  definition_sha1: 'sha1:definition',
  title: 'Payroll payment cycle',
  population: {
    table: 'payroll_register',
    column: 'PAYMENT_ID',
    selection: { mode: 'evidence_linked' },
  },
  coverage: { population_rows: 20, selected_rows: 1 },
  selection_basis: 'evidence_linked',
  assurance_scope: 'targeted_evidence_only',
  assurance_label: 'Targeted evidence - not a sample',
  tested_item_counts: { failed: 1 },
  assertion_counts: {
    match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
    ambiguous: 0, not_run: 0, total: 1,
  },
  columns: [{
    key: 'amount',
    label: 'Amount agrees',
    requirement: 'The records must agree.',
    applicable_roles: ['payslip', 'bank_payment'],
    counts: {
      match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
      ambiguous: 0, not_run: 0,
    },
    stale_cells: 0,
  }],
  rows: [{
    item_id: 'ITEM-1',
    label: 'PAY-001',
    evaluation_state: 'failed',
    disposition_state: 'pending',
    disposition_stale: false,
    definition_stale: false,
    roles_present: ['payslip', 'bank_payment'],
    missing_roles: [],
    shared_record_facts: [],
    cells: {
      amount: {
        verdict: 'mismatch',
        display: '1,000 vs 900',
        comparison_count: 2,
        evidence_count: 2,
        stale: false,
        attribution_stale: false,
        comparisons: [
          {
            role: 'payslip', document_id: 'DOC-PAYSLIP', verdict: 'match',
            record_ids: ['REC-1'], display_values: [1000], entry_count: 1, evidence_count: 1,
          },
          {
            role: 'bank_payment', document_id: 'DOC-BANK', verdict: 'mismatch',
            record_ids: ['REC-2'], display_values: [900], entry_count: 1, evidence_count: 1,
          },
        ],
      },
    },
  }],
  stale_definition: false,
  stale_cell_count: 0,
  page: { offset: 0, limit: 100, total: 1 },
  truncated: false,
}

const props = {
  workspaceId: 'WS-1',
  testId: 'DT-CYCLE',
  running: false,
  busy: false,
  metadata: null,
  findings: [],
}

const global = {
  directives: { tooltip: () => undefined },
}

afterEach(() => vi.restoreAllMocks())

describe('CycleVouchGrid', () => {
  it('loads only the paged grid projection and never the whole test', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(payload)
    const wrapper = mount(CycleVouchGrid, {
      props: { ...props },
      global,
    })
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith('/api/workspaces/WS-1/doc-tests/DT-CYCLE/grid?offset=0&limit=100')
    expect(get.mock.calls.some(([url]) => url === '/api/workspaces/WS-1/doc-tests/DT-CYCLE')).toBe(false)
    // How the items were selected is reported where the coverage numbers are,
    // not as a banner over the grid qualifying every result beneath it.
    expect(wrapper.find('.scope-label').exists()).toBe(false)
  })

  it('retains every projected comparison in a cell popover and opens the exact assertion', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload)
    const openDetail = vi.fn()
    const wrapper = mount(CycleVouchGrid, {
      props: { ...props },
      attrs: { onOpenDetail: openDetail },
      global,
    })
    await flushPromises()

    await wrapper.get('button[aria-label="Amount agrees for PAY-001: Mismatch"]').trigger('click')
    const comparisons = wrapper.findAll('.comparison-list article')
    expect(comparisons).toHaveLength(2)
    expect(comparisons[0].text()).toContain('DOC-PAYSLIP')
    expect(comparisons[1].text()).toContain('DOC-BANK')

    await wrapper.get('button[aria-label="Open assertion evidence"]').trigger('click')
    expect(openDetail).toHaveBeenCalledWith('ITEM-1', 'amount')
  })

  it('keeps filters, selected cell, and scroll positions when detail is shown and closed', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload)
    const Harness = defineComponent({
      components: { CycleVouchGrid },
      setup() {
        const detailOpen = ref(false)
        return { detailOpen }
      },
      template: `
        <CycleVouchGrid
          v-show="!detailOpen"
          workspace-id="WS-1"
          test-id="DT-CYCLE"
          :running="false"
          :busy="false"
          :metadata="null"
          :findings="[]"
          @open-detail="detailOpen = true"
        />
        <button v-if="detailOpen" class="back" @click="detailOpen = false">Back</button>
      `,
    })
    const wrapper = mount(Harness, { global })
    await flushPromises()

    const search = wrapper.get<HTMLInputElement>('input[type="search"]')
    await search.setValue('DOC-BANK')
    const scroll = wrapper.get<HTMLElement>('.grid-scroll').element
    scroll.scrollLeft = 340
    scroll.scrollTop = 125
    await wrapper.get('button[aria-label="Amount agrees for PAY-001: Mismatch"]').trigger('click')
    await wrapper.get('button[aria-label="Open assertion evidence"]').trigger('click')
    await wrapper.get('.back').trigger('click')

    expect(wrapper.get<HTMLInputElement>('input[type="search"]').element.value).toBe('DOC-BANK')
    expect(wrapper.get<HTMLElement>('.grid-scroll').element.scrollLeft).toBe(340)
    expect(wrapper.get<HTMLElement>('.grid-scroll').element.scrollTop).toBe(125)
    expect(wrapper.get('.assertion-cell').classes()).toContain('selected')
  })
})

// --------------------------------------------------------------------------- #
// The two records the grid carries for the test
// --------------------------------------------------------------------------- #
describe('CycleVouchGrid conclusion', () => {
  it('offers the control conclusion on the grid itself', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload)
    const wrapper = mount(CycleVouchGrid, { props: { ...props }, global })
    await flushPromises()

    // It lived only on an item detail one click deeper, so a cycle test — a
    // single-item one especially — presented no way to conclude it at all.
    const select = wrapper.get<HTMLSelectElement>('select[aria-label="Control conclusion"]')
    expect([...select.element.options].map(option => option.value)).toEqual([
      'no_conclusion', 'effective', 'partially_effective', 'ineffective', 'not_applicable',
    ])
  })

  it('asks the page to save only once the conclusion has been changed', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload)
    const saveConclusion = vi.fn()
    const wrapper = mount(CycleVouchGrid, {
      props: { ...props },
      attrs: { onSaveConclusion: saveConclusion },
      global,
    })
    await flushPromises()

    // Nothing to save while it still reads what was filed.
    expect(wrapper.find('button[aria-label="Save"]').exists()).toBe(false)

    await wrapper.get('select[aria-label="Control conclusion"]').setValue('effective')
    const save = wrapper.findAll('button').find(button => button.text().includes('Save'))
    await save!.trigger('click')

    expect(saveConclusion).toHaveBeenCalledTimes(1)
    // The value travels with the request: the page never loaded the test.
    expect(saveConclusion).toHaveBeenCalledWith('effective')
  })

  it('says what is still unanswered without refusing the conclusion', async () => {
    // A warning, not a gate: concluding over open records is the auditor's.
    vi.spyOn(api, 'get').mockResolvedValue(payload)
    const wrapper = mount(CycleVouchGrid, { props: { ...props }, global })
    await flushPromises()

    const warn = wrapper.find('.footer-warn')
    expect(warn.exists()).toBe(true)
    expect(warn.text()).toContain('carry no call yet')
    // The select is still there and still usable.
    expect(wrapper.get('select[aria-label="Control conclusion"]').attributes('disabled')).toBeUndefined()
  })

  it('shows the conclusion the projection filed, not a default', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ ...payload, control_conclusion: 'ineffective' })
    const wrapper = mount(CycleVouchGrid, { props: { ...props }, global })
    await flushPromises()

    const select = wrapper.get<HTMLSelectElement>('select[aria-label="Control conclusion"]')
    expect(select.element.value).toBe('ineffective')
    // Nothing to save: it reads exactly what the file holds.
    expect(wrapper.findAll('button').some(button => button.text().includes('Save'))).toBe(false)
  })

  it('draws no footer until the projection has loaded', async () => {
    vi.spyOn(api, 'get').mockImplementation(() => new Promise(() => {}))
    const wrapper = mount(CycleVouchGrid, { props: { ...props }, global })
    await flushPromises()

    expect(wrapper.find('.footer-row').exists()).toBe(false)
  })
})
