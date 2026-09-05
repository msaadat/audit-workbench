import { flushPromises, mount } from '@vue/test-utils'
import * as PrimeVueToast from 'primevue/usetoast'
import { describe, expect, it, vi } from 'vitest'

import PrimeVue from 'primevue/config'

import { api } from '../api'
import type { AuditReport, WorkspaceSummary } from '../types'
import ReportView from './ReportView.vue'

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

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

const MARKDOWN = [
  '# Internal Audit Report',
  'A review of procurement.',
  '## A. Executive Summary',
  'F-0571DE and F-43AEAB were reported.',
  '### 3. Audit Conclusion',
  'Marginal.',
  '## B. Detailed Findings',
  '### One', '### Two', '### Three', '### Four', '### Five',
].join('\n\n')

const ISSUES = [
  { code: 'preliminary_label_missing', severity: 'error', message: 'Open fieldwork remains.', refs: [], source: 'deterministic' },
  { code: 'report_rating_unsupported', severity: 'error', message: 'No rating can be assigned.', refs: [], source: 'deterministic' },
  { code: 'finding_draft', severity: 'error', message: 'F-0571DE remains a draft.', refs: ['finding:F-0571DE'], source: 'deterministic' },
  { code: 'unsupported_finding', severity: 'error', message: 'F-0571DE lacks support.', refs: ['finding:F-0571DE'], source: 'deterministic' },
  { code: 'stale_evidence', severity: 'warning', message: 'F-43AEAB evidence moved.', refs: ['finding:F-43AEAB'], source: 'deterministic' },
]

function reportPayload(): AuditReport {
  return {
    markdown: MARKDOWN, generated_markdown: '', generated_at: '2026-09-03T12:01:00Z',
    generated_by_run: null, edited: false, updated: null, generation_warnings: ['One note.'],
    quality: {
      checked_at: '2026-09-05T06:47:00Z', issues: ISSUES, ok: false,
      counts: { error: 4, warning: 1, info: 0 },
    },
  } as unknown as AuditReport
}

async function mountView() {
  vi.spyOn(api, 'get').mockImplementation((url: string) => {
    if (url.endsWith('/report')) return Promise.resolve(reportPayload())
    if (url.endsWith('/report/context')) {
      return Promise.resolve({
        statistics: { rcm_rows: 32, tests: 4, findings: 0, draft_findings: 18 },
        scope_limitations: [], draft_findings_excluded: ['F-0571DE'],
        preliminary: true, completion: { coverage: { rows_without_tests: ['R1', 'R2'] } },
      })
    }
    return Promise.resolve({
      items: [
        { id: 'F-0571DE', title: 'One', rcm_refs: [], test_refs: ['T'], evidence_refs: [{ id: 'E' }], management_response: 'x', cause_pending: false, auditor_confirmed: false },
        { id: 'F-43AEAB', title: 'Two', rcm_refs: ['R'], test_refs: ['T'], evidence_refs: [{ id: 'E' }], evidence_warnings: ['moved'], management_response: 'x', cause_pending: false, auditor_confirmed: true },
      ],
    })
  })
  const wrapper = mount(ReportView, {
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

describe('ReportView', () => {
  it('builds the outline from the headings and folds the detailed findings', async () => {
    const wrapper = await mountView()
    const entries = wrapper.findAll('.outline .entry').map(node => node.text())

    expect(entries.slice(0, 3)).toEqual([
      'Internal Audit Report', 'A. Executive Summary', '3. Audit Conclusion',
    ])
    expect(entries).toContain('… 2 more')
    // The excluded findings are marked where the reader meets them.
    expect(wrapper.find('.outline .badge').text()).toBe('excluded')
  })

  it('attaches each report issue to the heading it is about', async () => {
    const wrapper = await mountView()
    const strips = wrapper.findAll('.issue-strip')

    expect(strips).toHaveLength(2)
    // The whole-draft fact sits under the title; the rating under the conclusion.
    const title = wrapper.find('#internal-audit-report').element.parentElement
    expect(title?.querySelector('.issue-strip')?.textContent)
      .toContain('not labelled as a preliminary draft')
    expect(strips[1].text()).toContain('asserts a rating nothing supports')
  })

  it('lists one row per finding, not one per issue', async () => {
    const wrapper = await mountView()
    const rows = wrapper.findAll('.issue-row.finding')

    // Three finding-scoped issues over two findings.
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('F-0571DE')
    // The words are the register's own.
    expect(rows[0].text()).toContain('no risk')
    expect(rows[1].text()).toContain('evidence moved')
  })

  it('leads the rail with what the draft is allowed to claim', async () => {
    const wrapper = await mountView()

    // The band across the top is gone; the counts are on the rail's own rows,
    // and the one fact nothing else states leads the column.
    expect(wrapper.find('.verdict-bar').exists()).toBe(false)
    expect(wrapper.find('.preliminary').text()).toContain('2 risks have no test run')
    expect(wrapper.text()).toContain('About the report · 2')
    expect(wrapper.text()).toContain('Findings it cannot include · 2')
  })

  it('says who generated it and whether anyone has edited it since', async () => {
    const written = (await mountView()).findAll('.card').find(card => card.text().startsWith('Written'))

    expect(written?.text()).toContain('assistant')
    expect(written?.text()).toContain('not since')
  })
})
