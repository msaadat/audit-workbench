<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'

import { sentenceCase } from '../../format'
import type { FindingSummary, RcmRow, TestRollup } from '../../types'

/**
 * One row, quickly — read and sign off first, edit on click.
 *
 * Opening a row during a review or walkthrough presents the risk, control,
 * policy grounding (`criteria_refs`), linked Data/Document tests, and a direct
 * auditor sign-off control instead of dropping the reader straight into four
 * editable textboxes. Clicking "Edit" switches inline to the editable form over
 * a copy of the row.
 */

const props = defineProps<{
  row: RcmRow
  findings: FindingSummary[]
  saving?: boolean
}>()
const emit = defineEmits<{
  save: [changes: Partial<RcmRow>]
  approve: []
  close: []
  remove: []
  paper: []
  openRow: [tab?: string]
  openTest: [rollup: TestRollup]
  addTest: [kind: 'data' | 'document' | 'link' | 'generate']
}>()

const ratings = ['low', 'medium', 'high', 'critical']
const CONCLUSIONS: Record<string, { label: string; tone: string }> = {
  effective: { label: 'Effective', tone: 'ok' },
  partially_effective: { label: 'Partially effective', tone: 'warn' },
  ineffective: { label: 'Ineffective', tone: 'bad' },
  not_applicable: { label: 'Not applicable', tone: 'neutral' },
}
const EVIDENCE_KIND_LABELS: Record<string, string> = {
  tabular_population: 'Full population',
  transaction_cycle: 'Transaction cycle',
  document_content: 'Document',
  manual_inspection: 'Manual',
}

const editing = ref(false)
const draft = ref(snapshot())
function snapshot() {
  return {
    process: props.row.process,
    risk_rating: props.row.risk_rating,
    risk: props.row.risk,
    control: props.row.control,
    review_status: props.row.review_status,
  }
}
watch(() => props.row.id, () => {
  draft.value = snapshot()
  editing.value = false
})
// A background refresh must not overwrite an edit in progress. Saving does
// not leave edit mode here either: the parent closes the drawer once the save
// lands, and a failed save leaves the auditor's edit where it was.
watch(() => props.row.updated, () => {
  if (!editing.value) draft.value = snapshot()
})

const reviewed = computed({
  get: () => draft.value.review_status === 'reviewed',
  set: value => { draft.value.review_status = value ? 'reviewed' : 'draft' },
})
const conclusion = computed(() => {
  const key = String(props.row.execution_rollup.control_conclusion ?? '') || 'no_conclusion'
  return CONCLUSIONS[key] ?? { label: 'No conclusion', tone: 'neutral' }
})
const tests = computed<TestRollup[]>(() => props.row.execution_rollup.test_rollups ?? [])
const agentSet = computed(() => props.row.created_by === 'agent' && props.row.review_status !== 'reviewed')
const criteriaRefs = computed(() => props.row.criteria_refs ?? [])

const addMenu = ref<InstanceType<typeof Menu> | null>(null)
const addOptions = [
  { label: 'Data test', icon: 'aw-icon aw-icon-chart-column', command: () => emit('addTest', 'data') },
  { label: 'Document test', icon: 'aw-icon aw-icon-file-check', command: () => emit('addTest', 'document') },
  { separator: true },
  { label: 'Link an existing test', icon: 'aw-icon aw-icon-link', command: () => emit('addTest', 'link') },
  { label: 'Generate with assistant', icon: 'aw-icon aw-icon-sparkles', command: () => emit('addTest', 'generate') },
]

function testTone(rollup: TestRollup) {
  if (rollup.open_exception_count) return 'bad'
  if (String(rollup.status).startsWith('completed')) return 'ok'
  return 'neutral'
}

function evidenceKindLabel(kind?: string): string {
  if (!kind) return ''
  return EVIDENCE_KIND_LABELS[kind] ?? sentenceCase(kind.replace(/_/g, ' '))
}

function cancelEdit() {
  draft.value = snapshot()
  editing.value = false
}

function saveEdit() {
  emit('save', { ...draft.value })
}
</script>

