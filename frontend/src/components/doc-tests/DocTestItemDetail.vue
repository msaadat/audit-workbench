<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type { AuditDocument, AuditFinding, DocTest, DocTestItem, EvidenceRef } from '../../types'
import UiAdvancedSection from '../ui/UiAdvancedSection.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'

const props = defineProps<{
  test: DocTest
  item: DocTestItem
  documents: AuditDocument[]
  findings: AuditFinding[]
  running: boolean
  busy: boolean
}>()
const emit = defineEmits<{
  anchor: [EvidenceRef]
  attach: [documentId: string]
  saveChecks: []
  saveAttributes: []
  setState: [value: 'confirmed' | 'exception' | 'pending']
  saveConclusion: []
  generateFinding: [regenerate: boolean]
  openFinding: [findingId: string]
  updateEvidenceRequest: [requestId: string, status: 'received' | 'cancelled']
  run: []
  openRcm: [rcmId: string]
}>()

const attachId = ref<string | null>(null)
const methods = ['exact', 'normalized', 'fuzzy', 'numeric_tolerance', 'date_tolerance']
const kindLabel: Record<string, string> = {
  vouching: 'Vouching / tracing',
  attribute: 'Attribute test',
  review: 'Document review',
  qa: 'Cited Q&A',
}
const controlConclusions = [
  { label: 'No conclusion recorded', value: 'no_conclusion' },
  { label: 'Control effective', value: 'effective' },
  { label: 'Control partially effective', value: 'partially_effective' },
  { label: 'Control ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]

const documentOptions = computed(() => props.documents.map(doc => ({ label: doc.title, value: doc.id })))
const attachable = computed(() => documentOptions.value.filter(option => !props.item.document_ids.includes(option.value)))
const executedLocally = computed(() => props.test.kind === 'vouching')
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
// 'confirmed'/'exception' are already a direct read of the model's (or the
// deterministic comparison's) own outcome — nothing for an auditor to settle.
// Only 'manual_review'/'agent_checked' mean the result itself is unresolved.
const needsSignOff = computed(() => ['manual_review', 'agent_checked'].includes(props.item.state))


function documentTitle(id: string) {
  return props.documents.find(doc => doc.id === id)?.title || id
}
function attach() {
  if (!attachId.value) return
  emit('attach', attachId.value)
  attachId.value = null
}
</script>

<template>
  <div class="detail">
    <!-- 1. What this item concluded, before anything editable. -->
    <header class="detail-head">
      <div class="head-copy">
        <p class="eyebrow">{{ kindLabel[test.kind ?? ''] ?? 'Document work' }} · {{ item.id }}</p>
        <h3>{{ item.label || item.id }}</h3>
        <p class="context">
          <span>{{ test.title }}</span>
          <button v-if="test.rcm_id" type="button" class="link" @click="emit('openRcm', test.rcm_id)">
            {{ test.rcm_id }}
          </button>
          <em v-else>Not linked to an RCM row — this work does not count as coverage.</em>
        </p>
      </div>
      <div class="head-status">
        <UiTestStatus :status="item.state" showLabel />
        <Button
          label="Run test"
          icon="pi pi-play"
          size="small"
          :loading="running"
          :disabled="busy"
          @click="emit('run')"
        />
      </div>
    </header>

    <p class="mode-note">
      <i class="pi pi-info-circle" />
      {{ executedLocally
        ? 'Comparison values are matched deterministically; the assistant orchestrates the run and records citations.'
        : 'Assessed by the assistant against cited document text; every conclusion carries a page citation.' }}
    </p>
    <p v-if="item.runner_note" class="runner-note"><i class="pi pi-info-circle" />{{ item.runner_note }}</p>

    <section class="block">
      <h4>Procedure</h4>
      <p class="instruction">{{ item.instruction || 'No instruction was recorded for this item.' }}</p>
      <p v-if="item.question && item.question !== item.instruction" class="question">
        <i class="pi pi-question-circle" />{{ item.question }}
      </p>
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

    <!-- 2. Comparison detail, for the vouching branch only. -->
    <section v-if="item.checks?.length" class="block">
      <h4>Comparisons</h4>
      <article v-for="check in item.checks" :key="check.field" class="check">
        <div class="check-head">
          <strong>{{ check.field }}</strong>
          <UiTestStatus :status="check.verdict" showLabel />
        </div>
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

    <!-- 3. Evidence: what is attached and what is still missing. -->
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
          {{ item.evidence_request_ids.length }} evidence request(s) raised.
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

      <div class="attach">
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

    <!-- 4. The test-level conclusion the workflow recorded, and the -->
    <!-- auditor's own conclusion about the control this test covers. -->
    <section class="block outcome">
      <h4>Test conclusion</h4>
      <p v-if="test.result_summary" class="summary">{{ test.result_summary }}</p>
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
      <label>
        Control conclusion
        <Select
          v-model="test.control_conclusion"
          :options="controlConclusions"
          optionLabel="label"
          optionValue="value"
        />
      </label>
      <label>
        Conclusion
        <Textarea
          v-model="test.conclusion"
          rows="2"
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
    </section>
    <section class="block finding-action">
      <template v-if="findings.length">
        <div>
          <h4>Finding{{ findings.length === 1 ? '' : 's' }}</h4>
          <p>{{ findings.length === 1 ? 'A finding is linked to this test.' : `${findings.length} findings are linked to this test.` }}</p>
        </div>
        <div class="finding-actions">
          <Button
            v-for="finding in findings"
            :key="finding.id"
            :label="`Open ${finding.id}`"
            icon="pi pi-arrow-up-right"
            size="small"
            outlined
            @click="emit('openFinding', finding.id)"
          />
          <Button label="Regenerate" icon="pi pi-refresh" size="small" severity="secondary" :disabled="busy || !test.rcm_id || !test.status.startsWith('completed')" @click="emit('generateFinding', true)" />
        </div>
      </template>
      <template v-else>
        <div><h4>Finding</h4><p>Generate a draft from this test’s exception observations.</p></div>
        <Button label="Generate finding" icon="pi pi-sparkles" size="small" :disabled="busy || !test.rcm_id || !test.status.startsWith('completed')" @click="emit('generateFinding', false)" />
      </template>
    </section>

    <!-- 5. The auditor's own sign-off. Confirm/exception only apply while -->
    <!-- the result is unresolved — once it is 'confirmed' or 'exception' -->
    <!-- that is already a direct read of the model's (or the deterministic -->
    <!-- comparison's) own outcome, not something to settle by hand. -->
    <!-- 'Reset to pending' stays available regardless: it is the only way -->
    <!-- to force a re-check once new evidence makes the settled result stale. -->
    <footer class="sign-off">
      <p class="sign-off-note">
        {{ needsSignOff
          ? (item.runner_note || 'The model could not settle this item on its own — confirm the result or mark an exception.')
          : 'This result was derived directly from the model\'s assessment. Reset it to force a re-check.' }}
      </p>
      <div class="dispositions">
        <template v-if="needsSignOff">
          <Button
            label="Confirm result"
            icon="pi pi-check"
            size="small"
            severity="success"
            outlined
            :disabled="busy"
            @click="emit('setState', 'confirmed')"
          />
          <Button
            label="Mark exception"
            icon="pi pi-exclamation-triangle"
            size="small"
            severity="danger"
            outlined
            :disabled="busy"
            @click="emit('setState', 'exception')"
          />
        </template>
        <Button
          label="Reset to pending"
          icon="pi pi-refresh"
          size="small"
          text
          :disabled="busy || item.state === 'pending'"
          @click="emit('setState', 'pending')"
        />
      </div>
    </footer>
  </div>
</template>

<style scoped>
.detail { display: flex; flex-direction: column; gap: 0.85rem; min-width: 0; padding: 1rem; }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; min-width: 0; }
.head-copy { min-width: 0; }
.eyebrow { margin: 0; color: var(--aw-teal); font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; }
.detail-head h3 { margin: 0.15rem 0 0.25rem; font-size: 1.05rem; line-height: 1.3; }
.context { display: flex; align-items: center; flex-wrap: wrap; gap: 0.4rem; margin: 0; color: var(--aw-muted); font-size: 0.78rem; }
.context em { color: var(--aw-warn); font-style: normal; }
.link { border: 0; background: transparent; color: var(--aw-teal); cursor: pointer; font: inherit; font-weight: 600; padding: 0; }
.head-status { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; }

.mode-note { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0; padding: 0.55rem 0.7rem; border-radius: var(--aw-radius-sm); background: var(--aw-canvas); color: var(--aw-muted); font-size: 0.76rem; }

.block { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; padding: 0.8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: #fff; }
.block[data-empty='true'] { background: var(--aw-canvas); }
.block h4 { margin: 0; color: var(--aw-muted); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; }
.finding-action { align-items: center; flex-direction: row; justify-content: space-between; }
.finding-action p { margin: 0.2rem 0 0; color: var(--aw-muted); font-size: 0.78rem; }
.finding-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.4rem; }
.instruction { margin: 0; font-size: 0.88rem; line-height: 1.5; }
.question { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0; color: var(--aw-muted); font-size: 0.8rem; }
.response { margin: 0; font-size: 0.85rem; line-height: 1.55; }
.muted { margin: 0; color: var(--aw-muted); font-size: 0.8rem; }

.answer { display: flex; flex-direction: column; gap: 0.3rem; padding: 0.6rem 0.7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: var(--aw-canvas); }
.answer-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.answer p { margin: 0; font-size: 0.84rem; line-height: 1.5; }
.citations { display: flex; flex-wrap: wrap; gap: 0.2rem; }
blockquote { margin: 0; padding: 0.7rem 0.8rem; border-left: 3px solid var(--aw-teal); background: var(--aw-canvas); font-size: 0.82rem; line-height: 1.5; }
.runner-note { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0; padding: 0.55rem 0.7rem; border-radius: var(--aw-radius-sm); background: var(--p-blue-50); font-size: 0.78rem; }

.check { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.65rem 0.7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }
.check-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.comparison { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto auto; gap: 0.5rem; align-items: center; padding: 0.4rem 0; border-top: 1px solid var(--aw-border); font-size: 0.78rem; }
.comparison-source { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comparison-settings { display: grid; grid-template-columns: minmax(0, 1fr) 11rem 8rem; gap: 0.5rem; align-items: center; }
code { font-family: var(--aw-font-mono); font-size: 0.75rem; overflow-wrap: anywhere; }
.attribute { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1.4fr); gap: 0.5rem; align-items: center; padding: 0.5rem 0.6rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }

.attached { display: flex; flex-wrap: wrap; gap: 0.35rem; }
.doc-chip { display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.2rem 0.5rem; border-radius: 999px; background: var(--aw-teal-soft); color: var(--aw-teal); font-size: 0.74rem; font-weight: 600; }
.gap, .conflict { display: flex; flex-direction: column; gap: 0.2rem; padding: 0.6rem 0.7rem; border-radius: var(--aw-radius-sm); font-size: 0.78rem; }
.gap { background: var(--aw-warn-soft); color: var(--p-orange-800); }
.conflict { background: var(--aw-danger-soft); color: var(--p-red-700); }
.gap strong, .conflict strong { display: flex; align-items: center; gap: 0.35rem; }
.evidence-requests { display: grid; gap: 0.45rem; margin-top: 0.05rem; }
.evidence-request { display: grid; gap: 0.25rem; padding: 0.55rem 0.65rem; border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-warn); border-radius: var(--aw-radius-sm); background: var(--aw-canvas); }
.evidence-request[data-status='received'], .evidence-request[data-status='cancelled'] { border-left-color: var(--aw-ok); opacity: 0.8; }
.evidence-request-head { display: flex; justify-content: space-between; gap: 0.5rem; }
.evidence-request-head small { color: var(--aw-muted); font-family: var(--aw-font-mono); }
.evidence-request p { margin: 0; font-size: 0.76rem; line-height: 1.35; }
.evidence-request-actions { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.2rem; }
.attach { display: flex; align-items: center; gap: 0.5rem; }
.attach :deep(.p-select) { flex: 1; min-width: 0; }

