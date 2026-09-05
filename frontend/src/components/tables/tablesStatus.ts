import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'
import type { RuleSet, TableInfo, TableProfile } from '../../types'

/**
 * Whether the engagement data is ready to be tested against, in three answers.
 *
 * The page listed tables with a row count and a dot whose meaning was a
 * tooltip, and said nothing about the question the data actually raises: of
 * the columns the auditee supplied, which ones does no test evaluate? That
 * gap is what `column_coverage` was written to measure, and until now it
 * reached only the report, as counts.
 *
 * Three lanes, because they fail independently. A table can be profiled and
 * untested, tested and unvalidated, or validated with half its columns
 * unexamined — and the last of those reads as finished on every screen that
 * shows only a green dot.
 */

export type TablesFilter =
  | 'untested' | 'duplicates' | 'broken' | 'no_rules' | 'agent_built' | 'files' | 'joins'

/** What the page has managed to load about each table, so far. */
export interface TablesFacts {
  /** Per table, per column, the ids of the data tests naming it. */
  coverage: Record<string, Array<{ column: string; tests: string[] }>>
  profiles: Record<string, TableProfile>
  rulesets: RuleSet[]
}

export const EMPTY_FACTS: TablesFacts = { coverage: {}, profiles: {}, rulesets: [] }

function isFile(table: TableInfo): boolean { return table.kind !== 'join' }

/** Columns of one table that no data test names. Empty when unknown. */
export function untestedColumns(facts: TablesFacts, table: string): string[] {
  return (facts.coverage[table] ?? []).filter(item => !item.tests.length).map(item => item.column)
}

export function ruleSetsFor(facts: TablesFacts, table: string): RuleSet[] {
  return (facts.rulesets ?? []).filter(item => item.table === table)
}

export function duplicateRows(facts: TablesFacts, table: string): number {
  return facts.profiles[table]?.duplicate_rows ?? 0
}

/** Built by the assistant, which the join record says and the table row cannot. */
export function isAgentBuilt(table: TableInfo): boolean {
  return table.join?.created_by === 'agent'
}

interface Counts {
  tables: number
  files: number
  joins: number
  profiled: number
  broken: number
  duplicates: number
  /** Files every one of whose columns some test names. */
  fullyTested: number
  /** Files whose coverage has been read at all. */
  covered: number
  untestedColumnTotal: number
  tablesWithUntested: number
  validated: number
  withoutRules: number
  agentBuilt: number
}

function tally(tables: TableInfo[], facts: TablesFacts): Counts {
  const counts: Counts = {
    tables: tables.length, files: 0, joins: 0, profiled: 0, broken: 0, duplicates: 0,
    fullyTested: 0, covered: 0, untestedColumnTotal: 0, tablesWithUntested: 0,
    validated: 0, withoutRules: 0, agentBuilt: 0,
  }
  for (const table of tables) {
    if (isFile(table)) counts.files += 1
    else counts.joins += 1
    if (table.error) { counts.broken += 1; continue }
    if (facts.profiles[table.name]) counts.profiled += 1
    if (duplicateRows(facts, table.name) > 0) counts.duplicates += 1
    if (isAgentBuilt(table)) counts.agentBuilt += 1
    if (isFile(table)) {
      if (ruleSetsFor(facts, table.name).length) counts.validated += 1
      else counts.withoutRules += 1
      const known = facts.coverage[table.name]
      if (known) {
        counts.covered += 1
        const untested = untestedColumns(facts, table.name)
        counts.untestedColumnTotal += untested.length
        if (untested.length) counts.tablesWithUntested += 1
        else counts.fullyTested += 1
      }
    }
  }
  return counts
}

function lane(
  key: string, label: string, value: number, total: number, caption: string,
  tone: 'ok' | 'warn' | 'bad',
): StatusLane {
  return {
    key, label, state: total === 0 ? 'idle' : value === total ? 'done' : 'gap',
    value: String(value), total: String(total), caption,
    segments: [{ tone: value === total ? 'ok' : tone, portion: portion(value, total) }],
    chips: [], actions: [], rest: '',
  }
}

