<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import SplitButton from 'primevue/splitbutton'

import { api, ApiError } from '../../api'
import type {
  CheckMeta,
  ColumnSchema,
  RuleSet,
  ValidationRule,
  ValidationRun,
  WorkspaceSummary,
} from '../../types'
import CheckDialog from './CheckDialog.vue'
import RunResults from './RunResults.vue'
import ValidationGrid from './ValidationGrid.vue'

// One rule set: a header (title / bound table / run / save), the field-wise
// checks grid, and the results of the latest run. All edits mutate a local
// draft; Save persists the spec. Run always validates the *draft* rules, so
// unsaved edits can be tried before committing.
const props = defineProps<{ workspace: WorkspaceSummary; ruleset: RuleSet | null }>()
const emit = defineEmits<{
  saved: [RuleSet]
  changed: []
  deleted: []
  ran: [string | null, string]
}>()
const toast = useToast()
const confirm = useConfirm()

const title = ref(props.ruleset?.title ?? '')
const table = ref<string | null>(props.ruleset?.table ?? null)
const rules = ref<ValidationRule[]>(
  JSON.parse(JSON.stringify(props.ruleset?.rules ?? [])) as ValidationRule[],
)

const checks = ref<CheckMeta[]>([])
const schema = ref<ColumnSchema[]>([])
const run = ref<ValidationRun | null>(null)
const view = ref<'rules' | 'results'>('rules')
const running = ref(false)
const saving = ref(false)
const exporting = ref(false)

const dialogVisible = ref(false)
const dialogColumn = ref<string | null>(null)
const dialogRule = ref<ValidationRule | null>(null)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const enabledCount = computed(() => rules.value.filter((r) => r.enabled).length)

const dirty = computed(() => {
  if (!props.ruleset) return title.value.trim() !== '' || rules.value.length > 0
  return (
    title.value !== props.ruleset.title ||
    table.value !== props.ruleset.table ||
    JSON.stringify(rules.value) !== JSON.stringify(props.ruleset.rules)
  )
})

const viewOptions = computed(() => [
  { label: 'Rules', value: 'rules', disabled: false },
  { label: 'Results', value: 'results', disabled: !run.value },
])

const runAgainstItems = computed(() =>
  tableOptions.value
    .filter((name) => name !== table.value)
    .map((name) => ({ label: `Run against ${name}`, command: () => runRules(name) })),
)

async function loadChecks() {
  try {
    checks.value = await api.get<CheckMeta[]>('/api/validation/checks')
  } catch (error) {
    fail('Could not load checks', error)
  }
}
loadChecks()

async function loadSchema() {
  if (!table.value) {
    schema.value = []
    return
  }
  try {
    schema.value = (
      await api.get<{ columns: ColumnSchema[] }>(
        `/api/workspaces/${props.workspace.id}/tables/${table.value}/schema`,
      )
    ).columns
  } catch (error) {
    schema.value = []
    fail('Could not load table schema', error)
  }
}
watch(table, loadSchema, { immediate: true })

// Default a new rule set to the first table.
if (!props.ruleset && !table.value && tableOptions.value.length) {
  table.value = tableOptions.value[0]
}

async function runRules(targetTable?: string) {
  const target = targetTable ?? table.value
  if (!target || rules.value.length === 0) return
  running.value = true
  try {
    run.value = await api.post<ValidationRun>(
      `/api/workspaces/${props.workspace.id}/tables/${target}/validate`,
      { rules: rules.value },
    )
    view.value = 'results'
    emit('ran', props.ruleset?.id ?? null, run.value.verdict)
  } catch (error) {
    fail('Validation run failed', error)
  } finally {
    running.value = false
  }
}

async function save() {
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return
  }
  if (!table.value) return
  saving.value = true
  try {
    if (props.ruleset) {
      await api.patch<RuleSet>(
        `/api/workspaces/${props.workspace.id}/rulesets/${props.ruleset.id}`,
        { title: name, table: table.value, rules: rules.value },
      )
      emit('changed')
      toast.add({ severity: 'success', summary: 'Rule set saved', detail: name, life: 2500 })
    } else {
      const created = await api.post<RuleSet>(`/api/workspaces/${props.workspace.id}/rulesets`, {
        title: name,
        table: table.value,
        rules: rules.value,
      })
      emit('saved', created)
      toast.add({ severity: 'success', summary: 'Rule set saved', detail: name, life: 2500 })
    }
  } catch (error) {
    fail('Save failed', error)
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  if (!props.ruleset) return
  confirm.require({
    header: 'Delete rule set',
    message: `Delete "${props.ruleset.title}"?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/rulesets/${props.ruleset!.id}`)
        emit('deleted')
      } catch (error) {
        fail('Delete failed', error)
      }
    },
  })
}

