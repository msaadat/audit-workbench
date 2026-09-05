<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import SplitButton from 'primevue/splitbutton'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { SavedAnalysis, WorkspaceSummary } from '../types'
import AnalysisLibrary from './analysis/AnalysisLibrary.vue'
import AnalysisPython from './analysis/AnalysisPython.vue'
import AnalysisCode from './analysis/AnalysisCode.vue'
import AnalysisList from './analysis/AnalysisList.vue'
import AnalysisSummary from './analysis/AnalysisSummary.vue'
import { ANALYSIS_CHIPS, analysisStatus, visibleAnalyses } from './analysis/analysisStatus'
import type { AnalysisFilter } from './analysis/analysisStatus'
import { isOutstanding } from './analysis/classification'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import type { MenuItem } from 'primevue/menuitem'

import { plural } from '../format'

// The Analysis tab has two screens over the same saved procedures, and each
// borrows the page of the thing it most resembles.
//
// Summary is a written work product with a provenance story, which is the
// audit planning memorandum: `UiDocumentPage`, an outline, a document on a
// measure, a rail. Procedures is a register of things that ran and concluded,
// which is Data tests: `UiReviewBar`, a list with a dot and a meta line, a
// verdict bar.
//
// Both screens read the same loaded list, so a procedure can never disagree
// with itself between the two — only what each screen chooses to show differs.
//
// Creating: Library (a predefined audit test) or Code (hand-written Polars),
// one split control rather than two buttons. The assistant writes analyses
// into this same list.
const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const route = useRoute()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)

const analyses = ref<SavedAnalysis[]>([])
const view = ref<'summary' | 'procedures'>(route.query.view === 'procedures' ? 'procedures' : 'summary')
// Narrowings compose across axes: "an exception nobody has answered for" is
// two questions about the same procedure and neither answers the other.
const filters = ref<AnalysisFilter[]>(
  route.query.filter ? [String(route.query.filter) as AnalysisFilter] : [],
)
const search = ref('')
/**
 * Whether a summary exists, reported up by the tab that loads it. The header
 * owns the primary and the summary owns the memo, so the one has to be told
 * whether the other has anything to regenerate.
 */
const hasSummary = ref(false)
const selectedId = ref<string | null>(null)
const creating = ref<'library' | 'code' | null>(null)
const loading = ref(false)
const executing = ref(false)

const selected = computed(() => analyses.value.find(item => item.id === selectedId.value) ?? null)
const outstanding = computed(() => analyses.value.filter(isOutstanding))
const assistantUnavailable = computed(() => agent.isActive.value || assistantChat.state.busy)

/** The lanes, the chips and the whole filter vocabulary, from one tally. */
const status = computed(() => analysisStatus(analyses.value))

const shown = computed(() => {
  const term = search.value.trim().toLowerCase()
  const narrowed = visibleAnalyses(analyses.value, filters.value)
  if (!term) return narrowed
  return narrowed.filter(
    item => `${item.title} ${item.table ?? ''} ${item.source}`.toLowerCase().includes(term),
  )
})

async function load() {
  loading.value = true
  try {
    // One request. Every count the page shows is derived from the records
    // themselves, as the fieldwork pages derive theirs, so the engagement-level
    // summary endpoint was a second answer to a question already answered.
    const { analyses: loaded } = await api.get<{ analyses: SavedAnalysis[] }>(
      `/api/workspaces/${props.workspace.id}/analyses`,
    )
    analyses.value = loaded
    if (!creating.value && (!selectedId.value || !analyses.value.some(item => item.id === selectedId.value))) {
      selectedId.value = shown.value[0]?.id ?? analyses.value[0]?.id ?? null
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
  if (wantedFilter && !filters.value.includes(wantedFilter as AnalysisFilter)) {
    filters.value = [wantedFilter as AnalysisFilter]
  }
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
    // The URL carries one narrowing, as it always did; the rest of a composed
    // set is a working state rather than a place worth sending someone.
    filter: view.value === 'procedures' ? filters.value[0] : undefined,
    analysis: view.value === 'procedures' ? selectedId.value || undefined : undefined,
  })
}

function setView(next: 'summary' | 'procedures') {
  view.value = next
  void locate()
}

/** A Summary card was opened: switch to Procedures with that one selected. */
function openAnalysis(analysisId: string) {
  view.value = 'procedures'
  selectedId.value = analysisId
  creating.value = null
  void locate()
}

