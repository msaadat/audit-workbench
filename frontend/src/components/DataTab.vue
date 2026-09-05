<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import { api, ApiError } from '../api'
import { fileSize, plural, pluralWord } from '../format'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  ColumnProfile, FramePayload, RuleSet, TableInfo, TableProfile, WorkspaceSummary,
} from '../types'
import FrameTable from './FrameTable.vue'
import TableValidation from './validation/TableValidation.vue'
import JoinDrawer from './tables/JoinDrawer.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import UiVerdictBar from './ui/UiVerdictBar.vue'
import {
  EMPTY_FACTS, TABLE_CHIPS, duplicateRows, filterTables, isAgentBuilt, ruleSetsFor,
  tableMeta, tableTone, tablesStatus, untestedColumns,
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
  return scoped.value.filter(table => table.name.toLowerCase().includes(needle))
})
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
    icon: 'pi pi-exclamation-triangle',
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
  { label: 'Rename', icon: 'pi pi-pencil', disabled: !selectedTable.value, command: startRename },
  { label: 'Profile again', icon: 'pi pi-refresh', disabled: !selectedTable.value, command: () => void reprofile() },
  { label: 'Export table', icon: 'pi pi-download', disabled: !selectedTable.value, command: () => void download() },
  { separator: true },
  { label: 'Remove', icon: 'pi pi-trash', disabled: !selectedTable.value, command: removeTable },
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

const verdictTone = computed<'ok' | 'warn' | 'bad' | 'neutral'>(() => {
  const table = selectedTable.value
  if (!table) return 'neutral'
  if (table.error) return 'bad'
  if (!profile.value) return 'neutral'
  return duplicateRows(facts.value, table.name) > 0 || untested.value.length ? 'warn' : 'ok'
})

const eyebrow = computed(() => {
  const table = selectedTable.value
  if (!table) return ''
  if (table.join) {
    const built = isAgentBuilt(table) ? ' · built by the assistant' : ''
    return `Join · ${table.join.left} ⋈ ${table.join.right}${built}`
  }
  return `File · ${table.source}`
})

function openTest(id: string) { void nav.push('data-tests', { test: id }) }
</script>

