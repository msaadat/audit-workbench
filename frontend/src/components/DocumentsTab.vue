<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { renderAsync } from 'docx-preview'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Drawer from 'primevue/drawer'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import type { MenuItem } from 'primevue/menuitem'
import { api } from '../api'
import { TERMINAL_STATUSES, useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useSession } from '../composables/useSession'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AIActivityEvent, AgentRun, AssistantProvider, AssistantStatus, AuditDocument, DocumentAnalysisCitation, DocumentAnalysisDetail, DocumentCategory, DocumentIndexingStatus, DocumentPage, DocumentSearchResult, DocumentVocabulary, KnowledgePack, WorkspaceSummary } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import MarkdownView from './MarkdownView.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import DocumentTypeReview from './documents/DocumentTypeReview.vue'
import StructuredEvidenceSheet from './documents/StructuredEvidenceSheet.vue'
import {
  DOCUMENT_CHIPS, documentMeta, documentTone, documentsStatus, filterDocuments,
} from './documents/documentsStatus'
import type { DocumentsFilter } from './documents/documentsStatus'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'import-requested': [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const nav = useWorkspaceNav()
const assistantChat = useAssistantChat(props.workspace.id)
const agent = useAgentRun(props.workspace.id)

const documents = ref<AuditDocument[]>([])
const selectedId = ref('')
const previewPages = ref<DocumentPage[]>([])
const currentPage = ref(1)
const view = ref<'preview' | 'analysis' | 'activity'>('preview')
const detailViews = ['preview', 'analysis', 'activity'] as const
const search = ref('')
const statusFilter = ref<DocumentsFilter[]>([])
const groupBy = ref<'type' | 'folder' | 'status'>('type')
const collapsedGroups = ref<Set<string>>(new Set())
const sourceView = ref<'original' | 'text'>('original')
const docxContainer = ref<HTMLElement | null>(null)
const docxLoading = ref(false)
const busy = ref(false)
const classificationBusy = ref(false)
const activity = ref<AIActivityEvent[]>([])
const knowledgeOpen = ref(false)
const packs = ref<KnowledgePack[]>([])
const packSearch = ref('')
const packResults = ref<Array<Record<string, unknown>>>([])
const packInput = ref<HTMLInputElement | null>(null)
const analysis = ref<DocumentAnalysisDetail | null>(null)
const summaryDraft = ref('')
const notesDraft = ref('')
const analysisBusy = ref(false)
const compareCandidate = ref(false)
/** The vocabulary's field table, behind the count that names it. */
const fieldsOpen = ref(false)
const fullVisualCoverage = ref(false)
const session = useSession()
// Single-user installations run as an administrator, so this is transparent
// locally and only bites once real accounts exist.
const canConfigureVision = computed(
  () => session.state.singleUser || session.state.user?.is_admin === true,
)
const visionSettingsOpen = ref(false)
const settingsStatus = ref<AssistantStatus | null>(null)
const visionProvider = ref('')
const visionModel = ref('')
const visionSettingsBusy = ref(false)
const sourceSearch = ref('')
const searchResults = ref<DocumentSearchResult[]>([])
const sourceResults = ref<DocumentSearchResult[]>([])
const searchBusy = ref(false)
const indexingStatus = ref<DocumentIndexingStatus | null>(null)
let indexingTimer: number | undefined
let indexingPollRunning = false
let unsubscribeWorkspaceChanged: (() => void) | undefined

// What the engagement holds a document as. Ordered planning-first so the
// picker reads as the partition it is, with evidence — the one value that puts
// a document under a field schema — last.
const categories: DocumentCategory[] = ['policy', 'minutes', 'background', 'evidence']
const documentCategoryOptions = categories.map(value => ({ value, label: value.replace('_', ' ') }))
/** `fx_contract` -> `fx contract`; `local.broker_note` -> `broker note`. */
function documentTypeLabel(value: string): string {
  return value.replace(/^local\./, '').replace(/_/g, ' ')
}
const visualPageLimit = 20
const visionAvailable = computed(() => agent.state.status?.vision_configured === true)
const hasStructuredSummary = computed(() =>
  analysis.value?.effective?.summary_origin === 'structured_evidence',
)
const providerOptions = computed(() => settingsStatus.value?.providers || [])
const selectedVisionProvider = computed<AssistantProvider | undefined>(() =>
  providerOptions.value.find(provider => provider.id === visionProvider.value),
)
const visionModelOptions = computed(() => {
  const provider = selectedVisionProvider.value
  if (!provider) return []
  return [...new Set([...(provider.models || []), provider.vision_model || ''].filter(Boolean))]
})
const selected = computed(() => documents.value.find(doc => doc.id === selectedId.value) || null)
// Only transaction evidence has a type-level vocabulary to revise. Planning
// material is read as prose and carries no fields for a rule to name.
const isEvidence = computed(() => selected.value?.category === 'evidence')

// What each evidence type is read under. Loaded beside the documents because it
// is a property of the *type*, not of the selected document — the comparison
// that matters is between types, and a one-field dealing ticket is only obvious
// next to a thirteen-field payment instruction.
const vocabulary = ref<DocumentVocabulary[]>([])
const vocabularyByType = computed(
  () => new Map(vocabulary.value.map(item => [item.document_type, item])),
)
const selectedVocabulary = computed(() =>
  vocabularyByType.value.get(String(selected.value?.classification?.document_type || '')) || null,
)

async function loadVocabulary() {
  try {
    const payload = await api.get<{ items: DocumentVocabulary[] }>(
      `/api/workspaces/${props.workspace.id}/documents/vocabulary`,
    )
    vocabulary.value = payload.items || []
  } catch {
    // A missing vocabulary is not worth a toast: the rail still lists the
    // documents, and the panel simply does not render.
    vocabulary.value = []
  }
}

/** Why this type cannot carry a rule, in the terms it would be discovered in. */
function thinReason(item: DocumentVocabulary): string {
  if (item.unread_documents.length) {
    return `${item.unread_documents.length} document(s) of this type could not be read, so its vocabulary is withheld rather than stamped.`
  }
  if (item.documents_read.length < 2) {
    return 'Read from one document, so nothing corroborates its field names.'
  }
  if (!item.corroborated_fields) {
    return 'No field was stated by two documents of this type.'
  }
  if (!item.joinable) {
    return 'No identifier field, so nothing can join this document to another.'
  }
  return 'Only identifier fields, so there is nothing to test once a document is joined.'
}
/** What the review bar reads its counts from, and what the rows read theirs. */
const documentFacts = computed(() => ({
  vocabulary: vocabulary.value,
  visionAvailable: visionAvailable.value,
}))
const status = computed(() => documentsStatus(documents.value, documentFacts.value))
const scoped = computed(() => statusFilter.value.reduce<AuditDocument[]>(
  (rows, key) => filterDocuments(rows, key, documentFacts.value), documents.value,
))
const filtered = computed(() => scoped.value.filter(doc => {
  const term = search.value.toLowerCase()
  return !term || `${doc.title} ${doc.source}`.toLowerCase().includes(term)
}))
const eligibleDocuments = computed(() => documents.value.filter(document =>
  ['extracted', 'partial', 'image_only'].includes(document.text_state)
  && document.analysis_validity_state !== 'current'))

function groupValue(doc: AuditDocument): string {
  if (groupBy.value === 'folder') {
    const path = doc.relative_path || ''
    const cut = path.lastIndexOf('/')
    return cut > 0 ? path.slice(0, cut) : 'Direct uploads'
  }
  return groupBy.value === 'status' ? doc.text_state : doc.category
}

/** Split one group's documents by what they *are*, where that is asked at all.
 *
 * Only evidence carries a document type — it is the only material read under a
 * field schema, so it is the only material a type would mean anything for. A
 * category holding a mix of types is therefore always evidence, and everything
 * else returns a single unlabelled section that the rail renders flat.
 */
function subgroups(value: string, items: AuditDocument[]) {
  if (groupBy.value !== 'type' || value !== 'evidence') {
    return [{ key: '', label: '', items }]
  }
  const map = new Map<string, AuditDocument[]>()
  for (const doc of items) {
    const type = String(doc.classification?.document_type || '')
    map.set(type, [...(map.get(type) || []), doc])
  }
  return [...map.entries()]
    .map(([type, docs]) => ({
      key: type || 'unclassified',
      // Unread rather than unclassifiable: a document with no type yet has not
      // reached the classification stage, which is a different thing from the
      // `other` bucket an auditor is asked to retype.
      label: type ? documentTypeLabel(type) : 'not yet identified',
      items: docs,
    }))
    .sort((a, b) => (a.key === 'unclassified' ? 1 : 0) - (b.key === 'unclassified' ? 1 : 0)
      || a.label.localeCompare(b.label))
}

const groups = computed(() => {
  const map = new Map<string, AuditDocument[]>()
  for (const doc of filtered.value) {
    const value = groupValue(doc)
    map.set(value, [...(map.get(value) || []), doc])
  }
  const entries = [...map.entries()].map(([value, items]) => ({
    value,
    key: `${groupBy.value}:${value}`,
    label: groupBy.value === 'folder'
      ? value
      : (value ? value.replace(/_/g, ' ') : 'not yet read'),
    items,
    sections: subgroups(value, items),
  }))
  entries.sort((a, b) => groupBy.value === 'type'
    ? categories.indexOf(a.value as DocumentCategory) - categories.indexOf(b.value as DocumentCategory)
    : a.value.localeCompare(b.value))
  return entries
})
/** Three groupings, cycled from a link rather than chosen from a select. */
function cycleGrouping() {
  const order: Array<typeof groupBy.value> = ['type', 'folder', 'status']
  groupBy.value = order[(order.indexOf(groupBy.value) + 1) % order.length]
}

const current = computed(() => previewPages.value.find(page => page.page === currentPage.value) || previewPages.value[0])
const isPdf = computed(() => !!selected.value && /\.pdf$/i.test(selected.value.file))
const isDocx = computed(() => !!selected.value && /\.docx$/i.test(selected.value.file))
const isImage = computed(() => !!selected.value && /\.(png|jpe?g|webp|bmp)$/i.test(selected.value.file))
const hasOriginalView = computed(() => isPdf.value || isDocx.value)
const showTextView = computed(() => !isImage.value && (!hasOriginalView.value || sourceView.value === 'text'))
const fileUrl = computed(() => selected.value ? `/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/file` : '')
const indexingActive = computed(() =>
  indexingStatus.value?.state === 'indexing' || documents.value.some(document => document.search_index_state === 'indexing'),
)
const indexingDetail = computed(() => {
  const status = indexingStatus.value
  if (!status || status.state !== 'indexing') return 'New documents will become searchable automatically.'
  const completed = Math.min(status.completed_documents, status.total_documents)
  return `Preparing local search ${completed} of ${status.total_documents} complete. Ready documents can already be searched.`
})
/**
 * The count alone. Indexing is a transient background job, so it belongs in
 * the header row as a chip rather than in a two-line card sized like a problem
 * that needs solving; the sentence above stays on its tooltip.
 */
const indexingProgress = computed(() => {
  const status = indexingStatus.value
  if (!status || status.state !== 'indexing') return ''
  return `${Math.min(status.completed_documents, status.total_documents)} of ${status.total_documents}`
})

/**
 * Paging belongs to whatever is rendering the document, and only the
 * extracted-text view is rendered by this component — the PDF goes to the
 * browser's own viewer, which has page controls and a find of its own, and the
 * .docx renderer lays the whole file out at once. Duplicating the controls put
 * a second, worse pager above the real one: each arrow rebuilt the iframe at a
 * new `#page=` anchor, which reloads the file.
 */
const showPageNav = computed(() => showTextView.value)
/** What the head still has to say once the viewer says the rest. */
// Searching inside one document reads the content index, not the page on
// screen — it spans the extracted text and any vision transcript, and returns
// excerpts the viewer's own find cannot. Worth keeping, not worth a permanent
// row: it opens on demand and stays open while it has something to show.
const findOpen = ref(false)
const findInput = ref<{ $el?: HTMLElement } | null>(null)
const showDocumentSearch = computed(() => findOpen.value || Boolean(sourceSearch.value.trim()))
function toggleFind() {
  // Closing clears, so the bar cannot reappear on its own from a stale query.
  if (showDocumentSearch.value) {
    findOpen.value = false
    sourceSearch.value = ''
    sourceResults.value = []
    return
  }
  findOpen.value = true
  void nextTick(() => (findInput.value?.$el as HTMLInputElement | undefined)?.focus?.())
}
const secondaryActions = computed<MenuItem[]>(() => [
  { label: 'Reindex search', icon: 'pi pi-sync', command: () => void reindexAll() },
  { label: 'Methodology knowledge', icon: 'pi pi-book', command: () => void openKnowledge() },
  // Server-wide assistant configuration, so an administrator's to set. A
  // non-admin still needs to know whether it is available, which the label says.
  {
    label: canConfigureVision.value
      ? (visionAvailable.value ? 'Vision profile' : 'Configure vision')
      : (visionAvailable.value ? 'Vision configured' : 'Vision not configured'),
    icon: visionAvailable.value ? 'pi pi-eye' : 'pi pi-cog',
    disabled: !canConfigureVision.value,
    command: () => void openVisionSettings(),
  },
])
const documentActions = computed<MenuItem[]>(() => [
  { label: 'Re-extract text', icon: 'pi pi-refresh', command: () => void reextract() },
  { label: 'Delete document', icon: 'pi pi-trash', command: () => remove() },
])

async function openVisionSettings() {
  settingsStatus.value = await api.get<AssistantStatus>('/api/assistant/status')
  const current = agent.state.status?.vision_profile
  visionProvider.value = current?.provider || settingsStatus.value.provider || settingsStatus.value.backend
  visionModel.value = current?.model
    || selectedVisionProvider.value?.vision_model
    || selectedVisionProvider.value?.default_model
    || ''
  visionSettingsOpen.value = true
}

async function saveVisionSettings() {
  if (!visionProvider.value || !visionModel.value.trim()) return
  visionSettingsBusy.value = true
  try {
    await api.patch<AssistantStatus>('/api/assistant/settings', {
      vision_profile: {
        provider: visionProvider.value,
        model: visionModel.value.trim(),
        capabilities: ['vision'],
      },
    })
    await agent.refreshStatus()
    visionSettingsOpen.value = false
    toast.add({ severity: 'success', summary: 'Vision profile saved', detail: `${visionProvider.value} / ${visionModel.value}`, life: 3000 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Vision profile not saved', detail: String(error), life: 5000 })
  } finally {
    visionSettingsBusy.value = false
  }
}

watch(visionProvider, () => {
  const provider = selectedVisionProvider.value
  if (provider && !visionModelOptions.value.includes(visionModel.value)) {
    visionModel.value = provider.vision_model || provider.default_model
  }
})

const prefsKey = `aw-doc-rail:${props.workspace.id}`

function loadRailPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(prefsKey) || '{}') as { groupBy?: string; collapsed?: unknown }
    if (raw.groupBy === 'type' || raw.groupBy === 'folder' || raw.groupBy === 'status') groupBy.value = raw.groupBy
    if (Array.isArray(raw.collapsed)) collapsedGroups.value = new Set(raw.collapsed.map(String))
  } catch { /* corrupt prefs are discarded */ }
}

