<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AnalysisSummaryPayload, SavedAnalysis, WorkspaceSummary } from '../types'
import AnalysisLibrary from './analysis/AnalysisLibrary.vue'
import AnalysisPython from './analysis/AnalysisPython.vue'
import AnalysisCode from './analysis/AnalysisCode.vue'
import AnalysisList from './analysis/AnalysisList.vue'
import AnalysisSummary from './analysis/AnalysisSummary.vue'
import { BUCKET_CLASSIFICATIONS, OUTSTANDING } from './analysis/classification'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiTriageCounts from './ui/UiTriageCounts.vue'
import type { TriageCount } from './ui/UiTriageCounts.vue'

// The Analysis tab has two screens over the same saved procedures.
//
// Summary is the EDA entry point: only what is worth looking at without being
// asked — the charts the procedures produced, and the ones that concluded an
// exception. Procedures is the full triage rail: every saved procedure,
// filterable by what it concluded, with an editor for whichever one is open.
//
// Both screens read the same loaded list, so a procedure can never disagree
// with itself between the two — only what each screen chooses to show differs.
//
// Creating: Library (a predefined audit test) or Code (hand-written Polars).
// The assistant writes analyses into this same rail.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const route = useRoute()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)

const analyses = ref<SavedAnalysis[]>([])
const summary = ref<AnalysisSummaryPayload | null>(null)
const view = ref<'summary' | 'procedures'>(route.query.view === 'procedures' ? 'procedures' : 'summary')
const viewOptions = [
  { label: 'Summary', value: 'summary' },
  { label: 'Procedures', value: 'procedures' },
]
const filter = ref(String(route.query.filter || 'all'))
const search = ref('')
const selectedId = ref<string | null>(null)
const creating = ref<'library' | 'code' | null>(null)
const loading = ref(false)
const executing = ref(false)

const selected = computed(() => analyses.value.find(item => item.id === selectedId.value) ?? null)
const outstandingCount = computed(
  () => analyses.value.filter(item => OUTSTANDING.includes(item.classification)).length,
)
const assistantUnavailable = computed(() => agent.isActive.value || assistantChat.state.busy)

const triage = computed<TriageCount[]>(() => {
  const counts = summary.value?.counts
  return [
    { key: 'all', label: 'All procedures', value: analyses.value.length },
    { key: 'exception', label: 'Exceptions', value: counts?.exception ?? 0, tone: 'danger' },
    { key: 'unusual', label: 'Need review', value: counts?.unusual ?? 0, tone: 'warn' },
    { key: 'errors', label: 'Blocked', value: counts?.errors ?? 0, tone: 'danger' },
    { key: 'stale', label: 'Rerun required', value: counts?.stale ?? 0, tone: 'warn' },
    { key: 'clear', label: 'No exception', value: counts?.clear ?? 0, tone: 'ok' },
    { key: 'informational', label: 'Informational', value: counts?.informational ?? 0, tone: 'info' },
    { key: 'not_run', label: 'Not run', value: counts?.not_run ?? 0 },
  ]
})
const activeFilterLabel = computed(
  () => triage.value.find(count => count.key === filter.value)?.label ?? 'All procedures',
)

const visibleAnalyses = computed(() => {
  const wanted = BUCKET_CLASSIFICATIONS[filter.value]
  const term = search.value.trim().toLowerCase()
  return analyses.value.filter(item => {
    if (wanted && !wanted.includes(item.classification)) return false
    if (!term) return true
    return `${item.title} ${item.table ?? ''} ${item.source}`.toLowerCase().includes(term)
  })
})

async function load() {
  loading.value = true
  try {
    const [analysesResp, summaryResp] = await Promise.all([
      api.get<{ analyses: SavedAnalysis[] }>(`/api/workspaces/${props.workspace.id}/analyses`),
      api.get<AnalysisSummaryPayload>(`/api/workspaces/${props.workspace.id}/analyses/summary`),
    ])
    analyses.value = analysesResp.analyses
    summary.value = summaryResp
    if (!creating.value && (!selectedId.value || !analyses.value.some(item => item.id === selectedId.value))) {
      selectedId.value = visibleAnalyses.value[0]?.id ?? analyses.value[0]?.id ?? null
    }
  } catch (error) {
    fail('Could not load analyses', error)
  } finally {
    loading.value = false
  }
}
watch(() => props.workspace.id, () => void load(), { immediate: true })

// A deep link may name a screen, a filter, a procedure, or any combination.
watch(() => [route.query.view, route.query.filter, route.query.analysis], () => {
  const wantedView = String(route.query.view || '') === 'procedures' ? 'procedures' : null
  if (wantedView && wantedView !== view.value) view.value = wantedView
  const wantedFilter = String(route.query.filter || '')
  if (wantedFilter && wantedFilter !== filter.value) filter.value = wantedFilter
  const analysisId = String(route.query.analysis || '')
  if (analysisId && analysisId !== selectedId.value) {
    selectedId.value = analysisId
    creating.value = null
    view.value = 'procedures'
    if (!analyses.value.some(item => item.id === analysisId)) void load()
  }
}, { immediate: true })

