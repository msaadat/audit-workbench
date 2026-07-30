<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InputNumber from 'primevue/inputnumber'
import Message from 'primevue/message'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'

import { api } from '../../api'
import type { AnalyticsParamMeta, AnalyticsTest, ColumnSchema, WorkspaceSummary } from '../../types'
import AnalyticsCatalog from './AnalyticsCatalog.vue'

const props = defineProps<{
  workspace: WorkspaceSummary
  table: string | null
}>()
const spec = defineModel<{ test_id: string; params: Record<string, unknown> }>({ required: true })
const emit = defineEmits<{
  valid: [boolean]
  selected: [title: string, objective: string]
  error: [summary: string, error: unknown]
}>()

const tests = ref<AnalyticsTest[]>([])
const schema = ref<ColumnSchema[]>([])
const selected = ref<AnalyticsTest | null>(null)
const params = ref<Record<string, unknown>>({})
const pickerOpen = ref(true)

const ready = computed(() => {
  if (!props.table || !selected.value) return false
  return selected.value.params.every(param => {
    if (param.optional) return true
    const value = params.value[param.name]
    if (param.kind === 'columns') return Array.isArray(value) && value.length > 0
    return value !== null && value !== undefined && value !== ''
  })
})
const missingParams = computed(() => {
  if (!selected.value) return []
  return selected.value.params
    .filter(param => {
      if (param.optional) return false
      const value = params.value[param.name]
      if (param.kind === 'columns') return !Array.isArray(value) || !value.length
      return value === null || value === undefined || value === ''
    })
    .map(param => param.label)
})

function columnOptions(meta: AnalyticsParamMeta): string[] {
  const preferred = meta.column_kind
    ? schema.value.filter(column => column.kind === meta.column_kind).map(column => column.name)
    : []
  const rest = schema.value.map(column => column.name).filter(name => !preferred.includes(name))
  return [...preferred, ...rest]
}

function pick(test: AnalyticsTest) {
  selected.value = test
  pickerOpen.value = false
  const defaults: Record<string, unknown> = {}
  for (const param of test.params) {
    if (param.default !== undefined) defaults[param.name] = param.default
    else if (param.kind === 'columns') defaults[param.name] = []
    else defaults[param.name] = null
  }
  params.value = defaults
  emit('selected', test.label, test.description)
}

async function loadTests() {
  try {
    tests.value = await api.get<AnalyticsTest[]>('/api/analytics')
    const test = tests.value.find(item => item.id === spec.value.test_id)
    if (test) {
      selected.value = test
      params.value = JSON.parse(JSON.stringify(spec.value.params ?? {})) as Record<string, unknown>
      pickerOpen.value = false
    }
  } catch (error) {
    emit('error', 'Could not load the analytics library', error)
  }
}

async function loadSchema() {
  schema.value = []
  if (!props.table) return
  try {
    schema.value = (
      await api.get<{ columns: ColumnSchema[] }>(
        `/api/workspaces/${props.workspace.id}/tables/${props.table}/schema`,
      )
    ).columns
  } catch (error) {
    emit('error', 'Could not load the table schema', error)
  }
}

watch(
  [selected, params],
  () => {
    spec.value = {
      test_id: selected.value?.id ?? '',
      params: { ...params.value },
    }
  },
  { deep: true },
)
watch(ready, value => emit('valid', value), { immediate: true })
watch(() => props.table, () => void loadSchema(), { immediate: true })
void loadTests()
</script>

<template>
  <section class="author">
    <AnalyticsCatalog
      v-if="pickerOpen"
      :tests="tests"
      :selectedId="selected?.id ?? null"
      @select="pick"
    />

    <template v-else-if="selected">
      <!-- The picked analytic is stated once. The title and objective it fills
           in live under Details, so the same sentence is not repeated three
           times on one screen. -->
      <div class="selected">
        <i :class="selected.icon" />
        <div>
          <strong>{{ selected.label }}</strong>
          <span>{{ selected.description }}</span>
        </div>
        <button type="button" @click="pickerOpen = true">Change</button>
      </div>

      <div class="parameters">
        <p class="parameters-head">Parameters</p>
        <Message v-if="!table" severity="warn" :closable="false">
          Pick a table before setting parameters — the column pickers read its schema.
        </Message>
        <div v-else-if="selected.params.length" class="parameter-grid">
          <label v-for="param in selected.params" :key="param.name">
            {{ param.label }}<small v-if="param.optional">Optional</small>
            <Select
              v-if="param.kind === 'column'"
              v-model="params[param.name] as string | null"
              :options="columnOptions(param)"
              :showClear="!!param.optional"
              placeholder="Pick a column"
              filter
            />
            <MultiSelect
              v-else-if="param.kind === 'columns'"
              v-model="params[param.name] as string[]"
              :options="columnOptions(param)"
              display="chip"
              placeholder="Pick columns"
              filter
            />
            <Select
              v-else-if="param.kind === 'select'"
              v-model="params[param.name] as string | number | null"
              :options="param.options"
              optionLabel="label"
              optionValue="value"
            />
            <InputNumber
              v-else-if="param.kind === 'number'"
              v-model="params[param.name] as number"
              :useGrouping="false"
            />
          </label>
        </div>
        <p v-else class="muted">This analytic takes no parameters.</p>
        <p v-if="table && missingParams.length" class="missing">
          Still needed: {{ missingParams.join(', ') }}
        </p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.author { display: flex; flex-direction: column; gap: 0.7rem; min-width: 0; }
.selected { display: flex; align-items: center; gap: 0.7rem; padding: 0.65rem 0.75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: #fff; }
.selected > i { color: var(--aw-teal); font-size: 1.1rem; }
.selected div { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 0.1rem; }
.selected span, .muted { color: var(--aw-muted); font-size: 0.78rem; }
.selected button { border: 0; background: transparent; color: var(--aw-teal); cursor: pointer; font: inherit; font-size: 0.8rem; font-weight: 600; }
.parameters { display: flex; flex-direction: column; gap: 0.55rem; padding: 0.8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: var(--aw-canvas); }
.parameters-head { margin: 0; color: var(--aw-muted); font-size: 0.68rem; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; }
.parameter-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: 0.7rem; }
label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: #46576d; font-size: 0.75rem; font-weight: 600; }
label small { color: var(--aw-muted); font-weight: 400; }
label :deep(.p-select), label :deep(.p-multiselect), label :deep(.p-inputnumber), label :deep(.p-inputtext) { width: 100%; min-width: 0; }
.missing { margin: 0; color: var(--aw-warn); font-size: 0.75rem; }
</style>
