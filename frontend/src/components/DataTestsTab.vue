<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import type { MenuItem } from 'primevue/menuitem'

import { api, ApiError } from '../api'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type {
  DataTest,
  DataTestEngine,
  DataTestResult,
  DataTestStep,
  PlanningPayload,
  RcmRow,
  WorkspaceSummary,
} from '../types'
import CodeEditor from './CodeEditor.vue'
import AnalyticsTestAuthor from './data-tests/AnalyticsTestAuthor.vue'
import FrameTable from './FrameTable.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const router = useRouter()
const toast = useToast()
const assistantChat = useAssistantChat(props.workspace.id)

const tests = ref<DataTest[]>([])
const planning = ref<PlanningPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.test || '') || null)
const result = ref<DataTestResult | null>(null)
const createOpen = ref(false)
const createSession = ref(0)
const createMenu = ref<InstanceType<typeof Menu> | null>(null)
const saving = ref(false)
const running = ref(false)
const deleting = ref(false)
const filterRcm = ref<string | null>(null)
const filterStatus = ref<string | null>(null)
const filterEngine = ref<string | null>(null)
const analyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const analyticsReady = ref(false)
function emptyPolarsStep(): DataTestStep {
  return { label: '', instruction: '', code: 'result = df.head(100)' }
}
function polarsStepsValid(steps: DataTestStep[]): boolean {
  return steps.length > 0 && steps.every(step =>
    step.label.trim() && step.instruction.trim() && step.code.trim())
}
function addPolarsStep(list: DataTestStep[]) { list.push(emptyPolarsStep()) }
function removePolarsStep(list: DataTestStep[], index: number) { list.splice(index, 1) }
const polarsSteps = ref<DataTestStep[]>([emptyPolarsStep()])
const editAnalyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const editAnalyticsReady = ref(false)
const editPolarsSteps = ref<DataTestStep[]>([])
const draft = ref({
  title: '', objective: '', engine: 'analytics' as DataTestEngine,
  rcm_id: '', table_refs: [] as string[],
})

