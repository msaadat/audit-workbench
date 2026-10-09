<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { renderAsync } from 'docx-preview'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
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
import DocumentTypeReview from './documents/DocumentTypeReview.vue'
import StructuredEvidenceSheet from './documents/StructuredEvidenceSheet.vue'
import {
  DOCUMENT_QUEUES, documentLabel, documentMeta, documentTone, documentsStatus, filterDocuments, isReviewed, needsReview,
} from './documents/documentsStatus'
import type { DocumentsFilter } from './documents/documentsStatus'
import { plural, sentenceCase } from '../format'

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
/**
 * The page is three panes — the list, the original, and what was read from it
 * — because reviewing a reading means looking at the source while you do. The
 * reading used to be a tab *instead of* the original, so checking one claim
 * against its page was a round trip between two tabs.
 *
 * `readingTab` is which face of the reading is showing. `pane` only matters
 * when the page is too narrow for three columns (the assistant open beside it,
 * a laptop): the original and the reading then share one column, and this is
 * which of them it holds.
 */
const readingTab = ref<'reading' | 'notes' | 'activity'>('reading')
const pane = ref<'original' | 'reading'>('original')
const editingReading = ref(false)
const search = ref('')
const statusFilter = ref<DocumentsFilter[]>([])
const groupBy = ref<'type' | 'folder' | 'status'>('type')
const collapsedGroups = ref<Set<string>>(new Set())
const sourceView = ref<'original' | 'text' | 'fields'>('original')
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
const documentCategoryOptions = categories.map(value => ({ value, label: sentenceCase(value) }))
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
/**
 * The filters, as one choice. The page drew two rows of count chips and three
 * progress lanes under them, which said each number twice (`83 Analysis to
 * review` beside `REVIEWED 0/84`). The counts now sit in the header sentence
 * once, and the filter is one segmented control: All, To review, and whatever
 * else currently has something in it. Who typed a document is a different
 * axis, so it is a checkbox beside the control rather than another segment.
 */
const filterCounts = computed(() => new Map(
  (status.value.filters ?? []).flatMap(group => group.options.map(option => [option.key, option.value] as const)),
))
const queueFilter = computed<DocumentsFilter | ''>(
  () => statusFilter.value.find(key => key !== 'model_typed') ?? '',
)
const queues = computed(() => DOCUMENT_QUEUES
  .filter(option => !option.key || option.key === 'needs_review'
    || (filterCounts.value.get(option.key) ?? 0) > 0 || option.key === queueFilter.value)
  .map(option => ({ ...option, count: option.key ? filterCounts.value.get(option.key) ?? 0 : documents.value.length })))
const modelTypedOnly = computed({
  get: () => statusFilter.value.includes('model_typed'),
  set: (on: boolean) => {
    statusFilter.value = [...(queueFilter.value ? [queueFilter.value] : []), ...(on ? ['model_typed' as const] : [])]
  },
})
function setQueue(key: DocumentsFilter | '') {
  statusFilter.value = [...(key ? [key] : []), ...(modelTypedOnly.value ? ['model_typed' as const] : [])]
}
/** `84 documents · 84 read · 83 analysed · 0 reviewed` — the three lanes, said once. */
const countSentence = computed(() => {
  const lane = (key: string) => status.value.lanes.find(item => item.key === key)?.value ?? '0'
  return [
    plural(documents.value.length, 'document'),
    `${lane('read')} read`, `${lane('analysed')} analysed`, `${lane('reviewed')} reviewed`,
  ].join(' · ')
})

/**
 * Review walks the list in the order it is drawn. `Mark reviewed and next`
 * lands on the next document after this one that still needs review, wrapping
 * once, so working down a group is one button per document.
 */
const ordered = computed(() => groups.value.flatMap(group => group.items))
const reviewQueue = computed(() => ordered.value.filter(needsReview))
const nextToReview = computed(() => {
  const list = ordered.value
  const at = list.findIndex(doc => doc.id === selectedId.value)
  const after = [...list.slice(at + 1), ...list.slice(0, Math.max(at, 0))]
  return after.find(doc => needsReview(doc) && doc.id !== selectedId.value) ?? null
})
const queuePosition = computed(() => {
  const queue = reviewQueue.value
  if (!queue.length) return 'Nothing left to review'
  const at = queue.findIndex(doc => doc.id === selectedId.value)
  return at >= 0 ? `${at + 1} of ${queue.length} to review` : `${queue.length} to review`
})

async function markReviewedAndNext() {
  const next = nextToReview.value
  if (!(await saveAnalysis(true))) return
  if (next) await selectDocument(next.id, 1)
}

/** Who wrote the reading the pane shows, and when — a model reading is marked as one. */
const readingSource = computed(() => {
  const effective = analysis.value?.effective
  if (!effective) return null
  const edited = analysis.value?.review.summary_override != null || analysis.value?.review.audit_notes_override != null
  const at = effective.generated_at ? new Date(effective.generated_at) : null
  const when = at && !Number.isNaN(at.getTime())
    ? at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    : ''
  const pages = effective.coverage?.analyzed_pages?.length ?? 0
  return {
    edited,
    meta: [when, pages ? `${plural(pages, 'page')} read` : ''].filter(Boolean).join(' · '),
  }
})

const readingActions = computed<MenuItem[]>(() => {
  const doc = selected.value
  const items: MenuItem[] = []
  if (!doc) return items
  items.push(analysis.value?.generated
    ? { label: 'Refresh the reading', icon: 'aw-icon aw-icon-refresh-cw', disabled: analysisBusy.value, command: () => void startAnalysis('refresh') }
    : { label: 'Analyse this document', icon: 'aw-icon aw-icon-sparkles', disabled: analysisBusy.value, command: () => void startAnalysis('analyze') })
  if (doc.text_state === 'extracted' || doc.text_state === 'partial') {
    items.push({
      label: fullVisualCoverage.value ? `Full visual coverage is on (max ${visualPageLimit} pages)` : 'Read pages visually too',
      icon: fullVisualCoverage.value ? 'aw-icon aw-icon-images' : 'aw-icon aw-icon-file',
      command: () => { fullVisualCoverage.value = !fullVisualCoverage.value },
    })
  }
  if (isEvidence.value) {
    items.push({ label: 'Revise this type’s vocabulary', icon: 'aw-icon aw-icon-list-checks', disabled: analysisBusy.value, command: () => void startAnalysis('revise_vocabulary') })
  }
  if (analysis.value?.candidate) {
    items.push({ label: 'Compare the refreshed reading', icon: 'aw-icon aw-icon-git-compare', command: () => { compareCandidate.value = !compareCandidate.value } })
  }
  if (analysis.value?.review.summary_override != null) {
    items.push({ label: 'Revert to the generated summary', icon: 'aw-icon aw-icon-undo-2', command: () => void revertAnalysisField('summary') })
  }
  return items
})