function saveRailPrefs() {
  localStorage.setItem(prefsKey, JSON.stringify({ groupBy: groupBy.value, collapsed: [...collapsedGroups.value] }))
}

function toggleGroup(key: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedGroups.value = next
  saveRailPrefs()
}

async function loadDocuments() {
  const result = await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)
  documents.value = result.items
  const requested = String(route.query.doc || '')
  if (requested && result.items.some(doc => doc.id === requested)) selectedId.value = requested
  else if (!selectedId.value || !result.items.some(doc => doc.id === selectedId.value)) selectedId.value = result.items[0]?.id || ''
  await loadVocabulary()
}

function scheduleIndexingPoll(delay = 800) {
  if (indexingTimer !== undefined) window.clearTimeout(indexingTimer)
  indexingTimer = window.setTimeout(() => { void refreshIndexingStatus() }, delay)
}

async function refreshIndexingStatus() {
  if (indexingPollRunning) return
  indexingPollRunning = true
  const wasActive = indexingActive.value
  try {
    indexingStatus.value = await api.get<DocumentIndexingStatus>(`/api/workspaces/${props.workspace.id}/documents/indexing-status`)
    if (indexingStatus.value.state === 'indexing' || wasActive) await loadDocuments()
  } catch {
    // Indexing is best-effort background work; ordinary document actions remain available.
  } finally {
    indexingPollRunning = false
  }
  if (indexingStatus.value?.state === 'indexing') scheduleIndexingPoll(900)
}

function beginIndexingPolling() {
  indexingStatus.value = indexingStatus.value || {
    state: 'indexing', job_count: 1, total_documents: 0, completed_documents: 0,
    remaining_documents: 0, active_document_id: null, pace_seconds: 0,
  }
  indexingStatus.value.state = 'indexing'
  scheduleIndexingPoll(150)
}

async function selectDocument(id: string, page?: number) {
  if (id !== selectedId.value) {
    sourceSearch.value = ''
    sourceResults.value = []
    fullVisualCoverage.value = false
  }
  selectedId.value = id
  currentPage.value = page || Number(route.query.page || 1)
  await nav.replace('documents', { doc: id, page: currentPage.value })
  await loadDetail()
}

async function loadDetail() {
  if (!selectedId.value) return
  try {
    const data = await api.get<{ pages: DocumentPage[] }>(`/api/workspaces/${props.workspace.id}/documents/${selectedId.value}/preview`)
    previewPages.value = data.pages
    if (!data.pages.some(page => page.page === currentPage.value)) currentPage.value = data.pages[0]?.page || 1
    activity.value = (await api.get<{ items: AIActivityEvent[] }>(`/api/workspaces/${props.workspace.id}/ai-activity?document_id=${selectedId.value}`)).items
    await loadAnalysis()
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Document unavailable', detail: String(error), life: 5000 })
  }
}

async function loadAnalysis() {
  if (!selectedId.value) return
  analysis.value = await api.get<DocumentAnalysisDetail>(`/api/workspaces/${props.workspace.id}/documents/${selectedId.value}/analysis`)
  summaryDraft.value = analysis.value.effective?.summary_markdown || ''
  notesDraft.value = analysis.value.effective?.audit_notes_markdown || ''
}

