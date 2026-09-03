import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import { afterEach, describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { DataTest, WorkspaceSummary } from '../types'
import DataTestsTab from './DataTestsTab.vue'

const PrimeVueConfirmSymbol = (
  PrimeVueConfirm as unknown as { PrimeVueConfirmSymbol: symbol }
).PrimeVueConfirmSymbol
const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

const { navReplace, routeState } = vi.hoisted(() => ({
  navReplace: vi.fn(),
  routeState: { query: {} as Record<string, string>, params: { id: 'WS-1' } },
}))

vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return { ...actual, useRoute: () => routeState }
})

vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ replace: navReplace }),
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

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

// A Select we can drive: the real one needs a live overlay.
vi.mock('primevue/select', () => ({
  default: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: '<button class="select-stub" @click="$emit(\'update:modelValue\', \'ineffective\')">{{ modelValue }}</button>',
  },
}))

function dataTest(id: string, overrides: Partial<DataTest> = {}): DataTest {
  return {
    id, semantic_id: id, rcm_id: 'RCM-1', title: `Test ${id}`, objective: 'Objective',
    engine: 'analytics', table_refs: [], steps: [], spec: {},
    status: 'completed_no_exception', semantic_warnings: [],
    evaluation: { state: 'passed', suggested_control_conclusion: 'effective', input_sha1: 'sha' },
    exception_dispositions: [],
    conclusion_source: 'none', control_conclusion: 'no_conclusion',
    control_conclusion_source: 'none', control_conclusion_input_sha1: null,
    control_conclusion_stale: false, result_stale: false, semantic_review: null,
    last_run: null, evidence_refs: [], finding_refs: [],
    exception_count: 0, open_exception_count: 0,
    result_summary: '', conclusion: '', scope_limitations: '', next_action: '',
    created_by: 'agent', agent_run_id: null, created: '', updated: '',
    ...overrides,
  } as DataTest
}

const tests = [dataTest('DAT-A'), dataTest('DAT-B'), dataTest('DAT-C')]

function mountTab() {
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/data-tests')) return { items: tests.map(item => ({ ...item })) }
    if (url.endsWith('/planning')) return { rcm: [], findings: [] }
    throw new Error(`Unexpected URL: ${url}`)
  })
  return mount(DataTestsTab, {
    attachTo: document.body,
    props: { workspace: { id: 'WS-1', name: 'Fixture', tables: [] } as unknown as WorkspaceSummary },
    global: {
      provide: {
        [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
        [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
      },
      plugins: [PrimeVue],
      directives: { tooltip: () => undefined },
      stubs: { DataTestCreateDialog: true, ProvenanceRail: true, Drawer: true, DataTestResultPanel: true },
    },
  })
}

afterEach(() => { vi.restoreAllMocks(); navReplace.mockReset() })

describe('DataTestsTab conclusion rail', () => {
  it('records the conclusion on the test being edited, not on whatever the filter reselects', async () => {
    const wrapper = mountTab()
    await flushPromises()

    // Narrow to the tests with no conclusion, then pick the second one.
    await wrapper.getComponent({ name: 'UiStatusLanes' }).vm.$emit('filter', ['no_conclusion'])
    await flushPromises()

    const row = wrapper.findAll('button').find(b => b.text().includes('Test DAT-B'))!
    await row.trigger('click')
    await flushPromises()
    expect(wrapper.get('.detail-head .eyebrow').text()).toBe('DAT-B')

    // Conclude it: exactly what the auditor does in the rail.
    await wrapper.get('.select-stub').trigger('click')
    await flushPromises()

    // Choosing a value is a draft, so the narrowed list does not re-sort itself
    // out from under the record being written up.
    expect(wrapper.get('.detail-head .eyebrow').text()).toBe('DAT-B')
    expect(wrapper.findAll('button').some(b => b.text().includes('Test DAT-B'))).toBe(true)

    const patch = vi.spyOn(api, 'patch').mockResolvedValue({})
    const save = wrapper.findAll('button').find(b => b.text().includes('Save conclusion'))!
    await save.trigger('click')
    await flushPromises()

    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch.mock.calls[0]?.[0]).toBe('/api/workspaces/WS-1/data-tests/DAT-B')
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ control_conclusion: 'ineffective' })
    wrapper.unmount()
  })
})
