<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import ProgressBar from 'primevue/progressbar'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Message from 'primevue/message'

import { api, ApiError } from '../api'
import { fileSize, plural, pluralWord } from '../format'
import type {
  ColumnProfile,
  FramePayload,
  TableInfo,
  TableProfile,
  WorkspaceSummary,
} from '../types'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import FrameTable from './FrameTable.vue'
import JoinDialog from './JoinDialog.vue'
import TableValidation from './validation/TableValidation.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'import-requested': [] }>()

const toast = useToast()
const confirm = useConfirm()

const replaceInput = ref<HTMLInputElement>()
const replaceTarget = ref<string | null>(null)
const replacing = ref(false)
const renameTarget = ref<TableInfo | null>(null)
const renameName = ref('')
const renaming = ref(false)
const showJoin = ref(false)
const preview = ref<{ table: string; frame: FramePayload; total: number } | null>(null)
const previewLoading = ref(false)

// Selected table drives the profile panel below the cards.
const selected = ref<string | null>(null)
const profile = ref<TableProfile | null>(null)
const profiling = ref(false)
const expandedRows = ref({})
const tableView = ref<'profile' | 'validation'>('profile')
const tableViewOptions = [
  { label: 'Profile', value: 'profile' },
  { label: 'Validation', value: 'validation' },
]
// dup-row count per table once profiled, so rows can show a health signal.
const dupCache = ref<Record<string, number>>({})
const filter = ref('')

function matchesFilter(table: TableInfo): boolean {
  const q = filter.value.trim().toLowerCase()
  return !q || table.name.toLowerCase().includes(q)
}

const fileTables = computed(() =>
  props.workspace.tables.filter((t) => t.kind !== 'join' && matchesFilter(t)),
)
const joinTables = computed(() =>
  props.workspace.tables.filter((t) => t.kind === 'join' && matchesFilter(t)),
)
const hasJoins = computed(() => props.workspace.tables.some((t) => t.kind === 'join'))
// Selectable tables in display order, for arrow-key navigation.
const navigable = computed(() =>
  [...fileTables.value, ...joinTables.value].filter((t) => !t.error),
)
// The row holding tabindex=0 (roving tabindex): the selection when visible, else the first row.
const focusName = computed(() => {
  const list = navigable.value
  return list.some((t) => t.name === selected.value) ? selected.value : list[0]?.name ?? null
})

const rowEls: Record<string, HTMLElement> = {}
function setRowEl(name: string, el: unknown) {
  if (el instanceof HTMLElement) rowEls[name] = el
  else delete rowEls[name]
}

function onListKeydown(event: KeyboardEvent) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  const list = navigable.value
  if (!list.length) return
  event.preventDefault()
  const idx = list.findIndex((t) => t.name === selected.value)
  const next =
    idx === -1
      ? 0
      : event.key === 'ArrowDown'
        ? Math.min(idx + 1, list.length - 1)
        : Math.max(idx - 1, 0)
  const table = list[next]
  selected.value = table.name
  rowEls[table.name]?.focus()
}

// Profile every table in the background so health dots are consistent, not
// just filled in for tables the user happened to select. Results are cached
// server-side, so repeat fetches are cheap.
let profileSweepToken = 0
async function sweepProfiles() {
  const token = ++profileSweepToken
  const pending = props.workspace.tables.filter(
    (t) => !t.error && dupCache.value[t.name] === undefined,
  )
  for (const table of pending) {
    if (token !== profileSweepToken) return
    try {
      const result = await api.get<TableProfile>(
        `/api/workspaces/${props.workspace.id}/tables/${table.name}/profile`,
      )
      if (token !== profileSweepToken) return
      dupCache.value = { ...dupCache.value, [table.name]: result.duplicate_rows }
    } catch {
      // Leave the dot in its "not profiled" state; selecting the table surfaces the error.
    }
  }
}
onBeforeUnmount(() => {
  profileSweepToken += 1
})

