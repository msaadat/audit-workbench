<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AutoComplete from 'primevue/autocomplete'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import { api } from '../../api'
import type {
  AuditDocument, ColumnSchema, CycleRulesetCandidate, CycleRulesetDefinition,
  CycleVouchMetadata, DocTestKind, PlanningPayload, WorkspaceSummary,
} from '../../types'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'

/**
 * What a document test is, in one pass.
 *
 * The two-step stepper is gone. It split "what and why" from "scope" across a
 * Next button, which meant the shape — the one answer that decides which scope
 * fields exist — was chosen on a screen that could not show them, and the
 * blocker sentence sat in a footer away from the field it named.
 */

export interface DocTestDraft {
  title: string
  rcmId: string
  table: string
  size: number
  seed: number
  frozenFields: string[]
  identifierFields: string[]
  requiredDocumentTypes: string[]
  evidenceAware: boolean
  attributes: string[]
  documentId: string
  pages: string
  questions: string
  procedureKey: string
  selectionMode: 'evidence_linked' | 'sample'
  sampleMethod: 'random' | 'interval' | 'stratified'
  stratifyBy: string
  /** The approved rules a cycle test runs under, and the rows it runs over. */
  cycleRulesetDefinition?: CycleRulesetDefinition
  requirementRefs?: string[]
}

const props = defineProps<{
  workspace: WorkspaceSummary
  documents: AuditDocument[]
  planning: PlanningPayload | null
  documentTypes: string[]
  cycleMetadata: CycleVouchMetadata | null
}>()
const shape = defineModel<string>('shape', { required: true })
const draft = defineModel<DocTestDraft>({ required: true })
const emit = defineEmits<{
  valid: [boolean]
  error: [summary: string, error: unknown]
}>()

// One list: the vouching/tracing direction was previously asked a second time
// on the next step as if it were a different decision.
const shapes = [
  { value: 'vouching', kind: 'vouching' as DocTestKind, direction: 'vouching', label: 'Vouching', hint: 'Start from the recorded transaction and find its supporting document.' },
  { value: 'tracing', kind: 'vouching' as DocTestKind, direction: 'tracing', label: 'Tracing', hint: 'Start from the source document and find it in the records.' },
  { value: 'attribute', kind: 'attribute' as DocTestKind, direction: 'vouching', label: 'Attribute test', hint: 'Check named attributes are present on a document.' },
  { value: 'review', kind: 'review' as DocTestKind, direction: 'vouching', label: 'Document review', hint: 'Read a document, or named pages of it, and summarise.' },
  { value: 'qa', kind: 'qa' as DocTestKind, direction: 'vouching', label: 'Cited Q&A', hint: 'Ask questions and get answers with page citations.' },
  { value: 'cycle_vouch', kind: 'cycle_vouch' as DocTestKind, direction: 'vouching', label: 'Cycle vouch', hint: 'Test a registry-backed transaction cycle against typed evidence records.' },
]

const schema = ref<ColumnSchema[]>([])
const attributeSuggestions = ref<string[]>([])
const rulesetCandidate = ref<CycleRulesetCandidate | null>(null)
const cycleLoading = ref(false)

const selectedShape = computed(() => shapes.find(item => item.value === shape.value)!)
const kind = computed(() => selectedShape.value.kind)
const isVouching = computed(() => kind.value === 'vouching')
const isCycle = computed(() => kind.value === 'cycle_vouch')
const tableOptions = computed(() => props.workspace.tables.map(table => ({ label: table.name, value: table.name })))
const documentOptions = computed(() => props.documents.map(doc => ({ label: doc.title, value: doc.id })))
const rcmOptions = computed(() => (props.planning?.rcm ?? []).map(row => ({
  label: `${row.id} · ${row.risk}`,
  value: row.id,
})))
const columnOptions = computed(() => schema.value.map(column => column.name))
const documentTypeOptions = computed(() => props.documentTypes.map(value => ({
  label: value.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()),
  value,
})))
/** A workspace with approved rules authors nothing here: the roles, join keys
 *  and assertions were reviewed and approved in the cycle rules screen, and all
 *  that is left to decide is which rows to test. */
