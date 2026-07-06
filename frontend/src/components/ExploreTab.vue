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
import type { ColumnSchema, FilterSpec, QueryResult, WorkspaceSummary } from '../types'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const table = ref<string | null>(null)
const schema = ref<ColumnSchema[]>([])
const filterOps = ref<{ value: string; label: string }[]>([])
const aggFuncs = ref<string[]>([])

const filters = ref<FilterSpec[]>([])
const groupBy = ref<string[]>([])
const aggs = ref<{ column: string | null; func: string }[]>([])
const sortSpec = ref<{ column: string; desc: boolean }[]>([])
const page = ref(1)
const pageSize = ref(50)

const result = ref<QueryResult | null>(null)
const running = ref(false)
const exporting = ref(false)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const columnNames = computed(() => schema.value.map((c) => c.name))
const numericColumns = computed(() =>
  schema.value.filter((c) => c.kind === 'numeric').map((c) => c.name),
)

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

const wasGrouped = ref(false)

function needsValue(op: string): boolean {
  return !['blank', 'not_blank'].includes(op)
}

async function loadMeta() {
  const meta = await api.get<{ filter_ops: { value: string; label: string }[]; agg_funcs: string[] }>(
    '/api/explore/meta',
  )
  filterOps.value = meta.filter_ops
  aggFuncs.value = meta.agg_funcs
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
  page.value = 1
}

watch(table, loadSchema)
watch(
  () => props.workspace.tables.length,
  () => {
    if (!table.value && tableOptions.value.length) table.value = tableOptions.value[0]
  },
  { immediate: true },
)

function spec() {
  return {
    filters: filters.value.filter((f) => f.column && f.op),
    group_by: groupBy.value,
    aggs: aggs.value
      .filter((a) => a.func)
      .map((a) => ({ column: a.column ?? '', func: a.func })),
    sort: sortSpec.value,
    page: page.value,
    page_size: pageSize.value,
  }
}

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
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Query failed', detail, life: 6000 })
  } finally {
    running.value = false
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
  run()
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
  run()
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
    <div class="field grow">
      <label>Group by</label>
      <MultiSelect
        v-model="groupBy"
        :options="columnNames"
        placeholder="No grouping (row-level view)"
        display="chip"
        filter
        style="min-width: 18rem"
      />
    </div>
    <Button label="Run" icon="pi pi-play" :loading="running" :disabled="!table" @click="run()" />
    <Button
      label="Export"
      icon="pi pi-file-excel"
      severity="secondary"
      :loading="exporting"
      :disabled="!result"
      v-tooltip.bottom="'Full result to Excel'"
      @click="exportExcel"
    />
  </div>

  <div class="builders">
    <div class="builder">
      <div class="builder-head">
        <span>Filters</span>
        <Button label="Add" icon="pi pi-plus" text size="small" @click="filters.push({ column: '', op: 'eq', value: '' })" />
      </div>
      <p v-if="filters.length === 0" class="muted small">No filters — all rows included.</p>
      <div v-for="(filter, index) in filters" :key="index" class="builder-row">
        <Select v-model="filter.column" :options="columnNames" placeholder="Column" filter class="w-col" />
        <Select v-model="filter.op" :options="filterOps" optionLabel="label" optionValue="value" class="w-op" />
        <InputText v-if="needsValue(filter.op)" v-model="filter.value" placeholder="Value" class="w-val" @keyup.enter="run()" />
        <InputText v-if="filter.op === 'between'" v-model="filter.value2" placeholder="and…" class="w-val" @keyup.enter="run()" />
        <Button icon="pi pi-times" text severity="danger" size="small" @click="filters.splice(index, 1)" />
      </div>
    </div>

    <div class="builder" v-if="groupBy.length > 0">
      <div class="builder-head">
        <span>Aggregations</span>
        <Button label="Add" icon="pi pi-plus" text size="small" @click="aggs.push({ column: null, func: 'sum' })" />
      </div>
      <p v-if="aggs.length === 0" class="muted small">Default: row count per group.</p>
      <div v-for="(agg, index) in aggs" :key="index" class="builder-row">
        <Select v-model="agg.func" :options="aggFuncs" class="w-op" />
        <Select
          v-if="agg.func !== 'count'"
          v-model="agg.column"
          :options="['sum', 'mean'].includes(agg.func) ? numericColumns : columnNames"
          placeholder="Column"
          filter
          class="w-col"
        />
        <Button icon="pi pi-times" text severity="danger" size="small" @click="aggs.splice(index, 1)" />
      </div>
    </div>
  </div>

  <template v-if="result">
    <div class="result-meta">
      <Tag :value="`${result.filtered_rows.toLocaleString()} rows after filters`" severity="secondary" />
      <Tag v-if="wasGrouped" :value="`${result.total_rows.toLocaleString()} groups`" severity="info" />
      <span v-if="wasGrouped" class="muted small">Click a group row to drill down to its rows.</span>
    </div>

    <DataTable
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
      scrollHeight="55vh"
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
  <p v-else-if="table" class="muted">Build a query and press Run.</p>
</template>

<style scoped>
.grow {
  flex: 1;
}

.builders {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.builder {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.75rem 1rem;
}

.builder-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  font-size: 0.85rem;
  color: var(--p-surface-600);
  margin-bottom: 0.4rem;
}

.builder-row {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.45rem;
}

.w-col {
  min-width: 13rem;
}

.w-op {
  min-width: 10rem;
}

.w-val {
  min-width: 10rem;
}

.small {
  font-size: 0.85rem;
}

.result-meta {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.6rem;
}

.cell-null::after {
  content: '∅';
  color: var(--p-surface-300);
}
</style>
