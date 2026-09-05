import { describe, expect, it } from 'vitest'

import type { RuleSet, TableInfo, TableProfile } from '../../types'
import {
  TABLE_CHIPS, filterTables, tableMeta, tableTone, tablesStatus, untestedColumns,
} from './tablesStatus'
import type { TablesFacts } from './tablesStatus'

function file(name: string, overrides: Partial<TableInfo> = {}): TableInfo {
  return { name, kind: 'file', source: `${name}.xlsx`, rows: 52, columns: 3, error: null, ...overrides }
}
function join(name: string, overrides: Partial<TableInfo> = {}): TableInfo {
  return {
    name, kind: 'join', source: 'left join', rows: 52, columns: 6, error: null,
    join: {
      name, left: 'invoice_data', right: 'po_data', how: 'left',
      left_on: ['PO_NUMBER_LINK'], right_on: ['PO_NUMBER'], created_by: 'agent',
    },
    ...overrides,
  }
}
function profile(duplicates = 0): TableProfile {
  return {
    rows: 52, columns: 3, sampled: false, sample_rows: 52, duplicate_rows: duplicates,
    estimated_size_bytes: 5120, column_profiles: [],
  }
}

const FACTS: TablesFacts = {
  coverage: {
    invoice_data: [
      { column: 'INVOICE_NO', tests: ['DAT-1'] },
      { column: 'AMOUNT', tests: ['DAT-1', 'DAT-2'] },
      { column: 'DUE_DATE', tests: [] },
    ],
    po_data: [{ column: 'PO_NUMBER', tests: ['DAT-1'] }],
  },
  profiles: { invoice_data: profile(), po_data: profile(4) },
  rulesets: [{ id: 'RS-1', table: 'invoice_data' } as RuleSet],
}

const TABLES = [file('invoice_data'), file('po_data'), file('broken', { error: 'Could not read the file.' }), join('invoice_po')]

describe('tables status', () => {
  it('counts each answer against the population it is about', () => {
    const model = tablesStatus(TABLES, FACTS)
    const byKey = Object.fromEntries(model.lanes.map(lane => [lane.key, lane]))

    // Profiling counts every table that loaded; coverage and rules are per file.
    expect(`${byKey.profiled.value}/${byKey.profiled.total}`).toBe('2/3')
    expect(`${byKey.tested.value}/${byKey.tested.total}`).toBe('1/3')
    expect(`${byKey.validated.value}/${byKey.validated.total}`).toBe('1/3')
  })

  it('promotes only filters it derives, and puts the gap first', () => {
    const known = new Set(
      (tablesStatus(TABLES, FACTS).filters ?? []).flatMap(group => group.options.map(option => option.key)),
    )
    for (const chip of TABLE_CHIPS) expect(known.has(chip.filter)).toBe(true)
    expect(TABLE_CHIPS[0].filter).toBe('untested')
  })
})

describe('narrowing the list', () => {
  const names = (filter: Parameters<typeof filterTables>[1]) =>
    filterTables(TABLES, filter, FACTS).map(table => table.name)

  it('selects what each chip counts', () => {
    expect(names('untested')).toEqual(['invoice_data'])
    expect(names('duplicates')).toEqual(['po_data'])
    expect(names('broken')).toEqual(['broken'])
    expect(names('no_rules')).toEqual(['po_data', 'broken'])
    expect(names('agent_built')).toEqual(['invoice_po'])
  })

  it('reports nothing untested for a table whose coverage has not loaded', () => {
    expect(untestedColumns({ ...FACTS, coverage: {} }, 'invoice_data')).toEqual([])
  })
})

describe('what a row says', () => {
  it('states the shape in the notation a reader of tables already has', () => {
    expect(tableMeta(file('invoice_data'), FACTS)).toBe('52 × 3 · 1 untested')
    expect(tableMeta(file('po_data'), FACTS)).toBe('52 × 3 · 4 duplicate')
  })

  it('describes a join by the join, which its name failed to', () => {
    // The keys are on the detail; the row has width for the two names.
    expect(tableMeta(join('invoice_po'), FACTS)).toBe('invoice_data ⋈ po_data')
  })

  it('leads with the error when the file would not load', () => {
    expect(tableMeta(file('broken', { error: 'Could not read the file.' }), FACTS))
      .toBe('Could not read the file.')
  })

  it('stays neutral until something is actually known', () => {
    expect(tableTone(file('unknown'), FACTS)).toBe('neutral')
    expect(tableTone(file('invoice_data'), FACTS)).toBe('warn')
    expect(tableTone(file('po_data'), FACTS)).toBe('warn')
    expect(tableTone(file('broken', { error: 'nope' }), FACTS)).toBe('bad')
  })
})
