<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AutoComplete from 'primevue/autocomplete'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'

import { api } from '../../api'
import type {
  CheckMeta,
  CheckParamMeta,
  ColumnSchema,
  ColumnValues,
  ValidationRule,
  WorkspaceSummary,
} from '../../types'
import { newRuleId } from './rules'
import { plural } from '../../format'

// Add/edit one check: pick a check type applicable to the field's kind, fill
// its metadata-driven param form, choose severity (fail blocks, warn flags)
// and whether the rule is enabled. For allowed-values, the picker offers the
// column's actual distinct values while still accepting typed entries (type
// a value and press Enter) — policy lists don't have to exist in the data.
const props = defineProps<{
  workspace: WorkspaceSummary
  table: string | null
  schema: ColumnSchema[]
  checks: CheckMeta[]
  column: string | null
  rule: ValidationRule | null
}>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ save: [ValidationRule]; remove: [string] }>()

const selected = ref<CheckMeta | null>(null)
const params = ref<Record<string, unknown>>({})
const severity = ref<'fail' | 'warn'>('fail')
const enabled = ref(true)

const columnValues = ref<ColumnValues | null>(null)
const valueSuggestions = ref<string[]>([])

const severityOptions = [
  { label: 'Fail', value: 'fail' },
  { label: 'Warn', value: 'warn' },
]

const columnKind = computed(
  () => props.schema.find((c) => c.name === props.column)?.kind ?? 'text',
)

const applicable = computed(() =>
  props.column
    ? props.checks.filter(
        (c) => c.scope === 'column' && c.column_kinds.includes(columnKind.value),
      )
    : props.checks.filter((c) => c.scope === 'table'),
)

const columnOptions = computed(() => props.schema.map((c) => c.name))
// For single-column params (compare/conditional): the rule's own column is
// not a meaningful counterpart.
const otherColumnOptions = computed(() =>
  props.schema.map((c) => c.name).filter((name) => name !== props.column),
)
const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))

// Columns of the lookup table chosen for a referential check.
const lookupColumns = ref<string[]>([])
watch(
  () => params.value.lookup_table,
  async (table) => {
    lookupColumns.value = []
    if (!table) return
    try {
      lookupColumns.value = (
        await api.get<{ columns: ColumnSchema[] }>(
          `/api/workspaces/${props.workspace.id}/tables/${table}/schema`,
        )
      ).columns.map((c) => c.name)
    } catch {
      // Leave the picker empty; save-time validation reports the problem.
    }
  },
)

// At least one bound/flag must be set for checks whose params are all optional.
const ready = computed(() => {
  if (!selected.value) return false
  const filled = (value: unknown) =>
    value !== null && value !== undefined && value !== '' &&
    (!Array.isArray(value) || value.length > 0)
  const required = selected.value.params.filter((p) => !p.optional)
  if (!required.every((p) => filled(params.value[p.name]))) return false
  const optionals = selected.value.params.filter((p) => p.optional || p.kind === 'toggle')
  if (required.length === 0 && selected.value.params.length > 0) {
    return optionals.some((p) => filled(params.value[p.name]) && params.value[p.name] !== false)
  }
  return true
})

function pick(check: CheckMeta) {
  selected.value = check
  const defaults: Record<string, unknown> = {}
  for (const param of check.params) {
    if (param.default !== undefined) defaults[param.name] = param.default
    else if (param.kind === 'columns' || param.kind === 'values') defaults[param.name] = []
    else if (param.kind === 'toggle') defaults[param.name] = false
    else defaults[param.name] = null
  }
  params.value = defaults
  if (check.params.some((p) => p.kind === 'values')) loadColumnValues()
}

async function loadColumnValues() {
  columnValues.value = null
  if (!props.table || !props.column) return
  try {
    columnValues.value = await api.get<ColumnValues>(
      `/api/workspaces/${props.workspace.id}/tables/${props.table}/columns/${encodeURIComponent(props.column)}/values`,
    )
  } catch {
    // Values are a convenience — typed entry still works without them.
    columnValues.value = { distinct: 0, truncated: false, values: [] }
  }
}

