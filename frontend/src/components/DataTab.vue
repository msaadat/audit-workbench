<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import { api, ApiError } from '../api'
import { plural, pluralWord, sentenceCase } from '../format'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  ColumnProfile, FramePayload, RuleSet, TableInfo, TableProfile, WorkspaceSummary,
} from '../types'
import FrameTable from './FrameTable.vue'
import TableValidation from './validation/TableValidation.vue'
import JoinDrawer from './tables/JoinDrawer.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import {
  EMPTY_FACTS, TABLE_QUEUES, columnNotes, filterTables, isAgentBuilt, joinKeys, ruleSetsFor,
  tableIssues, tableLabel, tableShape, tableTone, tablesSentence, tablesStatus, untestedColumns,
} from './tables/tablesStatus'
import type { TablesFacts, TablesFilter } from './tables/tablesStatus'

/**
 * The engagement's data: what was imported, what shape it is in, and what of
 * it the audit has actually evaluated.
 *
 * The page used to answer the first of those and half of the second. A row
 * said `52×15` and carried a dot whose meaning was a tooltip; the panel below
 * spent four stat cards restating the row count it had just been clicked from.
 * The question it could not answer at all is the one `column_coverage`
 * measures — which columns no test names — and that gap is where an invoice of
 * 80,000,000 against a purchase order of 8,000,000 sat unexamined.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'import-requested': [] }>()

const toast = useToast()
const confirm = useConfirm()
const nav = useWorkspaceNav()

const replaceInput = ref<HTMLInputElement>()
const replaceTarget = ref<string | null>(null)
const replacing = ref(false)
const joinOpen = ref(false)
const search = ref('')
const statusFilter = ref<TablesFilter[]>([])
const selected = ref<string | null>(null)
const tab = ref<'profile' | 'preview' | 'validation' | 'relationships'>('profile')
const expanded = ref<Set<string>>(new Set())

const profiling = ref(false)
const previewLoading = ref(false)
const preview = ref<{ table: string; frame: FramePayload; total: number } | null>(null)
const renaming = ref(false)
const renameDraft = ref('')

/** Everything the page has learned about the tables, in one bag. */
const facts = ref<TablesFacts>({ ...EMPTY_FACTS, coverage: {}, profiles: {}, rulesets: [] })

const tables = computed(() => props.workspace.tables)
const status = computed(() => tablesStatus(tables.value, facts.value))
const scoped = computed(() => statusFilter.value.reduce<TableInfo[]>(
  (rows, key) => filterTables(rows, key, facts.value), tables.value,
))
const visible = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return scoped.value
  return scoped.value.filter(table =>
    `${table.name} ${tableLabel(table.name, tables.value)}`.toLowerCase().includes(needle))
})
/**
 * The filters as one choice, as on Documents: All and whatever currently has
 * something in it. Who built a join is a second axis and a checkbox.
 */
const filterCounts = computed(() => new Map(
  (status.value.filters ?? []).flatMap(group => group.options.map(option => [option.key, option.value] as const)),
))
const queueFilter = computed<TablesFilter | ''>(() => statusFilter.value.find(key => key !== 'agent_built') ?? '')
const queues = computed(() => TABLE_QUEUES
  .filter(option => !option.key || (filterCounts.value.get(option.key) ?? 0) > 0 || option.key === queueFilter.value)
  .map(option => ({ ...option, count: option.key ? filterCounts.value.get(option.key) ?? 0 : tables.value.length })))
const agentBuiltOnly = computed({
  get: () => statusFilter.value.includes('agent_built'),
  set: (on: boolean) => {
    statusFilter.value = [...(queueFilter.value ? [queueFilter.value] : []), ...(on ? ['agent_built' as const] : [])]
  },
})
function setQueue(key: TablesFilter | '') {
  statusFilter.value = [...(key ? [key] : []), ...(agentBuiltOnly.value ? ['agent_built' as const] : [])]
}
const sentence = computed(() => tablesSentence(tables.value, facts.value))
const label = (name: string) => tableLabel(name, tables.value)

