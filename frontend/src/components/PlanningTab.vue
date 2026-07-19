<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { AuditObservation, MarkdownTemplate, PlannedTest, PlannedTestMethod, PlanningPayload, RcmRow, WorkspaceSummary, WorkingPaper } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import RcmGrid from './planning/RcmGrid.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const router = useRouter()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { isActive, launchMode } = agent

const data = ref<PlanningPayload | null>(null)
const view = ref<'apm' | 'rcm'>('apm')
const saving = ref(false)
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)
const selectedRcmId = ref<string | null>(null)
const detailOpen = ref(false)
const paperOpen = ref(false)
const workingPaper = ref<WorkingPaper | null>(null)
const stepDrafts = ref<Record<string, string>>({})

const methods: Array<{ label: string; value: PlannedTestMethod }> = [
  { label: 'Data analytics', value: 'data_analytics' },
  { label: 'Validation / data quality', value: 'validation' },
  { label: 'Document inspection / vouching', value: 'document_inspection' },
  { label: 'Inquiry / walkthrough', value: 'inquiry' },
  { label: 'Hybrid', value: 'hybrid' },
  { label: 'Evidence unavailable', value: 'evidence_unavailable' },
]
const conclusions = ['effective', 'partially_effective', 'ineffective', 'no_conclusion', 'not_applicable']
const reviewStatuses = ['draft', 'prepared', 'review_required', 'reviewed']
const observationDispositions = [
  'confirmed_control_exception', 'data_quality_issue', 'expected_or_benign',
  'screening_follow_up', 'invalid_test_or_result', 'duplicate', 'draft_finding_candidate',
]
const viewOptions = computed(() => [
  { label: 'APM', value: 'apm', complete: Boolean(data.value?.planning.apm_markdown.trim()) },
  { label: 'Risk & Control Matrix', value: 'rcm', count: data.value?.rcm.length ?? 0 },
])
const selectedRcm = computed(() => data.value?.rcm.find(item => item.id === selectedRcmId.value) ?? null)
const selectedObservations = computed(() => (data.value?.observations ?? []).filter(item => item.rcm_id === selectedRcmId.value))

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
function initializeSteps(row: RcmRow) {
  stepDrafts.value = Object.fromEntries(row.planned_tests.map(item => [item.id, item.steps.join('\n')]))
}
async function reload(rollup = false) {
  if (rollup) await api.post(`/api/workspaces/${props.workspace.id}/rcm/rollup`)
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
  const requestedView = String(route.query.view || '')
  if (requestedView === 'rcm') view.value = 'rcm'
  const requestedRcm = String(route.query.rcm || '')
  const requestedPlanned = String(route.query.planned_test || '')
  const requestedObservation = String(route.query.observation || '')
  const plannedParent = requestedPlanned
    ? data.value.rcm.find(row => row.planned_tests.some(item => item.id === requestedPlanned))
    : undefined
  const observationParent = requestedObservation
    ? data.value.rcm.find(row => data.value?.observations.some(item => item.id === requestedObservation && item.rcm_id === row.id))
    : undefined
  if (requestedRcm && data.value.rcm.some(item => item.id === requestedRcm)) openRcm(data.value.rcm.find(item => item.id === requestedRcm)!)
  else if (plannedParent) openRcm(plannedParent)
  else if (observationParent) openRcm(observationParent)
  else if (selectedRcmId.value) {
    const row = data.value.rcm.find(item => item.id === selectedRcmId.value)
    if (row) initializeSteps(row)
  }
}
onMounted(() => void reload(true).catch(error => fail('Could not load planning', error)))
const unsubscribe = agent.onWorkspaceChanged(change => {
  if (['planning', 'rcm', 'planned_test', 'datatest', 'doctest', 'observation', 'evidence_request'].includes(change.kind)) void reload(true)
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
    await assistantChat.send(
      'Update the planning context and APM, then create or reconcile the RCM and its structured planned tests. Do not create a separate audit program.',
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
    await reload(true)
    toast.add({ severity: 'success', summary: 'RCM row saved', life: 1800 })
  } catch (error) { fail('Could not save the RCM row', error) }
}
function removeRcm(id: string) {
  const row = data.value?.rcm.find(item => item.id === id)
  const label = row?.process?.trim() || id
  const plannedCount = row?.planned_tests?.length ?? 0
  const plannedNote = plannedCount
    ? ` Its ${plannedCount} planned test${plannedCount === 1 ? '' : 's'} will also be deleted, and any linked Data/Document Tests and findings will be unlinked.`
    : ' Any linked Data/Document Tests and findings will be unlinked.'
  confirm.require({
    header: 'Remove RCM row',
    message: `Remove "${label}"?${plannedNote}`,
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
  initializeSteps(current)
  detailOpen.value = true
  void router.replace({ query: workspaceQuery('planning', { view: 'rcm', rcm: current.id }) })
}
async function addPlannedTest() {
  if (!selectedRcm.value) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/rcm/${selectedRcm.value.id}/planned-tests`, {
      title: 'New planned test', objective: 'Describe the test objective.', method: 'data_analytics', steps: [],
    })
    await reload(true)
  } catch (error) { fail('Could not add the planned test', error) }
}
async function savePlannedTest(item: PlannedTest) {
  if (!selectedRcm.value) return
  try {
    item.steps = (stepDrafts.value[item.id] ?? '').split('\n').map(value => value.trim()).filter(Boolean)
    await api.patch(`/api/workspaces/${props.workspace.id}/rcm/${selectedRcm.value.id}/planned-tests/${item.id}`, {
      title: item.title, objective: item.objective, criteria: item.criteria, method: item.method,
      steps: item.steps, expected_evidence: item.expected_evidence,
      conclusion: item.conclusion, control_conclusion: item.control_conclusion,
      scope_limitations: item.scope_limitations,
      next_action: item.next_action,
    })
    await reload(true)
    emit('changed')
    toast.add({ severity: 'success', summary: 'Planned test saved', life: 1800 })
  } catch (error) { fail('Could not save the planned test', error) }
}
async function saveObservation(item: AuditObservation) {
  if (!item.disposition) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/observations/${item.id}`, {
      disposition: item.disposition, auditor_note: item.auditor_note,
    })
    await reload(true)
    emit('changed')
  } catch (error) { fail('Could not disposition the observation', error) }
}
async function promoteObservation(item: AuditObservation) {
  try {
    const finding = await api.post<{ id: string }>(`/api/workspaces/${props.workspace.id}/findings`, {
      title: item.summary.slice(0, 120) || `Observation ${item.id}`,
      condition: item.summary, rcm_refs: [item.rcm_id],
      planned_test_refs: [item.planned_test_id], execution_refs: [item.execution_ref],
      auditor_confirmed: false,
    })
    emit('changed')
    void router.replace({ query: { tab: 'findings', finding: finding.id } })
  } catch (error) { fail('Could not create a draft finding', error) }
}
async function removePlannedTest(item: PlannedTest) {
  if (!selectedRcm.value) return
  try { await api.del(`/api/workspaces/${props.workspace.id}/rcm/${selectedRcm.value.id}/planned-tests/${item.id}`); await reload(true); emit('changed') }
  catch (error) { fail('Could not remove the planned test', error) }
}
function openExecution(ref: string) {
  const [kind, id] = ref.split(':', 2)
  void router.replace({ query: workspaceQuery(kind === 'datatest' ? 'data-tests' : 'doc-tests', { test: id }) })
}
function createExecution(item: PlannedTest, kind: 'data' | 'document') {
  if (!selectedRcm.value) return
  void router.replace({ query: { tab: kind === 'data' ? 'data-tests' : 'doc-tests', create: '1', rcm: selectedRcm.value.id, planned_test: item.id } })
}
async function refreshRollup() {
  try { await reload(true); emit('changed'); toast.add({ severity: 'success', summary: 'Execution roll-up refreshed', life: 1800 }) }
  catch (error) { fail('Could not refresh the roll-up', error) }
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
    <UiPageHeader title="Planning" description="The APM defines engagement context; the RCM is the single planning and fieldwork spine">
      <Button label="Generate planning drafts" icon="pi pi-sparkles" size="small" :disabled="isActive || !agent.state.status?.configured" @click="generate" />
    </UiPageHeader>
    <SelectButton class="planning-nav" v-model="view" :options="viewOptions" optionLabel="label" optionValue="value" :allowEmpty="false" dataKey="value">
      <template #option="{ option }"><span class="nav-option">{{ option.label }}<span v-if="option.count !== undefined" class="nav-count">{{ option.count }}</span><i v-else-if="option.complete" class="pi pi-check nav-check"/></span></template>
    </SelectButton>
    <section v-if="view === 'apm'" class="apm-view">
      <div class="section-toolbar"><div><strong>Audit Planning Memorandum</strong><span class="muted">{{ data.planning.created_by === 'agent' ? 'Agent draft' : 'Auditor edited' }}</span></div><span/><Button label="Template" icon="pi pi-file-edit" size="small" outlined @click="openTemplate"/><Button label="Save APM" icon="pi pi-save" size="small" :loading="saving" @click="savePlanning"/></div>
      <div class="apm-editor"><MarkdownEditor v-model="data.planning.apm_markdown"/></div>
    </section>
    <section v-else>
      <div class="rollup-bar"><span>Execution status is computed from linked durable Data and Document Test results.</span><Button label="Refresh roll-up" icon="pi pi-refresh" size="small" outlined @click="refreshRollup"/></div>
      <RcmGrid :rows="data.rcm" :dataTests="data.data_tests" :documentTests="data.document_tests" :findingRollups="data.finding_rollups" @add="addRcm" @update="updateRcm" @remove="removeRcm" @open="openRcm"/>
    </section>

    <Dialog v-model:visible="detailOpen" modal :header="selectedRcm ? `${selectedRcm.id} · RCM detail` : 'RCM detail'" :style="{ width: 'min(1120px, 97vw)' }" :contentStyle="{ maxHeight: '82vh', overflow: 'auto' }">
      <div v-if="selectedRcm" class="rcm-detail">
        <div class="rcm-fields"><label>Process<InputText v-model="selectedRcm.process"/></label><label>Risk rating<Select v-model="selectedRcm.risk_rating" :options="['low','medium','high','critical']"/></label><label class="wide">Risk<Textarea v-model="selectedRcm.risk" rows="2" autoResize/></label><label class="wide">Control<Textarea v-model="selectedRcm.control" rows="2" autoResize/></label><label>Assertion<InputText v-model="selectedRcm.assertion"/></label><label>Control type<InputText v-model="selectedRcm.control_type"/></label><label>Control owner<InputText v-model="selectedRcm.control_owner"/></label><label>Review status<Select v-model="selectedRcm.review_status" :options="reviewStatuses"/></label><label class="wide">Criteria<Textarea v-model="selectedRcm.criteria" rows="2" autoResize/></label></div>
        <div class="detail-actions"><Button label="Save RCM row" icon="pi pi-save" size="small" outlined @click="saveRcmDetail"/><Button label="RCM working paper" icon="pi pi-file" size="small" outlined @click="openWorkingPaper"/><Button label="Add planned test" icon="pi pi-plus" size="small" @click="addPlannedTest"/></div>
        <section class="planned-list"><article v-for="item in selectedRcm.planned_tests" :key="item.id" class="planned-card">
          <div class="planned-head"><div><strong>{{ item.id }}</strong><Tag :value="item.status.replaceAll('_',' ')" :severity="item.status.includes('exception') || item.status === 'blocked' ? 'danger' : item.status === 'completed_no_exception' ? 'success' : item.status === 'review_required' ? 'warn' : 'secondary'"/></div><span>{{ item.exception_count }} exception(s) · {{ item.open_exception_count }} open</span></div>
          <div class="planned-fields"><label>Title<InputText v-model="item.title"/></label><label>Method<Select v-model="item.method" :options="methods" optionLabel="label" optionValue="value"/></label><label class="wide">Objective<Textarea v-model="item.objective" rows="2" autoResize/></label><label class="wide">Criteria<Textarea v-model="item.criteria" rows="2" autoResize/></label><label class="wide">Steps — one per line<Textarea v-model="stepDrafts[item.id]" rows="4" autoResize/></label><label class="wide">Expected evidence<Textarea v-model="item.expected_evidence" rows="2" autoResize/></label></div>
          <div class="execution-cards"><strong>Linked execution</strong><button v-for="ref in item.execution_refs" :key="ref" @click="openExecution(ref)"><i :class="ref.startsWith('datatest:') ? 'pi pi-chart-bar' : 'pi pi-file-check'"/>{{ ref }}</button><span v-if="!item.execution_refs.length" class="muted">No execution artifact linked.</span><Button v-if="['data_analytics','validation','hybrid'].includes(item.method)" label="Create Data Test" icon="pi pi-plus" size="small" text @click="createExecution(item, 'data')"/><Button v-if="['document_inspection','inquiry','hybrid','evidence_unavailable'].includes(item.method)" label="Create Document Test" icon="pi pi-plus" size="small" text @click="createExecution(item, 'document')"/></div>
          <div class="outcome"><label>Result summary<Textarea v-model="item.result_summary" rows="2" disabled/></label><label>Control conclusion<Select v-model="item.control_conclusion" :options="conclusions"/></label><label class="wide">Conclusion<Textarea v-model="item.conclusion" rows="2" autoResize/></label><label class="wide">Scope limitations<Textarea v-model="item.scope_limitations" rows="2" autoResize/></label><label class="wide">Next action<Textarea v-model="item.next_action" rows="2" autoResize/></label></div>
          <div class="card-actions"><Button label="Save planned test" icon="pi pi-save" size="small" @click="savePlannedTest(item)"/><Button icon="pi pi-trash" severity="danger" text size="small" @click="removePlannedTest(item)"/></div>
        </article><p v-if="!selectedRcm.planned_tests.length" class="empty">This RCM row has no planned tests and cannot pass coverage.</p></section>
        <section v-if="selectedObservations.length" class="observations"><strong>Observation triage</strong><div v-for="item in selectedObservations" :key="item.id"><Tag :value="item.status" :severity="item.status === 'open' ? 'warn' : 'success'"/><span>{{ item.summary }}</span><small>Suggested: {{ item.suggested_disposition }}</small><Select v-model="item.disposition" :options="observationDispositions" placeholder="Auditor disposition"/><Textarea v-model="item.auditor_note" rows="2" placeholder="Auditor note"/><span class="observation-actions"><Button label="Save disposition" size="small" :disabled="!item.disposition" @click="saveObservation(item)"/><Button label="Draft finding" icon="pi pi-arrow-right" size="small" severity="secondary" @click="promoteObservation(item)"/></span></div></section>
      </div>
    </Dialog>
    <Dialog v-model:visible="templateOpen" modal header="APM template" :style="{ width: 'min(900px, 94vw)' }"><p class="muted">Workspace override · placeholders use <code v-pre>{{name}}</code>.</p><Textarea v-if="template" v-model="template.markdown" class="template-editor" rows="22" spellcheck="false"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
    <Dialog v-model:visible="paperOpen" modal header="RCM working paper" :style="{ width: 'min(980px, 95vw)' }"><div v-if="workingPaper" class="working-paper" v-html="workingPaper.html"/><template #footer><Button label="Copy Markdown" icon="pi pi-copy" text @click="copyPaper('markdown')"/><Button label="Copy HTML" icon="pi pi-copy" @click="copyPaper('html')"/></template></Dialog>
  </div>
</template>

<style scoped>
.planning-tab { display:flex; flex-direction:column; gap:1rem; min-height:100% }.planning-nav { align-self:flex-start }.nav-option { display:inline-flex; align-items:center; gap:.4rem }.nav-count { display:inline-grid; place-items:center; min-width:1.25rem; height:1.25rem; border:1px solid var(--aw-border); border-radius:999px; font-size:.68rem }.nav-check { color:var(--aw-ok) }.muted { color:var(--aw-muted); font-size:.78rem }.section-toolbar,.rollup-bar,.detail-actions,.card-actions { display:flex; align-items:center; gap:.55rem }.section-toolbar>div { display:flex; flex-direction:column }.section-toolbar>span,.rollup-bar>span { flex:1 }.rollup-bar { padding:.6rem .8rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas); color:var(--aw-muted); font-size:.78rem }.apm-editor { min-height:34rem }.apm-editor>:deep(.markdown-editor) { min-height:34rem }.template-editor { width:100%; font-family:var(--aw-font-mono); font-size:.8rem }.rcm-detail { display:flex; flex-direction:column; gap:1rem }.rcm-fields,.planned-fields,.outcome { display:grid; grid-template-columns:1fr 1fr; gap:.7rem }.wide { grid-column:1/-1 }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.planned-list { display:flex; flex-direction:column; gap:.8rem }.planned-card { padding:.85rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-md); background:#fff }.planned-head { display:flex; align-items:center; justify-content:space-between; gap:.5rem; margin-bottom:.7rem }.planned-head>div { display:flex; align-items:center; gap:.5rem }.planned-head>span { color:var(--aw-muted); font-size:.75rem }.execution-cards { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem; margin:.75rem 0; padding:.65rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas) }.execution-cards>strong { width:100% }.execution-cards button:not(.p-button) { border:1px solid var(--aw-border); background:#fff; border-radius:999px; padding:.3rem .55rem; color:var(--aw-teal); cursor:pointer }.execution-cards i { margin-right:.3rem }.card-actions { justify-content:flex-end; margin-top:.7rem }.observations { display:flex; flex-direction:column; gap:.75rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:6px }.observations>div { display:grid; grid-template-columns:auto minmax(0,1fr); gap:.4rem .5rem; align-items:center; padding-bottom:.65rem; border-bottom:1px solid var(--aw-border) }.observations>div:last-child { border-bottom:0 }.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions { grid-column:2; color:var(--aw-muted) }.observation-actions { display:flex; flex-wrap:wrap; gap:.4rem }.empty { padding:1rem; color:var(--aw-muted); border:1px dashed var(--aw-border); border-radius:6px }.working-paper { max-width:52rem; margin:auto; line-height:1.6 }@media(max-width:800px){.rcm-fields,.planned-fields,.outcome{grid-template-columns:1fr}.wide{grid-column:auto}.detail-actions{flex-wrap:wrap}.observations>div{grid-template-columns:1fr}.observations small,.observations :deep(.p-select),.observations textarea,.observation-actions{grid-column:1}}
</style>
