import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ProvenanceRail from './ProvenanceRail.vue'

/**
 * The rail groups a flat context manifest into the order a reviewer reads it:
 * the files they recognise, then the tables behind a count, then the
 * templates and prior drafts that say what the step was rather than what it
 * rested on. These pin the grouping and the reasons, not the prose.
 */

vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ push: vi.fn(), target: () => '/', to: () => '/' }),
}))

const responses: Record<string, unknown> = {}

vi.mock('../../api', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    get: (url: string) => {
      const key = url.includes('/provenance') ? 'provenance' : 'documents'
      return Promise.resolve(responses[key])
    },
  },
}))

function selection(sourceId: string, sourceType: string, sourceRef: string, kind: string, characters = 100) {
  return {
    source_id: sourceId,
    source_type: sourceType,
    source_ref: sourceRef,
    representation: { kind },
    supplied_size: { characters, estimated_tokens: characters / 4, items: 1, media_items: 0 },
  }
}

function omission(sourceId: string, reason: string, sourceRef: string | null = null) {
  return { source_id: sourceId, source_ref: sourceRef, source_hash: null, reason }
}

const DOCUMENTS = [
  { id: 'd1', source: 'Procurement SOP Extracts.docx', title: 'procurement_sop_extracts', category: 'policy' },
  { id: 'd2', source: 'Minutes of Meeting - CFO.docx', title: 'minutes_of_meeting_cfo', category: 'minutes' },
  { id: 'v1', source: 'PO2024004_Purchase_Order.pdf', title: 'po2024004_purchase_order', category: 'voucher' },
]

const SELECTOR_DECLINED = "Local selector strategy 'lexical' did not match the candidate."

async function render(context: Record<string, unknown>) {
  responses.documents = { items: DOCUMENTS }
  responses.provenance = {
    state: 'attributed',
    unit: { stage_title: 'Audit planning memorandum', capability: 'planning.apm_ready' },
    model: {},
    proposal: {},
    receipt: { state: 'unavailable', reason: 'none' },
    context: { state: 'available', selections: [], omissions: [], truncations: [], ...context },
  }
  const wrapper = mount(ProvenanceRail, {
    props: { workspaceId: 'ws', artifactRef: 'planning:apm' },
  })
  await flushPromises()
  return wrapper
}

describe('ProvenanceRail source grouping', () => {
  it('leads with named documents and folds tables behind a count', async () => {
    const wrapper = await render({
      selections: [
        selection('planning_context', 'planning', 'planning:context', 'planning_context'),
        selection('table_metadata', 'tables', 'table:po_data', 'table_metadata'),
        selection('table_profiles', 'tables', 'table:po_data', 'table_profile'),
        selection('table_metadata', 'tables', 'table:invoice_data', 'table_metadata'),
        selection('documents', 'documents', 'document:d1', 'summary'),
      ],
    })

    const text = wrapper.text()
    // The file the auditor recognises, not `document:d1`.
    expect(text).toContain('Procurement SOP Extracts.docx')
    expect(text).not.toContain('document:d1')
    // Two tables, though three selections named them.
    expect(text).toContain('2 tables')
    // Collapsed groups keep their rows out of the way until asked for.
    expect(text).not.toContain('po_data')
    expect(text).toContain('1 other source')
    // Documents come before the collapsed groups.
    expect(text.indexOf('Procurement SOP')).toBeLessThan(text.indexOf('2 tables'))
    expect(text.indexOf('2 tables')).toBeLessThan(text.indexOf('1 other source'))
  })

  it('merges a table selected for both its metadata and its profile into one row', async () => {
    const wrapper = await render({
      selections: [
        selection('table_metadata', 'tables', 'table:po_data', 'table_metadata', 900),
        selection('table_profiles', 'tables', 'table:po_data', 'table_profile', 2100),
      ],
    })
    await wrapper.find('.group-head.toggle').trigger('click')

    const rows = wrapper.findAll('.group-body .source')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('po_data')
    expect(rows[0].text()).toContain('table metadata · table profile')
    // Sizes add up rather than being reported twice.
    expect(rows[0].text()).toContain('3,000 chars')
  })

  it('names supporting context by what it is, not by its ref', async () => {
    const wrapper = await render({
      selections: [selection('apm_template', 'template', 'template:apm', 'artifact_template')],
    })
    await wrapper.find('.group-head.toggle').trigger('click')

    const row = wrapper.find('.group-body .source')
    expect(row.text()).toContain('Artifact template')
    // The ref stays, one line down, so the row is still identifiable.
    expect(row.text()).toContain('template:apm')
  })
})