const groups = computed(() => [
  { key: 'files', label: 'Files', tables: visible.value.filter(table => table.kind !== 'join') },
  { key: 'joins', label: 'Joins', tables: visible.value.filter(table => table.kind === 'join') },
].filter(group => group.tables.length))

const selectedTable = computed(() => tables.value.find(table => table.name === selected.value) ?? null)
const profile = computed(() => (selected.value ? facts.value.profiles[selected.value] ?? null : null))
const coverage = computed(() => (selected.value ? facts.value.coverage[selected.value] ?? null : null))
const untested = computed(() => (selected.value ? untestedColumns(facts.value, selected.value) : []))
const rules = computed(() => (selected.value ? ruleSetsFor(facts.value, selected.value) : []))
const relatedJoins = computed(() => tables.value.filter(
  table => table.join && (table.join.left === selected.value || table.join.right === selected.value),
))

/** Which tests name one column, for the profile's `Tested` cell. */
function testsFor(column: string): string[] {
  return coverage.value?.find(item => item.column === column)?.tests ?? []
}

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function loadRulesets() {
  try {
    const payload = await api.get<{ rulesets: RuleSet[] }>(`/api/workspaces/${props.workspace.id}/rulesets`)
    facts.value = { ...facts.value, rulesets: payload.rulesets }
  } catch { /* the meters simply stay unknown */ }
}

/**
 * Profile and coverage for every table, in the background.
 *
 * Both are read by the review bar and by every row's meta line, so fetching
 * them only for the selected table would leave the counts above the list
 * disagreeing with the list. Both are cached server-side on the workspace
 * signature, so the sweep is cheap after the first pass.
 */
let sweepToken = 0
async function sweep() {
  const token = ++sweepToken
  for (const table of tables.value) {
    if (token !== sweepToken) return
    if (table.error) continue
    if (!facts.value.profiles[table.name]) {
      try {
        const result = await api.get<TableProfile>(`/api/workspaces/${props.workspace.id}/tables/${table.name}/profile`)
        if (token !== sweepToken) return
        facts.value = { ...facts.value, profiles: { ...facts.value.profiles, [table.name]: result } }
      } catch { /* the row stays "not profiled" */ }
    }
    if (table.kind !== 'join' && !facts.value.coverage[table.name]) {
      try {
        const result = await api.get<{ columns: Array<{ column: string; tests: string[] }> }>(
          `/api/workspaces/${props.workspace.id}/tables/${table.name}/coverage`,
        )
        if (token !== sweepToken) return
        facts.value = { ...facts.value, coverage: { ...facts.value.coverage, [table.name]: result.columns } }
      } catch { /* coverage stays unknown for this table */ }
    }
  }
}
onBeforeUnmount(() => { sweepToken += 1 })

watch(tables, list => {
  const names = list.map(table => table.name)
  if (selected.value && !names.includes(selected.value)) selected.value = null
  if (!selected.value) selected.value = list.find(table => !table.error)?.name ?? null
  void sweep()
  void loadRulesets()
}, { immediate: true, deep: true })

watch(visible, rows => {
  if (!rows.length || rows.some(table => table.name === selected.value)) return
  selected.value = rows.find(table => !table.error)?.name ?? null
})
watch(selected, name => {
  tab.value = 'profile'
  preview.value = null
  renaming.value = false
  expanded.value = new Set()
  if (name && !facts.value.profiles[name]) void reprofile()
})
watch(tab, value => { if (value === 'preview') void loadPreview() })

async function reprofile() {
  const table = selected.value
  if (!table) return
  profiling.value = true
  try {
    const result = await api.get<TableProfile>(`/api/workspaces/${props.workspace.id}/tables/${table}/profile`)
    facts.value = { ...facts.value, profiles: { ...facts.value.profiles, [table]: result } }
  } catch (error) { fail('Profiling failed', error) }
  finally { profiling.value = false }
}

