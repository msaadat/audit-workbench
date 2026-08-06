<script setup lang="ts">
import { computed } from 'vue'
import Message from 'primevue/message'

import type { DataTest, DataTestResult } from '../../types'
import FrameTable from '../FrameTable.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'

const props = defineProps<{ test: DataTest; result: DataTestResult | null }>()

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
</script>

<template>
  <section class="result" :data-status="result?.status ?? test.status">
    <!-- The outcome leads. Editing the definition is the rarer action and now
         sits below, collapsed. -->
    <header>
      <div class="headline">
        <UiTestStatus :status="result?.status ?? test.status" showLabel />
        <p :class="{ failed: Boolean(result?.error) }">{{ headline }}</p>
      </div>
      <small v-if="ranAt" class="muted">Run {{ ranAt }}</small>
      <small v-else class="muted">Never run</small>
    </header>

    <Message v-if="issues.length" severity="warn" :closable="false">
      <strong>Review these before relying on the result</strong>
      <ul class="issues">
        <li v-for="issue in issues" :key="issue">{{ issue }}</li>
      </ul>
    </Message>
    <Message v-if="warnings.length" severity="warn" :closable="false">
      <ul class="issues">
        <li v-for="warning in warnings" :key="warning">{{ warning }}</li>
      </ul>
    </Message>

    <div v-if="result?.statistics.length" class="stats">
      <span v-for="stat in result.statistics" :key="stat.label">
        <small>{{ stat.label }}</small>
        <strong>{{ stat.value }}</strong>
      </span>
      <span v-if="test.open_exception_count">
        <small>Open exceptions</small>
        <strong>{{ test.open_exception_count }}</strong>
      </span>
    </div>

    <div v-if="result?.step_results?.length" class="steps">
      <p class="block-head">Steps</p>
      <div v-for="step in result.step_results" :key="step.step_id" class="step-row">
        <UiTestStatus :status="step.status" showLabel />
        <span>{{ step.step_label }}</span>
        <small>{{ step.exception_count }} exception row(s)</small>
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

    <details v-if="result?.exception_frame" open>
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
.result { display: flex; flex-direction: column; gap: 0.7rem; min-width: 0; padding: 0.9rem; border-left: 3px solid var(--aw-muted); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
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

.stats { display: flex; flex-wrap: wrap; gap: 0.5rem; min-width: 0; }
.stats span { display: flex; flex: 1 1 7rem; flex-direction: column; min-width: 0; padding: 0.5rem 0.6rem; border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.stats small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats strong { font-size: var(--aw-text-lg); }

.steps { min-width: 0; }
.step-row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; min-width: 0; padding: 0.4rem 0.55rem; border-radius: var(--aw-radius-control); background: var(--aw-canvas); font-size: var(--aw-text-sm); }
.step-row + .step-row { margin-top: 0.3rem; }
.step-row small { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.step-error { color: var(--aw-danger); overflow-wrap: anywhere; }

.outcome p { margin: 0 0 0.3rem; font-size: var(--aw-text-base); line-height: 1.5; }
.outcome dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 0.25rem 0.7rem; margin: 0; font-size: var(--aw-text-sm); }
.outcome dt { color: var(--aw-muted); font-weight: 600; }
.outcome dd { margin: 0; }

details { min-width: 0; max-width: 100%; overflow-x: auto; border-radius: var(--aw-radius-control); background: var(--aw-canvas); padding: 0.6rem; }
summary { cursor: pointer; font-size: var(--aw-text-sm); font-weight: 700; margin-bottom: 0.5rem; }

@container master-detail-content (max-width: 32rem) {
  header { flex-direction: column; }
  .outcome dl { grid-template-columns: minmax(0, 1fr); }
}
</style>
