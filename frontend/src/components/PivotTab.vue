<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import type { ColumnSchema, FilterSpec, PivotResult, PivotSpec, PivotValueSpec, WorkspaceSummary } from '../types'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const FUNC_LABELS: Record<string, string> = {
  sum: 'Sum',
  mean: 'Average',
  min: 'Min',
  max: 'Max',
  n_unique: 'Distinct',
  count: 'Count',
}
const NUMERIC_FUNCS = ['sum', 'mean', 'min', 'max', 'n_unique', 'count']
const TEXT_FUNCS = ['n_unique', 'min', 'max', 'count']

const table = ref<string | null>(null)
const schema = ref<ColumnSchema[]>([])
const filterOps = ref<{ value: string; label: string }[]>([])

const filters = ref<FilterSpec[]>([])
const rowFields = ref<string[]>([])
const columnField = ref<string | null>(null)
const values = ref<PivotValueSpec[]>([])

const result = ref<PivotResult | null>(null)
const running = ref(false)
const exporting = ref(false)
const lastError = ref<string | null>(null)

type ZoneName = 'filters' | 'rows' | 'columns' | 'values'
const drag = ref<{ field: string; from: ZoneName | 'list'; index: number } | null>(null)
const dragOver = ref<ZoneName | null>(null)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const columnNames = computed(() => schema.value.map((c) => c.name))

function kindOf(field: string): string {
  return schema.value.find((c) => c.name === field)?.kind ?? 'text'
}

function fieldIcon(field: string): string {
  const kind = kindOf(field)
  if (kind === 'numeric') return 'pi pi-hashtag'
  if (kind === 'date') return 'pi pi-calendar'
  if (kind === 'boolean') return 'pi pi-check-square'
  return 'pi pi-align-left'
}

function funcOptions(value: PivotValueSpec) {
  const funcs = value.column && kindOf(value.column) === 'numeric' ? NUMERIC_FUNCS : TEXT_FUNCS
  return funcs.map((f) => ({ value: f, label: FUNC_LABELS[f] }))
}

function valueLabel(value: PivotValueSpec): string {
  if (value.func === 'count') return 'Count of rows'
  return `${FUNC_LABELS[value.func] ?? value.func} of ${value.column}`
}

const inUse = computed(() => new Set([...rowFields.value, ...(columnField.value ? [columnField.value] : [])]))

async function loadMeta() {
  const meta = await api.get<{ filter_ops: { value: string; label: string }[] }>('/api/explore/meta')
  filterOps.value = meta.filter_ops
}
loadMeta()