function pickFilters(keys: AnalysisFilter[]) {
  filters.value = keys
  // Keep a selection that is still visible; otherwise open the first match, so
  // a narrowing never leaves the detail pane showing something it excluded.
  if (!shown.value.some(item => item.id === selectedId.value)) {
    selectedId.value = shown.value[0]?.id ?? null
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

/**
 * What `New procedure` offers. One split control: the two buttons it replaces
 * both opened the same editor with a different starting point.
 */
const createOptions = computed(() => [
  { label: 'Library test', icon: 'pi pi-book', command: () => startLibrary() },
  { label: 'Custom code', icon: 'pi pi-code', command: () => startCode() },
])

/**
 * What the page can do that is not its next act. `Run all` re-executes work
 * that is already current, which is the rarer half of the pair the header used
 * to spend two equal buttons on.
 */
const menuItems = computed<MenuItem[]>(() => {
  const unanswered = status.value.lanes
    .find(lane => lane.key === 'disposition')?.actions[0]?.ids?.length ?? 0
  return [
  {
    label: `Run all (${analyses.value.length})`,
    icon: 'pi pi-forward',
    disabled: executing.value || !analyses.value.length,
    command: () => void runAll(),
  },
  {
    label: 'Analyse with assistant',
    icon: 'pi pi-sparkles',
    disabled: assistantUnavailable.value,
    command: () => void analyzeWithAssistant(),
  },
  // What closes the `Answered` lane. Promotion is a fitting turn the assistant
  // makes over every unanswered procedure at once, so it is asked for once
  // here rather than offered as a per-row button the page cannot commit.
  {
    label: `Carry ${plural(unanswered, 'exception')} into tests`,
    icon: 'pi pi-shield',
    visible: unanswered > 0,
    disabled: assistantUnavailable.value,
    command: () => void carryIntoTests(),
  },
  ]
})

/**
 * Ask for the procedures holding exceptions to be carried into data tests.
 *
 * The same assistant path as every other workflow request, so the run is
 * visible and budgeted like the rest.
 */
async function carryIntoTests() {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Carry the saved procedures that found exceptions into data tests against '
      + 'the RCM rows they are evidence about, and record a reason for any you decline.',
      'act', agent.launchMode.value,
      { command: 'plan', source: 'tab_button' },
    )
    agent.openPanel()
  } catch (error) {
    fail('Could not start the promotion', error)
  }
}

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
    agent.openPanel()
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
    agent.openPanel()
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
    <!-- The title and the controls, nothing else. The review bar below states
         every count this page has, so a sentence beside the title would
         restate numbers the reader is about to be shown. -->
    <header class="page-head">
      <h1>Analysis</h1>
      <span class="grow" />
      <template v-if="analyses.length">
        <SplitButton
          label="New procedure"
          icon="pi pi-plus"
          size="small"
          outlined
          severity="secondary"
          :model="createOptions"
          @click="startLibrary"
        />
        <!-- The page's next act. Running what has no current result outranks
             regenerating a summary of results that are about to change. -->
        <Button
          v-if="outstanding.length"
          :label="`Run ${outstanding.length} outstanding`"
          icon="pi pi-play"
          size="small"
          :loading="executing"
          v-tooltip.bottom="'Execute every procedure with no current result — stale or never run'"
          @click="runOutstanding"
        />
        <Button
          v-else-if="view === 'summary'"
          :label="hasSummary ? 'Regenerate' : 'Write the summary'"
          icon="pi pi-sparkles"
          size="small"
          :disabled="assistantUnavailable"
          @click="writeSummary"
        />
        <Button
          v-else
          label="Analyse with assistant"
          icon="pi pi-sparkles"
          size="small"
          :disabled="assistantUnavailable"
          @click="analyzeWithAssistant"
        />
        <UiOverflowMenu :items="menuItems" tooltip="More analysis actions" />
      </template>
    </header>

    <!-- Two faces of one work product, not two places: an underline, not a
         form control. The `SelectButton` and the sentence beside it go. -->
    <nav v-if="analyses.length" class="ui-tabs" aria-label="Analysis views">
      <button
        type="button"
        class="ui-tab"
        :aria-current="view === 'summary' ? 'page' : undefined"
        @click="setView('summary')"
      >Summary</button>
      <button
        type="button"
        class="ui-tab"
        :aria-current="view === 'procedures' ? 'page' : undefined"
        @click="setView('procedures')"
      >
        Procedures
        <span class="ui-tab__badge">{{ analyses.length }}</span>
      </button>
    </nav>

    <AnalysisSummary
      v-if="view === 'summary' && analyses.length"
      :workspace="workspace"
      :analyses="analyses"
      @open="openAnalysis"
      @regenerate="writeSummary"
      @loaded="hasSummary = $event"
    />

    <template v-else-if="analyses.length || creating">
      <UiReviewBar
        v-if="analyses.length"
        :lanes="status.lanes"
        :chips="ANALYSIS_CHIPS"
        :filters="status.filters"
        allLabel="All procedures"
        :total="analyses.length"
        :filter="filters"
        @filter="pickFilters($event as AnalysisFilter[])"
      />

      <div class="layout">
        <section class="list-panel">
          <div class="list-head">
            <IconField>
              <InputIcon class="pi pi-search" />
              <InputText v-model="search" size="small" placeholder="Search procedures and tables" fluid />
            </IconField>
          </div>
          <div class="list-body">
            <AnalysisList :items="shown" :selectedId="selectedId" @select="select" />
          </div>
        </section>

        <section class="detail">
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
      </div>
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
  display: flex; flex-direction: column; gap: .75rem;
  min-width: 0; max-width: 100%; min-height: 0; height: 100%;
}

/* One 36px row: the title, what there is of it, and at most one primary. */
.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.grow { flex: 1; }

.layout { display: grid; grid-template-columns: 18.75rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { display: flex; flex-direction: column; gap: .5rem; padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext) { width: 100%; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

/* One panel for the whole detail column, as the fieldwork pages draw it. */
.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-width: 100%; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  container: master-detail-content / inline-size;
  overflow-y: auto;
}
</style>