async function waitForAnalysis(runId: string) {
  for (let attempt = 0; attempt < 300; attempt++) {
    const run = await api.get<AgentRun>(`/api/workspaces/${props.workspace.id}/agent/runs/${runId}`)
    if ([...TERMINAL_STATUSES, 'paused', 'interrupted'].includes(run.status)) return run
    await new Promise(resolve => window.setTimeout(resolve, 500))
  }
  throw new Error('Analysis is still running. Its progress remains available in the assistant.')
}

type AnalysisAction = 'analyze' | 'refresh' | 'revise_vocabulary'

// `force` had to split, because under an accumulating master one button was
// being asked two different questions and could only answer one of them.
// `refresh` re-reads this document under the vocabulary its siblings were read
// under — cheap, and it reports rather than applies a field the vocabulary has
// no place for. `revise_vocabulary` re-reads every document of this type and
// rebuilds that vocabulary from the pass. Naming them separately is the point:
// one is a document, one is a type, and the expensive one is only ever reached
// deliberately.
async function startAnalysis(action: AnalysisAction) {
  if (!selected.value) return
  analysisBusy.value = true
  try {
    const run = await api.post<AgentRun>(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/analysis-runs`, {
      action,
      full_visual_coverage: fullVisualCoverage.value,
    })
    const finished = await waitForAnalysis(run.id)
    await loadDocuments(); await loadAnalysis()
    if (finished.status === 'failed') throw new Error(finished.error || 'Document analysis failed.')
    const open = finished.status === 'completed_with_open_items'
    toast.add({ severity: open ? 'warn' : 'success', summary: open ? 'Document analysis needs review' : 'Document analysis ready', life: 3200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Analysis unavailable', detail: String(error), life: 5000 })
  } finally { analysisBusy.value = false }
}

async function saveAnalysis(reviewed = false) {
  if (!selected.value || !analysis.value) return
  analysisBusy.value = true
  try {
    const payload: Record<string, unknown> = {
      review_revision: analysis.value.review_revision,
      audit_notes_markdown: notesDraft.value,
      review_state: reviewed ? 'reviewed' : 'needs_review',
    }
    if (!hasStructuredSummary.value) payload.summary_markdown = summaryDraft.value
    analysis.value = await api.patch<DocumentAnalysisDetail>(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/analysis/review`, payload)
    await loadDocuments()
    toast.add({ severity: 'success', summary: reviewed ? 'Analysis reviewed' : 'Analysis edits saved', life: 2200 })
  } catch (error) { toast.add({ severity: 'error', summary: 'Analysis not saved', detail: String(error), life: 5000 }) }
  finally { analysisBusy.value = false }
}

async function revertAnalysisField(field: 'summary' | 'notes') {
  if (!selected.value || !analysis.value) return
  const payload: Record<string, unknown> = { review_revision: analysis.value.review_revision }
  payload[field === 'summary' ? 'summary_markdown' : 'audit_notes_markdown'] = null
  analysis.value = await api.patch<DocumentAnalysisDetail>(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/analysis/review`, payload)
  summaryDraft.value = analysis.value.effective?.summary_markdown || ''
  notesDraft.value = analysis.value.effective?.audit_notes_markdown || ''
  await loadDocuments()
}

async function acceptCandidate() {
  if (!selected.value || !analysis.value?.candidate) return
  analysisBusy.value = true
  try {
    analysis.value = await api.post<DocumentAnalysisDetail>(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/analysis/accept-candidate`, {
      index_revision: analysis.value.index_revision, review_revision: analysis.value.review_revision,
    })
    compareCandidate.value = false; await loadDocuments(); await loadAnalysis()
  } finally { analysisBusy.value = false }
}

async function runContentSearch(documentIds?: string[]) {
  const query = (documentIds ? sourceSearch.value : search.value).trim()
  if (!query) return
  searchBusy.value = true
  try {
    const result = await api.post<{ results: DocumentSearchResult[] }>(`/api/workspaces/${props.workspace.id}/documents/search`, { query, document_ids: documentIds, top_k: 6 })
    if (documentIds) sourceResults.value = result.results
    else searchResults.value = result.results
  } catch (error) { toast.add({ severity: 'error', summary: 'Search failed', detail: String(error), life: 5000 }) }
  finally { searchBusy.value = false }
}

async function openSearchResult(result: DocumentSearchResult) {
  await selectDocument(result.document_id, result.page)
  view.value = 'preview'; sourceView.value = 'text'
}

async function openCitation(citation: DocumentAnalysisCitation) {
  if (!selected.value) return
  currentPage.value = citation.page
  view.value = 'preview'
  sourceView.value = citation.evidence_kind === 'visual' ? 'original' : 'text'
  await nav.replace('documents', { doc: selected.value.id, page: citation.page })
}

async function reindexAll() {
  if (!documents.value.length) return
  busy.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/documents/reindex`, { document_ids: documents.value.map(document => document.id) })
    await loadDocuments(); beginIndexingPolling()
    toast.add({ severity: 'info', summary: 'Search indexing started', detail: 'Documents will become searchable in the background.', life: 3200 })
  } catch (error) { toast.add({ severity: 'error', summary: 'Reindex failed', detail: String(error), life: 5000 }) }
  finally { busy.value = false }
}

const typeReviewOpen = ref(false)

/** Documents the classifier could not name. Naming one is what lets it fill a
 *  role in a cycle test, so the count is surfaced rather than left to a menu. */
const unidentifiedCount = computed(
  () => documents.value.filter(doc => doc.classification?.document_type === 'other').length,
)

async function onRetyped(): Promise<void> {
  await loadDocuments()
}

async function batchAnalyze() {
  const eligible = eligibleDocuments.value
  if (!eligible.length) {
    toast.add({ severity: 'info', summary: 'All eligible documents already have current analysis', life: 2600 }); return
  }
  analysisBusy.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Analyse ${eligible.length === 1 ? 'this document' : `these ${eligible.length} documents`}.`,
      'act', agent.launchMode.value,
      {
        command: 'analyze_documents', source: 'tab_button',
        runContext: { document_ids: eligible.map(document => document.id), action: 'analyze' },
      },
    )
    agent.openPanel()
    toast.add({ severity: 'info', summary: 'Document analysis started', detail: 'Progress is visible in the assistant.', life: 3000 })
  } catch (error) { toast.add({ severity: 'error', summary: 'Batch analysis unavailable', detail: String(error), life: 5000 }) }
  finally { analysisBusy.value = false }
}

