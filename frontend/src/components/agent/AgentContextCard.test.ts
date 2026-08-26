import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { ContextRead } from '../../types'
import AgentContextCard from './AgentContextCard.vue'

/**
 * The read step, as files. These pin what a reader can see at a glance: which
 * documents the work rests on, and that the ones left out were a scope
 * decision rather than a gap.
 */

const push = vi.fn()
vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ push }),
}))

/** The record `narration.context_read` produces for the procurement APM. */
function context(overrides: Partial<ContextRead> = {}): ContextRead {
  return {
    at: '2026-08-14T06:01:00+00:00',
    stage_title: 'Audit planning memorandum',
    documents: [
      { document_id: 'd1', name: 'Procurement SOP Extracts.docx', category: 'policy', pages: 1 },
      { document_id: 'd2', name: 'Financial Approval Matrix.docx', category: 'policy', pages: 1 },
      { document_id: 'd3', name: 'Minutes of Meeting - CFO.docx', category: 'minutes', pages: 2 },
    ],
    withheld: [
      { document_id: 'v1', name: 'PO2024004_Purchase_Order.pdf', category: 'voucher', pages: 1 },
      { document_id: 'v2', name: 'GRN2024004_Signed_Receipt.pdf', category: 'voucher', pages: 1 },
    ],
    supporting: ['the planning context', 'the APM template'],
    unavailable: ['the methodology pack'],
    sentence: 'Reading 3 documents for Audit planning memorandum: …',
    ...overrides,
  }
}

describe('AgentContextCard', () => {
  it('names every document it read, as a file', () => {
    const wrapper = mount(AgentContextCard, { props: { context: context() } })

    const names = wrapper.findAll('.doc:not(.held-doc) .identity b').map(node => node.text())
    expect(names).toEqual([
      'Procurement SOP Extracts.docx',
      'Financial Approval Matrix.docx',
      'Minutes of Meeting - CFO.docx',
    ])
    // A file reads as a file before it is read at all.
    expect(wrapper.findAll('.doc:not(.held-doc) .badge').map(n => n.text())).toEqual([
      'DOC', 'DOC', 'DOC',
    ])
  })

  it('shows what was held back as a scope decision, by kind', () => {
    const wrapper = mount(AgentContextCard, { props: { context: context() } })

    expect(wrapper.text()).toContain('Held back')
    expect(wrapper.text()).toContain("2 vouchers — outside this step's scope")
    expect(wrapper.findAll('.held-doc')).toHaveLength(2)
    expect(wrapper.findAll('.held-doc .badge').map(n => n.text())).toEqual(['PDF', 'PDF'])
    // Never phrased as a failure.
    expect(wrapper.text()).not.toContain('did not match')
    expect(wrapper.text()).not.toContain('unsupported')
  })

  it('falls back to a plain count when the held-back kinds are mixed', () => {
    const wrapper = mount(AgentContextCard, {
      props: {
        context: context({
          withheld: [
            { document_id: 'v1', name: 'A.pdf', category: 'voucher', pages: 1 },
            { document_id: 'c1', name: 'B.docx', category: 'contract', pages: 1 },
          ],
        }),
      },
    })

    expect(wrapper.text()).toContain('2 documents')
  })

  it('keeps templates and plumbing in a quiet line, not on cards', () => {
    const wrapper = mount(AgentContextCard, { props: { context: context() } })

    const footer = wrapper.find('.footer').text()
    expect(footer).toContain('Also the planning context, the APM template.')
    expect(footer).toContain('The methodology pack was not available.')
    // Five cards, not seven: nobody needs a card for a template.
    expect(wrapper.findAll('.doc')).toHaveLength(5)
  })

  it('opens the document it names', async () => {
    const wrapper = mount(AgentContextCard, { props: { context: context() } })

    await wrapper.findAll('.doc')[0].trigger('click')

    expect(push).toHaveBeenCalledWith('documents', { doc: 'd1' })
  })

  it('gives assistive technology the sentence and each card a real label', () => {
    const wrapper = mount(AgentContextCard, { props: { context: context() } })

    expect(wrapper.find('section').attributes('aria-label')).toBe(context().sentence)
    // Focusable controls must be announced, never aria-hidden.
    expect(wrapper.find('.doc').attributes('aria-hidden')).toBeUndefined()
    expect(wrapper.find('.doc').attributes('aria-label')).toBe(
      'Open Procurement SOP Extracts.docx, Policy',
    )
  })
})

describe('AgentContextCard merged reads', () => {
  it('describes itself when a merged group has no single sentence', () => {
    const wrapper = mount(AgentContextCard, {
      props: { context: context({ sentence: '', stage_title: 'Document chunk analysis' }) },
    })

    expect(wrapper.find('section').attributes('aria-label')).toBe(
      "Reading 3 documents for Document chunk analysis. Holding back 2 vouchers, outside this step's scope.",
    )
  })

  it('says nothing about holding back when nothing was held', () => {
    const wrapper = mount(AgentContextCard, {
      props: { context: context({ sentence: '', withheld: [] }) },
    })

    const label = wrapper.find('section').attributes('aria-label')
    expect(label).toBe('Reading 3 documents for Audit planning memorandum.')
    expect(wrapper.text()).not.toContain('Held back')
  })
})
