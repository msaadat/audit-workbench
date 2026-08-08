<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AutoComplete from 'primevue/autocomplete'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import { api } from '../../api'
import type { AuditDocument, ColumnSchema, CycleAssertion, CycleCandidate, CycleEvidenceManifest, CycleEvidenceManifestGroup, CycleFieldSelector, CycleOperand, CycleOperator, CycleVouchDefinition, CycleVouchMetadata, DocTestKind, EvidenceSemanticType, PlanningPayload, WorkspaceSummary } from '../../types'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'

const props = defineProps<{
  workspace: WorkspaceSummary
  documents: AuditDocument[]
  planning: PlanningPayload | null
  documentTypes: string[]
  cycleMetadata: CycleVouchMetadata | null
  initialRcmId?: string
  creating: boolean
}>()
const visible = defineModel<boolean>({ required: true })
const emit = defineEmits<{
  create: [payload: { kind: DocTestKind; direction: string; draft: Draft }]
  error: [summary: string, error: unknown]
}>()

interface Draft {
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
  cycleCandidateId: string
  selectionMode: 'evidence_linked' | 'sample'
  sampleMethod: 'random' | 'interval' | 'stratified'
  stratifyBy: string
  assertions: CycleAssertion[]
  cycleDefinition?: CycleVouchDefinition
  cycleRegistry?: CycleEvidenceManifestGroup['registry']
  requirementRefs?: string[]
}

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

const step = ref<1 | 2>(1)
const shape = ref('vouching')
const schema = ref<ColumnSchema[]>([])
const attributeSuggestions = ref<string[]>([])
const cycleManifest = ref<CycleEvidenceManifest | null>(null)
const cycleLoading = ref(false)
const assertionRole = ref('')
const assertionField = ref('')
const assertionOperator = ref<CycleOperator>('present')
const assertionRightSource = ref<'row' | 'role'>('role')
const assertionRightColumn = ref('')
const assertionRightRole = ref('')
const assertionRightField = ref('')
const assertionTolerance = ref(0)
const draft = ref<Draft>(emptyDraft())

const selectedShape = computed(() => shapes.find(item => item.value === shape.value)!)
const kind = computed(() => selectedShape.value.kind)
const isVouching = computed(() => kind.value === 'vouching')
const isCycle = computed(() => kind.value === 'cycle_vouch')
const needsDocument = computed(() => ['attribute', 'review', 'qa'].includes(kind.value))
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
const cycleGroups = computed(() => cycleManifest.value?.groups ?? [])
const cycleCandidates = computed(() => cycleGroups.value.flatMap(group => group.candidates.map(candidate => ({ ...candidate, group }))))
const selectedCycle = computed<{ group: CycleEvidenceManifestGroup; candidate: CycleCandidate } | null>(() => {
  const match = cycleCandidates.value.find(item => item.candidate_id === draft.value.cycleCandidateId)
  return match ? { group: match.group, candidate: match } : null
})
const selectedPack = computed(() => props.cycleMetadata?.registry.packs.find(pack => pack.id === selectedCycle.value?.group.registry.pack_id))
const roleOptions = computed(() => selectedCycle.value?.group.roles ?? [])
const selectedAssertionRole = computed(() => roleOptions.value.find(role => role.role === assertionRole.value))
const fieldOptions = computed(() => fieldsForRole(assertionRole.value))
const OPERATOR_PHRASES: Record<CycleOperator, string> = {
  present: 'is present',
  equal_exact: 'equals exactly',
  equal_normalized: 'agrees with',
  numeric_within: 'agrees within tolerance with',
  date_on_or_before: 'is on or before',
  date_within: 'is within N days of',
}
// The validator rejects an operator whose operands are the wrong semantic type,
// so authoring offers only the fields that can satisfy it rather than letting
// the auditor build a definition that will be refused on save.
const OPERATOR_OPERAND_TYPES: Partial<Record<CycleOperator, EvidenceSemanticType>> = {
  numeric_within: 'number',
  date_on_or_before: 'date',
  date_within: 'date',
}
const operatorOptions = computed(() => (props.cycleMetadata?.operators ?? []).map(value => ({
  label: `${value.replaceAll('_', ' ')} — ${OPERATOR_PHRASES[value]}`,
  value,
})))
const requiredOperandType = computed(() => OPERATOR_OPERAND_TYPES[assertionOperator.value])
const isBinaryAssertion = computed(() => assertionOperator.value !== 'present')
const assertionToleranceLabel = computed(() => assertionOperator.value === 'numeric_within'
  ? 'Absolute tolerance'
  : assertionOperator.value === 'date_within' ? 'Tolerance (days)' : '')

