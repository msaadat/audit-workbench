<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import SplitButton from 'primevue/splitbutton'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { CycleVouchMetadata, DocumentSchemaCatalogEntry, PlanningPayload, RcmCompletion, RcmRow, TestRollup, WorkspaceSummary } from '../types'
import UiReviewBar from './ui/UiReviewBar.vue'
import { RCM_CHIPS, filterRows, rcmStatus } from './planning/rcmStatus'
import type { RcmFilter } from './planning/rcmStatus'
import RcmGrid from './planning/RcmGrid.vue'
import RcmRowDrawer from './planning/RcmRowDrawer.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const router = useRouter()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { isActive, launchMode } = agent

const data = ref<PlanningPayload | null>(null)
const cycleMeta = ref<CycleVouchMetadata | null>(null)
// The vocabulary a cycle requirement may address. Empty until induction has
// run, which is what the RCM stage now waits for.
const documentSchemas = ref<DocumentSchemaCatalogEntry[]>([])
const saving = ref(false)
const selectedRcmId = ref<string | null>(null)
const rcmImportInput = ref<HTMLInputElement>()
const rcmExporting = ref(false)
const rcmImporting = ref(false)
const generatingTests = ref(false)
const generatingFindings = ref(false)
const runningAllDataTests = ref(false)
const runningAllDocumentTests = ref(false)
const markingReviewed = ref(false)
const detailOpen = ref(false)
// The completion gates the status bar reports. Fetched beside the planning
// payload rather than derived here: the backend already owns what counts as an
// unreviewed conclusion or a capped one, and two definitions would drift.
const completion = ref<RcmCompletion | null>(null)
// Which subset of the matrix the bar has asked the grid to show, at most one
// per axis. A view over one array, so the counts above and the rows below can
// never disagree.
const rcmFilter = ref<RcmFilter[]>([])
// Open by default. What the memorandum was drafted from is the first thing a
// reviewer asks about it, and a panel behind a button is a panel nobody opens.
const selectedRcm = computed(() => data.value?.rcm.find(item => item.id === selectedRcmId.value) ?? null)
// Folded rather than combined: each narrowing runs the same predicate over
// what the last one left, so the filters compose without a second code path.
const visibleRcm = computed(() => rcmFilter.value.reduce(
  (rows, key) => filterRows(rows, key, data.value?.finding_rollups, completion.value),
  data.value?.rcm ?? [],
))
const rcmStatusModel = computed(() => rcmStatus(
  data.value?.rcm ?? [], data.value?.finding_rollups, completion.value,
))
const selectedFindings = computed(
  () => data.value?.finding_rollups.by_rcm[selectedRcmId.value ?? ''] ?? [],
)
const rowsWithoutTests = computed(() => (data.value?.rcm ?? []).filter(row => (row.execution_rollup.tests ?? row.test_refs.length) === 0))
const linkedDataTestCount = computed(() => (data.value?.data_tests ?? []).filter(test => test.rcm_id).length)
const linkedDocumentTestIds = computed(() => (data.value?.document_tests ?? [])
  .filter(test => Boolean(test.rcm_id || test.rcm_refs?.length))
  .map(test => test.id))

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
async function reload() {
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
  // The gates only feed the bar's disclosures. A matrix that loads is more use
  // than an error toast, so losing them costs the disclosures and nothing else.
  api.get<RcmCompletion>(`/api/workspaces/${props.workspace.id}/rcm/completion`)
    .then(payload => { completion.value = payload })
    .catch(() => { completion.value = null })
  if (!cycleMeta.value) {
    cycleMeta.value = (await api.get<{ cycle_vouch: CycleVouchMetadata }>(
      `/api/workspaces/${props.workspace.id}/doc-tests/meta`,
    )).cycle_vouch
  }
  // Reloaded every time: induction runs before the RCM stage and can also be
  // rerun after a retyping, so a cached list would offer fields that moved.
  // Losing it costs the schema surface, not the matrix.
  api.get<{ items: DocumentSchemaCatalogEntry[] }>(
    `/api/workspaces/${props.workspace.id}/documents/schemas`,
  )
    .then(payload => { documentSchemas.value = payload.items ?? [] })
    .catch(() => { documentSchemas.value = [] })
  const requestedRcm = String(route.query.rcm || '')
  const requestedObservation = String(route.query.observation || '')
  // Read before anything opens: `openRcm` rewrites the query, and the paper
  // link would be gone by the time it was looked for.
  const requestedPaper = String(route.query.paper || '')
  const observationParent = requestedObservation
    ? data.value.rcm.find(row => data.value?.observations.some(item => item.id === requestedObservation && item.rcm_id === row.id))
    : undefined
  if (requestedRcm && data.value.rcm.some(item => item.id === requestedRcm)) openRcm(data.value.rcm.find(item => item.id === requestedRcm)!)
  else if (observationParent) openRcm(observationParent)
  // A working-paper link opens the paper itself, over the detail dialog when
  // the same URL asked for one. Closing it drops the key, so a reload driven by
  // an agent commit cannot reopen a paper the auditor has already put down.
  const paperRow = requestedPaper ? data.value.rcm.find(item => item.id === requestedPaper) : undefined
  if (paperRow) openWorkingPaper(paperRow.id)
}
onMounted(() => {
  void reload().catch(error => fail('Could not load planning', error))
})