// Live-refresh after any durable agent commit. Revision-based invalidation also
// covers commits that do not expose a typed artifact event.
const unsubscribe = agent.onWorkspaceInvalidated(() => void reload())
onUnmounted(unsubscribe)

function locate() {
  return nav.replace('analysis', {
    view: view.value === 'summary' ? undefined : view.value,
    filter: view.value === 'procedures' && filter.value !== 'all' ? filter.value : undefined,
    analysis: view.value === 'procedures' ? selectedId.value || undefined : undefined,
  })
}

function setView(next: string) {
  view.value = next === 'procedures' ? 'procedures' : 'summary'
  void locate()
}

/** A Summary card was opened: switch to Procedures with that one selected. */
function openAnalysis(analysisId: string) {
  view.value = 'procedures'
  selectedId.value = analysisId
  creating.value = null
  void locate()
}

function pickFilter(key: string) {
  filter.value = key
  // Keep a selection that is still visible; otherwise open the first match, so
  // the filter never leaves the detail pane showing something it excluded.
  if (!visibleAnalyses.value.some(item => item.id === selectedId.value)) {
    selectedId.value = visibleAnalyses.value[0]?.id ?? null
    creating.value = null
  }
  void locate()
}

function select(analysis: SavedAnalysis) {
  selectedId.value = analysis.id
  creating.value = null
  void locate()
}

function startLibrary() {
  view.value = 'procedures'
  creating.value = 'library'
  selectedId.value = null
  void locate()
}
function startCode() {
  view.value = 'procedures'
  creating.value = 'code'
  selectedId.value = null
  void locate()
}

async function reload() {
  const keep = selectedId.value
  await load()
  if (keep && analyses.value.some(item => item.id === keep)) selectedId.value = keep
}

async function onSaved(created: SavedAnalysis) {
  creating.value = null
  await load()
  select(created)
}
async function onDeleted() {
  selectedId.value = null
  creating.value = null
  await load()
}

/**
 * Execute a set of saved procedures and record what each one finds.
 * `ids` omitted means "whatever isn't current" (stale or never run); an
 * explicit list forces every named procedure to re-execute regardless of
 * its current state — that distinction is Run outstanding vs. Run all.
 */
async function runAnalyses(ids?: string[]) {
  executing.value = true
  try {
    const body = await api.post<{ executed: { ok: boolean }[] }>(
      `/api/workspaces/${props.workspace.id}/analyses/execute`,
      ids ? { ids } : {},
    )
    await reload()
    const count = body.executed.length
    toast.add({
      severity: 'success',
      summary: count ? `Executed ${count} procedure${count === 1 ? '' : 's'}` : 'Everything is current',
      detail: count ? 'Results are recorded against each procedure.' : 'No procedure needed a rerun.',
      life: 3000,
    })
  } catch (error) {
    fail('Could not run the procedures', error)
  } finally {
    executing.value = false
  }
}
function runOutstanding() { return runAnalyses() }
function runAll() { return runAnalyses(analyses.value.map(item => item.id)) }

/** Hand the assistant the frames on screen, so it never has to guess the scope. */
async function analyzeWithAssistant() {
  const tables = props.workspace.tables.map(table => table.name)
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Analyse the data in this workspace and save the procedures that hold up.',
      'act', agent.launchMode.value,
      { command: 'analyze_data', source: 'tab_button', runContext: { tables } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'info', summary: 'Analysis started',
      detail: 'Progress is visible in the assistant.', life: 3000,
    })
  } catch (error) {
    fail('Could not start the analysis', error)
  }
}

/**
 * Write (or rewrite) the EDA summary.
 *
 * The memo is derived, so there is nothing to overwrite and no confirmation to
 * ask for. It goes through the same assistant path as any other workflow
 * request, so the run is visible and budgeted like the rest.
 */
async function writeSummary() {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Summarise the data analysis performed in this workspace.',
      'act', agent.launchMode.value,
      { command: 'analyze_data', source: 'tab_button' },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'info', summary: 'Writing the summary',
      detail: 'Progress is visible in the assistant.', life: 3000,
    })
  } catch (error) {
    fail('Could not start the summary', error)
  }
}

function fail(summaryText: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary: summaryText, detail, life: 6000 })
}
</script>

