import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueConfirm from 'primevue/useconfirm'
import * as PrimeVueToast from 'primevue/usetoast'
import { afterEach, describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { DocTest, DocTestSummaryPayload, WorkspaceSummary } from '../types'
import DocTestsTab from './DocTestsTab.vue'

const PrimeVueConfirmSymbol = (
  PrimeVueConfirm as unknown as { PrimeVueConfirmSymbol: symbol }
).PrimeVueConfirmSymbol
const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

const { navReplace, routeState } = vi.hoisted(() => ({
  navReplace: vi.fn(),
  routeState: { query: { test: 'DT-CYCLE' } as Record<string, string>, params: { id: 'WS-1' } },
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
      state: { panelMode: 'closed' },
      openPanel: vi.fn(),
      togglePanel: vi.fn(),
      onWorkspaceInvalidated: () => () => undefined,
    }),
  }
})

const { assistantSend } = vi.hoisted(() => ({ assistantSend: vi.fn() }))

vi.mock('../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({
    state: { busy: false },
    createChat: vi.fn(),
    send: assistantSend,
  }),
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

/** A kebab that renders its items, so what the menu offers can be asserted. */
const OverflowMenuStub = {
  props: ['items', 'tooltip'],
  template: '<div class="overflow"><button v-for="item in items" :key="item.label"'
    + ' :disabled="item.disabled || undefined" @click="item.command && item.command()">'
    + '{{ item.label }}</button></div>',
}

vi.mock('./doc-tests/DocTestItemDetail.vue', () => ({
  default: {
    props: ['item', 'focusAssertionKey'],
    template: '<div class="item-detail">{{ item.id }} · {{ focusAssertionKey }}</div>',
  },
}))

vi.mock('primevue/inputtext', () => ({
  default: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
}))

const summary: DocTestSummaryPayload = {
  entry_counts: { exception: 0, needs_review: 1, awaiting_evidence: 0, confirmed: 0, not_run: 0 },
  test_counts: { vouching: 0, attribute: 0, review: 0, qa: 0, cycle_vouch: 1, total: 1, item_first: 0 },
  tested_item_counts: {
    total: 1, executed: 1, passed: 0, failed: 1, incomplete: 0, needs_review: 0,
    not_run: 0, stale: 0, confirmed: 0, exceptions: 0, pending_disposition: 1,
  },
  assertion_counts: {
    match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
    ambiguous: 0, not_run: 0, total: 1,
  },
  entries: [{
    entry_type: 'cycle_test',
    test_id: 'DT-CYCLE',
    title: 'Payroll payment cycle',
    test_kind: 'cycle_vouch',
    test_status: 'completed_with_exception',
    rcm_id: 'RCM-1',
    classification: 'needs_review',
    conclusion_state: 'none',
    item_count: 1,
    tested_item_count: 1,
    evaluation_counts: { failed: 1 },
    disposition_counts: { pending: 1 },
    assertion_columns: 1,
    assertion_counts: {
      match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
      ambiguous: 0, not_run: 0, total: 1,
    },
    coverage: { selected_rows: 1 },
    selection_basis: 'evidence_linked',
    assurance_scope: 'targeted_evidence_only',
    assurance_label: 'Targeted evidence - not a sample',
    requirement_refs: ['RCM-1:payment'],
    updated: '2026-08-09T00:00:00Z',
  }],
}

