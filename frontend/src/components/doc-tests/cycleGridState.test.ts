import { describe, expect, it } from 'vitest'

import type { CycleVouchGridPayload } from '../../types'
import {
  EMPTY_CYCLE_GRID_FILTERS,
  cycleGridPageLabel,
  cycleGridSearchText,
  filterCycleGridRows,
} from './cycleGridState'

const rows: CycleVouchGridPayload['rows'] = [
  {
    item_id: 'ITEM-1',
    label: 'PAY-001',
    evaluation_state: 'failed',
    disposition_state: 'pending',
    disposition_stale: false,
    roles_present: ['payslip', 'bank_payment'],
    missing_roles: [],
    shared_record_facts: [],
    cells: {
      amount: {
        verdict: 'mismatch',
        display: '1,000 vs 900',
        comparison_count: 2,
        evidence_count: 2,
        comparisons: [
          {
            role: 'payslip', document_id: 'DOC-PAYSLIP', verdict: 'match',
            record_ids: ['REC-1'], display_values: [1000], entry_count: 1, evidence_count: 1,
          },
          {
            role: 'bank_payment', document_id: 'DOC-BANK', verdict: 'mismatch',
            record_ids: ['REC-2'], display_values: [900], entry_count: 1, evidence_count: 1,
          },
        ],
      },
    },
  },
  {
    item_id: 'ITEM-2',
    label: 'PAY-002',
    evaluation_state: 'incomplete',
    disposition_state: 'confirmed',
    disposition_stale: true,
    roles_present: ['payslip'],
    missing_roles: ['bank_payment'],
    shared_record_facts: [],
    cells: {
      amount: {
        verdict: 'missing_evidence', display: 'Bank payment missing',
        comparison_count: 1, evidence_count: 1, comparisons: [],
      },
    },
  },
]

describe('cycle grid projection state', () => {
  it('searches only bounded row and cell display fields, including every comparison', () => {
    expect(cycleGridSearchText(rows[0])).toContain('doc-bank')
    expect(filterCycleGridRows(rows, { ...EMPTY_CYCLE_GRID_FILTERS, search: '900' })).toEqual([rows[0]])
    expect(filterCycleGridRows(rows, { ...EMPTY_CYCLE_GRID_FILTERS, search: 'bank payment missing' })).toEqual([rows[1]])
  })

  it('combines deterministic evaluation, auditor disposition, missing role, and assertion filters', () => {
    expect(filterCycleGridRows(rows, {
      ...EMPTY_CYCLE_GRID_FILTERS,
      evaluation: 'incomplete',
      disposition: 'stale',
      missingRole: 'bank_payment',
      assertionKey: 'amount',
      assertionVerdict: 'missing_evidence',
    })).toEqual([rows[1]])
  })

  it('does not treat a stale confirmed disposition as current confirmed work', () => {
    expect(filterCycleGridRows(rows, { ...EMPTY_CYCLE_GRID_FILTERS, disposition: 'confirmed' })).toEqual([])
  })

  it('formats bounded page ranges', () => {
    expect(cycleGridPageLabel(100, 100, 245)).toBe('101-200 of 245')
    expect(cycleGridPageLabel(0, 100, 0)).toBe('No items')
  })
})