<template>
  <section class="drawer" aria-label="RCM row">
    <header class="drawer-head">
      <button type="button" class="row-id" @click="emit('openRow')">
        {{ row.id }}<i class="aw-icon aw-icon-arrow-up-right" />
      </button>
      <span class="rating" :data-rating="row.risk_rating"><span class="rating-dot" />{{ row.risk_rating }}</span>
      <span class="grow" />
      <button type="button" class="link" @click="emit('openRow')">
        Full record<i class="aw-icon aw-icon-arrow-up-right" />
      </button>
      <button type="button" class="link" @click="emit('paper')">Working paper</button>
      <button type="button" class="close" aria-label="Close" @click="emit('close')">
        <i class="aw-icon aw-icon-x" />
      </button>
    </header>

    <div class="drawer-body">
      <!-- Provenance & governance strip -->
      <div
        class="provenance-strip"
        :data-state="row.review_status === 'reviewed' ? 'reviewed' : row.created_by === 'agent' ? 'ai' : 'user'"
      >
        <i
          class="aw-icon"
          :class="row.review_status === 'reviewed' ? 'aw-icon-circle-check' : row.created_by === 'agent' ? 'aw-icon-sparkles' : 'aw-icon-user-pen'"
          aria-hidden="true"
        />
        <span v-if="row.review_status === 'reviewed'">
          <strong>Auditor signed off</strong> · owned by auditor and preserved on reruns
        </span>
        <span v-else-if="row.created_by === 'agent'">
          <strong>AI drafted</strong> from policies &amp; data · awaiting auditor sign-off
        </span>
        <span v-else>
          <strong>Auditor drafted</strong> · draft
        </span>
      </div>

      <!-- Read & verify posture (default) -->
      <template v-if="!editing">
        <div class="read-card">
          <p v-if="row.process" class="process-eyebrow">{{ row.process }}</p>
          <div class="prose-block">
            <span class="aw-label">Risk</span>
            <p class="prose-text">{{ row.risk }}</p>
          </div>
          <div class="prose-block">
            <span class="aw-label">Control</span>
            <p v-if="row.control?.trim()" class="prose-text soft">{{ row.control }}</p>
            <p v-else class="no-control">
              <i class="aw-icon aw-icon-info" aria-hidden="true" />No control identified
            </p>
          </div>
        </div>

        <!-- Policy grounding: verbatim SOP/policy excerpts behind this AI risk -->
        <section v-if="row.criteria?.trim() || criteriaRefs.length" class="group">
          <div class="group-head">
            <p class="aw-label">
              Policy basis<template v-if="criteriaRefs.length"> · {{ criteriaRefs.length }}</template>
            </p>
          </div>
          <p v-if="row.criteria?.trim()" class="criteria-text">{{ row.criteria }}</p>
          <blockquote v-for="refItem in criteriaRefs" :key="refItem.id" class="policy-quote">
            <span class="quote-meta">
              <i class="aw-icon aw-icon-book-open" aria-hidden="true" />
              <span v-if="refItem.page" class="page-chip">p.{{ refItem.page }}</span>
              <span v-if="refItem.citation_id" class="cite-id">{{ refItem.citation_id }}</span>
            </span>
            <p class="quote-body">“{{ refItem.excerpt }}”</p>
          </blockquote>
        </section>
      </template>

      <!-- Edit posture (on click) -->
      <template v-else>
        <div class="pair">
          <label>Process<InputText v-model="draft.process" size="small" /></label>
          <label>Rating<Select v-model="draft.risk_rating" :options="ratings" size="small" /></label>
        </div>
        <label>Risk<Textarea v-model="draft.risk" rows="3" autoResize /></label>
        <label>Control<Textarea v-model="draft.control" rows="3" autoResize placeholder="No control identified" /></label>
      </template>

      <section class="group">
        <div class="group-head">
          <p class="aw-label">Attributes · {{ row.control_attributes.length }}</p>
          <span class="grow" />
          <button type="button" class="link" @click="emit('openRow', 'attributes')">Add attribute</button>
        </div>
        <div v-for="attribute in row.control_attributes" :key="attribute.key" class="attribute">
          <div class="attr-tags">
            <span class="assertion">{{ sentenceCase(attribute.assertion) }}</span>
            <span v-if="attribute.evidence_kind" class="ev-kind">{{ evidenceKindLabel(attribute.evidence_kind) }}</span>
          </div>
          <span class="requirement">{{ attribute.requirement }}</span>
        </div>
        <p v-if="!row.control_attributes.length" class="muted">No attribute is recorded against this control.</p>
      </section>

      <section class="group">
        <div class="group-head">
          <p class="aw-label">Tests · {{ tests.length }}</p>
          <span class="grow" />
          <button type="button" class="link" aria-haspopup="true" @click="addMenu?.toggle($event)">
            Add test<i class="aw-icon aw-icon-chevron-down" />
          </button>
          <Menu ref="addMenu" :model="addOptions" popup />
        </div>
        <button
          v-for="rollup in tests"
          :key="rollup.test_id"
          type="button"
          class="test"
          @click="emit('openTest', rollup)"
        >
          <span class="dot" :data-tone="testTone(rollup)" />
          <span class="test-kind" :data-kind="rollup.kind">
            {{ rollup.kind === 'datatest' ? 'Data' : 'Doc' }}
          </span>
          <span class="test-title">{{ rollup.title }}</span>
          <span v-if="rollup.open_exception_count" class="open">{{ rollup.open_exception_count }} open</span>
        </button>
        <p v-if="!tests.length" class="muted">This risk has no linked test and cannot pass coverage.</p>
      </section>

      <section class="conclusion" :data-tone="conclusion.tone">
        <div class="conclusion-head">
          <strong>Conclusion: {{ conclusion.label }}</strong>
          <span class="grow" />
          <button type="button" class="link" @click="emit('openRow', 'tests')">Change</button>
        </div>
        <p v-if="agentSet" class="by-agent">Set by the agent. No one has read it yet.</p>
        <p v-for="finding in findings" :key="finding.id" class="finding">
          Finding {{ finding.id }} ({{ finding.severity }}) is drafted from it.
        </p>
      </section>
    </div>

    <footer class="drawer-foot">
      <template v-if="editing">
        <label class="toggle">
          <ToggleSwitch v-model="reviewed" />
          <span>Mark reviewed</span>
        </label>
        <span class="grow" />
        <Button
          v-tooltip.top="'Remove row'"
          icon="aw-icon aw-icon-trash-2"
          aria-label="Remove row"
          size="small"
          outlined
          severity="danger"
          :disabled="saving"
          @click="emit('remove')"
        />
        <Button label="Cancel" size="small" outlined severity="secondary" @click="cancelEdit" />
        <Button label="Save row" size="small" :loading="saving" @click="saveEdit" />
      </template>
      <template v-else>
        <Button
          v-tooltip.top="'Remove row'"
          icon="aw-icon aw-icon-trash-2"
          aria-label="Remove row"
          size="small"
          outlined
          severity="danger"
          :disabled="saving"
          @click="emit('remove')"
        />
        <Button
          label="Edit"
          icon="aw-icon aw-icon-pencil"
          size="small"
          outlined
          severity="secondary"
          :disabled="saving"
          @click="editing = true"
        />
        <span class="grow" />
        <Button
          v-if="row.review_status !== 'reviewed'"
          label="Approve & sign off"
          icon="aw-icon aw-icon-check"
          size="small"
          :loading="saving"
          @click="emit('approve')"
        />
        <span v-else class="signed-chip">
          <i class="aw-icon aw-icon-circle-check" aria-hidden="true" />Signed off
        </span>
      </template>
    </footer>
  </section>
