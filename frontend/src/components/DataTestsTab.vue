<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { DataTest, DataTestEngine, DataTestResult, PlanningPayload, PlannedTest, RcmRow, WorkspaceSummary } from '../types'
import FrameTable from './FrameTable.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const router = useRouter()
const toast = useToast()

const tests = ref<DataTest[]>([])
const planning = ref<PlanningPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.test || '') || null)
const result = ref<DataTestResult | null>(null)
const createOpen = ref(false)
const saving = ref(false)
const running = ref(false)
const filterRcm = ref<string | null>(null)
const filterStatus = ref<string | null>(null)
const filterEngine = ref<string | null>(null)
const specText = ref('')
const draft = ref({
  title: '', objective: '', engine: 'analytics' as DataTestEngine,
  rcm_id: '', planned_test_id: '', table_refs: [] as string[],
})

const engines = [
  { label: 'Library analytics', value: 'analytics' },
  { label: 'Validation', value: 'validation' },
  { label: 'Polars code', value: 'polars' },
]
const selected = computed(() => tests.value.find(item => item.id === selectedId.value) ?? null)
const rcmOptions = computed(() => (planning.value?.rcm ?? []).map(row => ({ label: `${row.id} · ${row.risk}`, value: row.id })))
const plannedOptions = computed(() => {
  const row = planning.value?.rcm.find(item => item.id === draft.value.rcm_id)
  return (row?.planned_tests ?? [])
    .filter(item => ['data_analytics', 'validation', 'hybrid'].includes(item.method))
    .map(item => ({ label: `${item.id} · ${item.title}`, value: item.id }))
})
const selectedPlannedOptions = computed(() => {
  const row = planning.value?.rcm.find(item => item.id === selected.value?.rcm_id)
  return (row?.planned_tests ?? [])
    .filter(item => ['data_analytics', 'validation', 'hybrid'].includes(item.method))
    .map(item => ({ label: `${item.id} · ${item.title}`, value: item.id }))
})
const tableOptions = computed(() => props.workspace.tables.map(item => ({ label: item.name, value: item.name })))
const filtered = computed(() => tests.value.filter(item =>
  (!filterRcm.value || item.rcm_id === filterRcm.value)
  && (!filterStatus.value || item.status === filterStatus.value)
  && (!filterEngine.value || item.engine === filterEngine.value),
))
const statuses = computed(() => [...new Set(tests.value.map(item => item.status))])
const createReady = computed(() => {
  const linked = Boolean(draft.value.rcm_id) === Boolean(draft.value.planned_test_id)
  return linked && Boolean(
    draft.value.title.trim()
    && draft.value.objective.trim()
    && draft.value.table_refs[0],
  )
})
const selectedLinkValid = computed(() => !selected.value
  || Boolean(selected.value.rcm_id) === Boolean(selected.value.planned_test_id))