const grid = {
  test_id: 'DT-CYCLE', test_sha1: 'sha1:test', definition_sha1: 'sha1:def',
  title: 'Payroll payment cycle',
  population: {
    candidate_id: 'CAND-1', selection_reason: 'Evidence linked', table: 'payroll_register',
    row_key: { column: 'PAYMENT_ID', identifier_kind: 'payroll.payment_id' },
    cycle_keys: [], selection: { mode: 'evidence_linked', assurance_scope: 'targeted_evidence_only' },
  },
  coverage: { selected_rows: 1 }, selection_basis: 'evidence_linked',
  assurance_scope: 'targeted_evidence_only', assurance_label: 'Targeted evidence - not a sample',
  tested_item_counts: { failed: 1 },
  assertion_counts: { match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0, ambiguous: 0, not_run: 0, total: 1 },
  columns: [{
    key: 'amount', label: 'Amount agrees', operator: 'numeric_within', applicable_roles: ['payslip'],
    counts: { match: 0, mismatch: 1, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0, ambiguous: 0, not_run: 0 },
  }],
  rows: [{
    item_id: 'ITEM-1', label: 'PAY-001', evaluation_state: 'failed', disposition_state: 'pending',
    disposition_stale: false, roles_present: ['payslip'], missing_roles: [], shared_record_facts: [],
    cells: { amount: { verdict: 'mismatch', display: '1,000 vs 900', comparison_count: 1, evidence_count: 1, comparisons: [] } },
  }],
  page: { offset: 0, limit: 100, total: 1 }, truncated: false,
}

const detail = {
  id: 'DT-CYCLE', kind: 'cycle_vouch', title: 'Payroll payment cycle', status: 'completed_with_exception',
  semantic_id: 'cycle', rcm_refs: ['RCM-1'], procedure_refs: [], rcm_id: 'RCM-1', spec: {}, steps: [],
  items: [{
    id: 'ITEM-1', label: 'PAY-001', instruction: 'Compare payment evidence', document_ids: [], evidence_refs: [],
    result_by_assertion: {}, evaluation: { state: 'failed', definition_sha1: 'sha1:def' },
    disposition: { state: 'pending', evaluated_definition_sha1: null, stale: false },
  }],
  created: '2026-08-09T00:00:00Z', updated: '2026-08-09T00:00:00Z', sha1: 'sha1:test',
} as unknown as DocTest

afterEach(() => {
  vi.restoreAllMocks()
  navReplace.mockReset()
  assistantSend.mockReset()
  routeState.query = { test: 'DT-CYCLE' }
})

