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
import UiVerdictBar from '../ui/UiVerdictBar.vue'
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
// Short labels: the field is already called "Conclusion", so repeating
// "Control" in every option only spent width the footer row does not have.
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
// the same fact the verdict bar already carries, in the runner's vocabulary
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
// The two readings the verdict bar keeps apart: what the run found, and what
// the auditor decided about it. Only the second is ever editable here.
const evaluation = computed(() => (props.item.evaluation ?? {}) as Partial<DocTestEvaluation>)
const disposition = computed(() => (props.item.disposition ?? {}) as Partial<DocTestDisposition>)
const evaluationState = computed(() => String(evaluation.value.state ?? 'not_run'))
const dispositionState = computed<DocTestDispositionState>(
  () => (disposition.value.state ?? 'pending') as DocTestDispositionState,
)
const isStale = computed(() => Boolean(disposition.value.stale))
const hasRun = computed(() => !['not_run', 'stale'].includes(evaluationState.value))

/* ---- The verdict bar ---------------------------------------------------- */

/** What the run found, in its own voice: a fact about the evidence. */
const RUN_LEAD: Record<string, string> = {
  passed: 'The run found no exception',
  failed: 'The run found an exception',
  inconclusive: 'The run could not settle this',
  agent_checked: 'The run has not finished settling this',
  incomplete: 'The run could not complete this',
  needs_review: 'The run left this for review',
  not_run: 'This item has not been run',
  stale: 'The run behind this item is out of date',
}
const runLead = computed(() => RUN_LEAD[evaluationState.value]
  ?? `The run recorded ${evaluationState.value.replaceAll('_', ' ')}`)
const verdictTone = computed<'ok' | 'warn' | 'bad' | 'neutral'>(() => ({
  passed: 'ok' as const,
  failed: 'bad' as const,
  inconclusive: 'warn' as const,
  agent_checked: 'warn' as const,
  incomplete: 'warn' as const,
  needs_review: 'warn' as const,
}[evaluationState.value] ?? 'neutral'))
const ranAt = computed(() => (evaluation.value.ran_at
  ? new Date(evaluation.value.ran_at).toLocaleString()
  : null))
/** Only a live call counts. A stale one stands on the record, not as current. */
const settled = computed(() => dispositionState.value !== 'pending' && !isStale.value)
const CALL_PHRASE: Record<string, string> = {
  confirmed: 'You confirmed this',
  exception: 'You recorded an exception',
  needs_review: 'You left this for review',
}
const calledOn = computed(() => (disposition.value.at
  ? new Date(disposition.value.at).toLocaleDateString()
  : null))
const staleSentence = computed(() => (isStale.value
  ? 'The evidence or procedure changed after your sign-off, so it no longer counts '
    + 'as current. Run the item again, then record your call.'
  : undefined))

