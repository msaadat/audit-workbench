import { describe, expect, it } from 'vitest'

import type { CycleGraph } from '../../types'
import { METRICS, layoutCycle, relationshipFields } from './cycleLayout'

/**
 * The procurement strip, as the backend projects it: four steps, five roles,
 * three populations, and the rules that bind them. The fixture is the shape the
 * mockups were drawn from, so what these assert is what the page renders.
 */
function graph(): CycleGraph {
  return {
    name: 'Procure-to-pay',
    steps: [
      {
        name: 'Requisition initiation and approval',
        documents: [{
          node: 'requisition', document_type: 'purchase_requisition',
          label: 'Purchase requisition', count: 9,
          fields: [
            { name: 'purchase_reference', role: 'attribute' },
            { name: 'financial_approval_by', role: 'control' },
          ],
          bound: false,
        }],
        populations: [{
          table: 'requisitions', rows: 55,
          columns: ['REQUISITION_ID'], borrowed: false, anchor: false,
        }],
        themes: [], stated: ['financial_approval_by'],
      },
      {
        name: 'Purchase order',
        documents: [{
          node: 'order', document_type: 'purchase_order', label: 'Purchase order',
          count: 14,
          fields: [
            { name: 'order_reference', role: 'identifier' },
            { name: 'po_total', role: 'attribute' },
            { name: 'po_date', role: 'attribute' },
            { name: 'vendor', role: 'attribute' },
          ],
          bound: true,
        }],
        populations: [{
          table: 'po_data', rows: 52,
          columns: ['PO_NUMBER', 'GRN_ID'], borrowed: false, anchor: true,
        }],
        themes: [], stated: [],
      },
      {
        name: 'Goods receipt and inspection',
        documents: [{
          node: 'grn', document_type: 'goods_receipt', label: 'Goods receipt',
          count: 11,
          fields: [
            { name: 'grn_number', role: 'identifier' },
            { name: 'grn_reference', role: 'identifier' },
            { name: 'quantity_received', role: 'attribute' },
          ],
          bound: true,
        }],
        populations: [{
          table: 'po_data', rows: 52,
          columns: ['GRN_ID'], borrowed: true, anchor: false,
        }],
        themes: [], stated: [],
      },
      {
        name: 'Invoice processing and payment',
        documents: [{
          node: 'invoice', document_type: 'vendor_invoice', label: 'Vendor invoice',
          count: 15,
          fields: [
            { name: 'invoice_number', role: 'identifier' },
            { name: 'order_reference', role: 'identifier' },
            { name: 'invoice_amount', role: 'attribute' },
          ],
          bound: true,
        }],
        populations: [{
          table: 'invoice_data', rows: 52,
          columns: ['PO_NUMBER'], borrowed: false, anchor: false,
        }],
        themes: [], stated: [],
      },
    ],
    edges: [
      {
        kind: 'anchor', rule_id: 'anchor', label: 'population row to its document',
        from: { step: 'Purchase order', node: 'po_data', field: 'PO_NUMBER' },
        to: { step: 'Purchase order', node: 'order', field: 'order_reference' },
      },
      {
        kind: 'join', rule_id: 'jk_grn', label: 'A receipt cites its order.',
        from: { step: 'Goods receipt and inspection', node: 'grn', field: 'grn_reference' },
        to: { step: 'Purchase order', node: 'order', field: 'order_reference' },
      },
      {
        kind: 'join', rule_id: 'jk_invoice', label: 'An invoice cites its order.',
        from: { step: 'Invoice processing and payment', node: 'invoice', field: 'order_reference' },
        to: { step: 'Purchase order', node: 'order', field: 'order_reference' },
      },
      {
        kind: 'assert', rule_id: 'as_total', label: 'The amount billed is the amount ordered.',
        from: { step: 'Invoice processing and payment', node: 'invoice', field: 'invoice_amount' },
        to: { step: 'Purchase order', node: 'order', field: 'po_total' },
      },
      {
        kind: 'table_join', rule_id: 'po_invoices', label: 'PO_NUMBER = PO_NUMBER',
        from: { step: 'Purchase order', node: 'po_data', field: 'PO_NUMBER' },
        to: { step: 'Invoice processing and payment', node: 'invoice_data', field: 'PO_NUMBER' },
      },
    ],
    cross_cutting: { name: 'Procurement operations', themes: [] },
    ruleset: { ruleset_id: 'lnk-1', status: 'proposed', cycle_label: 'Procure to pay' },
  }
}

