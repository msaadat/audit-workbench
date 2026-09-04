<script setup lang="ts">
import { computed, ref } from 'vue'

import type { DataTest, DataTestDispositionState, DataTestResult } from '../../types'
import ExceptionExplorer from './ExceptionExplorer.vue'
import FrameTable from '../FrameTable.vue'
import ProvenanceRail from '../agent/ProvenanceRail.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'
import { plural } from '../../format'

const props = defineProps<{
  test: DataTest
  result: DataTestResult | null
  busy?: boolean
  /** Optional so the panel stays mountable in a bare test harness. */
  workspaceId?: string
}>()
const emit = defineEmits<{
  (event: 'rule', payload: { key: string; state: DataTestDispositionState; note: string }): void
  (event: 'review-semantics', note: string): void
}>()

/**
 * What the run actually did, under the verdict bar that says what it found.
 *
 * Everything that restated the outcome is gone from here: the status chip, the
 * headline sentence, the two-readings line, the statistics tiles and the two
 * stale banners were five renderings of two facts, and both facts are now
 * stated once, above, by `UiVerdictBar`. What is left is the evidence — which
 * reason caught which rows, and the machinery behind it, folded away.
 */

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

// The runner often repeats the failure as verdict_text and again as a semantic
// issue. The verdict bar says it once; these are the sentences it does not
// carry.
const issues = computed(() => {
  const shown = new Set([props.result?.error ?? '', props.result?.verdict_text ?? ''])
  return (props.result?.semantic_issues ?? []).filter(issue => !shown.has(issue))
})
const warnings = computed(() => {
  const shown = new Set(issues.value)
  return (props.test.semantic_warnings ?? []).filter(warning => !shown.has(warning))
})
const hasFollowUp = computed(() => Boolean(props.test.next_action || props.test.scope_limitations))

// A run that produced no usable evidence is what holds a test back — not the
// warnings beside it, which qualify a result rather than withhold one.
// Recording that somebody read the failure is what releases the test; the note
// explaining why is welcome but not the price of releasing it.
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

/**
 * The machinery, behind three chevrons on one line.
 *
 * Each of these answers a question somebody asks once per engagement — which
 * checks ran, what the summary frame held, who wrote the definition — so they
 * are one row of links rather than three permanent blocks. Open state is local
 * because it is a reading posture, not a property of the test.
 */
const open = ref<Record<string, boolean>>({})
function toggle(key: string) { open.value[key] = !open.value[key] }
const disclosures = computed(() => [
  {
    key: 'steps',
    label: `Checks that ran · ${props.result?.step_results?.length ?? 0}`,
    shown: Boolean(props.result?.step_results?.length),
  },
  { key: 'summary', label: 'Summary output', shown: Boolean(props.result?.summary_frame) },
  { key: 'provenance', label: 'Where this came from', shown: Boolean(props.workspaceId) },
].filter(item => item.shown))
</script>

<template>
  <section class="result">
    <!-- What the runner could not vouch for. It qualifies the result and the
         conclusion drawn from it; it does not withhold either. -->
    <div v-if="issues.length || warnings.length" class="warnings">
      <p class="aw-label">Read these before relying on the result</p>
      <ul>
        <li v-for="issue in [...issues, ...warnings]" :key="issue">{{ issue }}</li>
      </ul>
      <p v-if="test.semantic_review" class="reviewed">
        Reviewed {{ new Date(test.semantic_review.at).toLocaleDateString() }}:
        “{{ test.semantic_review.note }}”
      </p>
    </div>

    <!-- The run itself produced nothing to read, so there is nothing to
         conclude over. Without this the test is stranded: it cannot be released
         and nobody could say why the failure does not matter. -->
    <div v-if="needsSemanticReview" class="warnings" data-blocking="true">
      <p class="aw-label">This run produced no usable evidence</p>
      <p class="warn-body">
        Run it again once the definition or the data is fixed, or record that you
        have read what went wrong and it does not stand against the control.
      </p>
      <button v-if="!reviewing" type="button" class="link" @click="reviewing = true">
        I have reviewed this and it does not invalidate the result
      </button>
      <form v-else class="review" @submit.prevent="commitReview">
        <label>
          Why this does not invalidate the result <span class="optional">(optional)</span>
          <textarea v-model="reviewNote" rows="2" />
        </label>
        <span class="review-actions">
          <button type="submit" class="link" :disabled="busy">Record review</button>
          <button type="button" class="link" @click="reviewing = false">Cancel</button>
        </span>
      </form>
    </div>

    <ExceptionExplorer
      v-if="profile && result?.exception_frame"
      :profile="profile"
      :frame="result.exception_frame"
      :dispositions="test.exception_dispositions"
      :busy="busy"
      @rule="emit('rule', $event)"
    />

    <!-- Where there is no exception profile the engine's own figures are all
         there is to show. With one, the explorer states the outcome in records
         and these would only repeat it in a larger typeface. -->
    <div v-if="result?.statistics.length && !profile" class="stats">
      <span v-for="stat in result.statistics" :key="stat.label">
        <small>{{ stat.label }}</small>
        <strong>{{ stat.value }}</strong>
      </span>
    </div>
    <details v-if="!profile && result?.exception_frame" class="raw" open>
      <summary>Exception rows ({{ result.exception_count }})</summary>
      <FrameTable :frame="result.exception_frame" scrollHeight="24rem" />
    </details>

    <div v-if="hasFollowUp" class="follow-up">
      <p class="aw-label">Follow-up</p>
      <dl>
        <template v-if="test.next_action">
          <dt>Next action</dt><dd>{{ test.next_action }}</dd>
        </template>
        <template v-if="test.scope_limitations">
          <dt>Limitation</dt><dd>{{ test.scope_limitations }}</dd>
        </template>
      </dl>
    </div>

    <div v-if="disclosures.length" class="disclosures">
      <p class="disclosure-links">
        <button
          v-for="item in disclosures"
          :key="item.key"
          type="button"
          class="disclosure-link"
          :aria-expanded="Boolean(open[item.key])"
          @click="toggle(item.key)"
        >
          <i class="pi" :class="open[item.key] ? 'pi-chevron-down' : 'pi-chevron-right'" />{{ item.label }}
        </button>
      </p>

      <div v-if="open.steps && result?.step_results?.length" class="steps">
        <div v-for="step in result.step_results" :key="step.step_id" class="step-row">
          <UiTestStatus :status="step.status" showLabel />
          <span>{{ step.step_label }}</span>
          <small>{{ plural(step.exception_count, 'exception row') }}</small>
          <small v-if="step.error" class="step-error">{{ step.error }}</small>
        </div>
      </div>
      <FrameTable v-if="open.summary && result?.summary_frame" :frame="result.summary_frame" scrollHeight="22rem" />
      <ProvenanceRail
        v-if="open.provenance && workspaceId"
        :key="test.id"
        :workspaceId="workspaceId"
        :artifactRef="`datatest:${test.id}`"
      />
    </div>
  </section>
