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
const splitField = ref<string | null>(null)
const aggs = ref<AggSpec[]>([])
const sortSpec = ref<{ column: string; desc: boolean }[]>([])
const visibleColumns = ref<string[]>([])
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

type ZoneName = 'filters' | 'group' | 'split' | 'aggs' | 'sort'
const drag = ref<{ field: string; from: ZoneName | 'list'; index: number } | null>(null)
const dragOver = ref<ZoneName | null>(null)

/** Cross-tab mode: a Split by field turns the flat query into a pivot grid. */
const isPivot = computed(() => !!splitField.value)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const columnNames = computed(() => schema.value.map((c) => c.name))
const visibleColumnSet = computed(() => new Set(visibleColumns.value))
const hiddenCount = computed(() => Math.max(columnNames.value.length - visibleColumns.value.length, 0))
const querySummary = computed(() => {
  if (!table.value) return 'Select a table to begin'
  const parts = [table.value]
  if (filters.value.length) parts.push(`${filters.value.length} filter${filters.value.length === 1 ? '' : 's'}`)
  if (groupBy.value.length) parts.push(`grouped by ${groupBy.value.join(', ')}`)
  if (splitField.value) parts.push(`split by ${splitField.value}`)
  if (aggs.value.length) parts.push(aggs.value.map(aggLabel).join(', '))
  return parts.join(' · ')
})

const resultSummary = computed(() => {
  const r = result.value
  if (!r) return 'Awaiting query'
  if (isPivot.value || wasGrouped.value) {
    const groupsLabel = r.total_rows === 1 ? 'group' : 'groups'
    return `${r.total_rows.toLocaleString()} ${groupsLabel} from ${r.filtered_rows.toLocaleString()} rows`
  }
  return `${r.filtered_rows.toLocaleString()} of ${r.total_rows.toLocaleString()} rows`
})

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

/** Fields already placed as dimensions (Group by / Split by) — dimmed in Fields. */
const inUse = computed(
  () => new Set([...groupBy.value, ...(splitField.value ? [splitField.value] : [])]),
)