/** Field kinds a role's supplied evidence actually carries, per operator type.
 *
 * Two further narrowings keep authoring inside what the semantic gate accepts.
 * A `present` assertion on a required role is offered only the attributes whose
 * existence shows a control operated, because the role is already bound before
 * any assertion runs and every other field is printed by the form regardless. A
 * selector the evidence already holds more than once is withheld from every
 * scalar operand, because it can only ever resolve as ambiguous.
 */
function fieldsForRole(roleName: string) {
  const role = roleOptions.value.find(item => item.role === roleName)
  const pack = selectedPack.value
  const group = selectedCycle.value?.group
  if (!role || !pack || !group) return []
  const record = pack.record_kinds.find(item => item.id === role.record_kind)
  const available = group.records
    .filter(item => item.record_kind === role.record_kind)
    .flatMap(item => item.available_fields)
  const wanted = requiredOperandType.value
  const controlEvidenceOnly = !isBinaryAssertion.value && role.required
  return pack.field_kinds
    .filter(item => record?.available_field_kinds.includes(item.id))
    .map(item => ({
      ...item,
      attributes: item.attributes.filter(attribute => (!wanted || attribute.semantic_type === wanted)
        && (!controlEvidenceOnly || attribute.control_evidence === true)
        && available.some(field =>
          field.group === item.group
          && field.kind === item.kind
          && field.attributes.includes(attribute.id)
          && (field.distinct_value_counts?.[attribute.id] ?? 1) <= 1,
        )),
    }))
    .filter(item => item.attributes.length)
}
const rightFieldOptions = computed(() => fieldsForRole(assertionRightRole.value))
const rowColumnOptions = computed(() => {
  const types = selectedCycle.value?.candidate.column_types ?? {}
  const wanted = requiredOperandType.value
  return Object.entries(types)
    .filter(([, type]) => !wanted || type === wanted)
    .map(([column]) => column)
})
const canAddAssertion = computed(() => {
  if (!assertionRole.value || !assertionField.value) return false
  if (!isBinaryAssertion.value) return true
  return assertionRightSource.value === 'row'
    ? Boolean(assertionRightColumn.value)
    : Boolean(assertionRightRole.value && assertionRightField.value)
})
const oversizedEvidenceSelection = computed(() => draft.value.selectionMode === 'evidence_linked' && (selectedCycle.value?.candidate.linked_rows ?? 0) > (props.cycleMetadata?.limits.max_items ?? 500))

const derivedTitle = computed(() => draft.value.title.trim() || `${selectedShape.value.label} test`)
// Step 1 gates on nothing except a shape; the RCM row is offered here, where
// it can actually be answered, instead of behind a disabled Next button.
const step2Ready = computed(() => {
  if (isCycle.value) return Boolean(draft.value.rcmId && selectedCycle.value && draft.value.procedureKey.trim() && draft.value.assertions.length && !oversizedEvidenceSelection.value)
  if (isVouching.value) return Boolean(draft.value.table)
  if (kind.value === 'qa') return Boolean(draft.value.documentId) && Boolean(draft.value.questions.trim())
  return Boolean(draft.value.documentId)
})
const missingLabel = computed(() => {
  if (isCycle.value && !draft.value.rcmId) return 'Link an RCM row with transaction-cycle attributes.'
  if (isCycle.value && !selectedCycle.value) return 'Pick a prevalidated cycle population.'
  if (isCycle.value && !draft.value.assertions.length) return 'Add at least one typed assertion.'
  if (oversizedEvidenceSelection.value) return 'Confirm a deterministic sample before creation.'
  if (isVouching.value && !draft.value.table) return 'Pick the population table to continue.'
  if (needsDocument.value && !draft.value.documentId) return 'Pick the document to continue.'
  if (kind.value === 'qa' && !draft.value.questions.trim()) return 'Add at least one question to continue.'
  return ''
})

