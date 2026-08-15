<script setup lang="ts">
import { computed, ref } from 'vue'
import Message from 'primevue/message'

import type { DataTest, DataTestDispositionState, DataTestResult } from '../../types'
import ExceptionExplorer from './ExceptionExplorer.vue'
import FrameTable from '../FrameTable.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'
import { plural } from '../../format'

const props = defineProps<{ test: DataTest; result: DataTestResult | null; busy?: boolean }>()
const emit = defineEmits<{
  (event: 'rule', payload: { key: string; state: DataTestDispositionState; note: string }): void
  (event: 'review-semantics', note: string): void
  (event: 'run'): void
}>()

/**
 * The record-level reading of the exceptions, and the groups an auditor rules
 * on.
 *
 * The rulable set is always `evaluation.reasons` — the backend refuses a ruling
 * on any label it does not hold, so offering one from anywhere else is an
 * action that cannot succeed. The stored result's profile is richer (it knows
 * the entity key, the population, and which columns each condition reads), so
 * it is used to enrich those groups, never to define them.
 */
const profile = computed(() => {
  const reasons = props.test.evaluation.reasons
  if (!reasons.length) return null
  const stored = props.result?.exception_profile ?? null
  const columnsFor = new Map(
    (stored?.reasons ?? []).map(reason => [reason.label, reason.columns]),
  )
  const fallbackColumns = (props.result?.exception_frame?.columns ?? []).filter(
    column => !column.startsWith('_'),
  )
  return {
    entity_key: stored?.entity_key ?? null,
    record_count: stored?.record_count ?? props.test.evaluation.exception_count,
    row_count: stored?.row_count ?? props.test.evaluation.exception_count,
    population: stored?.population ?? null,
    population_table: stored?.population_table ?? null,
    reason_source: stored?.reason_source ?? ('step' as const),
    reasons: reasons.map(reason => ({
      ...reason,
      columns: columnsFor.get(reason.label) ?? fallbackColumns,
    })),
  }
})

const headline = computed(() => {
  if (props.result?.error) return props.result.error
  return props.result?.verdict_text || props.test.result_summary || 'This test has not been run yet.'
})
// The runner often repeats the failure as verdict_text and again as a semantic
// issue. Say it once, in the headline, and drop the echoes.
const issues = computed(() => {
  const shown = new Set([headline.value, props.result?.error ?? '', props.result?.verdict_text ?? ''])
  return (props.result?.semantic_issues ?? []).filter(issue => !shown.has(issue))
})
const warnings = computed(() => {
  const shown = new Set([headline.value, ...issues.value])
  return (props.test.semantic_warnings ?? []).filter(warning => !shown.has(warning))
})
const ranAt = computed(() => {
  const runAt = props.result?.run_at ?? props.test.last_run?.run_at
  return runAt ? new Date(runAt).toLocaleString() : null
})
// The conclusion and control conclusion are edited live directly below this
// panel, so echoing them read-only here just showed the same words twice.
// What the runner produced and the auditor cannot edit still belongs here.
const hasFollowUp = computed(() => Boolean(props.test.next_action || props.test.scope_limitations))

// What the run found, said in the run's own voice. The status chip beside it is
// the joint reading of the run and the rulings, which is a different sentence
// once somebody has ruled — showing only one of the two was the whole defect.
const RUN_VERDICTS: Record<string, string> = {
  not_run: 'Not run',
  passed: 'The run found no exceptions',
  failed: 'The run found exceptions',
  inconclusive: 'The run could not produce reliable evidence',
}
const runVerdict = computed(() => RUN_VERDICTS[props.test.evaluation.state] ?? 'Not run')
const openCount = computed(() => props.test.open_exception_count)
const foundCount = computed(() => props.test.evaluation.exception_count)
const settled = computed(() => Boolean(foundCount.value) && openCount.value === 0)
// Semantic issues are what block a conclusion. Recording that somebody read
// them is what releases the test; the note explaining why is welcome but not
// the price of releasing it.
const needsSemanticReview = computed(
  () => props.test.evaluation.state === 'inconclusive' && !props.test.semantic_review,
)
const reviewing = ref(false)
const reviewNote = ref('')
function commitReview() {
  emit('review-semantics', reviewNote.value.trim())
  reviewing.value = false
  reviewNote.value = ''
}
</script>

