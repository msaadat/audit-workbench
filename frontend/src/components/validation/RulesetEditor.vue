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
  RuleSuggestion,
  RunSummary,
  ValidationRule,
  ValidationRun,
  WorkspaceSummary,
} from '../../types'
import PinDialog from '../PinDialog.vue'
import CheckDialog from './CheckDialog.vue'
import RunResults from './RunResults.vue'
import ValidationGrid from './ValidationGrid.vue'
import { newRuleId } from './rules'

// One rule set: a header (title / bound table / run / save), the field-wise
// checks grid, and the results of the latest run. All edits mutate a local
// draft; Save persists the spec. Run always validates the *draft* rules, so
// unsaved edits can be tried before committing.
const props = defineProps<{ workspace: WorkspaceSummary; ruleset: RuleSet | null; defaultTable?: string }>()
const emit = defineEmits<{
  saved: [RuleSet]
  changed: []
  deleted: []
  ran: [string | null, string]
}>()
const toast = useToast()
const confirm = useConfirm()

const title = ref(props.ruleset?.title ?? '')
const table = ref<string | null>(props.ruleset?.table ?? props.defaultTable ?? null)
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

const showPin = ref(false)
const pinning = ref(false)
// History arrives with saved-spec runs; seed it from the stored rule set.
const history = ref<RunSummary[]>([...(props.ruleset?.runs ?? [])])
// Ghost suggestions derived from the table profile — accepted or dismissed
// one by one, never silently added: the auditor owns the rule set.
const suggestions = ref<ValidationRule[]>([])
const suggesting = ref(false)

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))
const enabledCount = computed(() => rules.value.filter((r) => r.enabled).length)

const dirty = computed(() => {
  if (!props.ruleset) return title.value.trim() !== '' || rules.value.length > 0
  return (
    title.value !== props.ruleset.title ||
    table.value !== props.ruleset.table ||
    rulesDirty.value
  )
})

// Rules-only dirtiness decides which run endpoint to use: the saved spec runs
// through /rulesets/{id}/run (recorded in history), a draft runs statelessly.
const rulesDirty = computed(
  () =>
    !props.ruleset || JSON.stringify(rules.value) !== JSON.stringify(props.ruleset.rules),
)

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
// Suggestions are profiled from one table — a rebind invalidates them.
watch(table, () => {
  suggestions.value = []
})

// Default a new rule set to the first table.
if (!props.ruleset && !table.value && tableOptions.value.length) {
  table.value = tableOptions.value[0]
}

async function runRules(targetTable?: string) {
  const target = targetTable ?? table.value
  if (!target || rules.value.length === 0) return
  running.value = true
  try {
    if (props.ruleset && !rulesDirty.value) {
      run.value = await api.post<ValidationRun>(
        `/api/workspaces/${props.workspace.id}/rulesets/${props.ruleset.id}/run`,
        { table: target },
      )
      if (run.value.history) history.value = [...run.value.history]
    } else {
      run.value = await api.post<ValidationRun>(
        `/api/workspaces/${props.workspace.id}/tables/${target}/validate`,
        { rules: rules.value },
      )
    }
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

async function pinTile({ title: pinTitle, note }: { title: string; note: string }) {
  if (!table.value || rules.value.length === 0) return
  pinning.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/tiles`, {
      kind: 'validation',
      table: table.value,
      title: pinTitle,
      note,
      spec: { rules: rules.value },
    })
    showPin.value = false
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', detail: pinTitle, life: 3000 })
  } catch (error) {
    fail('Pin failed', error)
  } finally {
    pinning.value = false
  }
}

// ------------------------------------------------------------- suggestions
// Conservative, profile-driven proposals: only what the current data already
// satisfies (so accepting them starts green and guards future refreshes).
// The logic lives in the backend (shared with agent runs); this just maps
// the response onto ghost chips.
async function suggest() {
  if (!table.value) return
  suggesting.value = true
  try {
    const response = await api.get<{ suggestions: RuleSuggestion[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/suggest-rules`,
    )
    const have = new Set(rules.value.map((r) => `${r.column}|${r.check}`))
    for (const s of suggestions.value) have.add(`${s.column}|${s.check}`)
    const ghosts: ValidationRule[] = []
    for (const proposal of response.suggestions) {
      const key = `${proposal.column}|${proposal.check}`
      if (have.has(key)) continue
      have.add(key)
      ghosts.push({
        id: newRuleId(),
        column: proposal.column,
        check: proposal.check,
        params: proposal.params,
        severity: proposal.severity,
        enabled: true,
      })
    }
    suggestions.value = [...suggestions.value, ...ghosts]
    if (!ghosts.length) {
      toast.add({
        severity: 'info',
        summary: 'No new suggestions',
        detail: 'Every check the profile supports is already covered.',
        life: 3500,
      })
    }
  } catch (error) {
    fail('Could not build suggestions', error)
  } finally {
    suggesting.value = false
  }
}