function emptyDraft(): Draft {
  return {
    title: '', rcmId: props.initialRcmId ?? '', table: '', size: 10, seed: 42,
    frozenFields: [], identifierFields: [], requiredDocumentTypes: [],
    evidenceAware: true, attributes: [], documentId: '', pages: '', questions: '',
    procedureKey: 'cycle-vouch', cycleCandidateId: '', selectionMode: 'evidence_linked',
    sampleMethod: 'random', stratifyBy: '', assertions: [],
  }
}

async function loadCycleCandidates() {
  cycleManifest.value = null
  draft.value.cycleCandidateId = ''
  if (!isCycle.value || !draft.value.rcmId) return
  cycleLoading.value = true
  try {
    cycleManifest.value = await api.post<CycleEvidenceManifest>(
      `/api/workspaces/${props.workspace.id}/doc-tests/cycle-vouch/candidates`,
      { rcm_id: draft.value.rcmId },
    )
    draft.value.cycleCandidateId = cycleManifest.value.groups[0]?.candidates[0]?.candidate_id ?? ''
  } catch (error) { emit('error', 'Could not build cycle candidates', error) }
  finally { cycleLoading.value = false }
}

// Keys are immutable once results exist, so a repeated role/field/operator has
// to gain a suffix rather than silently overwrite the earlier column.
function uniqueAssertionKey(base: string): string {
  const cleaned = base.replace(/[^A-Za-z0-9_-]/g, '_')
  const taken = new Set(draft.value.assertions.map(item => item.key))
  if (!taken.has(cleaned)) return cleaned
  let suffix = 2
  while (taken.has(`${cleaned}_${suffix}`)) suffix += 1
  return `${cleaned}_${suffix}`
}

function selectorFor(field: { group: string; kind: string; attributes: Array<{ id: string }> }): CycleFieldSelector {
  // No attribute is implicit: `value` where the field exposes it, otherwise the
  // one attribute the supplied evidence actually carries.
  return {
    group: field.group,
    kind: field.kind,
    attribute: field.attributes.some(item => item.id === 'value') ? 'value' : field.attributes[0].id,
  }
}