async function loadSchema() {
  if (!table.value) return
  schema.value = (
    await api.get<{ columns: ColumnSchema[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/schema`,
    )
  ).columns
  filters.value = []
  rowFields.value = []
  columnField.value = null
  values.value = []
  result.value = null
  lastError.value = null
}

watch(table, loadSchema)
watch(
  () => props.workspace.tables.length,
  () => {
    if (!table.value && tableOptions.value.length) table.value = tableOptions.value[0]
  },
  { immediate: true },
)

// ------------------------------------------------------------- drag and drop
function startDrag(field: string, from: ZoneName | 'list', index = -1) {
  drag.value = { field, from, index }
}

function removeFromSource() {
  if (!drag.value) return
  const { from, index } = drag.value
  if (from === 'rows') rowFields.value.splice(index, 1)
  else if (from === 'columns') columnField.value = null
  else if (from === 'values') values.value.splice(index, 1)
  else if (from === 'filters') filters.value.splice(index, 1)
}

function onDrop(zone: ZoneName) {
  dragOver.value = null
  if (!drag.value) return
  const { field, from } = drag.value
  if (from === zone) {
    drag.value = null
    return
  }
  removeFromSource()
  addToZone(zone, field)
  drag.value = null
}

function addToZone(zone: ZoneName, field: string) {
  if (zone === 'rows') {
    if (rowFields.value.includes(field)) return
    if (columnField.value === field) columnField.value = null
    rowFields.value.push(field)
  } else if (zone === 'columns') {
    const rowIndex = rowFields.value.indexOf(field)
    if (rowIndex !== -1) rowFields.value.splice(rowIndex, 1)
    columnField.value = field
  } else if (zone === 'values') {
    values.value.push({ column: field, func: kindOf(field) === 'numeric' ? 'sum' : 'n_unique' })
  } else if (zone === 'filters') {
    filters.value.push({ column: field, op: 'eq', value: '' })
  }
}

/** Click a field: Excel-style default placement — numeric to Values, else to Rows. */
function quickAdd(field: string) {
  if (kindOf(field) === 'numeric') addToZone('values', field)
  else addToZone('rows', field)
}

function needsValue(op: string): boolean {
  return !['blank', 'not_blank'].includes(op)
}

// ------------------------------------------------------------------ querying
function spec(): PivotSpec {
  return {
    // A filter with no value yet is still being composed — leave it out.
    filters: filters.value.filter(
      (f) => f.column && f.op && (!needsValue(f.op) || f.value !== ''),
    ),
    rows: rowFields.value,
    columns: columnField.value ? [columnField.value] : [],
    values: values.value
      .filter((v) => v.func === 'count' || v.column)
      .map((v) => ({ column: v.column ?? '', func: v.func })),
    totals: true,
  }
}

let timer: ReturnType<typeof setTimeout> | undefined

watch(
  [rowFields, columnField, values, filters],
  () => {
    clearTimeout(timer)
    if (rowFields.value.length === 0) {
      result.value = null
      lastError.value = null
      return
    }
    timer = setTimeout(run, 400)
  },
  { deep: true },
)

async function run() {
  if (!table.value || rowFields.value.length === 0) return
  running.value = true
  try {
    result.value = await api.post<PivotResult>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/pivot`,
      spec(),
    )
    lastError.value = null
  } catch (error) {
    lastError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    running.value = false
  }
}

async function exportExcel() {
  if (!table.value || !result.value) return
  exporting.value = true
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/pivot/export`,
      spec(),
      `${table.value}_pivot.xlsx`,
    )
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Export failed', detail, life: 6000 })
  } finally {
    exporting.value = false
  }
}

// ----------------------------------------------------------------- rendering
function displayName(valueName: string): string {
  if (valueName === 'row_count') return 'Count of rows'
  for (const func of NUMERIC_FUNCS) {
    if (valueName.endsWith(`_${func}`)) {
      const column = valueName.slice(0, -func.length - 1)
      return `${FUNC_LABELS[func]} of ${column}`
    }
  }
  return valueName
}

/** Header groups over the data columns, in backend column order:
 *  one group per value (spanning the column keys), then one Total group. */
const headerGroups = computed(() => {
  const r = result.value
  if (!r) return []
  const hasTotal = r.columns.some((c) => c.endsWith('::Total'))
  if (!r.column_field) {
    return [{ label: '', columns: r.value_names.map((name) => ({ key: name, label: displayName(name) })) }]
  }
  const groups = r.value_names.map((name) => ({
    label: displayName(name),
    columns: r.column_keys.map((key) => ({ key: `${name}::${key}`, label: key })),
  }))
  if (hasTotal) {
    groups.push({
      label: 'Total',
      columns: r.value_names.map((name) => ({
        key: `${name}::Total`,
        label: r.value_names.length > 1 ? displayName(name) : 'Total',
      })),
    })
  }
  return groups
})

const twoRowHeader = computed(() => {
  const r = result.value
  return !!r && !!r.column_field && (r.value_names.length > 1 || false)
})

/** Data cell values by column key, per row, plus alignment. */
const bodyRows = computed(() => {
  const r = result.value
  if (!r) return []
  return r.rows.map((row) => {
    const byKey: Record<string, string | number | boolean | null> = {}
    r.columns.forEach((column, index) => {
      byKey[column] = row[index]
    })
    return byKey
  })
})

const grandByKey = computed(() => {
  const r = result.value
  if (!r || !r.grand_total) return null
  const byKey: Record<string, string | number | boolean | null> = {}
  r.columns.forEach((column, index) => {
    byKey[column] = r.grand_total![index]
  })
  return byKey
})

function fmt(value: string | number | boolean | null): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  return String(value)
}
</script>

<template>
  <div class="toolbar">
    <div class="field">
      <label>Table</label>
      <Select v-model="table" :options="tableOptions" placeholder="Pick a table" style="min-width: 14rem" />
    </div>
    <span class="grow" />
    <Tag v-if="result" :value="`${result.filtered_rows.toLocaleString()} rows`" severity="secondary" />
    <Button
      label="Export"
      icon="pi pi-file-excel"
      severity="secondary"
      :loading="exporting"
      :disabled="!result"
      v-tooltip.bottom="'Cross-tab to Excel, totals included'"
      @click="exportExcel"
    />
  </div>

  <div class="pivot-layout">
    <div class="pivot-result" :class="{ dim: running }">
      <p v-if="rowFields.length === 0" class="muted hint">
        <i class="pi pi-arrow-right" /> Drag a field into <strong>Rows</strong> to start — or click a
        field: text goes to Rows, numbers to Values.
      </p>
      <p v-else-if="lastError" class="error">{{ lastError }}</p>
      <p v-else-if="result && result.rows.length === 0" class="muted hint">
        No rows match the filters.
      </p>
      <div v-else-if="result" class="grid-wrap">
        <table class="pivot-grid">
          <thead>
            <tr v-if="twoRowHeader">
              <th
                v-for="field in result.row_fields"
                :key="field"
                class="rowhead"
                rowspan="2"
              >{{ field }}</th>
              <th
                v-for="group in headerGroups"
                :key="group.label"
                :colspan="group.columns.length"
                class="grouphead"
              >{{ group.label }}</th>
            </tr>
            <tr>
              <template v-if="!twoRowHeader">
                <th v-for="field in result.row_fields" :key="field" class="rowhead">{{ field }}</th>
              </template>
              <template v-for="group in headerGroups" :key="group.label">
                <th
                  v-for="column in group.columns"
                  :key="column.key"
                  class="num"
                  :class="{ total: column.key.endsWith('::Total') }"
                >{{ column.label }}</th>
              </template>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in bodyRows" :key="index">
              <td v-for="field in result.row_fields" :key="field" class="rowhead">
                <span :class="{ 'cell-null': row[field] === null }">{{ fmt(row[field]) }}</span>
              </td>
              <template v-for="group in headerGroups" :key="group.label">
                <td
                  v-for="column in group.columns"
                  :key="column.key"
                  class="num"
                  :class="{ total: column.key.endsWith('::Total') }"
                >{{ fmt(row[column.key]) }}</td>
              </template>
            </tr>
          </tbody>
          <tfoot v-if="grandByKey">
            <tr>
              <td :colspan="result.row_fields.length" class="rowhead">Grand total</td>
              <template v-for="group in headerGroups" :key="group.label">
                <td v-for="column in group.columns" :key="column.key" class="num">
                  {{ fmt(grandByKey[column.key]) }}
                </td>
              </template>
            </tr>
          </tfoot>
        </table>
      </div>
      <p v-else class="muted hint">Computing…</p>
    </div>

    <div class="pivot-panel">
      <div class="panel-section">
        <div class="panel-head">Fields</div>
        <div class="field-list">
          <span
            v-for="column in columnNames"
            :key="column"
            class="chip"
            :class="{ used: inUse.has(column) }"
            draggable="true"
            v-tooltip.left="'Drag into a zone below, or click to add'"
            @dragstart="startDrag(column, 'list')"
            @click="quickAdd(column)"
          >
            <i :class="fieldIcon(column)" /> {{ column }}
          </span>
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'filters' }"
        @dragover.prevent="dragOver = 'filters'"
        @dragleave="dragOver = null"
        @drop="onDrop('filters')"
      >
        <div class="panel-head"><i class="pi pi-filter" /> Filters</div>
        <p v-if="filters.length === 0" class="muted empty">Drop a field here</p>
        <div v-for="(filter, index) in filters" :key="index" class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(filter.column, 'filters', index)"
          >{{ filter.column }}</span>
          <Select
            v-model="filter.op"
            :options="filterOps"
            optionLabel="label"
            optionValue="value"
            size="small"
            class="op"
          />
          <InputText v-if="needsValue(filter.op)" v-model="filter.value" size="small" placeholder="Value" class="val" />
          <InputText v-if="filter.op === 'between'" v-model="filter.value2" size="small" placeholder="and…" class="val" />
          <Button icon="pi pi-times" text severity="danger" size="small" @click="filters.splice(index, 1)" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'columns' }"
        @dragover.prevent="dragOver = 'columns'"
        @dragleave="dragOver = null"
        @drop="onDrop('columns')"
      >
        <div class="panel-head"><i class="pi pi-arrows-h" /> Columns</div>
        <p v-if="!columnField" class="muted empty">Drop one field here — its values go across</p>
        <div v-else class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(columnField, 'columns', 0)"
          >{{ columnField }}</span>
          <Button icon="pi pi-times" text severity="danger" size="small" @click="columnField = null" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'rows' }"
        @dragover.prevent="dragOver = 'rows'"
        @dragleave="dragOver = null"
        @drop="onDrop('rows')"
      >
        <div class="panel-head"><i class="pi pi-arrows-v" /> Rows</div>
        <p v-if="rowFields.length === 0" class="muted empty">Drop fields here — they go down</p>
        <div v-for="(field, index) in rowFields" :key="field" class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(field, 'rows', index)"
          >{{ field }}</span>
          <Button icon="pi pi-times" text severity="danger" size="small" @click="rowFields.splice(index, 1)" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'values' }"
        @dragover.prevent="dragOver = 'values'"
        @dragleave="dragOver = null"
        @drop="onDrop('values')"
      >
        <div class="panel-head"><i class="pi pi-calculator" /> Values</div>
        <p v-if="values.length === 0" class="muted empty">Drop fields here — default: count of rows</p>
        <div v-for="(value, index) in values" :key="index" class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(value.column ?? '', 'values', index)"
            v-tooltip.left="valueLabel(value)"
          >{{ value.column ?? 'rows' }}</span>
          <Select
            v-model="value.func"
            :options="funcOptions(value)"
            optionLabel="label"
            optionValue="value"
            size="small"
            class="op"
          />
          <Button icon="pi pi-times" text severity="danger" size="small" @click="values.splice(index, 1)" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.grow {
  flex: 1;
}

.pivot-layout {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
}

.pivot-result {
  flex: 1;
  min-width: 0;
  transition: opacity 0.15s;
}

.pivot-result.dim {
  opacity: 0.55;
}

.hint {
  padding: 2rem 1rem;
}

.error {
  color: var(--p-red-600);
  padding: 1rem;
}

.grid-wrap {
  overflow: auto;
  max-height: 70vh;
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  background: var(--p-surface-0);
}

.pivot-grid {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.875rem;
}

.pivot-grid th,
.pivot-grid td {
  padding: 0.4rem 0.75rem;
  border-bottom: 1px solid var(--p-surface-100);
  white-space: nowrap;
}

.pivot-grid thead th {
  position: sticky;
  top: 0;
  background: var(--p-surface-50);
  border-bottom: 1px solid var(--p-surface-300);
  font-weight: 600;
  color: var(--p-surface-700);
  text-align: right;
  z-index: 1;
}

.pivot-grid th.rowhead,
.pivot-grid td.rowhead {
  text-align: left;
  font-weight: 600;
}

.pivot-grid td.rowhead {
  color: var(--p-surface-800);
}

.pivot-grid th.grouphead {
  text-align: center;
  border-left: 1px solid var(--p-surface-200);
}

.pivot-grid td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.pivot-grid .total {
  font-weight: 600;
  border-left: 1px solid var(--p-surface-200);
}

.pivot-grid tfoot td {
  position: sticky;
  bottom: 0;
  background: var(--p-surface-50);
  border-top: 2px solid var(--p-surface-300);
  font-weight: 600;
}

.cell-null::after {
  content: '(blank)';
  color: var(--p-surface-400);
  font-weight: 400;
}

.pivot-panel {
  width: 21rem;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.panel-section,
.zone {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
}

.zone {
  min-height: 4.2rem;
}

.zone.over {
  border-color: var(--p-primary-400);
  background: var(--p-primary-50);
}

.panel-head {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--p-surface-600);
  margin-bottom: 0.45rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.field-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  max-height: 11rem;
  overflow: auto;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  background: var(--p-surface-100);
  border: 1px solid var(--p-surface-200);
  border-radius: 6px;
  padding: 0.2rem 0.55rem;
  cursor: grab;
  user-select: none;
}

.chip:hover {
  background: var(--p-surface-200);
}

.chip.used {
  opacity: 0.45;
}

.chip i {
  font-size: 0.7rem;
  color: var(--p-surface-500);
}

.zone-chip {
  background: var(--p-primary-50);
  border-color: var(--p-primary-200);
  color: var(--p-primary-800);
}

.zone-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-bottom: 0.35rem;
}

.zone-row .op {
  min-width: 7.5rem;
  flex-shrink: 0;
}

.zone-row .val {
  flex: 1;
  min-width: 4rem;
}

.empty {
  font-size: 0.8rem;
  border: 1px dashed var(--p-surface-300);
  border-radius: 6px;
  padding: 0.5rem;
  text-align: center;
  margin: 0;
}
</style>
