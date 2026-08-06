<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  AuditDocument,
  AuditFinding,
  DocTest,
  DocTestClassification,
  DocTestItem,
  DocTestKind,
  DocTestSummaryItem,
  DocTestSummaryPayload,
  EvidenceRef,
  PlanningPayload,
  WorkspaceSummary,
} from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import DocTestCreateDialog from './doc-tests/DocTestCreateDialog.vue'
import DocTestItemDetail from './doc-tests/DocTestItemDetail.vue'
import DocTestItemList from './doc-tests/DocTestItemList.vue'
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
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { launchMode } = agent

const summary = ref<DocTestSummaryPayload | null>(null)
const documents = ref<AuditDocument[]>([])
const planning = ref<PlanningPayload | null>(null)
const documentTypes = ref<string[]>([])
const currentTest = ref<DocTest | null>(null)
const selectedItemId = ref<string | null>(String(route.query.item || '') || null)
const requestedTestId = ref<string | null>(String(route.query.test || '') || null)
// Read the deep link before any selection rewrites the query, otherwise the
// RCM grid's "Add Document Test" link loses its own parameters.
const createRequested = route.query.create === '1'
const requestedRcmId = String(route.query.rcm || '')
const filter = ref<'all' | DocTestClassification>('all')
const search = ref('')
const createOpen = ref(false)
const creating = ref(false)
const running = ref(false)
const anchorOpen = ref(false)
const anchor = ref<EvidenceRef | null>(null)

// The triage cards are the only filter. "Needs action" used to be a second
// control over the same axis, and it was exactly the union of the three cards
// beside it — the counts say the same thing without the extra row.
const triage = computed<TriageCount[]>(() => {
  const counts = summary.value?.counts
  return [
    { key: 'all', label: 'All items', value: summary.value?.items.length ?? 0 },
    { key: 'exception', label: 'Exceptions', value: counts?.exception ?? 0, tone: 'danger' },
    { key: 'needs_review', label: 'Need review', value: counts?.needs_review ?? 0, tone: 'warn' },
    { key: 'awaiting_evidence', label: 'Awaiting evidence', value: counts?.awaiting_evidence ?? 0, tone: 'warn' },
    { key: 'confirmed', label: 'Confirmed', value: counts?.confirmed ?? 0, tone: 'ok' },
    { key: 'not_run', label: 'Not run', value: counts?.not_run ?? 0 },
  ]
})
const visibleItems = computed(() => {
  const items = summary.value?.items ?? []
  const query = search.value.trim().toLowerCase()
  return items.filter(item => {
    if (filter.value !== 'all' && item.classification !== filter.value) return false
    if (!query) return true
    return [item.label, item.test_title, item.instruction, item.question, item.response]
      .some(value => value.toLowerCase().includes(query))
  })
})
const activeFilterLabel = computed(() =>
  triage.value.find(count => count.key === filter.value)?.label.toLowerCase() ?? 'items')
const currentItem = computed<DocTestItem | null>(() =>
  currentTest.value?.items.find(item => item.id === selectedItemId.value) ?? null)
const linkedFindings = computed<AuditFinding[]>(() => {
  const testId = currentTest.value?.id
  return testId ? (planning.value?.findings ?? []).filter(finding => finding.test_refs.includes(testId)) : []
})
const assistantUnavailable = computed(() => agent.isActive.value || assistantChat.state.busy)
const hasTests = computed(() => Boolean(summary.value?.tests.length))

