<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import Popover from 'primevue/popover'
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
import DataTestDefinitionForm from './data-tests/DataTestDefinitionForm.vue'
import type { DataTestDraft } from './data-tests/DataTestDefinitionForm.vue'
import DataTestList from './data-tests/DataTestList.vue'
import DataTestResultPanel from './data-tests/DataTestResultPanel.vue'
import { emptyPolarsStep } from './data-tests/steps'
import UiDefinitionDrawer from './ui/UiDefinitionDrawer.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import UiVerdictBar from './ui/UiVerdictBar.vue'
import {
  DATA_TEST_CHIPS, dataTestHeadline, dataTestStatus, filterDataTests,
} from './data-tests/dataTestStatus'
import type { DataTestFilter } from './data-tests/dataTestStatus'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const nav = useWorkspaceNav()
const toast = useToast()
const confirm = useConfirm()
const assistantChat = useAssistantChat(props.workspace.id)
const agent = useAgentRun(props.workspace.id)
const { launchMode } = agent

const tests = ref<DataTest[]>([])
const planning = ref<PlanningPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.test || '') || null)
// Captured before the first selection rewrites the query.
const createRequested = Boolean(route.query.create)
const requestedRcmId = String(route.query.rcm || '')
const requestedEngine: DataTestEngine = String(route.query.create) === 'polars' ? 'polars' : 'analytics'
const result = ref<DataTestResult | null>(null)
// Authoring is a mode, not a section of the record, and it is one mode whether
// the record exists yet or not: `New test` was a 56rem modal and `Edit
// definition` a right drawer, for the same fields.
const definitionOpen = ref(false)
const definitionMode = ref<'new' | 'edit'>('new')
const definitionReady = ref(false)
const definitionDraft = ref<DataTestDraft>(emptyDraft())
/** Remounts the authoring components when the record under edit changes. */
const definitionSession = ref('new')
const saving = ref(false)
const running = ref(false)
const runningAll = ref(false)
const generatingFindings = ref(false)
// The narrowings in force, at most one per axis. The review bar's chips are
// this same vocabulary: a count on the bar and a predicate here.
const statusFilter = ref<DataTestFilter[]>([])
const search = ref('')
const changePanel = ref<InstanceType<typeof Popover> | null>(null)

