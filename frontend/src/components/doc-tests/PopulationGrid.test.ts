import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import type { PopulationGrid as PopulationGridPayload } from '../../types'
import PopulationGrid from './PopulationGrid.vue'

function payload(overrides: Partial<PopulationGridPayload> = {}): PopulationGridPayload {
  return {
    test_id: 'DT-7608F21B',
    test_sha1: 'sha1:test',
    item_id: 'ITEM-C0F719EC',
    label: 'Verify expense classification',
    question: 'Does expense_description describe an expense the SOP permits?',
    document_type: 'payment_voucher',
    identifier_columns: ['voucher_id'],
    field_columns: ['expense_category', 'expense_description'],
    available_columns: ['voucher_id', 'employee_name', 'expense_category', 'expense_description'],
    criteria: [{
      kind: 'document',
      label: 'Expense Policy §4',
      text: 'Alcohol is not a reimbursable expense.',
      document_id: 'f408fb18a7',
      section: '4',
    }],
    summary: {
      item_id: 'ITEM-C0F719EC',
      label: 'Verify expense classification',
      document_type: 'payment_voucher',
      fields: ['expense_category', 'expense_description'],
      selection: { mode: 'all' },
      assurance_scope: 'full_population',
      population_records: 3,
      population_documents: 2,
      resolved: 3,
      resolved_documents: 2,
      assessed: 3,
      omitted_records: 0,
      capped: false,
      unread_documents: [],
      resolved_at: '2026-09-06T14:30:00+00:00',
      outcome_counts: { accepted: 2, exception: 1, needs_manual_check: 0 },
      run_current: true,
    },
    assurance_scope: 'full_population',
    rows: [
      {
        key: 'doc-b#1',
        document_id: 'doc-b',
        record_index: 1,
        document_title: 'PV-2025-002',
        values: {
          voucher_id: 'PV-2025-002',
          expense_category: 'Meals',
          expense_description: 'Client dinner – wine',
        },
        field_citations: { expense_description: 'c14' },
        missing_fields: [],
        outcome: 'exception',
        answer: 'Alcohol is excluded under SOP 4.2.',
        conclusion: 'Alcohol excluded under §4.2',
        evidence_refs: [{ page: 1, excerpt: 'Client dinner – wine' } as never],
        disposition: 'pending',
        disposition_note: '',
      },
      {
        key: 'doc-a#0',
        document_id: 'doc-a',
        record_index: 0,
        document_title: 'PV-2025-001',
        values: { voucher_id: 'PV-2025-001', expense_category: 'Transport' },
        field_citations: {},
        missing_fields: ['expense_description'],
        outcome: 'accepted',
        answer: 'Permitted transport expense.',
        conclusion: 'Permitted',
        evidence_refs: [],
        disposition: 'pending',
        disposition_note: '',
      },
    ],
    page: { offset: 0, limit: 50, total: 3 },
    truncated: false,
    ...overrides,
  }
}

const global = { directives: { tooltip: () => undefined } }
const test = {
  id: 'DT-7608F21B',
  rcm_id: 'RCM-FF30C9',
  control_conclusion: 'no_conclusion',
  items: [],
} as never
const props = {
  workspaceId: 'WS-1',
  testId: 'DT-7608F21B',
  itemId: 'ITEM-C0F719EC',
  test,
  findings: [],
  busy: false,
  running: false,
}

afterEach(() => vi.restoreAllMocks())

describe('PopulationGrid', () => {
  it('loads only the paged projection for one item', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue(payload())

    mount(PopulationGrid, { props, global })
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(1)
    expect(get).toHaveBeenCalledWith(
      '/api/workspaces/WS-1/doc-tests/DT-7608F21B/items/ITEM-C0F719EC/grid?offset=0&limit=50',
    )
  })

  it('names the RCM row the population work counts as coverage of', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()

    const chip = wrapper.find('.grid-head .rcm-link')
    expect(chip.text()).toContain('RCM-FF30C9')

    await chip.trigger('click')
    expect(wrapper.emitted('openRcm')).toEqual([['RCM-FF30C9']])
  })

  it('says so in the header when the test is linked to no RCM row', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())

    const wrapper = mount(PopulationGrid, {
      props: { ...props, test: { ...(test as object), rcm_id: null } as never },
      global,
    })
    await flushPromises()

    expect(wrapper.find('.grid-head .rcm-link').exists()).toBe(false)
    expect(wrapper.find('.grid-head .unlinked').text()).toContain('Not linked to an RCM row')
  })

  it('leads with the exception and names the criteria the rows were judged against', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()

    const rows = wrapper.findAll('tbody tr')
    expect(rows[0].text()).toContain('PV-2025-002')
    expect(rows[0].text()).toContain('Alcohol excluded under §4.2')
    expect(wrapper.text()).toContain('Expense Policy §4')
  })

  it('says a field the reading never stated is not stated, rather than blank', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()

    const accepted = wrapper.findAll('tbody tr')[1]
    expect(accepted.find('td.missing').text()).toContain('not stated')
  })

  it('states partial coverage rather than presenting it as a full population', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(
      payload({
        assurance_scope: 'sampled_population',
        summary: {
          ...payload().summary,
          assurance_scope: 'sampled_population',
          assessed: 2,
          resolved: 3,
          run_current: false,
          unread_documents: [{ document_id: 'doc-c', title: 'PV-2025-009' }],
        },
      }),
    )

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()

    const coverage = wrapper.get('.coverage')
    expect(coverage.text()).toContain('Partial coverage')
    expect(coverage.text()).toContain('2 of 3 payment_voucher records assessed')
    expect(coverage.text()).toContain('1 record not yet assessed — run to update')
    expect(coverage.text()).toContain('PV-2025-009')
  })

  it('confirms every accepted record in one call and leaves the flagged one open', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())
    const post = vi.spyOn(api, 'post').mockResolvedValue({} as never)

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()
    await wrapper.get('button[aria-label="Confirm all accepted"]').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/workspaces/WS-1/doc-tests/DT-7608F21B/items/ITEM-C0F719EC/record-dispositions',
      { dispositions: [{ key: 'doc-a#0', state: 'confirmed' }] },
    )
  })

  it('records one record call without touching its siblings', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())
    const post = vi.spyOn(api, 'post').mockResolvedValue({} as never)

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()
    await wrapper
      .get('button[aria-label="Mark PV-2025-002 record 1 an exception"]')
      .trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/workspaces/WS-1/doc-tests/DT-7608F21B/items/ITEM-C0F719EC/record-dispositions',
      { dispositions: [{ key: 'doc-b#1', state: 'exception' }] },
    )
  })

  it('opens one record in a pane rather than stacking every answer card', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()
    expect(wrapper.find('.detail-pane').exists()).toBe(false)

    await wrapper.findAll('tbody tr')[0].trigger('click')

    const pane = wrapper.get('.detail-pane')
    expect(pane.text()).toContain('Alcohol is excluded under SOP 4.2.')
    expect(pane.text()).toContain('cited c14')
  })

  it('re-resolves the population on demand', async () => {
    vi.spyOn(api, 'get').mockResolvedValue(payload())
    const post = vi.spyOn(api, 'post').mockResolvedValue({} as never)

    const wrapper = mount(PopulationGrid, { props, global })
    await flushPromises()
    await wrapper.get('button[aria-label="Re-resolve"]').trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith(
      '/api/workspaces/WS-1/doc-tests/DT-7608F21B/items/ITEM-C0F719EC/resolve',
      {},
    )
  })
})
