<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import ProgressBar from 'primevue/progressbar'
import Tag from 'primevue/tag'
import Message from 'primevue/message'

import { api, ApiError } from '../api'
import type {
  ColumnProfile,
  FramePayload,
  TableInfo,
  TableProfile,
  WorkspaceSummary,
} from '../types'
import FrameTable from './FrameTable.vue'
import JoinDialog from './JoinDialog.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()

const toast = useToast()
const confirm = useConfirm()

const fileInput = ref<HTMLInputElement>()
const replaceInput = ref<HTMLInputElement>()
const replaceTarget = ref<string | null>(null)
const replacing = ref(false)
const uploading = ref(false)
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
// dup-row count per table once profiled, so cards can show a health signal.
const dupCache = ref<Record<string, number>>({})

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

async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length) return
  uploading.value = true
  try {
    await api.upload(`/api/workspaces/${props.workspace.id}/tables`, files)
    emit('changed')
  } catch (error) {
    fail('Upload failed', error)
  } finally {
    uploading.value = false
  }
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
          tiles: number
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
    emit('changed')

    const total =
      renamed.updated.joins +
      renamed.updated.tiles +
      renamed.updated.analyses +
      renamed.updated.rulesets
    const codeDetail = renamed.updated.python_snippets
      ? ` Updated ${renamed.updated.python_snippets} saved Python snippet(s).`
      : ''
    toast.add({
      severity: 'success',
      summary: `Renamed "${renamed.old_name}" to "${renamed.name}"`,
      detail: total
        ? `Updated ${total} saved reference(s).${codeDetail}`
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
    emit('changed')
    const { removed_columns, added_columns } = replaced
    if (removed_columns.length) {
      toast.add({
        severity: 'warn',
        summary: `Replaced "${table}" — schema changed`,
        detail:
          `Dropped column(s): ${removed_columns.join(', ')}. ` +
          `Saved queries or analyses using them may now error until updated.`,
        life: 9000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: `Replaced "${table}"`,
        detail: added_columns.length
          ? `New column(s): ${added_columns.join(', ')}.`
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

watch(selected, loadProfile)
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

function rangeText(p: ColumnProfile): string {
  if (p.min === null && p.max === null) return ''
  const range = `${p.min ?? '?'} – ${p.max ?? '?'}`
  return p.mean !== null ? `${range} (mean ${p.mean})` : range
}
</script>

<template>
  <div class="toolbar">
    <input ref="fileInput" type="file" multiple accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="upload" />
    <input ref="replaceInput" type="file" accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="replaceData" />
    <Button label="Add files" icon="pi pi-upload" :loading="uploading" @click="fileInput?.click()" />
    <Button
      label="Add join"
      icon="pi pi-link"
      severity="secondary"
      :disabled="workspace.tables.length < 2"
      v-tooltip.bottom="workspace.tables.length < 2 ? 'Load at least two tables first' : ''"
      @click="showJoin = true"
    />
    <span class="muted">CSV, TSV and Excel files. Types are inferred automatically.</span>
  </div>

  <p v-if="workspace.tables.length === 0" class="muted">
    No tables yet — add the engagement's data files to get started.
  </p>

  <template v-else>
    <div class="cards" role="tablist">
      <button
        v-for="table in workspace.tables"
        :key="table.name"
        class="card"
        :class="{ active: selected === table.name, broken: !!table.error }"
        role="tab"
        :aria-selected="selected === table.name"
        @click="selectTable(table)"
      >
        <div class="card-head">
          <strong class="card-name" :title="table.name">{{ table.name }}</strong>
          <Tag :value="table.kind" :severity="table.kind === 'join' ? 'info' : 'secondary'" />
        </div>

        <div v-if="table.error" class="card-signal error" v-tooltip.bottom="table.error">
          <i class="pi pi-exclamation-triangle" /> failed to load
        </div>
        <template v-else>
          <div class="card-meta">
            {{ table.rows?.toLocaleString() }} rows · {{ table.columns }} cols
          </div>
          <div
            v-if="dupCache[table.name] !== undefined"
            class="card-signal"
            :class="dupCache[table.name] > 0 ? 'warn' : 'ok'"
          >
            <i :class="dupCache[table.name] > 0 ? 'pi pi-exclamation-triangle' : 'pi pi-check'" />
            {{ dupCache[table.name] > 0
              ? `${dupCache[table.name].toLocaleString()} dup rows`
              : 'no dup rows' }}
          </div>
        </template>

        <div class="card-actions">
          <Button
            icon="pi pi-eye"
            text
            size="small"
            :disabled="!!table.error"
            :loading="previewLoading"
            v-tooltip.bottom="'Preview first 100 rows'"
            @click.stop="openPreview(table)"
          />
          <Button
            v-if="table.kind === 'file'"
            icon="pi pi-sync"
            text
            size="small"
            :loading="replacing && replaceTarget === table.name"
            v-tooltip.bottom="'Replace data — keeps saved queries &amp; analyses'"
            @click.stop="startReplace(table)"
          />
          <Button
            icon="pi pi-pencil"
            text
            size="small"
            v-tooltip.bottom="'Rename - updates saved work'"
            @click.stop="startRename(table)"
          />
          <Button
            icon="pi pi-trash"
            text
            size="small"
            severity="danger"
            v-tooltip.bottom="'Remove'"
            @click.stop="removeTable(table)"
          />
        </div>
      </button>
    </div>

    <div class="profile-panel">
      <p v-if="profiling" class="muted">Profiling {{ selected }}…</p>

      <template v-else-if="profile && selectedTable">
        <h3 class="profile-title">{{ selectedTable.name }} <span class="muted">— profile</span></h3>

        <div class="stat-cards" style="margin-bottom: 1rem">
          <div class="stat-card">
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
            <div class="label">In-memory size</div>
            <div class="value">{{ profile.estimated_size_mb.toLocaleString() }} MB</div>
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
              <span class="muted dtype"> {{ data.dtype }}</span>
            </template>
          </Column>
          <Column field="inferred_type" header="Type">
            <template #body="{ data }">
              <Tag :value="data.inferred_type" :severity="typeSeverity[data.inferred_type] ?? 'secondary'" />
            </template>
          </Column>
          <Column field="blank_pct" header="Blank" style="width: 14rem">
            <template #body="{ data }">
              <div class="blank-cell">
                <ProgressBar :value="data.blank_pct" :showValue="false" style="height: 6px; flex: 1" />
                <span>{{ data.blank_pct }}%</span>
              </div>
            </template>
          </Column>
          <Column field="distinct_count" header="Distinct">
            <template #body="{ data }">
              {{ data.distinct_count.toLocaleString() }}
              <span class="muted">({{ data.distinct_pct }}%)</span>
            </template>
          </Column>
          <Column header="Range / Mean">
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
        Saved dashboard tiles, analyses, joins and validation rulesets will be rebound to the new name.
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
.cards {
  display: flex;
  gap: 0.75rem;
  overflow-x: auto;
  padding: 0.25rem 0.25rem 0.75rem;
  margin-bottom: 0.5rem;
}

.card {
  position: relative;
  flex: 0 0 15rem;
  min-width: 15rem;
  text-align: left;
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 12px;
  padding: 0.85rem 0.9rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.card:hover:not(.broken) {
  border-color: var(--p-primary-300);
}

.card.active {
  border-color: var(--p-primary-500);
  box-shadow: 0 0 0 1px var(--p-primary-500);
}

.card.broken {
  cursor: default;
  opacity: 0.75;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.card-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta {
  font-size: 0.82rem;
  color: var(--p-surface-500);
}

.card-signal {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.78rem;
  margin-top: 0.35rem;
}

.card-signal.ok {
  color: var(--p-green-600);
}

.card-signal.warn {
  color: var(--p-orange-500);
}

.card-signal.error {
  color: var(--p-red-500);
  font-weight: 600;
}

.card-actions {
  display: flex;
  gap: 0.15rem;
  margin-top: 0.5rem;
}

.profile-panel {
  border-top: 1px solid var(--p-surface-200);
  padding-top: 1rem;
}

.profile-title {
  margin: 0 0 0.75rem;
  font-size: 1.05rem;
}

.dtype {
  font-size: 0.75rem;
}

.blank-cell {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.warn {
  color: var(--p-orange-500);
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
  font-size: 0.85rem;
}

.tv-count {
  width: 9rem;
  text-align: right;
  font-size: 0.85rem;
  color: var(--p-surface-500);
}

.rename-body {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}
</style>