</template>

<style scoped>
.drawer { display: flex; flex-direction: column; min-width: 0; height: 100%; }
.grow { flex: 1; }

.drawer-head { display: flex; align-items: center; gap: .5rem; padding: .75rem 1rem; border-bottom: 1px solid var(--aw-border); }
.row-id {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: 0; border: 0; background: none; color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.row-id:hover { color: var(--aw-teal); }
.row-id .aw-icon { font-size: var(--aw-text-2xs); }
.rating { display: inline-flex; align-items: center; gap: .3125rem; font-size: var(--aw-text-xs); font-weight: 600; text-transform: capitalize; }
.rating-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--aw-muted); }
.rating[data-rating='critical'] { color: var(--aw-danger-ink); }
.rating[data-rating='critical'] .rating-dot { background: var(--aw-danger-ink); }
.rating[data-rating='high'] { color: var(--aw-danger); }
.rating[data-rating='high'] .rating-dot { background: var(--aw-danger); }
.rating[data-rating='medium'] { color: var(--aw-warn-ink); }
.rating[data-rating='medium'] .rating-dot { background: var(--aw-warn); }
.rating[data-rating='low'] { color: var(--aw-low-ink); }
.rating[data-rating='low'] .rating-dot { background: var(--aw-low); }
.close { display: grid; place-items: center; width: 1.75rem; height: 1.75rem; padding: 0; border: 0; border-radius: var(--aw-radius-control); background: none; color: var(--aw-muted); cursor: pointer; }
.close:hover { background: var(--aw-raised); color: var(--aw-ink); }

.drawer-body { display: flex; flex-direction: column; gap: .875rem; flex: 1; min-height: 0; overflow-y: auto; padding: .875rem 1rem; }

.provenance-strip {
  display: flex; align-items: center; gap: .45rem;
  padding: .45rem .65rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-raised);
  color: var(--aw-ink-soft);
  font-size: var(--aw-text-xs);
  line-height: 1.35;
}
.provenance-strip[data-state='ai'] {
  border-color: var(--aw-accent-line);
  background: var(--aw-accent-soft);
  color: var(--aw-accent);
}
.provenance-strip[data-state='reviewed'] {
  border-color: var(--aw-ok-line);
  background: var(--aw-ok-soft);
  color: var(--aw-ok);
}
.provenance-strip .aw-icon { font-size: var(--aw-text-sm); flex: none; }