async function loadPreview() {
  const table = selectedTable.value
  if (!table || table.error || preview.value?.table === table.name) return
  previewLoading.value = true
  try {
    const frame = await api.get<FramePayload & { total_rows: number }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.name}/preview?rows=100`,
    )
    preview.value = { table: table.name, frame, total: frame.total_rows }
  } catch (error) { fail('Preview failed', error) }
  finally { previewLoading.value = false }
}

function startReplace(table: TableInfo) {
  replaceTarget.value = table.name
  replaceInput.value?.click()
}

async function replaceData(event: Event) {
  const input = event.target as HTMLInputElement
  const file = (input.files ?? [])[0]
  input.value = ''
  const table = replaceTarget.value
  if (!file || !table) return
  replacing.value = true
  try {
    const { replaced } = await api.replace<{
      replaced: { removed_columns: string[]; added_columns: string[] }
    }>(`/api/workspaces/${props.workspace.id}/tables/${table}`, file)
    forget(table)
    emit('changed')
    const { removed_columns, added_columns } = replaced
    if (removed_columns.length) {
      toast.add({
        severity: 'warn',
        summary: `Replaced "${table}" — schema changed`,
        detail: `Dropped ${pluralWord(removed_columns.length, 'column')}: ${removed_columns.join(', ')}. `
          + 'Saved queries or analyses using them may now error until updated.',
        life: 9000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: `Replaced "${table}"`,
        detail: added_columns.length
          ? `New ${pluralWord(added_columns.length, 'column')}: ${added_columns.join(', ')}.`
          : 'Saved queries and analyses now use the new data.',
        life: 5000,
      })
    }
  } catch (error) { fail('Replace failed', error) }
  finally { replacing.value = false; replaceTarget.value = null }
}

/** Drop what we knew about a table whose data has moved under us. */
function forget(table: string) {
  const { [table]: droppedProfile, ...profiles } = facts.value.profiles
  const { [table]: droppedCoverage, ...coverageRest } = facts.value.coverage
  facts.value = { ...facts.value, profiles, coverage: coverageRest }
  void sweep()
}

function startRename() {
  renameDraft.value = selected.value ?? ''
  renaming.value = true
}
async function commitRename() {
  const table = selectedTable.value
  const next = renameDraft.value.trim()
  renaming.value = false
  if (!table || !next || next === table.name) return
  try {
    const { renamed } = await api.patch<{
      renamed: { old_name: string; name: string; updated: { joins: number; analyses: number; rulesets: number; python_snippets: number } }
    }>(`/api/workspaces/${props.workspace.id}/tables/${table.name}`, { name: next })
    forget(renamed.old_name)
    selected.value = renamed.name
    emit('changed')
    const total = renamed.updated.joins + renamed.updated.analyses + renamed.updated.rulesets
    const code = renamed.updated.python_snippets
      ? ` Updated ${plural(renamed.updated.python_snippets, 'saved Python snippet')}.`
      : ''
    toast.add({
      severity: 'success',
      summary: `Renamed "${renamed.old_name}" to "${renamed.name}"`,
      detail: total ? `Updated ${plural(total, 'saved reference')}.${code}` : code || 'No saved references needed changes.',
      life: 6000,
    })
  } catch (error) { fail('Rename failed', error) }
}

function removeTable() {
  const table = selectedTable.value
  if (!table) return
  confirm.require({
    header: `Remove ${table.kind === 'join' ? 'join' : 'table'}`,
    message: table.kind === 'join'
      ? `Remove join "${table.name}"?`
      : `Remove "${table.name}" and delete its file from the workspace?`,
    icon: 'aw-icon aw-icon-triangle-alert',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        const endpoint = table.kind === 'join' ? 'joins' : 'tables'
        await api.del(`/api/workspaces/${props.workspace.id}/${endpoint}/${table.name}`)
        selected.value = null
        emit('changed')
      } catch (error) { fail('Remove failed', error) }
    },
  })
}

async function download() {
  const table = selectedTable.value
  if (!table) return
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${table.name}/query/export`,
      {}, `${table.name}.xlsx`,
    )
  } catch (error) { fail('Export failed', error) }
}

