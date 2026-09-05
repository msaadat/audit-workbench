import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { PlanningPayload, RcmRow, WorkspaceSummary } from '../types'
import PlanningTab from './PlanningTab.vue'

const PrimeVueConfirmSymbol = (
  PrimeVueConfirm as unknown as { PrimeVueConfirmSymbol: symbol }
).PrimeVueConfirmSymbol
const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

const { routeState, routerReplace, navTo, navPush } = vi.hoisted(() => ({
  routeState: { query: {} as Record<string, string>, params: { id: 'WS-1' } },
  routerReplace: vi.fn(),
  navTo: vi.fn((destination: string, state?: Record<string, unknown>) => ({ destination, state })),
  navPush: vi.fn(),
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return {
    ...actual,
    useRoute: () => routeState,
    useRouter: () => ({ replace: routerReplace, push: vi.fn() }),
  }
})

vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ replace: vi.fn(), push: navPush, to: navTo }),
}))

vi.mock('../composables/useAgentRun', async () => {
  const { ref } = await import('vue')
  return {
    useAgentRun: () => ({
      isActive: ref(false),
      launchMode: ref('auto'),
      state: { panelMode: 'closed', status: { configured: true } },
      openPanel: vi.fn(),
      togglePanel: vi.fn(),
      onWorkspaceInvalidated: () => () => undefined,
    }),
  }
})

vi.mock('../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({ state: { busy: false }, createChat: vi.fn(), send: vi.fn() }),
}))

// The editors are irrelevant to sign-off and drag in CodeMirror; the grid is
// the surface the strip sits above, not the thing under test, and its virtual
// DataTable does not survive jsdom.
vi.mock('./MarkdownEditor.vue', () => ({ default: { template: '<div class="markdown-editor" />' } }))
vi.mock('./agent/ProvenanceRail.vue', () => ({ default: { template: '<div class="rail" />' } }))
vi.mock('./planning/RcmGrid.vue', () => ({
  default: {
    props: ['rows', 'findingRollups', 'selectedId'],
    emits: ['open'],
    template: '<div class="rcm-grid-stub">{{ rows.length }}</div>',
  },
}))

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

// PrimeVue's TieredMenu (behind SplitButton) binds a media-query listener on
// mount, and jsdom has neither.
globalThis.matchMedia ??= ((query: string) => ({
  matches: false, media: query, onchange: null,
  addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
  addListener() {}, removeListener() {},
})) as unknown as typeof globalThis.matchMedia

/** A Popover that renders inline, so the pressed chip's vocabulary is reachable. */
vi.mock('primevue/popover', () => ({
  default: {
    name: 'Popover',
    template: '<div class="popover"><slot /></div>',
    methods: { toggle() {}, hide() {} },
  },
}))

const workspace = { id: 'WS-1', name: 'Fixture' } as WorkspaceSummary

/** A row that has run, so the bar has executed work to ask about signing off. */
function row(id: string, review: RcmRow['review_status']): RcmRow {
  return {
    id, semantic_id: id, created_by: 'agent', agent_run_id: null,
    process: 'Process', risk: 'Risk', risk_rating: 'high', business_cycle: '',
    control_attributes: [], control: '', control_type: '', control_owner: '',
    criteria: '', criteria_refs: [], test_refs: [`T-${id}`],
    execution_rollup: {
      tests: 1, completed: 1, exceptions: 0,
      test_rollups: [{
        test_id: `T-${id}`, kind: 'datatest', title: 'Test', status: 'completed_no_exception',
        executed_count: 1, exception_count: 0, open_exception_count: 0, evidence_count: 0,
        result_summary: '', conclusion: '', control_conclusion: 'effective',
        scope_limitations: '', finding_refs: [],
      }],
    },
    finding_refs: [], evidence_refs: [], prepared_by: null,
    review_status: review, updated: '',
  } as unknown as RcmRow
}

function payload(rows: RcmRow[]): PlanningPayload {
  return {
    planning: { apm_markdown: '', created_by: 'agent' },
    rcm: rows, procedures: [], data_tests: [], observations: [], document_tests: [],
    findings: [], finding_rollups: { by_rcm: {}, by_test: {}, by_procedure: {} },
  } as unknown as PlanningPayload
}

const toastAdd = vi.fn()
/** Accepts whatever it is asked, so the write path runs; the message is kept. */
const confirmRequire = vi.fn((options: { message: string; accept: () => unknown }) => {
  void options.accept()
})