.read-card {
  display: flex; flex-direction: column; gap: .65rem;
  padding: .75rem .85rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
}
.process-eyebrow {
  margin: 0;
  color: var(--aw-teal-strong);
  font-size: var(--aw-text-xs);
  font-weight: 700;
}
.prose-block { display: flex; flex-direction: column; gap: .2rem; }
.prose-text { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-sm); line-height: 1.5; }
.prose-text.soft { color: var(--aw-ink-soft); }
.no-control { display: inline-flex; align-items: center; gap: .35rem; margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-sm); font-style: italic; }

.criteria-text { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.45; }
.policy-quote {
  display: flex; flex-direction: column; gap: .25rem;
  margin: 0;
  padding: .5rem .65rem;
  border-left: 3px solid var(--aw-teal);
  border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0;
  background: var(--aw-teal-soft);
}
.quote-meta { display: inline-flex; align-items: center; gap: .35rem; color: var(--aw-teal-strong); font-size: var(--aw-text-2xs); font-weight: 700; }
.quote-meta .aw-icon { font-size: var(--aw-text-xs); }
.page-chip {
  padding: .02rem .35rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-panel);
  border: 1px solid var(--aw-teal-line);
  font-family: var(--aw-font-mono);
}
.cite-id { color: var(--aw-muted); font-family: var(--aw-font-mono); }
.quote-body { margin: 0; color: var(--aw-ink); font-size: var(--aw-text-xs); line-height: 1.45; font-style: italic; }

.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .625rem; }
label { display: flex; flex-direction: column; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; font-size: var(--aw-text-base); font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--aw-ink); }

.group { display: flex; flex-direction: column; gap: .375rem; min-width: 0; }
.group-head { display: flex; align-items: center; gap: .5rem; }
.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap; cursor: pointer; }
.link:hover { text-decoration: underline; }
.link .aw-icon { margin-left: .25rem; font-size: var(--aw-text-2xs); }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.attribute { display: flex; flex-direction: column; gap: .3rem; padding: .5rem .625rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.attr-tags { display: flex; align-items: center; gap: .35rem; flex-wrap: wrap; }
.assertion { padding: .0625rem .4375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 700; }
.ev-kind { padding: .05rem .4rem; border-radius: var(--aw-radius-pill); background: var(--aw-panel); border: 1px solid var(--aw-border); color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; }
.requirement { min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.4; }

.test {
  display: flex; align-items: center; gap: .5rem; width: 100%;
  padding: .5rem .625rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.test:hover { background: var(--aw-raised); }
.dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='bad'] { background: var(--aw-danger); }
.test-kind {
  flex: none;
  padding: .04rem .38rem;
  border-radius: var(--aw-radius-pill);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
}
.test-kind[data-kind='datatest'] { background: var(--aw-teal-soft); color: var(--aw-teal-strong); border: 1px solid var(--aw-teal-line); }
.test-kind[data-kind='doctest'] { background: var(--aw-accent-soft); color: var(--aw-accent); border: 1px solid var(--aw-accent-line); }
.test-title { flex: 1; min-width: 0; overflow: hidden; color: var(--aw-ink-strong); font-size: var(--aw-text-sm); text-overflow: ellipsis; white-space: nowrap; }
.open { flex: none; color: var(--aw-danger); font-size: var(--aw-text-xs); font-weight: 600; }

.conclusion { display: flex; flex-direction: column; gap: .375rem; padding: .625rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.conclusion[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); }
.conclusion[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); }
.conclusion[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); }
.conclusion-head { display: flex; align-items: center; gap: .5rem; }
.conclusion-head strong { font-size: var(--aw-text-base); }
.conclusion[data-tone='bad'] .conclusion-head strong, .conclusion[data-tone='bad'] .link { color: var(--aw-danger-ink); }
.conclusion[data-tone='warn'] .conclusion-head strong, .conclusion[data-tone='warn'] .link { color: var(--aw-warn-ink); }
.conclusion[data-tone='ok'] .conclusion-head strong, .conclusion[data-tone='ok'] .link { color: var(--aw-ok); }
.by-agent { margin: 0; color: var(--aw-accent); font-size: var(--aw-text-xs); }
.finding { margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); }

.drawer-foot { display: flex; align-items: center; gap: .5rem; padding: .75rem 1rem; border-top: 1px solid var(--aw-border); background: var(--aw-canvas); }
.toggle { flex-direction: row; align-items: center; gap: .5rem; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 500; letter-spacing: 0; text-transform: none; }
.signed-chip {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .25rem .6rem;
  border: 1px solid var(--aw-ok-line);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-ok-soft);
  color: var(--aw-ok);
  font-size: var(--aw-text-xs); font-weight: 700;
}
</style>