function severity(value: string) {
  return value.includes('exception') || value === 'fail' || value === 'error' ? 'danger'
    : value === 'completed_no_exception' || value === 'ok' ? 'success'
      : value === 'review_required' || value === 'blocked' || value === 'warn' ? 'warn' : 'secondary'
}
function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
function defaultSpec(engine: DataTestEngine) {
  if (engine === 'analytics') return { test_id: 'duplicates', params: { columns: [] } }
  if (engine === 'validation') return { rules: [{ id: 'RULE-1', column: '', check: 'required', params: {}, severity: 'fail' }] }
  return { code: "result = df.head(100)", result_mode: 'exceptions' }
}
function openCreate(engine: DataTestEngine = 'analytics', row?: RcmRow, planned?: PlannedTest) {
  draft.value = {
    title: '', objective: '', engine,
    rcm_id: row?.id ?? '', planned_test_id: planned?.id ?? '', table_refs: [],
  }
  specText.value = JSON.stringify(defaultSpec(engine), null, 2)
  createOpen.value = true
}
function selectTest(item: DataTest) {
  selectedId.value = item.id
  specText.value = JSON.stringify(item.spec, null, 2)
  result.value = null
  void router.replace({ query: workspaceQuery('data-tests', { test: item.id }) })
  if (item.last_run) void loadResult(item, item.last_run.id)
}
async function load() {
  const [testPayload, planningPayload] = await Promise.all([
    api.get<{ items: DataTest[] }>(`/api/workspaces/${props.workspace.id}/data-tests`),
    api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`),
  ])
  tests.value = testPayload.items
  planning.value = planningPayload
  if (route.query.create === '1' && !createOpen.value) {
    const row = planning.value.rcm.find(item => item.id === String(route.query.rcm || ''))
    const planned = row?.planned_tests.find(item => item.id === String(route.query.planned_test || ''))
    const engine: DataTestEngine = planned?.method === 'validation' ? 'validation' : 'analytics'
    openCreate(engine, row, planned)
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
      spec: JSON.parse(specText.value),
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
      spec: JSON.parse(specText.value),
      auditor_disposition: selected.value.auditor_disposition,
      rcm_id: selected.value.rcm_id,
      planned_test_id: selected.value.planned_test_id,
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
async function loadResult(item: DataTest, runId: string) {
  try { result.value = await api.get(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}/runs/${runId}`) }
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
function openParent() {
  if (!selected.value?.rcm_id || !selected.value.planned_test_id) return
  void router.replace({ query: workspaceQuery('planning', { view: 'rcm', rcm: selected.value.rcm_id, planned_test: selected.value.planned_test_id }) })
}

onMounted(() => void load().catch(error => fail('Could not load Data Tests', error)))
</script>

<template>
  <div class="data-tests">
    <UiPageHeader title="Data Tests" description="Exploratory or RCM-linked analytics, validation, and visible Polars code with durable execution history">
      <Button label="New Data Test" icon="pi pi-plus" size="small" @click="openCreate()" />
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
        <div class="toolbar"><div><strong>{{ selected.id }}</strong><small>{{ selected.engine }} · {{ selected.planned_test_id ? `parent ${selected.planned_test_id}` : 'exploratory' }}</small></div><span/><Button v-if="selected.planned_test_id" label="Open RCM" icon="pi pi-map" outlined size="small" @click="openParent"/><Button label="Pin" icon="pi pi-thumbtack" outlined size="small" :disabled="!selected.last_run" @click="pin"/><Button label="Run" icon="pi pi-play" size="small" :loading="running" @click="runTest"/></div>
        <div class="form-grid"><label>Title<InputText v-model="selected.title" /></label><label>Auditor disposition<Select v-model="selected.auditor_disposition" :options="['pending','follow_up','accepted','invalid_test_or_result','not_applicable']" /></label><label class="wide">Objective<Textarea v-model="selected.objective" rows="2" autoResize /></label><label>Parent RCM (optional)<Select v-model="selected.rcm_id" :options="rcmOptions" optionLabel="label" optionValue="value" filter showClear @change="selected.planned_test_id = null"/></label><label>Planned test<Select v-model="selected.planned_test_id" :options="selectedPlannedOptions" optionLabel="label" optionValue="value" filter showClear :disabled="!selected.rcm_id"/></label></div>
        <label>Specification ({{ selected.engine === 'polars' ? 'visible Polars code and options' : 'JSON' }})<Textarea v-model="specText" rows="12" class="code" spellcheck="false" /></label>
        <div class="save-row"><span class="muted">Saving validates the definition but does not execute it.</span><Button label="Save definition" icon="pi pi-save" outlined :loading="saving" :disabled="!selectedLinkValid" @click="saveTest"/></div>
        <div v-if="selected.runs.length" class="history"><strong>Execution history</strong><button v-for="run in [...selected.runs].reverse()" :key="run.id" @click="loadResult(selected, run.id)"><span>{{ new Date(run.run_at).toLocaleString() }}</span><Tag :value="run.verdict" :severity="severity(run.verdict)"/><small>{{ run.exception_count }} exception(s) · {{ run.result_sha1.slice(0, 10) }}</small></button></div>
        <section v-if="result" class="result">
          <div class="result-head"><strong>Durable result</strong><Tag :value="result.status.replaceAll('_', ' ')" :severity="severity(result.status)"/><span>{{ result.verdict_text }}</span></div>
          <div v-if="result.semantic_issues.length" class="issues"><strong>Semantic review</strong><ul><li v-for="issue in result.semantic_issues" :key="issue">{{ issue }}</li></ul></div>
          <div v-if="result.statistics.length" class="stats"><span v-for="stat in result.statistics" :key="stat.label"><small>{{ stat.label }}</small><strong>{{ stat.value }}</strong></span></div>
          <details v-if="result.exception_frame" open><summary>Exception output ({{ result.exception_count }})</summary><FrameTable :frame="result.exception_frame" scrollHeight="26rem" /></details>
          <details v-if="result.summary_frame"><summary>Summary output</summary><FrameTable :frame="result.summary_frame" scrollHeight="24rem" /></details>
        </section>
      </section>
      <div v-else class="empty-state"><div><span class="empty-state-icon"><i class="pi pi-shield"/></span><h3>Create an exploratory or RCM-linked Data Test</h3></div></div>
    </UiMasterDetail>

    <Dialog v-model:visible="createOpen" modal header="New Data Test" :style="{ width: 'min(760px, 95vw)' }">
      <div class="dialog-form"><label>Authoring mode<Select v-model="draft.engine" :options="engines" optionLabel="label" optionValue="value" @change="specText = JSON.stringify(defaultSpec(draft.engine), null, 2)"/></label><label>Table<Select v-model="draft.table_refs[0]" :options="tableOptions" optionLabel="label" optionValue="value" filter/></label><div class="wide linkage-note"><strong>Audit linkage is optional</strong><span>Leave both fields blank for exploration. Exploratory results do not count as RCM coverage or support formal findings.</span></div><label>Parent RCM<Select v-model="draft.rcm_id" :options="rcmOptions" optionLabel="label" optionValue="value" filter showClear @change="draft.planned_test_id = ''"/></label><label>Planned test<Select v-model="draft.planned_test_id" :options="plannedOptions" optionLabel="label" optionValue="value" filter showClear :disabled="!draft.rcm_id"/></label><label class="wide">Title<InputText v-model="draft.title"/></label><label class="wide">Objective<Textarea v-model="draft.objective" rows="2"/></label><label class="wide">Specification<Textarea v-model="specText" rows="11" class="code" spellcheck="false"/></label></div>
      <template #footer><Button label="Create definition" icon="pi pi-save" :loading="saving" :disabled="!createReady" @click="createTest"/></template>
    </Dialog>
  </div>
</template>

<style scoped>
.data-tests { display:flex; flex-direction:column; gap:.8rem; min-height:100% }.filters { display:flex; gap:.5rem; flex-wrap:wrap }.filters :deep(.p-select) { min-width:12rem }.layout { min-height:34rem }.rail { display:flex; flex-direction:column; gap:.45rem; padding:.6rem }.rail button { border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; padding:.65rem; text-align:left; cursor:pointer }.rail button.active { border-color:var(--aw-teal); box-shadow:inset 3px 0 var(--aw-teal); background:var(--aw-teal-soft) }.rail button span { display:flex; gap:.4rem; justify-content:space-between; align-items:flex-start }.rail small,.toolbar small,.muted { color:var(--aw-muted) }.detail { padding:1rem; min-width:0; display:flex; flex-direction:column; gap:.85rem }.toolbar { display:flex; gap:.5rem; align-items:center; position:sticky; top:0; z-index:2; background:#fff; border-bottom:1px solid var(--aw-border); padding-bottom:.7rem }.toolbar>div { display:flex; flex-direction:column }.toolbar>span { flex:1 }.form-grid,.dialog-form { display:grid; grid-template-columns:1fr 1fr; gap:.7rem }.wide { grid-column:1/-1 }.linkage-note { display:flex; flex-direction:column; gap:.2rem; padding:.65rem .75rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas); font-size:.75rem }.linkage-note span { color:var(--aw-muted) }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.code { font-family:var(--aw-font-mono); font-size:.78rem }.save-row,.result-head { display:flex; align-items:center; justify-content:space-between; gap:.6rem }.history { display:flex; gap:.4rem; flex-wrap:wrap; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm) }.history>strong { width:100% }.history button { display:grid; gap:.2rem; text-align:left; border:1px solid var(--aw-border); background:#fff; border-radius:6px; padding:.5rem; cursor:pointer }.result { display:flex; flex-direction:column; gap:.7rem; border-top:1px solid var(--aw-border); padding-top:.8rem }.issues { padding:.7rem; border:1px solid var(--p-orange-200); background:var(--p-orange-50); border-radius:6px }.issues ul { margin:.35rem 0 0 }.stats { display:flex; flex-wrap:wrap; gap:.5rem }.stats span { display:flex; flex-direction:column; min-width:8rem; padding:.55rem; border:1px solid var(--aw-border); border-radius:6px }.stats small { color:var(--aw-muted) }details { border:1px solid var(--aw-border); border-radius:6px; padding:.6rem }summary { cursor:pointer; font-weight:700; margin-bottom:.5rem }@media(max-width:900px){.form-grid,.dialog-form{grid-template-columns:1fr}.wide{grid-column:auto}.toolbar{flex-wrap:wrap}.layout{min-height:0}}
</style>
