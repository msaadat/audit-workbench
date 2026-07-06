<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PieController,
  ArcElement,
  PointElement,
  Tooltip,
} from 'chart.js'

import type { FramePayload, VizSpec } from '../types'
import FrameTable from './FrameTable.vue'

Chart.register(
  BarController, BarElement, LineController, LineElement, PointElement,
  PieController, ArcElement, CategoryScale, LinearScale, Legend, Tooltip,
)

const PALETTE = [
  '#3b82f6', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6',
  '#14b8a6', '#f97316', '#6366f1', '#ec4899', '#84cc16',
]

const props = defineProps<{
  frame: FramePayload
  viz: VizSpec
  height?: string
}>()

const canvas = ref<HTMLCanvasElement>()
let chart: Chart | null = null

const chartable = computed(
  () =>
    props.viz.type !== 'table' &&
    !!props.viz.x &&
    (props.viz.y?.length ?? 0) > 0 &&
    props.frame.columns.includes(props.viz.x!) &&
    props.viz.y!.some((column) => props.frame.columns.includes(column)),
)

function columnValues(column: string): (string | number | null)[] {
  const index = props.frame.columns.indexOf(column)
  return props.frame.rows.map((row) => row[index] as string | number | null)
}

function render() {
  chart?.destroy()
  chart = null
  if (!canvas.value || !chartable.value) return

  const labels = columnValues(props.viz.x!).map((value) => String(value ?? '∅'))
  const yColumns = props.viz.y!.filter((column) => props.frame.columns.includes(column))
  const type = props.viz.type as 'bar' | 'line' | 'pie'

  const datasets =
    type === 'pie'
      ? [
          {
            label: yColumns[0],
            data: columnValues(yColumns[0]).map((value) => Number(value ?? 0)),
            backgroundColor: labels.map((_, index) => PALETTE[index % PALETTE.length]),
          },
        ]
      : yColumns.map((column, index) => ({
          label: column,
          data: columnValues(column).map((value) => Number(value ?? 0)),
          backgroundColor: PALETTE[index % PALETTE.length],
          borderColor: PALETTE[index % PALETTE.length],
          fill: false,
          tension: 0.25,
          pointRadius: labels.length > 60 ? 0 : 2.5,
        }))

  chart = new Chart(canvas.value, {
    type,
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: type === 'pie' || datasets.length > 1, position: 'bottom' },
      },
      scales:
        type === 'pie'
          ? undefined
          : { x: { ticks: { autoSkip: true, maxRotation: 45 } }, y: { beginAtZero: true } },
    },
  })
}

watch(() => [props.frame, props.viz], render, { deep: true })
watch(canvas, render)
onBeforeUnmount(() => chart?.destroy())
</script>

<template>
  <div v-if="chartable" class="chart-holder" :style="{ height: height ?? '280px' }">
    <canvas ref="canvas" />
  </div>
  <FrameTable v-else :frame="frame" :scrollHeight="height ?? '280px'" />
</template>

<style scoped>
.chart-holder {
  position: relative;
  width: 100%;
}
</style>