.outcome .summary { margin: 0; font-size: 0.85rem; font-weight: 600; }
.outcome p { margin: 0; font-size: 0.83rem; line-height: 1.5; }
.outcome dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.25rem 0.7rem; margin: 0; font-size: 0.78rem; }
.outcome dt { color: var(--aw-muted); font-weight: 600; }
.outcome dd { margin: 0; }

.sign-off { position: sticky; bottom: -1rem; z-index: 2; display: flex; flex-direction: column; gap: 0.55rem; margin: 0 -1rem -1rem; padding: 0.75rem 1rem; border-top: 1px solid var(--aw-border); background: #fff; }
.sign-off-note { margin: 0; color: var(--aw-muted); font-size: 0.78rem; }
.dispositions { display: flex; flex-wrap: wrap; gap: 0.45rem; }
label { display: flex; flex-direction: column; gap: 0.3rem; color: #46576d; font-size: 0.75rem; font-weight: 600; }
label :deep(.p-select), label :deep(.p-textarea) { width: 100%; }

/* Sized against the detail column itself, not the window. */
@container master-detail-content (max-width: 34rem) {
  .detail-head { flex-direction: column; }
  .head-status { flex-wrap: wrap; }
  .comparison, .comparison-settings, .attribute { grid-template-columns: minmax(0, 1fr); }
  .outcome dl { grid-template-columns: minmax(0, 1fr); }
  .outcome dt { margin-top: 0.3rem; }
}
</style>