const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void reload().catch(error => fail('Could not refresh planning', error))
})
onUnmounted(unsubscribe)

async function savePlanning() {
  if (!data.value) return
  saving.value = true
  try {
    data.value.planning = await api.patch(`/api/workspaces/${props.workspace.id}/planning`, {
      context: data.value.planning.context,
      apm_markdown: data.value.planning.apm_markdown,
    })
    emit('changed')
    toast.add({ severity: 'success', summary: 'Planning saved', life: 1800 })
  } catch (error) { fail('Could not save planning', error) }
  finally { saving.value = false }
}
async function generate() {
  try {
    await savePlanning()
    await assistantChat.createChat()
    await assistantChat.send(
      'Update the planning context and APM, then create or reconcile the RCM and the Document and Data Tests that cover it. Do not create a separate audit program.',
      'act', launchMode.value, { command: 'plan', source: 'tab_button' },
    )
    agent.openPanel()
  } catch (error) { fail('Could not start planning', error) }
}
async function addRcm() {
  try {
    const row = await api.post<RcmRow>(`/api/workspaces/${props.workspace.id}/rcm`, {
      process: 'New process', risk: 'Describe the audit risk', risk_rating: 'medium',
      control_attributes: [{ key: 'manual_inspection', assertion: 'Operational', requirement: 'Describe the control requirement', evidence_kind: 'manual_inspection' }],
    })
    // A new row rarely matches the filter that was on, and a risk that is added
    // and immediately invisible reads as a failure to add it.
    rcmFilter.value = []
    await reload(); openRcm(row); emit('changed')
  } catch (error) { fail('Could not add the risk', error) }
}
async function updateRcm(id: string, changes: Partial<RcmRow>) {
  try { await api.patch(`/api/workspaces/${props.workspace.id}/rcm/${id}`, changes); emit('changed') }
  catch (error) { fail('Could not update the risk', error) }
}
async function exportRcm() {
  rcmExporting.value = true
  try { await api.downloadGet(`/api/workspaces/${props.workspace.id}/rcm/export`, `${props.workspace.name}_RCM.xlsx`) }
  catch (error) { fail('Could not export the RCM', error) }
  finally { rcmExporting.value = false }
}
function triggerRcmImport() { rcmImportInput.value?.click() }
async function importRcm(event: Event) {
  const input = event.target as HTMLInputElement
  const file = (input.files ?? [])[0]
  input.value = ''
  if (!file) return
  rcmImporting.value = true
  try {
    const result = await api.uploadOne<{ updated: number; matched: number; unmatched: string[] }>(
      `/api/workspaces/${props.workspace.id}/rcm/import`, file,
    )
    await reload()
    emit('changed')
    toast.add({
      severity: result.unmatched.length ? 'warn' : 'success',
      summary: `RCM imported — ${plural(result.updated, 'row')} updated`,
      detail: result.unmatched.length
        ? `${plural(result.unmatched.length, 'row id')} not found and skipped (no rows are added or removed): ${result.unmatched.join(', ')}`
        : undefined,
      life: 7000,
    })
  } catch (error) { fail('Could not import the RCM', error) }
  finally { rcmImporting.value = false }
}
function openRcm(row: RcmRow) {
  const current = data.value?.rcm.find(item => item.id === row.id) ?? row
  selectedRcmId.value = current.id
  detailOpen.value = true
  void nav.replace('rcm', { rcm: current.id })
}
function closeRcm() {
  detailOpen.value = false
  selectedRcmId.value = null
  void nav.replace('rcm')
}
/** The row's own page, for everything the drawer does not hold. */
function openRcmRow(tab?: string) {
  if (!selectedRcmId.value) return
  void nav.push('rcm-row', { rcm: selectedRcmId.value, tab })
}
function openTest(rollup: TestRollup) {
  void nav.push(rollup.kind === 'datatest' ? 'data-tests' : 'doc-tests', { test: rollup.test_id })
}
function addTestTo(kind: 'data' | 'document' | 'generate') {
  const id = selectedRcmId.value
  if (!id) return
  if (kind === 'generate') return void generatePlannedTests([id])
  void nav.push(kind === 'data' ? 'data-tests' : 'doc-tests', { create: '1', rcm: id })
}
/** The drawer edits a copy; this is what it hands back. */
async function saveRcmRow(changes: Partial<RcmRow>) {
  const id = selectedRcmId.value
  if (!id) return
  saving.value = true
  try {
    await updateRcm(id, changes)
    await reload()
    detailOpen.value = false
    toast.add({ severity: 'success', summary: 'RCM row saved', life: 1800 })
  } finally { saving.value = false }
}
async function refreshRollup() {
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/rcm/rollup`)
    await reload()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Execution roll-up refreshed', life: 1800 })
  }
  catch (error) { fail('Could not refresh the roll-up', error) }
}
async function generatePlannedTests(rowIds: string[] = rowsWithoutTests.value.map(row => row.id)) {
  if (!rowIds.length) return
  generatingTests.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Generate planned test${rowIds.length === 1 ? '' : 's'} for ${rowIds.length} RCM row${rowIds.length === 1 ? '' : 's'}.`,
      'act', launchMode.value,
      {
        source: 'tab_button', requestedOutcomes: ['tests.specified'],
        runContext: { target_refs: rowIds.map(id => `rcm:${id}`) },
      },
    )
    agent.openPanel()
    toast.add({ severity: 'success', summary: `Generating planned test${rowIds.length === 1 ? '' : 's'} for ${rowIds.length} RCM row${rowIds.length === 1 ? '' : 's'}`, life: 3000 })
  } catch (error) { fail('Could not start planned test generation', error) }
  finally { generatingTests.value = false }
}
async function generateAllFindings() {
  generatingFindings.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Draft all eligible findings from the RCM observations.',
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button' },
    )
    agent.openPanel()
    toast.add({
      severity: 'success',
      summary: 'Generating all eligible findings',
      detail: 'Exception observations are used directly for finding drafts.',
      life: 4000,
    })
  } catch (error) { fail('Could not start finding generation', error) }
  finally { generatingFindings.value = false }
}
/** `testIds` runs the outstanding subset; omitted, it re-runs every linked test. */
async function runAllDataTests(testIds?: string[]) {
  runningAllDataTests.value = true
  try {
    const result = await api.post<{ total: number; completed: Array<Record<string, unknown>>; failed: Array<{ data_test_id: string; error: string }> }>(
      `/api/workspaces/${props.workspace.id}/data-tests/run-all-rcm`,
      testIds ? { test_ids: testIds } : {},
    )
    await reload()
    emit('changed')
    toast.add({
      severity: result.failed.length ? 'warn' : 'success',
      summary: `Ran ${result.completed.length} of ${result.total} RCM Data Test${result.total === 1 ? '' : 's'}`,
      detail: result.failed.length ? `${plural(result.failed.length, 'test')} could not run: ${result.failed.map(item => item.data_test_id).join(', ')}` : undefined,
      life: 6000,
    })
  } catch (error) { fail('Could not run RCM Data Tests', error) }
  finally { runningAllDataTests.value = false }
}
async function runAllDocumentTests(only?: string[]) {
  const testIds = only?.length ? only : linkedDocumentTestIds.value
  if (!testIds.length) return
  runningAllDocumentTests.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Run all ${testIds.length} RCM-linked Document Test${testIds.length === 1 ? '' : 's'} and preserve the results.`,
      'act', launchMode.value,
      {
        command: 'run_document_tests',
        source: 'tab_button',
        runContext: { test_ids: testIds },
      },
    )
    agent.openPanel()
    toast.add({
      severity: 'info',
      summary: `Running ${testIds.length} RCM Document Test${testIds.length === 1 ? '' : 's'}`,
      detail: 'Progress and any required actions are visible in the Console.',
      life: 4000,
    })
  } catch (error) { fail('Could not start RCM Document Tests', error) }
  finally { runningAllDocumentTests.value = false }
}
/**
 * Sign off a set of rows in one pass.
 *
 * Blanket sign-off is a real audit act, not a tidy-up, so it asks first and
 * says what it changes: a reviewed row is auditor-owned, and the planning
 * executor preserves rather than rewrites a row whose `created_by` is no
 * longer `agent`. The rows are walked one at a time because there is no bulk
 * route, and a run that only got partway must still report which rows moved.
 */
function markRowsReviewed(ids: string[]) {
  if (!ids.length) return
  confirm.require({
    header: 'Mark rows reviewed',
    message: `Mark ${plural(ids.length, 'RCM row')} as reviewed? `
      + 'Sign-off makes each row auditor-owned, so rerunning the agent will '
      + 'preserve it rather than update it.',
    icon: 'pi pi-check-circle',
    acceptProps: { label: 'Mark reviewed' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      markingReviewed.value = true
      const failed: string[] = []
      try {
        for (const id of ids) {
          try {
            await api.patch(`/api/workspaces/${props.workspace.id}/rcm/${id}`, { review_status: 'reviewed' })
          } catch { failed.push(id) }
        }
        await reload()
        emit('changed')
        toast.add({
          severity: failed.length ? 'warn' : 'success',
          summary: `${plural(ids.length - failed.length, 'row')} marked reviewed`,
          detail: failed.length
            ? `${plural(failed.length, 'row')} could not be updated: ${failed.join(', ')}`
            : undefined,
          life: failed.length ? 6000 : 2200,
        })
      } finally { markingReviewed.value = false }
    },
  })
}

/**
 * The paper is a tab on the row's own page, so this is a redirect rather than
 * a render. It stays because `?paper=` is what an agent milestone hands over,
 * and a link that used to open the engagement's most reviewable artifact must
 * not start landing on the matrix instead.
 */
function openWorkingPaper(rowId: string) {
  void router.replace(nav.to('rcm-row', { rcm: rowId, tab: 'paper' }))
}

// Everything the RCM bar used to spell out as its own button. Only generating
// the missing tests is frequent enough to earn a place in the header; the rest
// are occasional, so they live behind one menu instead of seven controls.
const agentBusy = computed(() => isActive.value || !agent.state.status?.configured)
// What the status bar's actions wait on. Deliberately not `agentBusy`: running
// Data Tests is deterministic and stays available on a workspace with no agent
// configured, so only work actually in flight disables the lane.
const rcmBusy = computed(() => isActive.value
  || generatingTests.value || generatingFindings.value
  || runningAllDataTests.value || runningAllDocumentTests.value
  || markingReviewed.value)
/**
 * The rows the file still owes a finding.
 *
 * The header carries one primary, and this is what claims it whenever there is
 * one: an adverse conclusion nobody has written up is the only thing on this
 * page that is both outstanding and closable from here. With none, the page's
 * ordinary next act — adding a risk — takes the slot.
 */
const findingsPending = computed(() => (data.value?.rcm ?? []).filter(row => {
  const conclusion = String(row.execution_rollup.control_conclusion ?? '')
  return (conclusion === 'ineffective' || conclusion === 'partially_effective')
    && !(data.value?.finding_rollups.by_rcm[row.id]?.length)
}))
const unreviewedRows = computed(
  () => (data.value?.rcm ?? []).filter(row => row.review_status !== 'reviewed').map(row => row.id),
)
/** Running is one act with three scopes, so it is one split button. */
const runOptions = computed(() => [
  {
    label: `Run ${plural(linkedDataTestCount.value, 'Data Test')}`,
    icon: 'pi pi-chart-bar',
    disabled: !linkedDataTestCount.value || isActive.value || runningAllDataTests.value,
    command: () => void runAllDataTests(),
  },
  {
    label: `Run ${plural(linkedDocumentTestIds.value.length, 'Document Test')}`,
    icon: 'pi pi-file-check',
    disabled: !linkedDocumentTestIds.value.length || isActive.value || runningAllDocumentTests.value,
    command: () => void runAllDocumentTests(),
  },
  { separator: true },
  {
    label: 'Run everything linked',
    icon: 'pi pi-play',
    disabled: isActive.value || runningAllDataTests.value || runningAllDocumentTests.value,
    command: async () => { await runAllDataTests(); await runAllDocumentTests() },
  },
])
// Everything the header used to spell out as its own button. None of these is
// frequent enough to stand permanently beside the one that says what is
// outstanding, so they live behind one menu instead of seven controls.
const rcmActions = computed(() => [
  {
    label: 'Generate planning drafts',
    icon: 'pi pi-sparkles',
    disabled: agentBusy.value,
    command: () => void generate(),
  },
  {
    label: 'Generate all findings',
    icon: 'pi pi-flag',
    disabled: agentBusy.value || generatingFindings.value,
    command: () => void generateAllFindings(),
  },
  {
    label: `Generate tests for ${plural(rowsWithoutTests.value.length, 'uncovered risk')}`,
    icon: 'pi pi-bolt',
    disabled: agentBusy.value || !rowsWithoutTests.value.length || generatingTests.value,
    command: () => void generatePlannedTests(),
  },
  { separator: true },
  {
    label: 'Refresh roll-up',
    icon: 'pi pi-refresh',
    command: () => void refreshRollup(),
  },
  { separator: true },
  {
    label: 'Export RCM',
    icon: 'pi pi-download',
    disabled: rcmExporting.value,
    command: () => void exportRcm(),
  },
  {
    label: 'Import RCM',
    icon: 'pi pi-upload',
    disabled: rcmImporting.value,
    command: () => triggerRcmImport(),
  },
])
</script>

<template>
  <div v-if="data" class="planning-tab">
    <!-- One title, not two: the matrix used to carry `RCM` here and `Risk and
         control matrix` again over the grid. One count sentence, and at most
         one primary — the write-up the file owes, or adding a risk. -->
    <header class="page-head">
      <h1>Risk and control matrix</h1>
      <span class="grow" />
      <Button label="Add risk" icon="pi pi-plus" size="small" outlined severity="secondary" @click="addRcm" />
      <SplitButton
        label="Run tests"
        icon="pi pi-play"
        size="small"
        outlined
        severity="secondary"
        :model="runOptions"
        :loading="runningAllDataTests || runningAllDocumentTests"
        :disabled="isActive"
        @click="runAllDataTests()"
      />
      <Button
        v-if="findingsPending.length"
        :label="`Draft ${plural(findingsPending.length, 'finding')}`"
        icon="pi pi-flag"
        size="small"
        severity="warn"
        :disabled="rcmBusy || agentBusy"
        @click="generateAllFindings"
      />
      <UiOverflowMenu :items="rcmActions" tooltip="More RCM actions" />
    </header>
    <section class="rcm-view">
      <input ref="rcmImportInput" type="file" accept=".xlsx,.xls,.csv,.tsv" hidden @change="importRcm"/>
      <UiReviewBar
        :lanes="rcmStatusModel.lanes"
        :chips="RCM_CHIPS"
        :filters="rcmStatusModel.filters"
        allLabel="All rows"
        :total="data.rcm.length"
        :filter="rcmFilter"
        @filter="rcmFilter = ($event as RcmFilter[])"
      >
        <!-- Blanket sign-off writes to the file, so it stays a button beside
             the chip that counts what it settles rather than becoming one. -->
        <template v-if="unreviewedRows.length" #settle>
          <button
            type="button"
            class="settle"
            :disabled="rcmBusy"
            @click="markRowsReviewed(unreviewedRows)"
          >
            Mark {{ unreviewedRows.length }} reviewed
          </button>
        </template>
      </UiReviewBar>

      <RcmGrid
        :rows="visibleRcm"
        :findingRollups="data.finding_rollups"
        :selectedId="detailOpen ? selectedRcmId : null"
        @open="openRcm"
      />
      <!-- A filter that hides every row would otherwise read as an empty RCM. -->
      <p v-if="data.rcm.length && !visibleRcm.length" class="empty">
        No row matches this filter. It was counted against the whole matrix, which may have moved since.
      </p>
    </section>

    <!-- Quick edits open beside the matrix rather than over it. What the
         drawer does not hold — the criteria and their citations, the cycle
         comparison editor, provenance, the observations — is on the row's own
         page, which the id in its header links to. -->
    <Drawer
      v-model:visible="detailOpen"
      position="right"
      class="aw-drawer--bare"
      :style="{ width: 'min(27.5rem, 96vw)' }"
      @hide="closeRcm"
    >
      <RcmRowDrawer
        v-if="selectedRcm"
        :key="selectedRcm.id"
        :row="selectedRcm"
        :findings="selectedFindings"
        :saving="saving"
        @save="saveRcmRow"
        @close="closeRcm"
        @paper="openRcmRow('paper')"
        @openRow="openRcmRow"
        @openTest="openTest"
        @addTest="addTestTo"
      />
    </Drawer>
  </div>
</template>

<style scoped>
.rcm-view { display:flex; flex-direction:column; gap:.75rem }

/* One 36px row: the title, what the matrix holds, and at most one primary. */
.page-head { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; min-height:2.25rem }
.page-head h1 { margin:0; color:var(--aw-ink-strong); font-size:var(--aw-text-xl); font-weight:700; letter-spacing:-0.01em }
.headline { margin:0; color:var(--aw-muted); font-size:var(--aw-text-sm) }
.grow { flex:1 }
.settle {
  padding:.25rem .625rem; border:1px solid var(--aw-border-strong); border-radius:var(--aw-radius-control);
  background:var(--aw-panel); color:var(--aw-ink-soft);
  font:inherit; font-size:var(--aw-text-xs); font-weight:600; white-space:nowrap; cursor:pointer;
}
.settle:hover:not(:disabled) { border-color:var(--aw-teal); color:var(--aw-teal) }
.settle:focus-visible { outline:2px solid var(--aw-teal); outline-offset:1px }
.settle:disabled { opacity:.5; cursor:not-allowed }
.planning-tab { display:flex; flex-direction:column; gap: var(--aw-section-gap); min-height:100% }
.muted { color:var(--aw-muted); font-size:var(--aw-text-sm) }
.section-toolbar { display:flex; align-items:center; gap:.55rem }
.section-toolbar>div { display:flex; flex-direction:column }
.section-toolbar>span { flex:1 }
.empty { padding:1rem; color:var(--aw-muted); border:1px dashed var(--aw-border); border-radius:var(--aw-radius-control) }
</style>
