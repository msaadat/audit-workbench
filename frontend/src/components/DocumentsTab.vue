<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { renderAsync } from 'docx-preview'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import { api } from '../api'
import { documentStatus } from '../composables/documentStatus'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { AIActivityEvent, AgentRun, AssistantProvider, AssistantStatus, AuditDocument, DocumentAnalysisCitation, DocumentAnalysisDetail, DocumentCategory, DocumentIndexingStatus, DocumentPage, DocumentSearchResult, KnowledgePack, WorkspaceSummary } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import MarkdownView from './MarkdownView.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'import-requested': [] }>()
const toast = useToast()
const confirm = useConfirm()
const route = useRoute()
const router = useRouter()
const assistantChat = useAssistantChat(props.workspace.id)
const agent = useAgentRun(props.workspace.id)

const documents = ref<AuditDocument[]>([])
const selectedId = ref('')
const previewPages = ref<DocumentPage[]>([])
const currentPage = ref(1)
const view = ref<'preview' | 'analysis' | 'activity'>('preview')
const detailViews = ['preview', 'analysis', 'activity'] as const
const search = ref('')
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
const fullVisualCoverage = ref(false)
const visionSettingsOpen = ref(false)
const settingsStatus = ref<AssistantStatus | null>(null)
const visionProvider = ref('')
const visionModel = ref('')
const visionSettingsBusy = ref(false)
const contentSearchOpen = ref(false)
const contentSearch = ref('')
const sourceSearch = ref('')
const searchResults = ref<DocumentSearchResult[]>([])
const sourceResults = ref<DocumentSearchResult[]>([])
const searchBusy = ref(false)
const indexingStatus = ref<DocumentIndexingStatus | null>(null)
let indexingTimer: number | undefined
let indexingPollRunning = false
let unsubscribeWorkspaceChanged: (() => void) | undefined

const categories = ['background', 'policy', 'regulation', 'contract', 'minutes', 'voucher', 'evidence', 'prior_report', 'correspondence', 'other']
const documentCategoryOptions = categories.map(value => ({ value, label: value.replace('_', ' ') }))
const groupOptions = [
  { value: 'type', label: 'Group by type' },
  { value: 'folder', label: 'Group by folder' },
  { value: 'status', label: 'Group by status' },
]
const visualPageLimit = 20
const visionAvailable = computed(() => agent.state.status?.vision_configured === true)
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
const filtered = computed(() => documents.value.filter(doc => {
  const term = search.value.toLowerCase()
  return !term || `${doc.title} ${doc.source}`.toLowerCase().includes(term)
}))

function groupValue(doc: AuditDocument): string {
  if (groupBy.value === 'folder') {
    const path = doc.relative_path || ''
    const cut = path.lastIndexOf('/')
    return cut > 0 ? path.slice(0, cut) : 'Direct uploads'
  }
  return groupBy.value === 'status' ? doc.text_state : doc.category
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
    label: groupBy.value === 'folder' ? value : value.replace(/_/g, ' '),
    items,
  }))
  entries.sort((a, b) => groupBy.value === 'type'
    ? categories.indexOf(a.value) - categories.indexOf(b.value)
    : a.value.localeCompare(b.value))
  return entries
})
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
const secondaryActions = computed(() => [
  { label: 'Search contents', icon: 'pi pi-search', command: () => { contentSearchOpen.value = true } },
  { label: 'Analyze eligible documents', icon: 'pi pi-sparkles', command: () => void batchAnalyze() },
  { label: 'Reindex documents', icon: 'pi pi-sync', command: () => void reindexAll() },
  { label: 'Methodology knowledge', icon: 'pi pi-book', command: () => void openKnowledge() },
])

const selectedStatus = computed(() => selected.value
  ? documentStatus(selected.value, { visionAvailable: visionAvailable.value })
  : null)
