<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'

import { api } from '../../api'
import type { AnalyticsParamMeta, AnalyticsTest, ColumnSchema, WorkspaceSummary } from '../../types'

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

const groups = computed(() => {
  const result: Array<{ name: string; tests: AnalyticsTest[] }> = []
  for (const test of tests.value) {
    const name = test.group || 'Other'
    let group = result.find((item) => item.name === name)
    if (!group) {
      group = { name, tests: [] }
      result.push(group)
    }
    group.tests.push(test)
  }
  return result
})

const ready = computed(() => {
  if (!props.table || !selected.value) return false
  return selected.value.params.every((param) => {
    if (param.optional) return true
    const value = params.value[param.name]
    if (param.kind === 'columns') return Array.isArray(value) && value.length > 0
    return value !== null && value !== undefined && value !== ''
  })
})

function columnOptions(meta: AnalyticsParamMeta): string[] {
  const preferred = meta.column_kind
    ? schema.value.filter((column) => column.kind === meta.column_kind).map((column) => column.name)
    : []
  const rest = schema.value.map((column) => column.name).filter((name) => !preferred.includes(name))
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
    const test = tests.value.find((item) => item.id === spec.value.test_id)
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
watch(ready, (value) => emit('valid', value), { immediate: true })
watch(() => props.table, () => void loadSchema(), { immediate: true })
void loadTests()
</script>

<template>
  <section class="author">
    <div v-if="selected && !pickerOpen" class="selected-test">
      <i :class="selected.icon" />
      <div>
        <strong>{{ selected.label }}</strong>
        <span>{{ selected.description }}</span>
      </div>
      <button type="button" @click="pickerOpen = true">Change test</button>
    </div>

    <div v-if="pickerOpen" class="catalog">
      <template v-for="group in groups" :key="group.name">
        <p>{{ group.name }}</p>
        <div class="test-grid">
          <button
            v-for="test in group.tests"
            :key="test.id"
            type="button"
            :class="{ active: selected?.id === test.id }"
            @click="pick(test)"
          >
            <i :class="test.icon" />
            <strong>{{ test.label }}</strong>
            <span>{{ test.description }}</span>
          </button>
        </div>
      </template>
    </div>

    <div v-if="selected && !pickerOpen" class="parameters">
      <strong>Parameters</strong>
      <div v-if="selected.params.length" class="parameter-grid">
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
      <p v-else class="muted">This test has no parameters.</p>
    </div>
  </section>
</template>

<style scoped>
.author { display:flex; flex-direction:column; gap:.8rem }
.selected-test { display:flex; align-items:center; gap:.75rem; border:1px solid var(--aw-border); border-radius:6px; padding:.7rem .8rem; background:#fff }
.selected-test>i { color:var(--aw-teal); font-size:1.15rem }
.selected-test div { display:flex; flex:1; min-width:0; flex-direction:column; gap:.15rem }
.selected-test span,.muted { color:var(--aw-muted); font-size:.78rem }
.selected-test button { border:0; background:transparent; color:var(--aw-teal); cursor:pointer; font:inherit; font-size:.8rem; font-weight:600 }
.catalog>p { margin:.7rem 0 .4rem; color:var(--aw-muted); font-size:.72rem; font-weight:700; text-transform:uppercase }
.catalog>p:first-child { margin-top:0 }
.test-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:.6rem }
.test-grid button { display:flex; min-height:7rem; flex-direction:column; gap:.35rem; border:1px solid var(--aw-border); border-radius:6px; padding:.8rem; background:#fff; color:inherit; cursor:pointer; font:inherit; text-align:left }
.test-grid button:hover,.test-grid button.active { border-color:var(--aw-teal); box-shadow:inset 0 0 0 1px var(--aw-teal) }
.test-grid i { color:var(--aw-teal) }
.test-grid span { color:var(--aw-muted); font-size:.76rem; line-height:1.35 }
.parameters { display:flex; flex-direction:column; gap:.6rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas) }
.parameter-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:.7rem }
label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }
label small { color:var(--aw-muted); font-weight:400 }
@media(max-width:700px){.test-grid,.parameter-grid{grid-template-columns:1fr}}
</style>