const menuItems = computed(() => [
  { label: 'Rename', icon: 'aw-icon aw-icon-pencil', disabled: !selectedTable.value, command: startRename },
  { label: 'Profile again', icon: 'aw-icon aw-icon-refresh-cw', disabled: !selectedTable.value, command: () => void reprofile() },
  { label: 'Export table', icon: 'aw-icon aw-icon-download', disabled: !selectedTable.value, command: () => void download() },
  { separator: true },
  { label: 'Remove', icon: 'aw-icon aw-icon-trash-2', disabled: !selectedTable.value, command: removeTable },
])

function toggleExpanded(column: string) {
  const next = new Set(expanded.value)
  if (!next.delete(column)) next.add(column)
  expanded.value = next
}

function rangeText(item: ColumnProfile): string {
  if (item.min === null && item.max === null) return ''
  const range = `${item.min ?? '?'} – ${item.max ?? '?'}`
  return item.mean !== null ? `${range} (mean ${item.mean})` : range
}

/**
 * What the selected table is, in one line under its name: its shape, its
 * condition, its period where a date column gives one, how much of it the
 * tests evaluate, and how many joins draw on it. A verdict box with three
 * slots used to say this in three sentences, the first a row of figures and
 * the last a pair of buttons for rule sets the Validation tab already holds.
 */
const factLine = computed(() => {
  const table = selectedTable.value
  if (!table || table.error || !profile.value) return []
  const parts: Array<{ text: string; tone?: 'warn' }> = [
    { text: plural(profile.value.rows, 'row') },
    { text: plural(profile.value.columns, 'column') },
    profile.value.duplicate_rows
      ? { text: plural(profile.value.duplicate_rows, 'duplicate row'), tone: 'warn' }
      : { text: 'no duplicate rows' },
  ]
  if (table.kind !== 'join' && coverage.value) {
    const tested = coverage.value.length - untested.value.length
    parts.push({ text: `${tested} of ${coverage.value.length} columns tested`, tone: tested < coverage.value.length ? 'warn' : undefined })
  }
  if (relatedJoins.value.length) parts.push({ text: `used by ${plural(relatedJoins.value.length, 'join')}` })
  return parts
})

/** The identifier under the readable name: the file it came from, or how the join was built. */
const identity = computed(() => {
  const table = selectedTable.value
  if (!table) return ''
  if (table.join) return `${table.name} · ${table.join.how} join ${joinKeys(table)}${isAgentBuilt(table) ? ' · built by the assistant' : ''}`
  return table.source
})

function openTest(id: string) { void nav.push('data-tests', { test: id }) }
</script>