async function reextract() {
  if (!selected.value) return
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/re-extract`); await loadDocuments(); await loadDetail(); beginIndexingPolling() }
  catch (error) { toast.add({ severity: 'error', summary: 'Extraction failed', detail: String(error), life: 5000 }) }
  finally { busy.value = false }
}

async function updateClassification(value: DocumentCategory) {
  const document = selected.value
  if (!document || value === document.category || classificationBusy.value) return
  classificationBusy.value = true
  try {
    const updated = await api.patch<AuditDocument>(`/api/workspaces/${props.workspace.id}/documents/${document.id}`, { category: value })
    documents.value = documents.value.map(item => item.id === updated.id ? updated : item)
    emit('changed')
    toast.add({ severity: 'success', summary: 'Classification updated', detail: updated.title, life: 2200 })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Classification not updated', detail: String(error), life: 5000 })
  } finally {
    classificationBusy.value = false
  }
}

function remove() {
  const doc = selected.value
  if (!doc) return
  confirm.require({
    header: 'Delete document',
    message: `Delete "${doc.title}"? Existing evidence references will remain visibly stale.`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      await api.del(`/api/workspaces/${props.workspace.id}/documents/${doc.id}`)
      await assistantChat.removeDocument(doc.id)
      selectedId.value = ''; await loadDocuments(); if (selectedId.value) await loadDetail(); emit('changed')
    },
  })
}

async function attachToAssistant() {
  if (!selected.value) return
  await assistantChat.addDocument(selected.value)
  agent.openPanel()
  toast.add({ severity: 'success', summary: 'Added to assistant', detail: selected.value.title, life: 2500 })
}

let docxToken = 0
async function renderDocx() {
  if (!selected.value || !isDocx.value || sourceView.value !== 'original' || view.value !== 'preview') return
  const token = ++docxToken
  docxLoading.value = true
  try {
    const response = await fetch(fileUrl.value)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const data = await response.arrayBuffer()
    if (token !== docxToken || !docxContainer.value) return
    await renderAsync(data, docxContainer.value)
  } catch (error) {
    if (token === docxToken) {
      sourceView.value = 'text'
      toast.add({ severity: 'warn', summary: 'Original view unavailable', detail: `Showing extracted text instead. ${error}`, life: 4000 })
    }
  } finally {
    if (token === docxToken) docxLoading.value = false
  }
}

watch([() => selected.value?.id, sourceView, view], () => { void renderDocx() }, { flush: 'post' })



/**
 * Whether the stored title says anything the filename does not.
 *
 * `title` is a slug derived from the file at intake — "Minutes of Meeting -
 * CFO.docx" becomes `minutes_of_meeting_cfo` — so showing it beside the file
 * repeats the same words in a worse form. It earns its place only once
 * someone has retitled the document to something genuinely different.
 */


async function copyText(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value)
    toast.add({ severity: 'success', summary: `${label} copied`, life: 1800 })
  } catch {
    toast.add({ severity: 'error', summary: `Could not copy ${label.toLowerCase()}`, life: 3000 })
  }
}

async function loadPacks() { packs.value = (await api.get<{ items: KnowledgePack[] }>(`/api/workspaces/${props.workspace.id}/knowledge-packs`)).items }
async function openKnowledge() { await loadPacks(); knowledgeOpen.value = true }
async function uploadPack(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return
  await api.uploadOne(`/api/workspaces/${props.workspace.id}/knowledge-packs`, file, { name: file.name.replace(/\.[^.]+$/, ''), scope: 'workspace' })
  await loadPacks(); if (packInput.value) packInput.value.value = ''
}
async function searchPacks() {
  packResults.value = packSearch.value.trim() ? (await api.get<{ items: Array<Record<string, unknown>> }>(`/api/workspaces/${props.workspace.id}/knowledge-packs/search?q=${encodeURIComponent(packSearch.value)}`)).items : []
}

watch(() => route.query.doc, id => { if (id && id !== selectedId.value) void selectDocument(String(id), Number(route.query.page || 1)) })
watch(currentPage, page => { if (selectedId.value) void nav.replace('documents', { doc: selectedId.value, page }) })
watch(groupBy, saveRailPrefs)
watch([selected, groupBy], () => {
  const doc = selected.value
  if (!doc) return
  const key = `${groupBy.value}:${groupValue(doc)}`
  if (collapsedGroups.value.has(key)) toggleGroup(key)
})
onMounted(async () => {
  loadRailPrefs()
  await loadDocuments()
  await refreshIndexingStatus()
  unsubscribeWorkspaceChanged = agent.onWorkspaceInvalidated(() => {
    void loadDocuments().then(() => loadDetail())
    scheduleIndexingPoll(150)
  })
  if (selectedId.value) await selectDocument(selectedId.value, Number(route.query.page || 1))
})
onUnmounted(() => {
  if (indexingTimer !== undefined) window.clearTimeout(indexingTimer)
  unsubscribeWorkspaceChanged?.()
})
</script>

<template>
  <section class="documents-tab">
    <header class="page-head">
      <h1>Documents</h1>
      <span class="grow" />
      <!-- A background job, reported at the size of a background job. -->
      <span
        v-if="indexingActive"
        class="indexing-chip"
        role="status"
        aria-live="polite"
        v-tooltip.bottom="indexingDetail"
      >
        <i class="pi pi-spin pi-spinner" />Indexing<template v-if="indexingProgress"> {{ indexingProgress }}</template>
      </span>
      <Button label="Add documents" icon="pi pi-plus" size="small" outlined severity="secondary" @click="emit('import-requested')" />
      <Button
        v-if="unidentifiedCount"
        :label="`Identify ${unidentifiedCount}`"
        icon="pi pi-question-circle"
        size="small"
        severity="warn"
        outlined
        @click="typeReviewOpen = true"
      />
      <Button
        v-if="eligibleDocuments.length"
        :label="`Analyse ${eligibleDocuments.length}`"
        icon="pi pi-sparkles"
        size="small"
        severity="warn"
        :loading="analysisBusy"
        @click="batchAnalyze"
      />
      <Button v-else label="Analyse all" icon="pi pi-sparkles" size="small" :loading="analysisBusy" :disabled="!documents.length" @click="batchAnalyze" />
      <UiOverflowMenu :items="secondaryActions" tooltip="More document actions" />
    </header>

    <UiReviewBar
      v-if="documents.length"
      :lanes="status.lanes"
      :chips="DOCUMENT_CHIPS"
      :filters="status.filters"
      allLabel="All documents"
      :total="documents.length"
      :filter="statusFilter"
      @filter="statusFilter = ($event as DocumentsFilter[])"
    />

    <div v-if="documents.length" class="document-layout surface-panel">
      <aside class="document-rail">
        <div class="rail-tools">
          <IconField>
            <InputIcon class="pi pi-search" />
            <InputText v-model="search" size="small" placeholder="Search documents" />
          </IconField>
          <!-- A link, not a full-width select: grouping is chosen once and
               then read past. -->
          <button type="button" class="group-by" @click="cycleGrouping">Group by {{ groupBy }} ▾</button>
        </div>
        <div v-if="!filtered.length" class="rail-empty">No document matches this view.</div>
        <div v-for="group in groups" :key="group.key" class="doc-group">
          <button class="group-head" :aria-expanded="!collapsedGroups.has(group.key)" @click="toggleGroup(group.key)">
            <i :class="collapsedGroups.has(group.key) ? 'pi pi-angle-right' : 'pi pi-angle-down'" />
            <span class="group-name">{{ group.label }}</span>
            <span class="group-count aw-figure">{{ group.items.length }}</span>
          </button>
          <template v-if="!collapsedGroups.has(group.key)">
            <button
              v-for="doc in group.items"
              :key="doc.id"
              class="doc-row"
              :class="{ active: doc.id === selectedId }"
              @click="selectDocument(doc.id, 1)"
            >
              <span class="dot" :data-tone="documentTone(doc, documentFacts)" aria-hidden="true" />
              <span class="doc-identity">
                <span class="doc-name">{{ doc.source }}</span>
                <span class="doc-meta aw-figure">
                  <template v-for="(part, index) in documentMeta(doc, documentFacts)" :key="part.text">
                    <span v-if="index" aria-hidden="true"> · </span><span :data-tone="part.tone">{{ part.text }}</span>
                  </template>
                </span>
              </span>
            </button>
          </template>
        </div>
        <button v-if="search.trim()" class="rail-deep-search" @click="runContentSearch()">
          <i class="pi pi-search" /><span>Search inside documents for “{{ search.trim() }}”</span>
        </button>
        <!-- The results of that search replace the list in place; the modal
             that used to hold them is retired. -->
        <div v-if="searchResults.length" class="rail-results">
          <p class="rail-results-head">
            {{ plural(searchResults.length, 'match') }}
            <button type="button" @click="searchResults = []">Clear</button>
          </p>
          <button v-for="result in searchResults" :key="result.citation_id" class="rail-result" @click="openSearchResult(result)">
            <span class="doc-name">{{ result.title }}</span>
            <span class="doc-meta aw-figure">page {{ result.page }}</span>
            <span class="excerpt">{{ result.excerpt }}</span>
          </button>
        </div>
      </aside>

      <main v-if="selected" class="document-detail">
        <!-- One 32px row. The page count, the analysis date and the review
             state are on the list row's meta line and on `Mark reviewed`; a
             header that restated them was a band the viewer paid for. -->
        <header class="detail-head">
          <span class="held" :data-empty="!selected.category">
            {{ selected.category ? selected.category : 'Not yet read' }}<template
              v-if="selected.classification?.document_type"> · {{ documentTypeLabel(selected.classification.document_type) }}</template>
          </span>
          <h2 :title="selected.source">{{ selected.source }}</h2>
          <span class="grow" />
          <label class="held-as">
            <span>Held as</span>
            <Select
              :modelValue="selected.category"
              :options="documentCategoryOptions"
              optionLabel="label"
              optionValue="value"
              :disabled="classificationBusy"
              placeholder="Not yet read"
              size="small"
              aria-label="What this engagement holds the document as"
              @update:modelValue="updateClassification"
            />
          </label>
          <Button label="Add to assistant" icon="pi pi-paperclip" size="small" outlined severity="secondary" @click="attachToAssistant" />
          <Button
            v-if="selected.analysis_review_state === 'reviewed'"
            label="Reviewed"
            icon="pi pi-check"
            size="small"
            outlined
            severity="secondary"
            disabled
          />
          <Button
            v-else
            label="Mark reviewed"
            icon="pi pi-check"
            size="small"
            :disabled="!analysis?.effective"
            :loading="analysisBusy"
            @click="saveAnalysis(true)"
          />
          <UiOverflowMenu :items="documentActions" tooltip="Document actions" />
        </header>

        <!-- Which view, and how that view is set up: one row rather than a tab
             bar above a tool bar. -->
        <div class="detail-views">
          <nav class="detail-tabs">
            <button v-for="item in detailViews" :key="item" :class="{ active: view === item }" @click="view = item">
              {{ item }}<span v-if="item === 'activity' && activity.length" class="tab-badge aw-figure">{{ activity.length }}</span>
            </button>
          </nav>
          <template v-if="view === 'preview'">
            <span v-if="showPageNav" class="page-nav">
              <Button icon="pi pi-angle-left" text :disabled="currentPage <= 1" aria-label="Previous page" @click="currentPage--" /><span>Page {{ currentPage }} of {{ selected.pages || previewPages.length || 1 }}</span><Button icon="pi pi-angle-right" text :disabled="currentPage >= (selected.pages || previewPages.length || 1)" aria-label="Next page" @click="currentPage++" />
            </span>
            <div v-if="hasOriginalView" class="source-toggle" role="group" aria-label="Preview mode">
              <button :class="{ active: sourceView === 'original' }" @click="sourceView = 'original'">Original</button>
              <button :class="{ active: sourceView === 'text' }" @click="sourceView = 'text'">Extracted text</button>
            </div>
            <Button
              label="Find"
              icon="pi pi-search"
              size="small"
              text
              :class="{ 'find-on': showDocumentSearch }"
              @click="toggleFind"
            />
            <a :href="fileUrl" target="_blank" class="open-original">Open original</a>
          </template>
        </div>

        <!-- The two states that need a sentence, in the fieldwork stale-strip
             form. Everything else about the analysis is on the row or the tab. -->
        <p v-if="selected.analysis_validity_state === 'stale'" class="strip warn">
          <i class="pi pi-history" aria-hidden="true" />
          <span>The analysis was made against an earlier version of this file. Refresh it before relying on it.</span>
          <button type="button" :disabled="analysisBusy" @click="startAnalysis('refresh')">Refresh</button>
        </p>
        <p v-else-if="selected.candidate_analysis_id" class="strip info">
          <i class="pi pi-clone" aria-hidden="true" />
          <span>A refreshed analysis is waiting.</span>
          <button type="button" @click="view = 'analysis'; compareCandidate = true">Compare</button>
        </p>

        <div v-if="view === 'preview'" class="detail-content preview-view">
          <div v-if="showDocumentSearch" class="source-search-bar">
            <InputText
              ref="findInput"
              v-model="sourceSearch"
              placeholder="Search this document's text and transcripts"
              @keyup.enter="runContentSearch(selected ? [selected.id] : [])"
            />
            <Button label="Search" icon="pi pi-search" severity="secondary" outlined :loading="searchBusy" @click="runContentSearch(selected ? [selected.id] : [])" />
          </div>
          <div v-if="sourceResults.length && sourceSearch" class="inline-search-results">
            <button v-for="result in sourceResults" :key="result.citation_id" @click="openSearchResult(result)"><strong>Page {{ result.page }}</strong><span>{{ result.excerpt }}</span></button>
          </div>
          <div v-if="current?.image_only && showTextView" class="scan-notice">
            <i class="pi pi-image" />
            <div>
              <strong>{{ selected.analysis_vision_used && selected.analysis_validity_state === 'current' ? 'Visual source—analysis available' : 'Visual source' }}</strong>
              <p v-if="selected.analysis_vision_used && selected.analysis_validity_state === 'current'">The extracted-text view is empty, but the current analysis includes an AI-derived visual transcription. Open the Analysis tab to review it.</p>
              <p v-else-if="visionAvailable">This page has insufficient extractable text. Analyse it with the configured vision profile; the original remains the authoritative source.</p>
              <p v-else>This page has insufficient extractable text. You can start analysis now; it will remain an open item without a model charge until a vision profile is configured.</p>
            </div>
          </div>
          <img v-if="isImage" class="document-image" :src="fileUrl" :alt="selected.title" />
          <iframe v-else-if="isPdf && sourceView === 'original'" :key="`${selected.id}:${currentPage}`" class="document-frame" :src="`${fileUrl}#page=${currentPage}`" :title="selected.title" />
          <div v-else-if="isDocx && sourceView === 'original'" :key="selected.id" ref="docxContainer" class="docx-frame" :class="{ loading: docxLoading }" :aria-busy="docxLoading" />
          <pre v-else class="page-text">{{ current?.text || 'No extractable text on this page.' }}</pre>
          <details class="technical-details">
            <summary>Technical details</summary>
            <dl><div><dt>Document ID</dt><dd><code>{{ selected.id }}</code><Button icon="pi pi-copy" text rounded size="small" aria-label="Copy document ID" @click="copyText(selected.id, 'Document ID')" /></dd></div><div><dt>Content hash</dt><dd><code>{{ selected.sha1 }}</code><Button icon="pi pi-copy" text rounded size="small" aria-label="Copy content hash" @click="copyText(selected.sha1, 'Content hash')" /></dd></div><div><dt>Stored file</dt><dd><code>{{ selected.file }}</code></dd></div><div v-if="selected.relative_path"><dt>Imported path</dt><dd>{{ selected.relative_path }}</dd></div><div><dt>Added</dt><dd>{{ selected.created }}</dd></div><div v-if="selected.updated"><dt>Replaced</dt><dd>{{ selected.updated }}</dd></div></dl>
          </details>
        </div>

        <div v-else-if="view === 'analysis'" class="detail-content analysis-view">
          <div class="analysis-toolbar">
            <span class="analysis-note">
              {{ analysis?.effective
                ? 'What the model read from this file, and what an auditor has added to it.'
                : 'Nothing has been read from this file yet.' }}
            </span>
            <div class="analysis-actions">
              <Button
                v-if="selected.text_state === 'extracted' || selected.text_state === 'partial'"
                :label="fullVisualCoverage ? `Full visual coverage (max ${visualPageLimit})` : 'Text coverage only'"
                :icon="fullVisualCoverage ? 'pi pi-images' : 'pi pi-file'"
                size="small"
                severity="secondary"
                outlined
                v-tooltip.bottom="`Opt in to visual analysis of text-bearing pages, bounded to ${visualPageLimit} pages for this document.`"
                @click="fullVisualCoverage = !fullVisualCoverage"
              />
              <Button v-if="!analysis?.generated" label="Analyse" icon="pi pi-sparkles" size="small" :loading="analysisBusy" @click="startAnalysis('analyze')" />
              <Button v-else label="Refresh" icon="pi pi-refresh" size="small" severity="secondary" outlined :loading="analysisBusy" @click="startAnalysis('refresh')" v-tooltip.bottom="'Re-read this document under the vocabulary its type already carries.'" />
              <Button v-if="analysis?.candidate" label="Compare candidate" icon="pi pi-clone" size="small" severity="secondary" outlined @click="compareCandidate = !compareCandidate" />
            </div>
          </div>

          <section v-if="selectedVocabulary" class="vocabulary-card" :class="{ thin: selectedVocabulary.thin }">
            <p class="vocabulary-line">
              <b>Read as {{ selectedVocabulary.document_type.replace(/_/g, ' ') }}</b>
              <span class="aw-figure">
                ·
                <button type="button" class="fields-link" @click="fieldsOpen = !fieldsOpen">
                  {{ selectedVocabulary.fields.length }} {{ selectedVocabulary.fields.length === 1 ? 'field' : 'fields' }}
                  <i class="pi" :class="fieldsOpen ? 'pi-chevron-down' : 'pi-chevron-right'" />
                </button>
                from {{ plural(selectedVocabulary.documents_read.length, 'document') }} ·
                {{ selectedVocabulary.corroborated_fields ? `${selectedVocabulary.corroborated_fields} stated by two or more` : 'none stated by two' }}
              </span>
            </p>
            <p v-if="selectedVocabulary.thin" class="strip warn inline">
              <i class="pi pi-exclamation-triangle" aria-hidden="true" />
              <span>{{ thinReason(selectedVocabulary) }}</span>
              <button v-if="isEvidence" type="button" :disabled="analysisBusy" @click="startAnalysis('revise_vocabulary')">Revise vocabulary</button>
            </p>
            <table v-if="fieldsOpen" class="vocabulary-fields">
              <tbody>
                <tr v-for="field in selectedVocabulary.fields" :key="field.name">
                  <td class="vf-name">{{ field.name }}</td>
                  <td class="vf-role">{{ field.role }}</td>
                  <td class="vf-fill" :class="{ partial: field.fill_count < selectedVocabulary.documents_read.length }">
                    {{ field.fill_count }} / {{ selectedVocabulary.documents_read.length }}
                  </td>
                  <td class="vf-unread">
                    <span
                      v-if="field.unread.length"
                      v-tooltip.left="`${field.unread.length} document(s) were read before this field existed, so their silence about it means nobody asked — not that they do not state it.`"
                    >{{ field.unread.length }} never asked</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <div v-if="analysis?.status.analysis_coverage_state === 'partial'" class="coverage-warning">
            <i class="pi pi-exclamation-triangle" />
            <div>
              <strong>Partial source coverage</strong>
              <p>Text pages: {{ analysis.effective?.coverage.text_analyzed_pages?.join(', ') || '—' }} · Visual pages: {{ analysis.effective?.coverage.vision_analyzed_pages?.join(', ') || '—' }}</p>
              <ul v-if="analysis.effective?.coverage.omissions?.length">
                <li v-for="item in analysis.effective.coverage.omissions" :key="`${item.page}:${item.reason}`">Page {{ item.page }} — {{ item.reason.replaceAll('_', ' ') }}</li>
              </ul>
              <p v-else>Omitted pages: {{ analysis.effective?.coverage.omitted_pages.join(', ') || '—' }}</p>
            </div>
          </div>
          <div v-if="analysis?.status.analysis_validity_state === 'stale'" class="coverage-warning"><i class="pi pi-history" /><span>This analysis belongs to an earlier source identity. It remains available to agent context; refresh it before relying on it as current.</span></div>

          <UiEmptyState v-if="!analysis?.effective" icon="pi pi-sparkles" title="Analyse this document once" description="Create reusable document analysis and audit notes. Source indexing remains local and independent." compact />
          <template v-else>
            <!-- A summary whose origin is the structured evidence is that
                 evidence written out as bullets: the same fields, the same
                 values, one screen apart. The sheet below is the better
                 rendering of it, so the prose copy is not drawn at all. A
                 model-written summary is a different thing and stays. -->
            <section v-if="!hasStructuredSummary" class="analysis-editor">
              <header>
                <div>
                  <h4 class="aw-label">Summary</h4>
                  <small>Auditor edits are stored separately from the generated basis.</small>
                </div>
                <div><Button v-if="analysis.review.summary_override !== null" label="Revert" text size="small" severity="secondary" @click="revertAnalysisField('summary')" /></div>
              </header>
              <MarkdownEditor v-model="summaryDraft" />
            </section>
            <section v-if="analysis.effective.records?.length" class="analysis-section">
              <header>
                <h4 class="aw-label">Structured evidence</h4>
                <small>What the model read from the page, checked against the {{ (analysis.effective.schema_ref?.document_type || 'document').replace(/_/g, ' ') }} schema</small>
              </header>
              <StructuredEvidenceSheet
                :records="analysis.effective.records"
                :schema="analysis.effective.schema_ref"
                :citations="analysis.effective.citations"
                :validated="analysis.status.analysis_coverage_state === 'complete'"
              />
            </section>
            <section class="analysis-editor">
              <header><div><h4 class="aw-label">Audit notes</h4><small>Freeform observations are not evidence that a control operated.</small></div><div><Button v-if="analysis.review.audit_notes_override !== null" label="Revert" text size="small" severity="secondary" @click="revertAnalysisField('notes')" /></div></header>
              <MarkdownEditor v-model="notesDraft" />
            </section>
            <div class="save-analysis"><Button :label="hasStructuredSummary ? 'Save notes' : 'Save edits'" icon="pi pi-save" severity="secondary" :loading="analysisBusy" @click="saveAnalysis(false)" /><Button label="Save and mark reviewed" icon="pi pi-check" :loading="analysisBusy" @click="saveAnalysis(true)" /></div>

            <section v-if="compareCandidate && analysis.candidate" class="candidate-compare">
              <h4>Refresh candidate</h4>
              <div><article><strong>Current effective summary</strong><MarkdownView :markdown="summaryDraft" /></article><article><strong>Candidate summary</strong><MarkdownView :markdown="analysis.candidate.summary_markdown" /></article></div>
              <div class="candidate-actions"><Button v-if="!hasStructuredSummary" label="Copy candidate summary into edits" severity="secondary" @click="summaryDraft = analysis.candidate.summary_markdown" /><Button label="Copy candidate notes into edits" severity="secondary" @click="notesDraft = analysis.candidate.audit_notes_markdown" /><Button label="Accept candidate as generated basis" icon="pi pi-check" @click="acceptCandidate" /></div>
            </section>

            <section class="analysis-sources">
              <h4 class="aw-label">Sources</h4>
              <button v-for="citation in analysis.effective.citations" :key="`${citation.id}:${citation.page}`" @click="openCitation(citation)">
                <strong>
                  [{{ citation.id }}] Page {{ citation.page }}
                  <Tag v-if="citation.evidence_kind === 'visual'" value="AI visual description" severity="info" />
                </strong>
                <span v-if="citation.evidence_kind === 'visual'">
                  {{ citation.description || 'Visual region' }}
                  <small v-if="citation.region"> · region {{ citation.region.x }}, {{ citation.region.y }}, {{ citation.region.width }} × {{ citation.region.height }}</small>
                </span>
                <span v-else>{{ citation.excerpt }}</span>
              </button>
              <p v-if="!analysis.effective.citations.length" class="muted">No validated source citations were generated.</p>
            </section>
            <details class="technical-details">
              <summary>Technical provenance</summary>
              <dl>
                <div><dt>Analysis ID</dt><dd><code>{{ analysis.effective.id }}</code></dd></div>
                <div><dt>Generated</dt><dd>{{ analysis.effective.generated_at }}</dd></div>
                <div><dt>Provider / model</dt><dd>{{ analysis.effective.provider || '—' }} / {{ analysis.effective.model || '—' }}</dd></div>
                <div><dt>Vision used</dt><dd>{{ analysis.effective.vision_used ? 'Yes' : 'No' }}</dd></div>
                <div v-for="profile in analysis.effective.generation_profiles" :key="profile.profile_hash">
                  <dt>{{ profile.name === 'vision' ? 'Vision profile' : 'Text profile' }}</dt>
                  <dd>{{ profile.provider }} / {{ profile.model }} · <code>{{ profile.profile_hash }}</code></dd>
                </div>
                <div><dt>Prompt version</dt><dd><code>{{ analysis.effective.prompt_version }}</code></dd></div>
                <div><dt>Extracted text hash</dt><dd><code>{{ analysis.effective.extracted_text_sha1 }}</code></dd></div>
                <div><dt>Transcription hash</dt><dd><code>{{ analysis.effective.derived_text_sha256 || '—' }}</code></dd></div>
                <div><dt>Prepared media</dt><dd><code>{{ analysis.effective.prepared_media_set_hash || '—' }}</code></dd></div>
              </dl>
            </details>
          </template>
        </div>

        <div v-else class="detail-content timeline">
          <article v-for="item in activity" :key="item.id"><i class="pi pi-sparkles" /><div><strong>{{ item.purpose.replace('_', ' ') }} · {{ item.disposition }}</strong><p>{{ item.at }} · {{ item.provider }} / {{ item.model }}</p><p>Pages {{ item.page_ranges?.join(', ') || '—' }}</p><details><summary>Technical details</summary><code>{{ item.id }} · response {{ item.response_hash || 'not available' }}</code></details></div></article>
          <p v-if="!activity.length" class="muted">No model activity references this document.</p>
        </div>
      </main>
      <UiEmptyState v-else icon="pi pi-file" title="Choose a document" description="Select a document from the inventory to preview it." compact />
    </div>
    <UiEmptyState v-else icon="pi pi-file-plus" title="Add engagement documents" description="Upload policies, contracts, evidence, reports, and other files. Extraction happens locally.">
      <Button label="Add documents" icon="pi pi-plus" @click="emit('import-requested')" />
    </UiEmptyState>

    <Drawer v-model:visible="knowledgeOpen" position="right" header="Methodology knowledge" :style="{ width: 'min(45rem, 96vw)' }">
      <div class="pack-toolbar">
        <input ref="packInput" type="file" hidden accept=".md,.markdown,.txt" @change="uploadPack" />
        <Button label="Add Markdown pack" icon="pi pi-plus" size="small" @click="packInput?.click()" />
        <InputText v-model="packSearch" size="small" placeholder="Search local methodology" @keyup.enter="searchPacks" />
        <Button label="Search" size="small" severity="secondary" outlined @click="searchPacks" />
      </div>
      <div class="pack-list">
        <article v-for="pack in packs" :key="`${pack.scope}:${pack.id}`">
          <strong>{{ pack.name }}</strong>
          <span class="pack-scope">{{ pack.scope }}</span>
          <p>Version {{ pack.version }} · updated {{ pack.updated }}</p>
          <details><summary>Technical details</summary><code>{{ pack.id }} · {{ pack.sha1 }}</code></details>
        </article>
      </div>
      <div v-if="packResults.length" class="search-results">
        <h4 class="aw-label">Cited sections</h4>
        <article v-for="(result, index) in packResults" :key="index"><strong>{{ result.citation }}</strong><p>{{ result.excerpt }}</p></article>
      </div>
    </Drawer>

    <Drawer v-model:visible="visionSettingsOpen" position="right" header="Vision profile" :style="{ width: 'min(34rem, 96vw)' }">
      <div class="vision-settings">
        <p>Document analysis uses this profile only for supported image and scanned-PDF pages. Later workflows reuse the persisted transcription without resending images.</p>
        <label><span>Provider</span><Select v-model="visionProvider" :options="providerOptions" optionLabel="label" optionValue="id" /></label>
        <label><span>Model</span><Select v-if="visionModelOptions.length" v-model="visionModel" :options="visionModelOptions" editable /><InputText v-else v-model="visionModel" placeholder="Vision-capable model name" /></label>
        <p v-if="agent.state.status?.vision_unavailability_reason" class="settings-warning">{{ agent.state.status.vision_unavailability_reason }}</p>
        <div class="drawer-foot">
          <Button label="Cancel" size="small" severity="secondary" outlined @click="visionSettingsOpen = false" />
          <Button label="Save vision profile" icon="pi pi-save" size="small" :loading="visionSettingsBusy" :disabled="!visionProvider || !visionModel.trim()" @click="saveVisionSettings" />
        </div>
      </div>
    </Drawer>

    <DocumentTypeReview
      v-model="typeReviewOpen"
      :workspace-id="props.workspace.id"
      @retyped="onRetyped"
      @error="(summary, error) => toast.add({ severity: 'error', summary, detail: String(error), life: 5000 })"
    />
  </section>
</template>

<style scoped>
.documents-tab { display: flex; flex-direction: column; gap: .75rem; height: 100%; min-height: 36rem; min-width: 0; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

/* One 32px row: the pill, the filename, and the acts. Everything the old
   three-line header restated is on the list row or on `Mark reviewed`. */
.detail-head { display: flex; align-items: center; gap: .5rem; min-height: 2rem; padding: .5rem 1.25rem; border-bottom: 1px solid var(--aw-border); }
.detail-head h2 { margin: 0; min-width: 0; overflow: hidden; color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.held { flex: none; padding: .1rem .5rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 600; text-transform: capitalize; }
.held[data-empty='true'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.held-as { display: flex; align-items: center; gap: .35rem; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.held-as :deep(.p-select) { min-width: 7.5rem; text-transform: capitalize; }

/* The two states that need a sentence, in the fieldwork strip form. */
.strip { display: flex; align-items: center; gap: .5rem; margin: 0; padding: .5rem 1.25rem; border-bottom: 1px solid var(--aw-border); font-size: var(--aw-text-sm); line-height: 1.4; }
.strip.warn { border-bottom-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.strip.info { border-bottom-color: var(--aw-info-line); background: var(--aw-info-soft); color: var(--aw-info); }
.strip span { flex: 1; min-width: 0; }
.strip button { flex: none; padding: 0; border: 0; background: none; color: inherit; font: inherit; font-weight: 700; text-decoration: underline; text-underline-offset: 2px; cursor: pointer; }
.strip.inline { margin: .4rem 0 0; padding: .4rem .625rem; border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control); }

.tab-badge { margin-left: .3rem; padding: 0 .3rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); }

/* One row per document: a readiness dot, the filename, and what the row owes. */
.doc-row { display: flex; align-items: center; gap: .625rem; }
.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }
.dot[data-tone='info'] { background: var(--aw-info); }
.doc-name { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); text-overflow: ellipsis; white-space: nowrap; }
.doc-row.active .doc-name { color: var(--aw-ink-strong); font-weight: 600; }
.doc-meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }
.doc-meta [data-tone='warn'] { color: var(--aw-warn-ink); }
.doc-meta [data-tone='bad'] { color: var(--aw-danger); }
.doc-meta [data-tone='agent'] { color: var(--aw-accent); }

.group-by { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; text-align: left; text-transform: capitalize; cursor: pointer; }

/* Deep-search results replace the list where the list was. */
.rail-results { display: flex; flex-direction: column; gap: .3rem; margin-top: .6rem; }
.rail-results-head { display: flex; align-items: baseline; justify-content: space-between; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
.rail-results-head button { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-2xs); cursor: pointer; }
.rail-result { display: flex; flex-direction: column; gap: 2px; width: 100%; padding: .45rem .55rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); text-align: left; cursor: pointer; }
.rail-result:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.rail-result .excerpt { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); line-height: 1.4; }