function cancelReadingEdit() {
  summaryDraft.value = analysis.value?.effective?.summary_markdown || ''
  editingReading.value = false
}

async function saveReadingEdit() {
  if (await saveAnalysis(false)) editingReading.value = false
}

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
/**
 * The structured evidence is a third way of looking at the document, beside
 * the original and its extracted text, rather than a block in the reading
 * pane. It is the page restated as fields, and it needs the page's width: in
 * the 24rem pane its values wrapped a character at a time.
 */
const hasRecords = computed(() => Boolean(analysis.value?.effective?.records?.length))
const showFields = computed(() => sourceView.value === 'fields' && hasRecords.value)
const showTextView = computed(() => !isImage.value && !showFields.value
  && (!hasOriginalView.value || sourceView.value === 'text'))
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
  { label: 'Reindex search', icon: 'aw-icon aw-icon-refresh-ccw', command: () => void reindexAll() },
  { label: 'Methodology knowledge', icon: 'aw-icon aw-icon-book-open', command: () => void openKnowledge() },
  // Server-wide assistant configuration, so an administrator's to set. A
  // non-admin still needs to know whether it is available, which the label says.
  {
    label: canConfigureVision.value
      ? (visionAvailable.value ? 'Vision profile' : 'Configure vision')
      : (visionAvailable.value ? 'Vision configured' : 'Vision not configured'),
    icon: visionAvailable.value ? 'aw-icon aw-icon-eye' : 'aw-icon aw-icon-settings',
    disabled: !canConfigureVision.value,
    command: () => void openVisionSettings(),
  },
])
const documentActions = computed<MenuItem[]>(() => [
  { label: 'Re-extract text', icon: 'aw-icon aw-icon-refresh-cw', command: () => void reextract() },
  { label: 'Delete document', icon: 'aw-icon aw-icon-trash-2', command: () => remove() },
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
    editingReading.value = false
    compareCandidate.value = false
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

async function saveAnalysis(reviewed = false): Promise<boolean> {
  if (!selected.value || !analysis.value) return false
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
    toast.add({ severity: 'success', summary: reviewed ? 'Reading reviewed' : 'Reading edits saved', life: 2200 })
    return true
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Reading not saved', detail: String(error), life: 5000 })
    return false
  } finally { analysisBusy.value = false }
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
  pane.value = 'original'; sourceView.value = 'text'
}

async function openCitation(citation: DocumentAnalysisCitation) {
  if (!selected.value) return
  currentPage.value = citation.page
  pane.value = 'original'
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
    icon: 'aw-icon aw-icon-triangle-alert',
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
  if (!selected.value || !isDocx.value || sourceView.value !== 'original') return
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

watch([() => selected.value?.id, sourceView], () => { void renderDocx() }, { flush: 'post' })
// A document with nothing structured to show falls back to its original
// rather than to a blank Fields view the toggle no longer offers.
watch(hasRecords, has => { if (!has && sourceView.value === 'fields') sourceView.value = 'original' })



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
      <div class="head-copy">
        <h1>Documents</h1>
        <p v-if="documents.length" class="aw-type-meta head-count">{{ countSentence }}</p>
      </div>
      <span class="grow" />
      <!-- A background job, reported at the size of a background job. -->
      <span
        v-if="indexingActive"
        class="indexing-chip"
        role="status"
        aria-live="polite"
        v-tooltip.bottom="indexingDetail"
      >
        <i class="aw-icon aw-icon-spin aw-icon-loader-circle" />Indexing<template v-if="indexingProgress"> {{ indexingProgress }}</template>
      </span>
      <!-- All three are neutral. The page's one filled button is the review
           itself — `Mark reviewed and next`, where the reading ends — and
           these are the chores that feed it. -->
      <Button
        v-if="unidentifiedCount"
        :label="`Identify ${unidentifiedCount}`"
        icon="aw-icon aw-icon-circle-help"
        size="small"
        severity="secondary"
        outlined
        @click="typeReviewOpen = true"
      />
      <Button
        v-if="eligibleDocuments.length"
        :label="`Analyse ${eligibleDocuments.length}`"
        icon="aw-icon aw-icon-sparkles"
        size="small"
        severity="secondary"
        outlined
        :loading="analysisBusy"
        @click="batchAnalyze"
      />
      <Button v-if="documents.length" label="Add documents" icon="aw-icon aw-icon-plus" size="small" outlined severity="secondary" @click="emit('import-requested')" />
      <UiOverflowMenu :items="secondaryActions" tooltip="More document actions" />
    </header>

    <div v-if="documents.length" class="aw-filter-row">
      <div class="aw-segmented" role="group" aria-label="Show">
        <button
          v-for="option in queues"
          :key="option.key || 'all'"
          type="button"
          :class="{ on: queueFilter === option.key }"
          :aria-pressed="queueFilter === option.key"
          @click="setQueue(option.key)"
        >{{ option.label }} <span class="aw-figure">{{ option.count }}</span></button>
      </div>
      <label v-if="filterCounts.get('model_typed')" class="aw-filter-check">
        <input v-model="modelTypedOnly" type="checkbox">
        Only types the assistant assigned <span class="aw-figure">{{ filterCounts.get('model_typed') }}</span>
      </label>
    </div>

    <div v-if="documents.length" class="document-layout">
      <div class="document-grid" :data-pane="pane">
        <aside class="document-rail" aria-label="Documents">
          <div class="rail-tools">
            <IconField>
              <InputIcon class="aw-icon aw-icon-search" />
              <InputText v-model="search" size="small" :placeholder="`Search ${plural(filtered.length, 'document')}`" />
            </IconField>
            <!-- A link, not a full-width select: grouping is chosen once and
                 then read past. -->
            <button type="button" class="group-by" @click="cycleGrouping">By {{ groupBy }} <i class="aw-icon aw-icon-chevron-down" aria-hidden="true" /></button>
          </div>
          <div v-if="!filtered.length" class="rail-empty">No document matches this view.</div>
          <div v-for="group in groups" :key="group.key" class="doc-group">
            <button class="group-head" :aria-expanded="!collapsedGroups.has(group.key)" @click="toggleGroup(group.key)">
              <i :class="collapsedGroups.has(group.key) ? 'aw-icon aw-icon-chevron-right' : 'aw-icon aw-icon-chevron-down'" />
              <span class="group-name">{{ sentenceCase(group.label) }}</span>
              <span class="group-count aw-figure">{{ group.items.length }}</span>
            </button>
            <template v-if="!collapsedGroups.has(group.key)">
              <button
                v-for="doc in group.items"
                :key="doc.id"
                class="doc-row"
                :class="{ active: doc.id === selectedId }"
                :title="doc.source"
                @click="selectDocument(doc.id, 1)"
              >
                <span class="dot" :data-tone="documentTone(doc, documentFacts)" aria-hidden="true" />
                <span class="doc-identity">
                  <span v-if="documentLabel(doc).reference" class="doc-name doc-ref">{{ documentLabel(doc).reference }}</span>
                  <span v-else class="doc-name">{{ documentLabel(doc).name }}</span>
                  <span class="doc-meta">
                    <template v-if="documentMeta(doc, documentFacts).length">
                      <template v-for="(part, index) in documentMeta(doc, documentFacts)" :key="part.text">
                        <span v-if="index" aria-hidden="true"> · </span><span :data-tone="part.tone">{{ part.text }}</span>
                      </template>
                    </template>
                    <template v-else-if="documentLabel(doc).reference">{{ documentLabel(doc).name }}</template>
                  </span>
                </span>
                <i v-if="isReviewed(doc)" class="aw-icon aw-icon-check reviewed-mark" role="img" aria-label="Reviewed" title="Reviewed" />
              </button>
            </template>
          </div>
          <button v-if="search.trim()" class="rail-deep-search" @click="runContentSearch()">
            <i class="aw-icon aw-icon-search" /><span>Search inside documents for “{{ search.trim() }}”</span>
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
              <span class="doc-meta">Page {{ result.page }}</span>
              <span class="excerpt">{{ result.excerpt }}</span>
            </button>
          </div>
        </aside>

        <!-- Only drawn when the original and the reading share one column. -->
        <div v-if="selected" class="pane-switch" role="group" aria-label="Show">
          <button type="button" :class="{ on: pane === 'original' }" :aria-pressed="pane === 'original'" @click="pane = 'original'">Original</button>
          <button type="button" :class="{ on: pane === 'reading' }" :aria-pressed="pane === 'reading'" @click="pane = 'reading'">
            Reading<i v-if="selected && needsReview(selected)" class="switch-dot" aria-label="needs review" />
          </button>
        </div>

        <!-- The original. One row of controls: what it is held as, its name,
             and how it is being looked at. Review lives with the reading. -->
        <main v-if="selected" class="viewer-pane">
          <header class="viewer-head">
            <Select
              class="held-select"
              :modelValue="selected.category"
              :options="documentCategoryOptions"
              optionLabel="label"
              optionValue="value"
              :disabled="classificationBusy"
              placeholder="Not yet read"
              size="small"
              aria-label="What this engagement holds the document as"
              v-tooltip.bottom="'Held as'"
              @update:modelValue="updateClassification"
            />
            <h2 :title="selected.source">{{ selected.source }}</h2>
            <span class="grow" />
            <span v-if="showPageNav" class="page-nav">
              <Button icon="aw-icon aw-icon-chevron-left" text size="small" :disabled="currentPage <= 1" aria-label="Previous page" @click="currentPage--" /><span class="aw-figure">{{ currentPage }} / {{ selected.pages || previewPages.length || 1 }}</span><Button icon="aw-icon aw-icon-chevron-right" text size="small" :disabled="currentPage >= (selected.pages || previewPages.length || 1)" aria-label="Next page" @click="currentPage++" />
            </span>
            <div v-if="hasOriginalView || hasRecords" class="source-toggle" role="group" aria-label="Preview mode">
              <button v-if="hasOriginalView || isImage" :class="{ active: !showFields && sourceView === 'original' }" @click="sourceView = 'original'">Original</button>
              <button v-if="!isImage" :class="{ active: showTextView }" @click="sourceView = 'text'">Extracted text</button>
              <button v-if="hasRecords" :class="{ active: showFields }" @click="sourceView = 'fields'">Fields</button>
            </div>
            <Button
              icon="aw-icon aw-icon-search"
              size="small"
              text
              severity="secondary"
              :class="{ 'find-on': showDocumentSearch }"
              aria-label="Find in this document"
              v-tooltip.bottom="'Find in this document'"
              @click="toggleFind"
            />
            <a :href="fileUrl" target="_blank" class="icon-link" aria-label="Open the original file" v-tooltip.bottom="'Open the original file'">
              <i class="aw-icon aw-icon-external-link" aria-hidden="true" />
            </a>
            <Button
              icon="aw-icon aw-icon-paperclip"
              size="small"
              text
              severity="secondary"
              aria-label="Add to the assistant's context"
              v-tooltip.bottom="'Add to the assistant’s context'"
              @click="attachToAssistant"
            />
            <UiOverflowMenu :items="documentActions" tooltip="Document actions" />
          </header>

          <div class="detail-content preview-view">
            <div v-if="showDocumentSearch" class="source-search-bar">
              <InputText
                ref="findInput"
                v-model="sourceSearch"
                placeholder="Search this document's text and transcripts"
                @keyup.enter="runContentSearch(selected ? [selected.id] : [])"
              />
              <Button label="Search" icon="aw-icon aw-icon-search" severity="secondary" outlined :loading="searchBusy" @click="runContentSearch(selected ? [selected.id] : [])" />
            </div>
            <div v-if="sourceResults.length && sourceSearch" class="inline-search-results">
              <button v-for="result in sourceResults" :key="result.citation_id" @click="openSearchResult(result)"><strong>Page {{ result.page }}</strong><span>{{ result.excerpt }}</span></button>
            </div>
            <div v-if="current?.image_only && showTextView" class="scan-notice">
              <i class="aw-icon aw-icon-image" />
              <div>
                <strong>{{ selected.analysis_vision_used && selected.analysis_validity_state === 'current' ? 'Visual source—analysis available' : 'Visual source' }}</strong>
                <p v-if="selected.analysis_vision_used && selected.analysis_validity_state === 'current'">The extracted-text view is empty, but the current reading includes an AI-derived visual transcription.</p>
                <p v-else-if="visionAvailable">This page has insufficient extractable text. Analyse it with the configured vision profile; the original remains the authoritative source.</p>
                <p v-else>This page has insufficient extractable text. You can start analysis now; it will remain an open item without a model charge until a vision profile is configured.</p>
              </div>
            </div>
            <section v-if="showFields && analysis?.effective" class="fields-view">
              <StructuredEvidenceSheet
                :records="analysis.effective.records ?? []"
                :schema="analysis.effective.schema_ref"
                :citations="analysis.effective.citations"
                :validated="analysis.status.analysis_coverage_state === 'complete'"
              />
            </section>
            <img v-else-if="isImage" class="document-image" :src="fileUrl" :alt="selected.title" />
            <iframe v-else-if="isPdf && sourceView === 'original'" :key="`${selected.id}:${currentPage}`" class="document-frame" :src="`${fileUrl}#page=${currentPage}`" :title="selected.title" />
            <div v-else-if="isDocx && sourceView === 'original'" :key="selected.id" ref="docxContainer" class="docx-frame" :class="{ loading: docxLoading }" :aria-busy="docxLoading" />
            <pre v-else class="page-text">{{ current?.text || 'No extractable text on this page.' }}</pre>
          </div>
        </main>

        <!-- What was read from it, and the review of that reading. -->
        <aside v-if="selected" class="reading-pane" aria-label="Reading">
          <nav class="reading-tabs">
            <button type="button" :class="{ on: readingTab === 'reading' }" @click="readingTab = 'reading'">Reading</button>
            <button type="button" :class="{ on: readingTab === 'notes' }" @click="readingTab = 'notes'">Audit notes</button>
            <button type="button" :class="{ on: readingTab === 'activity' }" @click="readingTab = 'activity'">
              Activity<span v-if="activity.length" class="tab-badge aw-figure">{{ activity.length }}</span>
            </button>
            <span class="grow" />
            <UiOverflowMenu v-if="readingTab === 'reading'" :items="readingActions" tooltip="Reading actions" />
          </nav>

          <!-- The two states that need a sentence. -->
          <p v-if="selected.analysis_validity_state === 'stale'" class="strip warn">
            <i class="aw-icon aw-icon-history" aria-hidden="true" />
            <span>This reading was made against an earlier version of the file. Refresh it before relying on it.</span>
            <button type="button" :disabled="analysisBusy" @click="startAnalysis('refresh')">Refresh</button>
          </p>
          <p v-else-if="selected.candidate_analysis_id" class="strip info">
            <i class="aw-icon aw-icon-git-compare" aria-hidden="true" />
            <span>A refreshed reading is waiting.</span>
            <button type="button" @click="readingTab = 'reading'; compareCandidate = true">Compare</button>
          </p>

          <div class="reading-body">
            <template v-if="readingTab === 'reading'">
              <UiEmptyState
                v-if="!analysis?.effective"
                icon="aw-icon aw-icon-sparkles"
                title="Not read yet"
                description="Nothing has been read from this file. The original remains the authoritative source; a reading is what the matrix and the tests draw on."
                compact
              >
                <Button label="Analyse this document" icon="aw-icon aw-icon-sparkles" :loading="analysisBusy" @click="startAnalysis('analyze')" />
              </UiEmptyState>
              <template v-else>
                <p v-if="readingSource" class="reading-source">
                  <span class="by" :data-edited="readingSource.edited || null">
                    <i class="aw-icon" :class="readingSource.edited ? 'aw-icon-user-pen' : 'aw-icon-sparkles'" aria-hidden="true" />
                    {{ readingSource.edited ? 'Edited by an auditor' : 'Written by the assistant' }}
                  </span>
                  <span v-if="readingSource.meta" class="aw-type-meta">{{ readingSource.meta }}</span>
                </p>

                <section v-if="selectedVocabulary" class="vocabulary-card" :class="{ thin: selectedVocabulary.thin }">
                  <p class="vocabulary-line">
                    <b>Read as {{ sentenceCase(selectedVocabulary.document_type) }}</b>
                    <span>
                      ·
                      <button type="button" class="fields-link" @click="fieldsOpen = !fieldsOpen">
                        {{ plural(selectedVocabulary.fields.length, 'field') }}
                        <i class="aw-icon" :class="fieldsOpen ? 'aw-icon-chevron-down' : 'aw-icon-chevron-right'" />
                      </button>
                      from {{ plural(selectedVocabulary.documents_read.length, 'document') }}
                    </span>
                  </p>
                  <p v-if="selectedVocabulary.thin" class="strip warn inline">
                    <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />
                    <span>{{ thinReason(selectedVocabulary) }}</span>
                  </p>
                  <table v-if="fieldsOpen" class="vocabulary-fields">
                    <tbody>
                      <tr v-for="field in selectedVocabulary.fields" :key="field.name">
                        <td class="vf-name">{{ field.name }}</td>
                        <td class="vf-role">{{ field.role }}</td>
                        <td class="vf-fill aw-figure" :class="{ partial: field.fill_count < selectedVocabulary.documents_read.length }">
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

                <div v-if="analysis.status.analysis_coverage_state === 'partial'" class="coverage-warning">
                  <i class="aw-icon aw-icon-triangle-alert" />
                  <div>
                    <strong>Partial source coverage</strong>
                    <p>Text pages: {{ analysis.effective.coverage.text_analyzed_pages?.join(', ') || '—' }} · Visual pages: {{ analysis.effective.coverage.vision_analyzed_pages?.join(', ') || '—' }}</p>
                    <ul v-if="analysis.effective.coverage.omissions?.length">
                      <li v-for="item in analysis.effective.coverage.omissions" :key="`${item.page}:${item.reason}`">Page {{ item.page }} — {{ item.reason.replaceAll('_', ' ') }}</li>
                    </ul>
                    <p v-else>Omitted pages: {{ analysis.effective.coverage.omitted_pages.join(', ') || '—' }}</p>
                  </div>
                </div>

                <!-- A summary whose origin is the structured evidence is that
                     evidence written out as bullets; the sheet is the better
                     rendering of it, so only a model-written summary is drawn. -->
                <template v-if="!hasStructuredSummary">
                  <MarkdownEditor v-if="editingReading" v-model="summaryDraft" class="reading-editor" />
                  <MarkdownView v-else :markdown="summaryDraft || '_No summary was written._'" class="reading-text" />
                </template>
                <button
                  v-if="analysis.effective.records?.length"
                  type="button"
                  class="fields-door"
                  @click="sourceView = 'fields'; pane = 'original'"
                >
                  <i class="aw-icon aw-icon-table" aria-hidden="true" />
                  <span><b>Structured evidence</b> · {{ plural(analysis.effective.records.length, 'record') }} read against the {{ sentenceCase(analysis.effective.schema_ref?.document_type || 'document') }} schema</span>
                  <i class="aw-icon aw-icon-arrow-right" aria-hidden="true" />
                </button>

                <section v-if="compareCandidate && analysis.candidate" class="candidate-compare">
                  <h4>The refreshed reading</h4>
                  <MarkdownView :markdown="analysis.candidate.summary_markdown" />
                  <div class="candidate-actions">
                    <Button v-if="!hasStructuredSummary" label="Use its summary" size="small" severity="secondary" outlined @click="summaryDraft = analysis.candidate.summary_markdown; editingReading = true" />
                    <Button label="Use its notes" size="small" severity="secondary" outlined @click="notesDraft = analysis.candidate.audit_notes_markdown; readingTab = 'notes'" />
                    <Button label="Accept it as the reading" icon="aw-icon aw-icon-check" size="small" severity="secondary" outlined @click="acceptCandidate" />
                  </div>
                </section>

                <section v-if="analysis.effective.citations.length" class="analysis-sources">
                  <h4 class="aw-label">Where it says so</h4>
                  <button v-for="citation in analysis.effective.citations" :key="`${citation.id}:${citation.page}`" type="button" @click="openCitation(citation)">
                    <strong>
                      <span class="aw-figure">{{ citation.id }}</span> · Page {{ citation.page }}
                      <span v-if="citation.evidence_kind === 'visual'" class="visual-tag">AI visual description</span>
                    </strong>
                    <span v-if="citation.evidence_kind === 'visual'">{{ citation.description || 'Visual region' }}</span>
                    <span v-else>{{ citation.excerpt }}</span>
                  </button>
                </section>
                <p v-else class="muted">No validated source citations were generated.</p>

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
            </template>

            <template v-else-if="readingTab === 'notes'">
              <p class="aw-type-meta notes-hint">Freeform observations — what an auditor noticed. They are not evidence that a control operated.</p>
              <MarkdownEditor v-model="notesDraft" class="reading-editor" />
              <div class="notes-actions">
                <Button v-if="analysis?.review.audit_notes_override != null" label="Revert to generated" text size="small" severity="secondary" @click="revertAnalysisField('notes')" />
                <span class="grow" />
                <Button label="Save notes" icon="aw-icon aw-icon-save" size="small" severity="secondary" outlined :disabled="!analysis?.effective" :loading="analysisBusy" @click="saveAnalysis(false)" />
              </div>
            </template>

            <div v-else class="timeline">
              <article v-for="item in activity" :key="item.id"><i class="aw-icon aw-icon-sparkles" /><div><strong>{{ sentenceCase(item.purpose) }} · {{ item.disposition }}</strong><p>{{ item.at }} · {{ item.provider }} / {{ item.model }}</p><p>Pages {{ item.page_ranges?.join(', ') || '—' }}</p><details><summary>Technical details</summary><code>{{ item.id }} · response {{ item.response_hash || 'not available' }}</code></details></div></article>
              <p v-if="!activity.length" class="muted">No model activity references this document.</p>
              <details class="technical-details">
                <summary>Technical details</summary>
                <dl><div><dt>Document ID</dt><dd><code>{{ selected.id }}</code><Button icon="aw-icon aw-icon-copy" text rounded size="small" aria-label="Copy document ID" @click="copyText(selected.id, 'Document ID')" /></dd></div><div><dt>Content hash</dt><dd><code>{{ selected.sha1 }}</code><Button icon="aw-icon aw-icon-copy" text rounded size="small" aria-label="Copy content hash" @click="copyText(selected.sha1, 'Content hash')" /></dd></div><div><dt>Stored file</dt><dd><code>{{ selected.file }}</code></dd></div><div v-if="selected.relative_path"><dt>Imported path</dt><dd>{{ selected.relative_path }}</dd></div><div><dt>Added</dt><dd>{{ selected.created }}</dd></div><div v-if="selected.updated"><dt>Replaced</dt><dd>{{ selected.updated }}</dd></div></dl>
              </details>
            </div>
          </div>

          <!-- The review, where the reading ends: the eye reaches it having
               read the thing it vouches for. The page's one filled button. -->
          <footer v-if="analysis?.effective" class="reading-foot">
            <!-- Its own line: beside the two buttons it was what the pane's
                 width could not fit, and both buttons clipped to make room. -->
            <span class="queue-position aw-type-meta">{{ queuePosition }}</span>
            <template v-if="editingReading">
              <Button label="Cancel" size="small" text severity="secondary" @click="cancelReadingEdit" />
              <Button label="Save edits" icon="aw-icon aw-icon-save" size="small" severity="secondary" outlined :loading="analysisBusy" @click="saveReadingEdit" />
            </template>
            <Button
              v-else-if="readingTab === 'reading' && !hasStructuredSummary"
              label="Edit reading"
              icon="aw-icon aw-icon-pencil"
              size="small"
              severity="secondary"
              outlined
              @click="editingReading = true"
            />
            <span class="grow" />
            <Button
              v-if="!isReviewed(selected)"
              :label="nextToReview ? 'Mark reviewed and next' : 'Mark reviewed'"
              icon="aw-icon aw-icon-check"
              size="small"
              :loading="analysisBusy"
              @click="markReviewedAndNext"
            />
            <Button
              v-else
              label="Next to review"
              icon="aw-icon aw-icon-arrow-right"
              iconPos="right"
              size="small"
              severity="secondary"
              outlined
              :disabled="!nextToReview"
              @click="nextToReview && selectDocument(nextToReview.id, 1)"
            />
          </footer>
        </aside>
        <UiEmptyState v-if="!selected" class="no-selection" icon="aw-icon aw-icon-file" title="Choose a document" description="Select a document from the list to read it beside what was read from it." compact />
      </div>
    </div>
    <UiEmptyState v-else icon="aw-icon aw-icon-file-plus" title="Add engagement documents" description="Upload policies, contracts, evidence, reports, and other files. Extraction happens locally.">
      <Button label="Add documents" icon="aw-icon aw-icon-plus" @click="emit('import-requested')" />
    </UiEmptyState>

    <Drawer v-model:visible="knowledgeOpen" position="right" header="Methodology knowledge" :style="{ width: 'min(45rem, 96vw)' }">
      <div class="pack-toolbar">
        <input ref="packInput" type="file" hidden accept=".md,.markdown,.txt" @change="uploadPack" />
        <Button label="Add Markdown pack" icon="aw-icon aw-icon-plus" size="small" @click="packInput?.click()" />
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
          <Button label="Save vision profile" icon="aw-icon aw-icon-save" size="small" :loading="visionSettingsBusy" :disabled="!visionProvider || !visionModel.trim()" @click="saveVisionSettings" />
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
.grow { flex: 1; }

.head-copy { display: flex; align-items: baseline; gap: .75rem; flex-wrap: wrap; min-width: 0; }
.head-count { margin: 0; }

/* A running background job, not a problem to be solved. */
.indexing-chip { display: inline-flex; align-items: center; gap: .4rem; min-height: var(--aw-control-height-sm); padding: .2rem .6rem; border: 1px solid var(--aw-info-line); border-radius: var(--aw-radius-pill); background: var(--aw-info-soft); color: var(--aw-info); font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap; }

/* --- three panes ---------------------------------------------------------- */
/* The layout is its own container, so the panes give way to the width they
   actually have — the assistant beside the page takes a third of it. */
.document-layout { container: documents / inline-size; flex: 1 1 auto; min-height: 0; display: flex; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); overflow: hidden; }
.document-grid {
  flex: 1; min-width: 0; min-height: 0; display: grid;
  grid-template-columns: minmax(15rem, 18.5rem) minmax(0, 1fr) minmax(19rem, 23.75rem);
  grid-template-rows: minmax(0, 1fr);
  grid-template-areas: "list viewer reading";
}
.document-rail { grid-area: list; }
.viewer-pane { grid-area: viewer; }
.reading-pane { grid-area: reading; }
.no-selection { grid-column: 2 / -1; margin: 1.25rem; }
.pane-switch { display: none; }

/* Too narrow for three columns: the original and the reading share one, and
   a switch above it says which is showing. */
@container documents (max-width: 60rem) {
  .document-grid {
    grid-template-columns: minmax(14rem, 17rem) minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
    grid-template-areas: "list switch" "list pane";
  }
  .pane-switch { grid-area: switch; display: flex; gap: 2px; margin: .5rem .75rem 0; padding: 3px; border-radius: var(--aw-radius-control); background: var(--aw-raised); align-self: start; justify-self: start; }
  .pane-switch button { display: inline-flex; align-items: center; gap: .375rem; height: 1.75rem; padding: 0 .75rem; border: 0; border-radius: 6px; background: transparent; color: var(--aw-ink-soft); font: inherit; font-size: var(--aw-text-sm); cursor: pointer; }
  .pane-switch button.on { background: var(--aw-panel); color: var(--aw-ink-strong); font-weight: 600; box-shadow: var(--aw-shadow-sm); }
  .switch-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--aw-warn); }
  .viewer-pane, .reading-pane { grid-area: pane; border-left: 0; }
  .document-grid[data-pane='reading'] .viewer-pane,
  .document-grid[data-pane='original'] .reading-pane { display: none; }
  .no-selection { grid-column: 2; grid-row: 1 / -1; }
}
@container documents (max-width: 36rem) {
  .document-grid { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto auto minmax(0, 1fr); grid-template-areas: "list" "switch" "pane"; }
  .document-rail { max-height: 16rem; border-right: 0; border-bottom: 1px solid var(--aw-border); }
}

