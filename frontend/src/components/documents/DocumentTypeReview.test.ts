import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import DocumentTypeReview from './DocumentTypeReview.vue'

vi.mock('primevue/dialog', () => ({
  default: {
    props: ['visible'], emits: ['update:visible'],
    template: '<section v-if="visible"><slot /><footer><slot name="footer" /></footer></section>',
  },
}))

const CATALOG = {
  areas: [
    { id: 'procure_to_pay', label: 'Procure to pay' },
    { id: 'governance', label: 'Governance and cross-cutting' },
  ],
  types: [
    {
      id: 'goods_receipt', label: 'Goods receipt', area: 'procure_to_pay',
      discriminator: 'Internal record that goods were received', aliases: ['GRN'], active: true,
    },
    {
      id: 'delivery_note', label: 'Delivery note', area: 'procure_to_pay',
      discriminator: "Supplier's document accompanying a shipment", aliases: [], active: true,
    },
    {
      id: 'other', label: 'Other', area: 'governance',
      discriminator: 'None of the above', aliases: [], active: true,
    },
  ],
  local_prefix: 'local.',
  other: 'other',
  local_types: [],
  summary: {
    documents: 2, classified: 2, unclassified: 0, other: 2,
    auditor_assigned: 0, types_present: [], local_types: [],
  },
}

function assignment(over: Record<string, unknown>) {
  return {
    document_type_other: null, assigned_by: 'model', assigned_at: '2026-08-29T00:00:00Z',
    confidence: 'high', rationale: '', previous_document_type: null,
    agent_run_id: null, unit_id: null, catalog_sha1: 'abc', ...over,
  }
}

/** One document that announced it needed attention, and two that did not — the
 *  half of the corpus a confidently wrong label hides in. */
function classifications(reclassifiable: string[] = []) {
  return {
    items: [
      assignment({
        document_id: 'doc-1', title: 'LOI scan', document_type: 'other',
        document_type_other: 'Letter of indemnity', confidence: 'low',
        rationale: 'No catalogued form matched.',
      }),
      assignment({ document_id: 'doc-2', title: 'GRN 4471', document_type: 'goods_receipt' }),
      assignment({ document_id: 'doc-3', title: 'Despatch 88', document_type: 'goods_receipt' }),
    ],
    reclassifiable,
  }
}

function stubGet(listing = classifications()) {
  return vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/types')) return CATALOG as never
    if (url.endsWith('/classifications')) return listing as never
    throw new Error(`unexpected GET ${url}`)
  })
}

function render() {
  return mount(DocumentTypeReview, {
    props: { workspaceId: 'ws-1', modelValue: true },
    global: { stubs: { Button: true, Message: true, Select: true, InputText: true } },
  })
}

afterEach(() => vi.restoreAllMocks())

