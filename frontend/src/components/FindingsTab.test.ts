import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import { describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { AuditFinding, FindingsPayload, WorkspaceSummary } from '../types'
import FindingsTab from './FindingsTab.vue'

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
  useWorkspaceNav: () => ({ replace: navReplace, push: vi.fn() }),
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

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

// The real Select binds a matchMedia listener on mount, which jsdom has not
// implemented; the severity picker only needs to hold a value here.
vi.mock('primevue/select', () => ({
  default: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<button class="select-stub" @click="$emit(\'update:modelValue\', \'critical\')">{{ modelValue }}</button>',
  },
}))

/** A Popover that renders inline, so what it holds is reachable in the DOM. */
vi.mock('primevue/popover', () => ({
  default: {
    template: '<div class="popover"><slot /></div>',
    methods: { toggle() {}, hide() {} },
  },
}))

function finding(id: string, overrides: Partial<AuditFinding> = {}): AuditFinding {
  return {
    id, semantic_id: id, created_by: 'agent', agent_run_id: null,
    title: `Finding ${id}`, severity: 'high',
    narrative: '## Condition\n\nApprovals exceeded the limit.\n\n## Root Cause\n\n',
    management_response: 'Management accepts the point.',
    rcm_refs: ['RCM-1'], test_refs: ['DAT-1'], procedure_refs: [],
    execution_refs: ['datatest:DAT-1'],
    evidence_refs: [{ id: 'EV-1', source_kind: 'datatest', source_id: 'DAT-1', source_sha1: 'b20d295d99' }],
    evidence_warnings: [],
    cause_pending: false, auditor_confirmed: true, source: 'agent',
    created: '2026-09-01T19:02:00Z', updated: '2026-09-03T11:54:00Z',
    ...overrides,
  } as AuditFinding
}

function payload(items: AuditFinding[]): FindingsPayload {
  return {
    items,
    rcm: [{ id: 'RCM-1', risk: 'Commitments made outside delegated authority' }],
    procedures: [], data_tests: [], document_tests: [],
    rollups: { by_rcm: {}, by_procedure: {} },
    evidence_options: [],
  } as unknown as FindingsPayload
}

const workspace = { id: 'WS-1', name: 'Procurement' } as WorkspaceSummary

async function mountTab(items: AuditFinding[]) {
  vi.spyOn(api, 'get').mockResolvedValue(payload(items))
  const wrapper = mount(FindingsTab, {
    props: { workspace },
    global: {
      plugins: [PrimeVue],
      provide: {
        [PrimeVueToastSymbol]: { add: vi.fn() },
        [PrimeVueConfirmSymbol]: { require: vi.fn(), close: vi.fn() },
      },
      stubs: { EvidenceAnchorDialog: true, ProvenanceRail: true, MarkdownEditor: true },
    },
  })
  await flushPromises()
  return wrapper
}

describe('FindingsTab', () => {
  it('leads with what the register holds and what the report can carry', async () => {
    const wrapper = await mountTab([
      finding('F-1', { severity: 'critical', rcm_refs: [] }),
      finding('F-2'),
    ])

    expect(wrapper.find('.headline').text())
      .toBe('2 findings · 1 critical · 1 high · 1 in the report')
  })

  it('groups the list by severity and says what each finding still owes', async () => {
    const wrapper = await mountTab([
      finding('F-1', { severity: 'critical', rcm_refs: [], cause_pending: true }),
      finding('F-2'),
    ])

    expect(wrapper.findAll('.group .severity').map(node => node.text()))
      .toEqual(['critical', 'high'])
    const rows = wrapper.findAll('.row .meta')
    expect(rows[0].text()).toContain('F-1')
    expect(rows[0].text()).toContain('no risk')
    expect(rows[0].text()).toContain('cause pending')
    // A finding owing nothing says only its id.
    expect(rows[1].text().replace(/\s+/g, ' ').trim()).toBe('F-2')
  })

  it('renders the narrative as the document the report copies, not an editor', async () => {
    const wrapper = await mountTab([finding('F-1')])

    expect(wrapper.findAll('.narrative h3').map(node => node.text()))
      .toEqual(['Condition', 'Root Cause'])
    expect(wrapper.find('.narrative').text()).toContain('Approvals exceeded the limit.')
    expect(wrapper.findComponent({ name: 'MarkdownEditor' }).exists()).toBe(false)
  })

  it('states the deferred root cause on the section it defers, and opens the editor there', async () => {
    const wrapper = await mountTab([finding('F-1', { cause_pending: true })])

    const pending = wrapper.find('.narrative .pending')
    expect(pending.text()).toContain('The report will carry this section empty')

    await pending.find('button').trigger('click')
    expect(wrapper.findComponent({ name: 'MarkdownEditor' }).exists()).toBe(true)
  })

  it('says once, on the verdict bar, what keeps a confirmed finding out of the report', async () => {
    const wrapper = await mountTab([finding('F-1', { rcm_refs: [], cause_pending: true })])

    const recorded = wrapper.find('.verdict-bar .recorded').text()
    expect(recorded).toContain('Left out of the report until it is supported')
    expect(recorded).toContain('not linked to a risk')
    expect(recorded).toContain('root cause pending')
    // The checkboxes the confirmation used to live in are gone.
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)
  })

  it('writes a confirmation as soon as it is made', async () => {
    const patch = vi.spyOn(api, 'patch').mockResolvedValue(finding('F-1'))
    const wrapper = await mountTab([finding('F-1', { auditor_confirmed: false })])

    expect(wrapper.find('.verdict-bar .found').text()).toBe('Not confirmed for reporting')
    const confirmButton = wrapper.findAll('.verdict-bar button')
      .find(node => node.text().includes('Confirm for reporting'))
    await confirmButton?.trigger('click')
    await flushPromises()

    expect(patch).toHaveBeenCalledWith(
      '/api/workspaces/WS-1/findings/F-1',
      expect.objectContaining({ auditor_confirmed: true }),
    )
  })

  it('offers the re-affirm beside the drift it settles, and re-pins the anchor', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue(finding('F-1'))
    const wrapper = await mountTab([finding('F-1', {
      evidence_warnings: ["Evidence source 'datatest:DAT-1' has changed since this finding was drafted."],
    })])

    expect(wrapper.find('.verdict-bar .stale').text())
      .toContain('has changed since the narrative was drafted')
    expect(wrapper.find('.card-row .pill').text()).toBe('changed')

    const reaffirm = wrapper.findAll('.verdict-bar button')
      .find(node => node.text().includes('Re-affirm'))
    await reaffirm?.trigger('click')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/workspaces/WS-1/findings/F-1/evidence/reaffirm')
  })

  it('names the missing risk where the link is made', async () => {
    const linked = await mountTab([finding('F-1')])
    expect(linked.find('.rail .card-id').text()).toBe('RCM-1')
    expect(linked.find('.rail .missing').exists()).toBe(false)

    const unlinked = await mountTab([finding('F-1', { rcm_refs: [] })])
    expect(unlinked.find('.rail .missing').text())
      .toContain('The report cannot place this finding in a process')
  })
})
