<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import type { MenuItem } from 'primevue/menuitem'

import { api, ApiError } from '../api'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type {
  DataTest,
  DataTestEngine,
  DataTestResult,
  FramePayload,
  PlanningPayload,
  RcmRow,
  ValidationRule,
  ValidationRun,
  WorkspaceSummary,
} from '../types'
import CodeEditor from './CodeEditor.vue'
import AnalyticsTestAuthor from './data-tests/AnalyticsTestAuthor.vue'
import ValidationTestAuthor from './data-tests/ValidationTestAuthor.vue'
import FrameTable from './FrameTable.vue'
import RunResults from './validation/RunResults.vue'
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
const validationRules = ref<ValidationRule[]>([])
const validationReady = ref(false)
const polarsCode = ref('result = df.head(100)')
const polarsResultMode = ref<'exceptions' | 'summary'>('exceptions')
const editAnalyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const editAnalyticsReady = ref(false)
const editValidationRules = ref<ValidationRule[]>([])
const editValidationReady = ref(false)
const editPolarsCode = ref('')
const editPolarsResultMode = ref<'exceptions' | 'summary'>('exceptions')
const validationView = ref<'rules' | 'results'>('rules')
const draft = ref({
  title: '', objective: '', engine: 'analytics' as DataTestEngine,
  rcm_id: '', table_refs: [] as string[],
})

