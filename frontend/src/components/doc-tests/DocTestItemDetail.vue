<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type {
  AuditDocument,
  AuditFinding,
  DocTest,
  DocTestDisposition,
  DocTestDispositionState,
  DocTestEvaluation,
  DocTestItem,
  EvidenceRef,
} from '../../types'
import ProvenanceRail from '../agent/ProvenanceRail.vue'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'
import { plural, pluralWord, verb } from '../../format'

const props = defineProps<{
  test: DocTest
  item: DocTestItem
  documents: AuditDocument[]
  findings: AuditFinding[]
  running: boolean
  busy: boolean
  focusAssertionKey?: string | null
  /** Optional so the component stays mountable in a bare test harness. */
  workspaceId?: string
}>()
const emit = defineEmits<{
  anchor: [EvidenceRef]
  attach: [documentId: string]
  saveChecks: []
  saveAttributes: []
  setState: [value: DocTestDispositionState, note?: string]
  saveConclusion: []
  generateFinding: [regenerate: boolean]
  openFinding: [findingId: string]
  updateEvidenceRequest: [requestId: string, status: 'received' | 'cancelled']
  run: []
  openRcm: [rcmId: string]
}>()

const attachId = ref<string | null>(null)
const detailRoot = ref<HTMLElement | null>(null)
const methods = [
  'exact',
  'normalized',
  'fuzzy',
  'numeric_tolerance',
  'date_tolerance',
]
const kindLabel: Record<string, string> = {
  vouching: 'Vouching / tracing',
  attribute: 'Attribute test',
  review: 'Document review',
  qa: 'Cited Q&A',
  cycle_vouch: 'Cycle vouch',
}
// Short labels: the field is already called "Control conclusion", so repeating
// "Control" in every option only spent width the rail does not have.
const controlConclusions = [
  { label: 'Not concluded', value: 'no_conclusion' },
  { label: 'Effective', value: 'effective' },
  { label: 'Partially effective', value: 'partially_effective' },
  { label: 'Ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]

const documentOptions = computed(() => props.documents.map(doc => ({ label: doc.title, value: doc.id })))
const attachable = computed(() => documentOptions.value.filter(option => !props.item.document_ids.includes(option.value)))
const machineNotePrefixes = [
  'Model assessment outcome:',
  'Cited answer generated from the attached pages',
  'Cited answers were generated from the attached pages',
  'Cited LLM assessment generated from the attached pages',
]
// The runner stamps `Model assessment outcome: <state>.` onto every LLM item —
// the same fact the status chip already carries, in the runner's vocabulary
// rather than the auditor's. Matching the prefixes the runner itself writes
// leaves a genuine note — a manual fallback, an OCR gap — untouched.
const runnerNote = computed(() => {
  const note = String((props.item.evaluation as { note?: string } | undefined)?.note
    ?? props.item.runner_note ?? '').trim()
  if (!note) return ''
  return machineNotePrefixes.some(prefix => note.startsWith(prefix)) ? '' : note
})
// The per-document breakdown is the real answer whenever more than one
// document was assessed; the flattened response loses which said what.
const perDocumentAnswers = computed(() => Object.entries(props.item.qa_answers ?? {}))
const hasAssessment = computed(() => Boolean(props.item.response) || perDocumentAnswers.value.length > 0)
// For a vouching item the comparison table *is* the assessment, so an empty
// narrative block would only claim the item had not run when it had.
const showAssessment = computed(() => hasAssessment.value || !props.item.checks?.length)
const coverage = computed(() => props.item.evidence_coverage)
const duplicates = computed(() => props.item.document_conflicts?.duplicate_documents ?? [])
const evidenceRequests = computed(() =>
  (props.test.evidence_requests ?? []).filter(request => request.item_id === props.item.id),
)
const isCanonicalCycle = computed(() => props.test.kind === 'cycle_vouch')
// The two readings the rail keeps apart: what the run found, and what the
// auditor decided about it. Only the second is ever editable here.
const evaluation = computed(() => (props.item.evaluation ?? {}) as Partial<DocTestEvaluation>)
const disposition = computed(() => (props.item.disposition ?? {}) as Partial<DocTestDisposition>)
const evaluationState = computed(() => String(evaluation.value.state ?? 'not_run'))
const dispositionState = computed<DocTestDispositionState>(
  () => (disposition.value.state ?? 'pending') as DocTestDispositionState,
)
const isStale = computed(() => Boolean(disposition.value.stale))
const hasRun = computed(() => !['not_run', 'stale'].includes(evaluationState.value))

// Every call stays available at all times. Gating them on the runner's verdict
// was what made a model-marked exception unchangeable: the buttons that would
// have overturned it were the ones hidden because it had a verdict at all.
const dispositionChoices = computed(() => {
  const choices: Array<{ value: DocTestDispositionState; label: string; icon: string; tone: string }> = [
    { value: 'confirmed', label: 'Confirm', icon: 'pi pi-check', tone: 'success' },
    { value: 'exception', label: 'Exception', icon: 'pi pi-exclamation-triangle', tone: 'danger' },
  ]
  // Parking is an item-first affordance; a cycle disposition stays binary.
  if (!isCanonicalCycle.value) {
    choices.push({ value: 'needs_review', label: 'Needs review', icon: 'pi pi-eye', tone: 'warn' })
  }
  return choices
})
// The runner's verdict read as the call an auditor would be making by agreeing
// with it — so we can tell agreement from disagreement without asking.
// Both shapes agree on these two; every other evaluation state is the runner
// declining to settle, which no disposition agrees or disagrees with.
const machineEquivalent = computed<DocTestDispositionState | null>(() =>
  ({ passed: 'confirmed', failed: 'exception' } as Record<string, DocTestDispositionState>)[
    evaluationState.value
  ] ?? null)
const noteDraft = ref('')
const editingNote = ref(false)
const reasonBox = ref<HTMLElement | null>(null)
// Whether the call on the file disagrees with what the run found. Worth saying
// so, and worth a written reason — but recording the call and writing up why
// are two acts, and making the first wait on the second only lost decisions.
const departsFromRun = computed(() =>
  Boolean(machineEquivalent.value)
  && dispositionState.value !== 'pending'
  && dispositionState.value !== machineEquivalent.value
  && !isStale.value,
)
// Offered only against a current call: re-recording one re-affirms it against
// today's evidence, which writing a note is not. A stale sign-off is re-made,
// not annotated.
const canNote = computed(() => dispositionState.value !== 'pending' && !isStale.value)
const canClearSignOff = computed(() => dispositionState.value !== 'pending')
// A finding drafted from a test nobody resolved is a finding about nothing. The
// backend now leaves such a test in `review_required` rather than `completed`,
// and this reads that rather than the old prefix check.
const canGenerateFinding = computed(() => props.test.status.startsWith('completed'))
const findingBlockedReason = computed(() => {
  if (canGenerateFinding.value || !props.test.rcm_id) return ''
  return props.test.status === 'review_required'
    ? 'Resolve every item on this test before drafting a finding from it.'
    : 'Run this test before drafting a finding from it.'
})

function cancelNote() {
  editingNote.value = false
  noteDraft.value = ''
}
/** Every call is one click. The reason, if there is one, comes after. */
function choose(value: DocTestDispositionState) {
  cancelNote()
  emit('setState', value)
}
async function beginNote() {
  editingNote.value = true
  noteDraft.value = String(disposition.value.note ?? '')
  await nextTick()
  const box = reasonBox.value
  // Focus first, then scroll: a smooth scroll started before focus gets
  // cancelled by it, which left the box open below the fold on a short window.
  // Instant, so it has landed by the time the auditor looks. Optional-chained
  // because jsdom implements neither.
  box?.querySelector('textarea')?.focus({ preventScroll: true })
  box?.scrollIntoView?.({ block: 'center' })
}
function saveNote() {
  // Re-records the same call carrying the note; the state itself is unchanged.
  emit('setState', dispositionState.value, noteDraft.value.trim() || undefined)
  cancelNote()
}
function signedOffLine(value: Partial<DocTestDisposition>) {
  const parts = [value.actor || 'auditor']
  if (value.at) parts.push(new Date(value.at).toLocaleString())
  return parts.join(' · ')
}
const cycleResults = computed(() => Object.entries(props.item.result_by_assertion ?? {}))

const cycleSpec = computed(() => (props.test.spec ?? {}) as Record<string, any>)
const cycleCoverage = computed(() => {
  const coverage = props.test.coverage ?? cycleSpec.value.coverage
  if (!coverage || typeof coverage !== 'object') return null
  return coverage as {
    population_rows: number
    selected_rows: number
    rows_with_evidence: number | null
    complete_cycles: number
    assurance_scope: string
  }
})
const cycleDocuments = computed<Array<{
  document_id: string
  role: string
  matched_by?: Array<Record<string, unknown>>
}>>(() => {
  return (props.item.role_bindings ?? []).map(binding => ({
    document_id: binding.document_id,
    role: binding.role,
    matched_by: binding.matched_by,
  }))
})
const missingRoles = computed(() => props.item.missing_roles ?? [])
const frozenRow = computed(() => Object.entries(props.item.frozen_row ?? props.item.frozen ?? {}))
const cycleConclusionEligible = computed(() => {
  if (!isCanonicalCycle.value) return true
  return Boolean(props.test.items.length) && props.test.items.every(item =>
    ['passed', 'failed'].includes(item.evaluation?.state ?? '')
    && ['confirmed', 'exception'].includes(item.disposition?.state ?? '')
    && !item.disposition?.stale,
  )
})
// A disclosure, not a bar. Concluding over items the runner never settled -
// including items nothing ran because the evidence never arrived - is the
// auditor's call; the backend records what was open as a scope limitation.
const controlConclusionReason = computed(() => {
  if (!isCanonicalCycle.value || cycleConclusionEligible.value) return ''
  return 'Not every item carries a complete evaluation and a current disposition. You can still conclude — it will be recorded as a scope limitation.'
})
// Item-first tests let the auditor conclude over open items — that judgment is
// theirs. Naming the items is what was missing: the save used to fail with a
// generic error that never said which item was holding it up.
const openItems = computed(() => {
  if (isCanonicalCycle.value) return []
  return (props.test.items ?? []).filter(item => {
    const disposition = item.disposition as { state?: string; stale?: boolean } | undefined
    const state = item.state ?? 'pending'
    return !['confirmed', 'exception'].includes(state) || Boolean(disposition?.stale)
  })
})
const concludedOverOpenItems = computed(() =>
  Boolean((props.test as { conclusion_override?: unknown }).conclusion_override))
// "Settled" is a live auditor call. A stale one is not settled — it falls back
// to the run, so the run leads again.
const settled = computed(() => dispositionState.value !== 'pending' && !isStale.value)
// The run's verdict as a past-tense clause, so the demoted line reads as a note
// about what happened rather than as a second live status.
const runVerdictPhrase = computed(() => ({
  passed: 'found no exception',
  failed: 'flagged an exception',
  inconclusive: 'could not settle this',
  agent_checked: 'had not finished settling this',
  not_run: 'has not been run',
  incomplete: 'could not complete',
  needs_review: 'left this for review',
  stale: 'is out of date',
}[evaluationState.value] ?? `recorded ${evaluationState.value}`))

function edgeLabel(edge: Record<string, unknown>) {
  return `${String(edge.identifier_kind ?? 'identifier')} = ${String(edge.normalized_value ?? '—')}`
}
function comparisonEvidence(comparison: Record<string, any>): EvidenceRef[] {
  return (comparison.entries ?? []).flatMap((entry: Record<string, any>) => entry.evidence_refs ?? [])
}
function comparisonRecordIds(comparison: Record<string, unknown>) {
  return Array.isArray(comparison.record_ids) ? comparison.record_ids.join(', ') : ''
}
function comparisonEntryCount(comparison: Record<string, unknown>) {
  return Array.isArray(comparison.entries) ? comparison.entries.length : 0
}

function documentTitle(id: string) {
  return props.documents.find(doc => doc.id === id)?.title || id
}
function attach() {
  if (!attachId.value) return
  emit('attach', attachId.value)
  attachId.value = null
}

async function focusAssertion() {
  if (!props.focusAssertionKey) return
  await nextTick()
  const target = Array.from(
    detailRoot.value?.querySelectorAll<HTMLElement>('[data-assertion-key]') ?? [],
  ).find(node => node.dataset.assertionKey === props.focusAssertionKey)
  if (!target) return
  target.focus({ preventScroll: true })
  target.scrollIntoView?.({ block: 'center' })
}

watch(() => props.focusAssertionKey, () => { void focusAssertion() })
onMounted(() => { void focusAssertion() })
</script>

<template>
  <div ref="detailRoot" class="detail">
    <!-- Identity only. The status, the run action, and everything the auditor
         records all live together in the rail. -->
    <header class="detail-head">
      <!-- The test title is the readable name of this work; the item label is
           a slug, so it identifies the specific check rather than heading it. -->
      <div class="head-copy">
        <p class="eyebrow">{{ kindLabel[test.kind ?? ''] ?? 'Document work' }} · {{ item.id }}</p>
        <h3>{{ test.title || item.label || item.id }}</h3>
        <p v-if="item.label && item.label !== test.title" class="context">{{ item.label }}</p>
      </div>
      <Button
        v-if="test.rcm_id"
        :label="test.rcm_id"
        icon="pi pi-map"
        size="small"
        outlined
        class="rcm-link"
        @click="emit('openRcm', test.rcm_id)"
      />
      <p v-else class="unlinked">Not linked to an RCM row — this work does not count as coverage.</p>
    </header>

    <!-- The record: what the procedure was and what the run found. -->
    <div class="detail-main">
    <p v-if="runnerNote" class="runner-note"><i class="pi pi-info-circle" />{{ runnerNote }}</p>

    <!-- `instruction` and `question` are one planned step written twice: the
         runner emits an imperative and an interrogative form of the same
         sentence. The old `question !== instruction` guard never fired — the
         two strings are never byte-identical, so every item rendered both.
         Nor does a similarity threshold separate them: the pair that reads
         most obviously as duplicated shares only 52% of its words, and the
         pair sharing 33% still asks the same thing, so any threshold low
         enough to suppress the first fires on all of them. State the
         procedure once; keep the question one disclosure away. -->
    <section class="block">
      <h4>Procedure</h4>
      <p class="instruction">{{ item.instruction || 'No instruction was recorded for this item.' }}</p>
      <UiAdvancedSection
        v-if="item.question"
        title="Question put to the model"
        description="The instruction as the runner phrased it"
      >
        <p class="question">{{ item.question }}</p>
      </UiAdvancedSection>
    </section>

    <section v-if="showAssessment" class="block" :data-empty="!hasAssessment">
      <h4>Assessment</h4>
      <template v-if="perDocumentAnswers.length">
        <article v-for="[documentId, answer] in perDocumentAnswers" :key="documentId" class="answer">
          <div class="answer-head">
            <strong>{{ documentTitle(documentId) }}</strong>
            <UiTestStatus :status="answer.outcome" showLabel />
          </div>
          <p>{{ answer.answer }}</p>
          <div v-if="answer.citations?.length" class="citations">
            <Button
              v-for="citation in answer.citations"
              :key="citation.id"
              :label="`Page ${citation.page || '—'}`"
              icon="pi pi-link"
              size="small"
              text
              @click="emit('anchor', citation)"
            />
          </div>
        </article>
      </template>
      <template v-else-if="item.response">
        <p class="response">{{ item.response }}</p>
        <div v-if="item.citations?.length" class="citations">
          <Button
            v-for="citation in item.citations"
            :key="citation.id"
            :label="`Page ${citation.page || '—'}`"
            icon="pi pi-link"
            size="small"
            text
            @click="emit('anchor', citation)"
          />
        </div>
      </template>
      <p v-else class="muted">Not executed yet. Run the test to record a cited assessment.</p>
      <blockquote v-if="item.excerpt">{{ item.excerpt }}</blockquote>
    </section>

    <!-- 2a. The transaction cycle: which documents stood in for which role. -->
    <section v-if="cycleDocuments.length" class="block">
      <h4>Transaction cycle</h4>
      <p v-if="missingRoles.length" class="missing-roles">
        No attached document fills the required role{{ missingRoles.length > 1 ? 's' : '' }}
        <strong>{{ missingRoles.join(', ') }}</strong>, so this item cannot be concluded.
      </p>
      <div v-for="entry in cycleDocuments" :key="entry.document_id" class="role-row">
        <span class="role-name">{{ entry.role }}</span>
        <span class="role-doc">{{ documentTitle(entry.document_id) }}</span>
        <small v-if="Array.isArray(entry.matched_by) && entry.matched_by.length" class="matched-chain">
          {{ entry.matched_by.map(edgeLabel).join(' → ') }}
        </small>
      </div>
      <div v-if="item.role_conflicts?.length || item.collisions?.length" class="conflict">
        <strong><i class="pi pi-exclamation-triangle" />Role binding requires review</strong>
        <span>{{ plural(item.role_conflicts?.length ?? 0, 'within-item conflict') }} · {{ plural(item.collisions?.length ?? 0, 'cross-item collision') }}</span>
      </div>
      <UiAdvancedSection title="Population record" description="The frozen row this cycle is tested against">
        <div class="frozen">
          <span v-for="[field, value] in frozenRow" :key="field">
            {{ field }}: <code>{{ value ?? '—' }}</code>
          </span>
        </div>
      </UiAdvancedSection>
    </section>

    <section v-if="cycleResults.length" class="block">
      <h4>Assertion results</h4>
      <article
        v-for="[key, result] in cycleResults"
        :key="key"
        class="check"
        :class="{ 'focused-assertion': focusAssertionKey === key }"
        :data-assertion-key="key"
        tabindex="-1"
      >
        <div class="check-head">
          <!-- The assertion's own label lives on the approved ruleset. The
               key is what the result was recorded under, and is what the
               auditor can match back to the rules review. -->
          <strong>{{ key }}</strong>
          <UiTestStatus :status="result.stale ? 'stale' : result.verdict" showLabel />
        </div>
        <p v-if="result.display" class="check-note">{{ result.display }}</p>
        <div
          v-for="(comparison, comparisonIndex) in result.comparisons"
          :key="`${key}:${comparisonIndex}`"
          class="comparison cycle-result-comparison"
        >
          <span class="comparison-source">
            {{ comparison.role ?? comparison.side ?? 'Comparison' }}
            <template v-if="comparison.document_id"> · {{ documentTitle(String(comparison.document_id)) }}</template>
          </span>
          <code>{{ comparison.verdict ?? comparison.state ?? '—' }}</code>
          <span class="path">
            {{ comparisonRecordIds(comparison) || plural(comparisonEntryCount(comparison), 'value') }}
          </span>
          <div class="citations">
            <Button
              v-for="anchor in comparisonEvidence(comparison)"
              :key="anchor.id"
              :label="`Page ${anchor.page ?? '—'}`"
              icon="pi pi-link"
              size="small"
              text
              @click="emit('anchor', anchor)"
            />
          </div>
        </div>
      </article>
    </section>

    <!-- 2b. Comparison detail, for the vouching branch only. -->
    <section v-if="item.checks?.length" class="block">
      <h4>Comparisons</h4>
      <article v-for="check in item.checks" :key="check.field" class="check">
        <div class="check-head">
          <strong>{{ check.field }}</strong>
          <UiTestStatus :status="check.verdict" showLabel />
        </div>

        <!-- Literal expectation matched against page text for simple vouching. -->
        <div
          v-for="result in check.comparisons"
          :key="`${result.document_id}:${result.page}`"
          class="comparison"
        >
          <span class="comparison-source">{{ documentTitle(result.document_id) }} · page {{ result.page || '—' }}</span>
          <code>{{ result.expected }} ↔ {{ result.found ?? 'missing' }}</code>
          <UiTestStatus :status="result.result" showLabel />
          <Button
            v-if="result.evidence"
            icon="pi pi-link"
            text
            rounded
            size="small"
            aria-label="Open evidence"
            @click="emit('anchor', result.evidence)"
          />
        </div>
        <UiAdvancedSection title="Matching rule" description="Change how this field is compared">
          <div class="comparison-settings">
            <span>Expected: <code>{{ check.expected }}</code></span>
            <Select v-model="check.method" :options="methods" />
            <InputText
              :modelValue="String(check.tolerance ?? '')"
              placeholder="Tolerance"
              @update:modelValue="check.tolerance = $event"
            />
          </div>
        </UiAdvancedSection>
      </article>
      <Button label="Save matching rules" icon="pi pi-save" size="small" outlined @click="emit('saveChecks')" />
    </section>

    <section v-if="item.attributes?.length" class="block">
      <h4>Attributes</h4>
      <article v-for="attribute in item.attributes" :key="attribute.name" class="attribute">
        <strong>{{ attribute.name }}</strong>
        <UiTestStatus :status="attribute.verdict" showLabel />
        <InputText v-model="attribute.note" placeholder="Auditor note" />
      </article>
      <Button label="Save attribute notes" icon="pi pi-save" size="small" outlined @click="emit('saveAttributes')" />
    </section>

    <!-- 3. What the run produced. The auditor's own conclusion is in the rail.
         This sits above Evidence because it is what the reader came for: the
         attachment list is the basis for the result, not the finish of it. -->
    <section class="block outcome">
      <h4>Result</h4>
      <p v-if="test.result_summary" class="summary">{{ test.result_summary }}</p>
      <!-- Coverage is what makes a cycle conclusion honest: how much of the
           population was actually reached, stated beside the result. -->
      <p v-if="cycleCoverage" class="coverage">
        Selected <strong>{{ cycleCoverage.selected_rows }}</strong> of
        <strong>{{ cycleCoverage.population_rows }}</strong> {{ pluralWord(cycleCoverage.population_rows, 'population row') }} from
        <code>{{ test.definition?.population.table }}.{{ test.definition?.population.column }}</code>.
        <span>{{ cycleCoverage.assurance_scope === 'sampled_population' ? 'Sampled population' : 'Targeted evidence — not a sample' }}.</span>
        <span v-if="cycleCoverage.rows_with_evidence !== null">
          {{ plural(cycleCoverage.rows_with_evidence, 'row') }} {{ verb(cycleCoverage.rows_with_evidence, 'has', 'have') }} linked evidence and {{ plural(cycleCoverage.complete_cycles, 'complete cycle') }} {{ verb(cycleCoverage.complete_cycles, 'was', 'were') }} identified.
        </span>
      </p>
      <dl v-if="test.next_action || test.scope_limitations || test.exception_count || test.open_exception_count">
        <template v-if="test.next_action">
          <dt>Next action</dt><dd>{{ test.next_action }}</dd>
        </template>
        <template v-if="test.scope_limitations">
          <dt>Limitation</dt><dd>{{ test.scope_limitations }}</dd>
        </template>
        <template v-if="test.exception_count || test.open_exception_count">
          <dt>Exceptions</dt><dd>{{ test.exception_count }} recorded · {{ test.open_exception_count }} open</dd>
        </template>
      </dl>
      <p v-if="!test.result_summary && !cycleCoverage" class="muted">
        No test-level result has been recorded yet.
      </p>
    </section>

    <!-- 4. Evidence: what is attached and what is still missing. -->
    <section class="block">
      <h4>Evidence</h4>
      <div v-if="item.document_ids.length" class="attached">
        <span v-for="documentId in item.document_ids" :key="documentId" class="doc-chip">
          <i class="pi pi-file" />{{ documentTitle(documentId) }}
        </span>
      </div>
      <p v-else class="muted">No document is attached to this item.</p>

      <div v-if="coverage && (coverage.missing_document_types.length || coverage.image_only)" class="gap">
        <strong><i class="pi pi-exclamation-triangle" />Evidence gap</strong>
        <span v-if="coverage.missing_document_types.length">
          Missing: {{ coverage.missing_document_types.join(', ') }}
        </span>
        <span v-if="coverage.image_only">Scanned image only — requires manual reading or OCR.</span>
        <span v-if="item.evidence_request_ids?.length">
          {{ plural(item.evidence_request_ids.length, 'evidence request') }} raised.
        </span>
      </div>

      <div v-if="evidenceRequests.length" class="evidence-requests">
        <article
          v-for="request in evidenceRequests"
          :key="request.id"
          class="evidence-request"
          :data-status="request.status"
        >
          <div class="evidence-request-head">
            <strong>{{ request.status === 'open' ? 'Open evidence request' : `Evidence request ${request.status}` }}</strong>
            <small>{{ request.id }}</small>
          </div>
          <p>{{ request.reason }}</p>
          <small v-if="request.next_action" class="muted">{{ request.next_action }}</small>
          <div v-if="request.status === 'open'" class="evidence-request-actions">
            <Button
              label="Clear request"
              icon="pi pi-check-circle"
              size="small"
              severity="success"
              outlined
              @click="emit('updateEvidenceRequest', request.id, 'cancelled')"
            />
            <Button
              label="Mark received"
              icon="pi pi-check"
              size="small"
              severity="secondary"
              text
              @click="emit('updateEvidenceRequest', request.id, 'received')"
            />
          </div>
        </article>
      </div>

      <div v-if="duplicates.length" class="conflict">
        <strong><i class="pi pi-copy" />Duplicate evidence attached</strong>
        <span>Resolve the duplication before accepting this item.</span>
      </div>

      <div v-if="!isCanonicalCycle" class="attach">
        <Select
          v-model="attachId"
          :options="attachable"
          optionLabel="label"
          optionValue="value"
          filter
          placeholder="Attach another document"
        />
        <Button label="Attach" icon="pi pi-paperclip" outlined :disabled="!attachId" @click="attach" />
      </div>
    </section>

    </div>

    <!-- The rail: everything the auditor decides, in one column that stays put
         while the record beside it scrolls. -->
    <aside class="detail-rail" aria-label="Your assessment">
      <!-- Both readings stay on the record, but they are not equals. Until a
           call is made the run's verdict *is* the status and leads; once the
           auditor has settled it, theirs leads and the run demotes to the
           historical note it has become. Two identical chips read as two
           competing live statuses, which is what made a signed-off item still
           look unresolved. -->
      <div class="rail-group rail-status">
        <dl v-if="settled" class="readings readings--settled">
          <div class="reading">
            <dt>Your call</dt>
            <dd><UiTestStatus :status="isStale ? 'stale' : dispositionState" showLabel /></dd>
          </div>
          <div class="reading reading--muted">
            <dd>The run {{ runVerdictPhrase }}.</dd>
          </div>
        </dl>
        <dl v-else class="readings">
          <div class="reading">
            <dt>Run result</dt>
            <dd><UiTestStatus :status="evaluationState" showLabel /></dd>
          </div>
          <div class="reading">
            <dt>Your call</dt>
            <dd><span class="reading-empty">Not recorded</span></dd>
          </div>
        </dl>
        <Button
          label="Run test"
          icon="pi pi-play"
          size="small"
          :loading="running"
          :disabled="busy"
          @click="emit('run')"
        />
      </div>

      <!-- The auditor's call is always available, whatever the runner found.
           Disagreeing with a verdict is a normal audit act; it records the
           disagreement rather than rewriting the run. -->
      <div class="rail-group">
        <h4>Your call</h4>

        <p v-if="isStale" class="rail-note rail-stale">
          <i class="pi pi-history" />
          The evidence or procedure changed after this sign-off, so it no longer
          counts as current. Re-run the item, then record your call again.
        </p>
        <p v-else-if="dispositionState !== 'pending'" class="rail-note rail-provenance">
          <i class="pi pi-user" />
          {{ signedOffLine(disposition) }}
        </p>
        <p v-else-if="!hasRun" class="rail-note">
          This item has not been run yet. You can still record a call, but there
          is no result behind it.
        </p>
        <p v-else class="rail-note">
          {{ machineEquivalent
            ? 'The run reached a verdict. Agree with it, or record a different call.'
            : 'The run could not settle this item — it is yours to decide.' }}
        </p>

        <p v-if="disposition.note" class="rail-note rail-reason">“{{ disposition.note }}”</p>

        <div class="dispositions">
          <Button
            v-for="choice in dispositionChoices"
            :key="choice.value"
            :label="choice.label"
            :icon="choice.icon"
            size="small"
            :severity="choice.tone"
            :outlined="dispositionState !== choice.value || isStale"
            :disabled="busy"
            :class="{ 'is-current': dispositionState === choice.value && !isStale }"
            :aria-pressed="dispositionState === choice.value && !isStale"
            @click="choose(choice.value)"
          />
        </div>

        <!-- The reason is prompted where it is missed, and written whenever the
             auditor gets to it — never as a toll on recording the call. -->
        <p v-if="departsFromRun && !disposition.note" class="rail-note rail-prompt">
          <i class="pi pi-pencil" />
          This departs from the run. A written reason is worth having on the file.
        </p>

        <template v-if="editingNote">
          <label ref="reasonBox" class="reason-label">
            {{ departsFromRun ? 'Why you disagree with the run' : 'Note' }} (optional)
            <Textarea
              v-model="noteDraft"
              rows="2"
              autoResize
              :placeholder="departsFromRun
                ? 'The run says otherwise — record what you saw that it did not.'
                : 'Anything worth leaving on the file.'"
            />
          </label>
          <div class="dispositions">
            <Button label="Save reason" icon="pi pi-save" size="small" :disabled="busy" @click="saveNote" />
            <Button label="Cancel" size="small" text :disabled="busy" @click="cancelNote" />
          </div>
        </template>
        <Button
          v-else-if="canNote"
          :label="disposition.note ? 'Edit reason' : 'Add a reason'"
          icon="pi pi-pencil"
          size="small"
          text
          :disabled="busy"
          @click="beginNote"
        />

        <Button
          label="Clear my call"
          icon="pi pi-refresh"
          size="small"
          text
          :disabled="busy || !canClearSignOff"
          @click="emit('setState', 'pending')"
        />
        <p class="rail-note rail-footnote">
          Clearing your call leaves the run's own verdict standing; it does not
          discard the result.
        </p>
      </div>

      <div class="rail-group">
        <h4>Your conclusion</h4>
        <label>
          Control conclusion
          <Select
            v-model="test.control_conclusion"
            :options="controlConclusions"
            optionLabel="label"
            optionValue="value"
            class="control-conclusion-select"
          />
        </label>
        <p v-if="controlConclusionReason" class="rail-note assurance-restriction">
          {{ controlConclusionReason }}
        </p>
        <!-- A warning, not a block. Saving over open items is allowed; the
             backend appends what was open to this test's scope limitations. -->
        <div v-if="openItems.length" class="rail-note open-items">
          <p>
            <i class="pi pi-exclamation-circle" />
            {{ openItems.length }} of {{ test.items.length }}
            {{ test.items.length === 1 ? 'item is' : 'items are' }} unresolved.
            You can still conclude — it will be recorded as a scope limitation.
          </p>
          <ul>
            <li v-for="item in openItems" :key="item.id">{{ item.label || item.id }}</li>
          </ul>
        </div>
        <p v-else-if="concludedOverOpenItems" class="rail-note open-items-cleared">
          <i class="pi pi-check" />
          Every item is resolved. Save the conclusion again to clear the recorded
          scope limitation.
        </p>
        <label>
          Conclusion
          <Textarea
            v-model="test.conclusion"
            rows="3"
            autoResize
            placeholder="What this result means for the control, in your own words."
          />
        </label>
        <Button
          label="Save conclusion"
          icon="pi pi-check"
          size="small"
          outlined
          :disabled="busy"
          @click="emit('saveConclusion')"
        />
      </div>

      <div class="rail-group">
        <h4>Finding{{ findings.length > 1 ? 's' : '' }}</h4>
        <template v-if="findings.length">
          <Button
            v-for="finding in findings"
            :key="finding.id"
            :label="`Open ${finding.id}`"
            icon="pi pi-arrow-up-right"
            size="small"
            outlined
            @click="emit('openFinding', finding.id)"
          />
          <Button label="Regenerate" icon="pi pi-refresh" size="small" severity="secondary" :disabled="busy || !test.rcm_id || !canGenerateFinding" @click="emit('generateFinding', true)" />
        </template>
        <template v-else>
          <p class="rail-note">
            {{ findingBlockedReason || 'Generate a draft from this test’s exception observations.' }}
          </p>
          <Button label="Generate finding" icon="pi pi-sparkles" size="small" :disabled="busy || !test.rcm_id || !canGenerateFinding" @click="emit('generateFinding', false)" />
        </template>
      </div>

      <!-- Provenance belongs to the test definition, not to the item: the
           agent wrote the test once and every item inherits it. -->
      <ProvenanceRail
        v-if="workspaceId"
        :key="test.id"
        :workspaceId="workspaceId"
        :artifactRef="`doctest:${test.id}`"
      />
    </aside>
  </div>
</template>

<style scoped>
/* The detail column is one panel, not a stack of them. Sections inside it are
   separated by a rule and whitespace; only the repeated records inside a
   section (an answer, a check, an evidence request) still take a fill. That
   keeps the nesting to one level instead of the three it had. */
/* `align-content: start` is load-bearing: `min-height: 100%` makes this grid
   taller than its rows, and the default stretch would pour that slack into the
   header row instead of leaving it at the bottom. */
.detail { display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; gap: var(--aw-space-4); min-width: 0; min-height: 100%; padding: 1rem; border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--aw-space-4); min-width: 0; }
.head-copy { min-width: 0; }
.eyebrow { margin: 0; }
.detail-head h3 { margin: 0.15rem 0 0.25rem; font-size: var(--aw-text-lg); line-height: 1.3; }
.context { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); font-family: var(--aw-font-mono); }
.rcm-link { flex: 0 0 auto; }
.unlinked { flex: 0 1 16rem; margin: 0; color: var(--aw-warn); font-size: var(--aw-text-sm); text-align: right; }

