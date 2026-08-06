<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import type {
  AuditDocument,
  AuditFinding,
  DocTest,
  DocTestCheck,
  DocTestCheckSide,
  DocComparison,
  DocTestItem,
  EvidenceRef,
} from '../../types'
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
const methods = [
  'exact',
  'normalized',
  'fuzzy',
  'numeric_tolerance',
  'date_tolerance',
  'date_order',
  'present',
]
// `present` reads only the left side, so a cycle check using it shows one row.
const unaryMethods = new Set(['present'])
const kindLabel: Record<string, string> = {
  vouching: 'Vouching / tracing',
  attribute: 'Attribute test',
  review: 'Document review',
  qa: 'Cited Q&A',
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


// A cycle check names both sides by path; a legacy check searches page text for
// a literal. They render differently because they mean different things — one
// is a resolved comparison between two records, the other a text match.
function isCycleCheck(check: DocTestCheck) {
  return Boolean(check.left)
}
function sides(check: DocTestCheck): DocTestCheckSide[] {
  return (check.comparisons ?? []).filter(
    (entry): entry is DocTestCheckSide => 'side' in entry && 'matches' in entry,
  )
}
function legacyComparisons(check: DocTestCheck): DocComparison[] {
  if (isCycleCheck(check)) return []
  return (check.comparisons ?? []).filter(
    (entry): entry is DocComparison => !('side' in entry),
  )
}
function visibleSides(check: DocTestCheck) {
  const resolved = sides(check)
  if (unaryMethods.has(check.method as string)) {
    return resolved.filter(side => side.side === 'left')
  }
  return resolved.filter(side => side.path)
}
function sideValue(side: DocTestCheckSide) {
  if (side.state === 'missing') return 'not found'
  if (side.state === 'ambiguous') return 'conflicting values'
  const value = side.matches[0]?.value
  if (value === null || value === undefined || value === '') return 'empty'
  return String(value)
}
// `row.<column>` reads the frozen population row and has no document behind it;
// every other path resolves to an excerpt in a named document.
function sideOrigin(side: DocTestCheckSide) {
  if (side.path.startsWith('row.')) return 'Population record'
  const match = side.matches[0]
  if (!match?.document_id) return 'No source'
  return `${documentTitle(match.document_id)} · page ${match.page ?? '—'}`
}
function sideAnchor(side: DocTestCheckSide): EvidenceRef | null {
  const match = side.matches.find(entry => entry.document_id && entry.excerpt)
  if (!match) return null
  return (props.item.evidence_refs ?? []).find(
    ref => ref.source_id === match.document_id && ref.excerpt === match.excerpt,
  ) ?? null
}

const cycleSpec = computed(() => (props.test.spec ?? {}) as Record<string, any>)
const cycleCoverage = computed(() => {
  const coverage = cycleSpec.value.coverage
  if (!coverage || typeof coverage !== 'object') return null
  return coverage as {
    population: number
    linked: number
    unlinked: number
    incomplete_roles: number
  }
})
const cycleDocuments = computed(() => props.item.documents ?? [])
const missingRoles = computed(() => props.item.missing_roles ?? [])
const frozenRow = computed(() => Object.entries(props.item.frozen ?? {}))

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
        <span
          v-if="entry.document_type && entry.document_type !== entry.role"
          class="role-type"
          :title="'Extracted document type, mapped into this role'"
        >{{ entry.document_type }}</span>
      </div>
      <UiAdvancedSection title="Population record" description="The frozen row this cycle is tested against">
        <div class="frozen">
          <span v-for="[field, value] in frozenRow" :key="field">
            {{ field }}: <code>{{ value ?? '—' }}</code>
          </span>
        </div>
      </UiAdvancedSection>
    </section>

    <!-- 2b. Comparison detail, for the vouching branch only. -->
    <section v-if="item.checks?.length" class="block">
      <h4>Comparisons</h4>
      <article v-for="check in item.checks" :key="check.field" class="check">
        <div class="check-head">
          <strong>{{ check.field }}</strong>
          <UiTestStatus :status="check.verdict" showLabel />
        </div>

        <!-- Cycle comparison: two resolved sides, each with its own source. -->
        <template v-if="isCycleCheck(check)">
          <div v-for="side in visibleSides(check)" :key="side.side" class="comparison">
            <span class="comparison-source">{{ sideOrigin(side) }}</span>
            <code :class="{ unresolved: side.state !== 'resolved' }">{{ sideValue(side) }}</code>
            <span class="path" :title="side.path">{{ side.path }}</span>
            <Button
              v-if="sideAnchor(side)"
              icon="pi pi-link"
              text
              rounded
              size="small"
              aria-label="Open evidence"
              @click="emit('anchor', sideAnchor(side)!)"
            />
          </div>
          <p v-if="check.note" class="check-note">{{ check.note }}</p>
        </template>

        <!-- Legacy comparison: a literal expectation matched against page text. -->
        <div
          v-for="result in legacyComparisons(check)"
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
            <span v-if="!isCycleCheck(check)">Expected: <code>{{ check.expected }}</code></span>
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

    <!-- What the run produced. The auditor's own conclusion is in the rail. -->
    <section class="block outcome">
      <h4>Result</h4>
      <p v-if="test.result_summary" class="summary">{{ test.result_summary }}</p>
      <!-- Coverage is what makes a cycle conclusion honest: how much of the
           population was actually reached, stated beside the result. -->
      <p v-if="cycleCoverage" class="coverage">
        Tested <strong>{{ cycleCoverage.linked }}</strong> of
        <strong>{{ cycleCoverage.population }}</strong> population row(s) from
        <code>{{ cycleSpec.table }}.{{ cycleSpec.anchor_key }}</code>.
        <span v-if="cycleCoverage.unlinked">
          {{ cycleCoverage.unlinked }} row(s) had no linked document.
        </span>
        <span v-if="cycleCoverage.incomplete_roles">
          {{ cycleCoverage.incomplete_roles }} linked row(s) are missing a required role.
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
    </div>

    <!-- The rail: everything the auditor decides, in one column that stays put
         while the record beside it scrolls. -->
    <aside class="detail-rail" aria-label="Your assessment">
      <div class="rail-group rail-status">
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

      <div class="rail-group">
        <h4>Your conclusion</h4>
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
          <Button label="Regenerate" icon="pi pi-refresh" size="small" severity="secondary" :disabled="busy || !test.rcm_id || !test.status.startsWith('completed')" @click="emit('generateFinding', true)" />
        </template>
        <template v-else>
          <p class="rail-note">Generate a draft from this test’s exception observations.</p>
          <Button label="Generate finding" icon="pi pi-sparkles" size="small" :disabled="busy || !test.rcm_id || !test.status.startsWith('completed')" @click="emit('generateFinding', false)" />
        </template>
      </div>

      <!-- Confirm/exception only apply while the result is unresolved — once it
           is 'confirmed' or 'exception' that is already a direct read of the
           model's (or the deterministic comparison's) own outcome, not
           something to settle by hand. 'Reset to pending' stays available
           regardless: it is the only way to force a re-check once new evidence
           makes the settled result stale. -->
      <div class="rail-group">
        <h4>Sign-off</h4>
        <p class="rail-note">
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
      </div>
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
.block { display: flex; flex-direction: column; gap: 0.55rem; min-width: 0; padding: 1.15rem 0 0.35rem; border-top: 1px solid var(--aw-border); }
.block h4 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 700; letter-spacing: -0.01em; }
.instruction { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.question { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.response { margin: 0; font-size: var(--aw-text-base); line-height: 1.55; }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.answer { display: flex; flex-direction: column; gap: 0.3rem; padding: 0.6rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.answer-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.answer p { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }
.citations { display: flex; flex-wrap: wrap; gap: 0.2rem; }
blockquote { margin: 0; padding: 0.7rem 0.8rem; border-left: 3px solid var(--aw-teal); background: var(--aw-raised); font-size: var(--aw-text-sm); line-height: 1.5; }
.runner-note { display: flex; align-items: flex-start; gap: 0.4rem; margin: 0 0 0.15rem; padding: 0.55rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-info-soft); font-size: var(--aw-text-sm); }

.check { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.65rem 0.7rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
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

/* Sized against the record column, not the whole detail column: the rail
   takes width out of it, so a query keyed to the outer column fired late. */
@container detail-main (max-width: 30rem) {
  .comparison, .comparison-settings, .attribute { grid-template-columns: minmax(0, 1fr); }
  .outcome dl { grid-template-columns: minmax(0, 1fr); }
  .outcome dt { margin-top: 0.3rem; }
}
</style>