/* --- the list ------------------------------------------------------------- */
.document-rail { min-height: 0; padding: .75rem; border-right: 1px solid var(--aw-border); background: var(--aw-canvas); overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.rail-tools { position: sticky; top: -.75rem; z-index: 1; display: flex; align-items: center; gap: .5rem; margin: -.75rem -.75rem .5rem; padding: .75rem; border-bottom: 1px solid var(--aw-border); background: var(--aw-canvas); }
.rail-tools :deep(.p-iconfield) { flex: 1; min-width: 0; }
.rail-tools :deep(.p-inputtext) { width: 100%; }
.group-by { display: inline-flex; align-items: center; gap: .25rem; flex: none; padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; text-transform: capitalize; cursor: pointer; }
.rail-empty { padding: 2rem .5rem; text-align: center; color: var(--aw-muted); }
.doc-group { display: grid; gap: .125rem; }
.group-head { display: flex; align-items: center; gap: .4rem; width: 100%; margin: .5rem 0 .125rem; padding: .2rem .25rem; border: 0; border-radius: var(--aw-radius-control); background: transparent; color: var(--aw-muted); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; text-align: left; cursor: pointer; }
.group-head:hover { color: var(--aw-teal); }
.group-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.group-count { margin-left: auto; font-weight: 400; }
.doc-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: .625rem; width: 100%; padding: .375rem .5rem; border: 1px solid transparent; border-radius: var(--aw-radius-control); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.doc-row:hover { border-color: var(--aw-border); background: var(--aw-panel); }
.doc-row.active { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }
.dot[data-tone='info'] { background: var(--aw-info); }
.doc-identity { display: grid; min-width: 0; gap: 1px; }
.doc-name { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.doc-ref { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); letter-spacing: -0.01em; }
.doc-row.active .doc-name { color: var(--aw-teal-strong); font-weight: 600; }
.doc-meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.doc-meta [data-tone='warn'] { color: var(--aw-warn-ink); }
.doc-meta [data-tone='bad'] { color: var(--aw-danger); }
.doc-meta [data-tone='agent'] { color: var(--aw-accent); }
.reviewed-mark { color: var(--aw-ok); font-size: var(--aw-text-sm); }
.rail-deep-search { display: flex; align-items: center; gap: .45rem; width: 100%; margin-top: .7rem; padding: .5rem .6rem; border: 1px dashed var(--aw-border); border-radius: var(--aw-radius-control); background: transparent; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); text-align: left; cursor: pointer; }
.rail-deep-search:hover { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.rail-deep-search span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-results { display: flex; flex-direction: column; gap: .3rem; margin-top: .6rem; }
.rail-results-head { display: flex; align-items: baseline; justify-content: space-between; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.rail-results-head button { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); cursor: pointer; }
.rail-result { display: flex; flex-direction: column; gap: 2px; width: 100%; padding: .45rem .55rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); font: inherit; text-align: left; cursor: pointer; }
.rail-result:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.rail-result .excerpt { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); line-height: 1.4; }