describe('relationshipFields', () => {
  it('lists only the fields an edge or a stated mark touches, in edge order', () => {
    const fields = relationshipFields(graph())

    expect(fields.get('order')).toEqual(['order_reference', 'po_total'])
    expect(fields.get('invoice')).toEqual(['order_reference', 'invoice_amount'])
    // Four induced fields, two in a rule: the page shows the vocabulary of the
    // rules rather than the schema.
    expect(fields.get('order')).toHaveLength(2)
  })

  it('keeps a field a one-operand assertion requires to be stated', () => {
    const fields = relationshipFields(graph())

    expect(fields.get('requisition')).toEqual(['financial_approval_by'])
  })
})

describe('layoutCycle', () => {
  it('places one column per step, in the order the cycle runs', () => {
    const layout = layoutCycle(graph())

    expect(layout.columns.map(column => column.step)).toEqual([
      'Requisition initiation and approval',
      'Purchase order',
      'Goods receipt and inspection',
      'Invoice processing and payment',
    ])
    expect(layout.columns[0].x).toBe(0)
    expect(layout.columns[1].x).toBe(METRICS.columnWidth + METRICS.gutter)
  })

  it('puts the document above its population in every column', () => {
    const layout = layoutCycle(graph())

    for (const column of layout.columns) {
      const document = column.nodes.find(node => node.kind === 'document')
      const population = column.nodes.find(node => node.kind === 'population')
      if (!document || !population) continue
      expect(document.y + document.height).toBeLessThanOrEqual(population.y)
    }
  })

  it('anchors an arrow to the centre of the field row it names', () => {
    const layout = layoutCycle(graph())
    const order = layout.columns[1].nodes.find(node => node.id === 'order')!
    const join = layout.edges.find(edge => edge.ruleId === 'jk_grn')!

    const target = order.fields.find(field => field.name === 'order_reference')!
    const last = join.points[join.points.length - 1]
    expect(last.y).toBe(order.y + target.y)
    expect(last.x).toBe(order.x + order.width)
  })

  it('routes the procurement strip with no crossing', () => {
    expect(layoutCycle(graph()).crossings).toBe(0)
  })

  it('gives two arrows leaving the same node edge different vertical runs', () => {
    // Riders routinely leave and enter at the same two node edges, so
    // separating only the bus left three of them sharing one line down.
    const layout = layoutCycle(graph())
    const verticals = layout.edges.flatMap(edge =>
      edge.points.slice(0, -1).flatMap((point, index) =>
        point.x === edge.points[index + 1].x ? [point.x] : [],
      ),
    )
    const riders = layout.edges.filter(edge => edge.rides)
    expect(riders.length).toBeGreaterThan(1)
    expect(new Set(verticals).size).toBeGreaterThan(1)
  })

  it('rides the bus above the nodes when an arrow skips a column', () => {
    const layout = layoutCycle(graph())
    // Invoice is two columns from the order it cites.
    const skipping = layout.edges.find(edge => edge.ruleId === 'jk_invoice')!

    expect(skipping.rides).toBe(true)
    const top = Math.min(...skipping.points.map(point => point.y))
    const nodeTop = Math.min(
      ...layout.columns.flatMap(column => column.nodes.map(node => node.y)),
    )
    expect(top).toBeLessThan(nodeTop)
  })

  it('never lets a rider pass through a node it skips over', () => {
    const layout = layoutCycle(graph())
    const nodes = layout.columns.flatMap(column => column.nodes)

    for (const edge of layout.edges.filter(item => item.rides)) {
      for (let index = 0; index < edge.points.length - 1; index += 1) {
        const from = edge.points[index]
        const to = edge.points[index + 1]
        if (from.y !== to.y) continue
        const [low, high] = from.x < to.x ? [from.x, to.x] : [to.x, from.x]
        for (const node of nodes) {
          const overlapsX = node.x < high && low < node.x + node.width
          const insideY = from.y > node.y && from.y < node.y + node.height
          expect(overlapsX && insideY).toBe(false)
        }
      }
    }
  })

  it('marks a role the rules could not bind rather than hiding the step', () => {
    const layout = layoutCycle(graph())
    const requisition = layout.columns[0].nodes.find(node => node.id === 'requisition')!

    expect(requisition.bound).toBe(false)
    expect(requisition.note).toContain('nothing links here')
  })

  it('says which columns hold a step that has no population of its own', () => {
    const layout = layoutCycle(graph())
    const borrowed = layout.columns[2].nodes.find(node => node.kind === 'population')!

    expect(borrowed.note).toContain('GRN_ID')
    expect(borrowed.note).toContain('No population of its own')
  })

  it('draws the flow with no fields at all before any schema exists', () => {
    const bare = graph()
    for (const step of bare.steps) {
      for (const document of step.documents) document.fields = []
      step.stated = []
    }
    bare.edges = bare.edges.filter(edge => edge.kind === 'table_join')
    bare.ruleset = null

    const layout = layoutCycle(bare)

    expect(layout.columns).toHaveLength(4)
    for (const column of layout.columns) {
      const document = column.nodes.find(node => node.kind === 'document')
      expect(document?.fields ?? []).toHaveLength(0)
    }
    // The populations and the joins between them are still readable.
    expect(layout.columns[1].nodes.some(node => node.kind === 'population')).toBe(true)
  })

  it('shows every induced field when asked, and counts what it was hiding', () => {
    const scoped = layoutCycle(graph())
    const all = layoutCycle(graph(), { showAllFields: true })

    const scopedOrder = scoped.columns[1].nodes.find(node => node.id === 'order')!
    const allOrder = all.columns[1].nodes.find(node => node.id === 'order')!

    expect(scopedOrder.fields).toHaveLength(2)
    expect(scopedOrder.hiddenFieldCount).toBe(2)
    expect(allOrder.fields).toHaveLength(4)
    expect(allOrder.hiddenFieldCount).toBe(0)
  })
})

