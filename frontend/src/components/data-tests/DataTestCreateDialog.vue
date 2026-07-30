<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type { DataTestEngine, DataTestStep, PlanningPayload, WorkspaceSummary } from '../../types'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'
import AnalyticsTestAuthor from './AnalyticsTestAuthor.vue'
import PolarsStepEditor from './PolarsStepEditor.vue'
import { emptyPolarsStep, missingStepFields, polarsStepsValid } from './steps'

const props = defineProps<{
  workspace: WorkspaceSummary
  planning: PlanningPayload | null
  initialRcmId?: string
  initialEngine?: DataTestEngine
  saving: boolean
}>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  create: [payload: {
    title: string; objective: string; engine: DataTestEngine
    rcm_id: string; table_refs: string[]; spec: Record<string, unknown>
  }]
  error: [summary: string, error: unknown]
}>()

const engine = ref<DataTestEngine>('analytics')
const title = ref('')
const objective = ref('')
const rcmId = ref('')
const table = ref('')
const session = ref(0)
const analyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const analyticsReady = ref(false)
const polarsSteps = ref<DataTestStep[]>([emptyPolarsStep()])

const tableOptions = computed(() => props.workspace.tables.map(item => ({ label: item.name, value: item.name })))
const rcmOptions = computed(() => (props.planning?.rcm ?? []).map(row => ({
  label: `${row.id} · ${row.risk}`,
  value: row.id,
})))
const definitionReady = computed(() => engine.value === 'analytics'
  ? analyticsReady.value
  : polarsStepsValid(polarsSteps.value))
const ready = computed(() => Boolean(
  title.value.trim() && objective.value.trim() && definitionReady.value
  && (engine.value === 'polars' || table.value),
))
const blocker = computed(() => {
  if (engine.value === 'analytics' && !analyticsSpec.value.test_id) return 'Pick an analytic from the library.'
  if (engine.value !== 'polars' && !table.value) return 'Pick the table to run against.'
  if (engine.value === 'polars' && !polarsStepsValid(polarsSteps.value)) {
    return `Complete ${missingStepFields(polarsSteps.value).join('; ')}.`
  }
  if (engine.value === 'analytics' && !analyticsReady.value) return 'Set the remaining parameters.'
  if (!title.value.trim() || !objective.value.trim()) return 'Add a title and objective under Details.'
  return ''
})

function reset() {
  engine.value = props.initialEngine ?? 'analytics'
  title.value = ''
  objective.value = ''
  rcmId.value = props.initialRcmId ?? ''
  table.value = props.workspace.tables[0]?.name ?? ''
  analyticsSpec.value = { test_id: '', params: {} }
  analyticsReady.value = false
  polarsSteps.value = [emptyPolarsStep()]
  session.value += 1
}
watch(visible, open => { if (open) reset() })

// The analytic names itself; the auditor only overrides if they want to.
function applyDefaults(label: string, description: string) {
  if (!title.value.trim()) title.value = label
  if (!objective.value.trim()) objective.value = description
}
function submit() {
  emit('create', {
    title: title.value.trim(),
    objective: objective.value.trim(),
    engine: engine.value,
    rcm_id: rcmId.value,
    table_refs: engine.value === 'polars' ? (table.value ? [table.value] : []) : [table.value],
    spec: engine.value === 'analytics'
      ? analyticsSpec.value
      : { schema_version: 2, steps: polarsSteps.value },
  })
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    header="New data test"
    :style="{ width: 'min(56rem, 95vw)' }"
    :contentStyle="{ maxHeight: '78vh', overflow: 'auto' }"
  >
    <div class="body">
      <!-- What to run comes first. Asking for a title before the analytic was
           picked meant naming something that had not been chosen. -->
      <AnalyticsTestAuthor
        v-if="engine === 'analytics'"
        :key="`analytics-${session}`"
        v-model="analyticsSpec"
        :workspace="workspace"
        :table="table || null"
        @valid="analyticsReady = $event"
        @selected="applyDefaults"
        @error="(summary, error) => emit('error', summary, error)"
      />
      <PolarsStepEditor v-else v-model="polarsSteps" />

      <p v-if="engine === 'analytics'" class="switch">
        Need something the library does not cover?
        <button type="button" @click="engine = 'polars'">Write Polars code instead</button>
      </p>
      <p v-else class="switch">
        <button type="button" @click="engine = 'analytics'">Back to the analytics library</button>
      </p>

      <div class="scope">
        <label>
          Table
          <Select v-model="table" :options="tableOptions" optionLabel="label" optionValue="value" filter />
          <small v-if="engine === 'polars'">Code can read every table; this one is the default frame.</small>
        </label>
        <label>
          Risk and control
          <Select
            v-model="rcmId"
            :options="rcmOptions"
            optionLabel="label"
            optionValue="value"
            filter
            showClear
            placeholder="Leave blank to explore"
          />
        </label>
      </div>

      <Message v-if="!rcmId" severity="secondary" :closable="false">
        Exploratory results do not count as RCM coverage and cannot support a formal finding.
      </Message>

      <UiAdvancedSection
        title="Details"
        description="Title and objective, prefilled from the analytic"
        :open="!title.trim() && Boolean(analyticsSpec.test_id)"
      >
        <div class="details">
          <label>Title<InputText v-model="title" /></label>
          <label>Objective<Textarea v-model="objective" rows="2" autoResize /></label>
        </div>
      </UiAdvancedSection>
    </div>

    <template #footer>
      <Button label="Cancel" text severity="secondary" @click="visible = false" />
      <small v-if="blocker" class="blocker">{{ blocker }}</small>
      <span class="grow" />
      <Button
        label="Create and run"
        icon="pi pi-play"
        :loading="saving"
        :disabled="!ready"
        @click="submit"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.body { display: flex; flex-direction: column; gap: 0.85rem; min-width: 0; }
.switch { margin: 0; color: var(--aw-muted); font-size: 0.78rem; }
.switch button { border: 0; background: transparent; color: var(--aw-teal); cursor: pointer; font: inherit; font-weight: 600; }
.scope, .details { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.8rem; }
.details label:last-child { grid-column: 1 / -1; }
label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: #46576d; font-size: 0.75rem; font-weight: 600; }
label small { color: var(--aw-muted); font-weight: 400; }
label :deep(.p-select), label :deep(.p-inputtext), label :deep(.p-textarea) { width: 100%; min-width: 0; }
.grow { flex: 1; }
.blocker { color: var(--aw-warn); font-size: 0.75rem; }
</style>
