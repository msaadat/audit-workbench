<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { SavedAnalysis, WorkspaceSummary } from '../types'
import AnalysisLibrary from './analysis/AnalysisLibrary.vue'
import AnalysisPython from './analysis/AnalysisPython.vue'
import AnalysisCode from './analysis/AnalysisCode.vue'
import AnalysisSummary from './analysis/AnalysisSummary.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiVerdictStatus from './ui/UiVerdictStatus.vue'

// The Analysis tab: a rail of saved analyses on the left, an editor on the
// right. Two creation paths — the predefined Library or Code (hand-written
// Polars). AI-assisted analysis lives in the Agent drawer, which saves into
// this same rail. Saving persists to the rail; Pinning (inside each pane)
// promotes a copy to the dashboard.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const route = useRoute()
const router = useRouter()

const analyses = ref<SavedAnalysis[]>([])
const selectedId = ref<string | null>(null)
const creating = ref<'library' | 'code' | null>(null)
const loading = ref(false)
const view = ref<'summary' | 'procedures'>(route.query.view === 'procedures' ? 'procedures' : 'summary')
const summaryRef = ref<InstanceType<typeof AnalysisSummary> | null>(null)
const views = [
  { label: 'Summary', value: 'summary' },
  { label: 'Procedures', value: 'procedures' },
]

const sourceIcon: Record<string, string> = {
  library: 'pi pi-book', ai: 'pi pi-sparkles', code: 'pi pi-code',
}
const sourceLabel: Record<string, string> = {
  library: 'library', ai: 'AI code', code: 'custom code',
}

const selected = computed(() => analyses.value.find((a) => a.id === selectedId.value) ?? null)

async function load() {
  loading.value = true
  try {
    analyses.value = (
      await api.get<{ analyses: SavedAnalysis[] }>(`/api/workspaces/${props.workspace.id}/analyses`)
    ).analyses
    if (!creating.value && (!selectedId.value || !analyses.value.some(item => item.id === selectedId.value))) {
      selectedId.value = analyses.value[0]?.id ?? null
    }
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not load analyses', detail, life: 6000 })
  } finally {
    loading.value = false
  }
}
watch(() => props.workspace.id, () => {
  if (view.value === 'procedures') void load()
}, { immediate: true })

watch(() => [route.query.view, route.query.analysis], () => {
  const analysisId = String(route.query.analysis || '')
  const target: 'summary' | 'procedures' = analysisId || String(route.query.view || 'summary') === 'procedures'
    ? 'procedures'
    : 'summary'
  if (target !== view.value) view.value = target
  if (analysisId) {
    selectedId.value = analysisId
    creating.value = null
    if (!analyses.value.some(item => item.id === analysisId)) void load()
  }
}, { immediate: true })

// Live-refresh the rail after any durable agent commit. Revision-based
// invalidation also covers commits that do not expose a typed artifact event.
const unsubscribe = useAgentRun(props.workspace.id).onWorkspaceInvalidated(() => {
  void onChanged()
})
onUnmounted(unsubscribe)

function startLibrary() {
  void setView('procedures')
  creating.value = 'library'
  selectedId.value = null
}
function startCode() {
  void setView('procedures')
  creating.value = 'code'
  selectedId.value = null
}
function select(a: SavedAnalysis) {
  selectedId.value = a.id
  creating.value = null
  void router.replace({ query: workspaceQuery('analysis', { view: 'procedures', analysis: a.id }) })
}

async function setView(raw: string) {
  const next: 'summary' | 'procedures' = raw === 'procedures' ? 'procedures' : 'summary'
  view.value = next
  if (next === 'procedures') await load()
  await router.replace({
    query: workspaceQuery('analysis', next === 'procedures'
      ? { view: next, analysis: selectedId.value || undefined }
      : { view: next }),
  })
}

async function openAnalysis(analysisId: string) {
  selectedId.value = analysisId
  creating.value = null
  await setView('procedures')
}

async function onSaved(created: SavedAnalysis) {
  await load()
  select(created)
}
async function onChanged() {
  const keep = selectedId.value
  if (view.value === 'procedures') await load()
  await summaryRef.value?.load()
  selectedId.value = keep
}
async function onDeleted() {
  selectedId.value = null
  creating.value = null
  if (view.value === 'procedures') await load()
  await summaryRef.value?.load()
}

</script>

