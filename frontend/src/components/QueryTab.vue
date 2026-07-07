<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import DataTable, { type DataTablePageEvent, type DataTableSortEvent, type DataTableRowClickEvent } from 'primevue/datatable'
import Column from 'primevue/column'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import type { AggSpec, ColumnSchema, FilterSpec, QueryResult, VizSpec, WorkspaceSummary } from '../types'
import ChartView from './ChartView.vue'
import PinDialog from './PinDialog.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const FUNC_LABELS: Record<string, string> = {
  count: 'Count',
  sum: 'Sum',
  mean: 'Average',
  min: 'Min',
  max: 'Max',
  n_unique: 'Distinct',
}
const NUMERIC_FUNCS = ['sum', 'mean', 'min', 'max', 'n_unique', 'count']
const TEXT_FUNCS = ['n_unique', 'min', 'max', 'count']

const table = ref<string | null>(null)
const schema = ref<ColumnSchema[]>([])
const filterOps = ref<{ value: string; label: string }[]>([])

const filters = ref<FilterSpec[]>([])
const groupBy = ref<string[]>([])
const aggs = ref<AggSpec[]>([])
const sortSpec = ref<{ column: string; desc: boolean }[]>([])
const page = ref(1)
const pageSize = ref(50)

const result = ref<QueryResult | null>(null)
const running = ref(false)
const exporting = ref(false)
const lastError = ref<string | null>(null)
const wasGrouped = ref(false)

const vizType = ref<VizSpec['type']>('table')
const vizX = ref<string | null>(null)
const vizY = ref<string[]>([])
const showPin = ref(false)
const pinning = ref(false)

type ZoneName = 'filters' | 'group' | 'aggs' | 'sort'
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

function aggFuncOptions(agg: AggSpec) {
  const funcs = agg.column && kindOf(agg.column) === 'numeric' ? NUMERIC_FUNCS : TEXT_FUNCS
  return funcs.map((f) => ({ value: f, label: FUNC_LABELS[f] }))
}

function aggLabel(agg: AggSpec): string {
  if (agg.func === 'count') return 'Count of rows'
  return `${FUNC_LABELS[agg.func] ?? agg.func} of ${agg.column}`
}

/** Fields already placed in Group by — dimmed in the Fields list. */
const inUse = computed(() => new Set(groupBy.value))

/** Columns available to order by: the shape the result will have. */
const orderableFields = computed(() => {
  if (groupBy.value.length > 0) {
    const names = [...groupBy.value]
    for (const agg of aggs.value) {
      if (agg.func === 'count') names.push('row_count')
      else if (agg.column) names.push(`${agg.column}_${agg.func}`)
    }
    if (aggs.value.length === 0) names.push('row_count')
    return names
  }
  if (aggs.value.length > 0) {
    return aggs.value.map((a) => (a.func === 'count' ? 'row_count' : `${a.column}_${a.func}`))
  }
  return columnNames.value
})

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
  groupBy.value = []
  aggs.value = []
  sortSpec.value = []
  result.value = null
  lastError.value = null
  page.value = 1
  vizType.value = 'table'
  vizX.value = null
  vizY.value = []
}

watch(table, loadSchema)
watch(vizType, syncVizDefaults)
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
  if (from === 'group') groupBy.value.splice(index, 1)
  else if (from === 'aggs') aggs.value.splice(index, 1)
  else if (from === 'filters') filters.value.splice(index, 1)
  else if (from === 'sort') sortSpec.value.splice(index, 1)
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
  if (zone === 'group') {
    if (!groupBy.value.includes(field)) groupBy.value.push(field)
  } else if (zone === 'aggs') {
    aggs.value.push({ column: field, func: kindOf(field) === 'numeric' ? 'sum' : 'n_unique' })
  } else if (zone === 'filters') {
    filters.value.push({ column: field, op: 'eq', value: '' })
  } else if (zone === 'sort') {
    // Once grouped, the sortable column is the aggregated output (e.g. amount_sum),
    // not the raw field — resolve the dropped field to a valid orderable column.
    const options = orderableFields.value
    const resolved = options.includes(field)
      ? field
      : options.find((o) => o.startsWith(`${field}_`)) ?? field
    if (!sortSpec.value.some((s) => s.column === resolved)) {
      const desc = resolved !== field || kindOf(field) === 'numeric'
      sortSpec.value.push({ column: resolved, desc })
    }
  }
}

