<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import SplitButton from 'primevue/splitbutton'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useSession } from '../composables/useSession'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  AuditDocument,
  AuditFinding,
  DocTest,
  DocTestDispositionState,
  DocTestItem,
  DocTestKind,
  DocTestSummaryEntry,
  DocTestSummaryItem,
  DocTestSummaryCycleTest,
  DocTestSummaryPayload,
  EvidenceRef,
  PlanningPayload,
  WorkspaceSummary,
  CycleRulesetDefinition, CycleVouchMetadata,
} from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import DocTestDefinitionForm from './doc-tests/DocTestDefinitionForm.vue'
import type { DocTestDraft } from './doc-tests/DocTestDefinitionForm.vue'
import CycleVouchGrid from './doc-tests/CycleVouchGrid.vue'
import CycleRulesetReview from './doc-tests/CycleRulesetReview.vue'
import DocTestItemDetail from './doc-tests/DocTestItemDetail.vue'
import DocTestItemList from './doc-tests/DocTestItemList.vue'
import UiDefinitionDrawer from './ui/UiDefinitionDrawer.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import {
  DOC_TEST_CHIPS, docTestHeadline, docTestStatus, filterDocTestEntries,
} from './doc-tests/docTestStatus'
import type { DocTestFilter } from './doc-tests/docTestStatus'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const nav = useWorkspaceNav()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { launchMode } = agent

const summary = ref<DocTestSummaryPayload | null>(null)
const documents = ref<AuditDocument[]>([])
const planning = ref<PlanningPayload | null>(null)
const documentTypes = ref<string[]>([])
const cycleMetadata = ref<CycleVouchMetadata | null>(null)
const currentTest = ref<DocTest | null>(null)
const selectedItemId = ref<string | null>(String(route.query.item || '') || null)
const selectedCycleTestId = ref<string | null>(null)
const focusedAssertionKey = ref<string | null>(null)
const cycleGrid = ref<{
  focusSelectedCell: () => void
  loadGrid: () => Promise<void>
} | null>(null)
const requestedTestId = ref<string | null>(String(route.query.test || '') || null)
// Read the deep link before any selection rewrites the query, otherwise the
// RCM grid's "Add Document Test" link loses its own parameters.
const createRequested = route.query.create === '1'
const requestedRcmId = String(route.query.rcm || '')
// The narrowings in force. This used to be three separate controls — a status
// filter, an outcome row and a conclusion row — whose counts restated each
// other and whose resting state was two permanent rows of chips saying
// "nothing is filtered". One vocabulary now, from the status model, still
// composing across axes: "exceptions nobody has concluded on" is two at once.
const statusFilter = ref<DocTestFilter[]>([])
const search = ref('')
const createOpen = ref(false)
const createShape = ref('vouching')
const createDraft = ref<DocTestDraft>(emptyDocDraft())
const createReady = ref(false)
const createForm = ref<{ payload: () => Parameters<typeof createTest>[0] } | null>(null)
const creating = ref(false)
const running = ref(false)
const runningAll = ref(false)
const runningOutstanding = ref(false)
const generatingFindings = ref(false)
const redrafting = ref(false)
const anchorOpen = ref(false)
const anchor = ref<EvidenceRef | null>(null)
// Bulk sign-off. A sampled test is 25 items the runner mostly got right, and
// opening each one to agree with it is the slowest part of the worklist.
const selectedIds = ref<string[]>([])
const bulkBusy = ref(false)

// The bar counts every entry, not the filtered worklist: a count that shrank
// as you filtered by it could never be clicked back out of.
const status = computed(() => docTestStatus(summary.value, planning.value?.findings ?? []))
const headline = computed(() => docTestHeadline(summary.value))
const statusBusy = computed(() =>
  runningAll.value || runningOutstanding.value || generatingFindings.value)
/** The list header's `Select` toggle. Checkboxes appear only once asked for. */
const selecting = ref(false)
// Folded rather than combined: each narrowing runs the same predicate over
// what the last one left, so the filters compose without a second code path.
const statusScope = computed(() => statusFilter.value.reduce(
  (entries, key) => filterDocTestEntries(entries, key, planning.value?.findings ?? []),
  summary.value?.entries ?? [],
))
const visibleItems = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return statusScope.value
  return statusScope.value.filter(item => {
    const values = item.entry_type === 'cycle_test'
      ? [item.title, item.assurance_label, item.rcm_id ?? '']
      : [item.label, item.test_title, item.instruction, item.question, item.response]
    return values.some(value => value.toLowerCase().includes(query))
  })
})
// Cycle tests are dispositioned in their own grid, so only item rows are
// selectable here — a mixed selection would need two different mutations.
const selectableItems = computed<DocTestSummaryItem[]>(() =>
  visibleItems.value.filter((entry): entry is DocTestSummaryItem => entry.entry_type === 'item'))
