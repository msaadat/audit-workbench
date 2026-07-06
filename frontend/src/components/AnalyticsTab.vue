<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import type {
  AnalyticsParamMeta,
  AnalyticsResult,
  AnalyticsTest,
  ColumnSchema,
  WorkspaceSummary,
} from '../types'
import FrameTable from './FrameTable.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const table = ref<string | null>(null)
const schema = ref<ColumnSchema[]>([])
const tests = ref<AnalyticsTest[]>([])
const selected = ref<AnalyticsTest | null>(null)
const params = ref<Record<string, unknown>>({})
const result = ref<AnalyticsResult | null>(null)
const running = ref(false)
const exporting = ref(false)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))

const verdictSeverity: Record<string, string> = {
  ok: 'success',
  warn: 'warn',
  fail: 'danger',
  info: 'info',
}

async function loadTests() {
  tests.value = await api.get<AnalyticsTest[]>('/api/analytics')
}
loadTests()

async function loadSchema() {
  if (!table.value) return
  schema.value = (
    await api.get<{ columns: ColumnSchema[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/schema`,
    )
  ).columns
  result.value = null
}

watch(table, loadSchema)
watch(
  () => props.workspace.tables.length,
  () => {
    if (!table.value && tableOptions.value.length) table.value = tableOptions.value[0]
  },
  { immediate: true },
)

function columnOptions(meta: AnalyticsParamMeta): string[] {
  // Prefer matching-kind columns but never hide the rest — inference isn't perfect.
  const preferred = meta.column_kind
    ? schema.value.filter((c) => c.kind === meta.column_kind).map((c) => c.name)
    : []
  const rest = schema.value.map((c) => c.name).filter((name) => !preferred.includes(name))
  return [...preferred, ...rest]
}

function pick(test: AnalyticsTest) {
  selected.value = test
  result.value = null
  const defaults: Record<string, unknown> = {}
  for (const param of test.params) {
    if (param.default !== undefined) defaults[param.name] = param.default
    else if (param.kind === 'columns') defaults[param.name] = []
    else defaults[param.name] = null
  }
  params.value = defaults
}

const ready = computed(() => {
  if (!selected.value || !table.value) return false
  return selected.value.params.every((p) => {
    if (p.optional) return true
    const value = params.value[p.name]
    if (p.kind === 'columns') return Array.isArray(value) && value.length > 0
    return value !== null && value !== undefined && value !== ''
  })
})

async function run() {
  if (!selected.value || !table.value) return
  running.value = true
  result.value = null
  try {
    result.value = await api.post<AnalyticsResult>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/analytics/${selected.value.id}`,
      params.value,
    )
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Test failed', detail, life: 7000 })
  } finally {
    running.value = false
  }
}

async function exportExcel() {
  if (!selected.value || !table.value) return
  exporting.value = true
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/analytics/${selected.value.id}/export`,
      params.value,
      `${table.value}_${selected.value.id}.xlsx`,
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
      <Select v-model="table" :options="tableOptions" placeholder="Pick a table" style="min-width: 16rem" />
    </div>
  </div>

  <div class="test-grid">
    <button
      v-for="test in tests"
      :key="test.id"
      class="test-card"
      :class="{ active: selected?.id === test.id }"
      @click="pick(test)"
    >
      <i :class="test.icon" />
      <strong>{{ test.label }}</strong>
      <span>{{ test.description }}</span>
    </button>
  </div>

  <div v-if="selected" class="run-panel">
    <div class="param-form">
      <div v-for="param in selected.params" :key="param.name" class="field">
        <label>{{ param.label }}</label>
        <Select
          v-if="param.kind === 'column'"
          v-model="params[param.name] as string | null"
          :options="columnOptions(param)"
          :showClear="!!param.optional"
          placeholder="Pick a column"
          filter
          style="min-width: 14rem"
        />
        <MultiSelect
          v-else-if="param.kind === 'columns'"
          v-model="params[param.name] as string[]"
          :options="columnOptions(param)"
          display="chip"
          filter
          placeholder="Pick columns"
          style="min-width: 16rem"
        />
        <Select
          v-else-if="param.kind === 'select'"
          v-model="params[param.name] as string | number | null"
          :options="param.options"
          optionLabel="label"
          optionValue="value"
          style="min-width: 12rem"
        />
        <InputNumber
          v-else-if="param.kind === 'number'"
          v-model="params[param.name] as number"
          :useGrouping="false"
          style="width: 8rem"
        />
      </div>
      <Button label="Run test" icon="pi pi-play" :disabled="!ready" :loading="running" @click="run" />
    </div>
  </div>

  <div v-if="result" class="result">
    <div class="result-head">
      <h3>{{ result.title }}</h3>
      <Tag :value="result.verdict_text || result.verdict" :severity="verdictSeverity[result.verdict]" />
      <Button
        label="Export"
        icon="pi pi-file-excel"
        severity="secondary"
        size="small"
        :loading="exporting"
        @click="exportExcel"
      />
    </div>

    <div class="stat-cards" style="margin: 0.75rem 0 1rem">
      <div v-for="stat in result.stats" :key="stat.label" class="stat-card">
        <div class="label">{{ stat.label }}</div>
        <div class="value">{{ stat.value }}</div>
      </div>
    </div>

    <template v-if="result.summary">
      <h4>Summary <span class="muted" v-if="result.summary_rows > result.summary.rows.length">(first {{ result.summary.rows.length }} of {{ result.summary_rows.toLocaleString() }})</span></h4>
      <FrameTable :frame="result.summary" scrollHeight="40vh" />
    </template>

    <template v-if="result.detail">
      <h4>
        Detail rows
        <span class="muted" v-if="result.detail_rows > result.detail.rows.length">
          (previewing {{ result.detail.rows.length }} of {{ result.detail_rows.toLocaleString() }} — export for all)
        </span>
      </h4>
      <FrameTable :frame="result.detail" scrollHeight="40vh" />
    </template>
  </div>
</template>

<style scoped>
.test-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.test-card {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  text-align: left;
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.9rem 1rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.test-card:hover {
  border-color: var(--p-primary-300);
}

.test-card.active {
  border-color: var(--p-primary-500);
  box-shadow: 0 0 0 1px var(--p-primary-500);
}

.test-card i {
  color: var(--p-primary-500);
  font-size: 1.1rem;
}

.test-card span {
  font-size: 0.8rem;
  color: var(--p-surface-500);
  line-height: 1.35;
}

.run-panel {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
}

.param-form {
  display: flex;
  flex-wrap: wrap;
  gap: 0.9rem;
  align-items: flex-end;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.result-head h3 {
  margin: 0;
}

h4 {
  margin: 1rem 0 0.5rem;
}
</style>