function filterValues(event: { query: string }) {
  const all = columnValues.value?.values ?? []
  const query = event.query.trim().toLowerCase()
  valueSuggestions.value = query
    ? all.filter((v) => v.toLowerCase().includes(query))
    : [...all]
}

function save() {
  if (!selected.value || !ready.value) return
  emit('save', {
    id: props.rule?.id ?? newRuleId(),
    column: selected.value.scope === 'column' ? props.column : null,
    check: selected.value.id,
    params: { ...params.value },
    severity: severity.value,
    enabled: enabled.value,
  })
}

function inputWidth(param: CheckParamMeta): string {
  return param.kind === 'number' ? '8rem' : param.kind === 'date' ? '10rem' : '100%'
}

watch(visible, (open) => {
  if (!open) return
  selected.value = null
  params.value = {}
  severity.value = props.rule?.severity ?? 'fail'
  enabled.value = props.rule?.enabled !== false
  columnValues.value = null
  if (props.rule) {
    const meta = props.checks.find((c) => c.id === props.rule!.check)
    if (meta) {
      pick(meta)
      params.value = { ...params.value, ...JSON.parse(JSON.stringify(props.rule.params ?? {})) }
    }
  }
})
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    :header="column ? `Check for ${column}` : 'Table-level check'"
    :style="{ width: '34rem', maxWidth: '95vw' }"
  >
    <!-- Check picker (only when adding; editing keeps the type fixed) -->
    <div v-if="!rule" class="check-list" :class="{ compact: !!selected }">
      <button
        v-for="check in applicable"
        :key="check.id"
        class="check-card"
        :class="{ active: selected?.id === check.id }"
        @click="pick(check)"
      >
        <i :class="check.icon" />
        <div>
          <strong>{{ check.label }}</strong>
          <span v-if="!selected">{{ check.description }}</span>
        </div>
      </button>
    </div>
    <div v-else-if="selected" class="edit-strip">
      <i :class="selected.icon" />
      <div>
        <strong>{{ selected.label }}</strong>
        <span>{{ selected.description }}</span>
      </div>
    </div>

    <template v-if="selected">
      <div class="param-form">
        <div v-for="param in selected.params" :key="param.name" class="field">
          <label v-if="param.kind !== 'toggle'">{{ param.label }}<span v-if="param.optional" class="muted"> (optional)</span></label>

          <InputNumber
            v-if="param.kind === 'number'"
            v-model="params[param.name] as number"
            :useGrouping="false"
            :maxFractionDigits="6"
            :style="{ width: inputWidth(param) }"
          />
          <Select
            v-else-if="param.kind === 'select'"
            v-model="params[param.name] as string | number | null"
            :options="param.options"
            optionLabel="label"
            optionValue="value"
            style="width: 100%"
          />
          <InputText
            v-else-if="param.kind === 'text'"
            v-model="params[param.name] as string"
            style="width: 100%"
          />
          <InputText
            v-else-if="param.kind === 'date'"
            v-model="params[param.name] as string"
            placeholder="YYYY-MM-DD"
            :style="{ width: inputWidth(param) }"
          />
          <label v-else-if="param.kind === 'toggle'" class="toggle-line">
            <Checkbox v-model="params[param.name] as boolean" binary />
            <span>{{ param.label }}</span>
          </label>
          <Select
            v-else-if="param.kind === 'column'"
            v-model="params[param.name] as string | null"
            :options="otherColumnOptions"
            placeholder="Pick a column"
            filter
            style="width: 100%"
          />
          <Select
            v-else-if="param.kind === 'table'"
            v-model="params[param.name] as string | null"
            :options="tableOptions"
            placeholder="Pick a table"
            style="width: 100%"
          />
          <Select
            v-else-if="param.kind === 'lookup_column'"
            v-model="params[param.name] as string | null"
            :options="lookupColumns"
            :placeholder="params.lookup_table ? 'Pick a column' : 'Pick the lookup table first'"
            :disabled="!params.lookup_table"
            filter
            style="width: 100%"
          />
          <template v-else-if="param.kind === 'code'">
            <Textarea
              v-model="params[param.name] as string"
              rows="3"
              class="code-input"
              placeholder='pl.col("qty") * pl.col("price") != pl.col("total")'
            />
            <small class="muted">
              A Polars expression that is <strong>True for violating rows</strong>;
              <code>pl</code> is available.
            </small>
          </template>
          <MultiSelect
            v-else-if="param.kind === 'columns'"
            v-model="params[param.name] as string[]"
            :options="columnOptions"
            display="chip"
            filter
            placeholder="Pick columns"
            style="width: 100%"
          />
          <template v-else-if="param.kind === 'values'">
            <AutoComplete
              v-model="params[param.name] as string[]"
              multiple
              dropdown
              :typeahead="true"
              :suggestions="valueSuggestions"
              @complete="filterValues"
              placeholder="Pick or type values, Enter to add"
              style="width: 100%"
            />
            <small v-if="columnValues" class="muted">
              {{ plural(columnValues.distinct, 'distinct value') }} in the current data<span
                v-if="columnValues.truncated"
              >
                — showing the first {{ columnValues.values.length }}</span
              >.
            </small>
          </template>
        </div>
      </div>

      <div class="rule-controls">
        <div class="field">
          <label>On violation</label>
          <SelectButton
            v-model="severity"
            :options="severityOptions"
            optionLabel="label"
            optionValue="value"
            :allowEmpty="false"
            size="small"
          />
        </div>
        <div class="field">
          <label>Enabled</label>
          <ToggleSwitch v-model="enabled" />
        </div>
      </div>
    </template>

    <template #footer>
      <Button
        v-if="rule"
        label="Delete"
        icon="pi pi-trash"
        severity="danger"
        text
        size="small"
        @click="emit('remove', rule.id)"
      />
      <span class="grow" />
      <Button label="Cancel" severity="secondary" outlined size="small" @click="visible = false" />
      <Button
        :label="rule ? 'Save check' : 'Add check'"
        icon="pi pi-check"
        size="small"
        :disabled="!ready"
        @click="save"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.check-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.check-list.compact { grid-template-columns: repeat(3, 1fr); }