describe('node height', () => {
  it('leaves room below the fields for the lines that follow them', () => {
    // The note, the pending line and the hidden-field count are rendered after
    // the field list, so they are counted into the height — otherwise the
    // population node is placed over the bottom of the document node.
    const bare = graph()
    bare.steps[0].documents[0].fields = []
    bare.steps[0].stated = []

    const layout = layoutCycle(bare)
    const column = layout.columns[0]
    const document = column.nodes.find(node => node.kind === 'document')!
    const population = column.nodes.find(node => node.kind === 'population')!

    expect(document.fields).toHaveLength(0)
    // A note and a pending line, and the population still clears them.
    expect(document.note).not.toBe('')
    expect(population.y).toBeGreaterThan(document.y + document.height - 1)
  })

  it('says a schema exists even when no field of it is in a rule', () => {
    const layout = layoutCycle(graph())
    const requisition = layout.columns[0].nodes.find(node => node.id === 'requisition')!

    // Two induced fields; one is in a rule through the stated mark.
    expect(requisition.hasSchema).toBe(true)

    const bare = graph()
    bare.steps[0].documents[0].fields = []
    const none = layoutCycle(bare).columns[0].nodes.find(node => node.id === 'requisition')!
    expect(none.hasSchema).toBe(false)
  })
})