/* The record column is its own container: the panels inside it size against
   the space they actually have, which is now roughly a rail narrower than
   the detail column itself. */
.detail-main { min-width: 0; container: detail-main / inline-size; }

/* Tinted so "what I decided" separates from "what the run found" without
   another border. Below the three-column breakpoint it simply stacks under
   the record, still as one group. */
.detail-rail { display: flex; flex-direction: column; gap: 0.9rem; min-width: 0; padding: 0.9rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.rail-group { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
.rail-group + .rail-group { padding-top: 0.9rem; border-top: 1px solid var(--aw-border-strong); }
.rail-group h4 { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.rail-status { align-items: flex-start; }
.rail-note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.4; }
.detail-rail :deep(.p-button) { width: 100%; justify-content: center; }
.dispositions { display: flex; flex-direction: column; gap: 0.4rem; }

/* Run result above your call, each with its own label: the pair is the whole
   point of the rail, so neither reading is allowed to stand for the other. */
.readings { display: flex; flex-direction: column; gap: 0.45rem; width: 100%; margin: 0; }
.reading { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
.reading dt { color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
.reading dd { margin: 0; min-width: 0; }
.reading-empty { color: var(--aw-muted); font-size: var(--aw-text-sm); font-style: italic; }
/* The demoted run verdict: prose, not a chip, so it cannot be mistaken for the
   item's current status. */
.readings--settled { gap: 0.2rem; }
.reading--muted dd { color: var(--aw-muted); font-size: var(--aw-text-xs); }

.rail-note.rail-stale,
.rail-note.rail-provenance,
.rail-note.rail-prompt,
.rail-note.rail-reason { display: flex; align-items: baseline; gap: 0.35rem; }
.rail-note.rail-stale { color: var(--aw-warn); }
.rail-note.rail-prompt { color: var(--aw-muted); }
.rail-note.rail-reason { color: var(--aw-ink); font-style: italic; }
.rail-note.rail-footnote { font-size: var(--aw-text-xs); }

/* Warn-toned, not danger-toned: this is a disclosure the auditor is entitled to
   make, not an error they have to clear. */
.open-items { color: var(--aw-warn); }
.open-items p { display: flex; align-items: baseline; gap: 0.35rem; margin: 0; }
.open-items ul { margin: 0.3rem 0 0; padding-left: 1.6rem; color: var(--aw-ink); font-size: var(--aw-text-xs); }
.open-items-cleared { display: flex; align-items: baseline; gap: 0.35rem; color: var(--aw-ok); }
.reason-label { display: flex; flex-direction: column; gap: 0.25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }

/* The call already on the file reads as filled rather than outlined, so the
   current decision is visible without opening the note. */
.dispositions :deep(.p-button.is-current) { box-shadow: inset 0 0 0 2px currentColor; }

/* 42rem, measured: at a 1440 window with the assistant docked the detail
   column is ~44rem, so a higher threshold never engaged where it matters. */
@container master-detail-content (min-width: 42rem) {
  .detail { grid-template-columns: minmax(0, 1fr) 13rem; }
  .detail-head { grid-column: 1 / -1; }
  /* Sticky against the surface panel, so the conclusion and the sign-off stay
     reachable however long the comparison list runs. */
  .detail-rail { position: sticky; top: 0; align-self: start; }
}

/* A muted 0.72rem heading sitting directly on body copy of the same weight did
   not read as a section break. The heading now takes ink colour and the body
   size, and the rule gets room to breathe on both sides. */
/* Measured: at `--aw-text-base` the heading was 14px/700 against body copy at
   14px/400 in a near-identical ink, so weight alone had to carry the section
   break. `--aw-text-md` is the one step between body and the panel title, which
   gives the heading its own tier without introducing a size the scale does not
   already define. */
.block { display: flex; flex-direction: column; gap: 0.55rem; min-width: 0; padding: 1.15rem 0 0.35rem; border-top: 1px solid var(--aw-border); }
.block h4 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 700; letter-spacing: -0.01em; }
.instruction { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.question { margin: 0; color: var(--aw-ink); font-size: var(--aw-text-base); line-height: 1.5; }
.response { margin: 0; font-size: var(--aw-text-base); line-height: 1.55; }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.answer { display: flex; flex-direction: column; gap: 0.3rem; padding: 0.6rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.answer-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.answer p { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.citations { display: flex; flex-wrap: wrap; gap: 0.2rem; }
blockquote { margin: 0; padding: 0.7rem 0.8rem; border-left: 3px solid var(--aw-teal); background: var(--aw-raised); font-size: var(--aw-text-sm); line-height: 1.5; }
.runner-note { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0 0 0.15rem; padding: 0.55rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-info-soft); font-size: var(--aw-text-sm); }

.check { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.65rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.check.focused-assertion { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.check-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto auto; gap: 0.5rem; align-items: center; padding: 0.4rem 0; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); }
.comparison-source { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comparison-settings { display: grid; grid-template-columns: minmax(0, 1fr) 11rem 8rem; gap: 0.5rem; align-items: center; }
/* The path is the audit trail for a cycle comparison: it says exactly which
   field of which record produced the value beside it. */
.path { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--aw-muted); font-family: var(--aw-mono, monospace); font-size: var(--aw-text-xs); }
.unresolved { color: var(--aw-warn); }
.check-note { margin: 0.2rem 0 0; color: var(--aw-warn); font-size: var(--aw-text-sm); }
.role-row { display: grid; grid-template-columns: 10rem minmax(0, 1fr) auto; gap: 0.5rem; align-items: center; padding: 0.3rem 0; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); }
.matched-chain { grid-column: 2 / -1; color: var(--aw-muted); overflow-wrap: anywhere; }
.cycle-result-comparison { grid-template-columns: minmax(9rem, 0.8fr) minmax(7rem, 0.6fr) minmax(0, 1fr) auto; }
.role-name { font-weight: 700; }
.role-doc { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.role-type { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.missing-roles { margin: 0 0 0.4rem; color: var(--aw-warn); font-size: var(--aw-text-sm); }
.coverage { margin: 0 0 0.5rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.frozen { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; font-size: var(--aw-text-sm); color: var(--aw-muted); }
code { font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); overflow-wrap: anywhere; }
.attribute { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1.4fr); gap: 0.5rem; align-items: center; padding: 0.5rem 0.6rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }

.attached { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.doc-chip { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.2rem 0.5rem; border-radius: var(--aw-radius-pill); background: var(--aw-teal-soft); color: var(--aw-teal); font-size: var(--aw-text-xs); font-weight: 600; }
.gap, .conflict { display: flex; flex-direction: column; gap: 0.2rem; padding: 0.6rem 0.7rem; border-radius: var(--aw-radius-control); font-size: var(--aw-text-sm); }
.gap { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.conflict { background: var(--aw-danger-soft); color: var(--aw-danger); }
.gap strong, .conflict strong { display: flex; align-items: center; gap: 0.35rem; }
.evidence-requests { display: grid; gap: 0.45rem; margin-top: 0.05rem; }
.evidence-request { display: grid; gap: 0.25rem; padding: 0.55rem 0.65rem; border-left: 3px solid var(--aw-warn); border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0; background: var(--aw-raised); }
.evidence-request[data-status='received'], .evidence-request[data-status='cancelled'] { border-left-color: var(--aw-ok); opacity: 0.8; }
.evidence-request-head { display: flex; justify-content: space-between; gap: 0.5rem; }
.evidence-request-head small { color: var(--aw-muted); font-family: var(--aw-font-mono); }
.evidence-request p { margin: 0; font-size: var(--aw-text-sm); line-height: 1.35; }
.evidence-request-actions { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.2rem; }
.attach { display: flex; align-items: center; gap: 0.5rem; }
.attach :deep(.p-select) { flex: 1; min-width: 0; }

.outcome .summary { margin: 0; font-size: var(--aw-text-base); font-weight: 600; }
.outcome p { margin: 0; font-size: var(--aw-text-sm); line-height: 1.5; }
.outcome dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.25rem 0.7rem; margin: 0; font-size: var(--aw-text-sm); }
.outcome dt { color: var(--aw-muted); font-weight: 600; }
.outcome dd { margin: 0; }

label { display: flex; flex-direction: column; gap: 0.3rem; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label :deep(.p-select), label :deep(.p-textarea) { width: 100%; }

/* PrimeVue leaves its control text at the browser default, so any control
   without an explicit `size` rendered at 16px — which made the "Attach another
   document" placeholder the largest text in the Evidence block, larger than the
   heading above it and larger than the document it attaches to. Binding the
   controls to the panel's body size removes that tier from the panel entirely
   rather than leaving a sixth size that belongs to no one. */
/* The rule has to name the control as well as its label: a button with no
   `size` prop keeps 16px on the host element even once the label span is
   bound, which is what left "Attach" a size larger than everything near it. */
.detail :deep(.p-button),
.detail :deep(.p-button-label),
.detail :deep(.p-select),
.detail :deep(.p-select-label),
.detail :deep(.p-inputtext),
.detail :deep(.p-textarea) { font-size: var(--aw-text-base); }
/* `<small>` in the disclosure summary is unsized globally, so it resolved to
   0.8em of whatever it inherited — an eighth size that belongs to no tier. */
.detail :deep(.ui-advanced > summary small) { font-size: var(--aw-text-xs); }

/* Sized against the record column, not the whole detail column: the rail
   takes width out of it, so a query keyed to the outer column fired late. */
@container detail-main (max-width: 30rem) {
  .comparison, .comparison-settings, .attribute { grid-template-columns: minmax(0, 1fr); }
  .outcome dl { grid-template-columns: minmax(0, 1fr); }
  .outcome dt { margin-top: 0.3rem; }
}
</style>
