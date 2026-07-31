<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../../api'
import type { AnalysisSummaryItem, AnalysisSummaryPayload, WorkspaceSummary } from '../../types'
import UiVerdictStatus from '../ui/UiVerdictStatus.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ open: [analysisId: string] }>()
const toast = useToast()

const data = ref<AnalysisSummaryPayload | null>(null)
const loading = ref(false)
const error = ref('')
const filter = ref<'review' | 'all'>('review')
const pinning = ref<Record<string, boolean>>({})
const filters = [
  { label: 'Needs review', value: 'review' },
  { label: 'All results', value: 'all' },
]

const visibleItems = computed(() => {
  const items = data.value?.items ?? []
  return filter.value === 'all'
    ? items
    : items.filter(item => ['exception', 'unusual', 'execution_error', 'stale'].includes(item.classification))
})

function label(item: AnalysisSummaryItem) {
  if (item.classification === 'exception') return 'Exception'
  if (item.classification === 'unusual') return 'Unusual result'
  if (item.classification === 'execution_error') return 'Execution issue'
  if (item.classification === 'stale') return 'Rerun required'
  if (item.classification === 'not_run') return 'Not run'
  if (item.classification === 'clear') return 'Clear'
  return 'Informational'
}

function severity(item: AnalysisSummaryItem) {
  if (item.classification === 'exception' || item.classification === 'execution_error') return 'danger'
  if (item.classification === 'unusual' || item.classification === 'stale') return 'warn'
  if (item.classification === 'clear') return 'success'
  return 'info'
}

