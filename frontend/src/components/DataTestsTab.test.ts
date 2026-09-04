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

/** A Popover that renders inline, so the form behind "Change" is reachable. */
vi.mock('primevue/popover', () => ({
  default: {
    template: '<div class="popover"><slot /></div>',
    methods: { toggle() {}, hide() {} },
  },
}))

function dataTest(id: string, overrides: Partial<DataTest> = {}): DataTest {
  return {
    id, semantic_id: id, rcm_id: 'RCM-1', title: `Test ${id}`, objective: 'Objective',
    engine: 'analytics', table_refs: ['invoice_data'], steps: [], spec: {},
    status: 'completed_no_exception', semantic_warnings: [],
    evaluation: {
      state: 'passed', suggested_control_conclusion: 'effective', input_sha1: 'sha',
      exception_count: 0, reasons: [], note: '', ran_at: null,
    },
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

const failing = dataTest('DAT-A', {
  status: 'completed_with_exception',
  exception_count: 2,
  open_exception_count: 2,
  control_conclusion: 'ineffective',
  control_conclusion_source: 'agent',
  control_conclusion_stale: true,
  last_run: { id: 'RUN-1', run_at: '2026-09-04T09:26:00Z' } as DataTest['last_run'],
  evaluation: {
    state: 'failed', suggested_control_conclusion: 'ineffective', input_sha1: 'sha',
    exception_count: 2, reasons: [], note: '', ran_at: '2026-09-04T09:26:00Z',
  },
})
const tests = [failing, dataTest('DAT-B'), dataTest('DAT-C', { semantic_warnings: ['Check the join'] })]

function mountTab(findings: unknown[] = []) {
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/data-tests')) return { items: tests.map(item => ({ ...item })) }
    if (url.endsWith('/planning')) return { rcm: [], findings }
    if (url.includes('/runs/')) return { exception_profile: null, statistics: [], step_results: [], semantic_issues: [] }
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
      stubs: { Drawer: true, DataTestResultPanel: true },
    },
  })
}

afterEach(() => { vi.restoreAllMocks(); navReplace.mockReset() })

describe('DataTestsTab header', () => {
  it('says how much there is, how much ran, and how much is open', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.get('.page-head h1').text()).toBe('Data tests')
    expect(wrapper.get('.headline').text()).toBe('3 tests · 1 of 3 run · 1 with open exceptions')
  })

  it('makes the outstanding write-up the primary, and Run all when there is none', async () => {
    const gap = mountTab()
    await flushPromises()
    const primary = gap.findAll('.page-head button').find(node => node.text().includes('Draft'))
    expect(primary?.text()).toContain('Draft 1 finding')

    const covered = mountTab([{ id: 'F-1', test_refs: ['DAT-A'], rcm_refs: ['RCM-1'] }])
    await flushPromises()
    expect(covered.findAll('.page-head button').some(node => node.text().includes('Draft'))).toBe(false)
    expect(covered.findAll('.page-head button').some(node => node.text().includes('Run all'))).toBe(true)
  })
})

describe('DataTestsTab review bar', () => {
  it('draws at most six chips, none of them empty, one of them pressed', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const chips = wrapper.findAll('.review-bar .chip')

    expect(chips.length).toBeLessThanOrEqual(6)
    expect(chips.map(node => node.text())).toEqual([
      '3All tests', '1Exceptions open', '1Findings to draft', '1Measurement warnings',
      '1Agent-set, unread', '2No exception',
    ])
    expect(wrapper.findAll('.review-bar .chip[aria-pressed="true"]').map(node => node.text()))
      .toEqual(['3All tests'])
  })

  it('narrows the list to what a chip counts', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await wrapper.findAll('.review-bar .chip')[1].trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.list .title').map(node => node.text())).toEqual(['Test DAT-A'])
  })
})

describe('DataTestsTab list rows', () => {
  it('says which table, how much failed, and how much is still open', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.findAll('.list .meta').map(node => node.text().replace(/\s+/g, ' '))).toEqual([
      'invoice_data · 2 failed · 2 open',
      'invoice_data · not run',
      'invoice_data · not run · warning',
    ])
  })

  it('leads with the outcome where the definition names no table', async () => {
    // A generated Polars test names its tables inside its step code, so most
    // of an agent-written programme has no `table_refs` — and "no table" as
    // the first word of every row is the least useful line the list could
    // carry.
    tests[1].table_refs = []
    const wrapper = mountTab()
    await flushPromises()
    tests[1].table_refs = ['invoice_data']

    expect(wrapper.findAll('.list .meta')[1].text().replace(/\s+/g, ' ')).toBe('not run')
  })
})

describe('DataTestsTab verdict bar', () => {
  it('keeps what the run found apart from what is recorded, and says it once', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.get('.verdict-bar .found').text()).toContain('2 records failed')
    expect(wrapper.get('.verdict-bar .recorded').text())
      .toContain('Concluded Ineffective by an unattended run.')
    expect(wrapper.get('.verdict-bar .by-agent').text()).toBe('No auditor has read it.')
    // Exactly once: the old detail carried the run's reading in the status
    // chip, the headline, the rail and the Result block.
    expect(wrapper.findAll('.detail .found')).toHaveLength(1)
  })

  it('attaches the stale sentence rather than banner-ing it four ways', async () => {
    const wrapper = mountTab()
    await flushPromises()

    expect(wrapper.get('.verdict-bar .stale').text())
      .toContain('recorded against an earlier run')
    expect(wrapper.findAll('.detail .stale')).toHaveLength(1)
  })

  it('offers the run’s own reading for acceptance while nobody has taken it', async () => {
    const wrapper = mountTab()
    await flushPromises()

    const accept = wrapper.findAll('.verdict-bar button')
      .find(node => node.text().includes('Accept conclusion'))
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({})
    await accept!.trigger('click')
    await flushPromises()

    expect(patch.mock.calls[0]?.[0]).toBe('/api/workspaces/WS-1/data-tests/DAT-A')
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ control_conclusion: 'ineffective' })
  })

  it('records the conclusion on the test being edited, not on whatever the filter reselects', async () => {
    const wrapper = mountTab()
    await flushPromises()

    // Narrow to the tests with no conclusion, then pick the second one.
    await wrapper.getComponent({ name: 'UiReviewBar' }).vm.$emit('filter', ['no_conclusion'])
    await flushPromises()

    const row = wrapper.findAll('.list .row').find(node => node.text().includes('Test DAT-B'))!
    await row.trigger('click')
    await flushPromises()
    expect(wrapper.get('.detail-id').text()).toBe('DAT-B')

    // Conclude it: exactly what the auditor does behind Change.
    await wrapper.get('.change-form .select-stub').trigger('click')
    await flushPromises()

    // Choosing a value is a draft, so the narrowed list does not re-sort itself
    // out from under the record being written up.
    expect(wrapper.get('.detail-id').text()).toBe('DAT-B')
    expect(wrapper.findAll('.list .title').some(node => node.text() === 'Test DAT-B')).toBe(true)

    const patch = vi.spyOn(api, 'patch').mockResolvedValue({})
    await wrapper.findAll('.change-form button').find(node => node.text() === 'Save')!.trigger('click')
    await flushPromises()

    expect(patch).toHaveBeenCalledTimes(1)
    expect(patch.mock.calls[0]?.[0]).toBe('/api/workspaces/WS-1/data-tests/DAT-B')
    expect(patch.mock.calls[0]?.[1]).toMatchObject({ control_conclusion: 'ineffective' })
    wrapper.unmount()
  })
})
