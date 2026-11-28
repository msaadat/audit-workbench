import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import StructuredEvidenceSheet from './StructuredEvidenceSheet.vue'
import type { AuditDocument, DocumentAnalysisCitation } from '../../types'

const CITATIONS: DocumentAnalysisCitation[] = [
  { id: 'c1', page: 1, excerpt: 'Vendor invoice: VINV001-202404', source_sha1: 'a' },
  { id: 'c2', page: 1, excerpt: 'OfficeSupply Co.', source_sha1: 'a' },
  { id: 'c9', page: 1, excerpt: 'PO2024004', source_sha1: 'a' },
  { id: 'c12', page: 1, excerpt: 'PKR 2,000,000.00', source_sha1: 'a' },
]

const DOCS = [
  { id: 'doc-inv', source: 'VINV001-202404_Invoice.pdf', title: 'vinv001_202404_invoice' },
  { id: 'doc-po', source: 'PO2024004_Purchase_Order.pdf', title: 'po2024004_purchase_order' },
] as AuditDocument[]

describe('StructuredEvidenceSheet', () => {
  it('renders clean grouped field cards with clickable citations and cross-document links', async () => {
    const wrapper = mount(StructuredEvidenceSheet, {
      props: {
        compact: true,
        validated: true,
        schema: { document_type: 'vendor_invoice', schema_version: 1, schema_hash: 'h' },
        citations: CITATIONS,
        documents: DOCS,
        currentDocumentId: 'doc-inv',
        records: [{
          fields: [
            { name: 'vendor_invoice_number', value: 'VINV001-202404', citation: 'c1' },
            { name: 'vendor_name', value: 'OfficeSupply Co.', citation: 'c2' },
            { name: 'purchase_reference', value: 'PO2024004', citation: 'c9' },
            { name: 'invoice_amount', value: 'PKR 2,000,000.00', citation: 'c12' },
          ],
        }],
      },
    })

    // No raw JSON braces rendered
    expect(wrapper.text()).not.toContain('{')
    expect(wrapper.text()).toContain('OfficeSupply Co.')
    expect(wrapper.text()).toContain('PKR 2,000,000.00')

    // Every field is on page 1, so the compact list repeats no page chip
    expect(wrapper.find('button.cite').exists()).toBe(false)

    // Clicking a matching reference jumps to the related workspace document
    const link = wrapper.find('button.field-link')
    expect(link.text()).toContain('PO2024004')
    await link.trigger('click')
    expect(wrapper.emitted('open-document')?.[0]).toEqual(['doc-po'])

    // Hovering and clicking a field card emits its deterministic highlight key
    const firstCard = wrapper.find('.field-card')
    await firstCard.trigger('mouseenter')
    expect(wrapper.emitted('hover')?.[0]).toEqual(['0:vendor_invoice_number:0'])
    await firstCard.trigger('click')
    expect(wrapper.emitted('focus-field')?.[0]?.[0]).toMatchObject({ key: '0:vendor_invoice_number:0' })
  })

  it('keeps page chips where the fields span more than one page', async () => {
    const wrapper = mount(StructuredEvidenceSheet, {
      props: {
        compact: true,
        citations: [CITATIONS[0], { ...CITATIONS[3], page: 2 }],
        records: [{
          fields: [
            { name: 'vendor_invoice_number', value: 'VINV001-202404', citation: 'c1' },
            { name: 'invoice_amount', value: 'PKR 2,000,000.00', citation: 'c12' },
          ],
        }],
      },
    })

    const chips = wrapper.findAll('button.cite')
    expect(chips.map(chip => chip.text())).toEqual(['p.1', 'p.2'])
    await chips[0].trigger('click')
    expect(wrapper.emitted('cite')?.[0]?.[0]).toEqual(CITATIONS[0])
  })
})
