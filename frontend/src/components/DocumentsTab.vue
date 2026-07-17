<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import type { AIActivityEvent, AuditDocument, DocumentCategory, DocumentPage, IntakeSuggestedAction, KnowledgePack, WorkspaceSummary } from '../types'
import PostImportPlanningOffer from './PostImportPlanningOffer.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'planning-started': [] }>()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const assistantChat = useAssistantChat(props.workspace.id)
const agent = useAgentRun(props.workspace.id)

const documents = ref<AuditDocument[]>([])
const selectedId = ref('')
const previewPages = ref<DocumentPage[]>([])
const currentPage = ref(1)
const view = ref<'preview' | 'versions' | 'activity'>('preview')
const detailViews = ['preview', 'versions', 'activity'] as const
const search = ref('')
const category = ref('all')
const state = ref('all')
const busy = ref(false)
const classificationBusy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const versions = ref<AuditDocument[]>([])
const activity = ref<AIActivityEvent[]>([])
const knowledgeOpen = ref(false)
const packs = ref<KnowledgePack[]>([])
const packSearch = ref('')
const packResults = ref<Array<Record<string, unknown>>>([])
const packInput = ref<HTMLInputElement | null>(null)
const planningAction = ref<IntakeSuggestedAction | null>(null)

const categories = ['all', 'background', 'policy', 'regulation', 'contract', 'minutes', 'voucher', 'evidence', 'prior_report', 'correspondence', 'other']
const states = ['all', 'extracted', 'partial', 'image_only', 'pending', 'failed']
const categoryOptions = categories.map(value => ({ value, label: value === 'all' ? 'All types' : value.replace('_', ' ') }))
const documentCategoryOptions = categoryOptions.filter(option => option.value !== 'all')
const stateOptions = states.map(value => ({ value, label: value === 'all' ? 'All statuses' : value.replace('_', ' ') }))
const selected = computed(() => documents.value.find(doc => doc.id === selectedId.value) || null)
const filtered = computed(() => documents.value.filter(doc => {
  const term = search.value.toLowerCase()
  return (!term || `${doc.title} ${doc.source}`.toLowerCase().includes(term)) && (category.value === 'all' || doc.category === category.value) && (state.value === 'all' || doc.text_state === state.value)
}))
const groups = computed(() => {
  const map = new Map<string, AuditDocument[]>()
  for (const doc of filtered.value) map.set(doc.category, [...(map.get(doc.category) || []), doc])
  return [...map.entries()]
})
const current = computed(() => previewPages.value.find(page => page.page === currentPage.value) || previewPages.value[0])
const secondaryActions = computed(() => [
  { label: 'Methodology knowledge', icon: 'pi pi-book', command: () => void openKnowledge() },
])

function severity(value: string): 'success' | 'danger' | 'warn' | 'secondary' {
  if (value === 'extracted') return 'success'
  if (value === 'failed') return 'danger'
  if (value === 'partial' || value === 'image_only') return 'warn'
  return 'secondary'
}

async function loadDocuments() {
  const result = await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)
  documents.value = result.items
  const requested = String(route.query.doc || '')
  if (requested && result.items.some(doc => doc.id === requested)) selectedId.value = requested
  else if (!selectedId.value || !result.items.some(doc => doc.id === selectedId.value)) selectedId.value = result.items[0]?.id || ''
}

async function selectDocument(id: string, page?: number) {
  selectedId.value = id
  currentPage.value = page || Number(route.query.page || 1)
  await router.replace({ query: { ...route.query, tab: 'documents', doc: id, page: String(currentPage.value) } })
  await loadDetail()
}