describe('ProvenanceRail withheld grouping', () => {
  it('states one shared reason on the group instead of on every row', async () => {
    const wrapper = await render({
      selections: [selection('documents', 'documents', 'document:d1', 'summary')],
      omissions: [
        omission('documents', SELECTOR_DECLINED, 'document:v1'),
        omission('documents', SELECTOR_DECLINED, 'document:d2'),
      ],
    })

    const withheld = wrapper.findAll('.card').find(card => card.text().startsWith('Not supplied'))
    expect(withheld).toBeTruthy()
    const text = withheld!.text()
    expect(text).toContain("Outside this step's scope")
    // Said once for the group, not once per document.
    expect(text.match(/Outside this step's scope/g)).toHaveLength(1)
    expect(text).toContain('PO2024004_Purchase_Order.pdf')
    // Never the selector implementation.
    expect(text).not.toContain('did not match')
  })

  it('falls back to per-row reasons when a group holds more than one', async () => {
    const wrapper = await render({
      selections: [selection('documents', 'documents', 'document:d1', 'summary')],
      omissions: [
        omission('methodology', 'Optional context source is unavailable.'),
        omission('population_summary', 'Global or per-source size limit reached.', 'workspace:populations'),
      ],
    })

    const withheld = wrapper.findAll('.card').find(card => card.text().startsWith('Not supplied'))!
    await withheld.find('.group-head.toggle').trigger('click')

    const text = withheld.text()
    expect(text).toContain('Not available')
    expect(text).toContain('Past the size limit')
    expect(text).toContain('Methodology')
  })

  it('reports a truncation as cut short, with what it lost', async () => {
    const wrapper = await render({
      selections: [selection('documents', 'documents', 'document:d1', 'summary')],
      truncations: [{
        source_id: 'documents',
        source_ref: 'document:d2',
        reason: 'Global or per-source character/token limit reached.',
        original_size: { characters: 8000, estimated_tokens: 2000, items: 1, media_items: 0 },
        supplied_size: { characters: 4000, estimated_tokens: 1000, items: 1, media_items: 0 },
      }],
    })

    const withheld = wrapper.findAll('.card').find(card => card.text().startsWith('Not supplied'))!
    expect(withheld.text()).toContain('Cut short')
    expect(withheld.text()).toContain('Minutes of Meeting - CFO.docx')
    expect(withheld.text()).toContain('8,000 chars')
  })
})

describe('ProvenanceRail trust verdict', () => {
  it('does not call a scope decision unsupported', async () => {
    const wrapper = await render({
      selections: [selection('documents', 'documents', 'document:d1', 'summary')],
      omissions: [omission('documents', SELECTOR_DECLINED, 'document:v1')],
    })

    const trust = wrapper.findAll('.card').find(card => card.text().startsWith('Trust'))!
    expect(trust.text()).toContain('Everything this step selected was supplied in full')
    expect(trust.text()).toContain("1 source was outside this step's scope")
    expect(trust.text()).not.toContain('unsupported')
  })

  it('reserves "unsupported" for material that was actually cut short', async () => {
    const wrapper = await render({
      selections: [selection('documents', 'documents', 'document:d1', 'summary')],
      truncations: [{
        source_id: 'documents',
        source_ref: 'document:d2',
        reason: 'Global or per-source character/token limit reached.',
        original_size: { characters: 8000, estimated_tokens: 2000, items: 1, media_items: 0 },
        supplied_size: { characters: 4000, estimated_tokens: 1000, items: 1, media_items: 0 },
      }],
    })

    const trust = wrapper.findAll('.card').find(card => card.text().startsWith('Trust'))!
    expect(trust.text()).toContain('1 source was cut short')
    expect(trust.text()).toContain('unsupported')
  })
})
