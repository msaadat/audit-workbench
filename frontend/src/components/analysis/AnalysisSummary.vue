<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api } from '../../api'
import type { AnalysisDetail, SavedAnalysis, WorkspaceSummary } from '../../types'
import ChartView from '../ChartView.vue'
import FrameTable from '../FrameTable.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import { classificationMeta } from './classification'

// The EDA overview: the graphs this engagement's procedures produced, and the
// procedures that concluded an exception. Everything else — clear,
// informational, stale, not run — belongs to the Procedures list; this screen
// exists to answer "what does the data look like, and what needs review",
// nothing more.
//
// The candidate lists cost nothing (both are filters over the already-loaded
// listing). Only what actually renders — a chart's frame, an exception's
// flagged rows — is fetched, one bounded recompute per card, the same
// GET /analyses/{id} a procedure's own detail pane uses.
const props = defineProps<{ workspace: WorkspaceSummary; analyses: SavedAnalysis[] }>()
const emit = defineEmits<{ open: [analysisId: string] }>()

const chartCandidates = computed(() =>
  props.analyses
    .filter(item => item.viz.type !== 'table')
    .sort((a, b) => (a.table ?? '').localeCompare(b.table ?? '') || a.title.localeCompare(b.title)),
)
const exceptions = computed(() =>
  props.analyses.filter(item => item.classification === 'exception'),
)

const details = ref<Record<string, AnalysisDetail>>({})
const failed = ref<Set<string>>(new Set())

async function loadDetails(ids: string[]) {
  const missing = ids.filter(id => !(id in details.value) && !failed.value.has(id))
  if (!missing.length) return
  const fetched = await Promise.all(
    missing.map(id =>
      api.get<AnalysisDetail>(`/api/workspaces/${props.workspace.id}/analyses/${id}`)
        .then(detail => ({ id, detail }))
        .catch(() => ({ id, detail: null })),
    ),
  )
  const nextDetails = { ...details.value }
  const nextFailed = new Set(failed.value)
  for (const entry of fetched) {
    if (entry.detail) nextDetails[entry.id] = entry.detail
    else nextFailed.add(entry.id)
  }
  details.value = nextDetails
  failed.value = nextFailed
}

watch(
  () => [...chartCandidates.value.map(item => item.id), ...exceptions.value.map(item => item.id)],
  ids => void loadDetails(ids),
  { immediate: true },
)
</script>

<template>
  <section class="analysis-summary">
    <section v-if="chartCandidates.length" class="summary-section">
      <p class="eyebrow">Statistics &amp; graphs</p>
      <div class="chart-grid">
        <button
          v-for="item in chartCandidates"
          :key="item.id"
          type="button"
          class="chart-card"
          @click="emit('open', item.id)"
        >
          <div class="chart-card-head">
            <strong>{{ item.title }}</strong>
            <Tag
              :value="classificationMeta(item.classification).label"
              :severity="classificationMeta(item.classification).severity"
            />
          </div>
          <span class="muted chart-card-table">{{ item.table }}</span>
          <ChartView
            v-if="details[item.id]?.frame"
            :frame="details[item.id]!.frame!"
            :viz="details[item.id]!.viz"
            height="180px"
          />
          <div v-else-if="failed.has(item.id)" class="chart-card-error">
            <i class="pi pi-exclamation-triangle" /> Could not compute
          </div>
          <div v-else class="chart-card-loading"><i class="pi pi-spin pi-spinner" /></div>
        </button>
      </div>
    </section>

    <section v-if="exceptions.length" class="summary-section">
      <p class="eyebrow">Exceptions</p>
      <div class="exception-list">
        <article v-for="item in exceptions" :key="item.id" class="exception-card">
          <header class="exception-head">
            <div>
              <strong>{{ item.title }}</strong>
              <span class="muted"> · {{ item.table }}</span>
            </div>
            <Button label="Open procedure" icon="pi pi-arrow-right" iconPos="right" text size="small" @click="emit('open', item.id)" />
          </header>
          <p class="exception-text">{{ item.last_result?.error || item.last_result?.verdict_text }}</p>
          <FrameTable
            v-if="details[item.id]?.frame"
            :frame="details[item.id]!.frame!"
            scrollHeight="220px"
          />
          <div v-else-if="failed.has(item.id)" class="chart-card-error">
            <i class="pi pi-exclamation-triangle" /> Could not recompute the flagged rows
          </div>
          <div v-else class="chart-card-loading"><i class="pi pi-spin pi-spinner" /></div>
        </article>
      </div>
    </section>

    <UiEmptyState
      v-if="!chartCandidates.length && !exceptions.length"
      icon="pi pi-chart-bar"
      title="Nothing to summarize yet"
      description="Run the saved procedures — charts and flagged exceptions will appear here as they conclude."
    />
  </section>
</template>

<style scoped>
.analysis-summary { display: flex; flex-direction: column; gap: var(--aw-space-6); }
.summary-section .eyebrow { margin: 0 0 var(--aw-space-3); }
.muted { color: var(--aw-muted); }

.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--aw-space-3);
}
.chart-card {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  text-align: left;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: var(--aw-space-3);
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.chart-card:hover { border-color: var(--aw-teal-line); box-shadow: var(--aw-shadow-sm); }
.chart-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.5rem; }
.chart-card-head strong { font-size: var(--aw-text-sm); line-height: 1.3; }
.chart-card-table { font-size: var(--aw-text-xs); margin-bottom: 0.2rem; }
.chart-card-loading, .chart-card-error {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  min-height: 10rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-sm);
}
.chart-card-error { color: var(--aw-danger); }

.exception-list { display: flex; flex-direction: column; gap: var(--aw-space-3); }
.exception-card {
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-danger);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  padding: var(--aw-space-3);
}
.exception-head { display: flex; align-items: center; justify-content: space-between; gap: var(--aw-space-3); flex-wrap: wrap; }
.exception-text { margin: 0.35rem 0 0.6rem; font-size: var(--aw-text-sm); }
</style>
