<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { AuditProcedure, MarkdownTemplate, PlanningPayload, RcmRow, WorkspaceSummary, WorkingPaper } from '../types'
import MarkdownView from './MarkdownView.vue'
import RcmGrid from './planning/RcmGrid.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const route = useRoute()
const agent = useAgentRun(props.workspace.id)
const { isActive } = agent

const data = ref<PlanningPayload | null>(null)
const view = ref<'context' | 'apm' | 'rcm' | 'program'>('context')
const mode = ref<'auto' | 'permission'>('permission')
const saving = ref(false)
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)
const selectedProcedureId = ref<string | null>(null)
const stepDraft = ref('')
const paperOpen = ref(false)
const workingPaper = ref<WorkingPaper | null>(null)

const viewOptions = [
  { label: 'Context', value: 'context' },
  { label: 'APM', value: 'apm' },
  { label: 'Risk & Control Matrix', value: 'rcm' },
  { label: 'Audit program', value: 'program' },
]
const modeOptions = [{ label: 'Auto', value: 'auto' }, { label: 'Permission', value: 'permission' }]
const selectedProcedure = computed(() => data.value?.procedures.find((item) => item.id === selectedProcedureId.value) ?? null)
function testLabel(ref: string) {
  const item = data.value?.document_tests.find(test => `doctest:${test.id}` === ref)
  if (!item) return ref
  const exceptions = item.state_counts?.exception ?? 0
  return `${item.title} · ${exceptions ? `${exceptions} exception(s)` : item.status.replace('_', ' ')}`
}

async function reload() {
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
  const requested = String(route.query.procedure || '')
  if (requested && data.value.procedures.some(item => item.id === requested)) {
    selectedProcedureId.value = requested
    view.value = 'program'
  } else if (!selectedProcedureId.value && data.value.procedures.length) selectedProcedureId.value = data.value.procedures[0].id
  if (['context', 'apm', 'rcm', 'program'].includes(String(route.query.view || ''))) view.value = String(route.query.view) as typeof view.value
  if (selectedProcedure.value) stepDraft.value = selectedProcedure.value.steps.join('\n')
}

onMounted(() => void reload().catch((error) => fail('Could not load planning', error)))
const unsubscribe = agent.onWorkspaceChanged((change) => {
  if (['planning', 'rcm', 'procedure'].includes(change.kind)) void reload()
})
onUnmounted(unsubscribe)

async function savePlanning() {
  if (!data.value) return
  saving.value = true
  try {
    data.value.planning = await api.patch<PlanningPayload['planning']>(`/api/workspaces/${props.workspace.id}/planning`, {
      status: data.value.planning.status,
      context: data.value.planning.context,
      apm_markdown: data.value.planning.apm_markdown,
    })
    toast.add({ severity: 'success', summary: 'Planning saved', life: 1800 })
  } catch (error) { fail('Could not save planning', error) }
  finally { saving.value = false }
}
async function togglePlanningStatus() {
  if (!data.value) return
  data.value.planning.status = data.value.planning.status === 'draft' ? 'final' : 'draft'
  await savePlanning()
}

async function generate(skipInterview: boolean) {
  try {
    await savePlanning()
    await agent.startRun(mode.value, { skip_interview: skipInterview }, 'planning')
  } catch (error) { fail('Could not start planning', error) }
}

async function openTemplate() {
  try {
    template.value = await api.get(`/api/workspaces/${props.workspace.id}/templates/apm`)
    templateOpen.value = true
  } catch (error) { fail('Could not load the template', error) }
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
    await api.post(`/api/workspaces/${props.workspace.id}/rcm`, { process: 'New process', risk: 'Describe the audit risk', risk_rating: 'medium' })
    await reload()
  } catch (error) { fail('Could not add the risk', error) }
}
async function updateRcm(id: string, changes: Partial<RcmRow>) {
  try { await api.patch(`/api/workspaces/${props.workspace.id}/rcm/${id}`, changes) }
  catch (error) { fail('Could not update the risk', error) }
}
async function removeRcm(id: string) {
  try { await api.del(`/api/workspaces/${props.workspace.id}/rcm/${id}`); await reload() }
  catch (error) { fail('Could not remove the risk', error) }
}

