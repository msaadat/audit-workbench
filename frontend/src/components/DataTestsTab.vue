<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  AuditFinding,
  DataTest,
  DataTestEngine,
  DataTestResult,
  DataTestStep,
  PlanningPayload,
  WorkspaceSummary,
} from '../types'
import AnalyticsTestAuthor from './data-tests/AnalyticsTestAuthor.vue'
import DataTestCreateDialog from './data-tests/DataTestCreateDialog.vue'
import DataTestList from './data-tests/DataTestList.vue'
import DataTestResultPanel from './data-tests/DataTestResultPanel.vue'
import PolarsStepEditor from './data-tests/PolarsStepEditor.vue'
import { emptyPolarsStep, polarsStepsValid } from './data-tests/steps'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiTriageCounts from './ui/UiTriageCounts.vue'
import type { TriageCount } from './ui/UiTriageCounts.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const nav = useWorkspaceNav()
const toast = useToast()
const confirm = useConfirm()
const assistantChat = useAssistantChat(props.workspace.id)
const agent = useAgentRun(props.workspace.id)

const tests = ref<DataTest[]>([])
const planning = ref<PlanningPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.test || '') || null)
// Captured before the first selection rewrites the query.
const createRequested = Boolean(route.query.create)
const requestedRcmId = String(route.query.rcm || '')
const requestedEngine: DataTestEngine = String(route.query.create) === 'polars' ? 'polars' : 'analytics'
const result = ref<DataTestResult | null>(null)
const createOpen = ref(false)
const creating = ref(false)
const saving = ref(false)
const running = ref(false)
const runningAll = ref(false)
const filter = ref<string>('all')
const search = ref('')
const editAnalyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const editAnalyticsReady = ref(false)
const editPolarsSteps = ref<DataTestStep[]>([])