<template>
  <section class="result" :data-status="result?.status ?? test.status">
    <!-- The outcome leads. Editing the definition is the rarer action and now
         sits below, collapsed. With an exception profile the outcome is stated
         once, by the explorer, in records; repeating the runner's sentence above
         it said the same thing twice in two different voices. -->
    <header>
      <div class="headline">
        <UiTestStatus :status="test.status" showLabel />
        <p v-if="!profile" :class="{ failed: Boolean(result?.error) }">{{ headline }}</p>
      </div>
      <small v-if="ranAt" class="muted">Run {{ ranAt }}</small>
      <small v-else class="muted">Never run</small>
    </header>

    <!-- The run itself is out of date. This qualifies everything below it, so
         it goes first: the readings, the rulings, and the conclusion all
         describe a state of the workspace that no longer holds. -->
    <Message v-if="test.result_stale" severity="warn" :closable="false">
      <strong>This result is out of date</strong>
      <p class="stale-body">
        The definition or the data has changed since this run. Everything below
        describes the earlier state.
        <button type="button" class="link" :disabled="busy" @click="emit('run')">
          Run it again
        </button>
      </p>
    </Message>

    <!-- Two readings, never one. The run's verdict is a fact about the data and
         does not change when somebody rules on it; the status above is the
         joint reading and does. -->
    <p class="two-readings" :data-stale="test.result_stale">
      <span class="run-said">{{ runVerdict }}<template v-if="foundCount">: {{ plural(foundCount, 'exception row') }}</template>.</span>
      <span v-if="foundCount" class="still-open" :data-settled="settled">
        <template v-if="settled">All accepted on review; none stand against the control.</template>
        <template v-else>{{ openCount }} still open.</template>
      </span>
    </p>

    <Message v-if="test.control_conclusion_stale" severity="warn" :closable="false">
      <strong>The conclusion on this test is out of date</strong>
      <p class="stale-body">
        It was recorded against a different definition or a different state of the
        data. Re-affirm it, or change it, in the conclusion panel.
      </p>
    </Message>

    <Message v-if="issues.length" severity="warn" :closable="false">
      <strong>Review these before relying on the result</strong>
      <ul class="issues">
        <li v-for="issue in issues" :key="issue">{{ issue }}</li>
      </ul>
      <!-- Without this the test is stranded: the runner will not conclude over
           evidence it cannot vouch for, and nobody could say why it is fine. -->
      <template v-if="needsSemanticReview">
        <button v-if="!reviewing" type="button" class="link" @click="reviewing = true">
          I have reviewed these and they do not invalidate the result
        </button>
        <form v-else class="review" @submit.prevent="commitReview">
          <label>
            Why these issues do not invalidate the result <span class="optional">(optional)</span>
            <textarea v-model="reviewNote" rows="2" />
          </label>
          <span class="review-actions">
            <button type="submit" class="link" :disabled="busy">
              Record review
            </button>
            <button type="button" class="link" @click="reviewing = false">Cancel</button>
          </span>
        </form>
      </template>
      <p v-else-if="test.semantic_review" class="reviewed">
        Reviewed {{ new Date(test.semantic_review.at).toLocaleDateString() }}:
        “{{ test.semantic_review.note }}”
      </p>
    </Message>
    <Message v-if="warnings.length" severity="warn" :closable="false">
      <ul class="issues">
        <li v-for="warning in warnings" :key="warning">{{ warning }}</li>
      </ul>
    </Message>

    <ExceptionExplorer
      v-if="profile && result?.exception_frame"
      :profile="profile"
      :frame="result.exception_frame"
      :dispositions="test.exception_dispositions"
      :busy="busy"
      @rule="emit('rule', $event)"
    />

    <!-- Statistics are the engine's own figures. Where the exception profile
         exists it already states the outcome in records, so the tiles would only
         repeat the headline back in a larger typeface. -->
    <div v-if="result?.statistics.length && !profile" class="stats">
      <span v-for="stat in result.statistics" :key="stat.label">
        <small>{{ stat.label }}</small>
        <strong>{{ stat.value }}</strong>
      </span>
    </div>

    <!-- Which checks ran, below the result they produced. A step that found
         nothing is still scope, so it stays; it just stops leading. -->
    <div v-if="result?.step_results?.length" class="steps">
      <p class="block-head">Checks that ran</p>
      <div v-for="step in result.step_results" :key="step.step_id" class="step-row">
        <UiTestStatus :status="step.status" showLabel />
        <span>{{ step.step_label }}</span>
        <small>{{ plural(step.exception_count, 'exception row') }}</small>
        <small v-if="step.error" class="step-error">{{ step.error }}</small>
      </div>
    </div>

    <div v-if="hasFollowUp" class="outcome">
      <p class="block-head">Follow-up</p>
      <dl>
        <template v-if="test.next_action">
          <dt>Next action</dt><dd>{{ test.next_action }}</dd>
        </template>
        <template v-if="test.scope_limitations">
          <dt>Limitation</dt><dd>{{ test.scope_limitations }}</dd>
        </template>
      </dl>
    </div>

    <details v-if="!profile && result?.exception_frame" open>
      <summary>Exception rows ({{ result.exception_count }})</summary>
      <FrameTable :frame="result.exception_frame" scrollHeight="24rem" />
    </details>
    <details v-if="result?.summary_frame">
      <summary>Summary output</summary>
      <FrameTable :frame="result.summary_frame" scrollHeight="22rem" />
    </details>
  </section>
