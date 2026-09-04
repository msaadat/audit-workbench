<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'

import type { FindingSummary, RcmRow, TestRollup } from '../../types'

/**
 * One row, quickly.
 *
 * This replaces a 1120px modal that carried the whole record — control type,
 * owner, criteria and their citations, the transaction-cycle comparison
 * editor, provenance, the exception observations — with no URL a reviewer
 * could send. What is here is what a reader changes while walking the matrix:
 * the process, the rating, the two statements, and the sign-off. Everything
 * else is on the row's own page, one click away through the id in the header.
 *
 * It edits a copy. Binding the fields to the row itself meant an abandoned
 * edit stayed on screen until the next reload, and a filtered grid could move
 * the row out from under the form mid-edit.
 */

const props = defineProps<{
  row: RcmRow
  findings: FindingSummary[]
  saving?: boolean
}>()
const emit = defineEmits<{
  save: [changes: Partial<RcmRow>]
  close: []
  paper: []
  openRow: [tab?: string]
  openTest: [rollup: TestRollup]
  addTest: [kind: 'data' | 'document' | 'generate']
}>()

const ratings = ['low', 'medium', 'high', 'critical']
const CONCLUSIONS: Record<string, { label: string; tone: string }> = {
  effective: { label: 'Effective', tone: 'ok' },
  partially_effective: { label: 'Partially effective', tone: 'warn' },
  ineffective: { label: 'Ineffective', tone: 'bad' },
  not_applicable: { label: 'Not applicable', tone: 'neutral' },
}

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
watch(() => props.row.id, () => { draft.value = snapshot() })
watch(() => props.row.updated, () => { draft.value = snapshot() })

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

const addMenu = ref<InstanceType<typeof Menu> | null>(null)
const addOptions = [
  { label: 'Data test', icon: 'pi pi-chart-bar', command: () => emit('addTest', 'data') },
  { label: 'Document test', icon: 'pi pi-file-check', command: () => emit('addTest', 'document') },
  { separator: true },
  // The sparkle that used to sit in the grid's action column, per row, for the
  // rows that had no test. It belongs where the tests are listed.
  { label: 'Generate with assistant', icon: 'pi pi-sparkles', command: () => emit('addTest', 'generate') },
]

function testTone(rollup: TestRollup) {
  if (rollup.open_exception_count) return 'bad'
  if (String(rollup.status).startsWith('completed')) return 'ok'
  return 'neutral'
}
</script>

<template>
  <section class="drawer" aria-label="RCM row">
    <header class="drawer-head">
      <!-- The id is the link to everything this drawer does not hold. -->
      <button type="button" class="row-id" @click="emit('openRow')">
        {{ row.id }}<i class="pi pi-arrow-up-right" />
      </button>
      <span class="rating" :data-rating="row.risk_rating"><span class="rating-dot" />{{ row.risk_rating }}</span>
      <span class="grow" />
      <button type="button" class="link" @click="emit('paper')">Working paper</button>
      <button type="button" class="close" aria-label="Close" @click="emit('close')">
        <i class="pi pi-times" />
      </button>
    </header>

    <div class="drawer-body">
      <div class="pair">
        <label>Process<InputText v-model="draft.process" size="small" /></label>
        <label>Rating<Select v-model="draft.risk_rating" :options="ratings" size="small" /></label>
      </div>
      <label>Risk<Textarea v-model="draft.risk" rows="3" autoResize /></label>
      <label>Control<Textarea v-model="draft.control" rows="3" autoResize placeholder="No control identified" /></label>

      <section class="group">
        <div class="group-head">
          <p class="aw-label">Attributes · {{ row.control_attributes.length }}</p>
          <span class="grow" />
          <button type="button" class="link" @click="emit('openRow', 'attributes')">Add attribute</button>
        </div>
        <div v-for="attribute in row.control_attributes" :key="attribute.key" class="attribute">
          <span class="assertion">{{ attribute.assertion }}</span>
          <span class="requirement">{{ attribute.requirement }}</span>
        </div>
        <p v-if="!row.control_attributes.length" class="muted">No attribute is recorded against this control.</p>
      </section>

      <section class="group">
        <div class="group-head">
          <p class="aw-label">Tests · {{ tests.length }}</p>
          <span class="grow" />
          <button type="button" class="link" aria-haspopup="true" @click="addMenu?.toggle($event)">
            Add test<i class="pi pi-chevron-down" />
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
          <span class="test-title">{{ rollup.title }}</span>
          <span v-if="rollup.open_exception_count" class="open">{{ rollup.open_exception_count }} open</span>
        </button>
        <p v-if="!tests.length" class="muted">This risk has no linked test and cannot pass coverage.</p>
      </section>

      <!-- The conclusion is rolled up from the tests, so it is stated here and
           changed where it is reached. `Change` goes to the tests behind it
           rather than offering a control that cannot write. -->
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
      <label class="toggle">
        <ToggleSwitch v-model="reviewed" />
        <span>Mark reviewed</span>
      </label>
      <span class="grow" />
      <Button label="Cancel" size="small" outlined severity="secondary" @click="emit('close')" />
      <Button label="Save row" size="small" :loading="saving" @click="emit('save', { ...draft })" />
    </footer>
  </section>
</template>

<style scoped>
.drawer { display: flex; flex-direction: column; min-width: 0; height: 100%; }
.grow { flex: 1; }

.drawer-head { display: flex; align-items: center; gap: .625rem; padding: .75rem 1rem; border-bottom: 1px solid var(--aw-border); }
.row-id {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: 0; border: 0; background: none; color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.row-id:hover { color: var(--aw-teal); }
.row-id .pi { font-size: var(--aw-text-2xs); }
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
.pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .625rem; }
label { display: flex; flex-direction: column; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; font-size: var(--aw-text-base); font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--aw-ink); }

.group { display: flex; flex-direction: column; gap: .375rem; min-width: 0; }
.group-head { display: flex; align-items: center; gap: .5rem; }
.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.link:hover { text-decoration: underline; }
.link .pi { margin-left: .25rem; font-size: var(--aw-text-2xs); }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.attribute { display: flex; gap: .5rem; padding: .5rem .625rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.assertion { flex: none; align-self: flex-start; padding: .0625rem .4375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
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

.drawer-foot { display: flex; align-items: center; gap: .625rem; padding: .75rem 1rem; border-top: 1px solid var(--aw-border); background: var(--aw-canvas); }
.toggle { flex-direction: row; align-items: center; gap: .5rem; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 500; letter-spacing: 0; text-transform: none; }
</style>
