import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import PrimeVue from 'primevue/config'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type { PlanningPayload, RcmRow } from '../types'
import RcmRowView from './RcmRowView.vue'

const PrimeVueConfirmSymbol = (
  PrimeVueConfirm as unknown as { PrimeVueConfirmSymbol: symbol }
).PrimeVueConfirmSymbol
const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

const { routeState, routerPush, routerReplace, navTo } = vi.hoisted(() => ({
  routeState: { query: {} as Record<string, string>, params: { id: 'WS-1' } },
  routerPush: vi.fn(),
  routerReplace: vi.fn(),
  navTo: vi.fn((destination: string, state?: Record<string, unknown>) => ({ destination, state })),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRoute: () => routeState,
    useRouter: () => ({ push: routerPush, replace: routerReplace }),
    RouterLink: { props: ['to'], template: '<a><slot /></a>' },
  }
})

vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ to: navTo, push: routerPush, replace: routerReplace }),
}))

vi.mock('../composables/useAgentRun', async () => {
  const { ref } = await import('vue')
  return {
    useAgentRun: () => ({
      isActive: ref(false),
      launchMode: ref('auto'),
      state: { drawerOpen: false },
      toggleDrawer: vi.fn(),
      onWorkspaceInvalidated: () => () => undefined,
    }),
  }
})
vi.mock('../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({ state: { busy: false }, createChat: vi.fn(), send: vi.fn() }),
}))
vi.mock('../components/agent/ProvenanceRail.vue', () => ({
  default: { template: '<div class="rail" />' },
}))
vi.mock('../components/planning/RcmControlAttributesEditor.vue', () => ({
  default: { props: ['modelValue', 'metadata', 'schemas'], template: '<div class="attributes-editor" />' },
}))

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver
globalThis.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
  addListener() {}, removeListener() {},
})) as unknown as typeof globalThis.matchMedia

function row(id: string, overrides: Partial<RcmRow> = {}): RcmRow {
  return {
    id, semantic_id: id, created_by: 'agent', agent_run_id: null,
    process: 'Requisition initiation', risk: 'A risk statement.', risk_rating: 'high',
    business_cycle: '', control_attributes: [{ key: 'a1', assertion: 'Authorization', requirement: 'Limit is enough.' }],
    control: 'A control statement.', control_type: 'Preventive', control_owner: 'Financial Authority',
    criteria: '', criteria_refs: [], test_refs: ['DAT-1'],
    execution_rollup: {
      tests: 1, completed: 1, exceptions: 2, control_conclusion: 'ineffective',
      test_rollups: [{
        test_id: 'DAT-1', kind: 'datatest', title: 'Approver limits', status: 'completed_with_exception',
        executed_count: 1, exception_count: 2, open_exception_count: 2, evidence_count: 0,
        result_summary: '', conclusion: '', control_conclusion: 'ineffective',
        scope_limitations: '', finding_refs: [],
      }],
    },
    finding_refs: [], evidence_refs: [], prepared_by: null,
    review_status: 'draft', updated: '',
    ...overrides,
  } as unknown as RcmRow
}

const PAPER = {
  rcm_id: 'RCM-A',
  generated_at: '2026-09-03T14:02:00Z',
  markdown: '## Purpose\n',
  html: '<h2>Purpose</h2><p>Why.</p><h2>Conclusion</h2><p>Ineffective.</p>',
}

function payload(rows: RcmRow[]): PlanningPayload {
  return {
    planning: {}, rcm: rows, procedures: [], data_tests: [], document_tests: [],
    observations: [{
      id: 'OBS-1', rcm_id: 'RCM-A', test_id: 'DAT-1', execution_ref: '', exception_count: 2,
      summary: 'Two requisitions exceeded the limit.', classification: 'control_failure',
      outcome: 'exception',
    }],
    findings: [],
    finding_rollups: { by_rcm: { 'RCM-A': [{ id: 'F-1', severity: 'critical' }] }, by_test: {}, by_procedure: {} },
  } as unknown as PlanningPayload
}