function acceptSuggestion(rule: ValidationRule) {
  suggestions.value = suggestions.value.filter((s) => s.id !== rule.id)
  rules.value.push(rule)
}
function dismissSuggestion(ruleId: string) {
  suggestions.value = suggestions.value.filter((s) => s.id !== ruleId)
}
function acceptAllSuggestions() {
  rules.value.push(...suggestions.value)
  suggestions.value = []
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
      label="Pin"
      icon="pi pi-thumbtack"
      severity="secondary"
      size="small"
      :disabled="rules.length === 0 || !table"
      @click="showPin = true"
      v-tooltip.bottom="'Pin a live validation tile to the dashboard'"
    />
    <Button
      label="Report"
      icon="pi pi-file-excel"
      severity="secondary"
      size="small"
      :disabled="rules.length === 0"
      :loading="exporting"
      @click="exportReport"
      v-tooltip.bottom="'Export the validation report (Excel, summary + failing rows)'"
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
    <template v-else>
      <div class="suggest-bar">
        <Button
          label="Suggest checks"
          icon="pi pi-lightbulb"
          severity="secondary"
          outlined
          size="small"
          :loading="suggesting"
          @click="suggest"
          v-tooltip.bottom="'Propose checks the current data already satisfies'"
        />
        <template v-if="suggestions.length">
          <span class="muted suggest-hint">
            {{ suggestions.length }} suggestion{{ suggestions.length === 1 ? '' : 's' }} —
            accept ✓ or dismiss ✕ each dashed chip.
          </span>
          <Button label="Accept all" text size="small" @click="acceptAllSuggestions" />
          <Button label="Dismiss all" text size="small" severity="secondary" @click="suggestions = []" />
        </template>
      </div>
      <ValidationGrid
        :schema="schema"
        :rules="rules"
        :suggestions="suggestions"
        :checks="checks"
        @add="openAdd"
        @edit="openEdit"
        @remove="removeRule"
        @accept="acceptSuggestion"
        @dismiss="dismissSuggestion"
      />
      <p v-if="rules.length" class="muted grid-footer">
        {{ rules.length }} rule{{ rules.length === 1 ? '' : 's' }}
        ({{ enabledCount }} enabled) — click a chip to edit it.
      </p>
    </template>
  </template>

  <RunResults
    v-else-if="run"
    :workspace="workspace"
    :run="run"
    :rules="rules"
    :boundTable="table"
    :history="history"
    @rebind="rebind"
  />

  <PinDialog
    v-model:visible="showPin"
    :defaultTitle="title || 'Validation rules'"
    :saving="pinning"
    @pin="pinTile"
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
  background: var(--aw-panel);
  padding: 0.4rem 0;
}
.detail-head .grow { flex: 1; }
.title-input { min-width: 16rem; font-weight: 600; }

.dirty-hint {
  margin: 0 0 0.75rem;
  font-size: var(--aw-text-sm);
}
.dirty-hint i { font-size: var(--aw-text-2xs); color: var(--aw-warn); vertical-align: middle; margin-right: 0.25rem; }

.grid-footer {
  margin-top: 0.6rem;
  font-size: var(--aw-text-sm);
}

.suggest-bar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.6rem;
  flex-wrap: wrap;
}
.suggest-hint { font-size: var(--aw-text-sm); }
</style>
