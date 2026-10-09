import { portion } from '../ui/statusLanes'
import type { StatusFilterGroup, StatusLane, StatusModel } from '../ui/statusLanes'
import { sentenceCase } from '../../format'
import type { ColumnProfile, RuleSet, TableInfo, TableProfile } from '../../types'

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
  /** Columns, across every file whose coverage has been read, that a test names. */
  testedColumnTotal: number
  tablesWithUntested: number
  validated: number
  withoutRules: number
  agentBuilt: number
}

function tally(tables: TableInfo[], facts: TablesFacts): Counts {
  const counts: Counts = {
    tables: tables.length, files: 0, joins: 0, profiled: 0, broken: 0, duplicates: 0,
    fullyTested: 0, covered: 0, untestedColumnTotal: 0, testedColumnTotal: 0, tablesWithUntested: 0,
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
        counts.testedColumnTotal += known.length - untested.length
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
 * The page's state, said once in the header: `10 files · 15 joins · all
 * profiled · no column is covered by a test yet`. It replaces three progress
 * lanes and a row of chips that restated the same counts.
 */
export function tablesSentence(tables: TableInfo[], facts: TablesFacts): string {
  const counts = tally(tables, facts)
  const loaded = counts.tables - counts.broken
  const parts = [`${counts.files} ${counts.files === 1 ? 'file' : 'files'}`]
  if (counts.joins) parts.push(`${counts.joins} ${counts.joins === 1 ? 'join' : 'joins'}`)
  if (counts.broken) parts.push(`${counts.broken} failed to load`)
  parts.push(counts.profiled >= loaded ? 'all profiled' : `${counts.profiled} of ${loaded} profiled`)
  if (counts.covered) {
    if (!counts.testedColumnTotal) parts.push('no column is covered by a test yet')
    else if (counts.fullyTested === counts.files) parts.push('every column is tested')
    else parts.push(`${counts.tablesWithUntested} of ${counts.files} files have untested columns`)
  }
  return parts.join(' · ')
}

/**
 * The page's one filter control, in reading order: everything, what the audit
 * has not looked at, what nobody wrote rules for, what the data is wrong
 * about, and what would not load. All always stands; the rest appear while
 * something matches. Who built a join is a second axis, offered as a checkbox.
 */
export const TABLE_QUEUES: Array<{ key: TablesFilter | ''; label: string }> = [
  { key: '', label: 'All' },
  { key: 'untested', label: 'Columns untested' },
  { key: 'no_rules', label: 'No validation rules' },
  { key: 'duplicates', label: 'Duplicate rows' },
  { key: 'broken', label: 'Failed to load' },
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
 * What a table is called on the page: `Deals` for `04_deals`, and
 * `Deals + Confirmations` for the join the assistant named
 * `04_deals_05_confirmations_joined`.
 *
 * A table's name is the identifier queries and tests use, so it is never
 * changed; this is only how a row reads. Files lose the ordinal a folder sorts
 * by and read as words. A join is read off its record rather than its name,
 * because a join of a join is named by concatenation and reached
 * `05_confirmations_04_deals_03_counterparties_joined_joined`.
 */
export function tableLabel(name: string, tables: TableInfo[], depth = 0): string {
  const table = tables.find(item => item.name === name)
  if (table?.join && depth < 4) {
    const side = (part: string) => {
      const label = tableLabel(part, tables, depth + 1)
      return tables.find(item => item.name === part)?.join ? `(${label})` : label
    }
    return `${side(table.join.left)} + ${side(table.join.right)}`
  }
  const words = name.replace(/^\d+[_\s-]+/, '').replace(/[_]+/g, ' ').trim()
  return sentenceCase(words || name)
}

/** The keys a join matches on: `on DEAL_ID`, or `CAPTURED_BY_ID = STAFF_ID`. */
export function joinKeys(table: TableInfo): string {
  const join = table.join
  if (!join) return ''
  if (join.left_on.join(',') === join.right_on.join(',')) return `on ${join.left_on.join(', ')}`
  return join.left_on.map((key, index) => `${key} = ${join.right_on[index] ?? '?'}`).join(', ')
}

/**
 * The right edge of a list row: a file's shape in the notation a reader of
 * tables already has (`1,000 × 21`). A join has none there — its name is long
 * enough already, and its keys go on the line under it.
 */
export function tableShape(table: TableInfo): string {
  if (table.join || table.error) return ''
  return `${(table.rows ?? 0).toLocaleString()} × ${table.columns ?? 0}`
}

/**
 * The line under a row's name: only what is wrong with the table, so a clean
 * table's row is one line. The shape it used to repeat is on the right edge.
 */
export function tableIssues(table: TableInfo, facts: TablesFacts): string {
  if (table.error) return table.error
  const parts: string[] = []
  const untested = untestedColumns(facts, table.name).length
  if (untested) parts.push(`${untested} ${untested === 1 ? 'column' : 'columns'} untested`)
  const duplicates = duplicateRows(facts, table.name)
  if (duplicates) parts.push(`${duplicates.toLocaleString()} duplicate ${duplicates === 1 ? 'row' : 'rows'}`)
  return parts.join(' · ')
}

/**
 * What the profile can say about one column without being asked: that it is a
 * key, which table it joins, that it is mostly or wholly blank, or holds one
 * value. Each is read off the profile and the join records — nothing here is
 * a test, and a note never claims a column is wrong, only what it looks like.
 */
export function columnNotes(
  column: ColumnProfile, table: string, tables: TableInfo[],
): Array<{ text: string; tone?: 'warn' }> {
  const notes: Array<{ text: string; tone?: 'warn' }> = []
  const rows = column.total
  // Unique is a fact; *key* is a reading of it, and in a 22-row staff table
  // every name and job title is unique. Only an identifier-shaped name earns it.
  if (rows > 1 && column.blank_count === 0 && column.distinct_count === rows) {
    notes.push({ text: /(^|_)(id|no|num|number|ref|reference|code|key)$/i.test(column.name) ? 'Unique · a key' : 'Unique' })
  }
  for (const join of tables) {
    const spec = join.join
    if (!spec) continue
    if (spec.left === table && spec.left_on.includes(column.name)) notes.push({ text: `Joins ${tableLabel(spec.right, tables)}` })
    else if (spec.right === table && spec.right_on.includes(column.name)) notes.push({ text: `Joined from ${tableLabel(spec.left, tables)}` })
  }
  if (rows && column.blank_count === rows) notes.push({ text: 'Always blank', tone: 'warn' })
  else if (column.blank_pct >= 50) notes.push({ text: 'Mostly blank' })
  else if (rows > 1 && column.distinct_count === 1) notes.push({ text: 'One value throughout' })
  return [...new Map(notes.map(note => [note.text, note])).values()]
}

export function tableTone(table: TableInfo, facts: TablesFacts): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (table.error) return 'bad'
  if (!facts.profiles[table.name] && !facts.coverage[table.name]) return 'neutral'
  if (duplicateRows(facts, table.name) > 0) return 'warn'
  if (isFile(table) && untestedColumns(facts, table.name).length) return 'warn'
  return 'ok'
}