const controlConclusions = [
  { label: 'No conclusion recorded', value: 'no_conclusion' },
  { label: 'Control effective', value: 'effective' },
  { label: 'Control partially effective', value: 'partially_effective' },
  { label: 'Control ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]
const selected = computed(() => tests.value.find(item => item.id === selectedId.value) ?? null)
const rcmRows = computed(() => planning.value?.rcm ?? [])
const rcmOptions = computed(() => rcmRows.value.map(row => ({ label: `${row.id} · ${row.risk}`, value: row.id })))
const tableOptions = computed(() => props.workspace.tables.map(item => ({ label: item.name, value: item.name })))

// The cards are the only filter. "Needs attention" was a second control over
// the same axis and was just the union of the three cards beside it.
const triage = computed<TriageCount[]>(() => {
  const count = (predicate: (test: DataTest) => boolean) => tests.value.filter(predicate).length
  return [
    { key: 'all', label: 'All tests', value: tests.value.length },
    { key: 'completed_with_exception', label: 'Exceptions', value: count(test => test.status === 'completed_with_exception'), tone: 'danger' },
    { key: 'review_required', label: 'Need review', value: count(test => test.status === 'review_required'), tone: 'warn' },
    { key: 'blocked', label: 'Blocked', value: count(test => test.status === 'blocked'), tone: 'warn' },
    { key: 'completed_no_exception', label: 'No exception', value: count(test => test.status === 'completed_no_exception'), tone: 'ok' },
    { key: 'not_run', label: 'Not run', value: count(test => !test.last_run) },
  ]
})
// Only offer a facet that can actually split the list. With one engine and two
// statuses, three always-on dropdowns filtered nothing.
const rcmFacet = computed(() => {
  const linked = new Set(tests.value.map(test => test.rcm_id).filter(Boolean))
  return linked.size > 1 ? rcmOptions.value.filter(option => linked.has(option.value)) : []
})
const filterRcm = ref<string | null>(null)
const visibleTests = computed(() => {
  const query = search.value.trim().toLowerCase()
  return tests.value.filter(test => {
    if (filterRcm.value && test.rcm_id !== filterRcm.value) return false
    if (filter.value === 'not_run' && test.last_run) return false
    if (filter.value !== 'all' && filter.value !== 'not_run' && test.status !== filter.value) return false
    if (!query) return true
    return [test.title, test.objective, test.criteria, test.rcm_id ?? '']
      .some(value => (value ?? '').toLowerCase().includes(query))
  })
})
const activeFilterLabel = computed(() =>
  triage.value.find(count => count.key === filter.value)?.label.toLowerCase() ?? 'tests')
const definitionReady = computed(() => {
  if (!selected.value) return false
  if (selected.value.engine === 'polars') return polarsStepsValid(editPolarsSteps.value)
  if (!selected.value.table_refs[0]) return false
  if (selected.value.engine === 'analytics') return editAnalyticsReady.value
  return false
})
const linkedFindings = computed<AuditFinding[]>(() => {
  const testId = selected.value?.id
  return testId ? (planning.value?.findings ?? []).filter(finding => finding.test_refs.includes(testId)) : []
})

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function load() {
  const [testPayload, planningPayload] = await Promise.all([
    api.get<{ items: DataTest[] }>(`/api/workspaces/${props.workspace.id}/data-tests`),
    api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`),
  ])
  tests.value = testPayload.items
  planning.value = planningPayload
  const target = tests.value.find(item => item.id === selectedId.value) ?? visibleTests.value[0] ?? tests.value[0]
  if (target) selectTest(target)
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
  void nav.replace('data-tests', { test: item.id })
  if (item.last_run) void loadResult(item, item.last_run.id)
}
function editSpec(): Record<string, unknown> {
  if (selected.value?.engine === 'analytics') return editAnalyticsSpec.value
  return { schema_version: 2, steps: editPolarsSteps.value }
}
async function loadResult(item: DataTest, runId: string) {
  try {
    result.value = await api.get(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}/runs/${runId}`)
  } catch (error) { fail('Could not open the stored result', error) }
}

async function createTest(payload: {
  title: string; objective: string; engine: DataTestEngine
  rcm_id: string; table_refs: string[]; spec: Record<string, unknown>
}) {
  creating.value = true
  try {
    const item = await api.post<DataTest>(`/api/workspaces/${props.workspace.id}/data-tests`, payload)
    createOpen.value = false
    // Creating a definition nobody ran was the commonest dead end, so the
    // create action runs it too.
    result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}/run`)
    selectedId.value = item.id
    await load()
    emit('changed')
  } catch (error) { fail('Could not create the data test', error) }
  finally { creating.value = false }
}

async function save(thenRun: boolean) {
  if (!selected.value) return
  saving.value = true
  const id = selected.value.id
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/data-tests/${id}`, {
      title: selected.value.title,
      objective: selected.value.objective,
      criteria: selected.value.criteria,
      table_refs: selected.value.table_refs,
      spec: editSpec(),
      rcm_id: selected.value.rcm_id,
    })
    if (thenRun) {
      running.value = true
      result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${id}/run`)
    }
    await load()
    emit('changed')
    toast.add({
      severity: 'success',
      summary: thenRun ? 'Saved and run' : 'Definition saved',
      detail: thenRun ? undefined : 'Run it to produce a result.',
      life: 2000,
    })
  } catch (error) { fail(thenRun ? 'Could not run the data test' : 'Could not save the data test', error) }
  finally { saving.value = false; running.value = false }
}
async function saveConclusion() {
  if (!selected.value) return
  saving.value = true
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}`, {
      conclusion: selected.value.conclusion,
      control_conclusion: selected.value.control_conclusion,
    })
    await load()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Conclusion saved', life: 1800 })
  } catch (error) { fail('Could not save the conclusion', error) }
  finally { saving.value = false }
}
async function runTest() {
  if (!selected.value) return
  running.value = true
  try {
    result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/run`)
    await load()
    emit('changed')
  } catch (error) { fail('Could not run the data test', error) }
  finally { running.value = false }
}
async function runAllTests() {
  runningAll.value = true
  try {
    const batch = await api.post<{
      total: number
      completed: Array<{ data_test_id: string; status: string; exception_count: number }>
      failed: Array<{ data_test_id: string; error: string }>
    }>(`/api/workspaces/${props.workspace.id}/data-tests/run-all`)
    await load()
    emit('changed')
    toast.add({
      severity: batch.failed.length ? 'warn' : 'success',
      summary: `Ran ${batch.completed.length} of ${batch.total} Data Test${batch.total === 1 ? '' : 's'}`,
      detail: batch.failed.length
        ? `${batch.failed.length} test(s) could not run: ${batch.failed.map(item => item.data_test_id).join(', ')}`
        : undefined,
      life: 6000,
    })
  } catch (error) { fail('Could not run all data tests', error) }
  finally { runningAll.value = false }
}
function deleteTest() {
  const item = selected.value
  if (!item) return
  confirm.require({
    header: 'Delete data test',
    message: `Delete "${item.title}" and its stored results? This cannot be undone.`,
    icon: 'pi pi-trash',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}`)
        selectedId.value = null
        result.value = null
        await nav.replace('data-tests')
        await load()
        emit('changed')
        toast.add({ severity: 'success', summary: 'Data test deleted', life: 1800 })
      } catch (error) { fail('Could not delete the data test', error) }
    },
  })
}
async function pin() {
  if (!selected.value?.last_run) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/pin`, {})
    toast.add({ severity: 'success', summary: 'Pinned to the dashboard', life: 1800 })
    emit('changed')
  } catch (error) { fail('Could not pin the result', error) }
}
async function draftFinding(regenerate = false) {
  if (!selected.value?.rcm_id || !selected.value.last_run) return
  try {
    await assistantChat.send(
      `${regenerate ? 'Regenerate' : 'Draft'} findings for RCM row ${selected.value.rcm_id}.`,
      'act', 'permission',
      { command: 'draft_findings', goalTemplate: 'finding_draft', source: 'tab_button', runContext: { rcm_id: selected.value.rcm_id } },
    )
    toast.add({ severity: 'success', summary: regenerate ? 'Finding regeneration started' : 'Finding-draft workflow started', detail: 'Exception observations will be used directly.', life: 3600 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
function openFinding(findingId: string) {
  void nav.replace('findings', { finding: findingId })
}
function openRcm() {
  if (!selected.value?.rcm_id) return
  void nav.replace('rcm', { rcm: selected.value.rcm_id })
}
function pickFilter(key: string) {
  filter.value = key
}

watch(visibleTests, items => {
  if (!items.length || items.some(item => item.id === selectedId.value)) return
  selectTest(items[0])
})

onMounted(() => {
  if (createRequested) createOpen.value = true
  void load().catch(error => fail('Could not load data tests', error))
})
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void load().catch(error => fail('Could not refresh data tests', error))
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="data-tests">
    <UiPageHeader title="Data tests">
      <Button label="Run all" icon="pi pi-play" size="small" outlined :loading="runningAll" :disabled="running || runningAll" @click="runAllTests" />
      <Button label="New test" icon="pi pi-plus" size="small" @click="createOpen = true" />
      <Button
        v-if="selected"
        icon="pi pi-trash"
        severity="danger"
        outlined
        rounded
        size="small"
        aria-label="Delete data test"
        @click="deleteTest"
      />
    </UiPageHeader>

    <template v-if="tests.length">
      <UiTriageCounts :counts="triage" :active="filter" @select="pickFilter" />

      <div class="toolbar">
        <IconField>
          <InputIcon class="pi pi-search" />
          <InputText v-model="search" size="small" placeholder="Search titles and objectives" />
        </IconField>
        <Select
          v-if="rcmFacet.length"
          v-model="filterRcm"
          :options="rcmFacet"
          optionLabel="label"
          optionValue="value"
          placeholder="All RCM rows"
          size="small"
          showClear
          class="rcm-facet"
        />
        <span class="muted">{{ visibleTests.length }} of {{ tests.length }} · {{ activeFilterLabel }}</span>
      </div>

      <UiMasterDetail railWidth="20rem" class="layout">
        <template #rail>
          <DataTestList :tests="visibleTests" :selectedId="selectedId" :rcmRows="rcmRows" @select="selectTest" />
        </template>

        <section v-if="selected" class="detail">
          <header class="detail-head">
            <div class="head-copy">
              <p class="eyebrow">{{ selected.id }}</p>
              <h3>{{ selected.title }}</h3>
              <p class="objective">{{ selected.objective }}</p>
            </div>
            <div class="head-actions">
              <Button label="Run" icon="pi pi-play" size="small" :loading="running" :disabled="runningAll" @click="runTest" />
              <Button v-if="selected.rcm_id" label="Open RCM" icon="pi pi-map" size="small" outlined @click="openRcm" />
              <Button label="Pin" icon="pi pi-thumbtack" size="small" outlined :disabled="!selected.last_run" @click="pin" />
            </div>
          </header>

          <DataTestResultPanel :test="selected" :result="result" />

          <div class="sign-off">
            <label>
              Control conclusion
              <Select
                v-model="selected.control_conclusion"
                :options="controlConclusions"
                optionLabel="label"
                optionValue="value"
              />
            </label>
            <label class="conclusion-note">
              Conclusion
              <Textarea v-model="selected.conclusion" rows="2" autoResize placeholder="What this result means for the control, in your own words." />
            </label>
            <Button label="Save conclusion" icon="pi pi-check" size="small" outlined :loading="saving" @click="saveConclusion" />
          </div>
          <section class="finding-action" aria-label="Finding">
            <template v-if="linkedFindings.length">
              <div>
                <strong>Finding{{ linkedFindings.length === 1 ? '' : 's' }}</strong>
                <p>{{ linkedFindings.length === 1 ? 'A finding is linked to this test.' : `${linkedFindings.length} findings are linked to this test.` }}</p>
              </div>
              <div class="finding-actions">
                <Button
                  v-for="finding in linkedFindings"
                  :key="finding.id"
                  :label="`Open ${finding.id}`"
                  icon="pi pi-arrow-up-right"
                  size="small"
                  outlined
                  @click="openFinding(finding.id)"
                />
                <Button label="Regenerate" icon="pi pi-refresh" size="small" severity="secondary" :disabled="!selected.rcm_id || !selected.last_run" @click="draftFinding(true)" />
              </div>
            </template>
            <template v-else>
              <div><strong>Finding</strong><p>Generate a draft from this test’s exception observations.</p></div>
              <Button label="Generate finding" icon="pi pi-sparkles" size="small" :disabled="!selected.rcm_id || !selected.last_run" @click="() => draftFinding()" />
            </template>
          </section>

          <!-- Authoring is the rarer action, so it is here and closed. -->
          <UiAdvancedSection
            title="Definition"
            description="What this test runs, and the plan it belongs to"
            :open="!selected.last_run"
          >
            <div class="definition">
              <div class="plan">
                <label>Title<InputText v-model="selected.title" /></label>
                <label>
                  Risk and control
                  <Select
                    v-model="selected.rcm_id"
                    :options="rcmOptions"
                    optionLabel="label"
                    optionValue="value"
                    filter
                    showClear
                    placeholder="Exploratory"
                  />
                </label>
                <label v-if="selected.engine !== 'polars'">
                  Table
                  <Select
                    v-model="selected.table_refs[0]"
                    :options="tableOptions"
                    optionLabel="label"
                    optionValue="value"
                    filter
                  />
                </label>
                <label class="wide">Objective<Textarea v-model="selected.objective" rows="2" autoResize /></label>
                <label class="wide">Criteria<Textarea v-model="selected.criteria" rows="2" autoResize /></label>
              </div>

              <AnalyticsTestAuthor
                v-if="selected.engine === 'analytics'"
                :key="selected.id"
                v-model="editAnalyticsSpec"
                :workspace="workspace"
                :table="selected.table_refs[0] || null"
                @valid="editAnalyticsReady = $event"
                @error="fail"
              />
              <PolarsStepEditor v-else-if="selected.engine === 'polars'" v-model="editPolarsSteps" />
              <p v-else class="muted">This draft has no executable definition yet.</p>

              <div class="save-row">
                <Button label="Save only" size="small" text :loading="saving" :disabled="!definitionReady" @click="save(false)" />
                <Button
                  label="Save and run"
                  icon="pi pi-play"
                  size="small"
                  :loading="saving || running"
                  :disabled="!definitionReady"
                  @click="save(true)"
                />
              </div>
            </div>
          </UiAdvancedSection>
        </section>
        <UiEmptyState
          v-else
          icon="pi pi-shield"
          title="No data test selected"
          description="Pick a test from the list, or create one."
        />
      </UiMasterDetail>
    </template>

    <UiEmptyState
      v-else
      icon="pi pi-shield"
      title="Run analytics over the imported data"
      description="Pick an analytic from the library, or write Polars code, and link it to an RCM row so the result counts as coverage."
    >
      <Button label="New test" icon="pi pi-plus" @click="createOpen = true" />
    </UiEmptyState>

    <DataTestCreateDialog
      v-model="createOpen"
      :workspace="workspace"
      :planning="planning"
      :initialRcmId="requestedRcmId"
      :initialEngine="requestedEngine"
      :saving="creating"
      @create="createTest"
      @error="fail"
    />
  </div>
</template>

<style scoped>
.data-tests { display: flex; flex-direction: column; gap: var(--aw-section-gap); min-width: 0; max-width: 100%; min-height: 100%; }
.toolbar { display: flex; align-items: center; gap: 0.6rem; min-width: 0; flex-wrap: wrap; }
.toolbar :deep(.p-iconfield) { flex: 1 1 14rem; min-width: 0; max-width: 22rem; }
.toolbar :deep(.p-inputtext) { width: 100%; }
.rcm-facet { flex: 0 1 14rem; min-width: 0; }
.muted { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.layout { min-height: 32rem; }

.detail { display: flex; flex-direction: column; gap: 0.8rem; min-width: 0; max-width: 100%; padding: 1rem; }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; min-width: 0; }
.head-copy { min-width: 0; }
.eyebrow { margin: 0; }
.detail-head h3 { margin: 0.15rem 0 0.25rem; font-size: var(--aw-text-lg); line-height: 1.3; }
.objective { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.45; }
.head-actions { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; }

.sign-off { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 0.6rem; }
.sign-off label { flex: 0 1 16rem; }
.sign-off label.conclusion-note { flex: 1 1 20rem; }
.finding-action { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; padding: 0.75rem 0.8rem; border-radius: var(--aw-radius-control); background: var(--aw-info-soft); }
.finding-action strong { font-size: var(--aw-text-sm); }
.finding-action p { margin: 0.15rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.finding-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.4rem; }

.definition { display: flex; flex-direction: column; gap: 0.85rem; min-width: 0; }
.plan { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.7rem; }
.wide { grid-column: 1 / -1; }
.save-row { display: flex; align-items: center; justify-content: flex-end; gap: 0.5rem; }

label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; }

@container master-detail-content (max-width: 34rem) {
  .detail-head { flex-direction: column; }
  .head-actions { justify-content: flex-start; }
  .finding-action { align-items: flex-start; flex-direction: column; }
  .plan { grid-template-columns: minmax(0, 1fr); }
  .wide { grid-column: auto; }
}
</style>
