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
import type { AuditDocument, ColumnSchema, DocTestKind, PlanningPayload, WorkspaceSummary } from '../../types'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'

const props = defineProps<{
  workspace: WorkspaceSummary
  documents: AuditDocument[]
  planning: PlanningPayload | null
  documentTypes: string[]
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
}

// One list: the vouching/tracing direction was previously asked a second time
// on the next step as if it were a different decision.
const shapes = [
  { value: 'vouching', kind: 'vouching' as DocTestKind, direction: 'vouching', label: 'Vouching', hint: 'Start from the recorded transaction and find its supporting document.' },
  { value: 'tracing', kind: 'vouching' as DocTestKind, direction: 'tracing', label: 'Tracing', hint: 'Start from the source document and find it in the records.' },
  { value: 'attribute', kind: 'attribute' as DocTestKind, direction: 'vouching', label: 'Attribute test', hint: 'Check named attributes are present on a document.' },
  { value: 'review', kind: 'review' as DocTestKind, direction: 'vouching', label: 'Document review', hint: 'Read a document, or named pages of it, and summarise.' },
  { value: 'qa', kind: 'qa' as DocTestKind, direction: 'vouching', label: 'Cited Q&A', hint: 'Ask questions and get answers with page citations.' },
]

const step = ref<1 | 2>(1)
const shape = ref('vouching')
const schema = ref<ColumnSchema[]>([])
const attributeSuggestions = ref<string[]>([])
const draft = ref<Draft>(emptyDraft())

const selectedShape = computed(() => shapes.find(item => item.value === shape.value)!)
const kind = computed(() => selectedShape.value.kind)
const isVouching = computed(() => kind.value === 'vouching')
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

const derivedTitle = computed(() => draft.value.title.trim() || `${selectedShape.value.label} test`)
// Step 1 gates on nothing except a shape; the RCM row is offered here, where
// it can actually be answered, instead of behind a disabled Next button.
const step2Ready = computed(() => {
  if (isVouching.value) return Boolean(draft.value.table)
  if (kind.value === 'qa') return Boolean(draft.value.documentId) && Boolean(draft.value.questions.trim())
  return Boolean(draft.value.documentId)
})
const missingLabel = computed(() => {
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
  }
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
watch(visible, open => {
  if (!open) return
  step.value = 1
  shape.value = 'vouching'
  draft.value = emptyDraft()
})

function submit() {
  emit('create', { kind: kind.value, direction: selectedShape.value.direction, draft: { ...draft.value, title: derivedTitle.value } })
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
      <template v-if="isVouching">
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
</style>