describe('DocTestsTab Cycle vouch navigation', () => {
  it('opens the grid first, then loads canonical item detail only after cell drill-down', async () => {
    const get = vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/summary')) return summary
      if (url.endsWith('/documents')) return { items: [] }
      if (url.endsWith('/planning')) return { findings: [] }
      if (url.endsWith('/doc-tests/meta')) return { document_types: [], cycle_vouch: {} }
      if (url.includes('/doc-tests/DT-CYCLE/grid?')) return grid
      if (url.endsWith('/doc-tests/DT-CYCLE')) return detail
      throw new Error(`Unexpected URL: ${url}`)
    })
    const wrapper = mount(DocTestsTab, {
      attachTo: document.body,
      props: { workspace: { id: 'WS-1', name: 'Fixture' } as WorkspaceSummary },
      global: {
        provide: {
          [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
          [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
        },
        plugins: [PrimeVue],
        directives: { tooltip: () => undefined },
        stubs: {
          EvidenceAnchorDialog: true,
          UiOverflowMenu: OverflowMenuStub,
        },
      },
    })
    await flushPromises()

    const urlsBeforeDetail = get.mock.calls.map(([url]) => url)
    expect(urlsBeforeDetail).toContain('/api/workspaces/WS-1/doc-tests/DT-CYCLE/grid?offset=0&limit=100')
    expect(urlsBeforeDetail).not.toContain('/api/workspaces/WS-1/doc-tests/DT-CYCLE')

    await wrapper.get<HTMLInputElement>('input[type="search"]').setValue('PAY-001')
    const scroll = wrapper.get<HTMLElement>('.grid-scroll').element
    scroll.scrollLeft = 280
    scroll.scrollTop = 90
    await wrapper.get('button[aria-label="Amount agrees for PAY-001: Mismatch"]').trigger('click')
    await wrapper.get('button[aria-label="Open assertion evidence"]').trigger('click')
    await flushPromises()

    expect(get.mock.calls.map(([url]) => url)).toContain('/api/workspaces/WS-1/doc-tests/DT-CYCLE')
    expect(wrapper.get('.item-detail').text()).toContain('ITEM-1 · amount')

    await wrapper.get('button[aria-label="Back to Cycle vouch grid"]').trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLInputElement>('input[type="search"]').element.value).toBe('PAY-001')
    expect(wrapper.get<HTMLElement>('.grid-scroll').element.scrollLeft).toBe(280)
    expect(wrapper.get<HTMLElement>('.grid-scroll').element.scrollTop).toBe(90)
    expect(wrapper.get('.assertion-cell').classes()).toContain('selected')
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Amount agrees for PAY-001: Mismatch')
    wrapper.unmount()
  })

  it('keeps question worklists item-first and never requests the cycle grid', async () => {
    routeState.query = { test: 'DT-QA', item: 'ITEM-QA' }
    const qaSummary: DocTestSummaryPayload = {
      entry_counts: { exception: 0, needs_review: 0, awaiting_evidence: 0, confirmed: 0, not_run: 1 },
      test_counts: { vouching: 0, attribute: 0, review: 0, qa: 1, cycle_vouch: 0, total: 1, item_first: 1 },
      tested_item_counts: {
        total: 0, executed: 0, passed: 0, failed: 0, incomplete: 0, needs_review: 0,
        not_run: 0, stale: 0, confirmed: 0, exceptions: 0, pending_disposition: 0,
      },
      assertion_counts: {
        match: 0, mismatch: 0, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
        ambiguous: 0, not_run: 0, total: 0,
      },
      entries: [{
        entry_type: 'item', test_id: 'DT-QA', test_title: 'Contract questions', test_kind: 'qa',
        test_status: 'ready', rcm_id: null, item_id: 'ITEM-QA', label: 'Approval clause',
        instruction: 'Read the approval clause', state: 'pending', classification: 'not_run',
        conclusion_state: 'none',
        evaluation: { state: 'not_run', note: '', input_sha1: null, ran_at: null },
        disposition: {
          state: 'pending', note: '', actor: null, at: null,
          evaluated_input_sha1: null, stale: false,
        },
        question: 'Was approval required?', response: '', runner_note: '', document_count: 1,
        citation_count: 0, evidence_count: 1, checks_total: 0, checks_matched: 0, checks_failed: 0,
        missing_document_types: [], image_only: false, evidence_request_count: 0, has_conflict: false,
        updated: '2026-08-09T00:00:00Z',
      }],
    }
    const qaDetail = {
      ...detail,
      id: 'DT-QA', kind: 'qa', title: 'Contract questions', status: 'ready',
      items: [{
        id: 'ITEM-QA', label: 'Approval clause', instruction: 'Read the approval clause',
        question: 'Was approval required?', state: 'pending', document_ids: [], evidence_refs: [],
      }],
    } as unknown as DocTest
    const get = vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/summary')) return qaSummary
      if (url.endsWith('/documents')) return { items: [] }
      if (url.endsWith('/planning')) return { findings: [] }
      if (url.endsWith('/doc-tests/meta')) return { document_types: [], cycle_vouch: {} }
      if (url.endsWith('/doc-tests/DT-QA')) return qaDetail
      throw new Error(`Unexpected URL: ${url}`)
    })
    const wrapper = mount(DocTestsTab, {
      props: { workspace: { id: 'WS-1', name: 'Fixture' } as WorkspaceSummary },
      global: {
        provide: {
          [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
          [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
        },
        plugins: [PrimeVue],
        directives: { tooltip: () => undefined },
        stubs: {
          EvidenceAnchorDialog: true,
          UiOverflowMenu: OverflowMenuStub,
          InputText: {
            props: ['modelValue'],
            emits: ['update:modelValue'],
            template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
          },
        },
      },
    })
    await flushPromises()

    expect(get.mock.calls.map(([url]) => url)).toContain('/api/workspaces/WS-1/doc-tests/DT-QA')
    expect(get.mock.calls.some(([url]) => url.includes('/grid?'))).toBe(false)
    expect(wrapper.get('.item-detail').text()).toContain('ITEM-QA')
  })
})