.check-card {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  text-align: left;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.55rem 0.7rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.check-card:hover { border-color: var(--aw-teal-line); }
.check-card.active { border-color: var(--aw-teal-600); box-shadow: 0 0 0 1px var(--aw-teal-600); }
.check-card i { color: var(--aw-teal-600); margin-top: 0.15rem; }
.check-card div { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
.check-card strong { font-size: var(--aw-text-base); }
.check-card span { font-size: var(--aw-text-xs); color: var(--aw-muted); line-height: 1.3; }

.edit-strip {
  display: flex;
  align-items: flex-start;
  gap: 0.6rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
  margin-bottom: 0.75rem;
}
.edit-strip i { color: var(--aw-teal-600); margin-top: 0.15rem; }
.edit-strip div { display: flex; flex-direction: column; gap: 0.15rem; }
.edit-strip span { font-size: var(--aw-text-sm); color: var(--aw-muted); }

.param-form {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  margin-bottom: 1rem;
}

.toggle-line {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: var(--aw-text-base);
  cursor: pointer;
}

.code-input {
  width: 100%;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: var(--aw-text-base);
}

.rule-controls {
  display: flex;
  gap: 2rem;
  align-items: flex-end;
  border-top: 1px solid var(--aw-border);
  padding-top: 0.9rem;
}

.grow { flex: 1; }
</style>
