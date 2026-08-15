<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { conclusionCounts, dataTestConclusion } from '../composables/conclusionFacet'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  AuditFinding,
  DataTest,
  DataTestEngine,
  DataTestResult,
  DataTestStep,
  PlanningPayload,
  TestConclusionState,
  WorkspaceSummary,
} from '../types'
import AnalyticsTestAuthor from './data-tests/AnalyticsTestAuthor.vue'
import DataTestCreateDialog from './data-tests/DataTestCreateDialog.vue'
import DataTestList from './data-tests/DataTestList.vue'
import DataTestResultPanel from './data-tests/DataTestResultPanel.vue'
import PolarsStepEditor from './data-tests/PolarsStepEditor.vue'
import { emptyPolarsStep, polarsStepsValid } from './data-tests/steps'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiMasterDetail from './ui/UiMasterDetail.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiStatusLanes from './ui/UiStatusLanes.vue'
import type { StatusAction } from './ui/statusLanes'
import {
  DATA_TEST_FILTER_LABELS, dataTestStatus, filterDataTests,
} from './data-tests/dataTestStatus'
import type { DataTestActionKey, DataTestFilter } from './data-tests/dataTestStatus'
import UiTriageCounts from './ui/UiTriageCounts.vue'
import type { TriageCount } from './ui/UiTriageCounts.vue'
import { plural } from '../format'

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
// Authoring is a mode, not a section of the record. It used to be an inline
// accordion that sprang open for every test the agent had drafted but not run
// — which is most of them — and pushed the result off the screen.
const definitionOpen = ref(false)
const definitionSnapshot = ref<Partial<DataTest> | null>(null)
const creating = ref(false)
const saving = ref(false)
const running = ref(false)
const runningAll = ref(false)
const generatingFindings = ref(false)
const filter = ref<string>('all')
// What the status bar has asked the list to show. It composes with the triage
// chips rather than replacing them: the lanes narrow by state of work, the
// chips by outcome, and both are views over the same array.
const statusFilter = ref<DataTestFilter | null>(null)
// A second axis over the same tests, not a sixth status: "exceptions nobody
// has concluded on" needs both halves at once.
const conclusionFilter = ref<'all' | TestConclusionState>('all')
const search = ref('')
const editAnalyticsSpec = ref<{ test_id: string; params: Record<string, unknown> }>({ test_id: '', params: {} })
const editAnalyticsReady = ref(false)
const editPolarsSteps = ref<DataTestStep[]>([])