function mountTab(rows: RcmRow[], query: Record<string, string> = {}) {
  routeState.query = query
  vi.spyOn(api, 'get').mockImplementation((url: string) => {
    if (url.endsWith('/planning')) return Promise.resolve(payload(rows) as never)
    if (url.endsWith('/documents')) return Promise.resolve({ items: [] } as never)
    if (url.endsWith('/doc-tests/meta')) return Promise.resolve({ cycle_vouch: {} } as never)
    if (url.endsWith('/working-paper')) {
      return Promise.resolve({ rcm_id: 'R1', markdown: '# Paper', html: '<h1>Paper</h1>' } as never)
    }
    return Promise.resolve({} as never)
  })
  return mount(PlanningTab, {
    props: { workspace, section: 'rcm' as const },
    global: {
      provide: {
        [PrimeVueConfirmSymbol as symbol]: { require: confirmRequire, close: vi.fn() },
        [PrimeVueToastSymbol as symbol]: { add: toastAdd, remove: vi.fn(), removeAllGroups: vi.fn() },
      },
      plugins: [PrimeVue],
      directives: { tooltip: () => undefined },
      stubs: {
        Dialog: true,
        // The drawer's contents are the row detail; a bare stub renders none.
        Drawer: { props: ['visible'], template: '<div class="drawer-host"><slot /></div>' },
        teleport: true,
      },
    },
  })
}

describe('PlanningTab sign-off', () => {
  beforeEach(() => { vi.restoreAllMocks(); toastAdd.mockClear(); confirmRequire.mockClear() })
  afterEach(() => { vi.restoreAllMocks() })

  it('signs off every unreviewed row the strip counted, and says what that costs', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = mountTab([row('R1', 'reviewed'), row('R2', 'draft'), row('R3', 'draft')])
    await flushPromises()

    // The settle action sits beside the chip that counts what it settles.
    const settle = wrapper.find('.review-bar .settle')
    expect(settle.text()).toBe('Mark 2 reviewed')
    await settle.trigger('click')
    await flushPromises()

    // The signed row is left alone; the two unsigned ones are written once each.
    expect(patch.mock.calls.map(call => call[0])).toEqual([
      '/api/workspaces/WS-1/rcm/R2', '/api/workspaces/WS-1/rcm/R3',
    ])
    expect(patch.mock.calls[0][1]).toEqual({ review_status: 'reviewed' })
    // Sign-off is not reversible in the way a status change looks: it makes the
    // row auditor-owned, and the prompt has to say so before it happens.
    expect(confirmRequire.mock.calls[0][0].message).toContain('auditor-owned')
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'success', summary: '2 rows marked reviewed',
    }))
  })

  it('reports the rows a partial pass could not move rather than claiming them', async () => {
    vi.spyOn(api, 'patch').mockImplementation((url: string) => (
      url.endsWith('/R3') ? Promise.reject(new Error('nope')) : Promise.resolve({} as never)
    ))
    const wrapper = mountTab([row('R2', 'draft'), row('R3', 'draft')])
    await flushPromises()

    await wrapper.find('.review-bar .settle').trigger('click')
    await flushPromises()

    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'warn',
      summary: '1 row marked reviewed',
      detail: expect.stringContaining('R3'),
    }))
  })

  it('offers no sign-off button once every row is signed', async () => {
    const wrapper = mountTab([row('R1', 'reviewed')])
    await flushPromises()

    // Nothing left to sign, so neither the chip nor the button is drawn.
    expect(wrapper.find('.review-bar .settle').exists()).toBe(false)
    expect(wrapper.findAll('.review-bar .chip').map(node => node.text()))
      .toEqual(['1All rows', '1No control'])
  })
})

describe('PlanningTab working paper', () => {
  beforeEach(() => {
    vi.restoreAllMocks(); toastAdd.mockClear()
    routerReplace.mockClear(); navTo.mockClear(); navPush.mockClear()
  })
  afterEach(() => { vi.restoreAllMocks(); routeState.query = {} })

  it('sends the row drawer to the paper tab of the row it belongs to', async () => {
    const wrapper = mountTab([row('R1', 'draft'), row('R2', 'draft')])
    await flushPromises()

    // The paper is a tab on the row page now, not a second modal over a first.
    wrapper.findComponent({ name: 'RcmGrid' }).vm.$emit('open', row('R2', 'draft'))
    await flushPromises()
    wrapper.findComponent({ name: 'RcmRowDrawer' }).vm.$emit('paper')
    await flushPromises()

    expect(navPush).toHaveBeenCalledWith('rcm-row', { rcm: 'R2', tab: 'paper' })
  })

  // The link an agent milestone hands over has to land on the paper itself,
  // not on the matrix with the paper still two clicks away.
  it('redirects the deep link the milestones still hand over', async () => {
    mountTab([row('R1', 'draft'), row('R2', 'draft')], { paper: 'R2' })
    await flushPromises()

    expect(navTo).toHaveBeenCalledWith('rcm-row', { rcm: 'R2', tab: 'paper' })
    expect(routerReplace).toHaveBeenCalled()
  })

  it('ignores a paper link naming a row the matrix no longer holds', async () => {
    mountTab([row('R1', 'draft')], { paper: 'R9' })
    await flushPromises()

    expect(routerReplace).not.toHaveBeenCalled()
    expect(toastAdd).not.toHaveBeenCalled()
  })
})