const selectedItems = computed<DocTestSummaryItem[]>(() =>
  selectableItems.value.filter(entry => selectedIds.value.includes(entry.item_id)))
const allSelected = computed(() =>
  selectableItems.value.length > 0 && selectedItems.value.length === selectableItems.value.length)

function toggleSelected(itemId: string) {
  selectedIds.value = selectedIds.value.includes(itemId)
    ? selectedIds.value.filter(value => value !== itemId)
    : [...selectedIds.value, itemId]
}
function toggleSelectAll() {
  selectedIds.value = allSelected.value ? [] : selectableItems.value.map(entry => entry.item_id)
}
const currentItem = computed<DocTestItem | null>(() =>
  currentTest.value?.items.find(item => item.id === selectedItemId.value) ?? null)
const selectedCycleEntry = computed<DocTestSummaryCycleTest | null>(() => {
  const entry = summary.value?.entries.find(item =>
    item.entry_type === 'cycle_test' && item.test_id === selectedCycleTestId.value,
  )
  return entry?.entry_type === 'cycle_test' ? entry : null
})
const selectedEntryId = computed(() =>
  selectedCycleTestId.value
    ? selectedCycleTestId.value
    : selectedItemId.value)
const linkedFindings = computed<AuditFinding[]>(() => {
  const testId = currentTest.value?.id ?? selectedCycleTestId.value
  return testId ? (planning.value?.findings ?? []).filter(finding => finding.test_refs.includes(testId)) : []
})
const assistantUnavailable = computed(() => agent.isActive.value || assistantChat.state.busy)
const hasTests = computed(() => Boolean(summary.value?.test_counts.total))
const allTestIds = computed(() => {
  const ids = new Set<string>()
  for (const entry of summary.value?.entries ?? []) ids.add(entry.test_id)
  return Array.from(ids)
})
// The tests that ran, found something, and are still waiting on the finding
// that says so. Drafting is per RCM row — the same scope the single-test
// button uses — so the row behind each test is what the batch names.
const findingsPending = computed(() => {
  const drafted = new Set((planning.value?.findings ?? []).flatMap(finding => finding.test_refs))
  const rows = new Map<string, string>()
  for (const entry of summary.value?.entries ?? []) {
    if (entry.test_status !== 'completed_with_exception') continue
    if (!entry.rcm_id || drafted.has(entry.test_id)) continue
    rows.set(entry.test_id, entry.rcm_id)
  }
  return rows
})
const outstandingTestIds = computed(() => {
  const ids = new Set<string>()
  for (const entry of summary.value?.entries ?? []) {
    if (entry.classification !== 'confirmed') ids.add(entry.test_id)
  }
  return Array.from(ids)
})

function entryId(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.test_id : entry.item_id
}