type HealthState = 'ok' | 'warn' | 'unknown'
function health(table: TableInfo): HealthState {
  const dups = dupCache.value[table.name]
  if (dups === undefined) return 'unknown'
  return dups > 0 ? 'warn' : 'ok'
}
function healthTooltip(table: TableInfo): string {
  const dups = dupCache.value[table.name]
  if (dups === undefined) return 'Not profiled yet'
  return dups > 0 ? `${dups.toLocaleString()} duplicate rows` : 'No duplicate rows'
}

const typeSeverity: Record<string, string> = {
  numeric: 'success',
  date: 'info',
  categorical: 'warn',
  id: 'contrast',
  boolean: 'secondary',
  text: 'secondary',
  empty: 'danger',
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

function startReplace(table: TableInfo) {
  replaceTarget.value = table.name
  replaceInput.value?.click()
}

function startRename(table: TableInfo) {
  renameTarget.value = table
  renameName.value = table.name
}

async function renameTable() {
  const table = renameTarget.value
  if (!table) return
  renaming.value = true
  try {
    const { renamed } = await api.patch<{
      renamed: {
        old_name: string
        name: string
        updated: {
          joins: number
          analyses: number
          rulesets: number
          python_snippets: number
        }
      }
    }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.name}`,
      { name: renameName.value },
    )
    if (selected.value === renamed.old_name) selected.value = renamed.name
    if (preview.value?.table === renamed.old_name) preview.value = null
    if (renamed.old_name in dupCache.value) {
      const { [renamed.old_name]: moved, ...rest } = dupCache.value
      dupCache.value = { ...rest, [renamed.name]: moved }
    }
    emit('changed')

    const total =
      renamed.updated.joins +
      renamed.updated.analyses +
      renamed.updated.rulesets
    const codeDetail = renamed.updated.python_snippets
      ? ` Updated ${plural(renamed.updated.python_snippets, 'saved Python snippet')}.`
      : ''
    toast.add({
      severity: 'success',
      summary: `Renamed "${renamed.old_name}" to "${renamed.name}"`,
      detail: total
        ? `Updated ${plural(total, 'saved reference')}.${codeDetail}`
        : codeDetail || 'No saved references needed changes.',
      life: 6000,
    })
    renameTarget.value = null
  } catch (error) {
    fail('Rename failed', error)
  } finally {
    renaming.value = false
  }
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
    const { [table]: stale, ...rest } = dupCache.value
    dupCache.value = rest
    emit('changed')
    const { removed_columns, added_columns } = replaced
    if (removed_columns.length) {
      toast.add({
        severity: 'warn',
        summary: `Replaced "${table}" — schema changed`,
        detail:
          `Dropped ${pluralWord(removed_columns.length, 'column')}: ${removed_columns.join(', ')}. ` +
          `Saved queries or analyses using them may now error until updated.`,
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
  } catch (error) {
    fail('Replace failed', error)
  } finally {
    replacing.value = false
    replaceTarget.value = null
  }
}

async function loadProfile() {
  if (!selected.value) {
    profile.value = null
    return
  }
  const table = selected.value
  profiling.value = true
  profile.value = null
  try {
    const result = await api.get<TableProfile>(
      `/api/workspaces/${props.workspace.id}/tables/${table}/profile`,
    )
    // Guard against a stale response if the selection changed mid-flight.
    if (selected.value !== table) return
    profile.value = result
    dupCache.value = { ...dupCache.value, [table]: result.duplicate_rows }
  } catch (error) {
    if (selected.value === table) fail('Profiling failed', error)
  } finally {
    if (selected.value === table) profiling.value = false
  }
}

function selectTable(table: TableInfo) {
  if (table.error) return
  selected.value = table.name
}

watch(selected, () => {
  tableView.value = 'profile'
  void loadProfile()
})
watch(
  () => props.workspace.tables,
  (tables) => {
    const names = tables.map((t) => t.name)
    // Drop a selection whose table was removed; default to the first loadable one.
    if (selected.value && !names.includes(selected.value)) selected.value = null
    if (!selected.value) {
      const first = tables.find((t) => !t.error)
      if (first) selected.value = first.name
    }
    void sweepProfiles()
  },
  { immediate: true, deep: true },
)

async function openPreview(table: TableInfo) {
  previewLoading.value = true
  try {
    const frame = await api.get<FramePayload & { total_rows: number }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.name}/preview?rows=100`,
    )
    preview.value = { table: table.name, frame, total: frame.total_rows }
  } catch (error) {
    fail('Preview failed', error)
  } finally {
    previewLoading.value = false
  }
}

