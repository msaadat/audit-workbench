import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { DocumentSchemaCatalogEntry, RcmControlAttribute } from '../../types'
import RcmControlAttributesEditor from './RcmControlAttributesEditor.vue'

const schemas: DocumentSchemaCatalogEntry[] = [
  {
    document_type: 'vendor_invoice',
    discriminator: "Supplier's demand for payment addressed to the entity",
    documents: 182,
    fields: [
      { name: 'invoice_number', role: 'identifier', value_type: 'identifier', label: 'Invoice number' },
      { name: 'total_amount', role: 'attribute', value_type: 'number', label: 'Total' },
    ],
  },
  {
    document_type: 'purchase_order',
    discriminator: "Entity's committed order to a supplier",
    documents: 1,
    fields: [
      { name: 'order_number', role: 'identifier', value_type: 'identifier', label: 'Order number' },
      { name: 'total_amount', role: 'attribute', value_type: 'number', label: 'Total' },
    ],
  },
]

const metadata = {
  registry: {
    evidence_kinds: [
      { id: 'transaction_cycle', label: 'Transaction cycle' },
      { id: 'manual_inspection', label: 'Manual inspection' },
    ],
    packs: [],
  },
  limits: { min_cycle_record_kinds: 2 },
} as never

function cycleAttribute(overrides: Record<string, unknown> = {}): RcmControlAttribute {
  return {
    key: 'invoice_match',
    assertion: 'Accuracy',
    requirement: 'The invoice agrees to the order.',
    evidence_kind: 'transaction_cycle',
    required_comparisons: [],
    ...overrides,
  } as RcmControlAttribute
}

function render(attributes: RcmControlAttribute[], withSchemas = true) {
  return mount(RcmControlAttributesEditor, {
    props: {
      modelValue: attributes,
      metadata,
      ...(withSchemas ? { schemas } : {}),
    },
    global: {
      stubs: {
        Button: true, Select: true, InputText: true, InputNumber: true,
        MultiSelect: true, Textarea: true,
      },
    },
  })
}

function emitted(wrapper: ReturnType<typeof render>): RcmControlAttribute[] {
  const events = wrapper.emitted('update:modelValue')!
  return events[events.length - 1][0] as RcmControlAttribute[]
}

describe('RcmControlAttributesEditor, against induced schemas', () => {
  it('writes a requirement against the fields the documents carry', () => {
    const wrapper = render([cycleAttribute()])

    expect(wrapper.text()).toContain('What must agree')
    // There is one vocabulary now, so there is no pack to choose and no record
    // kinds to declare.
    expect(wrapper.text()).not.toContain('Cycle pack')
    expect(wrapper.text()).not.toContain('Required record kinds')
  })

  it('says what to do when nothing has been induced yet', () => {
    const wrapper = render([cycleAttribute()], false)

    expect(wrapper.text()).toContain('Classify and induce first')
  })

  it('takes the comparisons with the strategy that owned them', async () => {
    const wrapper = render([cycleAttribute({
      required_comparisons: [{
        key: 'totals_agree',
        left: { document_type: 'vendor_invoice', field: 'total_amount' },
        right: { document_type: 'purchase_order', field: 'total_amount' },
      }],
    })])

    await (wrapper.vm as never as {
      setEvidenceKind: (index: number, kind: string) => void
    }).setEvidenceKind(0, 'manual_inspection')

    const attribute = emitted(wrapper)[0] as never as {
      evidence_kind: string
      required_comparisons?: unknown
    }
    expect(attribute.evidence_kind).toBe('manual_inspection')
    expect(attribute.required_comparisons).toBeUndefined()
  })

  it('adds a comparison naming two document types and their fields', async () => {
    const wrapper = render([cycleAttribute()])

    await (wrapper.vm as never as { addComparison: (i: number) => void })
      .addComparison(0)

    const comparison = (emitted(wrapper)[0] as never as {
      required_comparisons: Array<{ left: unknown; right: unknown; operator?: string }>
    }).required_comparisons[0]
    expect(comparison.left).toEqual({ document_type: 'vendor_invoice', field: 'invoice_number' })
    expect(comparison.right).toEqual({ document_type: 'purchase_order', field: 'order_number' })
    // How to compare is not authored here: it is settled against the values.
    expect(comparison.operator).toBeUndefined()
  })

  // A field name means nothing away from the type that states it, so carrying
  // it across would silently point the requirement at something no schema has.
  it('does not carry a field across a change of document type', async () => {
    const wrapper = render([cycleAttribute({
      required_comparisons: [{
        key: 'totals_agree',
        left: { document_type: 'vendor_invoice', field: 'total_amount' },
        right: { document_type: 'purchase_order', field: 'total_amount' },
      }],
    })])

    await (wrapper.vm as never as {
      setOperand: (a: number, c: number, s: string, v: Record<string, string>) => void
    }).setOperand(0, 0, 'left', { document_type: 'purchase_order' })

    const comparison = (emitted(wrapper)[0] as never as {
      required_comparisons: Array<{ left: { document_type: string; field: string } }>
    }).required_comparisons[0]
    expect(comparison.left).toEqual({ document_type: 'purchase_order', field: 'order_number' })
  })

  it('drops the right operand when the requirement reads one field', async () => {
    const wrapper = render([cycleAttribute({
      required_comparisons: [{
        key: 'has_order',
        left: { document_type: 'vendor_invoice', field: 'invoice_number' },
        right: { document_type: 'purchase_order', field: 'order_number' },
      }],
    })])

    await (wrapper.vm as never as {
      setComparesTwo: (a: number, c: number, comparesTwo: boolean) => void
    }).setComparesTwo(0, 0, false)

    const comparison = (emitted(wrapper)[0] as never as {
      required_comparisons: Array<{ right: unknown }>
    }).required_comparisons[0]
    expect(comparison.right).toBeNull()
  })

  it('says what to do when no comparison is stated', () => {
    const wrapper = render([cycleAttribute()])

    expect(wrapper.text()).toContain('Manual inspection, Inquiry, or Mixed')
  })

  it('shows how many documents carry a type, and what separates it from its neighbours', () => {
    // The blind spot this closes: an authoring pass chose a type coined for one
    // anomalous document as the deal-record side of population-wide comparisons,
    // on its name alone — which is all a bare list of type names offers a reader.
    const wrapper = render([cycleAttribute()])

    const options = (wrapper.vm as never as {
      documentTypeOptions: Array<{ label: string; documents: number; discriminator: string }>
    }).documentTypeOptions

    expect(options.map(option => option.label)).toEqual([
      'vendor_invoice · 182 documents',
      'purchase_order · 1 document',
    ])
    expect(options[1].discriminator).toContain('committed order')
  })
})
