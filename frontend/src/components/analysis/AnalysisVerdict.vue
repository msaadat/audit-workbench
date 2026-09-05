<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Select from 'primevue/select'

import type {
  AnalysisLastResult, AnalysisResultState, AnalysisSummaryClassification,
} from '../../types'
import { isManualResult } from '../../types'
import UiVerdictBar from '../ui/UiVerdictBar.vue'
import { formatExecutedAt } from './classification'
import { classificationTone } from './analysisStatus'

/**
 * What this procedure found, and what is recorded about it.
 *
 * It replaces `AnalysisOutcome`, which stated one of those and restated the
 * classification the list had already shown. The two facts are different in
 * kind — the first is about the data, the second about the file — and the page
 * that gets them confused is the one where a green result and an unread
 * unattended conclusion look the same.
 *
 * Staleness is the strip under the card rather than a replacement for the
 * verdict. A definition edited since the run does not un-find three breaches;
 * it means nobody has checked whether they are still there.
 */

const props = defineProps<{
  classification: AnalysisSummaryClassification
  state: AnalysisResultState
  result?: AnalysisLastResult | null
  /**
   * How returned rows are read. Only a Polars procedure has one — an analytics
   * test carries its own verdict — so the control is absent for the rest
   * rather than offering a choice that decides nothing.
   */
  policy?: 'exception_rows' | 'informational' | null
  busy?: boolean
}>()
const emit = defineEmits<{
  openRun: [runId: string]
  run: []
  'update:policy': [value: 'exception_rows' | 'informational']
}>()

const POLICY_OPTIONS = [
  { label: 'Exceptions', value: 'exception_rows' },
  { label: 'Informational', value: 'informational' },
]

const tone = computed(() => classificationTone(props.classification))

/** What the run found: the figures first, then when it ran. */
const failed = computed(() => props.result?.exception_count ?? 0)
const population = computed(() => props.result?.population ?? null)
const tested = computed(() => props.result?.tested ?? null)
const rate = computed(() => {
  const value = props.result?.exception_rate
  return typeof value === 'number' ? `${Math.round(value * 1000) / 10}%` : ''
})
/** A denominator that covers less than the population is itself the finding. */
const notComparable = computed(() => {
  const whole = population.value
  const part = tested.value
  return whole && part && part < whole ? whole - part : 0
})
const ranAt = computed(() => formatExecutedAt(props.result?.executed_at))

/** An agent run is inspectable; a manual execution is not a run at all. */
const agentRunId = computed(() =>
  props.result && !isManualResult(props.result) ? props.result.run_id : null,
)

const STALE = 'The definition or its source data changed after this result was recorded, '
  + 'so what it says is what the procedure found then, not what it would find now.'
</script>

<template>
  <UiVerdictBar :tone="tone" :stale="state === 'stale' ? STALE : ''">
    <template #found>
      <template v-if="!result">
        <span>This procedure has not been run yet.</span>
      </template>
      <template v-else-if="result.error">
        <span class="failed">Could not run</span>
        <code class="error">{{ result.error }}</code>
      </template>
      <template v-else-if="failed">
        <span class="aw-figure">
          {{ failed }}<template v-if="population"> of {{ population }}</template>
          {{ failed === 1 && !population ? 'row' : 'rows' }}
          {{ classification === 'exception' ? 'failed' : 'flagged' }}
        </span>
        <span v-if="rate" class="rate aw-figure" :data-heavy="(result.exception_rate ?? 0) >= 0.1">{{ rate }}</span>
      </template>
      <template v-else>
        <span class="aw-figure">
          No exceptions<template v-if="tested"> · {{ tested }} of {{ population ?? tested }} compared</template>
        </span>
      </template>
      <span v-if="ranAt" class="meta aw-figure">· run {{ ranAt }}</span>
    </template>

    <template #recorded>
      <template v-if="!result">
        Nothing is recorded against it, so it counts towards no coverage.
      </template>
      <template v-else>
        <!-- What is *recorded*, never a restatement of what was found: the
             line above already says that, and a second telling of it is what
             the four-times-over layouts this replaced were made of. -->
        <template v-if="policy">
          Returned rows are read as
          <b>{{ policy === 'exception_rows' ? 'exceptions' : 'informational' }}</b>.
        </template>
        <template v-if="isManualResult(result)">Run from this page.</template>
        <template v-else>
          Recorded by an unattended run.
          <button v-if="agentRunId" type="button" class="run-link" @click="emit('openRun', agentRunId)">
            View the run
          </button>
        </template>
        <span v-if="notComparable" class="meta">
          {{ notComparable }} rows could not be compared and no conclusion covers them.
        </span>
      </template>
    </template>

    <template #actions>
      <Select
        v-if="policy"
        :modelValue="policy"
        :options="POLICY_OPTIONS"
        optionLabel="label"
        optionValue="value"
        size="small"
        aria-label="How returned rows are read"
        @update:modelValue="emit('update:policy', $event)"
      />
      <Button
        label="Run"
        icon="pi pi-play"
        size="small"
        :loading="busy"
        v-tooltip.bottom="'Execute this procedure and record what it concludes'"
        @click="emit('run')"
      />
    </template>
  </UiVerdictBar>
</template>

<style scoped>
.failed { color: var(--aw-danger); }
.error { font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); font-weight: 500; }
.rate { color: var(--aw-muted); font-weight: 700; }
.rate[data-heavy='true'] { color: var(--aw-danger); }
.meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }
.run-link {
  padding: 0; border: 0; background: none;
  color: var(--aw-teal); font: inherit; font-weight: 600; cursor: pointer;
}
.run-link:hover { text-decoration: underline; }
</style>