function fail(summary_: string, error: unknown) {
  toast.add({ severity: 'error', summary: summary_, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function loadSummary() {
  summary.value = await api.get<DocTestSummaryPayload>(`/api/workspaces/${props.workspace.id}/doc-tests/summary`)
  const items = summary.value.items
  // A dashboard/deep link can point to a test the current filter hides. Keep
  // the requested test visible instead of letting the selection watcher
  // replace it with the first item that happens to match.
  const requested = requestedTestId.value
    ? items.find(item => item.test_id === requestedTestId.value)
    : undefined
  if (requested && filter.value !== 'all' && requested.classification !== filter.value) {
    filter.value = 'all'
  }
  // Honour a deep link first, then keep the current selection, then fall back
  // to the most severe item so the tab opens on work that needs doing.
  const target = items.find(item => item.item_id === selectedItemId.value)
    ?? (requestedTestId.value ? items.find(item => item.test_id === requestedTestId.value) : undefined)
    ?? items[0]
  requestedTestId.value = null
  if (target) await select(target)
  else { selectedItemId.value = null; currentTest.value = null }
}
async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function loadPlanning() {
  planning.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
}
async function loadMeta() {
  documentTypes.value = (await api.get<{ document_types: string[] }>(
    `/api/workspaces/${props.workspace.id}/doc-tests/meta`,
  )).document_types
}
async function loadTest(testId: string) {
  currentTest.value = await api.get<DocTest>(`/api/workspaces/${props.workspace.id}/doc-tests/${testId}`)
  // Item-level deep links can outlive a regenerated worklist. Recover to the
  // first current item instead of leaving the detail pane stuck on loading.
  if (!currentTest.value.items.some(item => item.id === selectedItemId.value)) {
    selectedItemId.value = currentTest.value.items[0]?.id ?? null
    await syncUrl()
  }
}
async function select(item: DocTestSummaryItem) {
  selectedItemId.value = item.item_id
  if (currentTest.value?.id !== item.test_id) await loadTest(item.test_id)
  await syncUrl()
}
async function syncUrl() {
  await nav.replace('doc-tests', {
    test: currentTest.value?.id,
    item: selectedItemId.value || undefined,
  })
}
async function refresh() {
  const testId = currentTest.value?.id
  await loadSummary()
  if (testId && currentTest.value?.id === testId) await loadTest(testId)
  emit('changed')
}

function pickFilter(key: string) {
  filter.value = key as 'all' | DocTestClassification
}

async function createTest({ kind, direction, draft }: {
  kind: DocTestKind
  direction: string
  draft: {
    title: string; rcmId: string; table: string; size: number; seed: number
    frozenFields: string[]; identifierFields: string[]; requiredDocumentTypes: string[]
    evidenceAware: boolean; attributes: string[]; documentId: string; pages: string; questions: string
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
    if (kind === 'vouching') {
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
    } else {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/qa`, {
        ...common,
        document_ids: draft.documentId ? [draft.documentId] : [],
        questions: draft.questions.split('\n').map(value => value.trim()).filter(Boolean),
      })
    }
    createOpen.value = false
    requestedTestId.value = created.id
    selectedItemId.value = null
    filter.value = 'all'
    await loadSummary()
    emit('changed')
  } catch (error) { fail('Could not create the document test', error) }
  finally { creating.value = false }
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
async function setItemState(state: 'confirmed' | 'exception' | 'pending') {
  if (!currentTest.value || !currentItem.value) return
  try {
    await api.patch(
      `/api/workspaces/${props.workspace.id}/doc-tests/${currentTest.value.id}/items/${currentItem.value.id}`,
      { state },
    )
    await refresh()
    toast.add({ severity: 'success', summary: 'Auditor sign-off saved', life: 1800 })
  } catch (error) { fail('Could not save the auditor sign-off', error) }
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
  const test = currentTest.value
  if (!test) return
  running.value = true
  try {
    await assistantChat.send(
      `Run document test ${test.id} and preserve its results.`,
      'act', launchMode.value,
      { command: 'run_document_tests', source: 'tab_button', runContext: { test_id: test.id } },
    )
    toast.add({ severity: 'info', summary: 'Document test started', detail: 'Progress is visible in the assistant.', life: 3000 })
  } catch (error) { fail('Could not start the document test', error) }
  finally { running.value = false }
}
async function generateFinding(regenerate: boolean) {
  const test = currentTest.value
  if (!test?.rcm_id || !test.status.startsWith('completed')) return
  try {
    await assistantChat.send(
      `${regenerate ? 'Regenerate' : 'Draft'} findings for RCM row ${test.rcm_id}.`,
      'act', launchMode.value,
      { command: 'draft_findings', goalTemplate: 'finding_draft', source: 'tab_button', runContext: { rcm_id: test.rcm_id } },
    )
    toast.add({ severity: 'success', summary: regenerate ? 'Finding regeneration started' : 'Finding-draft workflow started', detail: 'Exception observations will be used directly.', life: 3600 })
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
function openFinding(findingId: string) {
  void nav.replace('findings', { finding: findingId })
}
function deleteTest() {
  const test = currentTest.value
  if (!test) return
  const itemText = test.item_count === 1 ? '1 worklist item' : `${test.item_count ?? test.items.length} worklist items`
  confirm.require({
    header: 'Delete document test',
    message: `Delete "${test.title}" and its ${itemText}? This cannot be undone.`,
    icon: 'pi pi-trash',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/doc-tests/${test.id}`)
        selectedItemId.value = null
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
    await assistantChat.send(
      'Write the executable specification for each drafted Document Test, prioritizing imported evidence-covered transactions and creating explicit evidence requests for missing support.',
      'act', launchMode.value, { command: 'prepare_document_tests', source: 'tab_button' },
    )
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

// A filter change can hide the selected item; move to the first visible one.
watch(visibleItems, items => {
  if (!items.length || items.some(item => item.item_id === selectedItemId.value)) return
  void select(items[0])
})

onMounted(() => {
  if (createRequested) createOpen.value = true
  void Promise.all([loadSummary(), loadDocuments(), loadPlanning(), loadMeta()])
    .catch(error => fail('Could not load document tests', error))
})
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void Promise.all([loadSummary(), loadDocuments(), loadPlanning()])
    .catch(error => fail('Could not refresh document tests', error))
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="doc-tests">
    <UiPageHeader title="Document tests">
      <Button
        v-if="hasTests"
        label="Prepare with assistant"
        icon="pi pi-sparkles"
        size="small"
        :disabled="assistantUnavailable"
        @click="prepareTests"
      />
      <Button label="New test" icon="pi pi-plus" size="small" outlined @click="createOpen = true" />
      <Button
        v-if="currentTest"
        icon="pi pi-trash"
        severity="danger"
        outlined
        rounded
        size="small"
        aria-label="Delete document test"
        @click="deleteTest"
      />
    </UiPageHeader>

    <template v-if="hasTests">
      <UiTriageCounts :counts="triage" :active="filter" @select="pickFilter" />

      <div class="toolbar">
        <IconField>
          <InputIcon class="pi pi-search" />
          <InputText v-model="search" placeholder="Search items, tests, and answers" />
        </IconField>
        <span class="muted">
          {{ visibleItems.length }} of {{ summary?.items.length ?? 0 }} · {{ activeFilterLabel }}
        </span>
      </div>

      <UiMasterDetail railWidth="20rem" class="layout">
        <template #rail>
          <DocTestItemList :items="visibleItems" :selectedId="selectedItemId" @select="select" />
        </template>
        <DocTestItemDetail
          v-if="currentTest && currentItem"
          :key="currentItem.id"
          :test="currentTest"
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
        />
        <UiEmptyState
          v-else-if="!visibleItems.length"
          icon="pi pi-check-circle"
          title="Nothing in this view"
          description="Pick All items above to review every worklist item."
        />
        <UiEmptyState v-else icon="pi pi-verified" title="Loading item" description="Opening the selected worklist item." />
      </UiMasterDetail>
    </template>

    <UiEmptyState
      v-else
      icon="pi pi-verified"
      title="Prepare document fieldwork"
      description="Create document tests for the RCM rows they cover, prioritising transactions that already have imported evidence."
    >
      <Button label="Prepare with assistant" icon="pi pi-sparkles" :disabled="assistantUnavailable" @click="prepareTests" />
      <Button label="New test" icon="pi pi-plus" severity="secondary" outlined @click="createOpen = true" />
    </UiEmptyState>

    <DocTestCreateDialog
      v-model="createOpen"
      :workspace="workspace"
      :documents="documents"
      :planning="planning"
      :documentTypes="documentTypes"
      :initialRcmId="requestedRcmId"
      :creating="creating"
      @create="createTest"
      @error="fail"
    />
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
  </div>
</template>

<style scoped>
.doc-tests { display: flex; flex-direction: column; gap: 0.8rem; min-width: 0; min-height: 100%; }
.toolbar { display: flex; align-items: center; gap: 0.6rem; min-width: 0; }
.toolbar :deep(.p-iconfield) { flex: 1 1 16rem; min-width: 0; max-width: 26rem; }
.toolbar :deep(.p-inputtext) { width: 100%; }
.muted { color: var(--aw-muted); font-size: 0.76rem; white-space: nowrap; }
.layout { min-height: 32rem; }

/* Sized against the tab's own box, so the assistant drawer taking width
   collapses the two panes at the right moment. */
@container workspace-panel (max-width: 60rem) {
  .toolbar { flex-wrap: wrap; }
  .toolbar :deep(.p-iconfield) { max-width: none; }
}
</style>