/** Click a field: text/date → Group by, numbers → Aggregations (Excel-style). */
function quickAdd(field: string) {
  if (kindOf(field) === 'numeric') addToZone('aggs', field)
  else addToZone('group', field)
}

function needsValue(op: string): boolean {
  return !['blank', 'not_blank'].includes(op)
}

// ------------------------------------------------------------------ querying
function spec() {
  return {
    filters: filters.value.filter(
      (f) => f.column && f.op && (!needsValue(f.op) || f.value !== ''),
    ),
    group_by: groupBy.value,
    aggs: aggs.value
      .filter((a) => a.func === 'count' || a.column)
      .map((a) => ({ column: a.column ?? '', func: a.func })),
    sort: sortSpec.value.filter((s) => orderableFields.value.includes(s.column)),
    page: page.value,
    page_size: pageSize.value,
  }
}

let timer: ReturnType<typeof setTimeout> | undefined

// Live query: recompute (debounced) whenever the spec changes, Perspective-style.
watch(
  [filters, groupBy, aggs, sortSpec],
  () => {
    clearTimeout(timer)
    timer = setTimeout(() => run(true), 350)
  },
  { deep: true },
)

async function run(resetPage = true) {
  if (!table.value) return
  if (resetPage) page.value = 1
  running.value = true
  try {
    result.value = await api.post<QueryResult>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/query`,
      spec(),
    )
    wasGrouped.value = groupBy.value.length > 0
    lastError.value = null
    syncVizDefaults()
  } catch (error) {
    lastError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    running.value = false
  }
}

// Run once when a table is first loaded so the row-level view appears immediately.
watch(schema, () => {
  if (table.value && schema.value.length) run(true)
})

/** Keep the chart axes valid for the current result; default them when grouped. */
function syncVizDefaults() {
  if (!result.value) return
  const columns = result.value.columns
  const numeric = columns.filter((_, index) => /Int|Float|Decimal/.test(result.value!.dtypes[index]))
  if (vizX.value && !columns.includes(vizX.value)) vizX.value = null
  vizY.value = vizY.value.filter((column) => numeric.includes(column))
  if (vizType.value !== 'table') {
    if (!vizX.value) vizX.value = groupBy.value[0] ?? columns[0]
    if (vizY.value.length === 0 && numeric.length) vizY.value = [numeric[0]]
  }
}

const currentViz = computed<VizSpec>(() => ({
  type: vizType.value,
  x: vizX.value ?? undefined,
  y: vizY.value,
}))

const records = computed(() => {
  if (!result.value) return []
  return result.value.rows.map((row) => {
    const record: Record<string, unknown> = {}
    result.value!.columns.forEach((column, index) => {
      record[column] = row[index]
    })
    return record
  })
})

const resultNumericColumns = computed(() => {
  if (!result.value) return []
  return result.value.columns.filter((_, index) =>
    /Int|Float|Decimal/.test(result.value!.dtypes[index]),
  )
})

async function pinTile({ title, note }: { title: string; note: string }) {
  if (!table.value) return
  pinning.value = true
  try {
    const tileSpec = { ...spec() }
    delete (tileSpec as Record<string, unknown>).page
    delete (tileSpec as Record<string, unknown>).page_size
    await api.post(`/api/workspaces/${props.workspace.id}/tiles`, {
      kind: 'query',
      table: table.value,
      title,
      note,
      spec: tileSpec,
      viz: currentViz.value,
    })
    showPin.value = false
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', detail: title, life: 3000 })
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Pin failed', detail, life: 6000 })
  } finally {
    pinning.value = false
  }
}

function onPage(event: DataTablePageEvent) {
  page.value = event.page + 1
  pageSize.value = event.rows
  run(false)
}

function onSort(event: DataTableSortEvent) {
  if (typeof event.sortField === 'string') {
    sortSpec.value = [{ column: event.sortField, desc: event.sortOrder === -1 }]
  } else {
    sortSpec.value = []
  }
}

/** Click an aggregated row → drill down to its underlying rows. */
function onRowClick(event: DataTableRowClickEvent) {
  if (!wasGrouped.value || !result.value) return
  const record = event.data as Record<string, unknown>
  const drill: FilterSpec[] = groupBy.value.map((column) => {
    const value = record[column]
    return value === null || value === undefined
      ? { column, op: 'blank', value: '' }
      : { column, op: 'eq', value: String(value) }
  })
  filters.value = [...filters.value, ...drill]
  groupBy.value = []
  aggs.value = []
  sortSpec.value = []
}

async function exportExcel() {
  if (!table.value) return
  exporting.value = true
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/query/export`,
      spec(),
      `${table.value}_query.xlsx`,
    )
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Export failed', detail, life: 6000 })
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <div class="toolbar">
    <div class="field">
      <label>Table</label>
      <Select v-model="table" :options="tableOptions" placeholder="Pick a table" style="min-width: 14rem" />
    </div>
    <span class="grow" />
    <Tag
      v-if="result"
      :value="`${result.filtered_rows.toLocaleString()} rows after filters`"
      severity="secondary"
    />
    <Tag v-if="result && wasGrouped" :value="`${result.total_rows.toLocaleString()} groups`" severity="info" />
    <Button
      label="Export"
      icon="pi pi-file-excel"
      severity="secondary"
      :loading="exporting"
      :disabled="!result"
      v-tooltip.bottom="'Full result to Excel'"
      @click="exportExcel"
    />
    <Button
      label="Pin"
      icon="pi pi-thumbtack"
      severity="secondary"
      :disabled="!result"
      v-tooltip.bottom="'Pin this query to the dashboard'"
      @click="showPin = true"
    />
  </div>

  <div class="query-layout">
    <div class="query-result" :class="{ dim: running }">
      <p v-if="!table" class="muted hint">Pick a table to begin.</p>
      <p v-else-if="lastError" class="error">{{ lastError }}</p>
      <template v-else-if="result">
        <div class="result-meta" v-if="wasGrouped">
          <span class="muted small"><i class="pi pi-info-circle" /> Click a group row to drill down to its rows.</span>
          <span class="viz-controls">
            <Select v-model="vizType" :options="['table', 'bar', 'line', 'pie']" class="viz-type" />
            <template v-if="vizType !== 'table'">
              <Select v-model="vizX" :options="result.columns" placeholder="X axis" filter class="viz-axis" />
              <MultiSelect v-model="vizY" :options="resultNumericColumns" placeholder="Values" display="chip" class="viz-axis" />
            </template>
          </span>
        </div>

        <div v-if="vizType !== 'table'" class="chart-panel">
          <ChartView :frame="result" :viz="currentViz" height="320px" />
        </div>

        <DataTable
          v-else
          :value="records"
          lazy
          paginator
          :rows="result.page_size"
          :first="(result.page - 1) * result.page_size"
          :totalRecords="result.total_rows"
          :rowsPerPageOptions="[25, 50, 100, 250]"
          size="small"
          stripedRows
          scrollable
          scrollHeight="60vh"
          :rowHover="wasGrouped"
          :loading="running"
          @page="onPage"
          @sort="onSort"
          @row-click="onRowClick"
        >
          <Column
            v-for="column in result.columns"
            :key="column"
            :field="column"
            :header="column"
            sortable
          >
            <template #body="{ data }">
              <span :class="{ 'cell-null': data[column] === null }">
                {{ data[column] === null ? '' : typeof data[column] === 'number' ? data[column].toLocaleString() : data[column] }}
              </span>
            </template>
          </Column>
        </DataTable>
      </template>
      <p v-else class="muted hint">Computing…</p>
    </div>

    <div class="query-panel">
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
        <p v-if="filters.length === 0" class="muted empty">Drop a field here to filter</p>
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
          <InputText v-if="needsValue(filter.op)" v-model="filter.value" size="small" placeholder="Value" class="val" @keyup.enter="run()" />
          <InputText v-if="filter.op === 'between'" v-model="filter.value2" size="small" placeholder="and…" class="val" @keyup.enter="run()" />
          <Button icon="pi pi-times" text severity="danger" size="small" @click="filters.splice(index, 1)" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'group' }"
        @dragover.prevent="dragOver = 'group'"
        @dragleave="dragOver = null"
        @drop="onDrop('group')"
      >
        <div class="panel-head"><i class="pi pi-sitemap" /> Group by</div>
        <p v-if="groupBy.length === 0" class="muted empty">Drop fields here — one row per group</p>
        <div v-for="(field, index) in groupBy" :key="field" class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(field, 'group', index)"
          >{{ field }}</span>
          <Button icon="pi pi-times" text severity="danger" size="small" @click="groupBy.splice(index, 1)" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'aggs' }"
        @dragover.prevent="dragOver = 'aggs'"
        @dragleave="dragOver = null"
        @drop="onDrop('aggs')"
      >
        <div class="panel-head"><i class="pi pi-calculator" /> Aggregations</div>
        <p v-if="aggs.length === 0" class="muted empty">
          {{ groupBy.length ? 'Drop fields here — default: count of rows' : 'Drop fields here to aggregate' }}
        </p>
        <div v-for="(agg, index) in aggs" :key="index" class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(agg.column ?? '', 'aggs', index)"
            v-tooltip.left="aggLabel(agg)"
          >{{ agg.column ?? 'rows' }}</span>
          <Select
            v-model="agg.func"
            :options="aggFuncOptions(agg)"
            optionLabel="label"
            optionValue="value"
            size="small"
            class="op"
          />
          <Button icon="pi pi-times" text severity="danger" size="small" @click="aggs.splice(index, 1)" />
        </div>
      </div>

      <div
        class="zone"
        :class="{ over: dragOver === 'sort' }"
        @dragover.prevent="dragOver = 'sort'"
        @dragleave="dragOver = null"
        @drop="onDrop('sort')"
      >
        <div class="panel-head"><i class="pi pi-sort-alt" /> Order by</div>
        <p v-if="sortSpec.length === 0" class="muted empty">Drop fields here to sort</p>
        <div v-for="(sort, index) in sortSpec" :key="index" class="zone-row">
          <Select
            v-model="sort.column"
            :options="orderableFields"
            size="small"
            class="op grow-sel"
            filter
          />
          <Button
            :icon="sort.desc ? 'pi pi-sort-amount-down' : 'pi pi-sort-amount-up'"
            text
            size="small"
            v-tooltip.left="sort.desc ? 'Descending' : 'Ascending'"
            @click="sort.desc = !sort.desc"
          />
          <Button icon="pi pi-times" text severity="danger" size="small" @click="sortSpec.splice(index, 1)" />
        </div>
      </div>
    </div>
  </div>

  <PinDialog
    v-model:visible="showPin"
    :defaultTitle="wasGrouped ? `${table}: by ${groupBy.join(', ') || 'group'}` : `${table}: filtered rows`"
    :saving="pinning"
    @pin="pinTile"
  />
</template>

<style scoped>
.grow {
  flex: 1;
}

.query-layout {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
}

.query-result {
  flex: 1;
  min-width: 0;
  transition: opacity 0.15s;
}

.query-result.dim {
  opacity: 0.55;
}

.hint {
  padding: 2rem 1rem;
}

.error {
  color: var(--p-red-600);
  padding: 1rem;
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.6rem;
  flex-wrap: wrap;
}

.small {
  font-size: 0.85rem;
}

.viz-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-left: auto;
}

.viz-type {
  min-width: 7rem;
}

.viz-axis {
  min-width: 11rem;
}

.chart-panel {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 0.75rem;
}

.cell-null::after {
  content: '∅';
  color: var(--p-surface-300);
}

.query-panel {
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

.zone-row .grow-sel {
  flex: 1;
  min-width: 0;
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