<template>
  <div class="tables">
    <header class="page-head">
      <div class="head-copy">
        <h1>Source tables</h1>
        <p v-if="tables.length" class="aw-type-meta head-count">{{ sentence }}</p>
      </div>
      <span class="grow" />
      <input ref="replaceInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="replaceData" />
      <!-- Neither is the next step of the audit, so neither is filled. -->
      <Button
        v-if="tables.length"
        label="Add join"
        icon="aw-icon aw-icon-link"
        size="small"
        outlined
        severity="secondary"
        :disabled="tables.length < 2"
        @click="joinOpen = true"
      />
      <Button v-if="tables.length" label="Add files" icon="aw-icon aw-icon-upload" size="small" outlined severity="secondary" @click="emit('import-requested')" />
      <UiOverflowMenu :items="menuItems" tooltip="More table actions" />
    </header>

    <div v-if="tables.length" class="aw-filter-row">
      <div class="aw-segmented" role="group" aria-label="Show">
        <button
          v-for="option in queues"
          :key="option.key || 'all'"
          type="button"
          :class="{ on: queueFilter === option.key }"
          :aria-pressed="queueFilter === option.key"
          @click="setQueue(option.key)"
        >{{ option.label }} <span class="aw-figure">{{ option.count }}</span></button>
      </div>
      <label v-if="filterCounts.get('agent_built')" class="aw-filter-check">
        <input v-model="agentBuiltOnly" type="checkbox">
        Only what the assistant built <span class="aw-figure">{{ filterCounts.get('agent_built') }}</span>
      </label>
    </div>

    <div v-if="tables.length" class="layout">
      <section class="list-panel" aria-label="Tables">
        <div class="list-head">
          <IconField>
            <InputIcon class="aw-icon aw-icon-search" />
            <InputText v-model="search" size="small" :placeholder="`Filter ${plural(visible.length, 'table')}`" />
          </IconField>
        </div>
        <div class="list-body">
          <section v-for="group in groups" :key="group.key">
            <p class="group">
              <span class="group-name">{{ group.label }}</span>
              <span class="group-count aw-figure">{{ group.tables.length }}</span>
            </p>
            <button
              v-for="table in group.tables"
              :key="table.name"
              type="button"
              class="row"
              :class="{ active: table.name === selected }"
              :title="table.name"
              @click="selected = table.name"
            >
              <span class="dot" :data-tone="tableTone(table, facts)" aria-hidden="true" />
              <span class="copy">
                <span class="name">{{ label(table.name) }}</span>
                <span v-if="tableIssues(table, facts)" class="meta" :data-tone="table.error ? 'bad' : 'warn'">{{ tableIssues(table, facts) }}</span>
                <span v-else-if="table.join" class="meta aw-figure">{{ joinKeys(table) }}</span>
              </span>
              <span class="shape aw-figure">{{ tableShape(table) }}</span>
            </button>
          </section>
          <p v-if="!visible.length" class="empty">No table matches this view.</p>
        </div>
      </section>

      <section v-if="selectedTable" class="detail">
        <header class="detail-head">
          <div class="detail-copy">
            <!-- Renaming happens where the name is read, not in a dialog that
                 hides the list the name has to stay distinct within. -->
            <InputText
              v-if="renaming"
              v-model="renameDraft"
              class="rename"
              autofocus
              @keyup.enter="commitRename"
              @blur="commitRename"
            />
            <div v-else class="title-line">
              <h2 @dblclick="startRename">{{ label(selectedTable.name) }}</h2>
              <span class="identity aw-figure" :title="identity">{{ identity }}</span>
            </div>
            <p v-if="selectedTable.error" class="fact-line"><span class="failed">{{ selectedTable.error }}</span></p>
            <p v-else-if="factLine.length" class="fact-line">
              <template v-for="(part, index) in factLine" :key="part.text">
                <span v-if="index" aria-hidden="true"> · </span><span :class="{ warned: part.tone === 'warn' }">{{ part.text }}</span>
              </template>
            </p>
            <p v-else class="fact-line muted">{{ profiling ? 'Profiling…' : 'Not profiled yet.' }}</p>
          </div>
          <Button
            v-if="selectedTable.kind !== 'join'"
            label="Replace data"
            icon="aw-icon aw-icon-refresh-ccw"
            size="small"
            outlined
            severity="secondary"
            :loading="replacing"
            @click="startReplace(selectedTable)"
          />
        </header>

        <nav class="tabs" role="tablist">
          <button
            v-for="entry in [
              { key: 'profile', label: 'Profile', badge: '' },
              { key: 'preview', label: 'Rows', badge: '' },
              { key: 'validation', label: 'Validation', badge: rules.length ? String(rules.length) : '' },
              { key: 'relationships', label: 'Relationships', badge: relatedJoins.length ? String(relatedJoins.length) : '' },
            ]"
            :key="entry.key"
            type="button"
            role="tab"
            :aria-selected="tab === entry.key"
            :class="{ active: tab === entry.key }"
            @click="tab = (entry.key as typeof tab)"
          >
            {{ entry.label }}<span v-if="entry.badge" class="badge aw-figure">{{ entry.badge }}</span>
          </button>
        </nav>

        <template v-if="tab === 'profile'">
          <p v-if="profiling" class="note"><i class="aw-icon aw-icon-loader-circle aw-icon-spin" /> Profiling {{ label(selected ?? '') }}…</p>
          <template v-else-if="profile">
            <div class="profile-scroll">
              <table class="profile">
                <thead>
                  <tr>
                    <th class="expander"><span class="visually-hidden">Values</span></th>
                    <th>Column</th><th>Blank</th><th>Distinct</th>
                    <th>Range</th><th>Notes</th><th>Tested by</th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="column in profile.column_profiles" :key="column.name">
                    <tr>
                      <td class="expander">
                        <button type="button" :aria-expanded="expanded.has(column.name)" :aria-label="`Values of ${column.name}`" @click="toggleExpanded(column.name)">
                          <i class="aw-icon" :class="expanded.has(column.name) ? 'aw-icon-chevron-down' : 'aw-icon-chevron-right'" />
                        </button>
                      </td>
                      <td class="column-cell">
                        <b class="column-name">{{ column.name }}</b>
                        <span class="type">{{ sentenceCase(column.inferred_type) }}</span>
                      </td>
                      <td class="aw-figure nowrap">
                        <span class="blank"><i :style="{ width: `${Math.min(100, column.blank_pct)}%` }" /></span>{{ column.blank_pct }}%
                      </td>
                      <td class="aw-figure nowrap">{{ column.distinct_count.toLocaleString() }}</td>
                      <td class="aw-figure range" :title="rangeText(column)">{{ rangeText(column) }}</td>
                      <td class="notes">
                        <span
                          v-for="note in columnNotes(column, selectedTable.name, tables)"
                          :key="note.text"
                          class="note-chip"
                          :data-tone="note.tone || null"
                        >{{ note.text }}</span>
                      </td>
                      <td class="tested">
                        <template v-if="!coverage"><span class="muted">—</span></template>
                        <template v-else-if="testsFor(column.name).length">
                          <button type="button" class="tests" @click="openTest(testsFor(column.name)[0])">
                            <i class="aw-icon aw-icon-check" aria-hidden="true" />{{ plural(testsFor(column.name).length, 'test') }}
                          </button>
                        </template>
                        <span v-else class="none">None</span>
                      </td>
                    </tr>
                    <tr v-if="expanded.has(column.name)" class="values">
                      <td />
                      <td colspan="6">
                        <p v-if="!column.top_values.length" class="muted">No values.</p>
                        <div v-for="value in column.top_values" :key="value.value ?? ''" class="value-row">
                          <span class="value">{{ value.value ?? '∅' }}</span>
                          <span class="bar"><i :style="{ width: `${value.pct}%` }" /></span>
                          <span class="count aw-figure">{{ value.count.toLocaleString() }} ({{ value.pct }}%)</span>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
            <p class="note">
              Statistics over
              {{ profile.sampled ? `the first ${profile.sample_rows.toLocaleString()} rows` : `all ${profile.rows.toLocaleString()} rows` }}.
              <template v-if="selectedTable.kind === 'join'">Coverage is measured on the files a join draws from.</template>
            </p>
          </template>
          <p v-else class="note">This table has not been profiled.</p>
        </template>

        <template v-else-if="tab === 'preview'">
          <p v-if="previewLoading" class="note"><i class="aw-icon aw-icon-loader-circle aw-icon-spin" /> Loading rows…</p>
          <template v-else-if="preview">
            <p class="note">The first {{ Math.min(100, preview.total).toLocaleString() }} of {{ preview.total.toLocaleString() }} rows.</p>
            <FrameTable :frame="preview.frame" scrollHeight="28rem" />
          </template>
          <p v-else class="note">No rows to show.</p>
        </template>

        <TableValidation
          v-else-if="tab === 'validation'"
          :key="selectedTable.name"
          :workspace="workspace"
          :table="selectedTable.name"
        />

        <template v-else>
          <p v-if="!relatedJoins.length" class="note">No join uses this table.</p>
          <button
            v-for="join in relatedJoins"
            :key="join.name"
            type="button"
            class="join-row"
            @click="selected = join.name"
          >
            <span class="name">{{ label(join.name) }}</span>
            <span class="meta aw-figure">{{ joinKeys(join) }}</span>
          </button>
          <Button label="Add join" icon="aw-icon aw-icon-link" size="small" outlined severity="secondary" class="add-join" @click="joinOpen = true" />
        </template>
      </section>
      <UiEmptyState v-else icon="aw-icon aw-icon-table" title="No table selected" description="Select a table to profile it." />
    </div>

    <UiEmptyState
      v-else
      icon="aw-icon aw-icon-upload"
      title="Add engagement data"
      description="Import the populations the audit will test — invoices, purchase orders, payments — as CSV or Excel."
    >
      <Button label="Choose files" icon="aw-icon aw-icon-upload" @click="emit('import-requested')" />
    </UiEmptyState>

    <JoinDrawer v-model:visible="joinOpen" :workspace="workspace" @saved="emit('changed')" />
  </div>