function addAssertion() {
  const role = selectedAssertionRole.value
  const field = fieldOptions.value.find(item => item.id === assertionField.value)
  if (!role || !field) return
  const operator = assertionOperator.value
  const left: CycleOperand = { source: 'role', role: role.role, field: selectorFor(field) }

  let right: CycleOperand | undefined
  let rightLabel = ''
  if (operator !== 'present') {
    if (assertionRightSource.value === 'row') {
      if (!assertionRightColumn.value) return
      right = { source: 'row', column: assertionRightColumn.value }
      rightLabel = assertionRightColumn.value
    } else {
      const otherRole = roleOptions.value.find(item => item.role === assertionRightRole.value)
      const otherField = rightFieldOptions.value.find(item => item.id === assertionRightField.value)
      if (!otherRole || !otherField) return
      right = { source: 'role', role: otherRole.role, field: selectorFor(otherField) }
      rightLabel = `${otherField.label} on ${otherRole.role.replaceAll('_', ' ')}`
    }
  }

  const tolerance = operator === 'numeric_within'
    ? { absolute: assertionTolerance.value, percent: 0 }
    : operator === 'date_within'
      ? Math.trunc(assertionTolerance.value)
      : undefined
  const label = operator === 'present'
    ? `${field.label} is present on ${role.role.replaceAll('_', ' ')}`
    : `${field.label} on ${role.role.replaceAll('_', ' ')} ${OPERATOR_PHRASES[operator]} ${rightLabel}`

  draft.value.assertions.push({
    key: uniqueAssertionKey(`${role.role}_${field.kind}_${operator}`),
    label,
    left,
    operator,
    ...(right ? { right } : {}),
    ...(tolerance !== undefined ? { tolerance } : {}),
  })
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
watch([shape, () => draft.value.rcmId], () => void loadCycleCandidates())
watch(visible, open => {
  if (!open) return
  step.value = 1
  shape.value = 'vouching'
  draft.value = emptyDraft()
})

function submit() {
  const selected = selectedCycle.value
  let cycleDefinition: CycleVouchDefinition | undefined
  if (isCycle.value && selected) {
    const roles = selected.group.roles.map(role => {
      const facts = selected.candidate.relationship_facts?.[role.role]
      return {
        ...role,
        cardinality: (facts?.max_records_per_item ?? 0) > 1 ? 'many' as const : 'one' as const,
        reuse_across_items: (facts?.max_items_per_record ?? 0) > 1 ? 'allowed' as const : role.reuse_across_items,
      }
    })
    const selection = draft.value.selectionMode === 'evidence_linked'
      ? { mode: 'evidence_linked' as const, assurance_scope: 'targeted_evidence_only' as const }
      : {
          mode: 'sample' as const,
          assurance_scope: 'sampled_population' as const,
          method: draft.value.sampleMethod,
          size: draft.value.size,
          seed: draft.value.seed,
          ...(draft.value.sampleMethod === 'stratified' ? { stratify_by: draft.value.stratifyBy } : {}),
        }
    cycleDefinition = {
      population: {
        candidate_id: selected.candidate.candidate_id,
        selection_reason: `Auditor selected the ${selected.candidate.source_kind} ${selected.candidate.table} population.`,
        table: selected.candidate.table,
        row_key: selected.candidate.row_key,
        cycle_keys: selected.candidate.cycle_keys,
        selection,
      },
      roles,
      assertions: draft.value.assertions,
    }
  }
  emit('create', {
    kind: kind.value,
    direction: selectedShape.value.direction,
    draft: {
      ...draft.value,
      title: derivedTitle.value,
      cycleDefinition,
      cycleRegistry: selected?.group.registry,
      requirementRefs: selected?.group.requirement_refs.map(key => `${draft.value.rcmId}:${key}`),
    },
  })
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    header="New document test"
    :style="{ width: 'min(46rem, 94vw)' }"
    :contentStyle="{ maxHeight: '76vh', overflow: 'auto' }"
  >
    <ol class="steps">
      <li :class="{ active: step === 1, done: step > 1 }"><i>1</i>What and why</li>
      <li :class="{ active: step === 2 }"><i>2</i>Scope</li>
    </ol>

    <div v-if="step === 1" class="form">
      <label class="wide">
        Test shape
        <Select v-model="shape" :options="shapes" optionLabel="label" optionValue="value" />
        <small>{{ selectedShape.hint }}</small>
      </label>
      <label class="wide">
        Risk and control
        <Select
          v-model="draft.rcmId"
          :options="rcmOptions"
          optionLabel="label"
          optionValue="value"
          filter
          showClear
          placeholder="Link this test to an RCM row"
        />
      </label>
      <Message v-if="!draft.rcmId" severity="warn" :closable="false" class="wide">
        Without an RCM row this is standalone document work: it will not count towards
        engagement coverage and cannot support a formal finding.
      </Message>
      <label class="wide">
        Title
        <InputText v-model="draft.title" :placeholder="derivedTitle" />
      </label>
    </div>

    <div v-else class="form">
      <template v-if="isCycle">
        <Message v-if="!cycleGroups.length && !cycleLoading" severity="warn" :closable="false" class="wide">This RCM row has no prevalidated registry-backed cycle population.</Message>
        <label class="wide">
          Cycle population
          <Select v-model="draft.cycleCandidateId" :options="cycleCandidates" optionValue="candidate_id" :loading="cycleLoading" filter>
            <template #value="{ value }"><span>{{ cycleCandidates.find(item => item.candidate_id === value)?.table ?? 'Select a candidate' }}</span></template>
            <template #option="{ option }"><div class="candidate"><strong>{{ option.table }} · {{ option.row_key.column }}</strong><small>{{ option.linked_rows }}/{{ option.population_rows }} rows linked · {{ option.complete_cycle_count }} complete cycles · {{ option.source_kind }}</small></div></template>
          </Select>
        </label>
        <label>Procedure key<InputText v-model="draft.procedureKey" /></label>
        <label>Selection basis<Select v-model="draft.selectionMode" :options="[{ label: 'Targeted evidence', value: 'evidence_linked' }, { label: 'Sampled population', value: 'sample' }]" optionLabel="label" optionValue="value" /></label>
        <Message v-if="oversizedEvidenceSelection" severity="warn" :closable="false" class="wide">
          {{ selectedCycle?.candidate.linked_rows }} rows qualify, above the {{ cycleMetadata?.limits.max_items }}-item cap. No rows will be truncated.
          <Button label="Use suggested random sample (25, seed 42)" size="small" text @click="confirmSuggestedSample" />
        </Message>
        <template v-if="draft.selectionMode === 'sample'">
          <label>Sampling method<Select v-model="draft.sampleMethod" :options="cycleMetadata?.sampling_methods ?? []" /></label>
          <label>Sample size<InputNumber v-model="draft.size" :min="1" :max="cycleMetadata?.limits.max_items ?? 500" /></label>
          <label>Seed<InputNumber v-model="draft.seed" :useGrouping="false" /></label>
          <label v-if="draft.sampleMethod === 'stratified'">Stratify by<Select v-model="draft.stratifyBy" :options="Object.keys(selectedCycle?.candidate.column_types ?? {})" /></label>
        </template>
        <section class="wide assertion-author">
          <div><strong>Typed assertions</strong><small>Operands come from the selected pack and the fields the supplied evidence actually carries. Choosing an operator narrows both sides to the types it accepts.</small></div>
          <label>Operator<Select v-model="assertionOperator" :options="operatorOptions" optionLabel="label" optionValue="value" /></label>
          <label>Role<Select v-model="assertionRole" :options="roleOptions" optionLabel="role" optionValue="role" /></label>
          <label>Field<Select v-model="assertionField" :options="fieldOptions" optionLabel="label" optionValue="id" /></label>
          <template v-if="isBinaryAssertion">
            <label>Compare with<Select v-model="assertionRightSource" :options="[{ label: 'Population row column', value: 'row' }, { label: 'Another document role', value: 'role' }]" optionLabel="label" optionValue="value" /></label>
            <label v-if="assertionRightSource === 'row'">Row column<Select v-model="assertionRightColumn" :options="rowColumnOptions" filter /></label>
            <template v-else>
              <label>Other role<Select v-model="assertionRightRole" :options="roleOptions" optionLabel="role" optionValue="role" /></label>
              <label>Other field<Select v-model="assertionRightField" :options="rightFieldOptions" optionLabel="label" optionValue="id" /></label>
            </template>
            <label v-if="assertionToleranceLabel">{{ assertionToleranceLabel }}<InputNumber v-model="assertionTolerance" :min="0" :maxFractionDigits="assertionOperator === 'numeric_within' ? 2 : 0" /></label>
          </template>
          <Button label="Add assertion" icon="pi pi-plus" size="small" outlined :disabled="!canAddAssertion" @click="addAssertion" />
          <ul v-if="draft.assertions.length"><li v-for="(assertion, index) in draft.assertions" :key="assertion.key"><span><strong>{{ assertion.label }}</strong><small>{{ assertion.key }} · {{ assertion.operator }}</small></span><Button icon="pi pi-trash" text severity="danger" size="small" @click="draft.assertions.splice(index, 1)" /></li></ul>
        </section>
      </template>

      <template v-else-if="isVouching">
        <label>
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
        <label class="wide">
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
        <label class="wide">
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
        <div class="wide check">
          <Checkbox v-model="draft.evidenceAware" inputId="evidence-aware" binary />
          <label for="evidence-aware">Prioritise transactions that already have imported evidence</label>
        </div>
        <UiAdvancedSection class="wide" title="Sampling" description="Sample size and reproducible seed">
          <div class="form">
            <label>Sample size<InputNumber v-model="draft.size" :min="1" /></label>
            <label>Seed<InputNumber v-model="draft.seed" :useGrouping="false" /></label>
          </div>
        </UiAdvancedSection>
      </template>

      <template v-else>
        <label class="wide">
          Document
          <Select
            v-model="draft.documentId"
            :options="documentOptions"
            optionLabel="label"
            optionValue="value"
            filter
          />
        </label>
        <label v-if="kind === 'attribute'" class="wide">
          Attributes
          <AutoComplete
            v-model="draft.attributes"
            multiple
            :suggestions="attributeSuggestions"
            placeholder="Type an attribute and press enter"
            @complete="searchAttributes"
          />
        </label>
        <label v-else-if="kind === 'review'" class="wide">
          Pages
          <InputText v-model="draft.pages" placeholder="Leave blank to review every page (e.g. 1, 3, 4)" />
        </label>
        <label v-else class="wide">
          Questions
          <Textarea v-model="draft.questions" rows="5" placeholder="One question per line" />
        </label>
      </template>
    </div>

    <template #footer>
      <Button label="Cancel" text severity="secondary" @click="visible = false" />
      <small v-if="step === 2 && missingLabel" class="missing">{{ missingLabel }}</small>
      <span class="grow" />
      <Button v-if="step === 2" label="Back" severity="secondary" outlined @click="step = 1" />
      <Button
        v-if="step === 1"
        label="Next"
        icon="pi pi-arrow-right"
        iconPos="right"
        @click="step = 2"
      />
      <Button
        v-else
        label="Create test"
        icon="pi pi-plus"
        :loading="creating"
        :disabled="!step2Ready"
        @click="submit"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.steps { display: flex; gap: 0.5rem; margin: 0 0 1rem; padding: 0 0 0.6rem; border-bottom: 1px solid var(--aw-border); list-style: none; }
.steps li { display: flex; align-items: center; gap: 0.4rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.steps li + li { margin-left: 0.6rem; }
.steps i { display: grid; place-items: center; width: 1.4rem; height: 1.4rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); font-style: normal; }
.steps li.active { color: var(--aw-teal); }
.steps li.active i, .steps li.done i { color: var(--aw-on-dark); background: var(--aw-teal); }

.form { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.8rem; }
.wide { grid-column: 1 / -1; }
label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label small { color: var(--aw-muted); font-weight: 400; }
label :deep(.p-inputtext), label :deep(.p-select), label :deep(.p-multiselect), label :deep(.p-autocomplete), label :deep(.p-inputnumber) { width: 100%; min-width: 0; }
.check { display: flex; align-items: center; gap: 0.5rem; }
.check label { flex-direction: row; font-weight: 500; }
.grow { flex: 1; }
.missing { color: var(--aw-warn); font-size: var(--aw-text-sm); }
.candidate { display:flex; flex-direction:column; gap:.15rem }.candidate small { color:var(--aw-muted) }
.assertion-author { display:grid; grid-template-columns:1fr 1fr auto; gap:.65rem; align-items:end; padding:.75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }.assertion-author>div,.assertion-author ul { grid-column:1/-1 }.assertion-author>div { display:flex; flex-direction:column; gap:.2rem }.assertion-author small { color:var(--aw-muted); font-weight:400 }.assertion-author ul { display:flex; flex-direction:column; gap:.4rem; margin:0; padding:0; list-style:none }.assertion-author li { display:flex; align-items:center; justify-content:space-between; gap:.5rem; padding:.45rem; background:var(--aw-canvas); border-radius:var(--aw-radius-control) }.assertion-author li span { display:flex; flex-direction:column }
</style>
