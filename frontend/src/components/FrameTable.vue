<script setup lang="ts">
import { computed, ref } from 'vue'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import type { FramePayload } from '../types'

const props = defineProps<{
  frame: FramePayload
  scrollHeight?: string
  /** Columns to render, in order. Omit to render the whole frame. */
  visibleColumns?: string[]
  /** Headers for columns whose frame name is not what to call them. */
  columnLabels?: Record<string, string>
  /** Columns to render nowhere — not as a column, not in an expanded row. */
  hiddenColumns?: string[]
  /** Let a row open to the fields `visibleColumns` left out. */
  expandable?: boolean
}>()

const ROW_KEY = '_frameRow'

// DataTable wants row objects; the API sends compact row arrays.
const records = computed(() =>
  props.frame.rows.map((row, position) => {
    const record: Record<string, unknown> = { [ROW_KEY]: position }
    props.frame.columns.forEach((column, index) => {
      record[column] = row[index]
    })
    return record
  }),
)

const shown = computed(() =>
  (props.visibleColumns ?? props.frame.columns).filter(column =>
    props.frame.columns.includes(column),
  ),
)
// What a row opens to reveal: the fields the narrowed view left behind.
const withheld = computed(() =>
  props.frame.columns.filter(
    column =>
      !shown.value.includes(column) && !(props.hiddenColumns ?? []).includes(column),
  ),
)
const expanded = ref<Record<string, unknown>[]>([])

// An identifier that happens to be stored as an integer is not a quantity:
// grouping its digits invents a value ("1,020") and right-aligning it sorts it
// with the money. Read the name, not the dtype.
const IDENTIFIER_NAME = /(^|_)(ID|NO|NUM|NUMBER|KEY|CODE|REF)$/i

const numericColumns = computed(() => {
  const numeric = new Set<string>()
  props.frame.columns.forEach((column, index) => {
    const dtype = props.frame.dtypes[index] ?? ''
    if (/Int|Float|Decimal/.test(dtype) && !IDENTIFIER_NAME.test(column)) numeric.add(column)
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
    v-model:expandedRows="expanded"
    :value="records"
    :dataKey="ROW_KEY"
    size="small"
    stripedRows
    scrollable
    :scrollHeight="scrollHeight ?? '480px'"
    :tableStyle="visibleColumns ? undefined : 'min-width: 40rem'"
  >
    <Column v-if="expandable && withheld.length" expander class="expander-col" />
    <Column
      v-for="column in shown"
      :key="column"
      :field="column"
      :header="columnLabels?.[column] ?? column"
      :class="{ 'num-col': numericColumns.has(column), 'aw-figure': figureColumns.has(column) }"
    >
      <template #body="{ data }">
        <span :class="{ 'cell-null': data[column] === null || data[column] === undefined }">
          {{ format(data[column], column) }}
        </span>
      </template>
    </Column>

    <template v-if="expandable && withheld.length" #expansion="{ data }">
      <dl class="withheld">
        <template v-for="column in withheld" :key="column">
          <dt>{{ columnLabels?.[column] ?? column }}</dt>
          <dd :class="{ 'cell-null': data[column] === null || data[column] === undefined }">
            {{ format(data[column], column) }}
          </dd>
        </template>
      </dl>
    </template>
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

:deep(.expander-col) { width: 2.5rem; }

/* The full record, for the one row the auditor asked to see it for. */
.withheld {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 0.2rem 0.9rem;
  margin: 0;
  padding: 0.35rem 0.2rem;
  font-size: var(--aw-text-xs);
}
.withheld dt { color: var(--aw-muted); font-weight: 600; }
.withheld dd { margin: 0; overflow-wrap: anywhere; }
</style>
