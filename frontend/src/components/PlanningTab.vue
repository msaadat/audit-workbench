<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { AgentRun, AuditObservation, MarkdownTemplate, PlanningPayload, PlanningRecord, RcmRow, TestRollup, WorkspaceSummary, WorkingPaper } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import RcmGrid from './planning/RcmGrid.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiTestStatus from './ui/UiTestStatus.vue'

const props = defineProps<{ workspace: WorkspaceSummary; section: 'apm' | 'rcm' }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const router = useRouter()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { isActive, launchMode } = agent

const data = ref<PlanningPayload | null>(null)
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
const runningAllDataTests = ref(false)
const detailOpen = ref(false)
const paperOpen = ref(false)
const workingPaper = ref<WorkingPaper | null>(null)
const reviewStatuses = ['draft', 'prepared', 'review_required', 'reviewed']
const observationDispositions = [
  'confirmed_control_exception', 'data_quality_issue', 'expected_or_benign',
  'screening_follow_up', 'invalid_test_or_result', 'duplicate', 'draft_finding_candidate',
]
const selectedRcm = computed(() => data.value?.rcm.find(item => item.id === selectedRcmId.value) ?? null)
const selectedObservations = computed(() => (data.value?.observations ?? []).filter(item => item.rcm_id === selectedRcmId.value))
const rowsWithoutTests = computed(() => (data.value?.rcm ?? []).filter(row => (row.execution_rollup.tests ?? row.test_refs.length) === 0))
const linkedDataTestCount = computed(() => (data.value?.data_tests ?? []).filter(test => test.rcm_id).length)

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
async function reload() {
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
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
    await assistantChat.send(
      'Update the planning context and APM, then create or reconcile the RCM and the Document and Data Tests that cover it. Do not create a separate audit program.',
      'act', launchMode.value, { goalTemplate: 'planning', source: 'tab_button' },
    )
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
    const row = await api.post<RcmRow>(`/api/workspaces/${props.workspace.id}/rcm`, { process: 'New process', risk: 'Describe the audit risk', risk_rating: 'medium' })
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
      summary: `RCM imported — ${result.updated} row(s) updated`,
      detail: result.unmatched.length
        ? `${result.unmatched.length} row id(s) not found and skipped (no rows are added or removed): ${result.unmatched.join(', ')}`
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
      risk_rating: selectedRcm.value.risk_rating, assertion: selectedRcm.value.assertion,
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
  void router.replace({ query: workspaceQuery('rcm', { rcm: current.id }) })
}
function linkedTests(row: RcmRow): TestRollup[] {
  return row.execution_rollup.test_rollups ?? []
}
async function saveObservation(item: AuditObservation) {
  if (!item.disposition) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/observations/${item.id}`, {
      disposition: item.disposition, auditor_note: item.auditor_note,
    })
    await reload()
    emit('changed')
  } catch (error) { fail('Could not disposition the observation', error) }
}
async function promoteObservation(item: AuditObservation) {
  try {
    await assistantChat.send(
      `Draft a finding from observation ${item.id}.`,
      'act', 'permission', {
        goalTemplate: 'finding_draft', source: 'tab_button',
        runContext: { observation_id: item.id },
      },
    )
    toast.add({ severity: 'success', summary: 'Finding-draft workflow started', detail: 'Review the proposed finding in the assistant before it is saved.', life: 3200 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
function canDraftFinding(item: AuditObservation) {
  return item.status === 'disposed'
    && ['confirmed_control_exception', 'draft_finding_candidate'].includes(item.disposition ?? '')
}
function openTest(rollup: TestRollup) {
  void router.replace({
    query: workspaceQuery(rollup.kind === 'datatest' ? 'data-tests' : 'doc-tests', { test: rollup.test_id }),
  })
}
function createTest(kind: 'data' | 'document') {
  if (!selectedRcm.value) return
  void router.replace({
    query: { tab: kind === 'data' ? 'data-tests' : 'doc-tests', create: '1', rcm: selectedRcm.value.id },
  })
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
    const run = await api.post<AgentRun>(`/api/workspaces/${props.workspace.id}/agent/runs`, {
      mode: launchMode.value,
      requested_outcomes: ['tests.specified'],
      target_refs: rowIds.map(id => `rcm:${id}`),
      generation_mode: 'reuse_existing',
    })
    await agent.openRun(run.id)
    toast.add({ severity: 'success', summary: `Generating planned test${rowIds.length === 1 ? '' : 's'} for ${rowIds.length} RCM row${rowIds.length === 1 ? '' : 's'}`, life: 3000 })
  } catch (error) { fail('Could not start planned test generation', error) }
  finally { generatingTests.value = false }
}
async function runAllDataTests() {
  runningAllDataTests.value = true
  try {
    const result = await api.post<{ total: number; completed: Array<Record<string, unknown>>; failed: Array<{ data_test_id: string; error: string }> }>(
      `/api/workspaces/${props.workspace.id}/data-tests/run-all-rcm`,
    )
    await reload()
    emit('changed')
    toast.add({
      severity: result.failed.length ? 'warn' : 'success',
      summary: `Ran ${result.completed.length} of ${result.total} RCM Data Test${result.total === 1 ? '' : 's'}`,
      detail: result.failed.length ? `${result.failed.length} test(s) could not run: ${result.failed.map(item => item.data_test_id).join(', ')}` : undefined,
      life: 6000,
    })
  } catch (error) { fail('Could not run RCM Data Tests', error) }
  finally { runningAllDataTests.value = false }
}
async function openWorkingPaper() {
  if (!selectedRcm.value) return
  try { workingPaper.value = await api.get(`/api/workspaces/${props.workspace.id}/rcm/${selectedRcm.value.id}/working-paper`); paperOpen.value = true }
  catch (error) { fail('Could not render the RCM working paper', error) }
}
async function copyPaper(kind: 'markdown' | 'html') {
  if (workingPaper.value) await navigator.clipboard.writeText(workingPaper.value[kind])
}
</script>

<template>
  <div v-if="data" class="planning-tab">
    <UiPageHeader :title="section === 'apm' ? 'APM' : 'RCM'" :description="section === 'apm' ? 'Define the engagement context and audit approach.' : 'Manage risks, controls, coverage, and linked test execution.'">
      <Button label="Generate planning drafts" icon="pi pi-sparkles" size="small" :disabled="isActive || !agent.state.status?.configured" @click="generate" />
    </UiPageHeader>
    <section v-if="section === 'apm'" class="apm-view">
      <div class="section-toolbar"><div><strong>Audit Planning Memorandum</strong><span class="muted">{{ data.planning.created_by === 'agent' ? 'Agent draft' : 'Auditor edited' }}</span></div><span/><Button label="Export" icon="pi pi-download" size="small" outlined :loading="apmExporting" @click="exportApm"/><Button label="Import" icon="pi pi-upload" size="small" outlined :loading="apmImporting" @click="triggerApmImport"/><Button label="Template" icon="pi pi-file-edit" size="small" outlined @click="openTemplate"/><Button label="Save APM" icon="pi pi-save" size="small" :loading="saving" @click="savePlanning"/></div>
      <input ref="apmImportInput" type="file" accept=".md,.markdown,.txt" hidden @change="importApm"/>
      <div class="apm-editor"><MarkdownEditor v-model="data.planning.apm_markdown"/></div>
    </section>
    <section v-else>
      <div class="rollup-bar"><span>Execution status is computed from linked durable Data and Document Test results.</span><Button v-if="rowsWithoutTests.length" :label="`Generate planned tests (${rowsWithoutTests.length})`" icon="pi pi-sparkles" size="small" :disabled="isActive || !agent.state.status?.configured" :loading="generatingTests" @click="generatePlannedTests()"/><Button label="Run all Data Tests" icon="pi pi-play" size="small" outlined :disabled="!linkedDataTestCount || isActive" :loading="runningAllDataTests" @click="runAllDataTests"/><Button label="Export" icon="pi pi-download" size="small" outlined :loading="rcmExporting" @click="exportRcm"/><Button label="Import" icon="pi pi-upload" size="small" outlined :loading="rcmImporting" @click="triggerRcmImport"/><Button label="Refresh roll-up" icon="pi pi-refresh" size="small" outlined @click="refreshRollup"/></div>
      <input ref="rcmImportInput" type="file" accept=".xlsx,.xls,.csv,.tsv" hidden @change="importRcm"/>
      <RcmGrid :rows="data.rcm" :dataTests="data.data_tests" :documentTests="data.document_tests" :findingRollups="data.finding_rollups" :generating="generatingTests" :canGenerate="!isActive && Boolean(agent.state.status?.configured)" @add="addRcm" @update="updateRcm" @remove="removeRcm" @open="openRcm" @generate="generatePlannedTests"/>
    </section>

    <Dialog v-model:visible="detailOpen" modal :header="selectedRcm ? `${selectedRcm.id} · RCM detail` : 'RCM detail'" :style="{ width: 'min(1120px, 97vw)' }" :contentStyle="{ maxHeight: '82vh', overflow: 'auto' }">
      <div v-if="selectedRcm" class="rcm-detail">
        <div class="rcm-fields"><label>Process<InputText v-model="selectedRcm.process"/></label><label>Risk rating<Select v-model="selectedRcm.risk_rating" :options="['low','medium','high','critical']"/></label><label class="wide">Risk<Textarea v-model="selectedRcm.risk" rows="2" autoResize/></label><label class="wide">Control<Textarea v-model="selectedRcm.control" rows="2" autoResize/></label><label>Assertion<InputText v-model="selectedRcm.assertion"/></label><label>Control type<InputText v-model="selectedRcm.control_type"/></label><label>Control owner<InputText v-model="selectedRcm.control_owner"/></label><label>Review status<Select v-model="selectedRcm.review_status" :options="reviewStatuses"/></label><label class="wide">Criteria<Textarea v-model="selectedRcm.criteria" rows="2" autoResize/></label></div>
        <div class="detail-actions"><Button label="Save RCM row" icon="pi pi-save" size="small" outlined @click="saveRcmDetail"/><Button label="RCM working paper" icon="pi pi-file" size="small" outlined @click="openWorkingPaper"/><Button label="Add Data Test" icon="pi pi-chart-bar" size="small" outlined @click="createTest('data')"/><Button label="Add Document Test" icon="pi pi-file-check" size="small" @click="createTest('document')"/></div>
        <section class="planned-list"><article v-for="item in linkedTests(selectedRcm)" :key="item.test_id" class="planned-card">
          <div class="planned-head"><div><strong>{{ item.test_id }}</strong><Tag :value="item.kind === 'datatest' ? 'data' : 'document'" severity="secondary"/><UiTestStatus :status="item.status" /></div><span>{{ item.exception_count }} exception(s) · {{ item.open_exception_count }} open</span></div>
          <p class="planned-title">{{ item.title }}</p>
          <p class="muted">{{ item.result_summary || 'Not executed yet.' }}</p>
          <p v-if="item.scope_limitations" class="muted">Limitation: {{ item.scope_limitations }}</p>
          <div class="card-actions"><Button label="Open test" icon="pi pi-arrow-up-right" size="small" outlined @click="openTest(item)"/></div>
        </article><p v-if="!linkedTests(selectedRcm).length" class="empty">This RCM row has no linked test and cannot pass coverage.</p></section>
        <section v-if="selectedObservations.length" class="observations"><strong>Observation triage</strong><div v-for="item in selectedObservations" :key="item.id"><Tag :value="item.status" :severity="item.status === 'open' ? 'warn' : 'success'"/><span>{{ item.summary }}</span><small>Suggested: {{ item.suggested_disposition }}</small><Select v-model="item.disposition" :options="observationDispositions" placeholder="Auditor disposition"/><Textarea v-model="item.auditor_note" rows="2" placeholder="Auditor note"/><span class="observation-actions"><Button label="Save disposition" size="small" :disabled="!item.disposition" @click="saveObservation(item)"/><Button label="Draft finding" icon="pi pi-sparkles" size="small" severity="secondary" :disabled="!canDraftFinding(item)" title="Save an eligible auditor disposition before drafting with the assistant" @click="promoteObservation(item)"/></span></div></section>
      </div>
    </Dialog>
    <Dialog v-model:visible="templateOpen" modal header="APM template" :style="{ width: 'min(900px, 94vw)' }"><p class="muted">Workspace override · placeholders use <code v-pre>{{name}}</code>.</p><Textarea v-if="template" v-model="template.markdown" class="template-editor" rows="22" spellcheck="false"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
    <Dialog v-model:visible="paperOpen" modal header="RCM working paper" :style="{ width: 'min(980px, 95vw)' }"><div v-if="workingPaper" class="working-paper" v-html="workingPaper.html"/><template #footer><Button label="Copy Markdown" icon="pi pi-copy" text @click="copyPaper('markdown')"/><Button label="Copy HTML" icon="pi pi-copy" @click="copyPaper('html')"/></template></Dialog>
  </div>
</template>

<style scoped>
.planning-tab { display:flex; flex-direction:column; gap:1rem; min-height:100% }.muted { color:var(--aw-muted); font-size:.78rem }.section-toolbar,.rollup-bar,.detail-actions,.card-actions { display:flex; align-items:center; gap:.55rem }.section-toolbar>div { display:flex; flex-direction:column }.section-toolbar>span,.rollup-bar>span { flex:1 }.rollup-bar { padding:.6rem .8rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas); color:var(--aw-muted); font-size:.78rem }.apm-editor { min-height:34rem }.apm-editor>:deep(.markdown-editor) { min-height:34rem }.template-editor { width:100%; font-family:var(--aw-font-mono); font-size:.8rem }.rcm-detail { display:flex; flex-direction:column; gap:1rem }.rcm-fields,.planned-fields,.outcome { display:grid; grid-template-columns:1fr 1fr; gap:.7rem }.wide { grid-column:1/-1 }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.planned-list { display:flex; flex-direction:column; gap:.8rem }.planned-card { padding:.85rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-md); background:#fff }.planned-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; margin-bottom:.7rem }.planned-head>div { display:flex; align-items:center; gap:.5rem }.planned-head>span { color:var(--aw-muted); font-size:.75rem }.execution-cards { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin:.75rem 0; padding:.65rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas) }.execution-cards>strong { width:100% }.execution-cards button:not(.p-button) { border:1px solid var(--aw-border); background:#fff; border-radius:999px; padding:.3rem .55rem; color:var(--aw-teal); cursor:pointer }.execution-cards i { margin-right:.3rem }.card-actions { justify-content:flex-end; margin-top:.7rem }.observations { display:flex; flex-direction:column; gap:.75rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:6px }.observations>div { display:grid; grid-template-columns:auto minmax(0,1fr); gap:.4rem .5rem; align-items:center; padding-bottom:.65rem; border-bottom:1px solid var(--aw-border) }.observations>div:last-child { border-bottom:0 }.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions { grid-column:2; color:var(--aw-muted) }.observation-actions { display:flex; flex-wrap:wrap; gap:.4rem }.empty { padding:1rem; color:var(--aw-muted); border:1px dashed var(--aw-border); border-radius:6px }.working-paper { max-width:52rem; margin:auto; line-height:1.6 }@media(max-width:800px){.rcm-fields,.planned-fields,.outcome{grid-template-columns:1fr}.wide{grid-column:auto}.detail-actions{flex-wrap:wrap}.observations>div{grid-template-columns:1fr}.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions{grid-column:1}}
</style>
