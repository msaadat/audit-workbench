<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import type { AnalysisLastResult, AnalysisSummaryClassification } from '../../types'
import { isManualResult } from '../../types'
import { classificationMeta, formatExecutedAt } from './classification'

// What this procedure concluded, and when. This is the durable record — not the
// live recomputation shown below it — so it is stated once, at the top, in the
// same words the rail and the triage counts use.
const props = defineProps<{
  classification: AnalysisSummaryClassification
  result?: AnalysisLastResult | null
  runId?: string | null
}>()
defineEmits<{ openRun: [runId: string] }>()

const meta = computed(() => classificationMeta(props.classification))
const conclusion = computed(() => {
  const result = props.result
  if (!result) return meta.value.hint
  return result.error || result.verdict_text || meta.value.hint
})
// An agent run is inspectable in the drawer; a manual execution is not a run at
// all, so it names the auditor instead of showing an id that opens nothing.
const agentRunId = computed(() =>
  props.result && !isManualResult(props.result) ? props.result.run_id : null,
)
</script>

<template>
  <section class="outcome" :data-classification="classification">
    <span class="outcome-icon"><i :class="meta.icon" aria-hidden="true" /></span>
    <div class="outcome-copy">
      <div class="outcome-head">
        <p class="eyebrow">Recorded outcome</p>
        <Tag :value="meta.label" :severity="meta.severity" />
      </div>
      <p class="outcome-conclusion">{{ conclusion }}</p>
      <p v-if="result" class="outcome-meta">
        Executed {{ formatExecutedAt(result.executed_at) }}
        <template v-if="isManualResult(result)"> · run from this tab</template>
        <template v-else-if="agentRunId">
          ·
          <Button
            label="View the run"
            size="small"
            text
            class="run-link"
            @click="$emit('openRun', agentRunId)"
          />
        </template>
        <template v-if="result.status === 'ok'">
          · {{ result.row_count.toLocaleString() }} result row{{ result.row_count === 1 ? '' : 's' }}
        </template>
      </p>
    </div>
    <div v-if="result?.stats?.length" class="outcome-stats">
      <span v-for="stat in result.stats.slice(0, 4)" :key="stat.label">
        <small>{{ stat.label }}</small>
        <strong class="aw-figure">{{ stat.value }}</strong>
      </span>
    </div>
  </section>
</template>

<style scoped>
.outcome {
  display: flex;
  align-items: flex-start;
  gap: var(--aw-space-3);
  padding: 0.75rem 0.9rem;
  border: 1px solid var(--aw-border);
  border-left: 4px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  margin-bottom: var(--aw-space-4);
}
.outcome[data-classification='exception'],
.outcome[data-classification='execution_error'] { border-left-color: var(--aw-danger); background: var(--aw-danger-soft); }
.outcome[data-classification='unusual'],
.outcome[data-classification='stale'] { border-left-color: var(--aw-warn); background: var(--aw-warn-soft); }
.outcome[data-classification='clear'] { border-left-color: var(--aw-ok); background: var(--aw-ok-soft); }
.outcome[data-classification='informational'] { border-left-color: var(--aw-teal); }

.outcome-icon { font-size: var(--aw-text-lg); color: var(--aw-muted); line-height: 1.4; }
.outcome[data-classification='exception'] .outcome-icon,
.outcome[data-classification='execution_error'] .outcome-icon { color: var(--aw-danger); }
.outcome[data-classification='unusual'] .outcome-icon,
.outcome[data-classification='stale'] .outcome-icon { color: var(--aw-warn); }
.outcome[data-classification='clear'] .outcome-icon { color: var(--aw-ok); }
.outcome[data-classification='informational'] .outcome-icon { color: var(--aw-teal); }

.outcome-copy { flex: 1; min-width: 0; }
.outcome-head { display: flex; align-items: center; gap: var(--aw-space-2); }
.outcome-head .eyebrow { margin: 0; }
.outcome-conclusion { margin: 0.25rem 0 0; font-size: var(--aw-text-sm); }
.outcome-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 0.3rem; margin: 0.3rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.run-link { padding: 0; min-height: 0; font-size: var(--aw-text-xs); }

.outcome-stats { display: flex; gap: 0.35rem; flex-wrap: wrap; justify-content: flex-end; max-width: 60%; }
.outcome-stats span { display: flex; flex-direction: column; padding: 0.25rem 0.45rem; border-radius: var(--aw-radius-control); background: var(--aw-canvas); font-size: var(--aw-text-2xs); }
.outcome-stats small { color: var(--aw-muted); }

@container master-detail-content (max-width: 40rem) {
  .outcome { flex-wrap: wrap; }
  .outcome-stats { max-width: 100%; justify-content: flex-start; }
}
</style>