/* --- the original --------------------------------------------------------- */
.viewer-pane { min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; background: var(--aw-raised); }
/* One row. A control keeps its label whole and the filename gives way. */
.viewer-head { display: flex; align-items: center; gap: .375rem; min-height: 3rem; padding: .375rem .75rem; border-bottom: 1px solid var(--aw-border); background: var(--aw-panel); }
.viewer-head > :deep(.p-button), .viewer-head > .icon-link, .viewer-head > .source-toggle, .viewer-head > .page-nav { flex: none; }
.viewer-head h2 { flex: 0 1 auto; min-width: 3rem; margin: 0 0 0 .25rem; overflow: hidden; color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600; letter-spacing: 0; text-overflow: ellipsis; white-space: nowrap; }
.held-select { flex: none; }
.viewer-head :deep(.held-select.p-select) { min-height: 1.75rem; border-radius: var(--aw-radius-pill); }
.viewer-head :deep(.held-select .p-select-label) { padding: .125rem .25rem .125rem .625rem; font-size: var(--aw-text-xs); font-weight: 600; }
.viewer-head :deep(.held-select .p-select-dropdown) { width: 1.75rem; }
.viewer-head :deep(.find-on) { color: var(--aw-teal); }
.icon-link { display: grid; place-items: center; width: var(--aw-control-height); height: var(--aw-control-height); border-radius: var(--aw-radius-control); color: var(--aw-ink-soft); }
.icon-link:hover { background: var(--aw-raised); color: var(--aw-ink-strong); }
.page-nav { display: flex; align-items: center; gap: .125rem; color: var(--aw-muted); font-size: var(--aw-text-sm); white-space: nowrap; }
.source-toggle { display: flex; padding: 2px; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.source-toggle button { height: 1.5rem; padding: 0 .625rem; border: 0; border-radius: 6px; background: transparent; color: var(--aw-ink-soft); font: inherit; font-size: var(--aw-text-xs); cursor: pointer; white-space: nowrap; }
.source-toggle button.active { background: var(--aw-panel); color: var(--aw-ink-strong); font-weight: 600; box-shadow: var(--aw-shadow-sm); }
.detail-content { flex: 1 1 auto; min-height: 0; padding: 1rem; overflow-y: auto; overscroll-behavior: contain; }
.preview-view { display: flex; flex-direction: column; }
.preview-view > * { flex: none; }
.source-search-bar { display: flex; gap: .4rem; margin-bottom: .75rem; }
.source-search-bar .p-inputtext { flex: 1; min-width: 12rem; max-width: 24rem; }
.inline-search-results { display: grid; gap: .5rem; margin-bottom: .75rem; }
.inline-search-results button { display: grid; gap: .35rem; width: 100%; padding: .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: inherit; font: inherit; text-align: left; cursor: pointer; }
.inline-search-results button:hover { border-color: var(--aw-teal); }
.inline-search-results span { color: var(--aw-muted); line-height: 1.45; }
.scan-notice { display: flex; gap: .75rem; padding: .9rem; margin-bottom: .75rem; border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control); background: var(--aw-warn-soft); }
.scan-notice p { margin: .25rem 0 0; }
.document-image { display: block; max-width: 100%; max-height: 34rem; margin: auto; }
/* `flex: 1`, not a height guessed from the chrome above it. */
.document-frame { width: 100%; flex: 1 1 auto; min-height: 24rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.docx-frame { flex: 1 1 auto; min-height: 24rem; overflow: auto; border-radius: var(--aw-radius-surface); }
.docx-frame.loading { opacity: .5; }
.docx-frame :deep(.docx-wrapper) { background: transparent; padding: 0; }
.docx-frame :deep(.docx-wrapper > section.docx) { margin: 0 auto 1rem; box-shadow: var(--aw-shadow-md); }
.fields-view { max-width: 56rem; width: 100%; margin: 0 auto; padding: 1rem 1.25rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); box-shadow: var(--aw-shadow-sm); }
.fields-door { display: flex; align-items: center; gap: .5rem; width: 100%; padding: .625rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); color: var(--aw-ink-soft); font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer; }
.fields-door:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.fields-door > span { flex: 1; min-width: 0; }
.fields-door b { color: var(--aw-ink-strong); font-weight: 600; }
.fields-door .aw-icon { color: var(--aw-teal); }
.page-text { min-height: 25rem; margin: 0; padding: 1.35rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); font-family: var(--aw-font-sans); white-space: pre-wrap; line-height: 1.65; box-shadow: var(--aw-shadow-sm); }