describe('DocumentTypeReview', () => {
  it('lists the unidentified documents with what the classifier read them as', async () => {
    stubGet()
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('LOI scan')
    expect(wrapper.text()).toContain('Letter of indemnity')
    expect(wrapper.text()).toContain('No catalogued form matched.')
  })

  it('retypes to a listed type and reloads', async () => {
    const get = stubGet()
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = render()
    await flushPromises()

    ;(wrapper.vm as never as { chosenType: Record<string, string> }).chosenType['doc-1'] = 'goods_receipt'
    await (wrapper.vm as never as { retype: (id: string) => Promise<void> }).retype('doc-1')

    expect(patch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/documents/doc-1/type',
      { type_id: 'goods_receipt' },
    )
    expect(wrapper.emitted('retyped')).toHaveLength(1)
    // Reloaded: the first pair was the initial load, the second follows the write.
    expect(get.mock.calls.length).toBeGreaterThan(2)
  })

  it('coins a new type when a name is typed instead of chosen', async () => {
    stubGet()
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = render()
    await flushPromises()

    ;(wrapper.vm as never as { coinedName: Record<string, string> }).coinedName['doc-1'] = ' Letter of Indemnity '
    await (wrapper.vm as never as { retype: (id: string) => Promise<void> }).retype('doc-1')

    expect(patch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/documents/doc-1/type',
      { coin: 'Letter of Indemnity' },
    )
  })

  it('omits `other` from the picker, since retyping to it changes nothing', async () => {
    stubGet()
    const wrapper = render()
    await flushPromises()

    const groups = (wrapper.vm as never as {
      options: { label: string; items: { value: string }[] }[]
    }).options
    const values = groups.flatMap(group => group.items.map(item => item.value))
    expect(values).toContain('goods_receipt')
    expect(values).not.toContain('other')
  })

  it('offers coined types first, above the shipped catalogue', async () => {
    stubGet()
    vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
      if (url.endsWith('/types')) {
        return {
          ...CATALOG,
          local_types: [{
            id: 'local.letter_of_indemnity', label: 'Letter of Indemnity',
            discriminator: '', created: '', created_by: 'auditor',
          }],
        } as never
      }
      return classifications() as never
    })
    const wrapper = render()
    await flushPromises()

    const groups = (wrapper.vm as never as { options: { label: string }[] }).options
    expect(groups[0].label).toBe('Defined for this engagement')
  })

  it('offers re-examination only when the catalogue has grown past a stale answer', async () => {
    stubGet(classifications([]))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).not.toContain('can be re-examined')

    const second = mount(DocumentTypeReview, {
      props: { workspaceId: 'ws-1', modelValue: true },
      global: { stubs: { Button: true, Message: true, Select: true, InputText: true } },
    })
    vi.spyOn(api, 'get').mockImplementation(async (url: string) =>
      (url.endsWith('/types') ? CATALOG : classifications(['doc-9'])) as never)
    await (second.vm as never as { load: () => Promise<void> }).load()
    await flushPromises()
    expect(second.text()).toContain('can be re-examined')
  })

  it('groups the already-identified documents by the type they carry', async () => {
    stubGet()
    const wrapper = render()
    await flushPromises()

    const groups = (wrapper.vm as never as {
      groups: { id: string; label: string; items: unknown[] }[]
    }).groups
    // `other` belongs to the bucket above and is not offered as a group.
    expect(groups.map(group => group.id)).toEqual(['goods_receipt'])
    expect(groups[0].items).toHaveLength(2)
  })

  it('corrects a confident label, which never reaches the `other` bucket', async () => {
    stubGet()
    const patch = vi.spyOn(api, 'patch').mockResolvedValue({} as never)
    const wrapper = render()
    await flushPromises()

    ;(wrapper.vm as never as { chosenType: Record<string, string> }).chosenType['doc-3'] = 'delivery_note'
    await (wrapper.vm as never as { retype: (id: string) => Promise<void> }).retype('doc-3')

    expect(patch).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/documents/doc-3/type',
      { type_id: 'delivery_note' },
    )
    expect(wrapper.emitted('retyped')).toHaveLength(1)
  })

  it('filters the identified list by title or type', async () => {
    stubGet()
    const wrapper = render()
    await flushPromises()

    const vm = wrapper.vm as never as {
      filter: string
      groups: { items: { document_id: string }[] }[]
    }
    vm.filter = 'Despatch'
    await flushPromises()
    expect(vm.groups.flatMap(group => group.items.map(item => item.document_id))).toEqual(['doc-3'])
  })

  it('reports a failed retype instead of leaving the row looking saved', async () => {
    stubGet()
    vi.spyOn(api, 'patch').mockRejectedValue(new Error('nope'))
    const wrapper = render()
    await flushPromises()

    ;(wrapper.vm as never as { chosenType: Record<string, string> }).chosenType['doc-1'] = 'goods_receipt'
    await (wrapper.vm as never as { retype: (id: string) => Promise<void> }).retype('doc-1')

    expect(wrapper.emitted('error')).toHaveLength(1)
    expect(wrapper.emitted('retyped')).toBeUndefined()
  })
})