// Every call stays available at all times. Gating them on the runner's verdict
// was what made a model-marked exception unchangeable: the buttons that would
// have overturned it were the ones hidden because it had a verdict at all.
const dispositionChoices = computed(() => {
  const choices: Array<{ value: DocTestDispositionState; label: string; icon: string; tone: string }> = [
    { value: 'confirmed', label: 'Confirm', icon: 'pi pi-check', tone: 'ok' },
    { value: 'exception', label: 'Exception', icon: 'pi pi-exclamation-triangle', tone: 'bad' },
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
  && !isStale.value)
// Offered only against a current call: re-recording one re-affirms it against
// today's evidence, which writing a note is not. A stale sign-off is re-made,
// not annotated.
const canNote = computed(() => dispositionState.value !== 'pending' && !isStale.value)
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

/* ---- The footer row ----------------------------------------------------- */

/**
 * The conclusion is per test and the page carries one test at a time, so Save
 * appears only once the select has actually moved. A permanent Save beside an
 * untouched select is a control that does nothing, and this row has room for
 * exactly one thing that does.
 */
const savedConclusion = ref(props.test.control_conclusion)
// A new revision from the server — a save, or a reload — is by definition what
// is on the file. A change *without* one is the auditor moving the select, and
// that is the only thing Save exists for.
watch([() => props.test.id, () => props.test.sha1], () => {
  savedConclusion.value = props.test.control_conclusion
})
const conclusionChanged = computed(() => props.test.control_conclusion !== savedConclusion.value)
const provenanceOpen = ref(false)

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
    <!-- Identity, and the two things done to the item rather than recorded
         about it. Everything the auditor records is on the verdict bar. -->
    <header class="detail-head">
      <div class="head-copy">
        <p class="eyebrow">
          {{ kindLabel[test.kind ?? ''] ?? 'Document work' }} · <span class="item-id">{{ item.id }}</span>
        </p>
        <h2>{{ item.label || test.title || item.id }}</h2>
        <p v-if="test.title && test.title !== item.label" class="context">Test: {{ test.title }}</p>
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
      <Button
        label="Run test"
        icon="pi pi-play"
        size="small"
        outlined
        severity="secondary"
        :loading="running"
        :disabled="busy"
        @click="emit('run')"
      />
    </header>

    <!-- Replaced wholesale while a bulk selection is live: one call across
         many items is the same decision, made somewhere else. -->
    <slot name="verdict">
      <UiVerdictBar :tone="verdictTone" :stale="staleSentence">
        <template #found>
          <span>{{ runLead }}</span>
          <span class="run-meta aw-figure">
            <template v-if="test.open_exception_count">· {{ test.open_exception_count }} open </template>
            <template v-if="ranAt">· run {{ ranAt }}</template>
          </span>
        </template>

        <template #recorded>
          <template v-if="!settled">
            Your call is not recorded.
            <template v-if="!isStale && hasRun"> Agree with the run, or record a different one.</template>
          </template>
          <template v-else>
            {{ CALL_PHRASE[dispositionState] ?? 'You recorded a call' }}<template v-if="calledOn"> on {{ calledOn }}</template>.
            <span v-if="disposition.note" class="quoted">“{{ disposition.note }}”</span>
          </template>
        </template>

        <template #actions>
          <div class="call">
            <!-- One control with three positions, not three buttons: the
                 calls are mutually exclusive and a row of equal buttons never
                 said which one was in force. -->
            <span class="dispositions" role="group" aria-label="Record your call">
              <button
                v-for="choice in dispositionChoices"
                :key="choice.value"
                type="button"
                :data-tone="choice.tone"
                :aria-pressed="dispositionState === choice.value && !isStale"
                :disabled="busy"
                @click="choose(choice.value)"
              >
                <i :class="choice.icon" aria-hidden="true" />{{ choice.label }}
              </button>
            </span>
            <p v-if="canNote" class="call-links">
              <button type="button" class="link" :disabled="busy" @click="beginNote">
                {{ disposition.note ? 'Edit reason' : 'Add a reason' }}
              </button>
              <button type="button" class="link" :disabled="busy" @click="emit('setState', 'pending')">
                Clear my call
              </button>
            </p>
          </div>
        </template>
      </UiVerdictBar>
    </slot>

    <!-- The reason is prompted where it is missed, and written whenever the
         auditor gets to it — never as a toll on recording the call. -->
    <p v-if="departsFromRun && !disposition.note && !editingNote" class="departs">
      <i class="pi pi-pencil" />This departs from the run. A written reason is worth having on the file.
      <button type="button" class="link" @click="beginNote">Add one</button>
    </p>
    <form v-if="editingNote" ref="reasonBox" class="reason-form" @submit.prevent="saveNote">
      <label>
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
      <span class="reason-actions">
        <Button label="Save reason" icon="pi pi-save" size="small" :disabled="busy" @click="saveNote" />
        <Button label="Cancel" size="small" text severity="secondary" :disabled="busy" @click="cancelNote" />
      </span>
    </form>

    <!-- The record: what the procedure was and what the run found. -->
    <div class="detail-main">
    <p v-if="runnerNote" class="runner-note"><i class="pi pi-info-circle" />{{ runnerNote }}</p>

    <!-- `instruction` and `question` are one planned step written twice: the
         runner emits an imperative and an interrogative form of the same
         sentence. State the procedure once; keep the question one disclosure
         away. -->
    <section class="block">
      <p class="aw-label">Procedure</p>
      <p class="instruction">{{ item.instruction || 'No instruction was recorded for this item.' }}</p>
      <UiAdvancedSection
        v-if="item.question"
        title="Question as put to the model"
        description="The instruction as the runner phrased it"
      >
        <p class="question">{{ item.question }}</p>
      </UiAdvancedSection>
    </section>

    <section v-if="showAssessment" class="block" :data-empty="!hasAssessment">
      <p class="aw-label">Assessment</p>
      <!-- Coverage is what makes a cycle conclusion honest: how much of the
           population was actually reached, stated where the assessment is
           read rather than in a Result block of its own. -->
      <p v-if="cycleCoverage" class="coverage">
        Selected <strong>{{ cycleCoverage.selected_rows }}</strong> of
        <strong>{{ cycleCoverage.population_rows }}</strong> {{ pluralWord(cycleCoverage.population_rows, 'population row') }} from
        <code>{{ test.definition?.population.table }}.{{ test.definition?.population.column }}</code>.
        <span>{{ cycleCoverage.assurance_scope === 'sampled_population' ? 'Sampled population' : 'Targeted evidence — not a sample' }}.</span>
        <span v-if="cycleCoverage.rows_with_evidence !== null">
          {{ plural(cycleCoverage.rows_with_evidence, 'row') }} {{ verb(cycleCoverage.rows_with_evidence, 'has', 'have') }} linked evidence and {{ plural(cycleCoverage.complete_cycles, 'complete cycle') }} {{ verb(cycleCoverage.complete_cycles, 'was', 'were') }} identified.
        </span>
      </p>
      <template v-if="perDocumentAnswers.length">
        <article v-for="[documentId, answer] in perDocumentAnswers" :key="documentId" class="answer" :data-outcome="answer.outcome">
          <div class="answer-head">
            <i class="pi pi-file" aria-hidden="true" />
            <strong>{{ documentTitle(documentId) }}</strong>
            <span class="citations">
              <Button
                v-for="citation in answer.citations ?? []"
                :key="citation.id"
                :label="`Page ${citation.page || '—'}`"
                size="small"
                text
                @click="emit('anchor', citation)"
              />
            </span>
            <UiTestStatus :status="answer.outcome" showLabel />
          </div>
          <p>{{ answer.answer }}</p>
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

    <!-- The transaction cycle: which documents stood in for which role. -->
    <section v-if="cycleDocuments.length" class="block">
      <p class="aw-label">Transaction cycle</p>
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
      <p class="aw-label">Assertion results</p>
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

    <!-- Comparison detail, for the vouching branch only. -->
    <section v-if="item.checks?.length" class="block">
      <p class="aw-label">Comparisons</p>
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
      <p class="aw-label">Attributes</p>
      <article v-for="attribute in item.attributes" :key="attribute.name" class="attribute">
        <strong>{{ attribute.name }}</strong>
        <UiTestStatus :status="attribute.verdict" showLabel />
        <InputText v-model="attribute.note" placeholder="Auditor note" />
      </article>
      <Button label="Save attribute notes" icon="pi pi-save" size="small" outlined @click="emit('saveAttributes')" />
    </section>

    <!-- Evidence: what is attached and what is still missing. -->
    <section class="block">
      <div class="block-head">
        <p class="aw-label">Evidence</p>
        <span class="grow" />
      </div>
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
          placeholder="Attach a document"
        />
        <Button label="Attach" icon="pi pi-paperclip" outlined :disabled="!attachId" @click="attach" />
      </div>
    </section>

    </div>

    <!-- The two records the test carries once its items are answered for. -->
    <div class="footer-row">
      <div class="footer-cell">
        <p class="aw-label">Conclusion</p>
        <Select
          v-model="test.control_conclusion"
          :options="controlConclusions"
          optionLabel="label"
          optionValue="value"
          size="small"
          class="conclusion-select"
        />
        <Button
          v-if="conclusionChanged"
          label="Save"
          icon="pi pi-check"
          size="small"
          :disabled="busy"
          @click="emit('saveConclusion')"
        />
        <span v-else-if="!settled" class="footer-note">Record your call first.</span>
      </div>

      <div class="footer-cell">
        <p class="aw-label">Finding</p>
        <template v-if="findings.length">
          <button
            v-for="finding in findings"
            :key="finding.id"
            type="button"
            class="finding-chip"
            @click="emit('openFinding', finding.id)"
          >
            <span class="finding-id">{{ finding.id }}</span>{{ finding.severity }}
          </button>
          <Button
            label="Regenerate"
            size="small"
            text
            severity="secondary"
            :disabled="busy || !test.rcm_id || !canGenerateFinding"
            @click="emit('generateFinding', true)"
          />
        </template>
        <template v-else>
          <span class="footer-note">{{ findingBlockedReason || 'None yet.' }}</span>
          <Button
            label="Generate finding"
            icon="pi pi-sparkles"
            size="small"
            text
            severity="secondary"
            :disabled="busy || !test.rcm_id || !canGenerateFinding"
            @click="emit('generateFinding', false)"
          />
        </template>
      </div>

      <!-- A warning, not a block. Saving over open items is allowed; the
           backend appends what was open to this test's scope limitations. -->
      <p v-if="controlConclusionReason" class="footer-warn">{{ controlConclusionReason }}</p>
      <p v-else-if="openItems.length" class="footer-warn">
        {{ openItems.length }} of {{ test.items.length }}
        {{ test.items.length === 1 ? 'item is' : 'items are' }} unresolved. You can still
        conclude — it will be recorded as a scope limitation.
      </p>

      <p v-if="workspaceId" class="footer-provenance">
        <button
          type="button"
          class="disclosure-link"
          :aria-expanded="provenanceOpen"
          @click="provenanceOpen = !provenanceOpen"
        >
          <i class="pi" :class="provenanceOpen ? 'pi-chevron-down' : 'pi-chevron-right'" />Where this came from
        </button>
      </p>
      <!-- Provenance belongs to the test definition, not to the item: the
           agent wrote the test once and every item inherits it. -->
      <ProvenanceRail
        v-if="provenanceOpen && workspaceId"
        :key="test.id"
        :workspaceId="workspaceId"
        :artifactRef="`doctest:${test.id}`"
        class="provenance"
      />
    </div>
  </div>
</template>

<style scoped>
/* One column, not a record beside a rail. The rail held the run result, the
   call, the conclusion, the finding and provenance — five things, one of which
   the reader needed at a time, in a 13rem column with its own scrollbar. */
.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.detail-head { display: flex; align-items: flex-start; gap: 1rem; min-width: 0; }
.head-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.eyebrow { margin: 0; }
.item-id { font-family: var(--aw-font-mono); letter-spacing: 0; }
.detail-head h2 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: 600; letter-spacing: -0.01em; line-height: 1.3; }
.context { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); }
.rcm-link { flex: 0 0 auto; border-color: var(--aw-teal-line); color: var(--aw-teal); white-space: nowrap; }
.unlinked { flex: 0 1 16rem; margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-sm); text-align: right; }