/* --- the reading ---------------------------------------------------------- */
.reading-pane { min-width: 0; min-height: 0; display: flex; flex-direction: column; border-left: 1px solid var(--aw-border); background: var(--aw-panel); }
.reading-tabs { display: flex; align-items: flex-end; gap: 1rem; min-height: 3rem; padding: 0 .5rem 0 1rem; border-bottom: 1px solid var(--aw-border); }
.reading-tabs > button { display: inline-flex; align-items: center; gap: .3rem; height: 2.5rem; padding: 0 .125rem; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--aw-ink-soft); font: inherit; font-size: var(--aw-text-sm); font-weight: 500; cursor: pointer; }
.reading-tabs > button:hover { color: var(--aw-ink-strong); }
.reading-tabs > button.on { border-bottom-color: var(--aw-teal); color: var(--aw-teal-strong); font-weight: 600; }
.reading-tabs > :deep(*:last-child) { align-self: center; }
.tab-badge { padding: 0 .3rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.reading-body { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: .875rem; padding: 1rem; overflow-y: auto; overscroll-behavior: contain; font-size: var(--aw-text-sm); line-height: 1.55; }
.reading-source { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; margin: 0; }
.reading-source .by { display: inline-flex; align-items: center; gap: .3rem; padding: .125rem .5rem; border-radius: var(--aw-radius-pill); background: var(--aw-accent-soft); color: var(--aw-accent); font-size: var(--aw-text-xs); font-weight: 600; }
.reading-source .by[data-edited] { background: var(--aw-raised); color: var(--aw-ink-soft); }
.reading-text { color: var(--aw-ink); }
.reading-editor { min-height: 14rem; }
.notes-hint { margin: 0; }
.notes-actions { display: flex; align-items: center; gap: .5rem; }
.reading-foot { display: flex; align-items: center; flex-wrap: wrap; gap: .375rem .5rem; padding: .5rem .75rem .625rem; border-top: 1px solid var(--aw-border); background: var(--aw-panel); }
.reading-foot > :deep(.p-button) { flex: none; }
.queue-position { flex-basis: 100%; white-space: nowrap; }

/* The two states that need a sentence, in the fieldwork strip form. */
.strip { display: flex; align-items: center; gap: .5rem; margin: 0; padding: .5rem 1rem; border-bottom: 1px solid var(--aw-border); font-size: var(--aw-text-sm); line-height: 1.4; }
.strip.warn { border-bottom-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.strip.info { border-bottom-color: var(--aw-info-line); background: var(--aw-info-soft); color: var(--aw-info); }
.strip span { flex: 1; min-width: 0; }
.strip button { flex: none; padding: 0; border: 0; background: none; color: inherit; font: inherit; font-weight: 700; text-decoration: underline; text-underline-offset: 2px; cursor: pointer; }
.strip.inline { margin: .4rem 0 0; padding: .4rem .625rem; border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control); }

.vocabulary-card { display: flex; flex-direction: column; gap: .2rem; padding: .625rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.vocabulary-card.thin { border-color: var(--aw-warn-line); }
.vocabulary-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: .35rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.vocabulary-line b { color: var(--aw-ink-strong); font-size: var(--aw-text-sm); }
.fields-link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.vocabulary-fields { width: 100%; border-collapse: collapse; font-size: var(--aw-text-xs); }
.vocabulary-fields td { padding: .18rem .35rem; border-top: 1px solid var(--aw-border); }
.vf-name { font-family: var(--aw-font-mono); }
.vf-role { color: var(--aw-muted); }
.vf-fill { text-align: right; }
.vf-fill.partial { color: var(--aw-warn); }
.vf-unread { color: var(--aw-muted); text-align: right; }

.coverage-warning { display: flex; align-items: flex-start; gap: .6rem; padding: .75rem; border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.coverage-warning p { margin: .25rem 0 0; }
.coverage-warning ul { margin: .35rem 0 0; padding-left: 1.1rem; }
.analysis-section { display: flex; flex-direction: column; gap: .5rem; }
.analysis-sources { display: grid; gap: .375rem; }
.analysis-sources h4, .analysis-section h4 { margin: 0; }
.analysis-sources button { display: grid; gap: .25rem; width: 100%; padding: .5rem .625rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: inherit; font: inherit; font-size: var(--aw-text-xs); text-align: left; cursor: pointer; }
.analysis-sources button:hover { border-color: var(--aw-teal); }
.analysis-sources button strong { display: flex; align-items: center; gap: .375rem; color: var(--aw-ink-strong); font-weight: 600; }
.analysis-sources button > span { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; line-clamp: 3; overflow: hidden; color: var(--aw-ink-soft); line-height: 1.45; }
.visual-tag { padding: 0 .375rem; border-radius: var(--aw-radius-pill); background: var(--aw-accent-soft); color: var(--aw-accent); font-size: var(--aw-text-2xs); font-weight: 600; }
.candidate-compare { display: grid; gap: .625rem; padding: .75rem; border: 1px solid var(--aw-info-line); border-radius: var(--aw-radius-control); background: var(--aw-info-soft); }
.candidate-compare h4 { margin: 0; font-size: var(--aw-text-sm); }
.candidate-actions { display: flex; flex-wrap: wrap; gap: .375rem; }

.technical-details, .timeline details { padding: .65rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); color: var(--aw-muted); font-size: var(--aw-text-xs); }
.technical-details summary, .timeline summary { cursor: pointer; font-weight: 600; }
.technical-details dl { display: grid; gap: .45rem; margin: .7rem 0 0; }
.technical-details dl div { display: grid; grid-template-columns: 7rem minmax(0, 1fr); gap: .6rem; }
.technical-details dt { font-weight: 600; }
.technical-details dd { display: flex; align-items: center; gap: .3rem; margin: 0; overflow-wrap: anywhere; }
.technical-details dd code { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.timeline { display: grid; gap: .75rem; }
.timeline article { display: grid; grid-template-columns: auto 1fr; gap: .75rem; padding: .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }
.timeline p { margin: .25rem 0; color: var(--aw-muted); }
.timeline code { overflow-wrap: anywhere; font-size: var(--aw-text-xs); }

/* --- drawers -------------------------------------------------------------- */
.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }
.pack-toolbar .p-inputtext { flex: 1; }
.pack-list { display: flex; flex-direction: column; gap: .5rem; }
.pack-list article, .search-results article { padding: .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); }
.pack-scope { float: right; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.search-results { margin-top: 1.2rem; display: grid; gap: .55rem; }
.search-results p { margin: .4rem 0 0; color: var(--aw-muted); }
.drawer-foot { display: flex; justify-content: flex-end; gap: .5rem; padding-top: .875rem; border-top: 1px solid var(--aw-border); }
.vision-settings { display: grid; gap: 1rem; }
.vision-settings > p { margin: 0; color: var(--aw-muted); line-height: 1.5; }
.vision-settings label { display: grid; gap: .35rem; font-weight: 600; }
.vision-settings label :deep(.p-select), .vision-settings label .p-inputtext { width: 100%; }
.vision-settings .settings-warning { padding: .65rem; border-radius: var(--aw-radius-control); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
</style>