// Short labels — see DocTestItemDetail: the field name already says "Control".
const controlConclusions = [
  { label: 'Not concluded', value: 'no_conclusion' },
  { label: 'Effective', value: 'effective' },
  { label: 'Partially effective', value: 'partially_effective' },
  { label: 'Ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]
const selected = computed(() => tests.value.find(item => item.id === selectedId.value) ?? null)
// The verdict bar edits a draft carrying the id it belongs to, never the record
// in `tests`. Binding the fields straight to the selected test made choosing a
// conclusion re-filter the list mid-edit: the test stopped matching "not
// concluded", left the list, the watcher below reselected whatever took its
// place, and Save then wrote the draft to *that* test. A working paper cannot
// have a conclusion land on a control the auditor never looked at.
const conclusionDraft = ref<{
  id: string
  control_conclusion: DataTest['control_conclusion']
  conclusion: string
} | null>(null)
function seedConclusion(item: DataTest | null) {
  conclusionDraft.value = item
    ? { id: item.id, control_conclusion: item.control_conclusion, conclusion: item.conclusion }
    : null
}
function conclusionLabel(value: string) {
  return controlConclusions.find(item => item.value === value)?.label ?? value
}
// A prompt, not a gate: the save goes through either way, and this is what
// tells the auditor the working paper will be thin until they write it up.
const departsWithoutReason = computed(() => {
  const item = selected.value
  const draft = conclusionDraft.value
  if (!item || !draft) return false
  return (
    draft.control_conclusion !== 'no_conclusion'
    && draft.control_conclusion !== item.evaluation.suggested_control_conclusion
    && !draft.conclusion.trim()
  )
})
const rcmRows = computed(() => planning.value?.rcm ?? [])
const rcmOptions = computed(() => rcmRows.value.map(row => ({ label: `${row.id} · ${row.risk}`, value: row.id })))

// Only offer a facet that can actually split the list. With one engine and two
// statuses, three always-on dropdowns filtered nothing.
const rcmFacet = computed(() => {
  const linked = new Set(tests.value.map(test => test.rcm_id).filter(Boolean))
  return linked.size > 1 ? rcmOptions.value.filter(option => linked.has(option.value)) : []
})
const filterRcm = ref<string | null>(null)
// The bar counts every test, not the filtered list: a count that shrank as you
// filtered by it could never be clicked back out of.
const status = computed(() => dataTestStatus(tests.value, planning.value?.findings ?? []))
const headline = computed(() => dataTestHeadline(tests.value))
const statusBusy = computed(() => running.value || runningAll.value || generatingFindings.value)
const canRunAgent = computed(() => !agent.isActive.value)
// Folded rather than combined: each narrowing runs the same predicate over
// what the last one left, so the filters compose without a second code path.
const statusScope = computed(() => statusFilter.value
  .reduce(
    (rows, key) => filterDataTests(rows, key, planning.value?.findings ?? []),
    tests.value,
  )
  .filter(test => !filterRcm.value || test.rcm_id === filterRcm.value))
const visibleTests = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return statusScope.value
  return statusScope.value.filter(test => [test.title, test.objective, test.criteria, test.rcm_id ?? '']
    .some(value => (value ?? '').toLowerCase().includes(query)))
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
const staleTestIds = computed(() => tests.value.filter(test => test.result_stale).map(test => test.id))

/* ---- What the verdict bar says ---------------------------------------- */

/**
 * The population reading, from the stored result where there is one.
 *
 * `exception_profile` is the only place the population lives, so a test whose
 * result is not loaded states what it found without pretending to know what it
 * found it out of.
 */
const found = computed(() => {
  const test = selected.value
  if (!test) return null
  const profile = result.value?.exception_profile ?? null
  const failed = profile?.record_count ?? test.evaluation.exception_count
  const population = profile?.population ?? null
  const rate = population ? Math.round((failed / population) * 100) : null
  return {
    ran: test.evaluation.state !== 'not_run',
    failed,
    population,
    rate,
    open: test.open_exception_count,
    at: test.last_run?.run_at ? new Date(test.last_run.run_at).toLocaleString() : null,
    error: result.value?.error ?? null,
  }
})
const verdictTone = computed<'ok' | 'warn' | 'bad' | 'neutral'>(() => {
  const test = selected.value
  if (!test || test.evaluation.state === 'not_run') return 'neutral'
  if (test.evaluation.state === 'inconclusive') return 'warn'
  return test.evaluation.exception_count ? 'bad' : 'ok'
})
/**
 * Whether the run's own reading is still on offer.
 *
 * Anything but an auditor's current conclusion agreeing with the run leaves
 * something to accept: no conclusion at all, one the agent wrote, one recorded
 * against an earlier run, or one that simply differs.
 */
const canAccept = computed(() => {
  const test = selected.value
  if (!test || test.evaluation.state === 'not_run') return false
  return test.control_conclusion !== test.evaluation.suggested_control_conclusion
    || test.control_conclusion_source !== 'auditor'
    || test.control_conclusion_stale
})
const staleSentence = computed(() => (selected.value?.control_conclusion_stale
  ? 'The conclusion was recorded against an earlier run. Accepting re-affirms it against this one.'
  : selected.value?.result_stale
    ? 'The definition or the data has changed since this run. Everything below describes the earlier state.'
    : undefined))
const findingNote = computed(() => {
  const test = selected.value
  if (!test) return ''
  if (!test.rcm_id) return 'This test supports no RCM row, so no finding is owed.'
  if (test.open_exception_count) {
    return `None yet. ${plural(test.open_exception_count, 'exception row')} still open.`
  }
  return test.status === 'completed_with_exception' ? 'None yet.' : 'No exception to write up.'
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
/**
 * A blank definition. `requestedRcmId` and `requestedEngine` are what the RCM
 * row's "Add test" link carried over, so the drawer opens already answering
 * the two questions that link already answered.
 */
function emptyDraft(): DataTestDraft {
  return {
    title: '', objective: '', criteria: '',
    engine: requestedEngine,
    rcmId: requestedRcmId,
    table: '',
    analytics: { test_id: '', params: {} },
    steps: [emptyPolarsStep()],
  }
}
/** The record as the form reads it. The form edits a copy, never the record. */
function draftFrom(item: DataTest): DataTestDraft {
  const params = item.spec.params
  return {
    title: item.title,
    objective: item.objective,
    criteria: item.criteria,
    engine: item.engine ?? 'analytics',
    rcmId: item.rcm_id ?? '',
    table: item.table_refs[0] ?? '',
    analytics: {
      test_id: String(item.spec.test_id ?? ''),
      params: params && typeof params === 'object' && !Array.isArray(params)
        ? JSON.parse(JSON.stringify(params)) as Record<string, unknown>
        : {},
    },
    steps: Array.isArray(item.spec.steps) && item.spec.steps.length
      ? JSON.parse(JSON.stringify(item.spec.steps)) as DataTestStep[]
      : [emptyPolarsStep()],
  }
}
function openNewDefinition() {
  definitionMode.value = 'new'
  definitionDraft.value = emptyDraft()
  definitionSession.value = `new-${Date.now()}`
  definitionOpen.value = true
}
function openDefinition() {
  const item = selected.value
  if (!item) return
  definitionMode.value = 'edit'
  definitionDraft.value = draftFrom(item)
  definitionSession.value = item.id
  definitionOpen.value = true
}
/** The one shape the API takes, whichever half of the drawer is in force. */
function definitionPayload() {
  const value = definitionDraft.value
  return {
    title: value.title.trim(),
    objective: value.objective.trim(),
    criteria: value.criteria,
    engine: value.engine,
    rcm_id: value.rcmId,
    table_refs: value.table ? [value.table] : [],
    spec: value.engine === 'analytics'
      ? value.analytics
      : { schema_version: 2, steps: value.steps },
  }
}

function selectTest(item: DataTest) {
  selectedId.value = item.id
  seedConclusion(item)
  result.value = null
  void nav.replace('data-tests', { test: item.id })
  if (item.last_run) void loadResult(item, item.last_run.id)
}
async function loadResult(item: DataTest, runId: string) {
  try {
    result.value = await api.get(`/api/workspaces/${props.workspace.id}/data-tests/${item.id}/runs/${runId}`)
  } catch (error) { fail('Could not open the stored result', error) }
}

/**
 * Save the definition, and run it where asked.
 *
 * Creating a definition nobody ran was the commonest dead end, which is why
 * the create path ran unconditionally before. It still leads with running —
 * that is the primary — but writing one down to run later is now something the
 * drawer can do, because editing an existing definition always could.
 */
async function saveDefinition(thenRun: boolean) {
  saving.value = true
  const creating = definitionMode.value === 'new'
  try {
    const id = creating
      ? (await api.post<DataTest>(`/api/workspaces/${props.workspace.id}/data-tests`, definitionPayload())).id
      : selectedId.value
    if (!id) return
    if (!creating) {
      await api.patch(`/api/workspaces/${props.workspace.id}/data-tests/${id}`, definitionPayload())
    }
    if (thenRun) {
      running.value = true
      result.value = await api.post<DataTestResult>(`/api/workspaces/${props.workspace.id}/data-tests/${id}/run`)
    }
    selectedId.value = id
    await load()
    emit('changed')
    definitionOpen.value = false
    toast.add({
      severity: 'success',
      summary: creating ? 'Test created' : thenRun ? 'Saved and run' : 'Definition saved',
      detail: thenRun ? undefined : 'Run it to produce a result.',
      life: 2000,
    })
  } catch (error) {
    fail(creating ? 'Could not create the data test' : 'Could not save the data test', error)
  } finally { saving.value = false; running.value = false }
}

async function saveConclusion() {
  // Against the draft's own id, not whatever is selected by the time the
  // request goes out: `load()` and the visible-tests watcher can both move the
  // selection while this is in flight.
  const draft = conclusionDraft.value
  if (!draft) return
  saving.value = true
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/data-tests/${draft.id}`, {
      conclusion: draft.conclusion,
      control_conclusion: draft.control_conclusion,
    })
    await load()
    emit('changed')
    changePanel.value?.hide()
    toast.add({ severity: 'success', summary: 'Conclusion saved', life: 1800 })
  } catch (error) { fail('Could not save the conclusion', error) }
  finally { saving.value = false }
}
// One click for the commonest case: the run read it one way and the auditor
// agrees. It still records an auditor conclusion, because agreeing is a
// decision somebody made.
async function acceptRunReading() {
  if (!selected.value || !conclusionDraft.value) return
  conclusionDraft.value.control_conclusion = selected.value.evaluation.suggested_control_conclusion
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
async function draftFinding(regenerate = false) {
  if (!selected.value?.rcm_id || !selected.value.last_run) return
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `${regenerate ? 'Regenerate' : 'Draft'} findings for RCM row ${selected.value.rcm_id}.`,
      'act', launchMode.value,
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
      'act', launchMode.value,
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
function findingSeverity(id: string) {
  return linkedFindings.value.find(finding => finding.id === id)?.severity ?? ''
}

// The occasional actions, behind one menu rather than four more buttons in the
// header. Each was already a control on this page; none is frequent enough to
// stand permanently beside the one that says what is outstanding.
const menuItems = computed(() => [
  {
    label: `Re-run ${plural(staleTestIds.value.length, 'stale test')}`,
    icon: 'pi pi-refresh',
    disabled: !staleTestIds.value.length || statusBusy.value,
    command: () => void runAllTests(staleTestIds.value),
  },
  {
    label: 'Delete this test',
    icon: 'pi pi-trash',
    disabled: !selected.value,
    command: () => deleteTest(),
  },
])

watch(visibleTests, items => {
  if (!items.length || items.some(item => item.id === selectedId.value)) return
  selectTest(items[0])
})

onMounted(() => {
  if (createRequested) openNewDefinition()
  void load().catch(error => fail('Could not load data tests', error))
})
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void load().catch(error => fail('Could not refresh data tests', error))
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="data-tests">
    <!-- One title, one count sentence, at most one primary. What is
         outstanding is the primary when anything is; otherwise the page's
         ordinary next act takes the slot. -->
    <header class="page-head">
      <h1>Data tests</h1>
      <p class="headline aw-figure">{{ headline }}</p>
      <span class="grow" />
      <Button label="New test" icon="pi pi-plus" size="small" outlined severity="secondary" @click="openNewDefinition" />
      <Button
        v-if="findingsPending.length"
        label="Run all"
        icon="pi pi-play"
        size="small"
        outlined
        severity="secondary"
        :loading="runningAll"
        :disabled="running || runningAll"
        @click="runAllTests()"
      />
      <Button
        v-if="findingsPending.length"
        :label="`Draft ${plural(findingsPending.length, 'finding')}`"
        icon="pi pi-flag"
        size="small"
        severity="warn"
        :disabled="statusBusy || !canRunAgent"
        @click="draftPendingFindings()"
      />
      <Button
        v-else
        label="Run all"
        icon="pi pi-play"
        size="small"
        :loading="runningAll"
        :disabled="running || runningAll || !tests.length"
        @click="runAllTests()"
      />
      <UiOverflowMenu :items="menuItems" tooltip="More data test actions" />
    </header>

    <UiReviewBar
      v-if="tests.length"
      :lanes="status.lanes"
      :chips="DATA_TEST_CHIPS"
      :filters="status.filters"
      allLabel="All tests"
      :total="tests.length"
      :filter="statusFilter"
      @filter="statusFilter = ($event as DataTestFilter[])"
    />

    <div v-if="tests.length" class="layout">
      <section class="list-panel">
        <div class="list-head">
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
          />
        </div>
        <div class="list-body">
          <DataTestList :tests="visibleTests" :selectedId="selectedId" @select="selectTest" />
        </div>
      </section>

      <section v-if="selected" class="detail">
        <header class="detail-head">
          <div class="detail-copy">
            <p class="detail-id">{{ selected.id }}</p>
            <h2>{{ selected.title }}</h2>
            <p class="objective">{{ selected.objective }}</p>
          </div>
          <Button
            v-if="selected.rcm_id"
            :label="selected.rcm_id"
            icon="pi pi-map"
            size="small"
            outlined
            class="rcm-chip"
            @click="openRcm"
          />
          <Button label="Edit definition" icon="pi pi-sliders-h" size="small" outlined severity="secondary" @click="openDefinition" />
          <Button label="Run" icon="pi pi-play" size="small" outlined severity="secondary" :loading="running" :disabled="runningAll" @click="runTest" />
        </header>

        <!-- What the run found, and what is recorded. Twice, never four
             times: the status chip, the headline, the rail heading and the
             Result block all said one of these two things. -->
        <UiVerdictBar :tone="verdictTone" :stale="staleSentence">
          <template #found>
            <template v-if="found?.error">
              <span class="failed">{{ found.error }}</span>
            </template>
            <template v-else-if="!found?.ran">
              <span>This test has not been run yet.</span>
            </template>
            <template v-else>
              <span class="aw-figure">
                {{ found.failed }}<template v-if="found.population"> of {{ found.population }}</template>
                {{ found.failed === 1 && !found.population ? 'record' : 'records' }} failed
              </span>
              <span v-if="found.rate !== null" class="rate aw-figure" :data-heavy="found.rate >= 10">{{ found.rate }}%</span>
              <span class="meta aw-figure">
                <template v-if="found.open">· {{ found.open }} still open </template>
                <template v-if="found.at">· run {{ found.at }}</template>
              </span>
            </template>
          </template>

          <template #recorded>
            <template v-if="selected.control_conclusion_source === 'none' || selected.control_conclusion === 'no_conclusion'">
              Not concluded.
              <template v-if="found?.ran">
                The run reads this as <b>{{ conclusionLabel(selected.evaluation.suggested_control_conclusion) }}</b>.
              </template>
            </template>
            <template v-else-if="selected.control_conclusion_source === 'agent'">
              Concluded <b>{{ conclusionLabel(selected.control_conclusion) }}</b> by an unattended run.
              <span class="by-agent">No auditor has read it.</span>
            </template>
            <template v-else>
              Concluded <b>{{ conclusionLabel(selected.control_conclusion) }}</b> by an auditor.
            </template>
          </template>

          <template #actions>
            <Button
              v-if="canAccept"
              label="Accept conclusion"
              icon="pi pi-check"
              size="small"
              :loading="saving"
              @click="acceptRunReading"
            />
            <Button
              label="Change"
              icon="pi pi-chevron-down"
              iconPos="right"
              size="small"
              outlined
              severity="secondary"
              aria-haspopup="true"
              @click="changePanel?.toggle($event)"
            />
          </template>
        </UiVerdictBar>

        <DataTestResultPanel
          :test="selected"
          :result="result"
          :busy="saving"
          :workspaceId="workspace.id"
          @rule="ruleExceptionGroup"
          @review-semantics="reviewSemantics"
        />

        <span class="grow" />

        <!-- The write-up, on the row that closes the record. -->
        <div class="footer-row">
          <p class="aw-label">Finding</p>
          <template v-if="linkedFindings.length">
            <button
              v-for="finding in linkedFindings"
              :key="finding.id"
              type="button"
              class="finding-chip"
              @click="openFinding(finding.id)"
            >
              <span class="finding-id">{{ finding.id }}</span>{{ findingSeverity(finding.id) }}
            </button>
          </template>
          <p v-else class="finding-note">{{ findingNote }}</p>
          <span class="grow" />
          <Button
            :label="linkedFindings.length ? 'Regenerate' : 'Generate finding'"
            :icon="linkedFindings.length ? 'pi pi-refresh' : 'pi pi-sparkles'"
            size="small"
            outlined
            severity="secondary"
            :disabled="!selected.rcm_id || !selected.last_run"
            @click="draftFinding(linkedFindings.length > 0)"
          />
        </div>
      </section>
      <UiEmptyState
        v-else
        icon="pi pi-shield"
        title="No data test selected"
        description="Pick a test from the list, or create one."
      />
    </div>

    <UiEmptyState
      v-else
      icon="pi pi-shield"
      title="Run analytics over the imported data"
      description="Pick an analytic from the library, or write Polars code, and link it to an RCM row so the result counts as coverage."
    >
      <Button label="New test" icon="pi pi-plus" @click="openNewDefinition" />
    </UiEmptyState>

    <!-- Changing the recorded conclusion is the rarer half of the verdict bar,
         so it opens from it rather than standing as a permanent form. -->
    <Popover ref="changePanel">
      <div v-if="conclusionDraft" class="change-form">
        <label>
          Control conclusion
          <Select
            v-model="conclusionDraft.control_conclusion"
            :options="controlConclusions"
            optionLabel="label"
            optionValue="value"
          />
        </label>
        <label>
          Conclusion
          <Textarea v-model="conclusionDraft.conclusion" rows="3" autoResize placeholder="What this result means for the control, in your own words." />
        </label>
        <!-- Departing from the run is the judgement a working paper most wants
             to show, so the prompt stays. It does not block the save: deciding
             and writing up are separate acts. -->
        <small v-if="departsWithoutReason" class="needs-reason">
          This departs from the run — a written reason above is worth having.
        </small>
        <Button label="Save" icon="pi pi-check" size="small" :loading="saving" @click="saveConclusion" />
      </div>
    </Popover>

    <!-- One drawer for both halves of authoring. `New test` was a 56rem modal
         and `Edit definition` a right drawer, for the same fields. -->
    <UiDefinitionDrawer
      v-model="definitionOpen"
      :eyebrow="definitionMode === 'edit' ? `Definition · ${selectedId}` : 'New test'"
      :title="definitionDraft.title || 'Untitled data test'"
      :editing="definitionMode === 'edit'"
      :ready="definitionReady"
      :saving="saving"
      :running="running"
      consequence="Changing the definition marks the recorded conclusion out of date."
      @save="saveDefinition"
    >
      <DataTestDefinitionForm
        :key="definitionSession"
        v-model="definitionDraft"
        :workspace="workspace"
        :rcmRows="rcmRows"
        :session="definitionSession"
        :autoName="definitionMode === 'new'"
        @valid="definitionReady = $event"
        @error="fail"
      />
    </UiDefinitionDrawer>
  </div>
</template>

<style scoped>
.data-tests { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

/* One 36px row: the title, what there is of it, and at most one primary. */
.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

.layout { display: grid; grid-template-columns: 18.75rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { display: flex; flex-direction: column; gap: .5rem; padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext), .list-head :deep(.p-select) { width: 100%; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

/* One panel for the whole detail column: the rail is gone, so nothing inside
   it competes for width and nothing scrolls independently of the row it
   belongs to. */
.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-width: 100%; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  container: master-detail-content / inline-size;
  overflow-y: auto;
}
.detail-head { display: flex; align-items: flex-start; gap: 1rem; min-width: 0; }
.detail-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.detail-id { margin: 0; color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; }
.detail-head h2 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: 600; letter-spacing: -0.01em; }
.objective { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.45; }
.rcm-chip :deep(.p-button-label), .detail-head :deep(.p-button) { white-space: nowrap; }
.rcm-chip { border-color: var(--aw-teal-line); color: var(--aw-teal); }

.failed { color: var(--aw-danger); }
.rate { color: var(--aw-muted); font-weight: 700; }
.rate[data-heavy='true'] { color: var(--aw-danger); }
.meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }
.by-agent { color: var(--aw-accent); }

.footer-row { display: flex; align-items: center; gap: .625rem; flex-wrap: wrap; padding-top: .875rem; border-top: 1px solid var(--aw-border); }
.finding-note { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }
.finding-chip {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .125rem .5rem;
  border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.finding-chip .finding-id { font-family: var(--aw-font-mono); }

.change-form { display: flex; flex-direction: column; gap: .6rem; min-width: 18rem; max-width: 24rem; }
.needs-reason { color: var(--aw-warn-ink); font-size: var(--aw-text-xs); }

label { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 600; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; }

@container workspace-panel (max-width: 60rem) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .list-body { max-height: 18rem; }
}
</style>