async function addProcedure() {
  try {
    const item = await api.post<AuditProcedure>(`/api/workspaces/${props.workspace.id}/procedures`, { objective: 'New audit procedure', steps: [] })
    await reload(); selectedProcedureId.value = item.id; stepDraft.value = ''
  } catch (error) { fail('Could not add the procedure', error) }
}
async function saveProcedure() {
  const item = selectedProcedure.value
  if (!item) return
  try {
    item.steps = stepDraft.value.split('\n').map((step) => step.trim()).filter(Boolean)
    await api.patch(`/api/workspaces/${props.workspace.id}/procedures/${item.id}`, {
      rcm_refs: item.rcm_refs, objective: item.objective, criteria: item.criteria,
      steps: item.steps, method: item.method, expected_evidence: item.expected_evidence,
      result_summary: item.result_summary, conclusion: item.conclusion, scope_limitations: item.scope_limitations,
    })
    toast.add({ severity: 'success', summary: 'Procedure saved', life: 1800 })
  } catch (error) { fail('Could not save the procedure', error) }
}
async function removeProcedure() {
  const item = selectedProcedure.value
  if (!item) return
  try { await api.del(`/api/workspaces/${props.workspace.id}/procedures/${item.id}`); selectedProcedureId.value = null; await reload() }
  catch (error) { fail('Could not remove the procedure', error) }
}
function selectProcedure(item: AuditProcedure) { selectedProcedureId.value = item.id; stepDraft.value = item.steps.join('\n') }
function setRcmRefs(value: string | undefined) {
  if (selectedProcedure.value) selectedProcedure.value.rcm_refs = String(value ?? '').split(',').map((entry) => entry.trim()).filter(Boolean)
}
async function openWorkingPaper() {
  if (!selectedProcedure.value) return
  try {
    workingPaper.value = await api.get(`/api/workspaces/${props.workspace.id}/procedures/${selectedProcedure.value.id}/working-paper`)
    paperOpen.value = true
  } catch (error) { fail('Could not render the working paper', error) }
}
async function draftProcedureResults() {
  if (!selectedProcedure.value) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/procedures/${selectedProcedure.value.id}/draft-results`)
    await reload()
    await openWorkingPaper()
  } catch (error) { fail('Could not draft procedure results', error) }
}
async function copyWorkingPaper(kind: 'markdown' | 'html') {
  if (workingPaper.value) await navigator.clipboard.writeText(workingPaper.value[kind])
}
function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
</script>

<template>
  <div v-if="data" class="planning-tab">
    <div class="planning-head">
      <div><p class="eyebrow">Engagement planning</p><h2>Planning and audit program</h2><p class="muted">Build the planning basis, risk assessment, and linked fieldwork procedures.</p></div>
      <div class="head-actions">
        <Tag :value="data.planning.status" :severity="data.planning.status === 'final' ? 'success' : 'warn'" />
        <Button :label="data.planning.status === 'final' ? 'Reopen draft' : 'Mark final'" size="small" text severity="secondary" @click="togglePlanningStatus" />
        <span v-if="data.planning.updated" class="muted">Updated {{ data.planning.updated.slice(0, 16).replace('T', ' ') }}</span>
        <SelectButton v-model="mode" :options="modeOptions" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" />
        <Button label="Start interview" icon="pi pi-comments" size="small" outlined :disabled="isActive || !agent.state.status?.configured" @click="generate(false)" />
        <Button label="Generate planning drafts" icon="pi pi-sparkles" size="small" :disabled="isActive || !agent.state.status?.configured" @click="generate(true)" />
      </div>
    </div>
    <SelectButton class="planning-nav" v-model="view" :options="viewOptions" optionLabel="label" optionValue="value" :allowEmpty="false" />

    <section v-if="view === 'context'" class="context-layout">
      <div class="card form-grid">
        <label>Entity<InputText v-model="data.planning.context.entity" /></label>
        <label>Period<InputText v-model="data.planning.context.period" /></label>
        <label class="wide">Objective<Textarea v-model="data.planning.context.objective" rows="2" autoResize /></label>
        <label class="wide">Scope<Textarea v-model="data.planning.context.scope" rows="3" autoResize /></label>
        <label>Materiality<InputText v-model="data.planning.context.materiality" /></label>
        <label>Key contacts<InputText v-model="data.planning.context.key_contacts" /></label>
        <label class="wide">Background notes<Textarea v-model="data.planning.context.background_notes" rows="4" autoResize /></label>
        <div class="wide form-actions"><Button label="Save context" icon="pi pi-save" :loading="saving" @click="savePlanning" /></div>
      </div>
      <aside class="card basis"><h3>Planning basis</h3><p><strong>{{ workspace.tables.length }}</strong> structured source(s)</p><p><strong>{{ workspace.document_count ?? 0 }}</strong> document(s)</p><p>Structured rows remain local. {{ workspace.settings?.doc_llm_optin ? 'Logged methodology/document disclosures are enabled for this engagement.' : 'Document and methodology disclosure is off for this engagement.' }}</p><h4>Interview answers</h4><p v-if="!Object.keys(data.planning.context.interview_answers).length" class="muted">No interview answers captured yet.</p><div v-else class="interview-answers"><label v-for="(_answer, question) in data.planning.context.interview_answers" :key="question">{{ question }}<Textarea v-model="data.planning.context.interview_answers[question]" rows="2" autoResize /></label><Button label="Save answers" icon="pi pi-save" size="small" outlined :loading="saving" @click="savePlanning" /></div></aside>
    </section>

    <section v-else-if="view === 'apm'" class="apm-view">
      <div class="section-toolbar"><div><strong>Audit Planning Memorandum</strong><span class="muted">{{ data.planning.created_by === 'agent' ? 'Agent draft' : 'Auditor edited' }}</span></div><Button label="Template" icon="pi pi-file-edit" size="small" outlined @click="openTemplate" /><Button label="Save APM" icon="pi pi-save" size="small" :loading="saving" @click="savePlanning" /></div>
      <div class="split-editor"><Textarea v-model="data.planning.apm_markdown" spellcheck="false" /><div class="preview card"><MarkdownView :markdown="data.planning.apm_markdown || '*No APM draft yet.*'" /></div></div>
    </section>

    <RcmGrid v-else-if="view === 'rcm'" :rows="data.rcm" :documentTests="data.document_tests" :findingRollups="data.finding_rollups" @add="addRcm" @update="updateRcm" @remove="removeRcm" />

    <section v-else class="program-layout">
      <aside class="procedure-rail card"><div class="rail-head"><strong>Procedures</strong><Button icon="pi pi-plus" text size="small" @click="addProcedure" /></div><button v-for="item in data.procedures" :key="item.id" :class="{ active: item.id === selectedProcedureId }" @click="selectProcedure(item)"><strong>{{ item.id }}</strong><span>{{ item.objective }}</span><small>{{ item.rcm_refs.length }} RCM · {{ item.test_refs.length }} result link(s)</small></button><p v-if="!data.procedures.length" class="muted">No procedures yet.</p></aside>
      <div v-if="selectedProcedure" class="procedure-detail card"><div class="section-toolbar"><strong>{{ selectedProcedure.id }}</strong><span class="grow"/><Tag v-if="selectedProcedure.test_refs.length" :value="`${selectedProcedure.test_refs.length} linked test(s)`" severity="info"/><Button label="Working paper" icon="pi pi-file" size="small" outlined @click="openWorkingPaper"/><Button icon="pi pi-trash" severity="danger" text @click="removeProcedure"/><Button label="Save procedure" icon="pi pi-save" size="small" @click="saveProcedure"/></div><label>Objective<Textarea v-model="selectedProcedure.objective" rows="2" autoResize /></label><label>Criteria<Textarea v-model="selectedProcedure.criteria" rows="2" autoResize /></label><label>Steps — one per line<Textarea v-model="stepDraft" rows="7" autoResize /></label><div class="two"><label>Method<InputText v-model="selectedProcedure.method" /></label><label>RCM IDs (comma separated)<InputText :modelValue="selectedProcedure.rcm_refs.join(', ')" @update:modelValue="setRcmRefs" /></label></div><label>Expected evidence<Textarea v-model="selectedProcedure.expected_evidence" rows="2" autoResize /></label><div v-if="selectedProcedure.test_refs.length || data.finding_rollups.by_procedure[selectedProcedure.id]?.length" class="test-links"><small>Execution results</small><a v-for="ref in selectedProcedure.test_refs" :key="ref" :href="`?tab=doc-tests&test=${ref.replace('doctest:','')}`">{{ testLabel(ref) }}</a><a v-for="finding in data.finding_rollups.by_procedure[selectedProcedure.id] ?? []" :key="finding.id" :href="`?tab=findings&finding=${finding.id}`">{{ finding.id }} · {{ finding.severity }}</a></div><div v-if="selectedProcedure.methodology_refs?.length" class="methodology-citations"><small>Methodology basis</small><span v-for="ref in selectedProcedure.methodology_refs" :key="`${ref.pack_id}:${ref.section}`"><i class="pi pi-book" /> {{ ref.citation }} · {{ ref.sha1.slice(0, 10) }}</span></div><label>Result summary<Textarea v-model="selectedProcedure.result_summary" rows="2" autoResize /></label><label>Conclusion<Textarea v-model="selectedProcedure.conclusion" rows="2" autoResize /></label><label>Scope limitations<Textarea v-model="selectedProcedure.scope_limitations" rows="2" autoResize /></label></div>
      <div v-else class="empty card">Select or add a procedure.</div>
    </section>

    <Dialog v-model:visible="templateOpen" modal header="APM template" :style="{ width: 'min(900px, 94vw)' }"><p class="muted">Workspace override · placeholders use <code v-pre>{{name}}</code>.</p><Textarea v-if="template" v-model="template.markdown" class="template-editor" rows="22" spellcheck="false"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
    <Dialog v-model:visible="paperOpen" modal header="Working-paper draft" :style="{ width: 'min(980px, 95vw)' }"><div v-if="workingPaper" class="working-paper" v-html="workingPaper.html"/><template #footer><Button label="Draft result & conclusion" icon="pi pi-sparkles" outlined @click="draftProcedureResults"/><Button label="Copy Markdown" icon="pi pi-copy" text @click="copyWorkingPaper('markdown')"/><Button label="Copy HTML" icon="pi pi-copy" @click="copyWorkingPaper('html')"/></template></Dialog>
  </div>
</template>

<style scoped>
.planning-tab { display: flex; flex-direction: column; gap: 1rem; min-height: 100%; }
.planning-head { display: flex; justify-content: space-between; gap: 1rem; align-items: flex-start; }
.planning-head h2 { margin: 0.1rem 0; }
.planning-head p { margin: 0; }
.head-actions { display: flex; align-items: center; justify-content: flex-end; gap: 0.5rem; flex-wrap: wrap; }
.muted { color: var(--aw-muted); font-size: 0.78rem; }
.planning-nav { align-self: flex-start; }
.card { background: #fff; border: 1px solid var(--aw-border); border-radius: var(--aw-radius); box-shadow: var(--aw-shadow-sm); padding: 1rem; }
.context-layout { display: grid; grid-template-columns: minmax(0, 2fr) minmax(16rem, 1fr); gap: 1rem; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; }
label { display: flex; flex-direction: column; gap: 0.3rem; color: #46576d; font-size: 0.75rem; font-weight: 600; }
.wide { grid-column: 1 / -1; }
.form-actions { display: flex; justify-content: flex-end; }
.basis h3 { margin-top: 0; }.basis h4 { margin-bottom: 0.4rem; }.interview-answers { display: flex; flex-direction: column; gap: 0.7rem; }
.section-toolbar { display: flex; align-items: center; gap: 0.55rem; margin-bottom: 0.7rem; }.section-toolbar > div { display: flex; flex-direction: column; }.grow { flex: 1; }
.split-editor { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; min-height: 34rem; }.split-editor > :deep(textarea) { width: 100%; height: 100%; resize: vertical; font-family: var(--aw-font-mono); font-size: 0.8rem; }.preview { overflow-y: auto; }
.program-layout { display: grid; grid-template-columns: 17rem minmax(0, 1fr); gap: 1rem; }.procedure-rail { padding: 0.55rem; }.rail-head { display: flex; align-items: center; justify-content: space-between; padding: 0.2rem 0.4rem 0.55rem; }.procedure-rail button { width: 100%; display: grid; grid-template-columns: auto 1fr; gap: 0.15rem 0.45rem; text-align: left; border: 0; border-radius: 6px; background: transparent; padding: 0.55rem; cursor: pointer; }.procedure-rail button:hover, .procedure-rail button.active { background: var(--p-primary-50); }.procedure-rail button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.procedure-rail button small { grid-column: 2; color: var(--aw-muted); }.procedure-detail { display: flex; flex-direction: column; gap: 0.75rem; }.two { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; }.empty { color: var(--aw-muted); }
.template-editor { width: 100%; font-family: var(--aw-font-mono); font-size: 0.8rem; }
.methodology-citations { display: grid; gap: .35rem; padding: .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: var(--aw-canvas); }.methodology-citations small { color: var(--aw-muted); font-weight: 700; text-transform: uppercase; letter-spacing: .06em; }.methodology-citations span { color: var(--aw-ink); font-size: var(--aw-text-sm); }
.test-links { display:flex; gap:.35rem; align-items:center; flex-wrap:wrap; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:var(--aw-canvas) }.test-links small { margin-right:.3rem; text-transform:uppercase; color:var(--aw-muted); font-weight:700 }.test-links a { color:var(--aw-teal); border:1px solid var(--aw-border); border-radius:999px; padding:.2rem .5rem; text-decoration:none; font-size:.75rem }.working-paper { max-width:52rem; margin:auto; line-height:1.6 }
@media (max-width: 1050px) { .planning-head { flex-direction: column; }.context-layout, .split-editor, .program-layout { grid-template-columns: 1fr; }.procedure-rail { max-height: 15rem; overflow-y: auto; } }
</style>