function fail(summary_: string, error: unknown) {
  toast.add({ severity: 'error', summary: summary_, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function loadSummary() {
  const previousEntryId = selectedEntryId.value
  const preserveCycleDetail = Boolean(
    selectedCycleTestId.value && currentTest.value?.kind === 'cycle_vouch' && selectedItemId.value,
  )
  summary.value = await api.get<DocTestSummaryPayload>(`/api/workspaces/${props.workspace.id}/doc-tests/summary`)
  const items = summary.value.entries
  // A deep link can point to a test the current filter hides. Keep
  // the requested test visible instead of letting the selection watcher
  // replace it with the first item that happens to match.
  const requested = requestedTestId.value
    ? items.find(item => item.test_id === requestedTestId.value)
    : undefined
  if (requested && statusFilter.value.length && !statusScope.value.includes(requested)) {
    statusFilter.value = []
  }
  // Honour a deep link first, then keep the current selection, then fall back
  // to the most severe item so the tab opens on work that needs doing.
  const target = items.find(item => item.entry_type === 'item' && item.item_id === selectedItemId.value)
    ?? (requestedTestId.value ? items.find(item => item.test_id === requestedTestId.value) : undefined)
    ?? (previousEntryId ? items.find(item => entryId(item) === previousEntryId) : undefined)
    ?? items[0]
  requestedTestId.value = null
  if (target) await select(target, preserveCycleDetail)
  else {
    selectedItemId.value = null
    selectedCycleTestId.value = null
    currentTest.value = null
  }
}
async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function loadPlanning() {
  planning.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
}
async function loadMeta() {
  const meta = await api.get<{ document_types: string[]; cycle_vouch: CycleVouchMetadata }>(
    `/api/workspaces/${props.workspace.id}/doc-tests/meta`,
  )
  documentTypes.value = meta.document_types
  cycleMetadata.value = meta.cycle_vouch
}
async function loadTest(testId: string, itemId?: string | null) {
  currentTest.value = await api.get<DocTest>(`/api/workspaces/${props.workspace.id}/doc-tests/${testId}`)
  const requestedItem = itemId ?? selectedItemId.value
  // Cycle detail is opened from an exact grid row. It must never fall through
  // to a different item's evidence when the requested identity is stale.
  if (currentTest.value.kind === 'cycle_vouch') {
    selectedItemId.value = currentTest.value.items.some(item => item.id === requestedItem)
      ? requestedItem
      : null
    return
  }
  // Non-cycle work remains item-first and recovers to its first current item.
  selectedItemId.value = currentTest.value.items.some(item => item.id === requestedItem)
    ? requestedItem
    : currentTest.value.items[0]?.id ?? null
}
async function select(item: DocTestSummaryEntry, preserveCycleDetail = false) {
  if (item.entry_type === 'cycle_test') {
    const sameOpenDetail = preserveCycleDetail
      && selectedCycleTestId.value === item.test_id
      && currentTest.value?.kind === 'cycle_vouch'
      && selectedItemId.value
    selectedCycleTestId.value = item.test_id
    if (!sameOpenDetail) {
      selectedItemId.value = null
      currentTest.value = null
      focusedAssertionKey.value = null
    }
  } else {
    selectedCycleTestId.value = null
    focusedAssertionKey.value = null
    selectedItemId.value = item.item_id
    if (currentTest.value?.id !== item.test_id) await loadTest(item.test_id, item.item_id)
  }
  await syncUrl()
}
async function syncUrl() {
  await nav.replace('doc-tests', {
    test: currentTest.value?.id ?? selectedCycleTestId.value,
    item: selectedItemId.value || undefined,
  })
}
async function refresh() {
  const testId = currentTest.value?.id
  const itemId = selectedItemId.value
  await loadSummary()
  if (testId && currentTest.value?.id === testId) await loadTest(testId, itemId)
  if (selectedCycleTestId.value) await cycleGrid.value?.loadGrid()
  emit('changed')
}

async function openCycleDetail(itemId: string, assertionKey: string | null) {
  const testId = selectedCycleTestId.value
  if (!testId) return
  try {
    focusedAssertionKey.value = assertionKey
    await loadTest(testId, itemId)
    if (!currentItem.value) throw new Error('The selected cycle item is no longer current.')
    await syncUrl()
  } catch (error) { fail('Could not load the cycle item detail', error) }
}

async function closeCycleDetail() {
  selectedItemId.value = null
  currentTest.value = null
  focusedAssertionKey.value = null
  await syncUrl()
  await nextTick()
  cycleGrid.value?.focusSelectedCell()
}

async function closeCycleGrid() {
  selectedCycleTestId.value = null
  selectedItemId.value = null
  currentTest.value = null
  focusedAssertionKey.value = null
  await syncUrl()
}

/**
 * The lanes name what they want done; the tab still owns how. Both routes reuse
 * the batch paths the header buttons already use, scoped to what the lane
 * counted rather than to everything outstanding.
 */
/**
 * The chips narrow the worklist, so applying one from inside a cycle grid has
 * to go back to the list it narrows — otherwise the click lands on a view that
 * cannot show the result and appears to do nothing.
 */
function pickStatusFilter(value: DocTestFilter[]) {
  statusFilter.value = value
  if (value.length && selectedCycleTestId.value) {
    selectedCycleTestId.value = null
    selectedItemId.value = null
    currentTest.value = null
    focusedAssertionKey.value = null
  }
}
function toggleSelecting() {
  selecting.value = !selecting.value
  if (!selecting.value) selectedIds.value = []
}
function emptyDocDraft(): DocTestDraft {
  return {
    title: '', rcmId: requestedRcmId, table: '', size: 10, seed: 42,
    frozenFields: [], identifierFields: [], requiredDocumentTypes: [],
    evidenceAware: true, attributes: [], documentId: '', pages: '', questions: '',
    procedureKey: 'cycle-vouch', selectionMode: 'evidence_linked',
    sampleMethod: 'random', stratifyBy: '',
  }
}
function openCreate() {
  createShape.value = 'vouching'
  createDraft.value = emptyDocDraft()
  createOpen.value = true
}
/**
 * The drawer's primary runs the test it just wrote; `Save only` leaves it
 * ready. Running a document test is an assistant run rather than a request, so
 * "run" here means starting one.
 */
async function saveDefinition(thenRun: boolean) {
  const payload = createForm.value?.payload()
  if (!payload) return
  const created = await createTest(payload)
  if (created && thenRun) await runTestIds([created.id], runningAll, 'Could not start the document test')
}

async function createTest({ kind, direction, draft }: {
  kind: DocTestKind
  direction: string
  draft: {
    title: string; rcmId: string; table: string; size: number; seed: number
    frozenFields: string[]; identifierFields: string[]; requiredDocumentTypes: string[]
    evidenceAware: boolean; attributes: string[]; documentId: string; pages: string; questions: string
    procedureKey: string
    cycleRulesetDefinition?: CycleRulesetDefinition
    requirementRefs?: string[]
  }
}) {
  creating.value = true
  try {
    const common = {
      title: draft.title,
      rcm_id: draft.rcmId || null,
      rcm_refs: draft.rcmId ? [draft.rcmId] : [],
    }
    let created: DocTest
    if (kind === 'cycle_vouch') {
      if (!draft.cycleRulesetDefinition) {
        throw new Error('The Cycle vouch definition is incomplete.')
      }
      const result = await api.post<DocTest | { status: 'selection_confirmation'; selection_confirmation: { reason: string } }>(
        `/api/workspaces/${props.workspace.id}/doc-tests/build/cycle-vouch`,
        {
          ...common,
          objective: 'Vouch each selected transaction against the approved cycle rules.',
          requirement_refs: draft.requirementRefs ?? [],
          procedure_key: draft.procedureKey,
          definition: draft.cycleRulesetDefinition,
        },
      )
      if ('selection_confirmation' in result) {
        throw new Error(result.selection_confirmation.reason)
      }
      created = result as DocTest
    } else if (kind === 'vouching') {
      const path = draft.evidenceAware ? 'prepare-evidence-aware' : 'build/vouching'
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/${path}`, {
        ...common,
        table: draft.table,
        direction,
        size: draft.size,
        seed: draft.seed,
        frozen_fields: draft.frozenFields,
        identifier_fields: draft.identifierFields,
        required_document_types: draft.requiredDocumentTypes,
      })
    } else if (kind === 'attribute') {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/attribute`, {
        ...common,
        document_ids: [draft.documentId],
        attributes: draft.attributes.map(name => ({ name, expected: 'present' })),
      })
    } else if (kind === 'review') {
      const pages = draft.pages.split(',').map(value => Number(value.trim())).filter(Boolean)
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/review`, {
        ...common,
        document_id: draft.documentId,
        ...(pages.length ? { pages } : {}),
      })
    } else if (kind === 'qa') {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/qa`, {
        ...common,
        document_ids: draft.documentId ? [draft.documentId] : [],
        questions: draft.questions.split('\n').map(value => value.trim()).filter(Boolean),
      })
    } else {
      throw new Error('Cycle vouch authoring is introduced by the canonical Phase 2 builder.')
    }
    createOpen.value = false
    requestedTestId.value = created.id
    selectedItemId.value = null
    // A new test has run nothing yet, so any active narrowing would hide it.
    statusFilter.value = []
    await loadSummary()
    emit('changed')
    return created
  } catch (error) { fail('Could not create the document test', error) }
  finally { creating.value = false }
  return null
}

async function attachDocument(documentId: string) {
  if (!currentTest.value || !currentItem.value) return
  try {
    await api.post(
      `/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}/items/${currentItem.value.id}/documents`,
      { document_id: documentId },
    )
    await refresh()
  } catch (error) { fail('Could not attach the document', error) }
}
async function saveChecks() {
  if (!currentTest.value || !currentItem.value) return
  try {
    await api.patch(
      `/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}/items/${currentItem.value.id}/comparisons`,
      { checks: currentItem.value.checks ?? [] },
    )
    await refresh()
    toast.add({ severity: 'success', summary: 'Matching rules saved', life: 1800 })
  } catch (error) { fail('Could not save the matching rules', error) }
}
async function saveAttributes() {
  if (!currentTest.value || !currentItem.value) return
  try {
    await api.patch(
      `/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}/items/${currentItem.value.id}`,
      { attributes: currentItem.value.attributes ?? [] },
    )
    await refresh()
    toast.add({ severity: 'success', summary: 'Attribute notes saved', life: 1800 })
  } catch (error) { fail('Could not save the attribute notes', error) }
}
async function setItemState(state: DocTestDispositionState, note?: string) {
  if (!currentTest.value || !currentItem.value) return
  try {
    await api.patch(
      `/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}/items/${currentItem.value.id}`,
      { state, ...(note ? { disposition_note: note } : {}) },
    )
    await refresh()
    toast.add({
      severity: 'success',
      summary: state === 'pending' ? 'Your call cleared' : 'Your call recorded',
      life: 1800,
    })
  } catch (error) { fail('Could not record your call', error) }
}

async function setSelectedStates(state: DocTestDispositionState) {
  const dispositions = selectedItems.value.map(entry => ({
    test_id: entry.test_id, item_id: entry.item_id, state,
  }))
  if (!dispositions.length) return
  bulkBusy.value = true
  try {
    await api.post(
      `/api/workspaces/${props.workspace.id}/doc-tests/dispositions`,
      { dispositions },
    )
    selectedIds.value = []
    await refresh()
    toast.add({
      severity: 'success',
      summary: `Recorded on ${dispositions.length} item${dispositions.length > 1 ? 's' : ''}`,
      life: 1800,
    })
  } catch (error) { fail('Could not record your call on the selected items', error) }
  finally { bulkBusy.value = false }
}
async function saveConclusion() {
  if (!currentTest.value) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}`, {
      conclusion: currentTest.value.conclusion,
      control_conclusion: currentTest.value.control_conclusion,
    })
    await refresh()
    toast.add({ severity: 'success', summary: 'Conclusion saved', life: 1800 })
  } catch (error) { fail('Could not save the conclusion', error) }
}
async function updateEvidenceRequest(requestId: string, status: 'received' | 'cancelled') {
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/evidence-requests/${requestId}`, {
      status,
      auditor_note: status === 'received' ? 'Evidence reviewed by auditor.' : 'Request no longer required.',
    })
    await refresh()
    toast.add({
      severity: 'success',
      summary: status === 'received' ? 'Evidence request cleared' : 'Evidence request cancelled',
      life: 1800,
    })
  } catch (error) { fail('Could not update the evidence request', error) }
}
async function runTest() {
  const testId = currentTest.value?.id ?? selectedCycleEntry.value?.test_id
  if (!testId) return
  running.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Run document test ${testId} and preserve its results.`,
      'act', launchMode.value,
      { command: 'run_document_tests', source: 'tab_button', runContext: { test_id: testId } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({ severity: 'info', summary: 'Document test started', detail: 'Progress is visible in the assistant.', life: 3000 })
  } catch (error) { fail('Could not start the document test', error) }
  finally { running.value = false }
}
async function runTestIds(testIds: string[], busy: typeof runningAll, failSummary: string) {
  if (!testIds.length) return
  busy.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Run all ${testIds.length} Document Test${testIds.length === 1 ? '' : 's'} and preserve the results.`,
      'act', launchMode.value,
      { command: 'run_document_tests', source: 'tab_button', runContext: { test_ids: testIds } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'info',
      summary: `Running ${testIds.length} Document Test${testIds.length === 1 ? '' : 's'}`,
      detail: 'Progress is visible in the assistant.',
      life: 4000,
    })
  } catch (error) { fail(failSummary, error) }
  finally { busy.value = false }
}
async function runAllTests() {
  await runTestIds(allTestIds.value, runningAll, 'Could not start the document tests')
}
async function runOutstandingTests() {
  await runTestIds(outstandingTestIds.value, runningOutstanding, 'Could not start the outstanding document tests')
}
async function generateFinding(regenerate: boolean) {
  const test = currentTest.value
  if (!test?.rcm_id || !test.status.startsWith('completed')) return
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `${regenerate ? 'Regenerate' : 'Draft'} findings for RCM row ${test.rcm_id}.`,
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button', runContext: { rcm_id: test.rcm_id } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({ severity: 'success', summary: regenerate ? 'Finding regeneration started' : 'Finding-draft workflow started', detail: 'Exception observations will be used directly.', life: 3600 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
async function draftPendingFindings(testIds?: string[]) {
  const scope = testIds?.length
    ? [...findingsPending.value].filter(([testId]) => testIds.includes(testId))
    : [...findingsPending.value]
  const count = scope.length
  const rcmIds = [...new Set(scope.map(([, rcmId]) => rcmId))]
  if (!rcmIds.length) return
  generatingFindings.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Draft findings for ${rcmIds.length} RCM row${rcmIds.length === 1 ? '' : 's'} with undrafted exceptions.`,
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button', runContext: { rcm_ids: rcmIds } },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'success',
      summary: `Generating findings for ${count} test${count === 1 ? '' : 's'}`,
      detail: 'Exception observations will be used directly.',
      life: 3600,
    })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
  finally { generatingFindings.value = false }
}
function openFinding(findingId: string) {
  void nav.replace('findings', { finding: findingId })
}
// Redrafting *this* test, not the row it sits on. The row's other tests are
// work someone is already relying on, and regenerating them because a
// neighbour reads badly is the behaviour that made this action unaskable
// before the request could name a test.
async function redraftTest() {
  const test = currentTest.value
  const testId = test?.id ?? selectedCycleEntry.value?.test_id
  if (!testId) return
  redrafting.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Document test ${testId} does not look right; redraft it.`,
      'act', launchMode.value,
      {
        source: 'tab_button',
        requestedOutcomes: ['tests.specified'],
        runContext: {
          target_refs: [`doctest:${testId}`],
          generation_mode: 'force',
        },
      },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'success',
      summary: 'Redraft started',
      detail: `${testId} will be rewritten; the row's other tests are left as they are.`,
      life: 3600,
    })
  } catch (error) { fail('Could not start the redraft', error) }
  finally { redrafting.value = false }
}
function deleteTest() {
  const test = currentTest.value
  const cycle = selectedCycleEntry.value
  const testId = test?.id ?? cycle?.test_id
  const testTitle = test?.title ?? cycle?.title
  const itemCount = test?.item_count ?? test?.items.length ?? cycle?.item_count ?? 0
  if (!testId) return
  const itemText = itemCount === 1 ? '1 worklist item' : `${itemCount} worklist items`
  confirm.require({
    header: 'Delete document test',
    message: `Delete "${testTitle}" and its ${itemText}? This cannot be undone.`,
    icon: 'pi pi-trash',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/doc-tests/${testId}`)
        selectedItemId.value = null
        selectedCycleTestId.value = null
        currentTest.value = null
        await loadSummary()
        emit('changed')
        toast.add({ severity: 'success', summary: 'Document test deleted', life: 1800 })
      } catch (error) { fail('Could not delete the document test', error) }
    },
  })
}
async function prepareTests() {
  if (assistantUnavailable.value) {
    toast.add({
      severity: 'info', summary: 'The assistant is already working',
      detail: 'Finish or cancel the active run before preparing another batch.',
      life: 3000,
    })
    return
  }
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Write the executable specification for each drafted Document Test, prioritizing imported evidence-covered transactions and creating explicit evidence requests for missing support.',
      'act', launchMode.value, { command: 'prepare_document_tests', source: 'tab_button' },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({ severity: 'info', summary: 'Preparing document tests', detail: 'Review progress in the assistant.', life: 3000 })
  } catch (error) { fail('Could not start document test preparation', error) }
}
function openRcm(rcmId: string) {
  void nav.replace('rcm', { rcm: rcmId })
}
function showAnchor(value: EvidenceRef) {
  anchor.value = value
  anchorOpen.value = true
}

/**
 * Running is one act with two scopes, so it is one split button rather than
 * two. `Run outstanding` was a second full-width button for the narrower case
 * of the same thing, and the header had six of these.
 */
const runOptions = computed(() => [
  {
    label: `Run all ${allTestIds.value.length}`,
    icon: 'pi pi-play',
    disabled: assistantUnavailable.value || !allTestIds.value.length,
    command: () => void runAllTests(),
  },
  {
    label: `Run ${plural(outstandingTestIds.value.length, 'outstanding test')}`,
    icon: 'pi pi-forward',
    disabled: assistantUnavailable.value || !outstandingTestIds.value.length,
    command: () => void runOutstandingTests(),
  },
])
// The occasional actions, behind one menu. Cycle rules is read once per
// engagement; deleting is rarer still and destructive. Drafting the findings
// the file still owes is here rather than in the header because the write-up
// is offered on each test's own footer row, where the exception is read — the
// batch is the shortcut, not the route.
const menuItems = computed(() => [
  ...(findingsPending.value.size
    ? [{
        label: `Draft ${plural(findingsPending.value.size, 'finding')}`,
        icon: 'pi pi-flag',
        disabled: statusBusy.value || assistantUnavailable.value,
        command: () => void draftPendingFindings(),
      }]
    : []),
  {
    label: 'Cycle rules',
    icon: 'pi pi-sitemap',
    command: () => { rulesetReviewOpen.value = true },
  },
  {
    label: 'Redraft this test',
    icon: 'pi pi-pencil',
    disabled:
      assistantUnavailable.value
      || (!currentTest.value && !selectedCycleEntry.value),
    command: () => void redraftTest(),
  },
  {
    label: 'Delete this test',
    icon: 'pi pi-trash',
    disabled: !currentTest.value && !selectedCycleEntry.value,
    command: () => deleteTest(),
  },
])

// A filter change can hide the selected item; move to the first visible one.
watch(visibleItems, items => {
  if (!items.length || items.some(item => entryId(item) === selectedEntryId.value)) return
  void select(items[0])
})

onMounted(() => {
  if (createRequested) openCreate()
  void Promise.all([loadSummary(), loadDocuments(), loadPlanning(), loadMeta()])
    .catch(error => fail('Could not load document tests', error))
})
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void Promise.all([loadSummary(), loadDocuments(), loadPlanning()])
    .catch(error => fail('Could not refresh document tests', error))
})
onUnmounted(unsubscribe)
const rulesetReviewOpen = ref(false)
const session = useSession()

/** Approving is an auditor act, so it is signed with who they are. The route
 *  refuses an anonymous approval rather than recording rules as approved by
 *  nobody. */
const approverId = computed(() => session.user.value?.email ?? '')

function onRulesetApproved(): void {
  rulesetReviewOpen.value = false
  toast.add({
    severity: 'success',
    summary: 'Cycle rules approved',
    detail: 'Results will be produced under these rules.',
    life: 3200,
  })
}

</script>

<template>
  <div class="doc-tests">
    <!-- One title, one count sentence, at most one primary. The six buttons
         and a delete icon this row used to carry are a split button, a
         secondary and a kebab. -->
    <header class="page-head">
      <h1>Document tests</h1>
      <p class="headline aw-figure">{{ headline }}</p>
      <span class="grow" />
      <Button label="New test" icon="pi pi-plus" size="small" outlined severity="secondary" @click="openCreate" />
      <SplitButton
        v-if="hasTests"
        label="Run"
        icon="pi pi-play"
        size="small"
        outlined
        severity="secondary"
        :model="runOptions"
        :loading="runningAll || runningOutstanding"
        :disabled="assistantUnavailable || !allTestIds.length"
        @click="runAllTests"
      />
      <Button
        v-if="hasTests"
        label="Prepare with assistant"
        icon="pi pi-sparkles"
        size="small"
        :disabled="assistantUnavailable"
        @click="prepareTests"
      />
      <UiOverflowMenu :items="menuItems" tooltip="More document test actions" />
    </header>

    <UiReviewBar
      v-if="hasTests"
      :lanes="status.lanes"
      :chips="DOC_TEST_CHIPS"
      :filters="status.filters"
      allLabel="All items"
      :total="summary?.entries.length ?? 0"
      :filter="statusFilter"
      @filter="pickStatusFilter($event as DocTestFilter[])"
    />

    <template v-if="hasTests">
      <div v-if="selectedCycleTestId" class="cycle-review">
        <CycleVouchGrid
          v-show="!currentItem"
          :key="selectedCycleTestId"
          ref="cycleGrid"
          :workspaceId="workspace.id"
          :testId="selectedCycleTestId"
          :running="running"
          :busy="agent.isActive.value"
          :metadata="cycleMetadata"
          @close="closeCycleGrid"
          @error="fail"
          @openDetail="openCycleDetail"
          @run="runTest"
          @changed="refresh"
        />
        <template v-if="currentTest && currentItem">
          <div class="detail-return">
            <Button label="Back to Cycle vouch grid" icon="pi pi-arrow-left" text @click="closeCycleDetail" />
            <span v-if="focusedAssertionKey">Opened at assertion <code>{{ focusedAssertionKey }}</code></span>
          </div>
          <DocTestItemDetail
            :key="currentItem.id"
            :test="currentTest"
            :workspaceId="workspace.id"
            :item="currentItem"
            :documents="documents"
            :findings="linkedFindings"
            :running="running"
            :busy="agent.isActive.value"
            :focusAssertionKey="focusedAssertionKey"
            @anchor="showAnchor"
            @attach="attachDocument"
            @saveChecks="saveChecks"
            @saveAttributes="saveAttributes"
            @setState="setItemState"
            @saveConclusion="saveConclusion"
            @generateFinding="generateFinding"
            @openFinding="openFinding"
            @updateEvidenceRequest="updateEvidenceRequest"
            @run="runTest"
            @openRcm="openRcm"
          />
        </template>
      </div>

      <div v-else class="layout">
        <section class="list-panel">
          <div class="list-head">
            <IconField>
              <InputIcon class="pi pi-search" />
              <InputText v-model="search" size="small" placeholder="Search items and answers" />
            </IconField>
            <button type="button" class="select-toggle" :aria-pressed="selecting" @click="toggleSelecting">
              {{ selecting ? 'Done' : 'Select' }}
            </button>
          </div>
          <div class="list-body">
            <DocTestItemList
              :items="visibleItems"
              :selectedId="selectedEntryId"
              :checkedIds="selectedIds"
              :selecting="selecting"
              @select="select"
              @toggle="toggleSelected"
            />
          </div>
          <button
            v-if="selecting && selectableItems.length"
            type="button"
            class="select-all"
            @click="toggleSelectAll"
          >
            {{ allSelected ? 'Clear selection' : `Select all ${selectableItems.length}` }}
          </button>
        </section>

        <DocTestItemDetail
          v-if="currentTest && currentItem"
          :key="currentItem.id"
          :test="currentTest"
          :workspaceId="workspace.id"
          :item="currentItem"
          :documents="documents"
          :findings="linkedFindings"
          :running="running"
          :busy="agent.isActive.value"
          @anchor="showAnchor"
          @attach="attachDocument"
          @saveChecks="saveChecks"
          @saveAttributes="saveAttributes"
          @setState="setItemState"
          @saveConclusion="saveConclusion"
          @generateFinding="generateFinding"
          @openFinding="openFinding"
          @updateEvidenceRequest="updateEvidenceRequest"
          @run="runTest"
          @openRcm="openRcm"
        >
          <!-- One call across a selection is the same decision as one call on
               one item, so it stands where that decision is made rather than
               as a second bar above the list. -->
          <template v-if="selectedItems.length" #verdict>
            <div class="bulk-bar" role="group" aria-label="Bulk sign-off">
              <span class="bulk-count">
                {{ selectedItems.length }} item{{ selectedItems.length > 1 ? 's' : '' }} selected
              </span>
              <Button
                label="Confirm"
                icon="pi pi-check"
                size="small"
                severity="success"
                outlined
                :disabled="bulkBusy || agent.isActive.value"
                @click="setSelectedStates('confirmed')"
              />
              <Button
                label="Exception"
                icon="pi pi-exclamation-triangle"
                size="small"
                severity="danger"
                outlined
                :disabled="bulkBusy || agent.isActive.value"
                @click="setSelectedStates('exception')"
              />
              <Button
                label="Needs review"
                icon="pi pi-eye"
                size="small"
                severity="warn"
                outlined
                :disabled="bulkBusy || agent.isActive.value"
                @click="setSelectedStates('needs_review')"
              />
              <Button
                label="Clear calls"
                icon="pi pi-refresh"
                size="small"
                text
                :disabled="bulkBusy || agent.isActive.value"
                @click="setSelectedStates('pending')"
              />
            </div>
          </template>
        </DocTestItemDetail>
        <UiEmptyState
          v-else-if="!visibleItems.length"
          icon="pi pi-check-circle"
          title="Nothing in this view"
          description="Pick All items above to review every worklist item."
        />
        <UiEmptyState v-else icon="pi pi-verified" title="Loading item" description="Opening the selected worklist item." />
      </div>
    </template>

    <UiEmptyState
      v-else
      icon="pi pi-verified"
      title="Prepare document fieldwork"
      description="Create document tests for the RCM rows they cover, prioritising transactions that already have imported evidence."
    >
      <Button label="Prepare with assistant" icon="pi pi-sparkles" :disabled="assistantUnavailable" @click="prepareTests" />
      <Button label="New test" icon="pi pi-plus" severity="secondary" outlined @click="openCreate" />
    </UiEmptyState>

    <!-- The same drawer the data tests author in, with the shape picker as
         its first section: the two-step stepper chose the shape on a screen
         that could not show the scope fields the shape decides. -->
    <UiDefinitionDrawer
      v-model="createOpen"
      eyebrow="New test"
      :title="createDraft.title || 'Untitled document test'"
      :ready="createReady"
      :saving="creating"
      :running="runningAll"
      @save="saveDefinition"
    >
      <DocTestDefinitionForm
        ref="createForm"
        v-model="createDraft"
        v-model:shape="createShape"
        :workspace="workspace"
        :documents="documents"
        :planning="planning"
        :documentTypes="documentTypes"
        :cycleMetadata="cycleMetadata"
        @valid="createReady = $event"
        @error="fail"
      />
    </UiDefinitionDrawer>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" :documents="documents" />
    <CycleRulesetReview
      v-model="rulesetReviewOpen"
      :workspace-id="props.workspace.id"
      :approver-id="approverId"
      @approved="onRulesetApproved"
      @error="(summary, error) => fail(summary, error)"
    />
  </div>
</template>

<style scoped>
.doc-tests { display: flex; flex-direction: column; gap: .75rem; min-width: 0; min-height: 0; height: 100%; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

.layout { display: grid; grid-template-columns: 18.75rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { display: flex; align-items: center; gap: .5rem; padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield) { flex: 1; min-width: 0; }
.list-head :deep(.p-inputtext) { width: 100%; }
.select-toggle, .select-all {
  flex: none; padding: .25rem .4rem; border: 0; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.select-toggle:hover, .select-all:hover { color: var(--aw-teal); background: var(--aw-teal-soft); }
.select-toggle[aria-pressed='true'] { color: var(--aw-teal); }
.select-all { padding: .5rem .75rem; border-top: 1px solid var(--aw-border); border-radius: 0; text-align: left; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

/* The detail column is its own container so the panels inside it size against
   the space they have, not against the window. */
.layout > :deep(.detail) { container: master-detail-content / inline-size; overflow-y: auto; }

/* Where the verdict bar would be, in the same box, so a bulk selection reads
   as the same decision at a different scope. */
.bulk-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
  min-width: 0; padding: .625rem .875rem;
  border: 1px solid var(--aw-teal); border-radius: var(--aw-radius-surface);
  background: var(--aw-teal-soft);
}
.bulk-count { margin-right: auto; font-size: var(--aw-text-sm); font-weight: 600; }

.cycle-review { min-width: 0; }
.detail-return { display: flex; align-items: center; justify-content: space-between; gap: .7rem; margin-bottom: .55rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.detail-return code { font-family: var(--aw-font-mono); }

@container workspace-panel (max-width: 60rem) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .list-body { max-height: 18rem; }
}
</style>
