import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type { PlanningPayload, RcmRow, WorkspaceSummary } from '../types'
import PlanningTab from './PlanningTab.vue'

const PrimeVueConfirmSymbol = (
  PrimeVueConfirm as unknown as { PrimeVueConfirmSymbol: symbol }
).PrimeVueConfirmSymbol
const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

const { routeState } = vi.hoisted(() => ({
  routeState: { query: {} as Record<string, string>, params: { id: 'WS-1' } },
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return { ...actual, useRoute: () => routeState }
})

vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ replace: vi.fn() }),
}))

vi.mock('../composables/useAgentRun', async () => {
  const { ref } = await import('vue')
  return {
    useAgentRun: () => ({
      isActive: ref(false),
      launchMode: ref('auto'),
      state: { drawerOpen: false, status: { configured: true } },
      toggleDrawer: vi.fn(),
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
    props: ['rows', 'dataTests', 'documentTests', 'findingRollups', 'generating', 'canGenerate'],
    emits: ['add', 'remove', 'update', 'open', 'generate'],
    template: '<div class="rcm-grid-stub">{{ rows.length }}</div>',
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
    finding_refs: [], evidence_refs: [], prepared_by: null, reviewed_by: null,
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

function mountTab(rows: RcmRow[]) {
  vi.spyOn(api, 'get').mockImplementation((url: string) => {
    if (url.endsWith('/planning')) return Promise.resolve(payload(rows) as never)
    if (url.endsWith('/documents')) return Promise.resolve({ items: [] } as never)
    if (url.endsWith('/doc-tests/meta')) return Promise.resolve({ cycle_vouch: {} } as never)
    return Promise.resolve({} as never)
  })
  return mount(PlanningTab, {
    props: { workspace, section: 'rcm' as const },
    global: {
      provide: {
        [PrimeVueConfirmSymbol as symbol]: { require: confirmRequire, close: vi.fn() },
        [PrimeVueToastSymbol as symbol]: { add: toastAdd, remove: vi.fn(), removeAllGroups: vi.fn() },
      },
      stubs: { Dialog: true, teleport: true },
    },
  })
}

describe('PlanningTab sign-off', () => {
  beforeEach(() => { vi.restoreAllMocks(); toastAdd.mockClear(); confirmRequire.mockClear() })
  afterEach(() => { vi.restoreAllMocks() })

  it('signs off every unreviewed row the strip counted, and says what that costs', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = mountTab([row('R1', 'reviewed'), row('R2', 'draft'), row('R3', 'prepared')])
    await flushPromises()

    const settle = wrapper.find('.disclosure .settle')
    expect(settle.text()).toBe('Mark 2 rows reviewed')
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

    await wrapper.find('.disclosure .settle').trigger('click')
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

    expect(wrapper.find('.disclosure').text()).toContain('1 of 1 row reviewed')
    expect(wrapper.find('.disclosure .settle').exists()).toBe(false)
  })
})