function removeTable(table: TableInfo) {
  confirm.require({
    header: `Remove ${table.kind === 'join' ? 'join' : 'table'}`,
    message:
      table.kind === 'join'
        ? `Remove join "${table.name}"?`
        : `Remove "${table.name}" and delete its file from the workspace?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        const endpoint = table.kind === 'join' ? 'joins' : 'tables'
        await api.del(`/api/workspaces/${props.workspace.id}/${endpoint}/${table.name}`)
        emit('changed')
      } catch (error) {
        fail('Remove failed', error)
      }
    },
  })
}

const selectedTable = computed(() =>
  props.workspace.tables.find((t) => t.name === selected.value) ?? null,
)

function tableActions(table: TableInfo) {
  return [
    { label: 'Preview first 100 rows', icon: 'pi pi-eye', disabled: Boolean(table.error), command: () => void openPreview(table) },
    ...(table.kind === 'file' ? [{ label: 'Replace data', icon: 'pi pi-sync', command: () => startReplace(table) }] : []),
    { label: 'Rename', icon: 'pi pi-pencil', command: () => startRename(table) },
    { separator: true },
    { label: 'Remove', icon: 'pi pi-trash', command: () => removeTable(table) },
  ]
}

function rangeText(p: ColumnProfile): string {
  if (p.min === null && p.max === null) return ''
  const range = `${p.min ?? '?'} – ${p.max ?? '?'}`
  return p.mean !== null ? `${range} (mean ${p.mean})` : range
}
</script>

<template>
  <UiPageHeader title="Source tables">
    <input ref="replaceInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="replaceData" />
    <Button label="Add files" icon="pi pi-upload" @click="emit('import-requested')" />
    <Button
      label="Add join"
      icon="pi pi-link"
      severity="secondary"
      :disabled="workspace.tables.length < 2"
      v-tooltip.bottom="workspace.tables.length < 2 ? 'Load at least two tables first' : ''"
      @click="showJoin = true"
    />
  </UiPageHeader>

  <div v-if="workspace.tables.length === 0" class="empty-state import-empty">
    <div>
      <span class="empty-state-icon"><i class="pi pi-upload" /></span>
      <h3>Add engagement data</h3>
      <Button label="Choose files" icon="pi pi-upload" @click="emit('import-requested')" />
    </div>
  </div>

  <template v-else>
    <div class="data-workbench">
    <aside class="inventory surface-panel">
      <div class="inventory-head"><span>Tables</span><Tag :value="String(workspace.tables.length)" severity="secondary" /></div>
      <div v-if="workspace.tables.length > 5" class="inventory-filter">
        <i class="pi pi-search" />
        <input v-model="filter" type="search" placeholder="Filter tables" aria-label="Filter tables" />
      </div>
      <div class="table-list" role="listbox" aria-label="Tables" @keydown="onListKeydown">
        <template v-for="group in [
          { label: 'Files', tables: fileTables },
          { label: 'Joins', tables: joinTables },
        ]" :key="group.label">
          <div v-if="hasJoins && group.tables.length" class="group-label">
            {{ group.label }} · {{ group.tables.length }}
          </div>
          <div
            v-for="table in group.tables"
            :key="table.name"
            :ref="(el) => setRowEl(table.name, el)"
            class="row"
            :class="{ active: selected === table.name, broken: !!table.error }"
            role="option"
            :tabindex="table.error ? -1 : table.name === focusName ? 0 : -1"
            :aria-selected="selected === table.name"
            :aria-disabled="!!table.error"
            @click="selectTable(table)"
            @dblclick="!table.error && openPreview(table)"
            @keydown.enter="selectTable(table)"
            @keydown.space.prevent="selectTable(table)"
          >
            <i class="row-icon pi" :class="table.kind === 'join' ? 'pi-link' : 'pi-file'" />
            <div class="row-main">
              <span class="row-name" :title="table.name">{{ table.name }}</span>
              <span v-if="table.error" class="row-sub error" v-tooltip.bottom="table.error">
                {{ table.error }}
              </span>
              <span v-else-if="table.join" class="row-sub" :title="`${table.join.left} ⋈ ${table.join.right}`">
                {{ table.join.left }} ⋈ {{ table.join.right }}
              </span>
            </div>
            <button
              v-if="table.error && table.kind === 'file'"
              class="row-fix"
              type="button"
              @click.stop="startReplace(table)"
            >
              Replace
            </button>
            <span v-if="!table.error" class="row-meta">
              {{ table.rows?.toLocaleString() }}×{{ table.columns }}
            </span>
            <span
              v-if="!table.error"
              class="dot"
              :class="health(table)"
              v-tooltip.bottom="healthTooltip(table)"
            />
            <div class="row-actions" @click.stop @dblclick.stop>
              <UiOverflowMenu :items="tableActions(table)" />
            </div>
          </div>
        </template>
        <p v-if="!fileTables.length && !joinTables.length" class="muted no-match">
          No tables match “{{ filter }}”
        </p>
      </div>
    </aside>

    <section class="profile-panel surface-panel">
      <template v-if="selectedTable">
        <div class="profile-head">
          <div><p class="eyebrow">Selected table</p><h3 class="profile-title">{{ selectedTable.name }}</h3></div>
          <div class="profile-head-actions">
            <SelectButton
              v-model="tableView"
              :options="tableViewOptions"
              optionLabel="label"
              optionValue="value"
              :allowEmpty="false"
              size="small"
            />
            <Button
              v-if="tableView === 'profile'"
              label="Preview"
              icon="pi pi-eye"
              size="small"
              severity="secondary"
              outlined
              :loading="previewLoading"
              @click="openPreview(selectedTable)"
            />
            <Tag
              v-if="tableView === 'profile' && profile"
              :value="profile.duplicate_rows ? 'Review data health' : 'Profile complete'"
              :severity="profile.duplicate_rows ? 'warn' : 'success'"
            />
          </div>
        </div>

        <TableValidation
          v-if="tableView === 'validation'"
          :key="selectedTable.name"
          :workspace="workspace"
          :table="selectedTable.name"
        />

        <template v-else>
          <p v-if="profiling" class="muted loading-profile"><i class="pi pi-spinner pi-spin" /> Profiling {{ selected }}…</p>

          <template v-else-if="profile">
            <div class="stat-cards" style="margin-bottom: 1rem">
              <div class="stat-card" :data-tone="profile.duplicate_rows > 0 ? 'warn' : 'ok'">
                <div class="label">Rows</div>
                <div class="value">{{ profile.rows.toLocaleString() }}</div>
              </div>
              <div class="stat-card">
                <div class="label">Columns</div>
                <div class="value">{{ profile.columns }}</div>
              </div>
              <div class="stat-card">
                <div class="label">Duplicate rows</div>
                <div class="value" :class="{ warn: profile.duplicate_rows > 0 }">
                  {{ profile.duplicate_rows.toLocaleString() }}
                </div>
              </div>
              <div class="stat-card">
                <div class="label">In memory</div>
                <div class="value">{{ fileSize(profile.estimated_size_bytes) }}</div>
              </div>
            </div>

            <Message v-if="profile.sampled" severity="info" :closable="false" style="margin-bottom: 1rem">
              Column statistics are computed on the first {{ profile.sample_rows.toLocaleString() }} rows.
            </Message>

            <DataTable
              :value="profile.column_profiles"
              v-model:expandedRows="expandedRows"
              dataKey="name"
              size="small"
              stripedRows
            >
              <Column expander style="width: 3rem" />
              <Column field="name" header="Column">
                <template #body="{ data }">
                  <strong>{{ data.name }}</strong>
                  <!-- The separator is a margin, not a leading space in the
                       markup: the template compiler drops that space and the
                       cell renders as `JOB_TITLEString`. -->
                  <span class="muted dtype">{{ data.dtype }}</span>
                </template>
              </Column>
              <Column field="inferred_type" header="Type">
                <template #body="{ data }">
                  <Tag :value="data.inferred_type" :severity="typeSeverity[data.inferred_type] ?? 'secondary'" />
                </template>
              </Column>
              <Column field="blank_pct" header="Blank" bodyClass="aw-figure" style="width: 14rem">
                <template #body="{ data }">
                  <div class="blank-cell">
                    <ProgressBar :value="data.blank_pct" :showValue="false" style="height: 6px; flex: 1" />
                    <span>{{ data.blank_pct }}%</span>
                  </div>
                </template>
              </Column>
              <Column field="distinct_count" header="Distinct" bodyClass="aw-figure">
                <template #body="{ data }">
                  {{ data.distinct_count.toLocaleString() }}
                  <span class="muted">({{ data.distinct_pct }}%)</span>
                </template>
              </Column>
              <Column header="Range / Mean" bodyClass="aw-figure">
                <template #body="{ data }">{{ rangeText(data) }}</template>
              </Column>

              <template #expansion="{ data }">
                <div class="top-values">
                  <p v-if="data.top_values.length === 0" class="muted">No values.</p>
                  <div v-for="tv in data.top_values" :key="tv.value ?? ''" class="tv-row">
                    <span class="tv-value">{{ tv.value ?? '∅' }}</span>
                    <ProgressBar :value="tv.pct" :showValue="false" style="height: 6px; flex: 1" />
                    <span class="tv-count">{{ tv.count.toLocaleString() }} ({{ tv.pct }}%)</span>
                  </div>
                </div>
              </template>
            </DataTable>
          </template>

          <p v-else class="muted">Select a table above to profile it.</p>
        </template>
      </template>

      <p v-else class="muted">Select a table above to profile it.</p>
    </section>
    </div>
  </template>

  <Dialog
    :visible="preview !== null"
    modal
    :header="`${preview?.table} — first ${preview?.frame.rows.length} of ${preview?.total.toLocaleString()} rows`"
    :style="{ width: '90vw' }"
    @update:visible="preview = null"
  >
    <FrameTable v-if="preview" :frame="preview.frame" scrollHeight="60vh" />
  </Dialog>

  <Dialog
    :visible="renameTarget !== null"
    modal
    header="Rename table"
    :style="{ width: '26rem' }"
    @update:visible="renameTarget = null"
  >
    <div class="rename-body">
      <label for="rename-table-name">Name</label>
      <InputText
        id="rename-table-name"
        v-model="renameName"
        autofocus
        @keyup.enter="renameTable"
      />
      <p class="muted">
        Saved analyses, joins and validation rulesets will be rebound to the new name.
      </p>
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="renameTarget = null" />
      <Button label="Rename" icon="pi pi-check" :loading="renaming" @click="renameTable" />
    </template>
  </Dialog>

  <JoinDialog
    v-model:visible="showJoin"
    :workspace="workspace"
    @saved="emit('changed')"
  />
</template>

<style scoped>
/* Not stacked in a gapped column, so the header spaces itself here. */
.ui-page-header { margin-bottom: var(--aw-section-gap); }
.import-empty { min-height: 20rem; }
.data-workbench { display: grid; grid-template-columns: 18rem minmax(0, 1fr); gap: 1rem; align-items: start; }

.inventory {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 8.5rem);
  overflow: hidden;
  box-shadow: none;
}

.inventory-head { display: flex; flex: none; align-items: center; justify-content: space-between; padding: 0.75rem 0.9rem; border-bottom: 1px solid var(--aw-border); color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 750; }

.inventory-filter {
  position: relative;
  flex: none;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--aw-border);
}

.inventory-filter .pi-search {
  position: absolute;
  top: 50%;
  left: 1.2rem;
  transform: translateY(-50%);
  font-size: var(--aw-text-sm);
  color: var(--aw-muted);
  pointer-events: none;
}

.inventory-filter input {
  width: 100%;
  padding: 0.35rem 0.5rem 0.35rem 1.7rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  font: inherit;
  font-size: var(--aw-text-sm);
  color: inherit;
}

.inventory-filter input:focus {
  outline: none;
  border-color: var(--aw-teal-600);
}

.table-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 0.35rem 0;
}

.group-label {
  padding: 0.45rem 0.9rem 0.2rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-weight: 700;
}

.group-label + .group-label,
.row + .group-label {
  margin-top: 0.35rem;
  border-top: 1px solid var(--aw-border);
}

.row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-height: 2.25rem;
  padding: 0.3rem 0.6rem 0.3rem 0.9rem;
  border-left: 2px solid transparent;
  cursor: pointer;
}

.row:hover:not(.broken) {
  background: var(--aw-raised);
}

.row:focus-visible {
  outline: 2px solid var(--aw-teal-600);
  outline-offset: -2px;
}

.row.active {
  border-left-color: var(--aw-teal);
  background: var(--aw-teal-soft);
}

.row.broken {
  cursor: default;
  opacity: 0.8;
}

.row-icon {
  flex: none;
  font-size: var(--aw-text-sm);
  color: var(--aw-muted);
}

.row.active .row-icon {
  color: var(--aw-teal);
}

.row-main {
  flex: 1;
  min-width: 0;
}

.row-name {
  display: block;
  overflow: hidden;
  font-weight: 600;
  font-size: var(--aw-text-base);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-sub {
  display: block;
  overflow: hidden;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-sub.error {
  color: var(--aw-danger);
}

.row-meta {
  flex: none;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-variant-numeric: tabular-nums;
}

.row-fix {
  flex: none;
  padding: 0.15rem 0.5rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: var(--aw-teal);
  font: inherit;
  font-size: var(--aw-text-xs);
  font-weight: 600;
  cursor: pointer;
}

.row-fix:hover {
  border-color: var(--aw-teal-600);
}

.dot {
  flex: none;
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
}

.dot.ok { background: var(--aw-ok); }
.dot.warn { background: var(--aw-warn); }
.dot.unknown { border: 1px solid var(--aw-border-strong); }

.row-actions {
  display: flex;
  flex: none;
  opacity: 0;
  transition: opacity 0.15s;
}

.row:hover .row-actions,
.row:focus-within .row-actions,
.row.active .row-actions {
  opacity: 1;
}

.no-match {
  padding: 0.75rem 0.9rem;
  font-size: var(--aw-text-sm);
}

.profile-head-actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 0.5rem;
}

.profile-panel {
  min-width: 0;
  padding: 1rem;
}

.profile-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 0.75rem; }
.profile-head .eyebrow { margin-bottom: 0.15rem; }
.loading-profile { min-height: 12rem; display: grid; place-items: center; align-content: center; gap: .5rem; }

.profile-title {
  margin: 0;
  font-size: var(--aw-text-lg);
}

.dtype {
  margin-left: 0.4rem;
  font-size: var(--aw-text-sm);
}

.blank-cell {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.warn {
  color: var(--aw-warn);
}

.top-values {
  padding: 0.5rem 1rem 0.5rem 3rem;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  max-width: 44rem;
}

.tv-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.tv-value {
  width: 14rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: Consolas, monospace;
  font-size: var(--aw-text-base);
}

.tv-count {
  width: 9rem;
  text-align: right;
  font-size: var(--aw-text-base);
  color: var(--aw-muted);
}

.rename-body {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

@media (max-width: 1050px) {
  .data-workbench { grid-template-columns: 1fr; }
  .inventory { position: static; max-height: 18rem; }
  .row-actions { opacity: 1; }
}

@media (max-width: 700px) {
  .section-heading { flex-direction: column; }
}
</style>