<template>
  <div class="analysis">
    <UiPageHeader title="Analysis">
      <Button
        label="Analyse with assistant"
        icon="pi pi-sparkles"
        size="small"
        :disabled="assistantUnavailable"
        @click="analyzeWithAssistant"
      />
      <Button
        v-if="outstandingCount"
        :label="`Run outstanding (${outstandingCount})`"
        icon="pi pi-play"
        size="small"
        outlined
        :loading="executing"
        v-tooltip.bottom="'Execute only what has no current result — stale or never run'"
        @click="runOutstanding"
      />
      <Button
        v-if="analyses.length"
        :label="`Run all (${analyses.length})`"
        icon="pi pi-forward"
        size="small"
        outlined
        :loading="executing"
        v-tooltip.bottom="'Re-execute every saved procedure, including ones already current'"
        @click="runAll"
      />
      <Button label="Library" icon="pi pi-book" size="small" outlined @click="startLibrary" />
      <Button label="Code" icon="pi pi-code" size="small" outlined @click="startCode" />
    </UiPageHeader>

    <div v-if="analyses.length" class="analysis-nav">
      <SelectButton :modelValue="view" :options="viewOptions" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" @update:modelValue="setView" />
      <span v-if="view === 'summary'" class="muted">What the analysis found — the population, the exceptions, and the work outstanding.</span>
    </div>

    <AnalysisSummary
      v-if="view === 'summary' && analyses.length"
      :workspace="workspace"
      :analyses="analyses"
      @open="openAnalysis"
      @regenerate="writeSummary"
    />

    <template v-else-if="analyses.length || creating">
      <UiTriageCounts v-if="analyses.length" :counts="triage" :active="filter" @select="pickFilter" />

      <div v-if="analyses.length" class="toolbar">
        <IconField>
          <InputIcon class="pi pi-search" />
          <InputText v-model="search" size="small" placeholder="Search procedures and tables" />
        </IconField>
        <span class="muted">
          {{ visibleAnalyses.length }} of {{ analyses.length }} · {{ activeFilterLabel }}
        </span>
      </div>

      <UiMasterDetail railWidth="17rem" :empty="!analyses.length" class="layout">
        <template #rail>
          <AnalysisList :items="visibleAnalyses" :selectedId="selectedId" @select="select" />
          <p v-if="!visibleAnalyses.length" class="rail-empty">No procedure matches this view.</p>
        </template>

        <section class="detail surface-panel">
          <AnalysisLibrary
            v-if="creating === 'library' || (!creating && selected?.kind === 'analytics')"
            :key="selected?.id ?? 'new-library'"
            :workspace="workspace"
            :analysis="creating === 'library' ? null : selected"
            @saved="onSaved"
            @changed="reload"
            @deleted="onDeleted"
          />
          <AnalysisCode
            v-else-if="creating === 'code'"
            :key="`new-code-${workspace.id}`"
            :workspace="workspace"
            @saved="onSaved"
          />
          <AnalysisPython
            v-else-if="selected?.kind === 'python'"
            :key="selected.id"
            :workspace="workspace"
            :analysis="selected"
            @changed="reload"
            @deleted="onDeleted"
          />
          <UiEmptyState
            v-else
            icon="pi pi-chart-bar"
            title="Open a procedure"
            :description="loading ? 'Loading saved procedures…' : 'Pick a procedure from the list, or create one.'"
          >
            <Button label="Library test" icon="pi pi-book" size="small" @click="startLibrary" />
            <Button label="Custom code" icon="pi pi-code" size="small" outlined @click="startCode" />
          </UiEmptyState>
        </section>
      </UiMasterDetail>
    </template>

    <UiEmptyState
      v-else-if="loading"
      icon="pi pi-hourglass"
      title="Loading analyses"
      description="Reading the saved procedures and the outcomes they recorded."
    />

    <UiEmptyState
      v-else
      icon="pi pi-chart-bar"
      title="Analyse this engagement's data"
      description="A saved procedure is a rerunnable spec: pick a predefined audit test, write Polars yourself, or let the assistant propose procedures for the imported tables."
    >
      <Button label="Analyse with assistant" icon="pi pi-sparkles" :disabled="assistantUnavailable" @click="analyzeWithAssistant" />
      <Button label="Library test" icon="pi pi-book" outlined @click="startLibrary" />
      <Button label="Custom code" icon="pi pi-code" outlined @click="startCode" />
    </UiEmptyState>
  </div>
</template>

<style scoped>
.analysis {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
}
/* This column has no `gap` — its children space themselves — so the header
   keeps the margin the shared rule no longer carries. */
.analysis > .ui-page-header { margin-bottom: var(--aw-section-gap); }

.analysis-nav {
  display: flex;
  align-items: center;
  gap: var(--aw-space-3);
  margin: 0 0 var(--aw-space-4);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aw-space-3);
  flex-wrap: wrap;
  margin: var(--aw-space-3) 0;
}
.muted { color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* Bound the procedure rail to the space left below this page's controls.
   UiMasterDetail owns the rail's overflow, so the list then scrolls without
   moving the procedure that is open beside it. */
.layout { flex: 1; min-height: 12rem; }

.rail-empty {
  margin: 0;
  padding: var(--aw-space-4) 0;
  color: var(--aw-muted);
  font-size: var(--aw-text-sm);
  text-align: center;
}

.detail {
  min-width: 0;
  /* Room for the focus ring of the sticky header inputs. */
  padding: var(--aw-space-4);
}
</style>