.run-meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }
.quoted { color: var(--aw-ink); font-style: italic; }

.call { display: flex; flex-direction: column; align-items: flex-end; gap: .3rem; }
.dispositions { display: inline-flex; overflow: hidden; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.dispositions button {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: .4rem .75rem; border: 0; background: none;
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; white-space: nowrap; cursor: pointer;
}
.dispositions button + button { border-left: 1px solid var(--aw-border-strong); }
.dispositions button[data-tone='ok'] { color: var(--aw-ok); }
.dispositions button[data-tone='bad'] { color: var(--aw-danger); }
.dispositions button[data-tone='warn'] { color: var(--aw-warn-ink); }
.dispositions button:hover:not(:disabled) { background: var(--aw-raised); }
.dispositions button:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.dispositions button:disabled { opacity: .5; cursor: not-allowed; }
.dispositions button[aria-pressed='true'][data-tone='ok'] { background: var(--aw-ok-soft); }
.dispositions button[aria-pressed='true'][data-tone='bad'] { background: var(--aw-danger-soft); }
.dispositions button[aria-pressed='true'][data-tone='warn'] { background: var(--aw-warn-soft); }
.dispositions .pi { font-size: var(--aw-text-xs); }
.call-links { display: flex; gap: .75rem; margin: 0; }

.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; text-decoration: underline; cursor: pointer; }
.link[disabled] { color: var(--aw-muted); cursor: default; text-decoration: none; }