<template>
  <div class="tables">
    <header class="page-head">
      <h1>Source tables</h1>
      <span class="grow" />
      <input ref="replaceInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="replaceData" />
      <Button
        label="Add join"
        icon="pi pi-link"
        size="small"
        outlined
        severity="secondary"
        :disabled="tables.length < 2"
        @click="joinOpen = true"
      />
      <Button label="Add files" icon="pi pi-upload" size="small" @click="emit('import-requested')" />
      <UiOverflowMenu :items="menuItems" tooltip="More table actions" />
    </header>

    <UiReviewBar
      v-if="tables.length"
      :lanes="status.lanes"
      :chips="TABLE_CHIPS"
      :filters="status.filters"
      allLabel="All tables"
      :total="tables.length"
      :filter="statusFilter"
      @filter="statusFilter = ($event as TablesFilter[])"
    />

    <div v-if="tables.length" class="layout">
      <section class="list-panel">
        <div class="list-head">
          <IconField>
            <InputIcon class="pi pi-search" />
            <InputText v-model="search" size="small" placeholder="Filter tables" />
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
              @click="selected = table.name"
            >
              <span class="dot" :data-tone="tableTone(table, facts)" aria-hidden="true" />
              <span class="copy">
                <span class="name">{{ table.name }}</span>
                <span class="meta aw-figure" :data-tone="table.error ? 'bad' : undefined">{{ tableMeta(table, facts) }}</span>
              </span>
            </button>
          </section>
          <p v-if="!visible.length" class="empty">No table matches this view.</p>
        </div>
      </section>

      <section v-if="selectedTable" class="detail">
        <header class="detail-head">
          <div class="detail-copy">
            <p class="eyebrow">{{ eyebrow }}</p>
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
            <h2 v-else @dblclick="startRename">{{ selectedTable.name }}</h2>
            <p v-if="relatedJoins.length" class="sub">
              {{ plural(relatedJoins.length, 'join') }} {{ relatedJoins.length === 1 ? 'uses' : 'use' }} this table
            </p>
          </div>
          <Button
            v-if="selectedTable.kind !== 'join'"
            label="Replace data"
            icon="pi pi-sync"
            size="small"
            outlined
            severity="secondary"
            :loading="replacing"
            @click="startReplace(selectedTable)"
          />
        </header>

        <!-- What the population is, and how much of it the audit evaluated. -->
        <UiVerdictBar :tone="verdictTone">
          <template #found>
            <template v-if="selectedTable.error">
              <span class="failed">{{ selectedTable.error }}</span>
            </template>
            <template v-else-if="profile">
              <span class="aw-figure">
                {{ profile.rows.toLocaleString() }} {{ pluralWord(profile.rows, 'row') }} ·
                {{ profile.columns }} {{ pluralWord(profile.columns, 'column') }} ·
                <template v-if="profile.duplicate_rows">
                  <span class="warned">{{ plural(profile.duplicate_rows, 'duplicate row') }}</span>
                </template>
                <template v-else>no duplicate rows</template>
                · {{ fileSize(profile.estimated_size_bytes) }} in memory
              </span>
            </template>
            <span v-else>{{ profiling ? 'Profiling…' : 'Not profiled yet.' }}</span>
          </template>

          <template #recorded>
            <template v-if="selectedTable.kind === 'join'">
              Built from {{ selectedTable.join?.left }} and {{ selectedTable.join?.right }}; coverage is
              measured on the files it draws from.
            </template>
            <template v-else-if="!coverage">Test coverage has not been read yet.</template>
            <template v-else-if="untested.length">
              {{ coverage.length - untested.length }} of {{ coverage.length }} columns are evaluated by a data test.
              <b class="warned">{{ untested.join(', ') }}</b>
              {{ untested.length === 1 ? 'is' : 'are' }} evaluated by none.
            </template>
            <template v-else>Every column is evaluated by at least one data test.</template>
          </template>

          <template #actions>
            <template v-if="selectedTable.error">
              <Button label="Replace data" icon="pi pi-sync" size="small" :loading="replacing" @click="startReplace(selectedTable)" />
            </template>
            <template v-else-if="rules.length">
              <span class="rules aw-figure">{{ plural(rules.length, 'rule set') }}</span>
              <Button label="Open" size="small" outlined severity="secondary" @click="tab = 'validation'" />
            </template>
            <template v-else-if="selectedTable.kind !== 'join'">
              <span class="rules muted">No validation rules</span>
              <Button label="New rule set" size="small" outlined severity="secondary" @click="tab = 'validation'" />
            </template>
          </template>
        </UiVerdictBar>

        <nav class="tabs" role="tablist">
          <button
            v-for="entry in [
              { key: 'profile', label: 'Profile', badge: '' },
              { key: 'preview', label: 'Preview', badge: '100 rows' },
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
          <p v-if="profiling" class="note"><i class="pi pi-spinner pi-spin" /> Profiling {{ selected }}…</p>
          <template v-else-if="profile">
            <p class="note aw-figure">
              Statistics are computed on
              {{ profile.sampled ? `the first ${profile.sample_rows.toLocaleString()} rows` : `all ${profile.rows.toLocaleString()} rows` }}.
            </p>
            <table class="profile">
              <thead>
                <tr>
                  <th class="expander" />
                  <th>Column</th><th>Type</th><th>Blank</th><th>Distinct</th>
                  <th>Range / mean</th><th>Tested</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="column in profile.column_profiles" :key="column.name">
                  <tr :class="{ untested: coverage && !testsFor(column.name).length }">
                    <td class="expander">
                      <button type="button" :aria-expanded="expanded.has(column.name)" @click="toggleExpanded(column.name)">
                        <i class="pi" :class="expanded.has(column.name) ? 'pi-chevron-down' : 'pi-chevron-right'" />
                      </button>
                    </td>
                    <td>
                      <b class="column-name">{{ column.name }}</b>
                      <span class="dtype">{{ column.dtype }}</span>
                    </td>
                    <td><span class="type-pill" :data-kind="column.inferred_type">{{ column.inferred_type }}</span></td>
                    <td class="aw-figure">
                      <span class="blank">
                        <i :style="{ width: `${Math.min(100, column.blank_pct)}%` }" />
                      </span>{{ column.blank_pct }}%
                    </td>
                    <td class="aw-figure">{{ column.distinct_count.toLocaleString() }} <span class="muted">({{ column.distinct_pct }}%)</span></td>
                    <td class="aw-figure range">{{ rangeText(column) }}</td>
                    <td class="tested aw-figure">
                      <template v-if="!coverage"><span class="muted">—</span></template>
                      <template v-else-if="testsFor(column.name).length">
                        <button type="button" class="tests" @click="openTest(testsFor(column.name)[0])">
                          <i class="pi pi-check" aria-hidden="true" />{{ plural(testsFor(column.name).length, 'test') }}
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
          </template>
          <p v-else class="note">This table has not been profiled.</p>
        </template>

        <template v-else-if="tab === 'preview'">
          <p v-if="previewLoading" class="note"><i class="pi pi-spinner pi-spin" /> Loading rows…</p>
          <FrameTable v-else-if="preview" :frame="preview.frame" scrollHeight="28rem" />
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
            <span class="name">{{ join.name }}</span>
            <span class="meta aw-figure">{{ tableMeta(join, facts) }}</span>
          </button>
          <Button label="Add join" icon="pi pi-link" size="small" outlined severity="secondary" class="add-join" @click="joinOpen = true" />
        </template>
      </section>
      <UiEmptyState v-else icon="pi pi-table" title="No table selected" description="Select a table to profile it." />
    </div>

    <UiEmptyState
      v-else
      icon="pi pi-upload"
      title="Add engagement data"
      description="Import the populations the audit will test — invoices, purchase orders, payments — as CSV or Excel."
    >
      <Button label="Choose files" icon="pi pi-upload" @click="emit('import-requested')" />
    </UiEmptyState>

    <JoinDrawer v-model:visible="joinOpen" :workspace="workspace" @saved="emit('changed')" />
  </div>