async function exportReport() {
  const target = run.value?.table ?? table.value
  if (!target || rules.value.length === 0) return
  exporting.value = true
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${target}/validate/report`,
      { rules: rules.value },
      `${target}_validation_report.xlsx`,
    )
  } catch (error) {
    fail('Export failed', error)
  } finally {
    exporting.value = false
  }
}

// ------------------------------------------------------------ rule editing
function openAdd(column: string | null) {
  dialogColumn.value = column
  dialogRule.value = null
  dialogVisible.value = true
}
function openEdit(rule: ValidationRule) {
  dialogColumn.value = rule.column
  dialogRule.value = rule
  dialogVisible.value = true
}
function upsertRule(rule: ValidationRule) {
  const index = rules.value.findIndex((r) => r.id === rule.id)
  if (index >= 0) rules.value[index] = rule
  else rules.value.push(rule)
  dialogVisible.value = false
}
function removeRule(ruleId: string) {
  rules.value = rules.value.filter((r) => r.id !== ruleId)
  dialogVisible.value = false
}

// Rebind a rule set after a "Run against…" — makes the override permanent.
function rebind(target: string) {
  table.value = target
  toast.add({
    severity: 'info',
    summary: `Now bound to ${target}`,
    detail: 'Save the rule set to keep the new binding.',
    life: 4000,
  })
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}
</script>

<template>
  <div class="detail-head">
    <InputText v-model="title" placeholder="Rule set title" class="title-input" />
    <Select
      v-model="table"
      :options="tableOptions"
      placeholder="Table"
      style="min-width: 12rem"
      v-tooltip.bottom="'The table these rules are bound to'"
    />
    <span class="grow" />
    <SelectButton
      v-model="view"
      :options="viewOptions"
      optionLabel="label"
      optionValue="value"
      optionDisabled="disabled"
      :allowEmpty="false"
      size="small"
    />
    <SplitButton
      label="Run"
      icon="pi pi-play"
      size="small"
      :model="runAgainstItems"
      :disabled="!table || rules.length === 0"
      :loading="running"
      @click="runRules()"
      v-tooltip.bottom="runAgainstItems.length ? 'Run, or pick another table from the arrow' : ''"
    />
    <Button
      label="Save"
      icon="pi pi-save"
      size="small"
      :disabled="!dirty"
      :loading="saving"
      @click="save"
    />
    <Button
      label="Report"
      icon="pi pi-file-excel"
      severity="secondary"
      size="small"
      :disabled="rules.length === 0"
      :loading="exporting"
      @click="exportReport"
      v-tooltip.bottom="'Export the validation report (Excel)'"
    />
    <Button
      v-if="ruleset"
      icon="pi pi-trash"
      severity="danger"
      text
      size="small"
      v-tooltip.bottom="'Delete rule set'"
      @click="confirmDelete"
    />
  </div>

  <p v-if="dirty" class="muted dirty-hint">
    <i class="pi pi-circle-fill" /> Unsaved changes — Run uses the current draft.
  </p>

  <template v-if="view === 'rules'">
    <p v-if="!table" class="muted">
      Upload data in the Data tab first, then bind this rule set to a table.
    </p>
    <ValidationGrid
      v-else
      :schema="schema"
      :rules="rules"
      :checks="checks"
      @add="openAdd"
      @edit="openEdit"
      @remove="removeRule"
    />
    <p v-if="rules.length" class="muted grid-footer">
      {{ rules.length }} rule{{ rules.length === 1 ? '' : 's' }}
      ({{ enabledCount }} enabled) — click a chip to edit it.
    </p>
  </template>

  <RunResults
    v-else-if="run"
    :workspace="workspace"
    :run="run"
    :rules="rules"
    :boundTable="table"
    @rebind="rebind"
  />

  <CheckDialog
    v-model:visible="dialogVisible"
    :workspace="workspace"
    :table="table"
    :schema="schema"
    :checks="checks"
    :column="dialogColumn"
    :rule="dialogRule"
    @save="upsertRule"
    @remove="removeRule"
  />
</template>

<style scoped>
.detail-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.5rem;
  flex-wrap: wrap;
  position: sticky;
  top: -2px;
  z-index: 5;
  background: var(--p-surface-0);
  padding: 0.4rem 0;
}
.detail-head .grow { flex: 1; }
.title-input { min-width: 16rem; font-weight: 600; }

.dirty-hint {
  margin: 0 0 0.75rem;
  font-size: 0.78rem;
}
.dirty-hint i { font-size: 0.5rem; color: var(--p-amber-500); vertical-align: middle; margin-right: 0.25rem; }

.grid-footer {
  margin-top: 0.6rem;
  font-size: 0.8rem;
}
</style>