const isRulesetCycle = computed(() => Boolean(rulesetCandidate.value?.ruleset_id))
const rulesetReach = computed(() => rulesetCandidate.value?.reach ?? null)
const rulesetMissingRoles = computed(() =>
  Object.entries(rulesetReach.value?.missing_role_counts ?? {}),
)
const oversizedEvidenceSelection = computed(
  () =>
    draft.value.selectionMode === 'evidence_linked'
    && (rulesetReach.value?.linked_rows ?? 0) > (props.cycleMetadata?.limits.max_items ?? 500),
)

const derivedTitle = computed(() => draft.value.title.trim() || `${selectedShape.value.label} test`)
const ready = computed(() => {
  if (isCycle.value) {
    return Boolean(
      draft.value.rcmId
      && isRulesetCycle.value
      && draft.value.procedureKey.trim()
      && !oversizedEvidenceSelection.value,
    )
  }
  if (isVouching.value) return Boolean(draft.value.table)
  if (kind.value === 'qa') return Boolean(draft.value.documentId) && Boolean(draft.value.questions.trim())
  return Boolean(draft.value.documentId)
})
watch(ready, value => emit('valid', value), { immediate: true })

async function loadCycleCandidates() {
  rulesetCandidate.value = null
  if (!isCycle.value || !draft.value.rcmId) return
  cycleLoading.value = true
  try {
    rulesetCandidate.value = await api.post<CycleRulesetCandidate>(
      `/api/workspaces/${props.workspace.id}/doc-tests/cycle-vouch/candidates`,
      { rcm_id: draft.value.rcmId },
    )
  } catch (error) { emit('error', 'Could not read the approved cycle rules', error) }
  finally { cycleLoading.value = false }
}

/** The selection, in the shape the build endpoint reads. Shared by both
 *  branches because choosing rows never depended on the vocabulary. */
function selectionPayload() {
  return draft.value.selectionMode === 'evidence_linked'
    ? { mode: 'evidence_linked' as const }
    : {
        mode: 'sample' as const,
        method: draft.value.sampleMethod,
        size: draft.value.size,
        seed: draft.value.seed,
        ...(draft.value.sampleMethod === 'stratified' ? { stratify_by: draft.value.stratifyBy } : {}),
      }
}

function confirmSuggestedSample() {
  draft.value.selectionMode = 'sample'
  draft.value.sampleMethod = 'random'
  draft.value.size = 25
  draft.value.seed = 42
}

