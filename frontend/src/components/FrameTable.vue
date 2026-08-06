<script setup lang="ts">
import { computed } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import type { FramePayload } from '../types'

const props = defineProps<{
  frame: FramePayload
  scrollHeight?: string
}>()

// DataTable wants row objects; the API sends compact row arrays.
const records = computed(() =>
  props.frame.rows.map((row) => {
    const record: Record<string, unknown> = {}
    props.frame.columns.forEach((column, index) => {
      record[column] = row[index]
    })
    return record
  }),
)

const numericColumns = computed(() => {
  const numeric = new Set<string>()
  props.frame.columns.forEach((column, index) => {
    const dtype = props.frame.dtypes[index] ?? ''
    if (/Int|Float|Decimal/.test(dtype)) numeric.add(column)
  })
  return numeric
})

// Only figures get the mono ledger face. Putting whole tables in it — which is
// what the old global `td { font-family: mono }` did — set vendor names and
// status words in a code face and made every table read as noise.
const figureColumns = computed(() => {
  const figures = new Set(numericColumns.value)
  props.frame.columns.forEach((column, index) => {
    const dtype = props.frame.dtypes[index] ?? ''
    if (/Date|Time|Bool/.test(dtype)) figures.add(column)
  })
  return figures
})

function format(value: unknown, column: string): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number' && numericColumns.value.has(column)) {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  }
  return String(value)
}
</script>

<template>
  <DataTable
    :value="records"
    size="small"
    stripedRows
    scrollable
    :scrollHeight="scrollHeight ?? '480px'"
    tableStyle="min-width: 40rem"
  >
    <Column
      v-for="column in frame.columns"
      :key="column"
      :field="column"
      :header="column"
      :class="{ 'num-col': numericColumns.has(column), 'aw-figure': figureColumns.has(column) }"
    >
      <template #body="{ data }">
        <span :class="{ 'cell-null': data[column] === null || data[column] === undefined }">
          {{ format(data[column], column) }}
        </span>
      </template>
    </Column>
  </DataTable>
</template>

<style scoped>
:deep(.num-col) {
  text-align: right;
}

.cell-null::after {
  content: '∅';
  color: var(--aw-border-strong);
}
</style>
