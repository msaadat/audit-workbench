import type {
  CycleAssertionVerdict,
  CycleDispositionState,
  CycleEvaluationState,
  CycleVouchGridPayload,
} from '../../types'

export type CycleGridRow = CycleVouchGridPayload['rows'][number]

export interface CycleGridFilters {
  search: string
  evaluation: '' | CycleEvaluationState
  disposition: '' | CycleDispositionState | 'stale'
  missingRole: string
  assertionKey: string
  assertionVerdict: '' | CycleAssertionVerdict
}

export const EMPTY_CYCLE_GRID_FILTERS: CycleGridFilters = {
  search: '',
  evaluation: '',
  disposition: '',
  missingRole: '',
  assertionKey: '',
  assertionVerdict: '',
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try { return JSON.stringify(value) }
  catch { return String(value) }
}

/** Search only the bounded fields Phase 4 deliberately projected into the grid. */
export function cycleGridSearchText(row: CycleGridRow): string {
  const values: string[] = [
    row.label,
    row.item_id,
    row.evaluation_state,
    row.disposition_state,
    ...row.roles_present,
    ...row.missing_roles,
  ]
  for (const cell of Object.values(row.cells)) {
    values.push(cell.display, cell.verdict)
    for (const comparison of cell.comparisons) {
      values.push(
        comparison.side ?? '',
        comparison.role ?? '',
        comparison.document_id ?? '',
        comparison.state ?? '',
        comparison.verdict ?? '',
        ...comparison.record_ids,
        ...comparison.display_values.map(displayValue),
      )
    }
  }
  return values.join('\n').toLocaleLowerCase()
}

export function filterCycleGridRows(
  rows: CycleGridRow[],
  filters: CycleGridFilters,
): CycleGridRow[] {
  const query = filters.search.trim().toLocaleLowerCase()
  return rows.filter(row => {
    if (filters.evaluation && row.evaluation_state !== filters.evaluation) return false
    if (filters.disposition === 'stale' && !row.disposition_stale) return false
    if (
      filters.disposition
      && filters.disposition !== 'stale'
      && (row.disposition_state !== filters.disposition || row.disposition_stale)
    ) return false
    if (filters.missingRole && !row.missing_roles.includes(filters.missingRole)) return false
    if (filters.assertionKey && filters.assertionVerdict) {
      if (row.cells[filters.assertionKey]?.verdict !== filters.assertionVerdict) return false
    }
    if (query && !cycleGridSearchText(row).includes(query)) return false
    return true
  })
}

export function cycleGridPageLabel(offset: number, limit: number, total: number): string {
  if (!total) return 'No items'
  const start = Math.min(offset + 1, total)
  const end = Math.min(offset + limit, total)
  return `${start}-${end} of ${total}`
}