const selectedStatusSeverity = computed(() => {
  const status = selectedStatus.value
  if (!status) return 'secondary'
  if (status.level === 'attention') return status.failed ? 'danger' : 'warn'
  return status.level === 'processing' ? 'info' : 'secondary'
})
const documentActions = computed(() => [
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
  await router.replace({ query: workspaceQuery('documents', { doc: id, page: currentPage.value }) })
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
    if (['completed', 'completed_with_open_items', 'completed_with_issues', 'failed', 'cancelled', 'paused', 'interrupted'].includes(run.status)) return run
    await new Promise(resolve => window.setTimeout(resolve, 500))
  }
  throw new Error('Analysis is still running. Its progress remains available in the assistant.')
}

async function startAnalysis(action: 'analyze' | 'refresh') {
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
    analysis.value = await api.patch<DocumentAnalysisDetail>(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/analysis/review`, {
      review_revision: analysis.value.review_revision,
      summary_markdown: summaryDraft.value,
      audit_notes_markdown: notesDraft.value,
      review_state: reviewed ? 'reviewed' : 'needs_review',
    })
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
  const query = (documentIds ? sourceSearch.value : contentSearch.value).trim()
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
  contentSearchOpen.value = false
  await selectDocument(result.document_id, result.page)
  view.value = 'preview'; sourceView.value = 'text'
}

async function openCitation(citation: DocumentAnalysisCitation) {
  if (!selected.value) return
  currentPage.value = citation.page
  view.value = 'preview'
  sourceView.value = citation.evidence_kind === 'visual' ? 'original' : 'text'
  await router.replace({ query: workspaceQuery('documents', { doc: selected.value.id, page: citation.page }) })
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

async function batchAnalyze() {
  const eligible = documents.value.filter(document =>
    ['extracted', 'partial', 'image_only'].includes(document.text_state)
    && document.analysis_validity_state !== 'current')
  if (!eligible.length) {
    toast.add({ severity: 'info', summary: 'All eligible documents already have current analysis', life: 2600 }); return
  }
  analysisBusy.value = true
  try {
    const run = await api.post<AgentRun>(`/api/workspaces/${props.workspace.id}/documents/analysis-runs`, {
      document_ids: eligible.map(document => document.id),
      action: 'analyze',
      full_visual_coverage: false,
    })
    await waitForAnalysis(run.id); await loadDocuments(); if (selectedId.value) await loadAnalysis()
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
  if (!agent.state.drawerOpen) agent.toggleDrawer()
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

function fileIcon(doc: AuditDocument) {
  if (/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(doc.source)) return 'pi pi-image'
  if (/\.pdf$/i.test(doc.source)) return 'pi pi-file-pdf'
  return 'pi pi-file'
}

function normalizedName(value: string) {
  return value.replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
}

function showSource(doc: AuditDocument) {
  return normalizedName(doc.source) !== normalizedName(doc.title)
}

function openContentSearchFromRail() {
  contentSearch.value = search.value.trim()
  searchResults.value = []
  contentSearchOpen.value = true
  void runContentSearch()
}

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
watch(currentPage, page => { if (selectedId.value) void router.replace({ query: workspaceQuery('documents', { doc: selectedId.value, page }) }) })
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
    <UiPageHeader title="Documents" description="Engagement evidence and reference material">
      <Button label="Add documents" icon="pi pi-plus" @click="emit('import-requested')" />
      <UiOverflowMenu :items="secondaryActions" />
    </UiPageHeader>

    <div v-if="indexingActive" class="indexing-notice" role="status" aria-live="polite">
      <i class="pi pi-spin pi-spinner" />
      <div><strong>Indexing documents for search</strong><p>{{ indexingDetail }}</p></div>
    </div>

    <div v-if="documents.length" class="document-layout surface-panel">
      <aside class="document-rail">
        <div class="rail-tools">
          <span class="search-wrap"><i class="pi pi-search" /><InputText v-model="search" placeholder="Search documents" class="rail-search" /></span>
          <div class="filters">
            <Select v-model="groupBy" :options="groupOptions" optionLabel="label" optionValue="value" aria-label="Group documents by" />
          </div>
        </div>
        <div v-if="!filtered.length" class="rail-empty">No documents match this search.</div>
        <div v-for="group in groups" :key="group.key" class="doc-group">
          <button class="group-head" :aria-expanded="!collapsedGroups.has(group.key)" @click="toggleGroup(group.key)">
            <i :class="collapsedGroups.has(group.key) ? 'pi pi-angle-right' : 'pi pi-angle-down'" />
            <span class="group-name">{{ group.label }}</span>
            <span class="group-count">{{ group.items.length }}</span>
          </button>
          <template v-if="!collapsedGroups.has(group.key)">
            <button
              v-for="doc in group.items" :key="doc.id" class="doc-row" :class="{ active: doc.id === selectedId }"
              :title="`${doc.source} · ${doc.pages || 0} page${doc.pages === 1 ? '' : 's'}`" @click="selectDocument(doc.id, 1)"
            >
              <span class="doc-icon"><i :class="fileIcon(doc)" /></span>
              <span class="doc-identity"><strong>{{ doc.title }}</strong><small v-if="showSource(doc)">{{ doc.source }}</small></span>
              <template v-for="status in [documentStatus(doc, { visionAvailable })]" :key="`${doc.id}:status`">
                <span
                  v-if="status.level !== 'ready'" class="doc-status" :class="[status.level, { failed: status.failed }]"
                  :title="`${status.label} — ${status.detail}`"
                >
                  <i :class="status.level === 'processing' ? 'pi pi-spin pi-spinner' : 'pi pi-exclamation-circle'" />
                </span>
              </template>
            </button>
          </template>
        </div>
        <button v-if="search.trim()" class="rail-deep-search" @click="openContentSearchFromRail">
          <i class="pi pi-search" /><span>Search inside documents for “{{ search.trim() }}”</span>
        </button>
      </aside>

      <main v-if="selected" class="document-detail">
        <header class="detail-head">
          <div class="detail-identity">
            <h3>{{ selected.title }}</h3>
            <p>{{ selected.source }} · {{ selected.pages || 0 }} page{{ selected.pages === 1 ? '' : 's' }}</p>
          </div>
          <div class="detail-actions">
            <Tag v-if="selectedStatus" :value="selectedStatus.label" :severity="selectedStatusSeverity" v-tooltip.bottom="selectedStatus.detail" />
            <label class="classification-field">
              <span>Type</span>
              <Select
                :modelValue="selected.category"
                :options="documentCategoryOptions"
                optionLabel="label"
                optionValue="value"
                :disabled="classificationBusy"
                aria-label="Document classification"
                @update:modelValue="updateClassification"
              />
            </label>
            <Button label="Add to assistant" icon="pi pi-paperclip" size="small" outlined @click="attachToAssistant" />
            <UiOverflowMenu :items="documentActions" tooltip="Document actions" />
          </div>
        </header>
        <nav class="detail-tabs">
          <button v-for="item in detailViews" :key="item" :class="{ active: view === item }" @click="view = item">{{ item }}</button>
        </nav>

        <div v-if="view === 'preview'" class="detail-content preview-view">
          <div class="page-tools">
            <template v-if="!isImage">
              <Button icon="pi pi-angle-left" text :disabled="currentPage <= 1" @click="currentPage--" /><span>Page {{ currentPage }} of {{ selected.pages || previewPages.length || 1 }}</span><Button icon="pi pi-angle-right" text :disabled="currentPage >= (selected.pages || previewPages.length || 1)" @click="currentPage++" />
            </template>
            <div v-if="hasOriginalView" class="source-toggle" role="group" aria-label="Preview mode">
              <button :class="{ active: sourceView === 'original' }" @click="sourceView = 'original'">Original</button>
              <button :class="{ active: sourceView === 'text' }" @click="sourceView = 'text'">Extracted text</button>
            </div>
            <a :href="fileUrl" target="_blank">Open original</a>
          </div>
          <div class="source-search-bar">
            <InputText v-model="sourceSearch" placeholder="Search inside this document" @keyup.enter="runContentSearch(selected ? [selected.id] : [])" />
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
              <p v-else-if="visionAvailable">This page has insufficient extractable text. Analyze it with the configured vision profile; the original remains the authoritative source.</p>
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
            <div class="analysis-states">
              <Tag :value="analysis?.status.analysis_run_state || 'idle'" severity="secondary" />
              <Tag :value="analysis?.status.analysis_coverage_state || 'none'" :severity="analysis?.status.analysis_coverage_state === 'complete' ? 'success' : analysis?.status.analysis_coverage_state === 'partial' ? 'warn' : 'secondary'" />
              <Tag v-if="analysis?.status.analysis_validity_state" :value="analysis.status.analysis_validity_state" :severity="analysis.status.analysis_validity_state === 'current' ? 'success' : 'warn'" />
              <Tag v-if="analysis?.status.analysis_review_state !== 'not_applicable'" :value="analysis?.status.analysis_review_state?.replace('_', ' ')" severity="secondary" />
            </div>
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
              <Button
                :label="visionAvailable ? 'Vision configured' : 'Configure vision'"
                :icon="visionAvailable ? 'pi pi-eye' : 'pi pi-cog'"
                size="small"
                severity="secondary"
                text
                @click="openVisionSettings"
              />
              <Button v-if="!analysis?.generated" label="Analyze" icon="pi pi-sparkles" size="small" :loading="analysisBusy" @click="startAnalysis('analyze')" />
              <Button v-else label="Refresh" icon="pi pi-refresh" size="small" severity="secondary" :loading="analysisBusy" @click="startAnalysis('refresh')" />
              <Button v-if="analysis?.candidate" label="Compare candidate" icon="pi pi-clone" size="small" severity="secondary" @click="compareCandidate = !compareCandidate" />
            </div>
          </div>

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
          <div v-if="analysis?.status.analysis_validity_state === 'stale'" class="coverage-warning"><i class="pi pi-history" /><span>This analysis belongs to an earlier source identity and is excluded from agent context.</span></div>

          <UiEmptyState v-if="!analysis?.effective" icon="pi pi-sparkles" title="Analyze this document once" description="Create a reusable summary and freeform audit notes. Source indexing remains local and independent." compact />
          <template v-else>
            <section class="analysis-editor">
              <header><div><h4>Summary</h4><small>Auditor edits are stored separately from the generated basis.</small></div><div><Button v-if="analysis.review.summary_override !== null" label="Revert" text size="small" severity="secondary" @click="revertAnalysisField('summary')" /></div></header>
              <MarkdownEditor v-model="summaryDraft" />
            </section>
            <section class="analysis-editor">
              <header><div><h4>Audit Notes</h4><small>Freeform observations are not evidence that a control operated.</small></div><div><Button v-if="analysis.review.audit_notes_override !== null" label="Revert" text size="small" severity="secondary" @click="revertAnalysisField('notes')" /></div></header>
              <MarkdownEditor v-model="notesDraft" />
            </section>
            <div class="save-analysis"><Button label="Save edits" icon="pi pi-save" severity="secondary" :loading="analysisBusy" @click="saveAnalysis(false)" /><Button label="Save and mark reviewed" icon="pi pi-check" :loading="analysisBusy" @click="saveAnalysis(true)" /></div>

            <section v-if="compareCandidate && analysis.candidate" class="candidate-compare">
              <h4>Refresh candidate</h4>
              <div><article><strong>Current effective summary</strong><MarkdownView :markdown="summaryDraft" /></article><article><strong>Candidate summary</strong><MarkdownView :markdown="analysis.candidate.summary_markdown" /></article></div>
              <div class="candidate-actions"><Button label="Copy candidate summary into edits" severity="secondary" @click="summaryDraft = analysis.candidate.summary_markdown" /><Button label="Copy candidate notes into edits" severity="secondary" @click="notesDraft = analysis.candidate.audit_notes_markdown" /><Button label="Accept candidate as generated basis" icon="pi pi-check" @click="acceptCandidate" /></div>
            </section>

            <section class="analysis-sources">
              <h4>Sources</h4>
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

    <Dialog v-model:visible="knowledgeOpen" modal header="Methodology knowledge packs" :style="{ width: 'min(64rem, 95vw)' }">
      <div class="pack-toolbar"><input ref="packInput" type="file" hidden accept=".md,.markdown,.txt" @change="uploadPack" /><Button label="Add Markdown pack" icon="pi pi-plus" @click="packInput?.click()" /><InputText v-model="packSearch" placeholder="Search local methodology" @keyup.enter="searchPacks" /><Button label="Search" severity="secondary" @click="searchPacks" /></div>
      <div class="pack-grid"><article v-for="pack in packs" :key="`${pack.scope}:${pack.id}`"><strong>{{ pack.name }}</strong><Tag :value="pack.scope" severity="secondary" /><p>Version {{ pack.version }} · updated {{ pack.updated }}</p><details><summary>Technical details</summary><code>{{ pack.id }} · {{ pack.sha1 }}</code></details></article></div>
      <div v-if="packResults.length" class="search-results"><h4>Cited sections</h4><article v-for="(result, index) in packResults" :key="index"><strong>{{ result.citation }}</strong><p>{{ result.excerpt }}</p></article></div>
    </Dialog>
    <Dialog v-model:visible="contentSearchOpen" modal header="Search document contents" :style="{ width: 'min(58rem, 95vw)' }">
      <div class="global-search-bar"><InputText v-model="contentSearch" autofocus placeholder="Clause, amount, policy name, person, or audit concept" @keyup.enter="runContentSearch()" /><Button label="Search" icon="pi pi-search" :loading="searchBusy" @click="runContentSearch()" /></div>
      <div class="global-search-results"><button v-for="result in searchResults" :key="result.citation_id" @click="openSearchResult(result)"><div><strong>{{ result.title }}</strong><span><Tag v-if="result.origin === 'vision_transcript'" value="AI visual transcription" severity="info" /><Tag :value="`Page ${result.page}`" severity="secondary" /></span></div><p>{{ result.excerpt }}</p><small>Relevance {{ result.score.toFixed(2) }}</small></button><p v-if="contentSearch && !searchBusy && !searchResults.length" class="muted">No indexed source excerpts matched.</p></div>
    </Dialog>
    <Dialog v-model:visible="visionSettingsOpen" modal header="Vision model profile" :style="{ width: 'min(34rem, 95vw)' }">
      <div class="vision-settings">
        <p>Document analysis uses this profile only for supported image and scanned-PDF pages. Later workflows reuse the persisted transcription without resending images.</p>
        <label><span>Provider</span><Select v-model="visionProvider" :options="providerOptions" optionLabel="label" optionValue="id" /></label>
        <label><span>Model</span><Select v-if="visionModelOptions.length" v-model="visionModel" :options="visionModelOptions" editable /><InputText v-else v-model="visionModel" placeholder="Vision-capable model name" /></label>
        <p v-if="agent.state.status?.vision_unavailability_reason" class="settings-warning">{{ agent.state.status.vision_unavailability_reason }}</p>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" text @click="visionSettingsOpen = false" />
        <Button label="Save vision profile" icon="pi pi-save" :loading="visionSettingsBusy" :disabled="!visionProvider || !visionModel.trim()" @click="saveVisionSettings" />
      </template>
    </Dialog>
  </section>
</template>

<style scoped>
.documents-tab { display: grid; gap: 1rem; min-height: 100%; }
.detail-head h3 { margin: 0; }
.document-layout { display: grid; grid-template-columns: minmax(17rem, 20rem) minmax(0, 1fr); min-height: 36rem; overflow: hidden; border:1px solid var(--aw-border); border-radius:var(--aw-radius-md); background:#fff; }
.indexing-notice { display:flex; align-items:flex-start; gap:.7rem; margin:0 0 .85rem; padding:.75rem .9rem; border:1px solid var(--p-blue-200); border-radius:var(--aw-radius-sm); background:var(--p-blue-50); color:var(--p-blue-800); }.indexing-notice > i { margin-top:.15rem; }.indexing-notice p { margin:.15rem 0 0; color:var(--p-blue-700); font-size:.78rem; }
.document-rail { padding:.75rem; border-right:1px solid var(--aw-border); background:var(--p-surface-50); overflow-y:auto; }.rail-tools { position:sticky; top:-.75rem; z-index:1; margin:-.75rem -.75rem .75rem; padding:.75rem; border-bottom:1px solid var(--p-surface-200); background:var(--p-surface-50); }.search-wrap { position:relative; display:block; }.search-wrap > i { position:absolute; z-index:1; left:.75rem; top:50%; translate:0 -50%; color:var(--p-surface-400); }.rail-search { width:100%; padding-left:2.2rem; }.filters { display:grid; grid-template-columns:1fr; gap:.45rem; margin-top:.5rem; }.filters :deep(.p-select) { min-width:0; font-size:.76rem; }
.doc-group { display:grid; gap:.15rem; }.group-head { display:flex; align-items:center; gap:.4rem; width:100%; margin:.55rem 0 .05rem; padding:.2rem .25rem; border:0; border-radius:var(--aw-radius-sm); background:transparent; color:var(--aw-muted); text-transform:uppercase; font-size:var(--aw-text-xs); letter-spacing:.06em; font-weight:700; text-align:left; cursor:pointer; }.group-head:hover { color:var(--aw-teal); }.group-head i { font-size:.6rem; }.group-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.group-count { margin-left:auto; font-weight:400; }.doc-row { width:100%; display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:.55rem; padding:.3rem .5rem; border:1px solid transparent; border-radius:var(--aw-radius-sm); background:transparent; color:inherit; text-align:left; cursor:pointer; transition:border-color .15s, background .15s; }.doc-row:hover { border-color:var(--aw-border); background:#fff; }.doc-row.active { border-color:#a7ded8; background:var(--aw-teal-soft); box-shadow:inset 3px 0 0 var(--aw-teal); }.doc-icon { display:grid; width:1.55rem; height:1.55rem; place-items:center; border-radius:6px; color:var(--p-blue-600); background:var(--p-blue-50); font-size:.75rem; }.doc-identity { display:grid; min-width:0; gap:.04rem; }.doc-identity strong,.doc-identity small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.doc-identity strong { font-size:.8rem; }.doc-identity small { color:var(--aw-muted); font-size:.63rem; }.doc-status { display:grid; place-items:center; width:1.1rem; font-size:.72rem; }.doc-status.processing { color:var(--p-blue-500); }.doc-status.attention { color:var(--p-orange-500); }.doc-status.attention.failed { color:var(--p-red-500); }.rail-empty { padding:2rem .5rem; text-align:center; color:var(--aw-muted); }
.rail-deep-search { display:flex; align-items:center; gap:.45rem; width:100%; margin-top:.7rem; padding:.5rem .6rem; border:1px dashed var(--aw-border); border-radius:var(--aw-radius-sm); background:transparent; color:var(--aw-teal); font-size:.72rem; text-align:left; cursor:pointer; }.rail-deep-search:hover { border-color:var(--aw-teal); background:var(--aw-teal-soft); }.rail-deep-search span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.document-detail { min-width:0; display:flex; flex-direction:column; }.detail-head { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:1rem 1.25rem; border-bottom:1px solid var(--aw-border); }.detail-identity { min-width:0; }.detail-identity p { margin:.25rem 0 0; color:var(--aw-muted); font-size:.73rem; }.detail-actions { display:flex; align-items:center; gap:.35rem; flex-wrap:wrap; justify-content:flex-end; }.detail-tabs { display:flex; padding:0 1.25rem; border-bottom:1px solid var(--aw-border); }.detail-tabs button { padding:.75rem .85rem; border:0; border-bottom:2px solid transparent; background:transparent; color:var(--aw-muted); cursor:pointer; text-transform:capitalize; }.detail-tabs button.active { color:var(--aw-teal); border-color:var(--aw-teal); font-weight:700; }.detail-content { padding:1.25rem; overflow-y:auto; }.page-tools { display:flex; align-items:center; gap:.4rem; margin-bottom:.75rem; }.page-tools a { margin-left:auto; color:var(--aw-teal); }.page-text { min-height:25rem; margin:0; padding:1.35rem; border:1px solid var(--aw-border); border-radius:10px; background:#fff; font-family:var(--aw-font-sans); white-space:pre-wrap; line-height:1.65; box-shadow:0 1px 2px rgb(15 23 42 / 4%); }.scan-notice { display:flex; gap:.75rem; padding:.9rem; margin-bottom:.75rem; border:1px solid #f0cf9f; border-radius:var(--aw-radius-sm); background:var(--aw-warn-soft); }.scan-notice p { margin:.25rem 0 0; }.document-image { display:block; max-width:100%; max-height:34rem; margin:auto; }.document-frame { width:100%; height:calc(100vh - 26rem); min-height:32rem; border:1px solid var(--aw-border); border-radius:10px; background:#fff; }.docx-frame { height:calc(100vh - 26rem); min-height:32rem; overflow:auto; border:1px solid var(--aw-border); border-radius:10px; background:var(--p-surface-100); }.docx-frame.loading { opacity:.5; }.docx-frame :deep(.docx-wrapper) { background:var(--p-surface-100); padding:1.25rem; }.docx-frame :deep(.docx-wrapper > section.docx) { margin-bottom:1rem; box-shadow:0 2px 8px rgb(15 23 42 / 12%); }.source-toggle { display:flex; margin-left:.5rem; border:1px solid var(--aw-border); border-radius:999px; overflow:hidden; }.source-toggle button { padding:.28rem .75rem; border:0; background:transparent; color:var(--aw-muted); font-size:.72rem; cursor:pointer; }.source-toggle button.active { background:var(--aw-teal-soft); color:var(--aw-teal); font-weight:600; }
.classification-field { display:flex; align-items:center; gap:.4rem; color:var(--aw-muted); font-size:.72rem; }.classification-field :deep(.p-select) { min-width:8rem; min-height:2rem; font-size:.76rem; text-transform:capitalize; }
.preview-view .source-search-bar,.preview-view .inline-search-results { margin-bottom:.75rem; }.preview-view .source-search-bar .p-inputtext { max-width:24rem; }
.technical-details,.timeline details,.pack-grid details { margin-top:.8rem; padding:.65rem .75rem; border:1px solid var(--p-surface-200); border-radius:8px; background:var(--p-surface-50); color:var(--p-surface-500); font-size:.7rem; }.technical-details summary,.timeline summary,.pack-grid summary { cursor:pointer; font-weight:600; }.technical-details dl { display:grid; gap:.45rem; margin:.7rem 0 0; }.technical-details dl div { display:grid; grid-template-columns:7rem minmax(0,1fr); gap:.6rem; }.technical-details dt { font-weight:600; }.technical-details dd { display:flex; align-items:center; gap:.3rem; margin:0; overflow-wrap:anywhere; }.technical-details dd code { flex:1; min-width:0; overflow-wrap:anywhere; }
.timeline { display: grid; gap: .75rem; }.timeline article { display: grid; grid-template-columns: auto 1fr; gap: .75rem; padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.timeline p { margin: .25rem 0; color: var(--aw-muted); }.timeline code { overflow-wrap: anywhere; font-size: .7rem; }.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }.pack-toolbar .p-inputtext { flex: 1; }.pack-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(14rem,1fr)); gap: .65rem; }.pack-grid article,.search-results article { padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.pack-grid .p-tag { float: right; }.pack-grid p,.search-results p { margin: .4rem 0 0; color: var(--aw-muted); }.search-results { margin-top: 1.2rem; display: grid; gap: .55rem; }
.analysis-view { display:grid; gap:1rem; }.analysis-toolbar,.analysis-actions,.analysis-states,.save-analysis,.source-search-bar,.global-search-bar,.candidate-actions { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }.analysis-toolbar { justify-content:space-between; }.coverage-warning { display:flex; align-items:flex-start; gap:.6rem; padding:.75rem; border:1px solid #f0cf9f; border-radius:var(--aw-radius-sm); background:var(--aw-warn-soft); color:#7c4b00; }.coverage-warning p { margin:.25rem 0 0; }.coverage-warning ul { margin:.35rem 0 0; padding-left:1.1rem; }.source-search-bar .p-inputtext,.global-search-bar .p-inputtext { flex:1; min-width:14rem; }.analysis-editor { padding:1rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; }.analysis-editor header { display:flex; justify-content:space-between; gap:1rem; margin-bottom:.8rem; }.analysis-editor h4,.analysis-sources h4,.candidate-compare h4 { margin:0; }.analysis-editor small { color:var(--aw-muted); }.analysis-editor :deep(.markdown-editor) { min-height:18rem; }.inline-search-results,.analysis-sources,.global-search-results { display:grid; gap:.5rem; }.inline-search-results button,.analysis-sources button,.global-search-results button { display:grid; gap:.35rem; width:100%; padding:.75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; color:inherit; text-align:left; cursor:pointer; }.inline-search-results button:hover,.analysis-sources button:hover,.global-search-results button:hover { border-color:var(--aw-teal); }.inline-search-results span,.analysis-sources span,.global-search-results p { color:var(--aw-muted); line-height:1.45; }.analysis-sources button strong { display:flex; align-items:center; gap:.45rem; }.candidate-compare { display:grid; gap:.75rem; padding:1rem; border:1px solid var(--p-blue-200); border-radius:var(--aw-radius-sm); background:var(--p-blue-50); }.candidate-compare > div:not(.candidate-actions) { display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }.candidate-compare article { padding:.75rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:#fff; }.global-search-results { margin-top:1rem; }.global-search-results button > div { display:flex; justify-content:space-between; align-items:center; }.global-search-results button > div > span { display:flex; gap:.35rem; }.global-search-results p { margin:.2rem 0; }.global-search-results small { color:var(--aw-muted); }.vision-settings { display:grid; gap:1rem; }.vision-settings > p { margin:0; color:var(--aw-muted); line-height:1.5; }.vision-settings label { display:grid; gap:.35rem; font-weight:600; }.vision-settings label :deep(.p-select),.vision-settings label .p-inputtext { width:100%; }.vision-settings .settings-warning { padding:.65rem; border-radius:var(--aw-radius-sm); background:var(--aw-warn-soft); color:#7c4b00; }
@media (max-width: 900px) { .document-layout { grid-template-columns: 1fr; }.document-rail { max-height: 20rem; border-right: 0; border-bottom: 1px solid var(--aw-border); }.detail-head { align-items:flex-start; flex-direction:column; }.detail-actions { justify-content:flex-start; } }
</style>