</template>

<style scoped>
.tables { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

.grow { flex: 1; }
.head-copy { display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; min-width: 0; }
.head-count { margin: 0; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }

.layout { display: grid; grid-template-columns: minmax(14rem, 17.5rem) minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext) { width: 100%; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

.group { display: flex; flex-direction: column; gap: 1px; margin: 0; padding: .5rem .75rem .35rem; background: var(--aw-canvas); border-top: 1px solid var(--aw-border); }
.list-body > section:first-child .group { border-top: 0; }
.group-name { color: var(--aw-ink-strong); font-size: var(--aw-text-xs); font-weight: 600; }
.group-count { color: var(--aw-muted); font-size: var(--aw-text-2xs); }

.row {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: .625rem;
  width: 100%; min-width: 0;
  padding: .5rem .75rem;
  border: 0; border-left: 3px solid transparent;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.row:hover:not(.active) { background: var(--aw-raised); }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft); }
.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }
.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
/* The row reads by name, in words; the identifier is its tooltip. The shape
   sits on the right edge in figures, so the line under the name is free for
   the one thing that is wrong with the table, and a clean table is one line. */
.copy .name { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.row.active .name { color: var(--aw-teal-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.meta[data-tone='warn'] { color: var(--aw-warn-ink); }
.meta[data-tone='bad'] { color: var(--aw-danger); }
.shape { max-width: 9rem; overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }

.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-width: 100%; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  container: master-detail-content / inline-size;
  overflow-y: auto;
}
.detail > * { flex: none; }
.detail-head { display: flex; align-items: flex-start; gap: 1rem; min-width: 0; }
.detail-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.title-line { display: flex; align-items: baseline; gap: .625rem; min-width: 0; }
.detail-head h2, .rename { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: var(--aw-weight-title); }
.detail-head h2 { flex: none; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.identity { min-width: 0; overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.rename { width: 100%; padding: .1rem .25rem; }
.fact-line { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }

.failed { color: var(--aw-danger); }
.warned { color: var(--aw-warn-ink); font-weight: 600; }
.rules { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }

.tabs { display: flex; align-items: center; gap: 1rem; border-bottom: 1px solid var(--aw-border); }
.tabs button {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .45rem 0; margin-bottom: -1px;
  border: 0; border-bottom: 2px solid transparent;
  background: none; color: var(--aw-muted);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer;
}
.tabs button:hover { color: var(--aw-ink); }
.tabs button.active { border-bottom-color: var(--aw-teal); color: var(--aw-teal-strong); }
.tabs .badge { padding: 0 .3rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; }

.note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.profile-scroll { overflow-x: auto; }
.profile { width: 100%; border-collapse: collapse; font-size: var(--aw-text-sm); }
.profile th {
  padding: .35rem .5rem; border-bottom: 1px solid var(--aw-border);
  color: var(--aw-muted); font-size: var(--aw-text-2xs);
  font-weight: 600; letter-spacing: .06em; text-align: left; text-transform: uppercase;
}
.profile td { padding: .4rem .5rem; border-bottom: 1px solid var(--aw-border); color: var(--aw-ink); vertical-align: middle; }
.profile .expander { width: 2rem; }
.profile .expander button { padding: 0; border: 0; background: none; color: var(--aw-muted); cursor: pointer; font-size: .65rem; }
.column-name { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
/* A column's type is a word beside its name, not a coloured pill: the pills
   spent the success, warning and failure hues on `numeric`, `categorical` and
   `empty`, none of which is a state. */
.column-cell { white-space: nowrap; }
.type { margin-left: .5rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.nowrap { white-space: nowrap; }
.notes { min-width: 8rem; }
.note-chip { display: inline-block; margin: 1px 4px 1px 0; padding: 0 .4rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-xs); white-space: nowrap; }
.note-chip[data-tone='warn'] { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.blank { display: inline-block; width: 4rem; height: 5px; margin-right: .4rem; border-radius: var(--aw-radius-pill); background: var(--aw-border); overflow: hidden; vertical-align: middle; }
.blank i { display: block; height: 100%; background: var(--aw-border-strong); }
.range { max-width: 16rem; overflow: hidden; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.tested .tests { display: inline-flex; align-items: center; gap: .25rem; padding: 0; border: 0; background: none; color: var(--aw-ok); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.tested .none { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.muted { color: var(--aw-muted); }

.values td { background: var(--aw-canvas); }
.value-row { display: flex; align-items: center; gap: .5rem; padding: .15rem 0; }
.value-row .value { flex: 0 0 12rem; overflow: hidden; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.value-row .bar { flex: 1; height: 5px; border-radius: var(--aw-radius-pill); background: var(--aw-border); overflow: hidden; }
.value-row .bar i { display: block; height: 100%; background: var(--aw-teal); }
.value-row .count { color: var(--aw-muted); font-size: var(--aw-text-2xs); }

.join-row {
  display: flex; flex-direction: column; gap: 2px;
  width: 100%; padding: .5rem .625rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); font: inherit; text-align: left; cursor: pointer;
}
.join-row:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.join-row .name { color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 600; }
.add-join { align-self: flex-start; }

/* With the assistant open the detail is too narrow for every column at full
   width, and the last two — what the column looks like and what tests it —
   are the ones that scrolled off. The blank bar is a picture of the figure
   beside it, so it goes first; the range truncates harder, and keeps its full
   text on hover. */
@container master-detail-content (max-width: 48rem) {
  .blank { display: none; }
  .range { max-width: 7rem; }
  .column-cell .type { display: block; margin: 1px 0 0; }
  .notes { min-width: 6.5rem; }
  .note-chip { white-space: normal; }
  .profile th, .profile td { padding-inline: .375rem; }
}

/* Stacked only when the panel is genuinely narrow. At 60rem it stacked
   whenever the assistant was open, which put the profile a list's height
   below the table it was chosen from. */
@container workspace-panel (max-width: 40rem) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .list-body { max-height: 18rem; }
}
</style>
