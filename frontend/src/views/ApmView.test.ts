import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueToast from 'primevue/usetoast'
import { describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { WorkspaceSummary } from '../types'
import ApmView from './ApmView.vue'

const PrimeVueToastSymbol = (
  PrimeVueToast as unknown as { PrimeVueToastSymbol: symbol }
).PrimeVueToastSymbol

vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ replace: vi.fn(), push: vi.fn() }),
}))
vi.mock('../composables/useAgentRun', async () => {
  const { ref } = await import('vue')
  return {
    useAgentRun: () => ({
      isActive: ref(false), launchMode: ref('auto'),
      state: { panelMode: 'closed', status: { configured: true } },
      openPanel: vi.fn(),
      togglePanel: vi.fn(), onWorkspaceInvalidated: () => () => undefined,
    }),
  }
})
vi.mock('../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({ state: { busy: false }, createChat: vi.fn(), send: vi.fn() }),
}))

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

const APM = [
  '# Audit Planning Memorandum',
  '## Engagement',
  'Entity: Global Bank.',
  '## Introduction and background',
  'The Board adopted a growth strategy.',
].join('\n\n')

const PROVENANCE = {
  state: 'attributed',
  artifact_ref: 'planning:apm',
  unit: { stage_title: 'Audit planning memorandum', title: 'APM', finished_at: '2026-09-01T17:22:00Z' },
  context: {
    state: 'available',
    supplied_size: { items: 20, characters: 38400, estimated_tokens: 9000 },
    selections: [
      { source_type: 'documents', source_id: 'documents', source_ref: 'document:d1' },
      { source_type: 'documents', source_id: 'documents', source_ref: 'document:d2' },
      { source_type: 'tables', source_id: 'tables', source_ref: 'table:invoice_data' },
      { source_type: 'templates', source_id: 'templates', source_ref: 'template:apm' },
    ],
    omissions: [
      { source_id: 'd9', source_ref: 'document:d9', source_hash: null, reason: 'did not match the selector' },
      { source_id: 'x1', source_ref: null, source_hash: null, reason: 'source unavailable' },
    ],
  },
  model: { model: 'deepseek-v4', usage: { calls: 1 } },
  receipt: { workspace_revision_after: 116 },
}

function planning(overrides: Record<string, unknown> = {}) {
  return {
    planning: {
      apm_markdown: APM, apm_sha1: 'aaa', created_by: 'agent',
      updated: '2026-09-01T17:25:00Z',
      cycle: { steps: [{}, {}, {}, {}], apm_sha1: 'aaa' },
      ...overrides,
    },
    rcm: [{ id: 'R1', process: 'Payables' }, { id: 'R2', process: 'Treasury' }],
  }
}

async function mountView(overrides: Record<string, unknown> = {}) {
  vi.spyOn(api, 'get').mockImplementation((url: string) => {
    if (url.includes('/provenance')) return Promise.resolve(PROVENANCE)
    if (url.endsWith('/documents')) {
      return Promise.resolve({ items: [
        { id: 'd1', title: 'Procurement SOP', file: 'sop.docx', category: 'policy' },
        { id: 'd2', title: '', file: 'minutes.docx', category: 'minutes' },
      ] })
    }
    return Promise.resolve(planning(overrides))
  })
  const wrapper = mount(ApmView, {
    props: { workspace: { id: 'WS-1', name: 'Procurement' } as WorkspaceSummary },
    global: {
      plugins: [PrimeVue],
      provide: { [PrimeVueToastSymbol]: { add: vi.fn() } },
      stubs: { MarkdownEditor: true },
    },
  })
  await flushPromises()
  return wrapper
}

describe('ApmView', () => {
  it('builds the outline from the memorandum headings', async () => {
    const wrapper = await mountView()

    expect(wrapper.findAll('.outline .entry').map(node => node.text())).toEqual([
      'Audit Planning Memorandum', 'Engagement', 'Introduction and background',
    ])
  })

  it('names the documents the step read, and groups the rest', async () => {
    const wrapper = await mountView()
    const rows = wrapper.findAll('.rail .row').map(node => node.text())

    expect(rows[0]).toContain('Procurement SOP')
    // A document with no title falls back to its filename rather than its id.
    expect(rows[1]).toContain('minutes.docx')
    // The manifest names groups in the plural; a count of one must not.
    expect(wrapper.text()).toContain('1 table')
    expect(wrapper.text()).toContain('1 template')
  })

  it('separates a source held out of scope from one that was unavailable', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain("outside this step's scope")
    expect(wrapper.text()).toContain('not available')
  })

  it('says who wrote it in the rail, where the rest of the provenance is', async () => {
    const wrapper = await mountView()
    const written = wrapper.findAll('.rail .card')
      .find(card => card.text().startsWith('Written'))

    expect(written?.text()).toContain('assistant')
    expect(written?.text()).toContain('Drafted')
    expect(written?.text()).toContain('Edited')
    // The band that used to say this across the top of the page is gone.
    expect(wrapper.find('.verdict-bar').exists()).toBe(false)
  })

  it('says nothing about staleness while the cycle matches the memorandum', async () => {
    expect((await mountView()).find('.feeds').text()).toContain('derived from this version')
  })

  it('warns on the card it feeds when the cycle came from an earlier version', async () => {
    const wrapper = await mountView({ cycle: { steps: [{}], apm_sha1: 'older' } })

    expect(wrapper.find('.feeds').text()).toContain('derived from an earlier version')
    expect(wrapper.find('.feeds').attributes('data-tone')).toBe('warn')
  })
})
