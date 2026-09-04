import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import DocTestDefinitionForm from './DocTestDefinitionForm.vue'
import type { DocTestDraft } from './DocTestDefinitionForm.vue'

const candidate = {
  kind: 'ruleset',
  ruleset_id: 'lnk-1',
  ruleset_hash: 'sha256:rules',
  cycle_label: 'Procure to pay',
  roles: [
    { name: 'invoice', document_type: 'vendor_invoice', cardinality: 'one', required: true },
    { name: 'order', document_type: 'purchase_order', cardinality: 'one', required: true },
  ],
  anchor: { table: 'invoices', column: 'INVOICE_NO', role: 'invoice', field: 'invoice_number' },
  assertions: [{
    id: 'as_total', label: 'Totals agree', operator: 'numeric_within',
    rationale: 'The amount billed must be the amount ordered.',
  }],
  reach: {
    population_rows: 40, linked_rows: 32, complete_cycles: 30,
    missing_role_counts: { order: 2 },
  },
  selection_confirmation: null,
}

const metadata = {
  limits: { max_items: 500 },
  sampling_methods: ['random', 'interval', 'stratified'],
  registry: { packs: [], evidence_kinds: [] },
} as never

function draft(overrides: Partial<DocTestDraft> = {}): DocTestDraft {
  return {
    title: '', rcmId: '', table: '', size: 10, seed: 42,
    frozenFields: [], identifierFields: [], requiredDocumentTypes: [],
    evidenceAware: true, attributes: [], documentId: '', pages: '', questions: '',
    procedureKey: 'cycle-vouch', selectionMode: 'evidence_linked',
    sampleMethod: 'random', stratifyBy: '',
    ...overrides,
  }
}

function render(overrides: Record<string, unknown> = {}) {
  return mount(DocTestDefinitionForm, {
    props: {
      workspace: { id: 'ws-1', tables: [], joins: [] } as never,
      documents: [],
      planning: { rcm: [{ id: 'RCM-1', risk: 'Unordered purchases' }] } as never,
      documentTypes: [],
      cycleMetadata: metadata,
      shape: 'vouching',
      modelValue: draft(),
      ...overrides,
    },
    global: {
      stubs: {
        Button: true, Select: true, InputText: true, InputNumber: true,
        MultiSelect: true, Textarea: true, Checkbox: true, AutoComplete: true,
        UiAdvancedSection: true,
      },
    },
  })
}

/** A cycle test with its RCM row picked — the two answers the rules need. */
async function cycle(overrides: Record<string, unknown> = {}) {
  const wrapper = render({ shape: 'cycle_vouch', modelValue: draft({ rcmId: 'RCM-1' }), ...overrides })
  await flushPromises()
  return wrapper
}

afterEach(() => vi.restoreAllMocks())

describe('DocTestDefinitionForm, cycle on approved rules', () => {
  it('shows the approved cycle instead of asking the auditor to author one', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = await cycle()

    expect(wrapper.text()).toContain('Procure to pay')
    expect(wrapper.text()).toContain('invoices.INVOICE_NO')
    expect(wrapper.text()).toContain('The amount billed must be the amount ordered.')
    expect(wrapper.text()).not.toContain('Typed assertions')
  })

  it('states what the rules reach, including where they fall short', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = await cycle()

    expect(wrapper.text()).toContain('30')
    expect(wrapper.text()).toContain('2 row(s) reach no order')
  })

  it('submits the ruleset and the selection, and no pack definition', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = await cycle({
      modelValue: draft({ rcmId: 'RCM-1', procedureKey: 'match_invoice_to_order' }),
    })

    const payload = (wrapper.vm as never as {
      payload: () => { draft: {
        cycleRulesetDefinition?: { ruleset_id: string; population: { selection: { mode: string } } }
        cycleDefinition?: unknown
        requirementRefs?: string[]
      } }
    }).payload()

    expect(payload.draft.cycleRulesetDefinition).toEqual({
      ruleset_id: 'lnk-1',
      population: { selection: { mode: 'evidence_linked' } },
    })
    expect(payload.draft.cycleDefinition).toBeUndefined()
    expect(payload.draft.requirementRefs).toEqual(['RCM-1:match_invoice_to_order'])
  })

  it('names an oversized targeted selection rather than truncating it', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      ...candidate,
      reach: { ...candidate.reach, linked_rows: 900 },
    } as never)
    const wrapper = await cycle()

    expect(wrapper.text()).toContain('900 rows qualify')
    expect(wrapper.text()).toContain('No rows will be truncated')
  })

  it('says there is nothing to vouch against where no rules are approved', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      kind: 'ruleset', ruleset: null, reason: 'no_approved_ruleset',
    } as never)
    const wrapper = await cycle()

    expect(wrapper.text()).toContain('no approved cycle ruleset')
    expect(wrapper.text()).toContain('cycle rules review')
  })
})

describe('DocTestDefinitionForm readiness', () => {
  it('asks for everything the shape needs, on the one screen that shows it', async () => {
    // The stepper chose the shape on a screen that could not show the scope
    // fields the shape decides, so a blocker sentence in the footer was the
    // only way to name what was missing.
    const wrapper = render()
    await flushPromises()
    expect(wrapper.emitted('valid')?.at(-1)).toEqual([false])
    expect(wrapper.find('label[data-missing="true"]').exists()).toBe(true)

    await wrapper.setProps({ modelValue: draft({ table: 'invoices' }) })
    expect(wrapper.emitted('valid')?.at(-1)).toEqual([true])
  })

  it('holds a cited Q&A back until it has both a document and a question', async () => {
    const wrapper = render({ shape: 'qa', modelValue: draft({ documentId: 'DOC-1' }) })
    await flushPromises()
    expect(wrapper.emitted('valid')?.at(-1)).toEqual([false])

    await wrapper.setProps({ modelValue: draft({ documentId: 'DOC-1', questions: 'Was it approved?' }) })
    expect(wrapper.emitted('valid')?.at(-1)).toEqual([true])
  })

  it('names the shape it will build without making the auditor say it twice', () => {
    // Vouching and tracing are one kind and two directions; the old dialog
    // asked for the direction again on the next step.
    const wrapper = render({ shape: 'tracing', modelValue: draft({ table: 'invoices' }) })
    const payload = (wrapper.vm as never as {
      payload: () => { kind: string; direction: string }
    }).payload()

    expect(payload).toMatchObject({ kind: 'vouching', direction: 'tracing' })
  })
})
