import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it, vi } from 'vitest'

import ChainView from './ChainView.vue'

/**
 * A finding points at the rows it was written against; a row does not point
 * back. `RcmRow.finding_refs` is part of the shape but nothing populates it,
 * so the chain has to read `finding_rollups.by_rcm` — the index the server
 * sends for exactly this join. These pin the row's meta line, the Findings
 * hop, and the ordering that depends on them.
 */

vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ push: vi.fn(), replace: vi.fn(), to: () => '/', target: () => '/' }),
}))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: {} }) }))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))

const payload: Record<string, unknown> = {}

vi.mock('../../api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: (url: string) => Promise.resolve(url.includes('/documents') ? { items: [] } : payload.planning) },
}))

function row(id: string, risk: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    risk,
    control: `Control for ${id}`,
    criteria: 'Procurement SOP Extract [C3]',
    criteria_refs: [],
    test_refs: ['T1'],
    // Always empty in the real payload — the trap this component fell into.
    finding_refs: [],
    evidence_refs: [],
    execution_rollup: { tests: 2, exceptions: 1, control_conclusion: 'partially_effective' },
    review_status: 'draft',
    ...extra,
  }
}

const FINDING = {
  id: 'F-47F170',
  title: 'Purchase requisition evidence did not establish required delivery, justification and accounting information',
  severity: 'medium',
}

async function render(planning: Record<string, unknown>) {
  payload.planning = {
    rcm: [], data_tests: [], document_tests: [], findings: [], ...planning,
  }
  const wrapper = mount(ChainView, {
    props: { workspace: { id: 'procurement' } as never },
    global: {
      plugins: [PrimeVue],
      stubs: { EvidenceAnchorDialog: true },
      directives: { tooltip: {} },
    },
  })
  await flushPromises()
  return wrapper
}

describe('ChainView finding links', () => {
  it('counts the findings the server indexed against a row', async () => {
    const wrapper = await render({
      rcm: [row('RCM-F08A71', 'Requisitions may be initiated without a valid business need.')],
      finding_rollups: { by_rcm: { 'RCM-F08A71': [FINDING] }, by_test: {}, by_procedure: {} },
    })
    // The row carries `finding_refs: []`; reading it counted zero here.
    expect(wrapper.find('.list .meta').text()).toContain('1 finding')
  })

  it('lists those findings in the Findings hop', async () => {
    const wrapper = await render({
      rcm: [row('RCM-F08A71', 'Requisitions may be initiated without a valid business need.')],
      finding_rollups: { by_rcm: { 'RCM-F08A71': [FINDING] }, by_test: {}, by_procedure: {} },
    })
    const text = wrapper.text()
    expect(text).toContain('Purchase requisition evidence did not establish')
    expect(text).not.toContain('No finding has been drafted from this row.')
  })

  it('ranks a row with a finding above one without', async () => {
    const wrapper = await render({
      rcm: [
        row('RCM-AAAAAA', 'Bare row with no finding.'),
        row('RCM-F08A71', 'Row that produced a finding.'),
      ],
      finding_rollups: { by_rcm: { 'RCM-F08A71': [FINDING] }, by_test: {}, by_procedure: {} },
    })
    const risks = wrapper.findAll('.list .title').map(node => node.text())
    // Depth ordering weights findings highest; with the row field it read zero
    // for both and fell through to the id tiebreak, which put AAAAAA first.
    expect(risks[0]).toBe('Row that produced a finding.')
  })

  it('reports no findings for a row the index does not mention', async () => {
    const wrapper = await render({
      rcm: [row('RCM-AAAAAA', 'Bare row with no finding.')],
      finding_rollups: { by_rcm: { 'RCM-F08A71': [FINDING] }, by_test: {}, by_procedure: {} },
    })
    expect(wrapper.find('.list .meta').text()).toContain('no finding')
    expect(wrapper.text()).toContain('No finding has been drafted from this row.')
  })

  /**
   * With no matrix there is no chain, and the page says where one starts
   * rather than drawing an empty review bar over an empty list.
   */
  it('points at the matrix when there is nothing to follow', async () => {
    const wrapper = await render({})
    expect(wrapper.text()).toContain('Nothing to follow yet')
    expect(wrapper.find('.review-bar').exists()).toBe(false)
    expect(wrapper.find('.layout').exists()).toBe(false)
  })

  it('survives a payload with no rollups at all', async () => {
    const wrapper = await render({
      rcm: [row('RCM-F08A71', 'Requisitions may be initiated without a valid business need.')],
    })
    expect(wrapper.find('.list .meta').text()).toContain('no finding')
    expect(wrapper.text()).toContain('No finding has been drafted from this row.')
  })
})