.vocabulary-card { display: flex; flex-direction: column; gap: .2rem; padding: .7rem .85rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.vocabulary-card.thin { border-color: var(--aw-warn-line); }
.vocabulary-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: .35rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.vocabulary-line b { color: var(--aw-ink-strong); font-size: var(--aw-text-sm); text-transform: capitalize; }
.fields-link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }

.analysis-note { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.analysis-section { display: flex; flex-direction: column; gap: .5rem; }
.analysis-section header { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
.analysis-section h4 { margin: 0; }
.analysis-section small { color: var(--aw-muted); font-size: var(--aw-text-2xs); text-align: right; }

.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }
.pack-toolbar .p-inputtext { flex: 1; }
.pack-list { display: flex; flex-direction: column; gap: .5rem; }
.pack-list article { padding: .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }
.pack-scope { float: right; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-transform: uppercase; letter-spacing: .04em; }
.drawer-foot { display: flex; justify-content: flex-end; gap: .5rem; padding-top: .875rem; border-top: 1px solid var(--aw-border); }
.document-layout { display: grid; flex: 1 1 auto; grid-template-columns: minmax(17rem, 20rem) minmax(0, 1fr); min-height: 0; overflow: hidden; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel); }
/* A running background job, not a problem to be solved. The sentence it used
   to spell out over two lines is on the tooltip. */