.departs { display: flex; align-items: baseline; gap: .4rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.reason-form { display: flex; flex-direction: column; gap: .4rem; }
.reason-form label { display: flex; flex-direction: column; gap: .25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.reason-actions { display: flex; gap: .5rem; }

/* The record column is its own container: the panels inside it size against
   the space they actually have. */
.detail-main { display: flex; flex-direction: column; min-width: 0; container: detail-main / inline-size; }

.block { display: flex; flex-direction: column; gap: .55rem; min-width: 0; padding: 1.15rem 0 .35rem; border-top: 1px solid var(--aw-border); }
.block:first-child { padding-top: 0; border-top: 0; }
.block-head { display: flex; align-items: center; gap: .5rem; }
.grow { flex: 1; }
.instruction { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.question { margin: 0; color: var(--aw-ink); font-size: var(--aw-text-base); line-height: 1.5; }
.response { margin: 0; font-size: var(--aw-text-base); line-height: 1.55; }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* One card per document, ruled in its own verdict's tone: which document said
   what is the whole answer on a multi-document item. */
.answer { display: flex; flex-direction: column; gap: .4rem; padding: .7rem .8rem; border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); }
.answer[data-outcome='exception'], .answer[data-outcome='failed'] { border-left-color: var(--aw-danger); }
.answer[data-outcome='confirmed'], .answer[data-outcome='passed'] { border-left-color: var(--aw-ok); }
.answer[data-outcome='needs_review'], .answer[data-outcome='inconclusive'] { border-left-color: var(--aw-warn); }
.answer-head { display: flex; align-items: center; gap: .5rem; min-width: 0; }
.answer-head strong { min-width: 0; overflow: hidden; color: var(--aw-ink-strong); font-size: var(--aw-text-base); text-overflow: ellipsis; white-space: nowrap; }
.answer-head > .pi { flex: none; color: var(--aw-teal); }
.answer-head .citations { margin-left: auto; }
.answer p { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.citations { display: flex; flex-wrap: wrap; gap: .2rem; }
blockquote { margin: 0; padding: .7rem .8rem; border-left: 3px solid var(--aw-teal); background: var(--aw-raised); font-size: var(--aw-text-sm); line-height: 1.5; }
.runner-note { display: flex; align-items: flex-start; gap: .4rem; margin: 0 0 .15rem; padding: .55rem .7rem; border-radius: var(--aw-radius-control); background: var(--aw-info-soft); font-size: var(--aw-text-sm); }

.check { display: flex; flex-direction: column; gap: .4rem; padding: .65rem .7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.check.focused-assertion { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.check-head { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
.comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto auto; gap: .5rem; align-items: center; padding: .4rem 0; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); }
.comparison-source { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comparison-settings { display: grid; grid-template-columns: minmax(0, 1fr) 11rem 8rem; gap: .5rem; align-items: center; }
/* The path is the audit trail for a cycle comparison: it says exactly which
   field of which record produced the value beside it. */
.path { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
.check-note { margin: .2rem 0 0; color: var(--aw-warn); font-size: var(--aw-text-sm); }
.role-row { display: grid; grid-template-columns: 10rem minmax(0, 1fr) auto; gap: .5rem; align-items: center; padding: .3rem 0; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); }
.matched-chain { grid-column: 2 / -1; color: var(--aw-muted); overflow-wrap: anywhere; }
.cycle-result-comparison { grid-template-columns: minmax(9rem, .8fr) minmax(7rem, .6fr) minmax(0, 1fr) auto; }
.role-name { font-weight: 700; }
.role-doc { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.missing-roles { margin: 0 0 .4rem; color: var(--aw-warn); font-size: var(--aw-text-sm); }
.coverage { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.5; }
.frozen { display: flex; flex-wrap: wrap; gap: .5rem 1rem; font-size: var(--aw-text-sm); color: var(--aw-muted); }
code { font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); overflow-wrap: anywhere; }
.attribute { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1.4fr); gap: .5rem; align-items: center; padding: .5rem .6rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }

.attached { display: flex; flex-wrap: wrap; gap: .35rem; }
.doc-chip { display: inline-flex; align-items: center; gap: .3rem; padding: .2rem .5rem; border-radius: var(--aw-radius-pill); background: var(--aw-teal-soft); color: var(--aw-teal); font-size: var(--aw-text-xs); font-weight: 600; }
.gap, .conflict { display: flex; flex-direction: column; gap: .2rem; padding: .6rem .7rem; border-radius: var(--aw-radius-control); font-size: var(--aw-text-sm); }
.gap { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.conflict { background: var(--aw-danger-soft); color: var(--aw-danger); }
.gap strong, .conflict strong { display: flex; align-items: center; gap: .35rem; }
.evidence-requests { display: grid; gap: .45rem; margin-top: .05rem; }
.evidence-request { display: grid; gap: .25rem; padding: .55rem .65rem; border-left: 3px solid var(--aw-warn); border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0; background: var(--aw-raised); }
.evidence-request[data-status='received'], .evidence-request[data-status='cancelled'] { border-left-color: var(--aw-ok); opacity: .8; }
.evidence-request-head { display: flex; justify-content: space-between; gap: .5rem; }
.evidence-request-head small { color: var(--aw-muted); font-family: var(--aw-font-mono); }
.evidence-request p { margin: 0; font-size: var(--aw-text-sm); line-height: 1.35; }
.evidence-request-actions { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .2rem; }
.attach { display: flex; align-items: center; gap: .5rem; }
.attach :deep(.p-select) { flex: 1; min-width: 0; }

/* Two records on one rule: what the test concluded, and what was written up. */
.footer-row {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: .625rem .875rem;
  margin-top: auto; padding-top: .875rem; border-top: 1px solid var(--aw-border);
}
.footer-cell { display: flex; align-items: center; gap: .625rem; min-width: 0; }
.footer-cell .aw-label { flex: none; }
.footer-note { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.footer-warn, .footer-provenance { grid-column: 1 / -1; margin: 0; }
.footer-warn { color: var(--aw-warn-ink); font-size: var(--aw-text-sm); line-height: 1.45; }
.conclusion-select { min-width: 10rem; }
.finding-chip {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .125rem .5rem;
  border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.finding-chip .finding-id { font-family: var(--aw-font-mono); }
.disclosure-link {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: 0; border: 0; background: none; color: var(--aw-muted);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer;
}
.disclosure-link:hover { color: var(--aw-teal); }
.disclosure-link .pi { font-size: var(--aw-text-2xs); }
.provenance { grid-column: 1 / -1; }

/* PrimeVue leaves its control text at the browser default, so any control
   without an explicit `size` rendered at 16px — larger than the heading above
   it. Binding the controls to the panel's body size removes that tier. */
.detail :deep(.p-button),
.detail :deep(.p-button-label),
.detail :deep(.p-select),
.detail :deep(.p-select-label),
.detail :deep(.p-inputtext),
.detail :deep(.p-textarea) { font-size: var(--aw-text-base); }
.detail :deep(.ui-advanced > summary small) { font-size: var(--aw-text-xs); }

@container detail-main (max-width: 30rem) {
  .comparison, .comparison-settings, .attribute { grid-template-columns: minmax(0, 1fr); }
}
@container master-detail-content (max-width: 34rem) {
  .detail-head { flex-wrap: wrap; }
  .footer-row { grid-template-columns: minmax(0, 1fr); }
  .call { align-items: flex-start; }
}
</style>
