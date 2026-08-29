import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import DocTestCreateDialog from './DocTestCreateDialog.vue'

vi.mock('primevue/dialog', () => ({
  default: {
    props: ['visible'], emits: ['update:visible'],
    template: '<section v-if="visible"><slot /><footer><slot name="footer" /></footer></section>',
  },
}))

// Message renders its content in a slot; stubbing it away would hide the very
// warnings these assertions are about.
vi.mock('primevue/message', () => ({
  default: { template: '<aside><slot /></aside>' },
}))

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

function render(overrides: Record<string, unknown> = {}) {
  return mount(DocTestCreateDialog, {
    props: {
      workspace: { id: 'ws-1', tables: [], joins: [] } as never,
      documents: [],
      planning: { rcm: [{ id: 'RCM-1', risk: 'Unordered purchases' }] } as never,
      documentTypes: [],
      cycleMetadata: metadata,
      creating: false,
      modelValue: true,
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

/** Walk the dialog to the scope step with a cycle shape and an RCM row. */
async function toCycleScope(wrapper: ReturnType<typeof render>) {
  const vm = wrapper.vm as never as {
    shape: string
    step: number
    draft: { rcmId: string; procedureKey: string; selectionMode: string }
  }
  vm.shape = 'cycle_vouch'
  vm.draft.rcmId = 'RCM-1'
  await flushPromises()
  vm.step = 2
  await flushPromises()
  return vm
}

afterEach(() => vi.restoreAllMocks())

describe('DocTestCreateDialog, cycle on approved rules', () => {
  it('shows the approved cycle instead of asking the auditor to author one', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = render()
    await toCycleScope(wrapper)

    expect(wrapper.text()).toContain('Procure to pay')
    expect(wrapper.text()).toContain('invoices.INVOICE_NO')
    expect(wrapper.text()).toContain('The amount billed must be the amount ordered.')
    expect(wrapper.text()).not.toContain('Typed assertions')
  })

  it('states what the rules reach, including where they fall short', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = render()
    await toCycleScope(wrapper)

    expect(wrapper.text()).toContain('30')
    expect(wrapper.text()).toContain('2 row(s) reach no order')
  })

  it('submits the ruleset and the selection, and no pack definition', async () => {
    vi.spyOn(api, 'post').mockResolvedValue(candidate as never)
    const wrapper = render()
    const vm = await toCycleScope(wrapper)
    vm.draft.procedureKey = 'match_invoice_to_order'

    await (wrapper.vm as never as { submit: () => void }).submit()

    const [payload] = wrapper.emitted('create')![0] as [{
      draft: {
        cycleRulesetDefinition?: { ruleset_id: string; population: { selection: { mode: string } } }
        cycleDefinition?: unknown
        requirementRefs?: string[]
      }
    }]
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
    const wrapper = render()
    const vm = await toCycleScope(wrapper)
    vm.draft.procedureKey = 'match_invoice_to_order'
    await flushPromises()

    expect(wrapper.text()).toContain('900 rows qualify')
    expect(wrapper.text()).toContain('No rows will be truncated')
  })

  it('says there is nothing to vouch against where no rules are approved', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      kind: 'ruleset', ruleset: null, reason: 'no_approved_ruleset',
    } as never)
    const wrapper = render()
    await toCycleScope(wrapper)

    expect(wrapper.text()).toContain('no approved cycle ruleset')
    expect(wrapper.text()).toContain('cycle rules review')
  })
})