describe('DocTestsTab filtering', () => {
  function itemEntry(
    suffix: string,
    conclusion_state: 'none' | 'stale' | 'agent' | 'auditor',
  ) {
    return {
      entry_type: 'item' as const, test_id: `DT-${suffix}`, test_title: `Test ${suffix}`,
      test_kind: 'qa' as const, test_status: 'completed_with_exception' as const, rcm_id: null,
      item_id: `ITEM-${suffix}`, label: `Item ${suffix}`, instruction: 'Read it',
      state: 'exception' as const, classification: 'exception' as const, conclusion_state,
      evaluation: { state: 'failed' as const, note: '', input_sha1: null, ran_at: null },
      disposition: {
        state: 'exception' as const, note: '', actor: null, at: null,
        evaluated_input_sha1: null, stale: false,
      },
      question: '', response: '', runner_note: '', document_count: 0, citation_count: 0,
      evidence_count: 0, checks_total: 0, checks_matched: 0, checks_failed: 0,
      missing_document_types: [], image_only: false, evidence_request_count: 0,
      has_conflict: false, updated: '2026-08-09T00:00:00Z',
    }
  }
  const mixedSummary: DocTestSummaryPayload = {
    entry_counts: { exception: 2, needs_review: 0, awaiting_evidence: 0, confirmed: 1, not_run: 0 },
    test_counts: { vouching: 0, attribute: 0, review: 0, qa: 3, cycle_vouch: 0, total: 3, item_first: 3 },
    tested_item_counts: {
      total: 3, executed: 3, passed: 1, failed: 2, incomplete: 0, needs_review: 0,
      not_run: 0, stale: 0, confirmed: 1, exceptions: 2, pending_disposition: 0,
    },
    assertion_counts: {
      match: 0, mismatch: 0, cannot_determine: 0, missing_evidence: 0, invalid_extraction: 0,
      ambiguous: 0, not_run: 0, total: 0,
    },
    entries: [
      itemEntry('OPEN', 'none'),
      itemEntry('SIGNED', 'auditor'),
      { ...itemEntry('CLEAN', 'none'), classification: 'confirmed', state: 'confirmed' },
    ],
  }

  function mountTab() {
    routeState.query = {}
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/summary')) return mixedSummary
      if (url.endsWith('/documents')) return { items: [] }
      if (url.endsWith('/planning')) return { findings: [] }
      if (url.endsWith('/doc-tests/meta')) return { document_types: [], cycle_vouch: {} }
      if (url.includes('/doc-tests/DT-')) {
        return { ...detail, id: url.split('/').pop(), kind: 'qa', items: [] } as unknown as DocTest
      }
      throw new Error(`Unexpected URL: ${url}`)
    })
    return mount(DocTestsTab, {
      props: { workspace: { id: 'WS-1', name: 'Fixture' } as WorkspaceSummary },
      global: {
        provide: {
          [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
          [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
        },
        plugins: [PrimeVue],
        directives: { tooltip: () => undefined },
        stubs: {
          EvidenceAnchorDialog: true,
          UiOverflowMenu: OverflowMenuStub,
        },
      },
    })
  }

  /** The chips stand permanently: they are the page's filter row. */
  function chip(wrapper: ReturnType<typeof mountTab>, label: string) {
    return wrapper.findAll('.review-bar .chip').find(item => item.text().endsWith(label))
  }
  function menuRow(wrapper: ReturnType<typeof mountTab>, label: string) {
    return wrapper.findAll('.review-bar .menu .row').find(item => item.text().includes(label))
  }
  function titles(wrapper: ReturnType<typeof mountTab>) {
    return wrapper.findAll('.item-list .title').map(item => item.text())
  }

  it('draws at most six chips, none of them empty, one of them pressed', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const chips = wrapper.findAll('.review-bar .chip')

    expect(chips.length).toBeLessThanOrEqual(6)
    expect(chips.map(item => item.text()))
      .toEqual(['3All items', '2Exception', '1Confirmed'])
    expect(wrapper.findAll('.review-bar .chip[aria-pressed="true"]').map(item => item.text()))
      .toEqual(['3All items'])
  })

  it('narrows the worklist to exceptions nobody has concluded on', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(titles(wrapper)).toEqual(['Item OPEN', 'Item SIGNED', 'Item CLEAN'])

    await chip(wrapper, 'Exception')!.trigger('click')
    expect(titles(wrapper)).toEqual(['Item OPEN', 'Item SIGNED'])

    // Execution and conclusion are separate axes, so the two narrowings hold
    // at once rather than replacing each other. The rest of the vocabulary is
    // behind the pressed chip.
    await menuRow(wrapper, 'Not concluded')!.trigger('click')
    expect(titles(wrapper)).toEqual(['Item OPEN'])
  })

  it('replaces a narrowing with another from the same axis', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await chip(wrapper, 'Exception')!.trigger('click')
    expect(titles(wrapper)).toEqual(['Item OPEN', 'Item SIGNED'])

    // An item is not both an exception and confirmed, so the second wins
    // rather than leaving an empty worklist.
    await chip(wrapper, 'Confirmed')!.trigger('click')
    expect(titles(wrapper)).toEqual(['Item CLEAN'])
  })

  it('goes back to the whole worklist from the All chip', async () => {
    const wrapper = mountTab()
    await flushPromises()

    await chip(wrapper, 'Exception')!.trigger('click')
    await chip(wrapper, 'All items')!.trigger('click')
    expect(titles(wrapper)).toEqual(['Item OPEN', 'Item SIGNED', 'Item CLEAN'])
  })
})