const engines = [
  { label: 'Library analytics', value: 'analytics' },
  { label: 'Polars code', value: 'polars' },
]
const createItems: MenuItem[] = [
  { label: 'Library analytics', icon: 'pi pi-chart-bar', command: () => openCreate('analytics') },
  { label: 'Polars code', icon: 'pi pi-code', command: () => openCreate('polars') },
]
const selected = computed(() => tests.value.find(item => item.id === selectedId.value) ?? null)
const rcmOptions = computed(() => (planning.value?.rcm ?? []).map(row => ({ label: `${row.id} · ${row.risk}`, value: row.id })))
const tableOptions = computed(() => props.workspace.tables.map(item => ({ label: item.name, value: item.name })))
const filtered = computed(() => tests.value.filter(item =>
  (!filterRcm.value || item.rcm_id === filterRcm.value)
  && (!filterStatus.value || item.status === filterStatus.value)
  && (!filterEngine.value || item.engine === filterEngine.value),
))
const statuses = computed(() => [...new Set(tests.value.map(item => item.status))])
const createReady = computed(() => {
  const tableReady = draft.value.engine === 'polars' ? true : Boolean(draft.value.table_refs[0])
  const definitionReady = draft.value.engine === 'analytics' ? analyticsReady.value
    : polarsStepsValid(polarsSteps.value)
  return Boolean(draft.value.title.trim() && draft.value.objective.trim() && tableReady && definitionReady)
})
function severity(value: string) {
  return value.includes('exception') || value === 'fail' || value === 'error' ? 'danger'
    : value === 'completed_no_exception' || value === 'ok' ? 'success'
      : value === 'review_required' || value === 'blocked' || value === 'warn' ? 'warn' : 'secondary'
}
function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
function openCreate(engine: DataTestEngine = 'analytics', row?: RcmRow) {
  const firstTable = props.workspace.tables[0]?.name
  draft.value = {
    title: '', objective: '', engine,
    rcm_id: row?.id ?? '', table_refs: firstTable ? [firstTable] : [],
  }
  analyticsSpec.value = { test_id: '', params: {} }
  analyticsReady.value = false
  polarsSteps.value = [emptyPolarsStep()]
  createSession.value += 1
  createOpen.value = true
}
function createSpec(): Record<string, unknown> {
  if (draft.value.engine === 'analytics') return analyticsSpec.value
  return { schema_version: 2, steps: polarsSteps.value }
}
function applyAnalyticsDefaults(title: string, objective: string) {
  if (!draft.value.title.trim()) draft.value.title = title
  if (!draft.value.objective.trim()) draft.value.objective = objective
}
function selectTest(item: DataTest) {
  selectedId.value = item.id
  const params = item.spec.params
  editAnalyticsSpec.value = {
    test_id: String(item.spec.test_id ?? ''),
    params: params && typeof params === 'object' && !Array.isArray(params)
      ? JSON.parse(JSON.stringify(params)) as Record<string, unknown>
      : {},
  }
  editPolarsSteps.value = Array.isArray(item.spec.steps) && item.spec.steps.length
    ? JSON.parse(JSON.stringify(item.spec.steps)) as DataTestStep[]
    : [emptyPolarsStep()]
  editAnalyticsReady.value = false
  result.value = null
  void router.replace({ query: workspaceQuery('data-tests', { test: item.id }) })
  if (item.last_run) void loadResult(item, item.last_run.id)
}
function editSpec(): Record<string, unknown> {
  if (selected.value?.engine === 'analytics') return editAnalyticsSpec.value
  return { schema_version: 2, steps: editPolarsSteps.value }
}
const selectedDefinitionReady = computed(() => {
  if (!selected.value) return false
  if (selected.value.engine === 'polars') return polarsStepsValid(editPolarsSteps.value)
  if (!selected.value.table_refs[0]) return false
  if (selected.value.engine === 'analytics') return editAnalyticsReady.value
  return false
})
async function load() {
  const [testPayload, planningPayload] = await Promise.all([
    api.get<{ items: DataTest[] }>(`/api/workspaces/${props.workspace.id}/data-tests`),
    api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`),
  ])
  tests.value = testPayload.items
  planning.value = planningPayload
  if (route.query.create && !createOpen.value) {
    const row = planning.value.rcm.find(item => item.id === String(route.query.rcm || ''))
    const requestedEngine = String(route.query.create)
    const engine = ['analytics', 'polars'].includes(requestedEngine)
      ? requestedEngine as DataTestEngine
      : 'analytics'
    openCreate(engine, row)
  }
  const target = tests.value.find(item => item.id === selectedId.value) ?? tests.value[0]
  if (target) selectTest(target)
}
async function createTest() {
  saving.value = true
  try {
    const item = await api.post<DataTest>(`/api/workspaces/${props.workspace.id}/data-tests`, {
      ...draft.value,
      table_refs: draft.value.table_refs,
      spec: createSpec(),
    })
    createOpen.value = false
    await load()
    selectTest(item)
    emit('changed')
  } catch (error) { fail('Could not create Data Test', error) }
  finally { saving.value = false }
}
async function saveTest() {
  if (!selected.value) return
  saving.value = true
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}`, {
      title: selected.value.title,
      objective: selected.value.objective,
      table_refs: selected.value.table_refs,
      spec: editSpec(),
      auditor_disposition: selected.value.auditor_disposition,
      rcm_id: selected.value.rcm_id,
    })
    await load()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Data Test saved; execution is still required', life: 2200 })
  } catch (error) { fail('Could not save Data Test', error) }
  finally { saving.value = false }
}
async function runTest() {
  if (!selected.value) return
  running.value = true
  try {
    result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/run`)
    await load()
    emit('changed')
  } catch (error) { fail('Could not run Data Test', error) }
  finally { running.value = false }
}
async function deleteTest() {
  if (!selected.value) return
  const item = selected.value
  if (!window.confirm(`Delete "${item.title}"? This cannot be undone.`)) return
  deleting.value = true
  try {
    await api.del(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}`)
    selectedId.value = null
    result.value = null
    await router.replace({ query: workspaceQuery('data-tests') })
    await load()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Data Test deleted', life: 1800 })
  } catch (error) { fail('Could not delete Data Test', error) }
  finally { deleting.value = false }
}
async function loadResult(item: DataTest, runId: string) {
  try {
    result.value = await api.get(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}/runs/${runId}`)
  }
  catch (error) { fail('Could not reopen the result', error) }
}
async function pin() {
  if (!selected.value?.last_run) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/pin`, {})
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', life: 1800 })
    emit('changed')
  } catch (error) { fail('Could not pin result', error) }
}
async function draftFinding() {
  if (!selected.value?.rcm_id || !selected.value.last_run) return
  try {
    await assistantChat.send(
      `Draft findings for RCM row ${selected.value.rcm_id}.`,
      'act', 'permission', {
        goalTemplate: 'finding_draft', source: 'tab_button',
        runContext: { rcm_id: selected.value.rcm_id },
      },
    )
    toast.add({ severity: 'success', summary: 'Finding-draft workflow started', detail: 'The assistant will request an auditor disposition if one is needed.', life: 3600 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
function openParent() {
  if (!selected.value?.rcm_id) return
  void router.replace({ query: workspaceQuery('rcm', { rcm: selected.value.rcm_id }) })
}

onMounted(() => void load().catch(error => fail('Could not load Data Tests', error)))
</script>

<template>
  <div class="data-tests">
    <UiPageHeader title="Data Tests" description="Exploratory or RCM-linked analytics and visible Polars code with the current execution result">
      <Button
        label="New Data Test"
        icon="pi pi-plus"
        iconPos="left"
        size="small"
        aria-haspopup="true"
        @click="createMenu?.toggle($event)"
      />
      <Menu ref="createMenu" :model="createItems" popup />
    </UiPageHeader>
    <div class="filters">
      <Select v-model="filterRcm" :options="rcmOptions" optionLabel="label" optionValue="value" placeholder="All RCM rows" showClear />
      <Select v-model="filterEngine" :options="engines" optionLabel="label" optionValue="value" placeholder="All engines" showClear />
      <Select v-model="filterStatus" :options="statuses" placeholder="All statuses" showClear />
    </div>
    <UiMasterDetail railWidth="18rem" class="layout">
      <template #rail><aside class="rail">
        <button v-for="item in filtered" :key="item.id" :class="{ active: item.id === selectedId }" @click="selectTest(item)">
          <span><strong>{{ item.title }}</strong><Tag :value="item.status.replaceAll('_', ' ')" :severity="severity(item.status)" /></span>
          <small>{{ item.engine }} · {{ item.rcm_id || 'Exploratory' }}</small>
        </button>
        <p v-if="!filtered.length" class="muted">No Data Tests match these filters.</p>
      </aside></template>
      <section v-if="selected" class="detail">
        <div class="toolbar"><div><strong>{{ selected.id }}</strong><small>{{ selected.engine ?? 'unspecified' }} · {{ selected.rcm_id ? `RCM ${selected.rcm_id}` : 'exploratory' }}</small></div><span/><Button v-if="selected.rcm_id" label="Open RCM" icon="pi pi-map" outlined size="small" @click="openParent"/><Button v-if="selected.rcm_id" label="Draft finding" icon="pi pi-sparkles" outlined size="small" :disabled="!selected.last_run" title="Run the linked Data Test first, then draft through the assistant" @click="draftFinding"/><Button label="Pin" icon="pi pi-thumbtack" outlined size="small" :disabled="!selected.last_run" @click="pin"/><Button label="Run" icon="pi pi-play" size="small" :loading="running" @click="runTest"/><Button icon="pi pi-trash" severity="danger" outlined rounded aria-label="Delete Data Test" :loading="deleting" @click="deleteTest"/></div>
        <div class="form-grid"><label>Title<InputText v-model="selected.title" /></label><label>Auditor disposition<Select v-model="selected.auditor_disposition" :options="['pending','follow_up','accepted','invalid_test_or_result','not_applicable']" /></label><label class="wide">Objective<Textarea v-model="selected.objective" rows="2" autoResize /></label><label v-if="selected.engine !== 'polars'">Table<Select v-model="selected.table_refs[0]" :options="tableOptions" optionLabel="label" optionValue="value" filter/></label><label>RCM row (optional)<Select v-model="selected.rcm_id" :options="rcmOptions" optionLabel="label" optionValue="value" filter showClear/></label><label class="wide">Criteria<Textarea v-model="selected.criteria" rows="2" autoResize /></label></div>
        <AnalyticsTestAuthor
          v-if="selected.engine === 'analytics'"
          :key="selected.id"
          v-model="editAnalyticsSpec"
          :workspace="workspace"
          :table="selected.table_refs[0] || null"
          @valid="editAnalyticsReady = $event"
          @error="fail"
        />
        <div v-else-if="selected.engine === 'polars'" class="steps-author">
          <div class="steps-header"><strong>Steps</strong><Button label="Add step" icon="pi pi-plus" text size="small" @click="addPolarsStep(editPolarsSteps)"/></div>
          <div v-for="(step, index) in editPolarsSteps" :key="index" class="step-card">
            <div class="step-card-head"><span>Step {{ index + 1 }}</span><Button icon="pi pi-trash" text rounded severity="danger" :disabled="editPolarsSteps.length <= 1" aria-label="Remove step" @click="removePolarsStep(editPolarsSteps, index)"/></div>
            <label>Label<InputText v-model="step.label" /></label>
            <label>Instruction<Textarea v-model="step.instruction" rows="2" autoResize /></label>
            <small class="muted">All workspace tables and joins are available to this code.</small>
            <label>Code — each step's result rows are its exceptions<CodeEditor v-model="step.code" /></label>
          </div>
        </div>
        <div v-else class="definition-unavailable">
          This draft does not have an executable test definition yet.
        </div>
        <div class="save-row"><span class="muted">Saving validates the definition but does not execute it.</span><Button label="Save definition" icon="pi pi-save" outlined :loading="saving" :disabled="!selectedDefinitionReady" @click="saveTest"/></div>
        <section v-if="result" class="result">
          <div class="result-head"><strong>Durable result</strong><Tag :value="result.status.replaceAll('_', ' ')" :severity="severity(result.status)"/><span>{{ result.verdict_text }}</span></div>
          <div v-if="result.semantic_issues.length" class="issues"><strong>Semantic review</strong><ul><li v-for="issue in result.semantic_issues" :key="issue">{{ issue }}</li></ul></div>
          <div v-if="result.statistics.length" class="stats"><span v-for="stat in result.statistics" :key="stat.label"><small>{{ stat.label }}</small><strong>{{ stat.value }}</strong></span></div>
          <div v-if="result.step_results?.length" class="step-results">
            <strong>Step results</strong>
            <div v-for="step in result.step_results" :key="step.step_id" class="step-result-row">
              <Tag :value="step.status.replaceAll('_',' ')" :severity="severity(step.status)" />
              <span>{{ step.step_label }}</span>
              <small>{{ step.exception_count }} exception(s)</small>
              <small v-if="step.error" class="step-error">{{ step.error }}</small>
            </div>
          </div>
          <details v-if="result.exception_frame" open><summary>Exception output ({{ result.exception_count }})</summary><FrameTable :frame="result.exception_frame" scrollHeight="26rem" /></details>
          <details v-if="result.summary_frame"><summary>Summary output</summary><FrameTable :frame="result.summary_frame" scrollHeight="24rem" /></details>
        </section>
      </section>
      <div v-else class="empty-state"><div><span class="empty-state-icon"><i class="pi pi-shield"/></span><h3>Create an exploratory or RCM-linked Data Test</h3></div></div>
    </UiMasterDetail>

    <Dialog
      v-model:visible="createOpen"
      modal
      :header="`New ${engines.find(item => item.value === draft.engine)?.label ?? 'Data Test'}`"
      :style="{ width: 'min(1100px, 96vw)' }"
      :contentStyle="{ maxHeight: '78vh', overflow: 'auto' }"
    >
      <div class="dialog-form">
        <label v-if="draft.engine !== 'polars'">Table<Select v-model="draft.table_refs[0]" :options="tableOptions" optionLabel="label" optionValue="value" filter/></label>
        <label>RCM row (optional)<Select v-model="draft.rcm_id" :options="rcmOptions" optionLabel="label" optionValue="value" filter showClear/></label>
        <div class="wide linkage-note"><strong>Audit linkage is optional</strong><span>Leave the RCM row blank for exploration. Exploratory results do not count as RCM coverage or support formal findings.</span></div>
        <label class="wide">Title<InputText v-model="draft.title"/></label>
        <label class="wide">Objective<Textarea v-model="draft.objective" rows="2" autoResize/></label>

        <AnalyticsTestAuthor
          v-if="draft.engine === 'analytics'"
          :key="`analytics-${createSession}`"
          v-model="analyticsSpec"
          class="wide"
          :workspace="workspace"
          :table="draft.table_refs[0] || null"
          @valid="analyticsReady = $event"
          @selected="applyAnalyticsDefaults"
          @error="fail"
        />
        <div v-else class="wide steps-author">
          <div class="steps-header"><strong>Steps</strong><Button label="Add step" icon="pi pi-plus" text size="small" @click="addPolarsStep(polarsSteps)"/></div>
          <div v-for="(step, index) in polarsSteps" :key="index" class="step-card">
            <div class="step-card-head"><span>Step {{ index + 1 }}</span><Button icon="pi pi-trash" text rounded severity="danger" :disabled="polarsSteps.length <= 1" aria-label="Remove step" @click="removePolarsStep(polarsSteps, index)"/></div>
            <label>Label<InputText v-model="step.label" /></label>
            <label>Instruction<Textarea v-model="step.instruction" rows="2" autoResize /></label>
            <small class="muted">All workspace tables and joins are available to this code.</small>
            <label>Code — each step's result rows are its exceptions<CodeEditor v-model="step.code" /></label>
          </div>
        </div>
      </div>
      <template #footer><Button label="Create definition" icon="pi pi-save" :loading="saving" :disabled="!createReady" @click="createTest"/></template>
    </Dialog>
  </div>
</template>

<style scoped>
.data-tests { display:flex; flex-direction:column; gap:.8rem; min-width:0; max-width:100%; min-height:100%; overflow-x:hidden }
.filters { display:flex; gap:.5rem; flex-wrap:wrap; min-width:0 }
.filters :deep(.p-select) { flex:1 1 12rem; min-width:min(12rem,100%) }
.layout { width:100%; min-width:0; min-height:34rem }
.rail { display:flex; flex-direction:column; gap:.45rem; min-width:0; padding:.6rem }
.rail button { min-width:0; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; padding:.65rem; text-align:left; cursor:pointer }
.rail button.active { border-color:var(--aw-teal); box-shadow:inset 3px 0 var(--aw-teal); background:var(--aw-teal-soft) }
.rail button span { display:flex; gap:.4rem; justify-content:space-between; align-items:flex-start; min-width:0 }
.rail button strong,.rail small,.toolbar strong,.toolbar small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.rail button strong { min-width:0 }
.rail small,.toolbar small,.muted { color:var(--aw-muted) }
.detail { width:100%; min-width:0; max-width:100%; padding:1rem; display:flex; flex-direction:column; gap:.85rem; overflow-x:hidden }
.toolbar { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; position:sticky; top:0; z-index:2; background:#fff; border-bottom:1px solid var(--aw-border); padding-bottom:.7rem }
.toolbar>div { display:flex; flex:1 1 14rem; min-width:0; flex-direction:column }
.toolbar>span { flex:1 1 auto }
.form-grid,.dialog-form { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:.7rem; min-width:0 }
.wide { grid-column:1/-1 }
.linkage-note { display:flex; flex-direction:column; gap:.2rem; min-width:0; padding:.65rem .75rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas); font-size:.75rem }
.linkage-note span { color:var(--aw-muted) }
.steps-author { display:flex; flex-direction:column; gap:.6rem; min-width:0 }
.steps-header { display:flex; align-items:center; justify-content:space-between; gap:.5rem }
.step-card { display:flex; flex-direction:column; gap:.4rem; min-width:0; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); padding:.7rem; background:var(--aw-canvas) }
.step-card-head { display:flex; align-items:center; justify-content:space-between; font-weight:600; font-size:.75rem; color:var(--aw-muted) }
.step-results { display:flex; flex-direction:column; gap:.4rem; min-width:0 }
.step-result-row { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; min-width:0; padding:.4rem .55rem; border:1px solid var(--aw-border); border-radius:6px }
.step-error { color:var(--p-red-600); overflow-wrap:anywhere }
.definition-unavailable { padding:.8rem; border:1px solid var(--aw-border); border-radius:6px; color:var(--aw-muted); background:var(--aw-canvas) }
label { display:flex; flex-direction:column; gap:.3rem; min-width:0; color:#46576d; font-size:.75rem; font-weight:600 }
label :deep(.p-inputtext),label :deep(.p-textarea),label :deep(.p-select),label :deep(.p-multiselect) { width:100%; min-width:0 }
.code { font-family:var(--aw-font-mono); font-size:.78rem }
.save-row,.result-head { display:flex; align-items:center; justify-content:space-between; gap:.6rem; flex-wrap:wrap; min-width:0 }
.result-head span { min-width:0; overflow-wrap:anywhere }
.result { display:flex; flex-direction:column; gap:.7rem; min-width:0; max-width:100%; border-top:1px solid var(--aw-border); padding-top:.8rem }
.issues { padding:.7rem; border:1px solid var(--p-orange-200); background:var(--p-orange-50); border-radius:6px }
.issues ul { margin:.35rem 0 0 }
.stats { display:flex; flex-wrap:wrap; gap:.5rem; min-width:0 }
.stats span { display:flex; flex:1 1 8rem; flex-direction:column; min-width:0; padding:.55rem; border:1px solid var(--aw-border); border-radius:6px }
.stats small { color:var(--aw-muted) }
details { min-width:0; max-width:100%; overflow-x:auto; border:1px solid var(--aw-border); border-radius:6px; padding:.6rem }
summary { cursor:pointer; font-weight:700; margin-bottom:.5rem }
:deep(.author),:deep(.parameters),:deep(.catalog),:deep(.selected-test),:deep(.results),:deep(.rule-list),:deep(.grid) { min-width:0; max-width:100% }
:deep(.grid),:deep(.rule-list) { overflow-x:auto }
:deep(.p-datatable) { max-width:100% }
@media(max-width:900px){.form-grid,.dialog-form{grid-template-columns:1fr}.wide{grid-column:auto}.layout{min-height:0}}
</style>