async function pin(item: AnalysisSummaryItem) {
  pinning.value = { ...pinning.value, [item.analysis_id]: true }
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/analyses/${item.analysis_id}/pin`, {})
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', detail: item.title, life: 2500 })
  } catch (caught) {
    toast.add({ severity: 'error', summary: 'Pin failed', detail: caught instanceof ApiError ? caught.message : String(caught), life: 6000 })
  } finally {
    const next = { ...pinning.value }
    delete next[item.analysis_id]
    pinning.value = next
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.get<AnalysisSummaryPayload>(`/api/workspaces/${props.workspace.id}/analyses/summary`)
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught.message : String(caught)
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())
defineExpose({ load })
</script>

<template>
  <section class="analysis-summary">
    <header class="summary-head">
      <div>
        <p class="eyebrow">Analysis summary</p>
        <h2>Results across this engagement</h2>
        <p>Review flagged results, then open the underlying procedure for its current detail rows.</p>
      </div>
      <Button label="Refresh" icon="pi pi-refresh" severity="secondary" outlined size="small" :loading="loading" @click="load" />
    </header>

    <div v-if="data" class="count-grid">
      <button class="count-card review" @click="filter = 'review'"><strong>{{ data.counts.needs_review }}</strong><span>Need review</span></button>
      <button class="count-card error" @click="filter = 'review'"><strong>{{ data.counts.errors }}</strong><span>Execution issues</span></button>
      <button class="count-card stale" @click="filter = 'review'"><strong>{{ data.counts.stale }}</strong><span>Rerun required</span></button>
      <button class="count-card clear" @click="filter = 'all'"><strong>{{ data.counts.clear }}</strong><span>Clear</span></button>
      <button class="count-card" @click="filter = 'all'"><strong>{{ data.counts.informational }}</strong><span>Informational</span></button>
      <button class="count-card" @click="filter = 'all'"><strong>{{ data.counts.not_run }}</strong><span>Not run</span></button>
    </div>

    <div class="summary-toolbar">
      <SelectButton v-model="filter" :options="filters" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" />
      <span v-if="data" class="muted">{{ visibleItems.length }} procedure{{ visibleItems.length === 1 ? '' : 's' }}</span>
    </div>

    <p v-if="error" class="error"><i class="pi pi-exclamation-triangle" /> {{ error }}</p>
    <div v-else-if="loading && !data" class="empty">Loading analysis outcomes…</div>
    <div v-else-if="!visibleItems.length" class="empty">
      <i class="pi pi-check-circle" />
      <strong>{{ filter === 'review' ? 'No analysis results need review' : 'No analysis outcomes yet' }}</strong>
      <span>{{ filter === 'review' ? 'Clear and informational results remain available under All results.' : 'Run analysis procedures to populate this summary.' }}</span>
    </div>
    <div v-else class="summary-list">
      <article v-for="item in visibleItems" :key="item.analysis_id" class="summary-item" :data-classification="item.classification">
        <div class="item-main">
          <UiVerdictStatus v-if="item.classification !== 'stale' && item.classification !== 'not_run'" :verdict="item.status === 'error' ? 'error' : (item.verdict ?? 'info')" />
          <i v-else :class="item.classification === 'stale' ? 'pi pi-refresh' : 'pi pi-clock'" class="state-icon" />
          <div>
            <div class="item-title"><strong>{{ item.title }}</strong><Tag :value="label(item)" :severity="severity(item)" /></div>
            <p>{{ item.error || item.verdict_text || (item.classification === 'not_run' ? 'This procedure has not been executed.' : 'No conclusion was recorded.') }}</p>
            <small>{{ item.table || 'Multiple tables' }}<template v-if="item.executed_at"> · Executed {{ new Date(item.executed_at).toLocaleString() }}</template></small>
          </div>
        </div>
        <div class="item-actions">
          <div v-if="item.stats.length" class="stat-chips"><span v-for="stat in item.stats.slice(0, 4)" :key="stat.label"><small>{{ stat.label }}</small><strong>{{ stat.value }}</strong></span></div>
          <span v-else-if="item.status === 'ok'" class="row-count">{{ item.row_count.toLocaleString() }} result row{{ item.row_count === 1 ? '' : 's' }}</span>
          <div class="action-buttons">
            <Button label="Pin" icon="pi pi-thumbtack" size="small" outlined :loading="pinning[item.analysis_id]" @click="pin(item)" />
            <Button label="Open analysis" icon="pi pi-arrow-right" iconPos="right" size="small" text @click="emit('open', item.analysis_id)" />
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.analysis-summary { max-width: 1100px; margin: 0 auto; padding: .25rem 0 2rem; }
.summary-head { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; margin-bottom:1rem; }.summary-head h2 { margin:.15rem 0 .25rem; font-size:1.25rem; }.summary-head p { margin:0; color:var(--aw-muted); font-size:.88rem; }.summary-head .eyebrow { color:var(--aw-teal); font-weight:700; text-transform:uppercase; font-size:.7rem; letter-spacing:.06em; }
.count-grid { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:.55rem; margin-bottom:1rem; }.count-card { text-align:left; border:1px solid var(--aw-border); background:var(--aw-panel); border-radius:var(--aw-radius-sm); padding:.65rem .7rem; cursor:pointer; color:inherit; display:flex; flex-direction:column; gap:.1rem; }.count-card strong { font-size:1.25rem; }.count-card span { color:var(--aw-muted); font-size:.72rem; }.count-card.review { border-top:3px solid var(--aw-warn); }.count-card.error { border-top:3px solid var(--aw-danger); }.count-card.stale { border-top:3px solid var(--p-orange-400); }.count-card.clear { border-top:3px solid var(--aw-ok); }
.summary-toolbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:.75rem; }.muted, small { color:var(--aw-muted); }.error { color:var(--aw-danger); }.empty { min-height:15rem; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:.45rem; color:var(--aw-muted); text-align:center; }.empty > i { font-size:2rem; color:var(--aw-ok); }.empty strong { color:var(--aw-ink); }
.summary-list { display:flex; flex-direction:column; gap:.6rem; }.summary-item { display:flex; justify-content:space-between; gap:1rem; border:1px solid var(--aw-border); border-left:3px solid var(--aw-muted); border-radius:var(--aw-radius-sm); background:var(--aw-panel); padding:.75rem .85rem; }.summary-item[data-classification='exception'],.summary-item[data-classification='execution_error'] { border-left-color:var(--aw-danger); }.summary-item[data-classification='unusual'],.summary-item[data-classification='stale'] { border-left-color:var(--aw-warn); }.summary-item[data-classification='clear'] { border-left-color:var(--aw-ok); }.item-main { display:flex; gap:.65rem; min-width:0; }.item-main :deep(.verdict-icon),.state-icon { flex:0 0 auto; margin-top:.12rem; }.state-icon { color:var(--aw-warn); font-size:1rem; }.item-title { display:flex; align-items:center; gap:.45rem; flex-wrap:wrap; }.item-main p { margin:.25rem 0; font-size:.85rem; }.item-actions { display:flex; flex-direction:column; align-items:flex-end; justify-content:space-between; gap:.5rem; min-width:12rem; }.stat-chips { display:flex; gap:.35rem; flex-wrap:wrap; justify-content:flex-end; }.stat-chips span { display:flex; flex-direction:column; border-radius:4px; background:var(--aw-canvas); padding:.25rem .4rem; font-size:.74rem; }.row-count { font-size:.78rem; }
@media (max-width:900px) { .count-grid { grid-template-columns:repeat(3, 1fr); }.summary-item { flex-direction:column; }.item-actions { align-items:flex-start; min-width:0; }.stat-chips { justify-content:flex-start; } }
@media (max-width:600px) { .summary-head { flex-direction:column; }.count-grid { grid-template-columns:repeat(2, 1fr); } }
</style>