</template>

<style scoped>
/* The result is the primary content of the detail panel, not a card floating
   inside it. The status rule on the left is the only chrome it needs. */
.result { display: flex; flex-direction: column; gap: 0.7rem; min-width: 0; padding: 0.1rem 0 0.5rem 0.85rem; border-left: 3px solid var(--aw-border-strong); }
.result[data-status='completed_with_exception'] { border-left-color: var(--aw-danger); }
.result[data-status='completed_no_exception'] { border-left-color: var(--aw-ok); }
.result[data-status='review_required'], .result[data-status='blocked'] { border-left-color: var(--aw-warn); }

header { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.8rem; min-width: 0; }
.headline { display: flex; align-items: flex-start; gap: 0.5rem; min-width: 0; }
.headline p { margin: 0; font-size: var(--aw-text-md); font-weight: 600; line-height: 1.45; overflow-wrap: anywhere; }
.headline p.failed { color: var(--aw-danger); }
.muted { flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-xs); white-space: nowrap; }

.block-head { margin: 0 0 0.35rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.issues { margin: 0.25rem 0 0; padding-left: 1.1rem; }

/* What the run found, and what is left of it. Two facts on one line because
   they are read together and mean different things. */
.two-readings { display: flex; flex-wrap: wrap; gap: 0.25rem 0.6rem; margin: 0; font-size: var(--aw-text-sm); line-height: 1.5; }
/* Figures the banner above has just said are about an earlier state. */
.two-readings[data-stale='true'] { opacity: 0.65; }
.run-said { color: var(--aw-muted); }
.still-open { font-weight: 700; color: var(--aw-danger); }
.still-open[data-settled='true'] { color: var(--aw-ok); }
.stale-body { margin: 0.25rem 0 0; font-size: var(--aw-text-sm); line-height: 1.5; }

.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-sm); text-decoration: underline; cursor: pointer; }
.link[disabled] { color: var(--aw-muted); cursor: default; text-decoration: none; }
.review { display: flex; flex-direction: column; gap: 0.4rem; margin-top: 0.5rem; }
.review label { display: flex; flex-direction: column; gap: 0.25rem; font-size: var(--aw-text-xs); font-weight: 700; }
.review textarea {
  width: 100%;
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  font-size: var(--aw-text-sm);
  resize: vertical;
}
.optional { color: var(--aw-muted); font-weight: 400; }
.review-actions { display: flex; gap: 0.7rem; }
.reviewed { margin: 0.4rem 0 0; font-size: var(--aw-text-xs); line-height: 1.5; }

.stats { display: flex; flex-wrap: wrap; gap: 0.5rem; min-width: 0; }
.stats span { display: flex; flex: 1 1 7rem; flex-direction: column; min-width: 0; padding: 0.5rem 0.6rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.stats small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats strong { font-size: var(--aw-text-lg); }

.steps { min-width: 0; }
.step-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; min-width: 0; padding: 0.4rem 0.55rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); font-size: var(--aw-text-sm); }
.step-row + .step-row { margin-top: 0.3rem; }
.step-row small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.step-error { color: var(--aw-danger); overflow-wrap: anywhere; }

.outcome p { margin: 0 0 0.3rem; font-size: var(--aw-text-base); line-height: 1.5; }
.outcome dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.25rem 0.7rem; margin: 0; font-size: var(--aw-text-sm); }
.outcome dt { color: var(--aw-muted); font-weight: 600; }
.outcome dd { margin: 0; }

details { min-width: 0; max-width: 100%; overflow-x: auto; border-radius: var(--aw-radius-control); background: var(--aw-raised); padding: 0.6rem; }
summary { cursor: pointer; font-size: var(--aw-text-sm); font-weight: 700; margin-bottom: 0.5rem; }

@container master-detail-content (max-width: 32rem) {
  header { flex-direction: column; }
  .outcome dl { grid-template-columns: minmax(0, 1fr); }
}
</style>