</template>

<style scoped>
.result { display: flex; flex-direction: column; gap: 1rem; min-width: 0; }

/* Tinted rather than bordered: it is a qualification on the block below it,
   not a panel of its own. */
.warnings { display: flex; flex-direction: column; gap: .35rem; padding: .625rem .875rem; border-left: 3px solid var(--aw-warn); border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0; background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.warnings[data-blocking='true'] { border-left-color: var(--aw-danger); }
.warnings ul { margin: 0; padding-left: 1.1rem; font-size: var(--aw-text-sm); line-height: 1.5; }
.warn-body { margin: 0; font-size: var(--aw-text-sm); line-height: 1.5; }
.reviewed { margin: 0; font-size: var(--aw-text-xs); line-height: 1.5; }

.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-sm); text-decoration: underline; cursor: pointer; text-align: left; }
.link[disabled] { color: var(--aw-muted); cursor: default; text-decoration: none; }
.review { display: flex; flex-direction: column; gap: .4rem; margin-top: .25rem; }
.review label { display: flex; flex-direction: column; gap: .25rem; font-size: var(--aw-text-xs); font-weight: 700; }
.review textarea {
  width: 100%;
  padding: .4rem .5rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  font-size: var(--aw-text-sm);
  resize: vertical;
}
.optional { font-weight: 400; }
.review-actions { display: flex; gap: .7rem; }

.stats { display: flex; flex-wrap: wrap; gap: .5rem; min-width: 0; }
.stats span { display: flex; flex: 1 1 7rem; flex-direction: column; min-width: 0; padding: .5rem .6rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.stats small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats strong { font-size: var(--aw-text-lg); }

.raw { min-width: 0; max-width: 100%; overflow-x: auto; padding: .6rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.raw summary { margin-bottom: .5rem; font-size: var(--aw-text-sm); font-weight: 700; cursor: pointer; }

.follow-up { min-width: 0; }
.follow-up dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .25rem .7rem; margin: .35rem 0 0; font-size: var(--aw-text-sm); }
.follow-up dt { color: var(--aw-muted); font-weight: 600; }
.follow-up dd { margin: 0; }

/* One row of three, so the machinery reads as one thing put away rather than
   as three sections nobody opened. */
.disclosures { display: flex; flex-direction: column; gap: .6rem; min-width: 0; }
.disclosure-links { display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; margin: 0; }
.disclosure-link {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: 0; border: 0; background: none; color: var(--aw-muted);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer;
}
.disclosure-link:hover { color: var(--aw-teal); }
.disclosure-link:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.disclosure-link .pi { font-size: var(--aw-text-2xs); }

.steps { min-width: 0; }
.step-row { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; min-width: 0; padding: .4rem .55rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); font-size: var(--aw-text-sm); }
.step-row + .step-row { margin-top: .3rem; }
.step-row small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.step-error { color: var(--aw-danger); overflow-wrap: anywhere; }

@container master-detail-content (max-width: 34rem) {
  .follow-up dl { grid-template-columns: minmax(0, 1fr); }
}
</style>