async function loadSchema(table: string) {
  schema.value = []
  if (!table) return
  try {
    schema.value = (await api.get<{ columns: ColumnSchema[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${table}/schema`,
    )).columns
  } catch (error) { emit('error', 'Could not load the table schema', error) }
}

function searchAttributes(event: { query: string }) {
  const query = event.query.trim().toLowerCase()
  const base = ['approval', 'signature', 'date', 'amount', 'reference', 'authorisation', 'receipt']
  attributeSuggestions.value = query
    ? [query, ...base.filter(value => value.includes(query) && value !== query)]
    : base
}

watch(() => draft.value.table, table => void loadSchema(table))
// Immediate, because the drawer can open already answering both — the RCM
// row's "Add test" link carries the row over, and the shape can be restored.
watch([shape, () => draft.value.rcmId], () => void loadCycleCandidates(), { immediate: true })

/** What the tab posts. Built here because only this form knows the shape. */
function payload() {
  return {
    kind: kind.value,
    direction: selectedShape.value.direction,
    draft: {
      ...draft.value,
      title: derivedTitle.value,
      ...(isCycle.value
        ? {
            cycleRulesetDefinition: {
              ruleset_id: String(rulesetCandidate.value?.ruleset_id ?? ''),
              population: { selection: selectionPayload() },
            },
            requirementRefs: [`${draft.value.rcmId}:${draft.value.procedureKey.trim()}`],
          }
        : {}),
    },
  }
}
defineExpose({ payload })
</script>

<template>
  <section class="section">
    <p class="aw-label">Test shape</p>
    <Select v-model="shape" :options="shapes" optionLabel="label" optionValue="value" autofocus />
    <p class="note">{{ selectedShape.hint }}</p>
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
      placeholder="Leave it empty for standalone document work"
      :class="{ 'is-missing': isCycle && !draft.rcmId }"
    />
    <p v-if="!draft.rcmId" class="note">
      Without an RCM row this is standalone document work: it will not count
      towards coverage and cannot support a formal finding.
    </p>
  </section>

  <section class="section">
    <p class="aw-label">Scope</p>

    <template v-if="isCycle">
      <p v-if="!isRulesetCycle && !cycleLoading" class="missing">
        This engagement has no approved cycle ruleset, so there are no rules to
        vouch against. Propose and approve one in the cycle rules review.
      </p>
      <div v-if="isRulesetCycle" class="approved-cycle">
        <div class="approved-head">
          <strong>{{ rulesetCandidate?.cycle_label || 'Approved cycle' }}</strong>
          <small>
            Roles, join keys and assertions were approved in the cycle rules
            review. Choose which rows to test.
          </small>
        </div>
        <dl>
          <div>
            <dt>Seeded from</dt>
            <dd><code>{{ rulesetCandidate?.anchor?.table }}.{{ rulesetCandidate?.anchor?.column }}</code></dd>
          </div>
          <div><dt>Rows</dt><dd>{{ rulesetReach?.population_rows }}</dd></div>
          <div><dt>With evidence</dt><dd>{{ rulesetReach?.linked_rows }}</dd></div>
          <div><dt>Complete cycles</dt><dd>{{ rulesetReach?.complete_cycles }}</dd></div>
        </dl>
        <ul v-if="rulesetMissingRoles.length" class="gaps">
          <li v-for="[role, count] in rulesetMissingRoles" :key="role">
            {{ count }} row(s) reach no <strong>{{ role }}</strong>. Those items
            are still created, and report the gap rather than passing.
          </li>
        </ul>
        <ul class="rules">
          <li v-for="check in rulesetCandidate?.assertions ?? []" :key="check.id">
            <strong>{{ check.label || check.id }}</strong>
            <small>{{ check.rationale }}</small>
          </li>
        </ul>
      </div>
      <div class="pair">
        <label :data-missing="!draft.procedureKey.trim()">
          Procedure key<InputText v-model="draft.procedureKey" />
        </label>
        <label>
          Selection basis
          <Select
            v-model="draft.selectionMode"
            :options="[{ label: 'Targeted evidence', value: 'evidence_linked' }, { label: 'Sampled population', value: 'sample' }]"
            optionLabel="label"
            optionValue="value"
          />
        </label>
      </div>
      <div v-if="oversizedEvidenceSelection" class="missing">
        {{ rulesetReach?.linked_rows }} rows qualify, above the
        {{ cycleMetadata?.limits.max_items }}-item cap. No rows will be truncated.
        <Button label="Use suggested random sample (25, seed 42)" size="small" text @click="confirmSuggestedSample" />
      </div>
      <div v-if="draft.selectionMode === 'sample'" class="pair">
        <label>Sampling method<Select v-model="draft.sampleMethod" :options="cycleMetadata?.sampling_methods ?? []" /></label>
        <label>Sample size<InputNumber v-model="draft.size" :min="1" :max="cycleMetadata?.limits.max_items ?? 500" /></label>
        <label>Seed<InputNumber v-model="draft.seed" :useGrouping="false" /></label>
        <label v-if="draft.sampleMethod === 'stratified'">Stratify by<Select v-model="draft.stratifyBy" :options="columnOptions" /></label>
      </div>
    </template>

    <template v-else-if="isVouching">
      <div class="pair">
        <label :data-missing="!draft.table">
          Population table
          <Select v-model="draft.table" :options="tableOptions" optionLabel="label" optionValue="value" filter />
        </label>
        <label>
          Required document types
          <MultiSelect
            v-model="draft.requiredDocumentTypes"
            :options="documentTypeOptions"
            optionLabel="label"
            optionValue="value"
            display="chip"
            placeholder="Any supporting document"
          />
        </label>
      </div>
      <label>
        Identifier fields
        <MultiSelect
          v-model="draft.identifierFields"
          :options="columnOptions"
          display="chip"
          filter
          :disabled="!draft.table"
          placeholder="Columns that identify the transaction"
        />
      </label>
      <label>
        Frozen fields
        <MultiSelect
          v-model="draft.frozenFields"
          :options="columnOptions"
          display="chip"
          filter
          :disabled="!draft.table"
          placeholder="Columns compared against the document"
        />
      </label>
      <div class="check">
        <Checkbox v-model="draft.evidenceAware" inputId="evidence-aware" binary />
        <label for="evidence-aware">Prioritise transactions that already have imported evidence</label>
      </div>
      <UiAdvancedSection title="Sampling" description="Sample size and reproducible seed">
        <div class="pair">
          <label>Sample size<InputNumber v-model="draft.size" :min="1" /></label>
          <label>Seed<InputNumber v-model="draft.seed" :useGrouping="false" /></label>
        </div>
      </UiAdvancedSection>
    </template>

    <template v-else>
      <label :data-missing="!draft.documentId">
        Document
        <Select
          v-model="draft.documentId"
          :options="documentOptions"
          optionLabel="label"
          optionValue="value"
          filter
        />
      </label>
      <label v-if="kind === 'attribute'">
        Attributes
        <AutoComplete
          v-model="draft.attributes"
          multiple
          :suggestions="attributeSuggestions"
          placeholder="Type an attribute and press enter"
          @complete="searchAttributes"
        />
      </label>
      <label v-else-if="kind === 'review'">
        Pages
        <InputText v-model="draft.pages" placeholder="Leave blank to review every page (e.g. 1, 3, 4)" />
      </label>
      <label v-else :data-missing="!draft.questions.trim()">
        Questions
        <Textarea v-model="draft.questions" rows="5" placeholder="One question per line" />
      </label>
    </template>
  </section>

  <section class="section">
    <p class="aw-label">Title</p>
    <InputText v-model="draft.title" :placeholder="derivedTitle" />
  </section>
</template>

<style scoped>
.section { display: flex; flex-direction: column; gap: .5rem; min-width: 0; }
.section > :deep(.p-select), .section > :deep(.p-inputtext) { width: 100%; }
.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .625rem; }
label { display: flex; flex-direction: column; gap: .25rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label :deep(.p-inputtext), label :deep(.p-select), label :deep(.p-multiselect), label :deep(.p-autocomplete), label :deep(.p-inputnumber) { width: 100%; min-width: 0; }
label[data-missing='true'] :deep(.p-inputtext),
label[data-missing='true'] :deep(.p-select),
label[data-missing='true'] :deep(.p-textarea),
.is-missing { border-color: var(--aw-warn-line); }
.check { display: flex; align-items: center; gap: .5rem; }
.check label { flex-direction: row; font-weight: 500; }
.note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.45; }
.missing { margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); line-height: 1.45; }

.approved-cycle { display: flex; flex-direction: column; gap: .5rem; padding: .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.approved-head { display: flex; flex-direction: column; gap: .125rem; }
.approved-cycle small { color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.4; }
.approved-cycle dl { display: flex; flex-wrap: wrap; gap: 1.25rem; margin: 0; }
.approved-cycle dt { color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
.approved-cycle dd { margin: 0; font-variant-numeric: tabular-nums; font-weight: 600; }
.approved-cycle code { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
.gaps, .rules { display: flex; flex-direction: column; gap: .25rem; margin: 0; padding-left: 1.1rem; font-size: var(--aw-text-sm); }
.rules li { display: flex; flex-direction: column; }
</style>
