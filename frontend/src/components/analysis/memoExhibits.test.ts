import { describe, expect, it } from 'vitest'

import type { SavedAnalysis } from '../../types'
import { memoCitations, memoSegments, memoUnits, testedColumns } from './memoExhibits'

function fence(id: string, caption = ''): string {
  return ['```embed', `analysis: ${id}`, 'as: exception_table', ...(caption ? [`caption: ${caption}`] : []), '```'].join('\n')
}

/** The shape of the procurement memo's second finding: one passage, eight fences. */
const MEMO = [
  'Lead paragraph.',
  '## What the analysis found',
  '### 1. Invoices billed above approved amounts',
  'Three invoices exceed both totals (A-8BFCE4A3, A-A69AB394).',
  fence('A-8BFCE4A3', '3 rows of 52 breach INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT'),
  fence('A-A69AB394'),
  '### 2. Payments made to vendors not eligible for payment',
  'Two paid invoices (A-9D05207A, A-A153094C).',
  fence('A-9D05207A'),
  fence('A-A153094C'),
  fence('A-04F21956'),
  '## How far these results can be relied on',
  'The duplicate test found no reuse (A-B8A820A7).',
].join('\n\n')

function analysis(id: string, extra: Partial<SavedAnalysis> = {}): SavedAnalysis {
  return { id, title: id, classification: 'exception', ...extra } as SavedAnalysis
}

describe('memo exhibits', () => {
  it('numbers every fence in reading order and sets consecutive fences together', () => {
    const units = memoUnits(memoSegments(MEMO))
    const sets = units.filter(unit => unit.kind === 'exhibits')

    expect(sets.map(unit => unit.kind === 'exhibits' ? unit.exhibits.map(exhibit => exhibit.number) : []))
      .toEqual([[1, 2], [3, 4, 5]])
    expect(units.filter(unit => unit.kind === 'prose')).toHaveLength(3)
  })

  it('cites an exhibited procedure by its exhibit and the rest by their result', () => {
    const units = memoUnits(memoSegments(MEMO))
    const citations = memoCitations([
      analysis('A-8BFCE4A3'), analysis('A-9D05207A'),
      analysis('A-B8A820A7', { classification: 'clear', title: 'Vendor invoice number reused' }),
    ], units)

    expect(citations['A-8BFCE4A3']).toEqual({ exhibit: 1, tone: 'bad', title: 'A-8BFCE4A3' })
    expect(citations['A-9D05207A'].exhibit).toBe(3)
    expect(citations['A-B8A820A7']).toEqual({ exhibit: undefined, tone: 'ok', title: 'Vendor invoice number reused' })
  })

  it("puts the columns a procedure's spec names first", () => {
    const compare = analysis('A-8BFCE4A3', {
      spec: { test: 'compare_columns', params: { column: 'INVOICE_AMOUNT', op: 'le', other: 'PO_TOTAL_AMOUNT', compare_as: 'number' } },
    })
    const columns = ['INVOICE_ID', 'PO_TOTAL_AMOUNT', 'INVOICE_AMOUNT', 'VENDOR_ID']

    expect(testedColumns(compare, columns)).toEqual(['INVOICE_AMOUNT', 'PO_TOTAL_AMOUNT'])
    expect(testedColumns(analysis('A-1', { spec: { params: { columns: ['VENDOR_ID', 'GONE'] } } }), columns))
      .toEqual(['VENDOR_ID'])
    expect(testedColumns(undefined, columns)).toEqual([])
  })
})