// Short labels — see DocTestItemDetail: the field name already says "Control".
const controlConclusions = [
  { label: 'Not concluded', value: 'no_conclusion' },
  { label: 'Effective', value: 'effective' },
  { label: 'Partially effective', value: 'partially_effective' },
  { label: 'Ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]
const selected = computed(() => tests.value.find(item => item.id === selectedId.value) ?? null)
function conclusionLabel(value: string) {
  return controlConclusions.find(item => item.value === value)?.label ?? value
}
// A prompt, not a gate: the save goes through either way, and this is what
// tells the auditor the working paper will be thin until they write it up.
const departsWithoutReason = computed(() => {
  const item = selected.value
  if (!item) return false
  return (
    item.control_conclusion !== 'no_conclusion'
    && item.control_conclusion !== item.evaluation.suggested_control_conclusion
    && !item.conclusion.trim()
  )
})
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
// The status chips stay a whole-tab tally, so the conclusion row is counted
// within everything else already applied — the number on a conclusion chip is
// what clicking it would leave, not a separate total.
// The lanes count every test, not the filtered list: a count that shrank as you
// filtered by it could never be clicked back out of.
const status = computed(() => dataTestStatus(tests.value, planning.value?.findings ?? []))
const statusFilterLabel = computed(() =>
  (statusFilter.value ? DATA_TEST_FILTER_LABELS[statusFilter.value] : ''))
const statusScope = computed(() =>
  filterDataTests(tests.value, statusFilter.value, planning.value?.findings ?? []))
const conclusionScope = computed(() => statusScope.value.filter(test => {
  if (filterRcm.value && test.rcm_id !== filterRcm.value) return false
  if (filter.value === 'not_run' && test.last_run) return false
  if (filter.value !== 'all' && filter.value !== 'not_run' && test.status !== filter.value) return false
  return true
}))
const conclusionFacets = computed(() =>
  conclusionCounts(conclusionScope.value.map(dataTestConclusion)))
const visibleTests = computed(() => {
  const query = search.value.trim().toLowerCase()
  return conclusionScope.value.filter(test => {
    if (conclusionFilter.value !== 'all' && dataTestConclusion(test) !== conclusionFilter.value) return false
    if (!query) return true
    return [test.title, test.objective, test.criteria, test.rcm_id ?? '']
      .some(value => (value ?? '').toLowerCase().includes(query))
  })
})
const activeFilterLabel = computed(() => {
  const status = triage.value.find(count => count.key === filter.value)?.label.toLowerCase() ?? 'tests'
  const conclusion = conclusionFilter.value === 'all'
    ? null
    : conclusionFacets.value.find(count => count.key === conclusionFilter.value)?.label.toLowerCase()
  return conclusion ? `${status} · ${conclusion}` : status
})
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
// The tests that ran, found something, and are still waiting on the finding
// that says so. Drafting is per RCM row — the same scope the single-test
// button uses — so the button offers the rows behind these tests at once.
const findingsPending = computed(() => {
  const drafted = new Set((planning.value?.findings ?? []).flatMap(finding => finding.test_refs))
  return tests.value.filter(test =>
    test.rcm_id
    && test.last_run
    && test.status === 'completed_with_exception'
    && !drafted.has(test.id))
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
function seedEditors(item: DataTest) {
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
}

// The plan fields bind straight to the selected test, so leaving the drawer
// without saving has to put the record back the way it was. Escape and a mask
// click go through the same event, which is why the revert lives here and not
// on the Cancel button. `hide` rather than `after-hide`: `after-hide` waits on
// the leave animation, and a revert that silently does not happen is worse
// than one that happens a frame early.
function openDefinition() {
  const item = selected.value
  if (!item) return
  definitionSnapshot.value = {
    title: item.title,
    objective: item.objective,
    criteria: item.criteria,
    rcm_id: item.rcm_id,
    table_refs: [...item.table_refs],
  }
  definitionOpen.value = true
}
function onDefinitionHide() {
  const item = selected.value
  const snapshot = definitionSnapshot.value
  if (item && snapshot) {
    Object.assign(item, snapshot)
    seedEditors(item)
  }
  definitionSnapshot.value = null
}

function selectTest(item: DataTest) {
  selectedId.value = item.id
  seedEditors(item)
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
    // The edit landed, so there is nothing to roll back — drop the snapshot
    // before closing or `hide` would undo what was just saved.
    definitionSnapshot.value = null
    definitionOpen.value = false
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
// One click for the commonest case: the run read it one way and the auditor
// agrees. It still records an auditor conclusion, because agreeing is a
// decision somebody made.
async function acceptRunReading() {
  if (!selected.value) return
  selected.value.control_conclusion = selected.value.evaluation.suggested_control_conclusion
  await saveConclusion()
}
async function ruleExceptionGroup(
  payload: { key: string; state: string; note: string },
) {
  if (!selected.value) return
  saving.value = true
  try {
    await api.post(
      `/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/exception-dispositions`,
      payload,
    )
    await load()
    emit('changed')
  } catch (error) { fail('Could not record the ruling', error) }
  finally { saving.value = false }
}
async function reviewSemantics(note: string) {
  if (!selected.value) return
  saving.value = true
  try {
    await api.post(
      `/api/workspaces/${props.workspace.id}/data-tests/${selected.value.id}/semantic-review`,
      { note },
    )
    await load()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Review recorded', life: 1800 })
  } catch (error) { fail('Could not record the review', error) }
  finally { saving.value = false }
}
async function execute(id: string) {
  running.value = true
  try {
    result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${id}/run`)
    await load()
    emit('changed')
  } catch (error) { fail('Could not run the data test', error) }
  finally { running.value = false }
}
async function runTest() {
  const item = selected.value
  if (!item) return
  // A re-run no longer overwrites a conclusion, but it does replace the
  // evidence one was reached against, which can leave it stale. Say so before
  // it happens rather than showing a warning afterwards.
  if (item.control_conclusion_source === 'auditor' || item.exception_dispositions.some(
    value => value.source === 'auditor' && value.state !== 'pending',
  )) {
    confirm.require({
      header: 'Run again?',
      message:
        'This replaces the result your conclusion was recorded against. The '
        + 'conclusion is kept, but it will be flagged as out of date if the '
        + 'definition or the data has changed since.',
      icon: 'pi pi-refresh',
      acceptProps: { label: 'Run again' },
      rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
      accept: () => { void execute(item.id) },
    })
    return
  }
  await execute(item.id)
}
/** `testIds` runs that subset; omitted, it re-runs every test in the tab. */
async function runAllTests(testIds?: string[]) {
  runningAll.value = true
  try {
    const batch = await api.post<{
      total: number
      completed: Array<{ data_test_id: string; status: string; exception_count: number }>
      failed: Array<{ data_test_id: string; error: string }>
    }>(`/api/workspaces/${props.workspace.id}/data-tests/run-all`, testIds ? { test_ids: testIds } : {})
    await load()
    emit('changed')
    toast.add({
      severity: batch.failed.length ? 'warn' : 'success',
      summary: `Ran ${batch.completed.length} of ${batch.total} Data Test${batch.total === 1 ? '' : 's'}`,
      detail: batch.failed.length
        ? `${plural(batch.failed.length, 'test')} could not run: ${batch.failed.map(item => item.data_test_id).join(', ')}`
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
    await assistantChat.createChat()
    await assistantChat.send(
      `${regenerate ? 'Regenerate' : 'Draft'} findings for RCM row ${selected.value.rcm_id}.`,
      'act', 'permission',
      { command: 'draft_findings', source: 'tab_button', runContext: { rcm_id: selected.value.rcm_id } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({ severity: 'success', summary: regenerate ? 'Finding regeneration started' : 'Finding-draft workflow started', detail: 'Exception observations will be used directly.', life: 3600 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
async function draftPendingFindings(testIds?: string[]) {
  const scope = testIds?.length
    ? findingsPending.value.filter(test => testIds.includes(test.id))
    : findingsPending.value
  const rcmIds = [...new Set(scope.map(test => test.rcm_id as string))]
  if (!rcmIds.length) return
  generatingFindings.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Draft findings for ${plural(rcmIds.length, 'RCM row')} with undrafted exceptions.`,
      'act', 'permission',
      { command: 'draft_findings', source: 'tab_button', runContext: { rcm_ids: rcmIds } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'success',
      summary: `Generating findings for ${plural(scope.length, 'test')}`,
      detail: 'Exception observations will be used directly.',
      life: 3600,
    })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
  finally { generatingFindings.value = false }
}
function openFinding(findingId: string) {
  void nav.replace('findings', { finding: findingId })
}
function openRcm() {
  if (!selected.value?.rcm_id) return
  void nav.replace('rcm', { rcm: selected.value.rcm_id })
}
/**
 * The lanes name what they want done; the tab still owns how. Both runs go
 * through the same batch endpoint the header's "Run all" uses, scoped to the
 * tests the lane counted.
 */
function runStatusAction(action: StatusAction) {
  switch (action.key as DataTestActionKey) {
    case 'run_tests':
    case 'rerun_stale': return void runAllTests(action.ids)
    case 'draft_findings': return void draftPendingFindings(action.ids)
  }
}
function pickFilter(key: string) {
  filter.value = key
}
function pickConclusion(key: string) {
  conclusionFilter.value = key as 'all' | TestConclusionState
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
      <Button label="Run all" icon="pi pi-play" size="small" outlined :loading="runningAll" :disabled="running || runningAll" @click="runAllTests()" />
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

    <UiStatusLanes
      v-if="tests.length"
      :lanes="status.lanes"
      :disclosures="status.disclosures"
      :filter="statusFilter"
      :filterLabel="statusFilterLabel"
      :busy="running || runningAll || generatingFindings"
      :canRunAgent="!agent.isActive.value"
      @filter="statusFilter = ($event as DataTestFilter | null)"
      @action="runStatusAction"
    />

    <template v-if="tests.length">
      <div class="facets">
        <UiTriageCounts :counts="triage" :active="filter" label="Outcome" @select="pickFilter" />
        <UiTriageCounts
          :counts="conclusionFacets"
          :active="conclusionFilter"
          label="Conclusion"
          @select="pickConclusion"
        />
      </div>

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

      <!-- 16rem, not 20: the width the list gives back is what makes room for
           the action rail in the detail. -->
      <UiMasterDetail railWidth="20rem" class="layout">
        <template #rail>
          <DataTestList :tests="visibleTests" :selectedId="selectedId" :rcmRows="rcmRows" @select="selectTest" />
        </template>

        <section v-if="selected" class="detail">
          <!-- Identity only; the actions all sit in the rail. -->
          <header class="detail-head">
            <p class="eyebrow">{{ selected.id }}</p>
            <h3>{{ selected.title }}</h3>
            <p class="objective">{{ selected.objective }}</p>
          </header>

          <div class="detail-main">
            <DataTestResultPanel
              :test="selected"
              :result="result"
              :busy="saving"
              @rule="ruleExceptionGroup"
              @review-semantics="reviewSemantics"
              @run="runTest"
            />
          </div>

          <!-- The rail: run it, and record what it means. -->
          <aside class="detail-rail" aria-label="Your assessment">
            <div class="rail-group">
              <Button label="Run" icon="pi pi-play" size="small" :loading="running" :disabled="runningAll" @click="runTest" />
              <Button label="Edit definition" icon="pi pi-sliders-h" size="small" outlined @click="openDefinition" />
              <Button v-if="selected.rcm_id" label="Open RCM" icon="pi pi-map" size="small" outlined @click="openRcm" />
              <Button label="Pin" icon="pi pi-thumbtack" size="small" outlined :disabled="!selected.last_run" @click="pin" />
            </div>

            <div class="rail-group">
              <h4>Your conclusion</h4>

              <!-- What the run reads as, stated before the control that departs
                   from it. Accepting is still an auditor conclusion; the run
                   only ever suggests. -->
              <p v-if="selected.evaluation.state !== 'not_run'" class="run-reading">
                The run reads this as
                <strong>{{ conclusionLabel(selected.evaluation.suggested_control_conclusion) }}</strong>.
                <Button
                  v-if="selected.control_conclusion !== selected.evaluation.suggested_control_conclusion
                    || selected.control_conclusion_source !== 'auditor'"
                  label="Accept" link size="small" :loading="saving" @click="acceptRunReading"
                />
              </p>
              <p v-if="selected.control_conclusion_source === 'agent'" class="by-agent">
                Concluded by an unattended run. No auditor has reviewed it.
              </p>
              <p v-else-if="selected.control_conclusion_stale" class="is-stale">
                Recorded against evidence that has since changed — re-save to re-affirm it.
              </p>

              <label>
                Control conclusion
                <Select
                  v-model="selected.control_conclusion"
                  :options="controlConclusions"
                  optionLabel="label"
                  optionValue="value"
                />
              </label>
              <label>
                Conclusion
                <Textarea v-model="selected.conclusion" rows="3" autoResize placeholder="What this result means for the control, in your own words." />
              </label>
              <!-- Departing from the run is the judgement a working paper most
                   wants to show, so the prompt stays. It no longer blocks the
                   save: deciding and writing up are separate acts. -->
              <small v-if="departsWithoutReason" class="needs-reason">
                This departs from the run — a written reason above is worth having.
              </small>
              <Button
                label="Save conclusion" icon="pi pi-check" size="small" outlined
                :loading="saving" @click="saveConclusion"
              />
            </div>

            <div class="rail-group">
              <h4>Finding{{ linkedFindings.length > 1 ? 's' : '' }}</h4>
              <template v-if="linkedFindings.length">
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
              </template>
              <template v-else>
                <p class="rail-note">Generate a draft from this test’s exception observations.</p>
                <Button label="Generate finding" icon="pi pi-sparkles" size="small" :disabled="!selected.rcm_id || !selected.last_run" @click="() => draftFinding()" />
              </template>
            </div>
          </aside>
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

    <!-- Authoring gets the width it needs instead of competing with the result
         for the record column. -->
    <Drawer
      v-model:visible="definitionOpen"
      position="right"
      class="definition-drawer"
      :style="{ width: 'min(52rem, 96vw)' }"
      @hide="onDefinitionHide"
    >
      <template #header>
        <div class="drawer-head">
          <p class="eyebrow">Definition</p>
          <strong>{{ selected?.title }}</strong>
        </div>
      </template>

      <div v-if="selected" class="definition">
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
      </div>

      <template #footer>
        <div class="save-row">
          <Button label="Cancel" size="small" text severity="secondary" @click="definitionOpen = false" />
          <Button label="Save only" size="small" outlined :loading="saving" :disabled="!definitionReady" @click="save(false)" />
          <Button
            label="Save and run"
            icon="pi pi-play"
            size="small"
            :loading="saving || running"
            :disabled="!definitionReady"
            @click="save(true)"
          />
        </div>
      </template>
    </Drawer>
  </div>
</template>

<style scoped>
.data-tests { display: flex; flex-direction: column; gap: var(--aw-section-gap); min-width: 0; max-width: 100%; min-height: 100%; }
.facets { display: flex; flex-direction: column; gap: var(--aw-space-2); min-width: 0; }
.toolbar { display: flex; align-items: center; gap: 0.6rem; min-width: 0; flex-wrap: wrap; }
.toolbar :deep(.p-iconfield) { flex: 1 1 14rem; min-width: 0; max-width: 22rem; }
.toolbar :deep(.p-inputtext) { width: 100%; }
.rcm-facet { flex: 0 1 14rem; min-width: 0; }
.muted { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.layout { min-height: 32rem; }

/* One panel for the whole detail column — see DocTestItemDetail for why. */
/* align-content: start — see DocTestItemDetail; min-height plus a stretching
   grid would otherwise inflate the header row. */
.detail { display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; gap: var(--aw-space-4); min-width: 0; max-width: 100%; min-height: 100%; padding: 1rem; border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.detail-head { min-width: 0; }
.eyebrow { margin: 0; }
.detail-head h3 { margin: 0.15rem 0 0.25rem; font-size: var(--aw-text-lg); line-height: 1.3; }
.objective { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.45; }

.detail-main { display: flex; flex-direction: column; gap: 0.8rem; min-width: 0; container: detail-main / inline-size; }

.detail-rail { display: flex; flex-direction: column; gap: 0.9rem; min-width: 0; padding: 0.9rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.rail-group { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
.rail-group + .rail-group { padding-top: 0.9rem; border-top: 1px solid var(--aw-border-strong); }
.rail-group h4 { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.rail-note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.4; }
.detail-rail :deep(.p-button) { width: 100%; justify-content: center; }

/* 42rem — see DocTestItemDetail for why this is measured, not round. */
@container master-detail-content (min-width: 42rem) {
  .detail { grid-template-columns: minmax(0, 1fr) 13rem; }
  .detail-head { grid-column: 1 / -1; }
  /* A pinned rail taller than the window puts its own last control out of
     reach: it sticks at the top and the overflow below cannot be scrolled to.
     Measured at 695px against a 700px viewport, so the conclusion state lines
     were one sentence away from stranding the Save button. */
  .detail-rail {
    position: sticky;
    top: 0;
    align-self: start;
    max-height: 100vh;
    overflow-y: auto;
  }
}

/* The definition editor now lives in a drawer, which PrimeVue teleports to the
   body. Scoped styles still reach it — the scope attribute is on these
   elements — but a container query cannot, because `detail-main` is no longer
   an ancestor. Hence a viewport query for the narrow case. */
.drawer-head { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
.drawer-head strong { font-size: var(--aw-text-md); }
.definition { display: flex; flex-direction: column; gap: 0.85rem; min-width: 0; }
.plan { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0.7rem; }
.wide { grid-column: 1 / -1; }
.save-row { display: flex; align-items: center; justify-content: flex-end; gap: 0.5rem; }

label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; }

@media (max-width: 44rem) {
  .plan { grid-template-columns: minmax(0, 1fr); }
  .wide { grid-column: auto; }
}
</style>