async function loadDetail() {
  if (!selectedId.value) return
  try {
    const data = await api.get<{ pages: DocumentPage[] }>(`/api/workspaces/${props.workspace.id}/documents/${selectedId.value}/preview`)
    previewPages.value = data.pages
    if (!data.pages.some(page => page.page === currentPage.value)) currentPage.value = data.pages[0]?.page || 1
    const [versionData, activityData] = await Promise.all([
      api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents/${selectedId.value}/versions`),
      api.get<{ items: AIActivityEvent[] }>(`/api/workspaces/${props.workspace.id}/ai-activity?document_id=${selectedId.value}`),
    ])
    versions.value = versionData.items; activity.value = activityData.items
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Document unavailable', detail: String(error), life: 5000 })
  }
}

async function upload(event: Event) {
  const files = Array.from((event.target as HTMLInputElement).files || [])
  if (!files.length) return
  busy.value = true
  try {
    const result = await api.upload<{ suggested_actions?: IntakeSuggestedAction[] }>(`/api/workspaces/${props.workspace.id}/documents`, files)
    planningAction.value = result.suggested_actions?.find(action => action.agent_kind === 'planning') ?? null
    await loadDocuments(); if (selectedId.value) await loadDetail()
    emit('changed')
    toast.add({ severity: 'success', summary: 'Documents added', detail: `${files.length} file(s) stored and extracted locally.`, life: 3500 })
  } catch (error) { toast.add({ severity: 'error', summary: 'Upload failed', detail: String(error), life: 5000 }) }
  finally { busy.value = false; if (fileInput.value) fileInput.value.value = '' }
}

async function reextract() {
  if (!selected.value) return
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}/re-extract`); await loadDocuments(); await loadDetail() }
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

async function remove() {
  if (!selected.value || !window.confirm(`Delete ${selected.value.title}? Existing evidence references will remain visibly stale.`)) return
  await api.del(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}`)
  await assistantChat.removeDocument(selected.value.id)
  selectedId.value = ''; await loadDocuments(); if (selectedId.value) await loadDetail(); emit('changed')
}

async function attachToAssistant() {
  if (!selected.value) return
  await assistantChat.addDocument(selected.value)
  if (!agent.state.drawerOpen) agent.toggleDrawer()
  toast.add({ severity: 'success', summary: 'Added to assistant', detail: selected.value.title, life: 2500 })
}

function fileIcon(doc: AuditDocument) {
  if (/\.(png|jpe?g|webp|bmp|tiff?)$/i.test(doc.source)) return 'pi pi-image'
  if (/\.pdf$/i.test(doc.source)) return 'pi pi-file-pdf'
  return 'pi pi-file'
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
watch(currentPage, page => { if (selectedId.value) void router.replace({ query: { ...route.query, tab: 'documents', doc: selectedId.value, page: String(page) } }) })
onMounted(async () => { await loadDocuments(); if (selectedId.value) await selectDocument(selectedId.value, Number(route.query.page || 1)) })
</script>

<template>
  <section class="documents-tab">
    <UiPageHeader title="Documents" description="Engagement evidence and reference material">
      <input ref="fileInput" type="file" multiple hidden accept=".pdf,.txt,.md,.markdown,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" @change="upload" />
      <Button label="Add documents" icon="pi pi-plus" :loading="busy" @click="fileInput?.click()" />
      <UiOverflowMenu :items="secondaryActions" />
    </UiPageHeader>

    <PostImportPlanningOffer
      v-if="planningAction"
      :workspaceId="workspace.id"
      :action="planningAction"
      @planning-started="emit('planning-started')"
    />

    <div v-if="documents.length" class="document-layout surface-panel">
      <aside class="document-rail">
        <div class="rail-tools">
          <span class="search-wrap"><i class="pi pi-search" /><InputText v-model="search" placeholder="Search documents" class="rail-search" /></span>
          <div class="filters">
            <Select v-model="category" :options="categoryOptions" optionLabel="label" optionValue="value" aria-label="Document type" />
            <Select v-model="state" :options="stateOptions" optionLabel="label" optionValue="value" aria-label="Extraction status" />
          </div>
        </div>
        <div v-if="!filtered.length" class="rail-empty">No documents match these filters.</div>
        <div v-for="[group, items] in groups" :key="group" class="doc-group">
          <h4>{{ String(group).replace('_', ' ') }} <span>{{ items.length }}</span></h4>
          <button v-for="doc in items" :key="doc.id" class="doc-row" :class="{ active: doc.id === selectedId }" @click="selectDocument(doc.id, 1)">
            <span class="doc-icon"><i :class="fileIcon(doc)" /></span>
            <span class="doc-identity"><strong>{{ doc.title }}</strong><small>{{ doc.source }}</small><small>{{ doc.pages || 0 }} page{{ doc.pages === 1 ? '' : 's' }} · version {{ doc.version }}</small></span>
            <span class="state-pill" :class="`state-${doc.text_state}`">{{ doc.text_state.replace('_', ' ') }}</span>
          </button>
        </div>
      </aside>

      <main v-if="selected" class="document-detail">
        <header class="detail-head">
          <div class="detail-identity">
            <p class="eyebrow">{{ selected.category.replace('_', ' ') }}</p>
            <h3>{{ selected.title }}</h3>
            <p>{{ selected.source }} · {{ selected.pages || 0 }} page{{ selected.pages === 1 ? '' : 's' }} · version {{ selected.version }}</p>
          </div>
          <div class="detail-actions">
            <label class="classification-field">
              <span>Classification</span>
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
            <Tag :value="selected.text_state.replace('_', ' ')" :severity="severity(selected.text_state)" />
            <Button label="Add to assistant" icon="pi pi-paperclip" size="small" @click="attachToAssistant" />
            <Button icon="pi pi-refresh" text rounded aria-label="Re-extract" v-tooltip.top="'Re-extract'" @click="reextract" />
            <Button icon="pi pi-trash" text rounded severity="danger" aria-label="Delete" v-tooltip.top="'Delete document'" @click="remove" />
          </div>
        </header>
        <nav class="detail-tabs">
          <button v-for="item in detailViews" :key="item" :class="{ active: view === item }" @click="view = item">{{ item }}</button>
        </nav>

        <div v-if="view === 'preview'" class="detail-content preview-view">
          <div class="page-tools"><Button icon="pi pi-angle-left" text :disabled="currentPage <= 1" @click="currentPage--" /><span>Page {{ currentPage }} of {{ selected.pages || previewPages.length || 1 }}</span><Button icon="pi pi-angle-right" text :disabled="currentPage >= (selected.pages || previewPages.length || 1)" @click="currentPage++" /><a :href="`/api/workspaces/${workspace.id}/documents/${selected.id}/file`" target="_blank">Open original</a></div>
          <div v-if="current?.image_only" class="scan-notice"><i class="pi pi-image" /><div><strong>Image-only page</strong><p>This page has insufficient extractable text. Use the original image/PDF or a configured vision workflow; tiled scans may require uploading the page as an image.</p></div></div>
          <img v-if="selected.file.match(/\.(png|jpe?g|webp|bmp)$/i)" class="document-image" :src="`/api/workspaces/${workspace.id}/documents/${selected.id}/file`" :alt="selected.title" />
          <pre v-else class="page-text">{{ current?.text || 'No extractable text on this page.' }}</pre>
          <details class="technical-details">
            <summary>Technical details</summary>
            <dl><div><dt>Document ID</dt><dd><code>{{ selected.id }}</code><Button icon="pi pi-copy" text rounded size="small" aria-label="Copy document ID" @click="copyText(selected.id, 'Document ID')" /></dd></div><div><dt>Content hash</dt><dd><code>{{ selected.sha1 }}</code><Button icon="pi pi-copy" text rounded size="small" aria-label="Copy content hash" @click="copyText(selected.sha1, 'Content hash')" /></dd></div><div><dt>Stored file</dt><dd><code>{{ selected.file }}</code></dd></div><div v-if="selected.relative_path"><dt>Imported path</dt><dd>{{ selected.relative_path }}</dd></div><div><dt>Added</dt><dd>{{ selected.created }}</dd></div></dl>
          </details>
        </div>

        <div v-else-if="view === 'versions'" class="detail-content timeline">
          <article v-for="item in versions" :key="item.id"><i class="pi pi-history" /><div><strong>Version {{ item.version }} <Tag v-if="item.id === selected.id" value="current" severity="info" /></strong><p>{{ item.created }} · {{ item.source }}</p><details><summary>Technical details</summary><code>{{ item.id }} · {{ item.sha1 }}</code></details><Button label="Open version" text size="small" @click="selectDocument(item.id, 1)" /></div></article>
        </div>

        <div v-else class="detail-content timeline">
          <article v-for="item in activity" :key="item.id"><i class="pi pi-sparkles" /><div><strong>{{ item.purpose.replace('_', ' ') }} · {{ item.disposition }}</strong><p>{{ item.at }} · {{ item.provider }} / {{ item.model }}</p><p>Pages {{ item.page_ranges?.join(', ') || '—' }}</p><details><summary>Technical details</summary><code>{{ item.id }} · response {{ item.response_hash || 'not available' }}</code></details></div></article>
          <p v-if="!activity.length" class="muted">No model activity references this document.</p>
        </div>
      </main>
      <UiEmptyState v-else icon="pi pi-file" title="Choose a document" description="Select a document from the inventory to preview it." compact />
    </div>
    <UiEmptyState v-else icon="pi pi-file-plus" title="Add engagement documents" description="Upload policies, contracts, evidence, reports, and other files. Extraction happens locally.">
      <Button label="Add documents" icon="pi pi-plus" :loading="busy" @click="fileInput?.click()" />
    </UiEmptyState>

    <Dialog v-model:visible="knowledgeOpen" modal header="Methodology knowledge packs" :style="{ width: 'min(64rem, 95vw)' }">
      <div class="pack-toolbar"><input ref="packInput" type="file" hidden accept=".md,.markdown,.txt" @change="uploadPack" /><Button label="Add Markdown pack" icon="pi pi-plus" @click="packInput?.click()" /><InputText v-model="packSearch" placeholder="Search local methodology" @keyup.enter="searchPacks" /><Button label="Search" severity="secondary" @click="searchPacks" /></div>
      <div class="pack-grid"><article v-for="pack in packs" :key="`${pack.scope}:${pack.id}`"><strong>{{ pack.name }}</strong><Tag :value="pack.scope" severity="secondary" /><p>Version {{ pack.version }} · updated {{ pack.updated }}</p><details><summary>Technical details</summary><code>{{ pack.id }} · {{ pack.sha1 }}</code></details></article></div>
      <div v-if="packResults.length" class="search-results"><h4>Cited sections</h4><article v-for="(result, index) in packResults" :key="index"><strong>{{ result.citation }}</strong><p>{{ result.excerpt }}</p></article></div>
    </Dialog>
  </section>
</template>

<style scoped>
.documents-tab { display: grid; gap: 1rem; min-height: 100%; }
.detail-head h3 { margin: 0; }
.document-layout { display: grid; grid-template-columns: minmax(17rem, 20rem) minmax(0, 1fr); min-height: 36rem; overflow: hidden; border:1px solid var(--aw-border); border-radius:var(--aw-radius-md); background:#fff; }
.document-rail { padding:.75rem; border-right:1px solid var(--aw-border); background:var(--p-surface-50); overflow-y:auto; }.rail-tools { position:sticky; top:-.75rem; z-index:1; margin:-.75rem -.75rem .75rem; padding:.75rem; border-bottom:1px solid var(--p-surface-200); background:var(--p-surface-50); }.search-wrap { position:relative; display:block; }.search-wrap > i { position:absolute; z-index:1; left:.75rem; top:50%; translate:0 -50%; color:var(--p-surface-400); }.rail-search { width:100%; padding-left:2.2rem; }.filters { display:grid; grid-template-columns:1fr 1fr; gap:.45rem; margin-top:.5rem; }.filters :deep(.p-select) { min-width:0; font-size:.76rem; }
.doc-group { display:grid; gap:.15rem; }.doc-group h4 { display:flex; justify-content:space-between; margin:.7rem .25rem .05rem; color:var(--aw-muted); text-transform:uppercase; font-size:var(--aw-text-xs); letter-spacing:.06em; }.doc-row { width:100%; min-height:var(--aw-row-height); display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:.55rem; padding:.42rem .5rem; border:1px solid transparent; border-radius:var(--aw-radius-sm); background:transparent; color:inherit; text-align:left; cursor:pointer; transition:border-color .15s, background .15s; }.doc-row:hover { border-color:var(--aw-border); background:#fff; }.doc-row.active { border-color:#a7ded8; background:var(--aw-teal-soft); box-shadow:inset 3px 0 0 var(--aw-teal); }.doc-icon { display:grid; width:1.8rem; height:1.8rem; place-items:center; border-radius:6px; color:var(--p-blue-600); background:var(--p-blue-50); }.doc-identity { display:grid; min-width:0; gap:.04rem; }.doc-identity strong,.doc-identity small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.doc-identity strong { font-size:.8rem; }.doc-identity small { color:var(--aw-muted); font-size:.63rem; }.state-pill { width:.55rem; height:.55rem; overflow:hidden; padding:0; border-radius:999px; color:transparent; background:var(--p-surface-300); font-size:0; }.state-extracted { background:var(--p-green-500); }.state-failed { background:var(--p-red-500); }.state-partial,.state-image_only { background:var(--p-orange-500); }.rail-empty { padding:2rem .5rem; text-align:center; color:var(--aw-muted); }
.document-detail { min-width:0; display:flex; flex-direction:column; }.detail-head { display:flex; justify-content:space-between; align-items:center; gap:1rem; padding:1rem 1.25rem; border-bottom:1px solid var(--aw-border); }.detail-identity { min-width:0; }.detail-identity p { margin:.25rem 0 0; color:var(--aw-muted); font-size:.73rem; }.detail-actions { display:flex; align-items:center; gap:.35rem; flex-wrap:wrap; justify-content:flex-end; }.detail-tabs { display:flex; padding:0 1.25rem; border-bottom:1px solid var(--aw-border); }.detail-tabs button { padding:.75rem .85rem; border:0; border-bottom:2px solid transparent; background:transparent; color:var(--aw-muted); cursor:pointer; text-transform:capitalize; }.detail-tabs button.active { color:var(--aw-teal); border-color:var(--aw-teal); font-weight:700; }.detail-content { padding:1.25rem; overflow-y:auto; }.page-tools { display:flex; align-items:center; gap:.4rem; margin-bottom:.75rem; }.page-tools a { margin-left:auto; color:var(--aw-teal); }.page-text { min-height:25rem; margin:0; padding:1.35rem; border:1px solid var(--aw-border); border-radius:10px; background:#fff; font-family:var(--aw-font-sans); white-space:pre-wrap; line-height:1.65; box-shadow:0 1px 2px rgb(15 23 42 / 4%); }.scan-notice { display:flex; gap:.75rem; padding:.9rem; margin-bottom:.75rem; border:1px solid #f0cf9f; border-radius:var(--aw-radius-sm); background:var(--aw-warn-soft); }.scan-notice p { margin:.25rem 0 0; }.document-image { display:block; max-width:100%; max-height:34rem; margin:auto; }
.classification-field { display:grid; gap:.2rem; min-width:10rem; color:var(--aw-muted); font-size:.65rem; font-weight:700; }.classification-field :deep(.p-select) { width:100%; min-height:2rem; font-size:.76rem; font-weight:400; text-transform:capitalize; }
.technical-details,.timeline details,.pack-grid details { margin-top:.8rem; padding:.65rem .75rem; border:1px solid var(--p-surface-200); border-radius:8px; background:var(--p-surface-50); color:var(--p-surface-500); font-size:.7rem; }.technical-details summary,.timeline summary,.pack-grid summary { cursor:pointer; font-weight:600; }.technical-details dl { display:grid; gap:.45rem; margin:.7rem 0 0; }.technical-details dl div { display:grid; grid-template-columns:7rem minmax(0,1fr); gap:.6rem; }.technical-details dt { font-weight:600; }.technical-details dd { display:flex; align-items:center; gap:.3rem; margin:0; overflow-wrap:anywhere; }.technical-details dd code { flex:1; min-width:0; overflow-wrap:anywhere; }
.timeline { display: grid; gap: .75rem; }.timeline article { display: grid; grid-template-columns: auto 1fr; gap: .75rem; padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.timeline p { margin: .25rem 0; color: var(--aw-muted); }.timeline code { overflow-wrap: anywhere; font-size: .7rem; }.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }.pack-toolbar .p-inputtext { flex: 1; }.pack-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(14rem,1fr)); gap: .65rem; }.pack-grid article,.search-results article { padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.pack-grid .p-tag { float: right; }.pack-grid p,.search-results p { margin: .4rem 0 0; color: var(--aw-muted); }.search-results { margin-top: 1.2rem; display: grid; gap: .55rem; }
@media (max-width: 900px) { .document-layout { grid-template-columns: 1fr; }.document-rail { max-height: 20rem; border-right: 0; border-bottom: 1px solid var(--aw-border); }.detail-head { align-items:flex-start; flex-direction:column; }.detail-actions { justify-content:flex-start; } }
</style>