describe('DocTestsTab finding generation', () => {
  function mountTab(findings: unknown[]) {
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/doc-tests/summary')) return summary
      if (url.endsWith('/documents')) return { items: [] }
      if (url.endsWith('/planning')) return { findings }
      if (url.endsWith('/doc-tests/meta')) return { document_types: [], cycle_vouch: {} }
      if (url.includes('/doc-tests/DT-CYCLE/grid?')) return grid
      if (url.endsWith('/doc-tests/DT-CYCLE')) return detail
      throw new Error(`Unexpected URL: ${url}`)
    })
    return mount(DocTestsTab, {
      props: { workspace: { id: 'WS-1', name: 'Fixture' } as WorkspaceSummary },
      global: {
        provide: {
          [PrimeVueToastSymbol as symbol]: { add: vi.fn() },
          [PrimeVueConfirmSymbol as symbol]: { require: vi.fn() },
        },
        plugins: [PrimeVue],
        directives: { tooltip: () => undefined },
        stubs: {
          EvidenceAnchorDialog: true,
          UiOverflowMenu: OverflowMenuStub,
        },
      },
    })
  }

  it('offers the exception tests that have no finding yet and drafts their RCM rows', async () => {
    const wrapper = mountTab([])
    await flushPromises()

    // The write-up is offered on each test's own footer row, where the
    // exception is read. The batch is the shortcut, so it sits in the kebab
    // rather than taking the header's one primary slot from Prepare.
    const button = wrapper.findAll('.overflow button')
      .find(item => item.text().includes('Draft'))
    expect(button?.text()).toBe('Draft 1 finding')

    await button!.trigger('click')
    await flushPromises()

    expect(assistantSend.mock.calls[0][3]).toMatchObject({
      command: 'draft_findings',
      runContext: { rcm_ids: ['RCM-1'] },
    })
  })

  it('offers nothing to draft once every exception test has a finding', async () => {
    const wrapper = mountTab([{ id: 'F-1', test_refs: ['DT-CYCLE'], rcm_refs: ['RCM-1'] }])
    await flushPromises()

    expect(wrapper.findAll('button').some(item => item.text().includes('Draft'))).toBe(false)
    // The meter still reports it, without a sentence claiming credit for it.
    expect(wrapper.findAll('.review-bar .meter-label').map(item => item.text()))
      .toContain('Findings 1/1')
  })
})