function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'coverage',
      label: 'Coverage',
      options: [
        { key: 'untested', label: 'Columns untested', value: counts.tablesWithUntested, tone: 'warn' },
        { key: 'no_rules', label: 'No validation rules', value: counts.withoutRules, tone: 'neutral' },
      ],
    },
    {
      key: 'condition',
      label: 'Condition',
      options: [
        { key: 'duplicates', label: 'Duplicate rows', value: counts.duplicates, tone: 'warn' },
        { key: 'broken', label: 'Failed to load', value: counts.broken, tone: 'bad' },
      ],
    },
    {
      key: 'origin',
      label: 'Origin',
      options: [
        { key: 'files', label: 'Files', value: counts.files, tone: 'neutral' },
        { key: 'joins', label: 'Joins', value: counts.joins, tone: 'neutral' },
        { key: 'agent_built', label: 'Built by the assistant', value: counts.agentBuilt, tone: 'neutral' },
      ],
    },
  ]
}

export function tablesStatus(tables: TableInfo[], facts: TablesFacts): StatusModel {
  const counts = tally(tables, facts)
  return {
    lanes: [
      lane('profiled', 'Profiled', counts.profiled, counts.tables - counts.broken,
        `of ${counts.tables - counts.broken} tables profiled`, 'warn'),
      lane('tested', 'Tested', counts.fullyTested, counts.files,
        `of ${counts.files} files have every column evaluated by a test`, 'warn'),
      lane('validated', 'Validated', counts.validated, counts.files,
        `of ${counts.files} files carry a rule set`, 'warn'),
    ],
    disclosures: [],
    filters: filtersFor(counts),
  }
}

/**
 * The five narrowings worth a permanent chip, in reading order: what the audit
 * has not looked at, what the data itself is wrong about, what would not load,
 * what nobody wrote rules for, and what the assistant built.
 */
export const TABLE_CHIPS: ReviewChip[] = [
  { filter: 'untested', tone: 'warn', label: 'Columns untested' },
  { filter: 'duplicates', tone: 'warn', label: 'Duplicate rows' },
  { filter: 'broken', tone: 'bad', label: 'Failed to load' },
  { filter: 'no_rules', tone: 'neutral', label: 'No validation rules' },
  { filter: 'agent_built', tone: 'agent', label: 'Built by the assistant' },
]

/** Narrow the same list the meters counted. */
export function filterTables(
  tables: TableInfo[], filter: TablesFilter | null, facts: TablesFacts,
): TableInfo[] {
  if (!filter) return tables
  return tables.filter(table => {
    switch (filter) {
      case 'untested': return isFile(table) && untestedColumns(facts, table.name).length > 0
      case 'duplicates': return duplicateRows(facts, table.name) > 0
      case 'broken': return Boolean(table.error)
      case 'no_rules': return isFile(table) && !ruleSetsFor(facts, table.name).length
      case 'agent_built': return isAgentBuilt(table)
      case 'files': return isFile(table)
      case 'joins': return !isFile(table)
      default: return true
    }
  })
}

/**
 * What one table's row says after its name: the shape of the population, and
 * what is wrong with it — in the shortest form that still says it.
 *
 * `52 rows · 15 columns · 1 column untested` is nine words to carry three
 * numbers, repeated down eighteen rows. `52 × 15` is the notation a reader of
 * tables already has for the first two, which leaves the row's width for the
 * one thing that is actually wrong with this table. A join says what it joins
 * instead, because its name — `invoice_data_po_data_joined` — already failed
 * to; the keys it joins on are on the detail, where there is room for them.
 */
export function tableMeta(table: TableInfo, facts: TablesFacts): string {
  if (table.error) return table.error
  if (table.join) return `${table.join.left} ⋈ ${table.join.right}`
  const parts = [`${(table.rows ?? 0).toLocaleString()} × ${table.columns ?? 0}`]
  const untested = untestedColumns(facts, table.name).length
  if (untested) parts.push(`${untested} untested`)
  const duplicates = duplicateRows(facts, table.name)
  if (duplicates) parts.push(`${duplicates.toLocaleString()} duplicate`)
  return parts.join(' · ')
}

export function tableTone(table: TableInfo, facts: TablesFacts): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (table.error) return 'bad'
  if (!facts.profiles[table.name] && !facts.coverage[table.name]) return 'neutral'
  if (duplicateRows(facts, table.name) > 0) return 'warn'
  if (isFile(table) && untestedColumns(facts, table.name).length) return 'warn'
  return 'ok'
}