/** Columns available to order by: the shape a flat (non-pivot) result will have. */
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
  splitField.value = null
  aggs.value = []
  sortSpec.value = []
  visibleColumns.value = columnNames.value
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
  else if (from === 'split') splitField.value = null
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
    if (splitField.value === field) splitField.value = null
    if (!groupBy.value.includes(field)) groupBy.value.push(field)
  } else if (zone === 'split') {
    const rowIndex = groupBy.value.indexOf(field)
    if (rowIndex !== -1) groupBy.value.splice(rowIndex, 1)
    splitField.value = field
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

function toggleColumn(field: string) {
  if (visibleColumnSet.value.has(field)) {
    if (visibleColumns.value.length <= 1) return
    visibleColumns.value = visibleColumns.value.filter((column) => column !== field)
  } else {
    const ordered = columnNames.value.filter((column) => column === field || visibleColumnSet.value.has(column))
    visibleColumns.value = ordered
  }
}

function showAllColumns() {
  visibleColumns.value = columnNames.value
}

function clearQuery() {
  filters.value = []
  groupBy.value = []
  splitField.value = null
  aggs.value = []
  sortSpec.value = []
  visibleColumns.value = columnNames.value
}

function needsValue(op: string): boolean {
  return !['blank', 'not_blank'].includes(op)
}

// ------------------------------------------------------------------ querying
function spec() {
  const payload = {
    filters: filters.value.filter(
      (f) => f.column && f.op && (!needsValue(f.op) || f.value !== ''),
    ),
    group_by: groupBy.value,
    split_by: splitField.value,
    aggs: aggs.value
      .filter((a) => a.func === 'count' || a.column)
      .map((a) => ({ column: a.column ?? '', func: a.func })),
    sort: sortSpec.value.filter((s) => orderableFields.value.includes(s.column)),
    page: page.value,
    page_size: pageSize.value,
  }
  if (!splitField.value && groupBy.value.length === 0 && aggs.value.length === 0) {
    return { ...payload, columns: visibleColumns.value }
  }
  return payload
}

let timer: ReturnType<typeof setTimeout> | undefined

// Live query: recompute (debounced) whenever the spec changes, Perspective-style.
watch(
  [filters, groupBy, splitField, aggs, sortSpec, visibleColumns],
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
    wasGrouped.value = !isPivot.value && groupBy.value.length > 0
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

// ------------------------------------------------------------- visualization
const vizXOptions = computed(() => {
  const r = result.value
  if (!r) return []
  return isPivot.value ? r.row_fields ?? [] : r.columns
})

const vizYOptions = computed(() => {
  const r = result.value
  if (!r) return []
  if (isPivot.value) {
    const rowFields = new Set(r.row_fields ?? [])
    return r.columns.filter((c, i) => !rowFields.has(c) && /Int|Float|Decimal/.test(r.dtypes[i]))
  }
  return r.columns.filter((_, i) => /Int|Float|Decimal/.test(r.dtypes[i]))
})

/** Keep chart axes valid for the current result; default them when charting. */
function syncVizDefaults() {
  const r = result.value
  if (!r) return
  if (vizX.value && !vizXOptions.value.includes(vizX.value)) vizX.value = null
  vizY.value = vizY.value.filter((c) => vizYOptions.value.includes(c))
  if (!isPivot.value && !wasGrouped.value && vizType.value !== 'table') vizType.value = 'table'
  if (vizType.value !== 'table') {
    if (!vizX.value) vizX.value = vizXOptions.value[0] ?? null
    if (vizY.value.length === 0 && vizYOptions.value.length) vizY.value = [vizYOptions.value[0]]
  }
}

const showChartControls = computed(() => !!result.value && (isPivot.value || wasGrouped.value))

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

const displayedFlatColumns = computed(() => {
  const r = result.value
  if (!r) return []
  if (isPivot.value || wasGrouped.value) return r.columns
  const visible = r.columns.filter((column) => visibleColumnSet.value.has(column))
  return visible.length ? visible : r.columns
})

const numericFlatColumns = computed(() => {
  const r = result.value
  const numeric = new Set<string>()
  if (!r) return numeric
  r.columns.forEach((column, index) => {
    if (/Int|Float|Decimal/.test(r.dtypes[index] ?? '')) numeric.add(column)
  })
  return numeric
})

const showFlatPaginator = computed(() => {
  const r = result.value
  return !!r && !isPivot.value && r.total_rows > r.page_size
})

// ------------------------------------------------- cross-tab grid rendering
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

/** Header groups over the data columns: one group per value (spanning the
 *  split keys), then a Total group. */
const headerGroups = computed(() => {
  const r = result.value
  if (!r || !isPivot.value) return []
  const valueNames = r.value_names ?? []
  const splitKeys = r.column_keys ?? []
  const hasTotal = r.columns.some((c) => c.endsWith('::Total'))
  if (!r.split_field) {
    return [{ label: '', columns: valueNames.map((name) => ({ key: name, label: displayName(name) })) }]
  }
  const groups = valueNames.map((name) => ({
    label: displayName(name),
    columns: splitKeys.map((key) => ({ key: `${name}::${key}`, label: key })),
  }))
  if (hasTotal) {
    groups.push({
      label: 'Total',
      columns: valueNames.map((name) => ({
        key: `${name}::Total`,
        label: valueNames.length > 1 ? displayName(name) : 'Total',
      })),
    })
  }
  return groups
})

const twoRowHeader = computed(() => {
  const r = result.value
  return !!r && isPivot.value && !!r.split_field && (r.value_names ?? []).length > 1
})

const bodyRows = computed(() => {
  const r = result.value
  if (!r || !isPivot.value) return []
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
  if (!r || !isPivot.value || !r.grand_total) return null
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

// ------------------------------------------------------------------ export
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
  <div class="query-commandbar surface-panel">
    <div class="field">
      <label>Source table</label>
      <Select v-model="table" :options="tableOptions" placeholder="Pick a table" style="min-width: 14rem" />
    </div>
    <div class="query-context">
      <strong>{{ querySummary }}</strong>
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
    <Button icon="pi pi-undo" severity="secondary" text :disabled="!filters.length && !groupBy.length && !aggs.length && !splitField && !sortSpec.length" v-tooltip.bottom="'Clear query'" @click="clearQuery" />
  </div>

  <div class="query-layout">
    <div class="query-result surface-panel" :class="{ dim: running }">
      <div class="result-titlebar">
        <div><strong>{{ resultSummary }}</strong></div>
        <span v-if="running" class="computing"><i class="pi pi-spinner pi-spin" /> Recomputing</span>
      </div>
      <div class="result-body">
      <div v-if="!table" class="empty-state compact-empty"><div><span class="empty-state-icon"><i class="pi pi-table" /></span><h3>Select a source table</h3></div></div>
      <div v-else-if="lastError" class="error"><i class="pi pi-exclamation-circle" /> {{ lastError }}</div>
      <template v-else-if="result">
        <div class="result-meta" v-if="showChartControls || wasGrouped">
          <span v-if="wasGrouped" class="muted small"><i class="pi pi-info-circle" /> Click a group row to drill down to its rows.</span>
          <span v-if="showChartControls" class="viz-controls">
            <Select v-model="vizType" :options="['table', 'bar', 'line', 'pie']" class="viz-type" />
            <template v-if="vizType !== 'table'">
              <Select v-model="vizX" :options="vizXOptions" placeholder="X axis" filter class="viz-axis" />
              <MultiSelect v-model="vizY" :options="vizYOptions" placeholder="Values" display="chip" class="viz-axis" />
            </template>
          </span>
        </div>

        <div v-if="vizType !== 'table'" class="chart-panel">
          <ChartView :frame="result" :viz="currentViz" height="320px" />
        </div>

        <!-- Cross-tab grid (Split by active) -->
        <div v-else-if="isPivot && result.rows.length" class="grid-wrap">
          <table class="pivot-grid">
            <thead>
              <tr v-if="twoRowHeader">
                <th v-for="field in result.row_fields" :key="field" class="rowhead" rowspan="2">{{ field }}</th>
                <th v-for="group in headerGroups" :key="group.label" :colspan="group.columns.length" class="grouphead">
                  {{ group.label }}
                </th>
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
                <td :colspan="result.row_fields!.length" class="rowhead">Grand total</td>
                <template v-for="group in headerGroups" :key="group.label">
                  <td v-for="column in group.columns" :key="column.key" class="num">
                    {{ fmt(grandByKey[column.key]) }}
                  </td>
                </template>
              </tr>
            </tfoot>
          </table>
        </div>
        <p v-else-if="isPivot" class="muted hint">No rows match the filters.</p>

        <!-- Flat result (no Split by) -->
        <DataTable
          v-else
          :value="records"
          :class="['query-table', { 'query-table--drillable': wasGrouped }]"
          lazy
          :paginator="showFlatPaginator"
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
            v-for="column in displayedFlatColumns"
            :key="column"
            :field="column"
            :header="column"
            :headerClass="{ 'num-col': numericFlatColumns.has(column) }"
            :bodyClass="{ 'num-col': numericFlatColumns.has(column), 'aw-figure': numericFlatColumns.has(column) }"
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
      <p v-else class="muted hint"><i class="pi pi-spinner pi-spin" /> Computing result…</p>
      </div>
    </div>

    <aside class="query-panel">
      <div class="builder-title"><div><p class="eyebrow">Query builder</p><strong>Shape the result</strong></div><span class="muted small">Drag or click fields</span></div>
      <div class="panel-section">
        <div class="panel-head">
          <span>Fields</span>
          <span class="grow" />
          <span v-if="hiddenCount" class="muted small">{{ hiddenCount }} hidden</span>
          <Button
            icon="pi pi-eye"
            text
            rounded
            size="small"
            :disabled="hiddenCount === 0"
            v-tooltip.left="'Show all fields'"
            @click="showAllColumns"
          />
        </div>
        <div class="field-list">
          <div
            v-for="column in columnNames"
            :key="column"
            class="field-item"
            :class="{ hidden: !visibleColumnSet.has(column) }"
          >
            <span
              class="chip field-chip"
              :class="{ used: inUse.has(column) }"
              draggable="true"
              v-tooltip.left="'Drag into a zone below, or click to add'"
              @dragstart="startDrag(column, 'list')"
              @click="quickAdd(column)"
            >
              <i :class="fieldIcon(column)" />
              <span class="field-name">{{ column }}</span>
            </span>
            <Button
              :icon="visibleColumnSet.has(column) ? 'pi pi-eye' : 'pi pi-eye-slash'"
              text
              rounded
              size="small"
              class="visibility-btn"
              :severity="visibleColumnSet.has(column) ? 'secondary' : 'contrast'"
              :disabled="visibleColumnSet.has(column) && visibleColumns.length <= 1"
              v-tooltip.left="visibleColumnSet.has(column) ? 'Hide field' : 'Show field'"
              @click.stop="toggleColumn(column)"
            />
          </div>
        </div>
      </div>

      <div
        data-zone="filters"
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
        data-zone="group"
        class="zone"
        :class="{ over: dragOver === 'group' }"
        @dragover.prevent="dragOver = 'group'"
        @dragleave="dragOver = null"
        @drop="onDrop('group')"
      >
        <div class="panel-head"><i class="pi pi-arrows-v" /> Group by</div>
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

      <details
        data-zone="split"
        class="zone advanced-zone"
        :open="Boolean(splitField)"
        :class="{ over: dragOver === 'split' }"
        @dragover.prevent="dragOver = 'split'"
        @dragleave="dragOver = null"
        @drop="onDrop('split')"
      >
        <summary class="panel-head"><span><i class="pi pi-arrows-h" /> Split by</span><small>Advanced</small><i class="pi pi-chevron-down" /></summary>
        <p v-if="!splitField" class="muted empty">Drop one field here — its values become columns</p>
        <div v-else class="zone-row">
          <span
            class="chip zone-chip"
            draggable="true"
            @dragstart="startDrag(splitField, 'split', 0)"
          >{{ splitField }}</span>
          <Button icon="pi pi-times" text severity="danger" size="small" @click="splitField = null" />
        </div>
      </details>

      <details
        data-zone="aggs"
        class="zone advanced-zone"
        :open="Boolean(aggs.length)"
        :class="{ over: dragOver === 'aggs' }"
        @dragover.prevent="dragOver = 'aggs'"
        @dragleave="dragOver = null"
        @drop="onDrop('aggs')"
      >
        <summary class="panel-head"><span><i class="pi pi-calculator" /> Aggregations</span><small>Advanced</small><i class="pi pi-chevron-down" /></summary>
        <p v-if="aggs.length === 0" class="muted empty">
          {{ groupBy.length || isPivot ? 'Drop fields here — default: count of rows' : 'Drop fields here to aggregate' }}
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
      </details>

      <details
        v-if="!isPivot"
        data-zone="sort"
        class="zone advanced-zone"
        :open="Boolean(sortSpec.length)"
        :class="{ over: dragOver === 'sort' }"
        @dragover.prevent="dragOver = 'sort'"
        @dragleave="dragOver = null"
        @drop="onDrop('sort')"
      >
        <summary class="panel-head"><span><i class="pi pi-sort-alt" /> Order by</span><small>Advanced</small><i class="pi pi-chevron-down" /></summary>
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
      </details>
    </aside>
  </div>
</template>

<style scoped>
.grow {
  flex: 1;
}

.query-commandbar { display: flex; flex-wrap: wrap; align-items: flex-end; gap: .75rem; margin-bottom: 1rem; padding: .75rem .9rem; position: sticky; top: 0; z-index: 8; box-shadow: var(--aw-shadow-md); }
.query-context { display: flex; flex-direction: column; align-self: center; min-width: 15rem; }
.query-context .eyebrow { margin: 0; }
.query-context strong { max-width: 36rem; overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-sm); text-overflow: ellipsis; white-space: nowrap; }
.query-layout {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
}

.query-result {
  flex: 1;
  min-width: 0;
  transition: opacity 0.15s;
  overflow: hidden;
  box-shadow: none;
}
.result-titlebar { display: flex; align-items: center; justify-content: space-between; min-height: 3.7rem; padding: .7rem .9rem; border-bottom: 1px solid var(--aw-border); background: var(--aw-raised); }
.result-titlebar .eyebrow { margin-bottom: .1rem; }
.result-titlebar strong { font-size: var(--aw-text-sm); font-family: var(--aw-font-mono); letter-spacing: -0.01em; }
.result-body { padding: .75rem; }
.computing { display: flex; align-items: center; gap: .35rem; color: var(--aw-teal); font-size: var(--aw-text-sm); font-weight: 650; }
.compact-empty { min-height: 24rem; border: 0; background: transparent; }

.query-result.dim {
  opacity: 0.55;
}

.hint {
  padding: 2rem 1rem;
}

.error {
  color: var(--aw-danger);
  padding: 1rem;
  border: 1px solid var(--aw-danger);
  border-radius: var(--aw-radius-control);
  background: var(--aw-danger-soft);
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.6rem;
  flex-wrap: wrap;
}

.small {
  font-size: var(--aw-text-base);
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
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 1rem;
  margin-bottom: 0.75rem;
}

.cell-null::after {
  content: '∅';
  color: var(--aw-border-strong);
}

:deep(.query-table) {
  width: fit-content;
  max-width: 100%;
  overflow: hidden;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  box-shadow: var(--aw-shadow-sm);
  font-size: var(--aw-text-sm);
}

:deep(.query-table .p-datatable-table-container) {
  background: var(--aw-panel);
}

:deep(.query-table .p-datatable-table) {
  width: max-content;
  min-width: max-content;
  border-collapse: separate;
  border-spacing: 0;
}

:deep(.query-table .p-datatable-thead > tr > th),
:deep(.query-table .p-datatable-tbody > tr > td) {
  padding: 0.52rem 0.7rem;
  white-space: nowrap;
  min-width: 7.5rem;
}

:deep(.query-table .p-datatable-thead > tr > th) {
  border-bottom: 1px solid var(--aw-border-strong);
  background: var(--aw-raised);
  color: var(--aw-ink-strong);
  font-size: var(--aw-text-xs);
  font-weight: 700;
  line-height: 1.2;
}

:deep(.query-table .p-column-header-content) {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  max-width: 100%;
}

:deep(.query-table .num-col .p-column-header-content) {
  justify-content: flex-end;
  width: 100%;
}

:deep(.query-table .p-datatable-thead > tr > th:first-child) {
  border-top-left-radius: calc(var(--aw-radius-control) - 1px);
}

:deep(.query-table .p-datatable-thead > tr > th:last-child) {
  border-top-right-radius: calc(var(--aw-radius-control) - 1px);
}

:deep(.query-table .p-datatable-tbody > tr > td) {
  border-color: var(--aw-border);
  color: var(--aw-ink);
  line-height: 1.35;
}

:deep(.query-table .p-datatable-tbody > tr:nth-child(even) > td) {
  background: var(--aw-canvas);
}

:deep(.query-table.query-table--drillable .p-datatable-tbody > tr) {
  cursor: pointer;
}

:deep(.query-table.query-table--drillable .p-datatable-tbody > tr:hover > td) {
  background: var(--aw-teal-soft);
}

:deep(.query-table .p-sortable-column-icon),
:deep(.query-table .p-datatable-sort-icon) {
  width: 0.85rem;
  height: 0.85rem;
  color: var(--aw-muted);
}

:deep(.query-table .p-paginator) {
  min-height: 3.1rem;
  padding: 0.45rem 0.65rem;
  border-top: 1px solid var(--aw-border);
  background: var(--aw-canvas);
  gap: 0.15rem;
}

:deep(.query-table .p-paginator .p-paginator-page),
:deep(.query-table .p-paginator .p-paginator-first),
:deep(.query-table .p-paginator .p-paginator-prev),
:deep(.query-table .p-paginator .p-paginator-next),
:deep(.query-table .p-paginator .p-paginator-last) {
  min-width: 2.1rem;
  height: 2.1rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-muted);
}

:deep(.query-table .p-paginator .p-paginator-page.p-highlight),
:deep(.query-table .p-paginator .p-paginator-page.p-paginator-page-selected) {
  background: var(--aw-teal-soft);
  color: var(--aw-teal);
  font-weight: 700;
}

:deep(.query-table .p-paginator .p-select) {
  height: 2.35rem;
  border-radius: var(--aw-radius-control);
}

:deep(.query-table .p-paginator .p-select-label) {
  padding-top: 0.42rem;
  padding-bottom: 0.42rem;
}

:deep(.query-table .num-col) {
  text-align: right;
  font-family: var(--aw-font-mono);
  font-variant-numeric: tabular-nums;
  min-width: 8rem;
}

/* cross-tab grid */
.grid-wrap {
  width: fit-content;
  max-width: 100%;
  overflow: auto;
  max-height: 70vh;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  box-shadow: var(--aw-shadow-sm);
}

.pivot-grid {
  border-collapse: separate;
  border-spacing: 0;
  width: max-content;
  min-width: max-content;
  font-size: var(--aw-text-sm);
}

.pivot-grid th,
.pivot-grid td {
  padding: 0.52rem 0.7rem;
  border-bottom: 1px solid var(--aw-border);
  white-space: nowrap;
  line-height: 1.35;
  min-width: 7.5rem;
}

.pivot-grid thead th {
  position: sticky;
  top: 0;
  background: var(--aw-raised);
  border-bottom: 1px solid var(--aw-border-strong);
  font-size: var(--aw-text-xs);
  font-weight: 700;
  color: var(--aw-ink-strong);
  text-align: right;
  z-index: 1;
}

.pivot-grid tbody tr:nth-child(even) td {
  background: var(--aw-canvas);
}

.pivot-grid th.rowhead,
.pivot-grid td.rowhead {
  text-align: left;
  font-weight: 600;
}

.pivot-grid td.rowhead {
  color: var(--aw-ink);
}

.pivot-grid th.grouphead {
  text-align: center;
  border-left: 1px solid var(--aw-border);
}

.pivot-grid td.num {
  text-align: right;
  font-family: var(--aw-font-mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}

.pivot-grid .total {
  font-weight: 600;
  border-left: 1px solid var(--aw-border);
}

.pivot-grid tfoot td {
  position: sticky;
  bottom: 0;
  background: var(--aw-raised);
  border-top: 2px solid var(--aw-border-strong);
  font-weight: 600;
}

.pivot-grid tfoot td.num { font-family: var(--aw-font-mono); }

.pivot-grid .cell-null::after {
  content: '(blank)';
  color: var(--aw-muted);
  font-weight: 400;
}

.query-panel {
  order: -1;
  width: 19rem;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.builder-title { display: flex; align-items: flex-end; justify-content: space-between; padding: .15rem .1rem .25rem; }
.builder-title .eyebrow { margin-bottom: .1rem; }

.panel-section,
.zone {
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.6rem 0.75rem;
}

.zone { border-left-width: 3px; }
.zone[data-zone='filters'] { border-left-color: var(--aw-warn); }
.zone[data-zone='group'] { border-left-color: var(--aw-info); }
.zone[data-zone='split'] { border-left-color: var(--aw-accent); }
.zone[data-zone='aggs'] { border-left-color: var(--aw-teal); }
.zone[data-zone='sort'] { border-left-color: var(--aw-muted); }

.zone {
  min-height: 4.2rem;
}
.advanced-zone { min-height: 2.65rem; }
.advanced-zone > summary { min-height: 1.6rem; margin: 0; cursor: pointer; list-style: none; }
.advanced-zone > summary::-webkit-details-marker { display: none; }
.advanced-zone > summary span { display: flex; align-items: center; gap: .4rem; }
.advanced-zone > summary small { margin-left: auto; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; }
.advanced-zone > summary > .pi-chevron-down { color: var(--aw-muted); font-size: var(--aw-text-2xs); transition: rotate .15s; }
.advanced-zone[open] > summary > .pi-chevron-down { rotate: 180deg; }

.zone.over {
  border-color: var(--aw-teal);
  box-shadow: inset 0 0 0 1px var(--aw-teal);
  background: var(--aw-teal-soft);
}

.panel-head {
  font-size: var(--aw-text-sm);
  font-weight: 600;
  color: var(--aw-ink);
  margin-bottom: 0.45rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.field-list {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  max-height: 11rem;
  overflow: auto;
}

.field-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 2rem;
  align-items: center;
  gap: 0.2rem;
  min-height: 2rem;
}

.field-item.hidden .field-chip {
  opacity: 0.5;
  text-decoration: line-through;
}

.field-chip {
  min-width: 0;
}

.field-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.visibility-btn {
  width: 1.8rem;
  height: 1.8rem;
  justify-self: end;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: var(--aw-text-sm);
  background: var(--aw-raised);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.25rem 0.6rem;
  cursor: grab;
  user-select: none;
  transition: border-color 0.15s, background 0.15s;
}

.chip:hover {
  background: var(--aw-panel);
  border-color: var(--aw-teal);
}

.chip.used {
  opacity: 0.45;
}

@media (max-width: 1100px) {
  .query-layout { flex-direction: column; }
  .query-panel { order: 0; width: 100%; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .builder-title { grid-column: 1 / -1; }
  .query-result { width: 100%; }
}

@media (max-width: 720px) {
  .query-context { display: none; }
  .query-panel { grid-template-columns: 1fr; }
  .builder-title { grid-column: auto; }
}

.chip i {
  font-size: var(--aw-text-xs);
  color: var(--aw-muted);
}

.zone-chip {
  background: var(--aw-teal-soft);
  border-color: var(--aw-teal-line);
  color: var(--aw-teal-strong);
  font-weight: 600;
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
  font-size: var(--aw-text-sm);
  border: 1px dashed var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  padding: 0.5rem;
  text-align: center;
  margin: 0;
}
</style>