<template>
  <section class="analysis-shell">
    <div class="analysis-nav">
      <SelectButton :modelValue="view" :options="views" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" @update:modelValue="setView" />
      <span v-if="view === 'procedures'" class="muted">Create, edit, and run saved procedures.</span>
    </div>
    <AnalysisSummary v-if="view === 'summary'" ref="summaryRef" :workspace="workspace" @open="openAnalysis" />
  <UiMasterDetail v-if="view === 'procedures'" railWidth="17rem" class="analysis">
    <template #rail><div class="rail">
      <div class="rail-heading"><p class="eyebrow">Analysis library</p><strong>Saved procedures</strong></div>
      <div class="rail-actions">
        <Button label="Library" icon="pi pi-book" size="small" :outlined="creating !== 'library'" @click="startLibrary" />
        <Button label="Code" icon="pi pi-code" size="small" :outlined="creating !== 'code'" @click="startCode" />
      </div>

      <p class="rail-title">Engagement analyses</p>

      <button
        v-for="a in analyses"
        :key="a.id"
        class="rail-item"
        :class="{ active: selectedId === a.id }"
        @click="select(a)"
      >
        <div class="rail-item-head">
          <span class="rail-item-title">{{ a.title }}</span>
          <UiVerdictStatus v-if="a.error || a.last_result?.status === 'error'" verdict="error" />
          <UiVerdictStatus v-else-if="a.last_result?.verdict ?? a.verdict" :verdict="a.last_result?.verdict ?? a.verdict!" />
        </div>
        <div class="rail-item-meta">
          <i :class="sourceIcon[a.source] ?? 'pi pi-book'" />
          {{ sourceLabel[a.source] ?? a.source }}
          <span v-if="a.table"> · {{ a.table }}</span>
          <span v-if="a.last_result"> · executed</span>
        </div>
      </button>
    </div></template>

    <section class="detail surface-panel">
      <AnalysisLibrary
        v-if="creating === 'library' || selected?.kind === 'analytics'"
        :key="selected?.id ?? 'new-library'"
        :workspace="workspace"
        :analysis="creating === 'library' ? null : selected"
        @saved="onSaved"
        @changed="onChanged"
        @deleted="onDeleted"
      />
      <AnalysisCode
        v-if="creating === 'code'"
        :key="workspace.id"
        :workspace="workspace"
        @saved="onSaved"
      />
      <AnalysisPython
        v-if="!creating && selected?.kind === 'python'"
        :key="selected.id"
        :workspace="workspace"
        :analysis="selected"
        @changed="onChanged"
        @deleted="onDeleted"
      />
      <div v-if="!creating && !selected" class="empty-state analysis-empty">
        <div><span class="empty-state-icon"><i class="pi pi-shield" /></span>
        <h3>Choose an analysis path</h3></div>
      </div>
    </section>
  </UiMasterDetail>
  </section>
</template>

<style scoped>
.analysis {
  /* Fill the viewport below the app banner / workspace header / tab strip so
     the rail and the detail pane scroll independently instead of the whole
     page scrolling as one. */
  min-height: 32rem;
}
.analysis-shell { min-width:0; }.analysis-nav { display:flex; align-items:center; gap:.75rem; margin:0 0 1rem; }.muted { color:var(--aw-muted); font-size:.82rem; }

.rail {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  overflow: visible;
  padding: 0.75rem;
  box-shadow: none;
}
.rail > * { flex-shrink: 0; }

.rail-actions {
  display: flex;
  gap: 0.5rem;
}
.rail-heading { padding: .15rem .1rem .5rem; border-bottom: 1px solid var(--aw-border); }
.rail-heading .eyebrow { margin-bottom: .1rem; }
/* Three creation buttons share the 15rem rail — trim padding so the labels
   stay on one line. */
.rail-actions :deep(.p-button) { flex: 1; padding-inline: 0.35rem; white-space: nowrap; }

.rail-title {
  margin: 0.5rem 0 0.15rem;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--aw-muted);
}

.small { font-size: 0.85rem; }

.rail-item {
  text-align: left;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-sm);
  box-shadow: var(--aw-shadow-sm);
  padding: 0.65rem 0.8rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.rail-item:hover { border-color: var(--aw-teal); box-shadow: var(--aw-shadow-md); }
.rail-item.active { border-color: var(--aw-teal); box-shadow: inset 3px 0 0 var(--aw-teal); background: var(--aw-teal-soft); }

.rail-item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.4rem;
}
.rail-item-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.3;
  font-size: 0.92rem;
  font-weight: 600;
}
.rail-item-meta {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.3rem;
  font-size: 0.75rem;
  color: var(--aw-muted);
}
.rail-item-meta .del {
  margin-left: auto;
  cursor: pointer;
  padding: 0.2rem;
  border-radius: 4px;
}
.rail-item-meta .del:hover { color: var(--aw-danger); }

.detail {
  min-width: 0;
  overflow: visible;
  /* Room for the focus ring of the sticky header inputs. */
  padding: 1rem;
  box-shadow: none;
}

.empty {
  text-align: center;
  color: var(--p-surface-500);
  padding: 3rem 1rem;
}
.empty i { font-size: 2rem; color: var(--p-primary-400); }
.empty p { margin-top: 0.6rem; }
.analysis-empty { height: 100%; border: 0; background: transparent; }

@media (max-width: 900px) {
  .analysis { min-height: 0; }
  .rail { flex-basis: auto; max-height: 18rem; }
  .detail { min-height: 24rem; }
}
</style>
