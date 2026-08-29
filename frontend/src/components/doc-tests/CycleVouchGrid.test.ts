import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import type { CycleVouchGridPayload } from '../../types'
import CycleVouchGrid from './CycleVouchGrid.vue'

const payload: CycleVouchGridPayload = {
  test_id: 'DT-CYCLE',
  test_sha1: 'sha1:test',
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

const global = {
  directives: { tooltip: () => undefined },
}

afterEach(() => vi.restoreAllMocks())

describe('CycleVouchGrid', () => {
  it('loads only the paged grid projection and never the whole test', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(payload)
    const wrapper = mount(CycleVouchGrid, {
      props: { workspaceId: 'WS-1', testId: 'DT-CYCLE', running: false, busy: false, metadata: null },
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
      props: { workspaceId: 'WS-1', testId: 'DT-CYCLE', running: false, busy: false, metadata: null },
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
