import { describe, expect, it } from 'vitest'

import type { CycleVouchGridPayload } from '../../types'
import { buildStageSpans, planCycleColumns, type CycleVouchGridColumn } from './cycleColumnOrg'

function col(
  key: string,
  label: string,
  applicable_roles: string[],
  mismatch = 0,
): CycleVouchGridColumn {
  return {
    key,
    label,
    requirement: label,
    applicable_roles,
    counts: {
      match: mismatch ? 1 : 2,
      mismatch,
      cannot_determine: 0,
      missing_evidence: 0,
      invalid_extraction: 0,
      ambiguous: 0,
      not_run: 0,
    },
    stale_cells: 0,
  }
}

const sampleColumns: CycleVouchGridColumn[] = [
  col('pair_order_payment_vendor', 'Payment vendor matches PO vendor', ['order', 'payment']),
  col('pair_req_po_desc', 'PO description matches requisition', ['requisition_outcome', 'order']),
  col('pair_req_po_vendor', 'PO vendor matches requisition vendor', ['requisition_outcome', 'order']),
  col('pair_order_receipt_desc', 'Receipt description matches PO description', ['order', 'receipt']),
  col('pair_order_receipt_qty', 'Received quantity matches PO quantity', ['order', 'receipt'], 1),
  col('assert_grn_signed', 'Goods receipt is signed', ['receipt']),
  col('assert_invoice_cites_po', 'Invoice cites purchase order', ['invoice_approval']),
  col('pair_order_invoice_desc', 'Invoice description matches PO description', ['order', 'invoice_approval']),
  col('assert_verified_amount', 'Approved invoice amount matches verified amount', ['invoice_verification', 'invoice_approval']),
  col('pair_invoice_payment_amt', 'Payment amount matches approved invoice amount', ['invoice_approval', 'payment']),
]

function makePayload(title: string, withRoles = true): CycleVouchGridPayload {
  return {
    test_id: 'DT-1',
    test_sha1: 'sha',
    definition_sha1: 'def',
    title,
    ...(withRoles
      ? {
          roles: [
            { name: 'requisition_outcome', document_type: 'purchase_requisition' },
            { name: 'order', document_type: 'purchase_order' },
            { name: 'receipt', document_type: 'goods_receipt' },
            { name: 'invoice_verification', document_type: 'vendor_invoice' },
            { name: 'invoice_approval', document_type: 'vendor_invoice' },
            { name: 'payment', document_type: 'payment_voucher' },
          ],
        }
      : {}),
    control_conclusion: 'no_conclusion',
    rcm_id: 'RCM-1',
    scope_limitation_note: '',
    scope_limitation_disclosure: '',
    population: { table: 'requisitions', column: 'REQ_ID', selection: {} },
    coverage: {},
    selection_basis: 'evidence_linked',
    assurance_scope: 'targeted_evidence_only',
    assurance_label: 'Targeted',
    tested_item_counts: {},
    pending_dispositions: 0,
    assertion_counts: {
      match: 19, mismatch: 1, cannot_determine: 0, missing_evidence: 0,
      invalid_extraction: 0, ambiguous: 0, not_run: 0, total: 20,
    },
    columns: sampleColumns,
    rows: [],
    stale_definition: false,
    stale_cell_count: 0,
    page: { offset: 0, limit: 100, total: 2 },
    truncated: false,
  }
}

describe('planCycleColumns', () => {
  it('groups and orders columns by chronological document stage with mismatches first', () => {
    const plan = planCycleColumns(makePayload('Invoice matched to purchase order and goods receipt note'))

    expect(plan.stages.map(s => s.label)).toEqual([
      'Purchase Order',
      'Goods Receipt',
      'Vendor Invoice',
      'Payment Voucher',
    ])
    const receiptStage = plan.stages[1]
    expect(receiptStage.mismatched).toBe(1)
    expect(receiptStage.tone).toBe('bad')
    // Mismatched column floats to the front of its stage.
    expect(receiptStage.columns[0].key).toBe('pair_order_receipt_qty')
    expect(plan.flaggedKeys).toEqual(['pair_order_receipt_qty'])

    const spans = buildStageSpans(plan.orderedColumns, plan)
    expect(spans.map(s => [s.label, s.span])).toEqual([
      ['Purchase Order', 2],
      ['Goods Receipt', 3],
      ['Vendor Invoice', 3],
      ['Payment Voucher', 2],
    ])
  })

  it('works identically without payload.roles using role fallback normalization', () => {
    const plan = planCycleColumns(makePayload('Purchase order details agree with approved requisition', false))
    expect(plan.stages.map(s => s.label)).toEqual([
      'Purchase Order',
      'Goods Receipt',
      'Vendor Invoice',
      'Payment Voucher',
    ])
  })

  it("takes the backend's assertion scope over the title heuristic", () => {
    // The vendor title alone would pick the vendor column; the requirement the
    // test cites asks for the receipt quantity and signature instead.
    const scoped = planCycleColumns({
      ...makePayload('PO vendor agrees with approved requisition vendor'),
      assertion_scope: ['assert_grn_signed', 'pair_order_receipt_qty'],
    })
    expect(scoped.relevantKeys).toEqual(['pair_order_receipt_qty', 'assert_grn_signed'])

    const unscoped = planCycleColumns({
      ...makePayload('PO vendor agrees with approved requisition vendor'),
      assertion_scope: null,
    })
    expect(unscoped.relevantKeys).toEqual(['pair_req_po_vendor'])
  })

  it('picks focused relevant columns matching each specific test', () => {
    const poVendor = planCycleColumns(makePayload('PO vendor agrees with approved requisition vendor'))
    expect(poVendor.relevantKeys).toEqual(['pair_req_po_vendor'])

    const poDetails = planCycleColumns(makePayload('Purchase order details agree with approved requisition'))
    expect(poDetails.relevantKeys).toEqual(['pair_req_po_desc', 'pair_req_po_vendor'])

    const grnInspect = planCycleColumns(makePayload('Inspection of goods against the purchase order before the goods receipt note is completed'))
    expect(grnInspect.relevantKeys).toEqual([
      'pair_order_receipt_qty',
      'pair_order_receipt_desc',
      'assert_grn_signed',
    ])

    const invoiceMatch = planCycleColumns(makePayload('Invoice matched to purchase order and goods receipt note'))
    expect(invoiceMatch.relevantKeys).toEqual([
      'assert_invoice_cites_po',
      'pair_order_invoice_desc',
      'assert_verified_amount',
    ])

    const invoiceChanges = planCycleColumns(makePayload('Invoice changes between verification and payment identified'))
    expect(invoiceChanges.relevantKeys).toEqual([
      'assert_verified_amount',
      'pair_invoice_payment_amt',
    ])
  })
})
