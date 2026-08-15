<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import SplitButton from 'primevue/splitbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AuditObservation, CycleVouchMetadata, MarkdownTemplate, PlanningPayload, PlanningRecord, RcmCompletion, RcmRow, TestRollup, WorkspaceSummary, WorkingPaper } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import ProvenanceRail from './agent/ProvenanceRail.vue'
import UiStatusLanes from './ui/UiStatusLanes.vue'
import type { StatusAction } from './ui/statusLanes'
import { FILTER_LABELS, filterRows, rcmStatus } from './planning/rcmStatus'
import type { RcmActionKey, RcmFilter } from './planning/rcmStatus'
import RcmGrid from './planning/RcmGrid.vue'
import RcmControlAttributesEditor from './planning/RcmControlAttributesEditor.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiTestStatus from './ui/UiTestStatus.vue'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary; section: 'apm' | 'rcm' }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { isActive, launchMode } = agent

const data = ref<PlanningPayload | null>(null)
const cycleMeta = ref<CycleVouchMetadata | null>(null)
const saving = ref(false)
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)
const selectedRcmId = ref<string | null>(null)
const apmImportInput = ref<HTMLInputElement>()
const rcmImportInput = ref<HTMLInputElement>()
const apmExporting = ref(false)
const apmImporting = ref(false)
const rcmExporting = ref(false)
const rcmImporting = ref(false)
const generatingTests = ref(false)
const generatingFindings = ref(false)
const runningAllDataTests = ref(false)
const runningAllDocumentTests = ref(false)
const detailOpen = ref(false)
const paperOpen = ref(false)
// The completion gates the status bar reports. Fetched beside the planning
// payload rather than derived here: the backend already owns what counts as an
// unreviewed conclusion or a capped one, and two definitions would drift.
const completion = ref<RcmCompletion | null>(null)
// Which subset of the matrix the bar has asked the grid to show. A view over
// one array, so the counts above and the rows below can never disagree.
const rcmFilter = ref<RcmFilter | null>(null)
// Provenance is a reviewer's question, not an author's, so it stays closed
// until asked for rather than taking a column from the editor by default.
const apmProvenanceOpen = ref(false)
const workingPaper = ref<WorkingPaper | null>(null)
const reviewStatuses = ['draft', 'prepared', 'review_required', 'reviewed']
const selectedRcm = computed(() => data.value?.rcm.find(item => item.id === selectedRcmId.value) ?? null)
const visibleRcm = computed(() => filterRows(
  data.value?.rcm ?? [], rcmFilter.value, data.value?.finding_rollups, completion.value,
))
const rcmStatusModel = computed(() => rcmStatus(
  data.value?.rcm ?? [], data.value?.finding_rollups, completion.value,
))
const rcmFilterLabel = computed(() => (rcmFilter.value ? FILTER_LABELS[rcmFilter.value] : ''))
const selectedObservations = computed(() => (data.value?.observations ?? []).filter(item => item.rcm_id === selectedRcmId.value))
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
  const requestedRcm = String(route.query.rcm || '')
  const requestedObservation = String(route.query.observation || '')
  const observationParent = requestedObservation
    ? data.value.rcm.find(row => data.value?.observations.some(item => item.id === requestedObservation && item.rcm_id === row.id))
    : undefined
  if (requestedRcm && data.value.rcm.some(item => item.id === requestedRcm)) openRcm(data.value.rcm.find(item => item.id === requestedRcm)!)
  else if (observationParent) openRcm(observationParent)
}
onMounted(() => void reload().catch(error => fail('Could not load planning', error)))
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
async function exportApm() {
  apmExporting.value = true
  try { await api.downloadGet(`/api/workspaces/${props.workspace.id}/planning/apm/export`, `${props.workspace.name}_APM.md`) }
  catch (error) { fail('Could not export the APM', error) }
  finally { apmExporting.value = false }
}
function triggerApmImport() { apmImportInput.value?.click() }
async function importApm(event: Event) {
  const input = event.target as HTMLInputElement
  const file = (input.files ?? [])[0]
  input.value = ''
  if (!file) return
  apmImporting.value = true
  try {
    data.value!.planning = await api.uploadOne<PlanningRecord>(`/api/workspaces/${props.workspace.id}/planning/apm/import`, file)
    emit('changed')
    toast.add({ severity: 'success', summary: 'APM imported', life: 1800 })
  } catch (error) { fail('Could not import the APM', error) }
  finally { apmImporting.value = false }
}
async function generate() {
  try {
    await savePlanning()
    await assistantChat.createChat()
    await assistantChat.send(
      'Update the planning context and APM, then create or reconcile the RCM and the Document and Data Tests that cover it. Do not create a separate audit program.',
      'act', launchMode.value, { command: 'plan', source: 'tab_button' },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
  } catch (error) { fail('Could not start planning', error) }
}
async function openTemplate() {
  try { template.value = await api.get(`/api/workspaces/${props.workspace.id}/templates/apm`); templateOpen.value = true }
  catch (error) { fail('Could not load the template', error) }
}
async function saveTemplate(reset = false) {
  if (!template.value) return
  try {
    template.value = await api.put(`/api/workspaces/${props.workspace.id}/templates/apm`, reset ? { reset: true } : { markdown: template.value.markdown })
    toast.add({ severity: 'success', summary: reset ? 'Default template restored' : 'Template saved', life: 1800 })
  } catch (error) { fail('Could not save the template', error) }
}
async function addRcm() {
  try {
    const row = await api.post<RcmRow>(`/api/workspaces/${props.workspace.id}/rcm`, {
      process: 'New process', risk: 'Describe the audit risk', risk_rating: 'medium',
      control_attributes: [{ key: 'manual_inspection', assertion: 'Operational', requirement: 'Describe the control requirement', evidence_kind: 'manual_inspection' }],
    })
    // A new row rarely matches the filter that was on, and a risk that is added
    // and immediately invisible reads as a failure to add it.
    rcmFilter.value = null
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
async function saveRcmDetail() {
  if (!selectedRcm.value) return
  try {
    await updateRcm(selectedRcm.value.id, {
      process: selectedRcm.value.process, risk: selectedRcm.value.risk,
      risk_rating: selectedRcm.value.risk_rating,
      // business_cycle is a projection of the attributes; the backend derives it.
      control_attributes: selectedRcm.value.control_attributes,
      control: selectedRcm.value.control, control_type: selectedRcm.value.control_type,
      control_owner: selectedRcm.value.control_owner, criteria: selectedRcm.value.criteria,
      review_status: selectedRcm.value.review_status,
    })
    await reload()
    toast.add({ severity: 'success', summary: 'RCM row saved', life: 1800 })
  } catch (error) { fail('Could not save the RCM row', error) }
}
function removeRcm(id: string) {
  const row = data.value?.rcm.find(item => item.id === id)
  const label = row?.process?.trim() || id
  const testCount = row?.test_refs?.length ?? 0
  const testNote = testCount
    ? ` Its ${testCount} linked test${testCount === 1 ? '' : 's'} will be unlinked, not deleted; findings will be unlinked too.`
    : ' Any linked findings will be unlinked.'
  confirm.require({
    header: 'Remove RCM row',
    message: `Remove "${label}"?${testNote}`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try { await api.del(`/api/workspaces/${props.workspace.id}/rcm/${id}`); detailOpen.value = false; await reload(); emit('changed') }
      catch (error) { fail('Could not remove the risk', error) }
    },
  })
}
function openRcm(row: RcmRow) {
  const current = data.value?.rcm.find(item => item.id === row.id) ?? row
  selectedRcmId.value = current.id
  detailOpen.value = true
  void nav.replace('rcm', { rcm: current.id })
}
function linkedTests(row: RcmRow): TestRollup[] {
  return row.execution_rollup.test_rollups ?? []
}
async function promoteObservation(item: AuditObservation) {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Draft a finding from observation ${item.id}.`,
      'act', 'permission', {
        command: 'draft_findings', source: 'tab_button',
        runContext: { observation_id: item.id },
      },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({ severity: 'success', summary: 'Finding-draft workflow started', detail: 'Review the proposed finding in the assistant before it is saved.', life: 3200 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
function openTest(rollup: TestRollup) {
  void nav.replace(rollup.kind === 'datatest' ? 'data-tests' : 'doc-tests', { test: rollup.test_id })
}
function createTest(kind: 'data' | 'document') {
  if (!selectedRcm.value) return
  void nav.replace(kind === 'data' ? 'data-tests' : 'doc-tests', { create: '1', rcm: selectedRcm.value.id })
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
    if (!agent.state.drawerOpen) agent.toggleDrawer()
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
    if (!agent.state.drawerOpen) agent.toggleDrawer()
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
    if (!agent.state.drawerOpen) agent.toggleDrawer()
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
 * The status bar names what it wants done; the tab still owns how. Each key
 * routes to the handler that already existed for it, scoped to the rows or
 * tests the lane counted rather than to the whole matrix.
 */
function runStatusAction(action: StatusAction) {
  switch (action.key as RcmActionKey) {
    case 'generate_tests':
      return void generatePlannedTests(action.ids?.length ? action.ids : undefined)
    case 'run_data_tests': return void runAllDataTests(action.ids)
    case 'run_document_tests': return void runAllDocumentTests(action.ids)
    case 'refresh_rollup': return void refreshRollup()
    case 'draft_findings': return void generateAllFindings()
  }
}

async function openWorkingPaper() {
  if (!selectedRcm.value) return
  try { workingPaper.value = await api.get(`/api/workspaces/${props.workspace.id}/rcm/${selectedRcm.value.id}/working-paper`); paperOpen.value = true }
  catch (error) { fail('Could not render the RCM working paper', error) }
}
async function copyPaper(kind: 'markdown' | 'html') {
  if (!workingPaper.value) return
  await navigator.clipboard.writeText(workingPaper.value[kind])
  toast.add({ severity: 'success', summary: `${kind === 'markdown' ? 'Markdown' : 'HTML'} copied`, life: 1600 })
}
const copyOptions = [
  { label: 'Copy Markdown', icon: 'pi pi-copy', command: () => void copyPaper('markdown') },
  { label: 'Copy HTML', icon: 'pi pi-code', command: () => void copyPaper('html') },
]

// Everything the RCM bar used to spell out as its own button. Only generating
// the missing tests is frequent enough to earn a place in the header; the rest
// are occasional, so they live behind one menu instead of seven controls.
const agentBusy = computed(() => isActive.value || !agent.state.status?.configured)
// What the status bar's actions wait on. Deliberately not `agentBusy`: running
// Data Tests is deterministic and stays available on a workspace with no agent
// configured, so only work actually in flight disables the lane.
const rcmBusy = computed(() => isActive.value
  || generatingTests.value || generatingFindings.value
  || runningAllDataTests.value || runningAllDocumentTests.value)
// An untouched memorandum gets an empty state rather than a blank editor: there
// is nothing to attribute, nothing to save, and no reason to show a formatting
// toolbar above nothing.
const apmHasContent = computed(() => Boolean(data.value?.planning.apm_markdown?.trim()))
/** Open the editor on a memorandum the auditor intends to write by hand. */
function startApm() {
  if (data.value) data.value.planning.apm_markdown = '# Audit planning memorandum\n\n'
}
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
  { separator: true },
  {
    label: 'Run all Data Tests',
    icon: 'pi pi-play',
    disabled: !linkedDataTestCount.value || isActive.value || runningAllDataTests.value,
    command: () => void runAllDataTests(),
  },
  {
    label: 'Run all Document Tests',
    icon: 'pi pi-play',
    disabled: !linkedDocumentTestIds.value.length || isActive.value || runningAllDocumentTests.value,
    command: () => void runAllDocumentTests(),
  },
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
    <UiPageHeader :title="section === 'apm' ? 'APM' : 'RCM'">
      <Button
        v-if="section === 'apm'"
        label="Generate planning drafts"
        icon="pi pi-sparkles"
        size="small"
        :disabled="agentBusy"
        @click="generate"
      />
      <!-- The status bar owns every action that depends on where the file
           stands, so the header keeps only the occasional ones. -->
      <UiOverflowMenu v-else :items="rcmActions" tooltip="More RCM actions" />
    </UiPageHeader>
    <section v-if="section === 'apm'" class="apm-view">
      <!-- Provenance describes a document that exists. On an untouched
           engagement the old unconditional label read "Auditor edited" over a
           blank editor nobody had opened. -->
      <div class="section-toolbar"><div><strong>Audit planning memorandum</strong><span v-if="apmHasContent" class="muted">{{ data.planning.created_by === 'agent' ? 'Agent draft' : 'Auditor edited' }}</span></div><span/><Button :label="apmProvenanceOpen ? 'Hide provenance' : 'Provenance'" icon="pi pi-shield" size="small" :outlined="!apmProvenanceOpen" @click="apmProvenanceOpen = !apmProvenanceOpen"/><Button label="Export" icon="pi pi-download" size="small" outlined :loading="apmExporting" @click="exportApm"/><Button label="Import" icon="pi pi-upload" size="small" outlined :loading="apmImporting" @click="triggerApmImport"/><Button label="Template" icon="pi pi-file-edit" size="small" outlined @click="openTemplate"/><Button label="Save APM" icon="pi pi-save" size="small" :loading="saving" @click="savePlanning"/></div>
      <input ref="apmImportInput" type="file" accept=".md,.markdown,.txt" hidden @change="importApm"/>
      <div class="apm-body" :class="{ 'with-rail': apmProvenanceOpen }">
        <UiEmptyState
          v-if="!apmHasContent"
          icon="pi pi-map"
          title="No planning memorandum yet"
          description="The assistant drafts it from the engagement material — the documents you imported, the planning context, and the risks already recorded. You can also write it yourself."
        >
          <Button label="Generate planning drafts" icon="pi pi-sparkles" :disabled="agentBusy" @click="generate" />
          <Button label="Start writing" icon="pi pi-pencil" outlined @click="startApm" />
        </UiEmptyState>
        <div v-else class="apm-editor"><MarkdownEditor v-model="data.planning.apm_markdown" placeholder="Write the planning memorandum, or generate a draft from the engagement material."/></div>
        <!-- Attribution is per artifact, which is what the sidecars record.
             There is no per-section trail because no per-section record exists. -->
        <ProvenanceRail v-if="apmProvenanceOpen" :workspaceId="workspace.id" artifactRef="planning:apm"/>
      </div>
    </section>
    <section v-else class="rcm-view">
      <input ref="rcmImportInput" type="file" accept=".xlsx,.xls,.csv,.tsv" hidden @change="importRcm"/>
      <UiStatusLanes
        :lanes="rcmStatusModel.lanes"
        :disclosures="rcmStatusModel.disclosures"
        :filter="rcmFilter"
        :filterLabel="rcmFilterLabel"
        :busy="rcmBusy"
        :canRunAgent="!isActive && Boolean(agent.state.status?.configured)"
        @filter="rcmFilter = ($event as RcmFilter | null)"
        @action="runStatusAction"
      />
      <RcmGrid :rows="visibleRcm" :dataTests="data.data_tests" :documentTests="data.document_tests" :findingRollups="data.finding_rollups" :generating="generatingTests" :canGenerate="!isActive && Boolean(agent.state.status?.configured)" @add="addRcm" @update="updateRcm" @remove="removeRcm" @open="openRcm" @generate="generatePlannedTests"/>
      <!-- A filter that hides every row would otherwise read as an empty RCM. -->
      <p v-if="data.rcm.length && !visibleRcm.length" class="empty">
        No row matches this filter. It was counted against the whole matrix, which may have moved since.
      </p>
    </section>

    <Dialog v-model:visible="detailOpen" modal :header="selectedRcm ? `${selectedRcm.id} · RCM detail` : 'RCM detail'" :style="{ width: 'min(1120px, 97vw)' }" :contentStyle="{ maxHeight: '82vh', overflow: 'auto' }">
      <div v-if="selectedRcm" class="rcm-detail">
        <div class="rcm-fields"><label>Process<InputText v-model="selectedRcm.process"/></label><label>Risk rating<Select v-model="selectedRcm.risk_rating" :options="['low','medium','high','critical']"/></label><label class="wide">Risk<Textarea v-model="selectedRcm.risk" rows="2" autoResize/></label><label class="wide">Control<Textarea v-model="selectedRcm.control" rows="2" autoResize/></label><label>Control type<InputText v-model="selectedRcm.control_type"/></label><label>Control owner<InputText v-model="selectedRcm.control_owner"/></label><label>Review status<Select v-model="selectedRcm.review_status" :options="reviewStatuses"/></label><label class="wide">Criteria<Textarea v-model="selectedRcm.criteria" rows="2" autoResize/></label></div>
        <RcmControlAttributesEditor v-model="selectedRcm.control_attributes" :metadata="cycleMeta" />
        <div class="detail-actions"><Button label="Save RCM row" icon="pi pi-save" size="small" outlined @click="saveRcmDetail"/><Button label="RCM working paper" icon="pi pi-file" size="small" outlined @click="openWorkingPaper"/><Button label="Add Data Test" icon="pi pi-chart-bar" size="small" outlined @click="createTest('data')"/><Button label="Add Document Test" icon="pi pi-file-check" size="small" @click="createTest('document')"/></div>
        <section class="planned-list"><article v-for="item in linkedTests(selectedRcm)" :key="item.test_id" class="planned-card">
          <div class="planned-head"><div><strong>{{ item.test_id }}</strong><Tag :value="item.kind === 'datatest' ? 'data' : 'document'" severity="secondary"/><UiTestStatus :status="item.status" /></div><span>{{ plural(item.exception_count, 'exception') }} · {{ item.open_exception_count }} open</span></div>
          <p class="planned-title">{{ item.title }}</p>
          <p class="muted">{{ item.result_summary || 'Not executed yet.' }}</p>
          <p v-if="item.assurance_scope" class="muted">
            <strong>{{ item.assurance_label }}</strong> · {{ plural(item.tested_items ?? 0, 'tested item') }} ·
            {{ item.failed_items ?? 0 }} failed · {{ item.incomplete_items ?? 0 }} incomplete ·
            {{ item.assertion_mismatches ?? 0 }} diagnostic assertion mismatch(es) ·
            {{ item.conclusion_eligible ? 'population conclusion eligible' : 'no population conclusion' }}
          </p>
          <p v-if="item.scope_limitations" class="muted">Limitation: {{ item.scope_limitations }}</p>
          <div class="card-actions"><Button label="Open test" icon="pi pi-arrow-up-right" size="small" outlined @click="openTest(item)"/></div>
        </article><p v-if="!linkedTests(selectedRcm).length" class="empty">This RCM row has no linked test and cannot pass coverage.</p></section>
        <ProvenanceRail v-if="selectedRcm" :key="selectedRcm.id" :workspaceId="workspace.id" :artifactRef="`rcm:${selectedRcm.id}`" class="detail-provenance"/>
        <section v-if="selectedObservations.length" class="observations"><strong>Exception observations</strong><div v-for="item in selectedObservations" :key="item.id"><Tag :value="item.outcome" :severity="item.outcome === 'exception' ? 'danger' : 'warn'"/><span>{{ item.summary }}</span><small>{{ item.classification.replaceAll('_', ' ') }}</small><span class="observation-actions"><Button label="Draft finding" icon="pi pi-sparkles" size="small" severity="secondary" :disabled="item.outcome !== 'exception'" @click="promoteObservation(item)"/></span></div></section>
      </div>
    </Dialog>
    <Dialog v-model:visible="templateOpen" modal header="APM template" :style="{ width: 'min(900px, 94vw)' }"><p class="muted">Workspace override · placeholders use <code v-pre>{{name}}</code>.</p><Textarea v-if="template" v-model="template.markdown" class="template-editor" rows="22" spellcheck="false"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
    <Dialog v-model:visible="paperOpen" modal header="RCM working paper" :style="{ width: 'min(980px, 95vw)' }"><div v-if="workingPaper" class="working-paper" v-html="workingPaper.html"/><template #footer><SplitButton label="Copy" icon="pi pi-copy" :model="copyOptions" @click="copyPaper('markdown')"/></template></Dialog>
  </div>
</template>

<style scoped>
.apm-body { display:grid; grid-template-columns:minmax(0,1fr); gap:1rem; align-items:start }
.apm-body.with-rail { grid-template-columns:minmax(0,1fr) 20rem }
@container workspace-panel (max-width: 60rem) { .apm-body.with-rail { grid-template-columns:minmax(0,1fr) } }
.detail-provenance { max-width:34rem }
.rcm-view { display:flex; flex-direction:column; gap:.85rem }
.planning-tab { display:flex; flex-direction:column; gap: var(--aw-section-gap); min-height:100% }.muted { color:var(--aw-muted); font-size:var(--aw-text-sm) }.section-toolbar,.detail-actions,.card-actions { display:flex; align-items:center; gap:.55rem }.section-toolbar>div { display:flex; flex-direction:column }.section-toolbar>span { flex:1 }.apm-editor { min-height:34rem }.apm-editor>:deep(.markdown-editor) { min-height:34rem }.template-editor { width:100%; font-family:var(--aw-font-mono); font-size:var(--aw-text-sm) }.rcm-detail { display:flex; flex-direction:column; gap: var(--aw-section-gap) }.rcm-fields,.planned-fields,.outcome { display:grid; grid-template-columns:1fr 1fr; gap:.7rem }.wide { grid-column:1/-1 }label { display:flex; flex-direction:column; gap:.3rem; color:var(--aw-ink-soft); font-size:var(--aw-text-sm); font-weight:600 }.planned-list { display:flex; flex-direction:column; gap:.8rem }.planned-card { padding:.85rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel) }.planned-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; margin-bottom:.7rem }.planned-head>div { display:flex; align-items:center; gap:.5rem }.planned-head>span { color:var(--aw-muted); font-size:var(--aw-text-sm) }.execution-cards { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin:.75rem 0; padding:.65rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-canvas) }.execution-cards>strong { width:100% }.execution-cards button:not(.p-button) { border:1px solid var(--aw-border); background:var(--aw-panel); border-radius:var(--aw-radius-pill); padding:.3rem .55rem; color:var(--aw-teal); cursor:pointer }.execution-cards i { margin-right:.3rem }.card-actions { justify-content:flex-end; margin-top:.7rem }.observations { display:flex; flex-direction:column; gap:.75rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }.observations>div { display:grid; grid-template-columns:auto minmax(0,1fr); gap:.4rem .5rem; align-items:center; padding-bottom:.65rem; border-bottom:1px solid var(--aw-border) }.observations>div:last-child { border-bottom:0 }.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions { grid-column:2; color:var(--aw-muted) }.observation-actions { display:flex; flex-wrap:wrap; gap:.4rem }.empty { padding:1rem; color:var(--aw-muted); border:1px dashed var(--aw-border); border-radius:var(--aw-radius-control) }.working-paper { max-width:52rem; margin:auto; line-height:1.6 }@media(max-width:800px){.rcm-fields,.planned-fields,.outcome{grid-template-columns:1fr}.wide{grid-column:auto}.detail-actions{flex-wrap:wrap}.observations>div{grid-template-columns:1fr}.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions{grid-column:1}}
</style>