</template>

<style scoped>
.tables { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

.layout { display: grid; grid-template-columns: 18.75rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext) { width: 100%; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

.group { display: flex; flex-direction: column; gap: 1px; margin: 0; padding: .5rem .75rem .35rem; background: var(--aw-canvas); border-top: 1px solid var(--aw-border); }
.list-body > section:first-child .group { border-top: 0; }
.group-name { color: var(--aw-ink-strong); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.group-count { color: var(--aw-muted); font-size: var(--aw-text-2xs); }

.row {
  display: flex; align-items: center; gap: .625rem;
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
.copy .name { overflow: hidden; color: var(--aw-ink); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.row.active .name { color: var(--aw-ink-strong); }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }
.meta[data-tone='bad'] { color: var(--aw-danger); }
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
.detail-copy { display: flex; flex-direction: column; gap: .2rem; flex: 1; min-width: 0; }
.eyebrow { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.detail-head h2, .rename { margin: 0; color: var(--aw-ink-strong); font-family: var(--aw-font-mono); font-size: var(--aw-text-lg); font-weight: 600; }
.rename { width: 100%; padding: .1rem .25rem; }
.sub { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }

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

.profile { width: 100%; border-collapse: collapse; font-size: var(--aw-text-sm); }
.profile th {
  padding: .35rem .5rem; border-bottom: 1px solid var(--aw-border);
  color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs);
  font-weight: 600; letter-spacing: .06em; text-align: left; text-transform: uppercase;
}
.profile td { padding: .4rem .5rem; border-bottom: 1px solid var(--aw-border); color: var(--aw-ink); vertical-align: middle; }
.profile tr.untested .column-name { color: var(--aw-warn-ink); }
.profile .expander { width: 2rem; }
.profile .expander button { padding: 0; border: 0; background: none; color: var(--aw-muted); cursor: pointer; font-size: .65rem; }
.column-name { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
.dtype { margin-left: .4rem; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.type-pill { padding: 0 .375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 600; }
.type-pill[data-kind='numeric'] { background: var(--aw-ok-soft); color: var(--aw-ok); }
.type-pill[data-kind='date'] { background: var(--aw-teal-soft); color: var(--aw-teal); }
.type-pill[data-kind='categorical'] { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.type-pill[data-kind='empty'] { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.blank { display: inline-block; width: 4rem; height: 5px; margin-right: .4rem; border-radius: var(--aw-radius-pill); background: var(--aw-border); overflow: hidden; vertical-align: middle; }
.blank i { display: block; height: 100%; background: var(--aw-border-strong); }
.range { color: var(--aw-ink-soft); font-size: var(--aw-text-xs); }
.tested .tests { display: inline-flex; align-items: center; gap: .25rem; padding: 0; border: 0; background: none; color: var(--aw-ok); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.tested .none { color: var(--aw-warn-ink); font-size: var(--aw-text-xs); font-weight: 600; }
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
.join-row .name { color: var(--aw-ink-strong); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; }
.add-join { align-self: flex-start; }

@container workspace-panel (max-width: 60rem) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .list-body { max-height: 18rem; }
}
</style>