function mountView(rowId = 'RCM-A', query: Record<string, string> = {}) {
  routeState.query = query
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/planning')) return payload([row('RCM-A'), row('RCM-B')]) as never
    if (url.endsWith('/working-paper')) return PAPER as never
    if (url.endsWith('/documents')) return { items: [] } as never
    if (url.endsWith('/doc-tests/meta')) return { cycle_vouch: {} } as never
    if (url.endsWith('/documents/schemas')) return { items: [] } as never
    return {} as never
  })
  return mount(RcmRowView, {
    props: { id: 'WS-1', rowId },
    global: {
      provide: {
        [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
        [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
      },
      plugins: [PrimeVue],
      directives: { tooltip: () => undefined },
      stubs: { EvidenceAnchorDialog: true },
    },
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  routerPush.mockReset()
  routerReplace.mockReset()
  routeState.query = {}
})

describe('RcmRowView', () => {
  it('leads with the risk statement and counts what each tab holds', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('h1').text()).toBe('A risk statement.')
    expect(wrapper.findAll('.tab').map(node => node.text().replace(/\s+/g, ' ')))
      .toEqual(['Definition', 'Attributes 1', 'Tests 1 · 2 exc', 'Working paper', 'Where this came from'])
    // The open-exception badge is what makes the tab worth reading first.
    expect(wrapper.findAll('.badge')[1].attributes('data-tone')).toBe('bad')
  })

  it('walks the matrix without going back to it', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('.stepper span').text()).toBe('1 of 2')
    await wrapper.findAll('.stepper button')[1].trigger('click')

    expect(navTo).toHaveBeenCalledWith('rcm-row', expect.objectContaining({ rcm: 'RCM-B' }))
  })

  it('offers sign-off as one act while the conclusion is the agent’s', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('.conclusion').text()).toContain('Conclusion: Ineffective')
    expect(wrapper.get('.by-agent').text()).toContain('No one has read it yet')

    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    await wrapper.findAll('.conclusion button').find(node =>
      node.text().includes('Accept and mark reviewed'))!.trigger('click')
    await flushPromises()

    expect(patch.mock.calls[0][0]).toBe('/api/workspaces/WS-1/rcm/RCM-A')
    expect(patch.mock.calls[0][1]).toMatchObject({ review_status: 'reviewed' })
  })

  it('renders the paper only once its tab is asked for, with a list of its sections', async () => {
    const resting = mountView()
    await flushPromises()
    expect(vi.mocked(api.get).mock.calls.map(call => call[0]))
      .not.toContain('/api/workspaces/WS-1/rcm/RCM-A/working-paper')
    resting.unmount()

    const wrapper = mountView('RCM-A', { tab: 'paper' })
    await flushPromises()

    expect(vi.mocked(api.get).mock.calls.map(call => call[0]))
      .toContain('/api/workspaces/WS-1/rcm/RCM-A/working-paper')
    // The contents come from the paper's own headings, so a changed template
    // cannot leave a list that names sections it no longer has.
    expect(wrapper.findAll('.paper-link').map(node => node.text())).toEqual(['Purpose', 'Conclusion'])
    expect(wrapper.get('.paper').html()).toContain('id="paper-section-0"')
  })

  it('keeps the observations beside the provenance that explains them', async () => {
    const wrapper = mountView('RCM-A', { tab: 'provenance' })
    await flushPromises()

    expect(wrapper.find('.rail').exists()).toBe(true)
    expect(wrapper.get('.observation').text()).toContain('Two requisitions exceeded the limit.')
  })

  it('says so rather than rendering an empty page when the row is gone', async () => {
    const wrapper = mountView('RCM-GONE')
    await flushPromises()

    expect(wrapper.text()).toContain('This row is no longer in the matrix')
  })
})