.indexing-chip { display:inline-flex; align-items:center; gap:.4rem; min-height:var(--aw-control-height-sm); padding:.2rem .6rem; border:1px solid var(--aw-info-line); border-radius:var(--aw-radius-pill); background:var(--aw-info-soft); color:var(--aw-info); font-size:var(--aw-text-xs); font-weight:600; white-space:nowrap; }
.indexing-chip .pi { font-size:var(--aw-text-xs); }
.document-rail { min-height:0; padding:.75rem; border-right:1px solid var(--aw-border); background:var(--aw-canvas); overflow-y:auto; overscroll-behavior:contain; scrollbar-gutter:stable; }.rail-tools { position:sticky; top:-.75rem; z-index:1; margin:-.75rem -.75rem .75rem; padding:.75rem; border-bottom:1px solid var(--aw-border); background:var(--aw-canvas); }.search-wrap { position:relative; display:block; }.search-wrap > i { position:absolute; z-index:1; left:.75rem; top:50%; translate:0 -50%; color:var(--aw-border-strong); }.rail-search { width:100%; padding-left:2.2rem; }.filters { display:grid; grid-template-columns:1fr; gap:.45rem; margin-top:.5rem; }.filters :deep(.p-select) { min-width:0; font-size:var(--aw-text-sm); }
.doc-group { display:grid; gap:.15rem; }.group-head { display:flex; align-items:center; gap:.4rem; width:100%; margin:.55rem 0 .05rem; padding:.2rem .25rem; border:0; border-radius:var(--aw-radius-control); background:transparent; color:var(--aw-muted); font-size:var(--aw-text-xs); font-weight:700; text-align:left; cursor:pointer; }.group-head:hover { color:var(--aw-teal); }.group-head i { font-size:var(--aw-text-2xs); }.group-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.group-count { margin-left:auto; font-weight:400; }.doc-row { width:100%; display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:.55rem; padding:.3rem .5rem; border:1px solid transparent; border-radius:var(--aw-radius-control); background:transparent; color:inherit; text-align:left; cursor:pointer; transition:border-color .15s, background .15s; }.doc-row:hover { border-color:var(--aw-border); background:var(--aw-panel); }.doc-row.active { border-color:var(--aw-teal-line); background:var(--aw-teal-soft); box-shadow:inset 3px 0 0 var(--aw-teal); }.doc-icon { display:grid; width:1.55rem; height:1.55rem; place-items:center; border-radius:var(--aw-radius-control); color:var(--aw-info); background:var(--aw-info-soft); font-size:var(--aw-text-sm); }.doc-identity { display:grid; min-width:0; gap:.04rem; }.doc-identity strong,.doc-identity small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.doc-identity strong { font-size:var(--aw-text-sm); }.doc-identity small { color:var(--aw-muted); font-size:var(--aw-text-2xs); }.doc-status { display:grid; place-items:center; width:1.1rem; font-size:var(--aw-text-xs); }.doc-status.processing { color:var(--aw-info); }.doc-status.attention { color:var(--aw-warn); }.doc-status.attention.failed { color:var(--aw-danger); }.doc-subgroup { display:flex; align-items:center; gap:.4rem; margin:.35rem 0 .05rem; padding:.1rem .25rem .1rem 1.15rem; color:var(--aw-muted); font-size:var(--aw-text-2xs); font-weight:700; letter-spacing:.02em; text-transform:capitalize; }.subgroup-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.subgroup-count { margin-left:auto; font-weight:400; }.doc-row.nested { margin-left:.9rem; }.rail-empty { padding:2rem .5rem; text-align:center; color:var(--aw-muted); }
.subgroup-thin { color:var(--aw-warn); font-size:var(--aw-text-2xs); }
.subgroup-fields { color:var(--aw-muted); font-weight:400; font-variant-numeric:tabular-nums; }
.vocabulary-panel { display:grid; gap:.5rem; margin:.75rem 0; padding:.7rem .85rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel); }
.vocabulary-panel.thin { border-color:var(--aw-warn); }
.vocabulary-head { display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem; justify-content:space-between; }
.vocabulary-summary { color:var(--aw-muted); font-size:var(--aw-text-xs); }
.vocabulary-warning { display:flex; align-items:flex-start; gap:.45rem; margin:0; color:var(--aw-warn); font-size:var(--aw-text-xs); }
.vocabulary-fields { width:100%; border-collapse:collapse; font-size:var(--aw-text-xs); }
.vocabulary-fields td { padding:.18rem .35rem; border-top:1px solid var(--aw-border); }
.vf-name { font-family:var(--aw-font-mono, monospace); }
.vf-role { color:var(--aw-muted); }
.vf-fill { text-align:right; font-variant-numeric:tabular-nums; }
.vf-fill.partial { color:var(--aw-warn); }
.vf-unread { color:var(--aw-muted); text-align:right; }
.rail-deep-search { display:flex; align-items:center; gap:.45rem; width:100%; margin-top:.7rem; padding:.5rem .6rem; border:1px dashed var(--aw-border); border-radius:var(--aw-radius-control); background:transparent; color:var(--aw-teal); font-size:var(--aw-text-xs); text-align:left; cursor:pointer; }.rail-deep-search:hover { border-color:var(--aw-teal); background:var(--aw-teal-soft); }.rail-deep-search span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.document-detail { min-width:0; min-height:0; display:flex; flex-direction:column; overflow:hidden; }
/* One line: the name, its state, and whatever the viewer does not say itself. */
.detail-head { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:.6rem 1.25rem; border-bottom:1px solid var(--aw-border); }.detail-identity { display:flex; align-items:baseline; gap:.55rem; min-width:0; }.detail-identity h3 { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.detail-identity p { margin:0; color:var(--aw-muted); font-size:var(--aw-text-xs); white-space:nowrap; }.detail-actions { display:flex; align-items:center; gap:.35rem; flex-wrap:wrap; justify-content:flex-end; }
/* Which view, and how that view is set up, on one row. They were a tab bar
   above a tool bar, which spent two bands on one decision. */
.detail-views { display:flex; align-items:center; flex-wrap:wrap; gap:.4rem; padding:.25rem 1.25rem; border-bottom:1px solid var(--aw-border); }
.detail-tabs { display:flex; gap:.15rem; }.detail-tabs button { padding:.35rem .7rem; border:0; border-radius:var(--aw-radius-pill); background:transparent; color:var(--aw-muted); font-size:var(--aw-text-sm); cursor:pointer; text-transform:capitalize; }.detail-tabs button:hover { background:var(--aw-raised); }.detail-tabs button.active { background:var(--aw-teal-soft); color:var(--aw-teal); font-weight:700; }
.page-nav { display:flex; align-items:center; gap:.2rem; margin-left:.35rem; color:var(--aw-muted); font-size:var(--aw-text-sm); white-space:nowrap; }
.detail-views .find-on { color:var(--aw-teal); font-weight:700; }
.open-original { margin-left:auto; color:var(--aw-teal); font-size:var(--aw-text-sm); white-space:nowrap; }
/* A column, so the viewer can take the height the bars above it gave back. */
.detail-content { flex:1 1 auto; min-height:0; padding:1.25rem; overflow-y:auto; overscroll-behavior:contain; }.preview-view { display:flex; flex-direction:column; min-height:100%; }.page-text { min-height:25rem; margin:0; padding:1.35rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel); font-family:var(--aw-font-sans); white-space:pre-wrap; line-height:1.65; box-shadow: var(--aw-shadow-sm); }.scan-notice { display:flex; gap:.75rem; padding:.9rem; margin-bottom:.75rem; border:1px solid var(--aw-warn-line); border-radius:var(--aw-radius-control); background:var(--aw-warn-soft); }.scan-notice p { margin:.25rem 0 0; }.document-image { display:block; max-width:100%; max-height:34rem; margin:auto; }/* `flex: 1`, not `calc(100vh - 26rem)`: the old rule subtracted a hard-coded
   guess at the chrome above it, so trimming that chrome would have handed the
   height back as whitespace rather than as document. */
