<script setup lang="ts">
import { computed } from 'vue'

import type { FramePayload } from '../../types'
import { figureColumnSet, formatCell, numericColumnSet } from '../frameFormat'

/**
 * The rows a procedure flagged, as an exhibit in a written memo.
 *
 * Not the interactive table: an exhibit is read, not sorted, and twenty-five
 * columns in the frame's own order put the two the test compared somewhere
 * off to the right. The tested columns come first and are tinted, which is
 * the whole point of the exhibit — every row here breaches them — and the rest
 * follow, scrolling sideways inside the exhibit rather than the page.
 */
const props = defineProps<{ frame: FramePayload; tested: string[] }>()

const columns = computed(() => [
  ...props.tested.filter(column => props.frame.columns.includes(column)),
  ...props.frame.columns.filter(column => !props.tested.includes(column)),
])
const positions = computed(() => columns.value.map(column => props.frame.columns.indexOf(column)))
const numeric = computed(() => numericColumnSet(props.frame))
const figures = computed(() => figureColumnSet(props.frame))
const testedSet = computed(() => new Set(props.tested))
</script>

<template>
  <div class="exhibit-table">
    <table>
      <thead>
        <tr>
          <th
            v-for="column in columns"
            :key="column"
            :class="{ num: numeric.has(column), tested: testedSet.has(column) }"
          >{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, rowIndex) in frame.rows" :key="rowIndex">
          <td
            v-for="(column, index) in columns"
            :key="column"
            :class="{ num: numeric.has(column), figure: figures.has(column), tested: testedSet.has(column) }"
          >{{ formatCell(row[positions[index]], numeric.has(column)) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.exhibit-table { max-height: 18rem; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); }
th {
  position: sticky; top: 0;
  padding: .45rem .625rem;
  border-bottom: 1px solid var(--aw-border-strong);
  background: var(--aw-sheet);
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 650;
  letter-spacing: .03em; text-align: left; text-transform: uppercase; white-space: nowrap;
}
td { padding: .45rem .625rem; border-bottom: 1px solid var(--aw-border); white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
.num { text-align: right; }
.figure { font-family: var(--aw-font-mono); font-variant-numeric: tabular-nums; }
th.tested { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
td.tested { background: color-mix(in srgb, var(--aw-danger-soft) 55%, var(--aw-sheet)); color: var(--aw-danger); font-weight: 600; }

@media print {
  .exhibit-table { max-height: none; overflow: visible; }
  td, th { white-space: normal; }
}
</style>
