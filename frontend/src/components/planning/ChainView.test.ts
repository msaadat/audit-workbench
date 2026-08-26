import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ChainView from './ChainView.vue'

/**
 * A finding points at the rows it was written against; a row does not point
 * back. `RcmRow.finding_refs` is part of the shape but nothing populates it,
 * so the chain has to read `finding_rollups.by_rcm` — the index the server
 * sends for exactly this join. These pin the rail counts, the Findings hop,
 * and the ordering that depends on them.
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
    global: { stubs: { EvidenceAnchorDialog: true } },
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
    expect(wrapper.find('.rail-links').text()).toContain('1 find')
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
    const risks = wrapper.findAll('.rail-risk').map(node => node.text())
    // Depth ordering weights findings highest; with the row field it read zero
    // for both and fell through to the id tiebreak, which put AAAAAA first.
    expect(risks[0]).toBe('Row that produced a finding.')
  })

  it('reports no findings for a row the index does not mention', async () => {
    const wrapper = await render({
      rcm: [row('RCM-AAAAAA', 'Bare row with no finding.')],
      finding_rollups: { by_rcm: { 'RCM-F08A71': [FINDING] }, by_test: {}, by_procedure: {} },
    })
    expect(wrapper.find('.rail-links').text()).toContain('0 find')
    expect(wrapper.text()).toContain('No finding has been drafted from this row.')
  })

  it('survives a payload with no rollups at all', async () => {
    const wrapper = await render({
      rcm: [row('RCM-F08A71', 'Requisitions may be initiated without a valid business need.')],
    })
    expect(wrapper.find('.rail-links').text()).toContain('0 find')
    expect(wrapper.text()).toContain('No finding has been drafted from this row.')
  })
})