const engines = [
  { label: 'Library analytics', value: 'analytics' },
  { label: 'Validation', value: 'validation' },
  { label: 'Polars code', value: 'polars' },
]
const createItems: MenuItem[] = [
  { label: 'Library analytics', icon: 'pi pi-chart-bar', command: () => openCreate('analytics') },
  { label: 'Validation rules', icon: 'pi pi-check-square', command: () => openCreate('validation') },
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
const validationViewOptions = computed(() => [
  { label: 'Rules', value: 'rules', disabled: false },
  { label: 'Results', value: 'results', disabled: !validationRun.value },
])
const createReady = computed(() => {
  return Boolean(
    draft.value.title.trim()
    && draft.value.objective.trim()
    && draft.value.table_refs[0]
    && (
      (draft.value.engine === 'analytics' && analyticsReady.value)
      || (draft.value.engine === 'validation' && validationReady.value)
      || (draft.value.engine === 'polars' && polarsCode.value.trim())
    )
  )
})
function frameRecords(frame: FramePayload | null): Record<string, string | number | boolean | null>[] {
  if (!frame) return []
  return frame.rows.map(row => Object.fromEntries(frame.columns.map((column, index) => [column, row[index] ?? null])))
}
function textValue(value: unknown, fallback = ''): string {
  return value === null || value === undefined ? fallback : String(value)
}
function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}
function nullableNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
function validationVerdict(value: unknown): 'ok' | 'warn' | 'fail' | 'info' {
  return value === 'ok' || value === 'warn' || value === 'info' ? value : 'fail'
}
function ruleVerdict(value: unknown): 'ok' | 'warn' | 'fail' | 'error' | 'skipped' {
  return value === 'ok' || value === 'warn' || value === 'fail' || value === 'error' || value === 'skipped' ? value : 'error'
}
function ruleSeverity(value: unknown): 'fail' | 'warn' {
  return value === 'warn' ? 'warn' : 'fail'
}
const validationRunRules = computed(() => {
  if (selected.value?.engine !== 'validation' || !Array.isArray(selected.value.spec.rules)) return []
  return selected.value.spec.rules as ValidationRule[]
})
const validationRun = computed<ValidationRun | null>(() => {
  if (selected.value?.engine !== 'validation' || !result.value?.summary_frame) return null
  const rules = validationRunRules.value
  const results = frameRecords(result.value.summary_frame).map((row, index) => {
    const rule = rules[index]
    const field = textValue(row.field)
    return {
      rule_id: rule?.id ?? null,
      column: field === '(table)' || !field ? null : field,
      check: rule?.check ?? '',
      label: textValue(row.rule, rule?.check ?? 'Rule'),
      severity: ruleSeverity(row.severity),
      verdict: ruleVerdict(row.verdict),
      fail_count: numberValue(row.failing_rows),
      checked_rows: numberValue(row.rows_checked),
      pass_pct: nullableNumber(row.pass_pct),
      error: row.error ? String(row.error) : null,
    }
  })
  const counts = {
    passed: results.filter(item => item.verdict === 'ok').length,
    warned: results.filter(item => item.verdict === 'warn').length,
    failed: results.filter(item => item.verdict === 'fail').length,
    errored: results.filter(item => item.verdict === 'error').length,
    skipped: results.filter(item => item.verdict === 'skipped').length,
  }
  return {
    table: Object.keys(result.value.dataset_fingerprints)[0] ?? selected.value.table_refs[0] ?? '',
    rows: results.reduce((max, item) => Math.max(max, item.checked_rows), 0),
    run_at: result.value.run_at,
    verdict: validationVerdict(result.value.verdict),
    counts,
    results,
  }
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
  validationRules.value = []
  validationReady.value = false
  polarsCode.value = 'result = df.head(100)'
  polarsResultMode.value = 'exceptions'
  createSession.value += 1
  createOpen.value = true
}
function createSpec(): Record<string, unknown> {
  if (draft.value.engine === 'analytics') return analyticsSpec.value
  if (draft.value.engine === 'validation') return { rules: validationRules.value }
  return { code: polarsCode.value, result_mode: polarsResultMode.value }
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
  editValidationRules.value = Array.isArray(item.spec.rules)
    ? JSON.parse(JSON.stringify(item.spec.rules)) as ValidationRule[]
    : []
  editPolarsCode.value = String(item.spec.code ?? '')
  editPolarsResultMode.value = item.spec.result_mode === 'summary' ? 'summary' : 'exceptions'
  editAnalyticsReady.value = false
  editValidationReady.value = editValidationRules.value.length > 0
  validationView.value = item.engine === 'validation' && item.last_run ? 'results' : 'rules'
  result.value = null
  void router.replace({ query: workspaceQuery('data-tests', { test: item.id }) })
  if (item.last_run) void loadResult(item, item.last_run.id)
}
function editSpec(): Record<string, unknown> {
  if (selected.value?.engine === 'analytics') return editAnalyticsSpec.value
  if (selected.value?.engine === 'validation') return { rules: editValidationRules.value }
  return { code: editPolarsCode.value, result_mode: editPolarsResultMode.value }
}
const selectedDefinitionReady = computed(() => {
  if (!selected.value?.table_refs[0]) return false
  if (selected.value.engine === 'analytics') return editAnalyticsReady.value
  if (selected.value.engine === 'validation') return editValidationReady.value
  if (selected.value.engine === 'polars') return Boolean(editPolarsCode.value.trim())
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
    const engine = ['analytics', 'validation', 'polars'].includes(requestedEngine)
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
    if (selected.value.engine === 'validation') validationView.value = 'results'
    await load()
    emit('changed')
  } catch (error) { fail('Could not run Data Test', error) }
  finally { running.value = false }
}
async function deleteTest() {
  if (!selected.value) return
  const item = selected.value
  const runText = item.runs.length === 1 ? '1 execution result' : `${item.runs.length} execution results`
  if (!window.confirm(`Delete "${item.title}" and its ${runText}? This cannot be undone.`)) return
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
    if (item.engine === 'validation') validationView.value = 'results'
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
function openParent() {
  if (!selected.value?.rcm_id) return
  void router.replace({ query: workspaceQuery('planning', { view: 'rcm', rcm: selected.value.rcm_id }) })
}
function rebindValidationResult(table: string) {
  if (!selected.value) return
  selected.value.table_refs = [table]
  toast.add({ severity: 'info', summary: `Now bound to ${table}`, detail: 'Save the definition to keep the new binding.', life: 3600 })
}

onMounted(() => void load().catch(error => fail('Could not load Data Tests', error)))
</script>

<template>
  <div class="data-tests">
    <UiPageHeader title="Data Tests" description="Exploratory or RCM-linked analytics, validation, and visible Polars code with durable execution history">
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
        <div class="toolbar"><div><strong>{{ selected.id }}</strong><small>{{ selected.engine ?? 'unspecified' }} · {{ selected.rcm_id ? `RCM ${selected.rcm_id}` : 'exploratory' }}</small></div><span/><Button v-if="selected.rcm_id" label="Open RCM" icon="pi pi-map" outlined size="small" @click="openParent"/><Button label="Pin" icon="pi pi-thumbtack" outlined size="small" :disabled="!selected.last_run" @click="pin"/><Button label="Run" icon="pi pi-play" size="small" :loading="running" @click="runTest"/><Button icon="pi pi-trash" severity="danger" outlined rounded aria-label="Delete Data Test" :loading="deleting" @click="deleteTest"/></div>
        <div class="form-grid"><label>Title<InputText v-model="selected.title" /></label><label>Auditor disposition<Select v-model="selected.auditor_disposition" :options="['pending','follow_up','accepted','invalid_test_or_result','not_applicable']" /></label><label class="wide">Objective<Textarea v-model="selected.objective" rows="2" autoResize /></label><label>Table<Select v-model="selected.table_refs[0]" :options="tableOptions" optionLabel="label" optionValue="value" filter/></label><label>RCM row (optional)<Select v-model="selected.rcm_id" :options="rcmOptions" optionLabel="label" optionValue="value" filter showClear/></label><label class="wide">Criteria<Textarea v-model="selected.criteria" rows="2" autoResize /></label><label class="wide">Expected evidence<Textarea v-model="selected.expected_evidence" rows="2" autoResize /></label></div>
        <AnalyticsTestAuthor
          v-if="selected.engine === 'analytics'"
          :key="selected.id"
          v-model="editAnalyticsSpec"
          :workspace="workspace"
          :table="selected.table_refs[0] || null"
          @valid="editAnalyticsReady = $event"
          @error="fail"
        />
        <section v-else-if="selected.engine === 'validation'" class="validation-detail">
          <div class="validation-tabs">
            <SelectButton
              v-model="validationView"
              :options="validationViewOptions"
              optionLabel="label"
              optionValue="value"
              optionDisabled="disabled"
              :allowEmpty="false"
              size="small"
            />
          </div>
          <ValidationTestAuthor
            v-if="validationView === 'rules'"
            :key="selected.id"
            v-model="editValidationRules"
            :workspace="workspace"
            :table="selected.table_refs[0] || null"
            @valid="editValidationReady = $event"
            @error="fail"
          />
          <RunResults
            v-else-if="validationRun"
            :workspace="workspace"
            :run="validationRun"
            :rules="validationRunRules"
            :boundTable="selected.table_refs[0] || null"
            @rebind="rebindValidationResult"
          />
        </section>
        <div v-else-if="selected.engine === 'polars'" class="code-author">
          <label>Polars code<CodeEditor v-model="editPolarsCode" /></label>
          <label>Result interpretation
            <Select
              v-model="editPolarsResultMode"
              :options="[
                { label: 'Rows are exceptions', value: 'exceptions' },
                { label: 'Rows are a summary', value: 'summary' },
              ]"
              optionLabel="label"
              optionValue="value"
            />
          </label>
        </div>
        <div v-else class="definition-unavailable">
          This draft does not have an executable test definition yet.
        </div>
        <div class="save-row"><span class="muted">Saving validates the definition but does not execute it.</span><Button label="Save definition" icon="pi pi-save" outlined :loading="saving" :disabled="!selectedDefinitionReady" @click="saveTest"/></div>
        <div v-if="selected.runs.length" class="history"><strong>Execution history</strong><button v-for="run in [...selected.runs].reverse()" :key="run.id" @click="loadResult(selected, run.id)"><span>{{ new Date(run.run_at).toLocaleString() }}</span><Tag :value="run.verdict" :severity="severity(run.verdict)"/><small>{{ run.exception_count }} exception(s) · {{ run.result_sha1.slice(0, 10) }}</small></button></div>
        <section v-if="result && selected.engine !== 'validation'" class="result">
          <div class="result-head"><strong>Durable result</strong><Tag :value="result.status.replaceAll('_', ' ')" :severity="severity(result.status)"/><span>{{ result.verdict_text }}</span></div>
          <div v-if="result.semantic_issues.length" class="issues"><strong>Semantic review</strong><ul><li v-for="issue in result.semantic_issues" :key="issue">{{ issue }}</li></ul></div>
          <div v-if="result.statistics.length" class="stats"><span v-for="stat in result.statistics" :key="stat.label"><small>{{ stat.label }}</small><strong>{{ stat.value }}</strong></span></div>
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
        <label>Table<Select v-model="draft.table_refs[0]" :options="tableOptions" optionLabel="label" optionValue="value" filter/></label>
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
        <ValidationTestAuthor
          v-else-if="draft.engine === 'validation'"
          :key="`validation-${createSession}`"
          v-model="validationRules"
          class="wide"
          :workspace="workspace"
          :table="draft.table_refs[0] || null"
          @valid="validationReady = $event"
          @error="fail"
        />
        <div v-else class="wide code-author">
          <label>Polars code<CodeEditor v-model="polarsCode" /></label>
          <label>Result interpretation
            <Select
              v-model="polarsResultMode"
              :options="[
                { label: 'Rows are exceptions', value: 'exceptions' },
                { label: 'Rows are a summary', value: 'summary' },
              ]"
              optionLabel="label"
              optionValue="value"
            />
          </label>
        </div>
      </div>
      <template #footer><Button label="Create definition" icon="pi pi-save" :loading="saving" :disabled="!createReady" @click="createTest"/></template>
    </Dialog>
  </div>
