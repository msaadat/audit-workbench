import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import AnalysisSummary from './AnalysisSummary.vue'
import { api } from '../../api'
import type { AnalysisLastResult, AnalysisMemo, SavedAnalysis, WorkspaceSummary } from '../../types'

/**
 * The summary is a written work product with a provenance story, so it takes
 * the memorandum's page. What is worth holding is what the full-bleed version
 * could not say: where to jump to, which sections report an exception, what it
 * was written from, and what it left out.
 */

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'ws-1' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

const MARKDOWN = [
  'The population is small — 52 invoices.',
  '',
  '## Data received and its limitations',
  '',
  'Six tables were received.',
  '',
  '## What the analysis found',
  '',
  '### 1. Invoices billed above approved amounts',
  '',
  'Three invoices exceed the linked PO total.',
  '',
  '```embed',
  'analysis: A-FAIL',
  'as: exception_table',
  'caption: 3 of 52 breach INVOICE_AMOUNT <= PO_TOTAL_AMOUNT',
  '```',
  '',
  '### 2. Nothing else',
  '',
  'Everything else was clean.',
].join('\n')

const workspace = { id: 'ws-1', name: 'Procurement', tables: [] } as unknown as WorkspaceSummary

function analysis(overrides: Partial<SavedAnalysis> = {}): SavedAnalysis {
  return {
    id: 'A-1', title: 'A procedure', kind: 'analytics', table: 'invoice_data', note: '',
    viz: {} as SavedAnalysis['viz'], source: 'ai', created: '',
    state: 'current', classification: 'clear',
    ...overrides,
  } as SavedAnalysis
}

const failing = analysis({
  id: 'A-FAIL',
  title: 'Invoiced amount exceeds the linked PO total',
  classification: 'exception',
  last_result: { exception_count: 3, population: 52, tested: 52 } as AnalysisLastResult,
})

function memo(overrides: Partial<AnalysisMemo> = {}): AnalysisMemo {
  return {
    markdown: MARKDOWN,
    cited_analysis_ids: ['A-FAIL'],
    generated_at: '2026-09-01T17:17:12+00:00',
    run_id: 'run-1',
    stale: false,
    ...overrides,
  }
}

function render(analyses: SavedAnalysis[], value: AnalysisMemo | null = memo()) {
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/memo')) {
      if (!value) throw new Error('none')
      return value as never
    }
    return {} as never
  })
  return mount(AnalysisSummary, {
    props: { workspace, analyses },
    global: {
      stubs: { MemoView: { props: ['entries', 'eyebrow'], template: '<div class="stub-memo" />' } },
      directives: { tooltip: {} },
    },
  })
}

afterEach(() => vi.restoreAllMocks())

describe('AnalysisSummary', () => {
  it('gives the memo an outline over the whole of it', async () => {
    const wrapper = render([failing, analysis()])
    await flushPromises()

    expect(wrapper.findAll('.outline .entry').map(node => node.text())).toEqual([
      'Data received and its limitations',
      'What the analysis found',
      '1. Invoices billed above approved amounts',
      '2. Nothing else',
    ])
  })

  /**
   * A section that rests on a result holding exceptions is dotted, as the
   * report dots the sections it has a problem with. The mark comes from the
   * embed fence rather than from the prose: a fence is the memo asserting that
   * this section rests on this result.
   */
  it('marks the sections that rest on an exception', async () => {
    const wrapper = render([failing, analysis()])
    await flushPromises()

    const marked = wrapper.findAll('.entry').filter(node => node.find('.mark').exists())
    expect(marked.map(node => node.text())).toEqual(['1. Invoices billed above approved amounts'])
  })

  it('says what it cites and what it does not', async () => {
    const wrapper = render([failing, analysis({ id: 'A-QUIET', title: 'Quiet one' })])
    await flushPromises()

    const cards = wrapper.findAll('.card')
    expect(cards[0].text()).toContain('1 of 2')
    expect(cards[0].text()).toContain('Invoiced amount exceeds the linked PO total')
    expect(cards[1].text()).toContain('Quiet one')
  })

  it('names an uncited procedure that found something as a gap in the account', async () => {
    const uncitedFail = analysis({
      id: 'A-LOOSE',
      classification: 'exception',
      last_result: { exception_count: 2 } as AnalysisLastResult,
    })
    const wrapper = render([failing, uncitedFail])
    await flushPromises()

    // A clean procedure's absence changes nothing; one that found something
    // and is not cited is a gap in the account, and the band says which it is.
    expect(wrapper.find('.verdict-bar').text()).toContain('1 procedure that found something')
    expect(wrapper.find('.verdict-bar').text()).toContain('is not cited')
  })

  /**
   * A stale memo is still shown: it is what the analysis concluded at the time,
   * and hiding it would lose that. The strip is what stops it reading as
   * current — attached to the band it qualifies, not floated above the page.
   */
  it('qualifies a summary whose procedures no longer stand', async () => {
    const wrapper = render([analysis({ id: 'A-STALE', state: 'stale' })])
    await flushPromises()

    expect(wrapper.find('.verdict-bar .stale').text())
      .toContain('1 procedure has no current result')
  })

  it('offers to write one where there is none', async () => {
    const wrapper = render([analysis()], null)
    await flushPromises()

    expect(wrapper.text()).toContain('No analysis summary yet')
    expect(wrapper.emitted('loaded')?.at(-1)).toEqual([false])
  })
})