.document-frame { width:100%; flex:1 1 auto; min-height:24rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel); }.docx-frame { flex:1 1 auto; min-height:24rem; overflow:auto; border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-raised); }.docx-frame.loading { opacity:.5; }.docx-frame :deep(.docx-wrapper) { background:var(--aw-raised); padding:1.25rem; }.docx-frame :deep(.docx-wrapper > section.docx) { margin-bottom:1rem; box-shadow: var(--aw-shadow-md); }.source-toggle { display:flex; margin-left:.5rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-pill); overflow:hidden; }.source-toggle button { padding:.28rem .75rem; border:0; background:transparent; color:var(--aw-muted); font-size:var(--aw-text-xs); cursor:pointer; }.source-toggle button.active { background:var(--aw-teal-soft); color:var(--aw-teal); font-weight:600; }
.classification-field { display:flex; align-items:center; gap:.4rem; color:var(--aw-muted); font-size:var(--aw-text-xs); }.classification-field :deep(.p-select) { min-width:8rem; min-height:2rem; font-size:var(--aw-text-sm); text-transform:capitalize; }.classification-field.read-only strong { color:var(--aw-ink); font-size:var(--aw-text-sm); font-weight:600; text-transform:capitalize; }
.preview-view .source-search-bar { display:flex; gap:.4rem; }
.preview-view .source-search-bar,.preview-view .inline-search-results { flex:none; margin-bottom:.75rem; }
.preview-view .scan-notice,.preview-view .document-image,.preview-view .page-text,.preview-view .technical-details { flex:none; }.preview-view .source-search-bar .p-inputtext { max-width:24rem; }
.technical-details,.timeline details,.pack-grid details { margin-top:.8rem; padding:.65rem .75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-canvas); color:var(--aw-muted); font-size:var(--aw-text-xs); }.technical-details summary,.timeline summary,.pack-grid summary { cursor:pointer; font-weight:600; }.technical-details dl { display:grid; gap:.45rem; margin:.7rem 0 0; }.technical-details dl div { display:grid; grid-template-columns:7rem minmax(0,1fr); gap:.6rem; }.technical-details dt { font-weight:600; }.technical-details dd { display:flex; align-items:center; gap:.3rem; margin:0; overflow-wrap:anywhere; }.technical-details dd code { flex:1; min-width:0; overflow-wrap:anywhere; }
.timeline { display: grid; gap: .75rem; }.timeline article { display: grid; grid-template-columns: auto 1fr; gap: .75rem; padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }.timeline p { margin: .25rem 0; color: var(--aw-muted); }.timeline code { overflow-wrap: anywhere; font-size: var(--aw-text-xs); }.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }.pack-toolbar .p-inputtext { flex: 1; }.pack-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(14rem,1fr)); gap: .65rem; }.pack-grid article,.search-results article { padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }.pack-grid .p-tag { float: right; }.pack-grid p,.search-results p { margin: .4rem 0 0; color: var(--aw-muted); }.search-results { margin-top: 1.2rem; display: grid; gap: .55rem; }
.analysis-view { display:grid; gap:1rem; }.analysis-toolbar,.analysis-actions,.analysis-states,.save-analysis,.source-search-bar,.global-search-bar,.candidate-actions { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }.analysis-toolbar { justify-content:space-between; }.coverage-warning { display:flex; align-items:flex-start; gap:.6rem; padding:.75rem; border:1px solid var(--aw-warn-line); border-radius:var(--aw-radius-control); background:var(--aw-warn-soft); color:var(--aw-warn-ink); }.coverage-warning p { margin:.25rem 0 0; }.coverage-warning ul { margin:.35rem 0 0; padding-left:1.1rem; }.source-search-bar .p-inputtext,.global-search-bar .p-inputtext { flex:1; min-width:14rem; }.analysis-editor,.analysis-fields { padding:1rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-panel); }.analysis-editor header { display:flex; justify-content:space-between; gap:1rem; margin-bottom:.8rem; }.analysis-editor h4,.analysis-sources h4,.candidate-compare h4 { margin:0; }.analysis-editor small,.analysis-fields small { color:var(--aw-muted); }.analysis-editor :deep(.markdown-editor) { min-height:18rem; }.analysis-fields>summary { display:flex; align-items:center; justify-content:space-between; gap:1rem; cursor:pointer; list-style:none; }.analysis-fields>summary::-webkit-details-marker { display:none; }.analysis-fields>summary span { display:grid; gap:.2rem; }.analysis-fields>summary i { transition:transform .15s ease; }.analysis-fields[open]>summary i { transform:rotate(180deg); }.analysis-fields pre { max-height:32rem; margin:.8rem 0 0; overflow:auto; padding:1rem; border-radius:var(--aw-radius-control); background:var(--aw-canvas); color:var(--aw-ink); font:500 .78rem/1.55 var(--aw-font-mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace); white-space:pre; }.inline-search-results,.analysis-sources,.global-search-results { display:grid; gap:.5rem; }.inline-search-results button,.analysis-sources button,.global-search-results button { display:grid; gap:.35rem; width:100%; padding:.75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-panel); color:inherit; text-align:left; cursor:pointer; }.inline-search-results button:hover,.analysis-sources button:hover,.global-search-results button:hover { border-color:var(--aw-teal); }.inline-search-results span,.analysis-sources span,.global-search-results p { color:var(--aw-muted); line-height:1.45; }.analysis-sources button strong { display:flex; align-items:center; gap:.45rem; }.candidate-compare { display:grid; gap:.75rem; padding:1rem; border:1px solid var(--aw-info-line); border-radius:var(--aw-radius-control); background:var(--aw-info-soft); }.candidate-compare > div:not(.candidate-actions) { display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }.candidate-compare article { padding:.75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control); background:var(--aw-panel); }.global-search-results { margin-top:1rem; }.global-search-results button > div { display:flex; justify-content:space-between; align-items:center; }.global-search-results button > div > span { display:flex; gap:.35rem; }.global-search-results p { margin:.2rem 0; }.global-search-results small { color:var(--aw-muted); }.vision-settings { display:grid; gap:1rem; }.vision-settings > p { margin:0; color:var(--aw-muted); line-height:1.5; }.vision-settings label { display:grid; gap:.35rem; font-weight:600; }.vision-settings label :deep(.p-select),.vision-settings label .p-inputtext { width:100%; }.vision-settings .settings-warning { padding:.65rem; border-radius:var(--aw-radius-control); background:var(--aw-warn-soft); color:var(--aw-warn-ink); }
@media (max-width: 900px) { .document-layout { grid-template-columns: 1fr; }.document-rail { max-height: 20rem; border-right: 0; border-bottom: 1px solid var(--aw-border); }.detail-head { align-items:flex-start; flex-direction:column; }.detail-identity { flex-wrap:wrap; }.detail-actions { justify-content:flex-start; }.open-original { margin-left:0; } }
</style>