</template>

<style scoped>
.data-tests { display:flex; flex-direction:column; gap:.8rem; min-height:100% }.filters { display:flex; gap:.5rem; flex-wrap:wrap }.filters :deep(.p-select) { min-width:12rem }.layout { min-height:34rem }.rail { display:flex; flex-direction:column; gap:.45rem; padding:.6rem }.rail button { border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; padding:.65rem; text-align:left; cursor:pointer }.rail button.active { border-color:var(--aw-teal); box-shadow:inset 3px 0 var(--aw-teal); background:var(--aw-teal-soft) }.rail button span { display:flex; gap:.4rem; justify-content:space-between; align-items:flex-start }.rail small,.toolbar small,.muted { color:var(--aw-muted) }.detail { padding:1rem; min-width:0; display:flex; flex-direction:column; gap:.85rem }.toolbar { display:flex; gap:.5rem; align-items:center; position:sticky; top:0; z-index:2; background:#fff; border-bottom:1px solid var(--aw-border); padding-bottom:.7rem }.toolbar>div { display:flex; flex-direction:column }.toolbar>span { flex:1 }.form-grid,.dialog-form { display:grid; grid-template-columns:1fr 1fr; gap:.7rem }.wide { grid-column:1/-1 }.linkage-note { display:flex; flex-direction:column; gap:.2rem; padding:.65rem .75rem; border:1px solid var(--aw-border); border-radius:6px; background:var(--aw-canvas); font-size:.75rem }.linkage-note span { color:var(--aw-muted) }.code-author { display:grid; grid-template-columns:minmax(0,1fr) 15rem; gap:.7rem }.code-author>label:first-child { min-width:0 }.definition-unavailable { padding:.8rem; border:1px solid var(--aw-border); border-radius:6px; color:var(--aw-muted); background:var(--aw-canvas) }.validation-detail { display:flex; flex-direction:column; gap:.7rem }.validation-tabs { display:flex; justify-content:flex-end }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.code { font-family:var(--aw-font-mono); font-size:.78rem }.save-row,.result-head { display:flex; align-items:center; justify-content:space-between; gap:.6rem }.history { display:flex; gap:.4rem; flex-wrap:wrap; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm) }.history>strong { width:100% }.history button { display:grid; gap:.2rem; text-align:left; border:1px solid var(--aw-border); background:#fff; border-radius:6px; padding:.5rem; cursor:pointer }.result { display:flex; flex-direction:column; gap:.7rem; border-top:1px solid var(--aw-border); padding-top:.8rem }.issues { padding:.7rem; border:1px solid var(--p-orange-200); background:var(--p-orange-50); border-radius:6px }.issues ul { margin:.35rem 0 0 }.stats { display:flex; flex-wrap:wrap; gap:.5rem }.stats span { display:flex; flex-direction:column; min-width:8rem; padding:.55rem; border:1px solid var(--aw-border); border-radius:6px }.stats small { color:var(--aw-muted) }details { border:1px solid var(--aw-border); border-radius:6px; padding:.6rem }summary { cursor:pointer; font-weight:700; margin-bottom:.5rem }@media(max-width:900px){.form-grid,.dialog-form,.code-author{grid-template-columns:1fr}.wide{grid-column:auto}.toolbar{flex-wrap:wrap}.layout{min-height:0}.validation-tabs{justify-content:flex-start}}
</style>
