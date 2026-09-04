<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type { DataTestEngine, DataTestStep, RcmRow, WorkspaceSummary } from '../../types'
import AnalyticsTestAuthor from './AnalyticsTestAuthor.vue'
import PolarsStepEditor from './PolarsStepEditor.vue'
import { missingStepFields, polarsStepsValid } from './steps'

/**
 * What a data test is, in four sections and one order: what to run, what to
 * run it over, what it counts as coverage for, and what to call it.
 *
 * The old create dialog asked for a title before the analytic had been picked
 * — naming something that had not been chosen — and kept the table in a
 * "scope" block away from the parameters it constrains. The edit drawer asked
 * for the same things in a different order, in a different surface, with a
 * different footer.
 */

export interface DataTestDraft {
  title: string
  objective: string
  criteria: string
  engine: DataTestEngine
  rcmId: string
  table: string
  analytics: { test_id: string; params: Record<string, unknown> }
  steps: DataTestStep[]
}

const props = defineProps<{
  workspace: WorkspaceSummary
  rcmRows: RcmRow[]
  /** Remounts the authoring components when the record under edit changes. */
  session: string
  /** Fill the title and objective from the analytic only on a new test. */
  autoName?: boolean
}>()
const draft = defineModel<DataTestDraft>({ required: true })
const emit = defineEmits<{
  valid: [boolean]
  error: [summary: string, error: unknown]
}>()

const analyticsReady = ref(false)
const criteriaOpen = ref(false)

const tableOptions = computed(() => props.workspace.tables.map(item => ({ label: item.name, value: item.name })))
const rcmOptions = computed(() => props.rcmRows.map(row => ({ label: `${row.id} · ${row.risk}`, value: row.id })))

const definitionReady = computed(() => (draft.value.engine === 'polars'
  ? polarsStepsValid(draft.value.steps)
  : analyticsReady.value && Boolean(draft.value.table)))
const ready = computed(() => Boolean(
  draft.value.title.trim() && draft.value.objective.trim() && definitionReady.value,
))
watch(ready, value => emit('valid', value), { immediate: true })

/** Named where the value goes, rather than as a sentence in the footer. */
const missingSteps = computed(() => (draft.value.engine === 'polars'
  ? missingStepFields(draft.value.steps)
  : []))

// The analytic names itself; the auditor overrides only if they want to.
function applyDefaults(label: string, description: string) {
  if (!props.autoName) return
  if (!draft.value.title.trim()) draft.value.title = label
  if (!draft.value.objective.trim()) draft.value.objective = description
}
</script>

<template>
  <section class="section">
    <p class="aw-label">Analytic</p>
    <AnalyticsTestAuthor
      v-if="draft.engine === 'analytics'"
      :key="`analytics-${session}`"
      v-model="draft.analytics"
      :workspace="workspace"
      :table="draft.table || null"
      @valid="analyticsReady = $event"
      @selected="applyDefaults"
      @error="(summary, error) => emit('error', summary, error)"
    >
      <template #lead>
        <label :data-missing="!draft.table">
          Table
          <Select
            v-model="draft.table"
            :options="tableOptions"
            optionLabel="label"
            optionValue="value"
            filter
            placeholder="Pick a table"
          />
        </label>
      </template>
    </AnalyticsTestAuthor>

    <template v-else>
      <PolarsStepEditor v-model="draft.steps" />
      <p v-if="missingSteps.length" class="missing">Complete {{ missingSteps.join('; ') }}.</p>
    </template>

    <!-- One link, not a mode switch dressed as a paragraph. -->
    <button
      type="button"
      class="link"
      @click="draft.engine = draft.engine === 'polars' ? 'analytics' : 'polars'"
    >
      {{ draft.engine === 'polars' ? 'Back to the analytics library' : 'Write Polars code instead' }}
    </button>
  </section>

  <section v-if="draft.engine === 'polars'" class="section">
    <p class="aw-label">Default frame</p>
    <label>
      Table
      <Select
        v-model="draft.table"
        :options="tableOptions"
        optionLabel="label"
        optionValue="value"
        filter
        showClear
        placeholder="No default frame"
      />
      <small>Code can read every table; this one is the default frame.</small>
    </label>
  </section>

  <section class="section">
    <p class="aw-label">Counts as coverage for</p>
    <Select
      v-model="draft.rcmId"
      :options="rcmOptions"
      optionLabel="label"
      optionValue="value"
      filter
      showClear
      placeholder="Leave it empty for an exploratory test"
    />
    <p v-if="!draft.rcmId" class="note">
      Leave it empty for an exploratory test. Exploratory results do not count as
      coverage and cannot support a formal finding.
    </p>
  </section>

  <section class="section">
    <p class="aw-label">Title and objective</p>
    <label :data-missing="!draft.title.trim()">
      <InputText v-model="draft.title" placeholder="What this test is called" />
    </label>
    <label :data-missing="!draft.objective.trim()">
      <Textarea v-model="draft.objective" rows="2" autoResize placeholder="What it sets out to establish" />
    </label>
    <button type="button" class="link chevron" :aria-expanded="criteriaOpen" @click="criteriaOpen = !criteriaOpen">
      <i class="pi" :class="criteriaOpen ? 'pi-chevron-down' : 'pi-chevron-right'" />Criteria
    </button>
    <label v-if="criteriaOpen">
      <Textarea v-model="draft.criteria" rows="2" autoResize placeholder="The rule this test is measured against" />
    </label>
  </section>
</template>

<style scoped>
.section { display: flex; flex-direction: column; gap: .5rem; min-width: 0; }
label { display: flex; flex-direction: column; gap: .25rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label small { color: var(--aw-muted); font-weight: 400; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; }
.section > :deep(.p-select) { width: 100%; }
/* A missing required value is outlined where the value goes; the footer
   carries no blocker sentence to carry back up to the controls. */
label[data-missing='true'] :deep(.p-inputtext),
label[data-missing='true'] :deep(.p-textarea),
label[data-missing='true'] :deep(.p-select) { border-color: var(--aw-warn-line); }
.note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.45; }
.missing { margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); }
.link {
  align-self: flex-start; display: inline-flex; align-items: center; gap: .375rem;
  padding: 0; border: 0; background: none; color: var(--aw-teal);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.link:hover { text-decoration: underline; }
.link.chevron { color: var(--aw-muted); }
.link.chevron:hover { color: var(--aw-teal); }
.link .pi { font-size: var(--aw-text-2xs); }
</style>
