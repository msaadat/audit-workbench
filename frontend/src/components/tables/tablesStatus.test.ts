import { describe, expect, it } from 'vitest'

import type { ColumnProfile, RuleSet, TableInfo, TableProfile } from '../../types'
import {
  TABLE_QUEUES, columnNotes, filterTables, joinKeys, tableIssues, tableLabel, tableShape, tableTone,
  tablesSentence, tablesStatus, untestedColumns,
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
    for (const queue of TABLE_QUEUES) if (queue.key) expect(known.has(queue.key)).toBe(true)
    expect(TABLE_QUEUES[1].key).toBe('untested')
  })

  it('says the page state in one sentence', () => {
    expect(tablesSentence(TABLES, FACTS)).toBe('3 files · 1 join · 1 failed to load · 2 of 3 profiled · 1 of 3 files have untested columns')
    const untested = { ...FACTS, coverage: { invoice_data: [{ column: 'A', tests: [] }] } }
    expect(tablesSentence(TABLES, untested)).toContain('no column is covered by a test yet')
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
  it('states the shape on the right and only what is wrong underneath', () => {
    expect(tableShape(file('invoice_data'))).toBe('52 × 3')
    expect(tableIssues(file('invoice_data'), FACTS)).toBe('1 column untested')
    expect(tableIssues(file('po_data'), FACTS)).toBe('4 duplicate rows')
    expect(tableIssues(file('clean'), FACTS)).toBe('')
  })

  it('describes a join by its keys, which its name failed to', () => {
    expect(tableShape(join('invoice_po'))).toBe('')
    expect(joinKeys(join('invoice_po'))).toBe('PO_NUMBER_LINK = PO_NUMBER')
    expect(joinKeys(join('j', { join: { name: 'j', left: 'a', right: 'b', how: 'left', left_on: ['ID'], right_on: ['ID'] } }))).toBe('on ID')
  })

  it('leads with the error when the file would not load', () => {
    expect(tableIssues(file('broken', { error: 'Could not read the file.' }), FACTS))
      .toBe('Could not read the file.')
  })

  it('names files in words and joins from their record, nesting included', () => {
    const tables = [
      file('04_deals'), file('05_confirmations'), file('03_counterparties'),
      join('04_deals_05_confirmations_joined', { join: { name: 'x', left: '04_deals', right: '05_confirmations', how: 'left', left_on: ['DEAL_ID'], right_on: ['DEAL_ID'] } }),
      join('nested', { join: { name: 'y', left: '04_deals_05_confirmations_joined', right: '03_counterparties', how: 'left', left_on: ['C'], right_on: ['C'] } }),
    ]
    expect(tableLabel('04_deals', tables)).toBe('Deals')
    expect(tableLabel('04_deals_05_confirmations_joined', tables)).toBe('Deals + Confirmations')
    expect(tableLabel('nested', tables)).toBe('(Deals + Confirmations) + Counterparties')
  })

  it('notes keys, joins and blanks from the profile alone', () => {
    const column = (overrides: Partial<ColumnProfile>): ColumnProfile => ({
      name: 'PO_NUMBER_LINK', dtype: 'String', total: 10, blank_count: 0, blank_pct: 0,
      distinct_count: 10, distinct_pct: 100, inferred_type: 'text', min: null, max: null, mean: null,
      top_values: [], ...overrides,
    })
    const tables = [file('invoice_data'), file('po_data'), join('invoice_po')]
    expect(columnNotes(column({}), 'invoice_data', tables).map(note => note.text))
      .toEqual(['Unique', 'Joins PO data'])
    expect(columnNotes(column({ name: 'PO_NUMBER' }), 'po_data', tables).map(note => note.text))
      .toEqual(['Unique · a key', 'Joined from Invoice data'])
    expect(columnNotes(column({ name: 'X', blank_count: 7, blank_pct: 70, distinct_count: 2 }), 'invoice_data', tables))
      .toEqual([{ text: 'Mostly blank' }])
    expect(columnNotes(column({ name: 'X', blank_count: 10, blank_pct: 100, distinct_count: 0 }), 'invoice_data', tables))
      .toEqual([{ text: 'Always blank', tone: 'warn' }])
  })

  it('stays neutral until something is actually known', () => {
    expect(tableTone(file('unknown'), FACTS)).toBe('neutral')
    expect(tableTone(file('invoice_data'), FACTS)).toBe('warn')
    expect(tableTone(file('po_data'), FACTS)).toBe('warn')
    expect(tableTone(file('broken', { error: 'nope' }), FACTS)).toBe('bad')
  })
})
