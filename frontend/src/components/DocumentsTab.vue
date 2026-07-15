<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import Checkbox from 'primevue/checkbox'
import { api } from '../api'
import type { AIActivityEvent, AuditDocument, DisclosureEvent, DocumentPage, EvidenceRef, IntakeSuggestedAction, KnowledgePack, WorkspaceSummary } from '../types'
import MarkdownView from './MarkdownView.vue'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import PostImportPlanningOffer from './PostImportPlanningOffer.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: []; 'planning-started': [] }>()
const toast = useToast()
const route = useRoute()
const router = useRouter()

const documents = ref<AuditDocument[]>([])
const selectedId = ref('')
const previewPages = ref<DocumentPage[]>([])
const currentPage = ref(1)
const view = ref<'preview' | 'ask' | 'versions' | 'activity'>('preview')
const detailViews = ['preview', 'ask', 'versions', 'activity'] as const
const search = ref('')
const category = ref('all')
const state = ref('all')
const busy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const optin = ref(Boolean(props.workspace.settings?.doc_llm_optin))
const piiMasking = ref(Boolean(props.workspace.settings?.doc_pii_masking))
const confirmOptin = ref(false)
const disclosuresOpen = ref(false)
const disclosures = ref<DisclosureEvent[]>([])
const question = ref('')
const askPages = ref('1')
const answer = ref('')
const citations = ref<EvidenceRef[]>([])
const versions = ref<AuditDocument[]>([])
const activity = ref<AIActivityEvent[]>([])
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const knowledgeOpen = ref(false)
const packs = ref<KnowledgePack[]>([])
const packSearch = ref('')
const packResults = ref<Array<Record<string, unknown>>>([])
const packInput = ref<HTMLInputElement | null>(null)
const planningAction = ref<IntakeSuggestedAction | null>(null)

const categories = ['all', 'background', 'policy', 'regulation', 'contract', 'minutes', 'voucher', 'evidence', 'prior_report', 'correspondence', 'other']
const states = ['all', 'extracted', 'partial', 'image_only', 'pending', 'failed']
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
  answer.value = ''; citations.value = []
  await router.replace({ query: { ...route.query, tab: 'documents', doc: id, page: String(currentPage.value) } })
  await loadDetail()
}

async function loadDetail() {
  if (!selectedId.value) return
  try {
    const data = await api.get<{ pages: DocumentPage[] }>(`/api/workspaces/${props.workspace.id}/documents/${selectedId.value}/preview`)
    previewPages.value = data.pages
    if (!data.pages.some(page => page.page === currentPage.value)) currentPage.value = data.pages[0]?.page || 1
    askPages.value = String(currentPage.value)
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

async function remove() {
  if (!selected.value || !window.confirm(`Delete ${selected.value.title}? Existing evidence references will remain visibly stale.`)) return
  await api.del(`/api/workspaces/${props.workspace.id}/documents/${selected.value.id}`)
  selectedId.value = ''; await loadDocuments(); if (selectedId.value) await loadDetail(); emit('changed')
}

async function enableOptin() {
  const settings = await api.patch<{ doc_llm_optin: boolean }>(`/api/workspaces/${props.workspace.id}/settings`, { doc_llm_optin: true })
  optin.value = settings.doc_llm_optin; confirmOptin.value = false; emit('changed')
}

async function disableOptin() {
  await api.patch(`/api/workspaces/${props.workspace.id}/settings`, { doc_llm_optin: false })
  optin.value = false; emit('changed')
}

async function setMasking() {
  await api.patch(`/api/workspaces/${props.workspace.id}/settings`, { doc_pii_masking: piiMasking.value })
  emit('changed')
}

async function openDisclosures() {
  disclosures.value = (await api.get<{ items: DisclosureEvent[] }>(`/api/workspaces/${props.workspace.id}/documents/disclosures`)).items
  disclosuresOpen.value = true
}

function parsedPages(): number[] {
  return [...new Set(askPages.value.split(',').map(value => Number(value.trim())).filter(value => Number.isInteger(value) && value > 0))]
}

async function ask() {
  if (!selected.value || !question.value.trim()) return
  busy.value = true
  try {
    const result = await api.post<{ answer: string; citations: EvidenceRef[] }>(`/api/workspaces/${props.workspace.id}/doc-chat`, {
      document_id: selected.value.id, question: question.value, pages: parsedPages(), mask_pii: piiMasking.value,
    })
    answer.value = result.answer; citations.value = result.citations; await loadDetail()
  } catch (error) { toast.add({ severity: 'error', summary: 'Document question failed', detail: String(error), life: 6000 }) }
  finally { busy.value = false }
}

function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }

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
watch(() => props.workspace.settings?.doc_llm_optin, value => { optin.value = Boolean(value) })
watch(() => props.workspace.settings?.doc_pii_masking, value => { piiMasking.value = Boolean(value) })
onMounted(async () => { await loadDocuments(); if (selectedId.value) await selectDocument(selectedId.value, Number(route.query.page || 1)) })
</script>

<template>
  <section class="documents-tab">
    <div class="toolbar doc-toolbar">
      <div><p class="eyebrow">Evidence library</p><h2>Documents</h2></div>
      <div class="toolbar-actions">
        <input ref="fileInput" type="file" multiple hidden accept=".pdf,.txt,.md,.markdown,.docx,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" @change="upload" />
        <Button label="Add documents" icon="pi pi-plus" :loading="busy" @click="fileInput?.click()" />
        <Button label="Knowledge packs" icon="pi pi-book" severity="secondary" @click="openKnowledge" />
        <Button label="Disclosure log" icon="pi pi-list" severity="secondary" @click="openDisclosures" />
        <Button v-if="!optin" label="Document AI off" icon="pi pi-lock" severity="secondary" @click="confirmOptin = true" />
        <Button v-else label="Document AI on" icon="pi pi-unlock" severity="success" @click="disableOptin" />
      </div>
    </div>

    <div class="privacy-strip" :class="{ enabled: optin }">
      <i :class="optin ? 'pi pi-cloud-upload' : 'pi pi-lock'" />
      <span v-if="optin">Structured rows remain local. Only document pages you confirm may be sent to the configured model, and every disclosure is logged.</span>
      <span v-else>All document content remains local. Document questions are disabled until this engagement explicitly opts in.</span>
      <label v-if="optin"><Checkbox v-model="piiMasking" binary @change="setMasking" /> Mask common email/number patterns before disclosure</label>
    </div>

    <PostImportPlanningOffer
      v-if="planningAction"
      :workspaceId="workspace.id"
      :action="planningAction"
      :documentAiEnabled="optin"
      @settings-changed="optin = true; emit('changed')"
      @planning-started="emit('planning-started')"
    />

    <div class="document-layout surface-panel">
      <aside class="document-rail">
        <InputText v-model="search" placeholder="Search documents" class="rail-search" />
        <div class="filters"><Select v-model="category" :options="categories" /><Select v-model="state" :options="states" /></div>
        <div v-if="!filtered.length" class="rail-empty">No documents match these filters.</div>
        <div v-for="[group, items] in groups" :key="group" class="doc-group">
          <h4>{{ String(group).replace('_', ' ') }} <span>{{ items.length }}</span></h4>
          <button v-for="doc in items" :key="doc.id" class="doc-row" :class="{ active: doc.id === selectedId }" @click="selectDocument(doc.id, 1)">
            <i class="pi pi-file" /><span><strong>{{ doc.title }}</strong><small>{{ doc.source }} · v{{ doc.version }}</small></span>
            <Tag :value="doc.text_state.replace('_', ' ')" :severity="severity(doc.text_state)" />
          </button>
        </div>
      </aside>

      <main v-if="selected" class="document-detail">
        <header class="detail-head">
          <div><p class="eyebrow">{{ selected.category }} · version {{ selected.version }}</p><h3>{{ selected.title }}</h3><code>{{ selected.sha1 }}</code></div>
          <div><Button icon="pi pi-refresh" text rounded aria-label="Re-extract" @click="reextract" /><Button icon="pi pi-trash" text rounded severity="danger" aria-label="Delete" @click="remove" /></div>
        </header>
        <nav class="detail-tabs">
          <button v-for="item in detailViews" :key="item" :class="{ active: view === item }" @click="view = item">{{ item === 'ask' ? 'Ask document' : item }}</button>
        </nav>

        <div v-if="view === 'preview'" class="detail-content preview-view">
          <div class="page-tools"><Button icon="pi pi-angle-left" text :disabled="currentPage <= 1" @click="currentPage--" /><span>Page {{ currentPage }} of {{ selected.pages || previewPages.length || 1 }}</span><Button icon="pi pi-angle-right" text :disabled="currentPage >= (selected.pages || previewPages.length || 1)" @click="currentPage++" /><a :href="`/api/workspaces/${workspace.id}/documents/${selected.id}/file`" target="_blank">Open original</a></div>
          <div v-if="current?.image_only" class="scan-notice"><i class="pi pi-image" /><div><strong>Image-only page</strong><p>This page has insufficient extractable text. Use the original image/PDF or a configured vision workflow; tiled scans may require uploading the page as an image.</p></div></div>
          <img v-if="selected.file.match(/\.(png|jpe?g|webp|bmp)$/i)" class="document-image" :src="`/api/workspaces/${workspace.id}/documents/${selected.id}/file`" :alt="selected.title" />
          <pre v-else class="page-text">{{ current?.text || 'No extractable text on this page.' }}</pre>
        </div>

        <div v-else-if="view === 'ask'" class="detail-content ask-view">
          <div class="field"><label>Question</label><Textarea v-model="question" rows="4" auto-resize placeholder="What evidence does this document provide?" /></div>
          <div class="field"><label>Pages to disclose</label><InputText v-model="askPages" placeholder="1, 2, 4" /></div>
          <div class="disclosure-preview"><strong>Disclosure preview</strong><p>Purpose: document Q&amp;A · pages {{ parsedPages().join(', ') || 'none' }} · source {{ selected.sha1.slice(0, 12) }}…</p><p>{{ piiMasking ? 'Common email and number patterns will be masked in the model copy.' : 'The extracted page text will be sent without masking.' }}</p></div>
          <Button :label="optin ? 'Disclose pages and ask' : 'Enable document AI to ask'" icon="pi pi-send" :disabled="!optin || !question.trim() || !parsedPages().length" :loading="busy" @click="ask" />
          <div v-if="answer" class="answer surface-panel"><MarkdownView :markdown="answer" /><div class="citations"><Button v-for="citation in citations" :key="citation.id" :label="`Page ${citation.page}`" icon="pi pi-link" size="small" severity="secondary" @click="showAnchor(citation)" /></div></div>
        </div>

        <div v-else-if="view === 'versions'" class="detail-content timeline">
          <article v-for="item in versions" :key="item.id"><i class="pi pi-history" /><div><strong>Version {{ item.version }} <Tag v-if="item.id === selected.id" value="selected" severity="info" /></strong><p>{{ item.created }} · {{ item.source }}</p><code>{{ item.sha1 }}</code><Button label="Open version" text size="small" @click="selectDocument(item.id, 1)" /></div></article>
        </div>

        <div v-else class="detail-content timeline">
          <article v-for="item in activity" :key="item.id"><i class="pi pi-sparkles" /><div><strong>{{ item.purpose }} · {{ item.disposition }}</strong><p>{{ item.at }} · {{ item.provider }} / {{ item.model }}</p><p>Pages {{ item.page_ranges?.join(', ') || '—' }} · response {{ item.response_hash?.slice(0, 12) || 'fallback' }}</p></div></article>
          <p v-if="!activity.length" class="muted">No model activity references this document.</p>
        </div>
      </main>
      <div v-else class="empty-state"><div><i class="pi pi-file empty-state-icon" /><h3>Add engagement documents</h3><p>PDF, DOCX, text, Markdown, and common image formats are stored and processed locally.</p></div></div>
    </div>

    <Dialog v-model:visible="confirmOptin" modal header="Enable document AI for this engagement?" :style="{ width: 'min(34rem, 92vw)' }">
      <p>Structured data rows will still never leave this device. For documents only, the pages and purpose shown in each disclosure preview may be sent to your configured model. Every disclosure and model result is appended to the engagement logs.</p>
      <template #footer><Button label="Keep off" severity="secondary" @click="confirmOptin = false" /><Button label="Enable document AI" icon="pi pi-check" @click="enableOptin" /></template>
    </Dialog>

    <Dialog v-model:visible="disclosuresOpen" modal header="Document disclosure log" :style="{ width: 'min(58rem, 94vw)' }">
      <div class="timeline"><article v-for="item in disclosures" :key="item.id"><i class="pi pi-cloud-upload" /><div><strong>{{ item.purpose }} · pages {{ item.pages.join(', ') }}</strong><p>{{ item.at }} · {{ item.document_id }} · {{ item.pii_masked ? 'PII masking selected' : 'Unmasked' }}</p><code>{{ item.source_sha1 }}</code></div></article><p v-if="!disclosures.length" class="muted">Nothing has been disclosed.</p></div>
    </Dialog>

    <Dialog v-model:visible="knowledgeOpen" modal header="Methodology knowledge packs" :style="{ width: 'min(64rem, 95vw)' }">
      <div class="pack-toolbar"><input ref="packInput" type="file" hidden accept=".md,.markdown,.txt" @change="uploadPack" /><Button label="Add Markdown pack" icon="pi pi-plus" @click="packInput?.click()" /><InputText v-model="packSearch" placeholder="Search local methodology" @keyup.enter="searchPacks" /><Button label="Search" severity="secondary" @click="searchPacks" /></div>
      <div class="pack-grid"><article v-for="pack in packs" :key="`${pack.scope}:${pack.id}`"><strong>{{ pack.name }}</strong><Tag :value="pack.scope" severity="secondary" /><p>Version {{ pack.version }} · {{ pack.sha1.slice(0, 12) }}</p></article></div>
      <div v-if="packResults.length" class="search-results"><h4>Cited sections</h4><article v-for="(result, index) in packResults" :key="index"><strong>{{ result.citation }}</strong><p>{{ result.excerpt }}</p></article></div>
    </Dialog>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
  </section>
</template>

<style scoped>
.documents-tab { display: grid; gap: 1rem; min-height: 100%; }
.doc-toolbar { align-items: center; justify-content: space-between; margin: 0; }
.doc-toolbar h2, .detail-head h3 { margin: 0; }.toolbar-actions { display: flex; flex-wrap: wrap; gap: .5rem; }
.privacy-strip { display: flex; align-items: center; gap: .65rem; padding: .75rem 1rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-sm); }
.privacy-strip.enabled { background: var(--aw-teal-soft); color: var(--aw-ink); border-color: #b7e3dc; }.privacy-strip label { margin-left: auto; display: flex; align-items: center; gap: .45rem; }
.document-layout { display: grid; grid-template-columns: 20rem minmax(0, 1fr); min-height: 38rem; overflow: hidden; }
.document-rail { padding: .8rem; border-right: 1px solid var(--aw-border); background: var(--aw-raised); overflow-y: auto; }.rail-search { width: 100%; }.filters { display: grid; grid-template-columns: 1fr 1fr; gap: .45rem; margin: .5rem 0 1rem; }.filters :deep(.p-select) { min-width: 0; }
.doc-group h4 { display: flex; justify-content: space-between; margin: 1rem .35rem .35rem; color: var(--aw-muted); text-transform: uppercase; font-size: var(--aw-text-xs); letter-spacing: .06em; }.doc-row { width: 100%; display: grid; grid-template-columns: auto minmax(0,1fr) auto; align-items: center; gap: .55rem; padding: .65rem; border: 0; border-radius: var(--aw-radius-sm); background: transparent; color: inherit; text-align: left; cursor: pointer; }.doc-row:hover,.doc-row.active { background: #fff; box-shadow: var(--aw-shadow-sm); }.doc-row span { min-width: 0; }.doc-row strong,.doc-row small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.doc-row small { margin-top: .18rem; color: var(--aw-muted); }.rail-empty { padding: 2rem .5rem; text-align: center; color: var(--aw-muted); }
.document-detail { min-width: 0; display: flex; flex-direction: column; }.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; padding: 1rem 1.25rem; border-bottom: 1px solid var(--aw-border); }.detail-head code { display: block; margin-top: .35rem; color: var(--aw-muted); font-size: .68rem; }.detail-tabs { display: flex; padding: 0 1.25rem; border-bottom: 1px solid var(--aw-border); }.detail-tabs button { padding: .75rem .85rem; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--aw-muted); cursor: pointer; text-transform: capitalize; }.detail-tabs button.active { color: var(--aw-teal); border-color: var(--aw-teal); font-weight: 700; }.detail-content { padding: 1.25rem; overflow-y: auto; }.page-tools { display: flex; align-items: center; gap: .4rem; margin-bottom: .75rem; }.page-tools a { margin-left: auto; color: var(--aw-teal); }.page-text { min-height: 25rem; margin: 0; padding: 1.2rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); background: #fff; font-family: var(--aw-font-sans); white-space: pre-wrap; line-height: 1.65; }.scan-notice,.disclosure-preview { display: flex; gap: .75rem; padding: .9rem; margin-bottom: .75rem; border: 1px solid #f0cf9f; border-radius: var(--aw-radius-sm); background: var(--aw-warn-soft); }.scan-notice p,.disclosure-preview p { margin: .25rem 0 0; }.document-image { display: block; max-width: 100%; max-height: 34rem; margin: auto; }.ask-view { display: grid; gap: .9rem; max-width: 54rem; }.disclosure-preview { display: block; }.answer { padding: 1rem; }.citations { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .8rem; }
.timeline { display: grid; gap: .75rem; }.timeline article { display: grid; grid-template-columns: auto 1fr; gap: .75rem; padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.timeline p { margin: .25rem 0; color: var(--aw-muted); }.timeline code { overflow-wrap: anywhere; font-size: .7rem; }.pack-toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }.pack-toolbar .p-inputtext { flex: 1; }.pack-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(14rem,1fr)); gap: .65rem; }.pack-grid article,.search-results article { padding: .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-sm); }.pack-grid .p-tag { float: right; }.pack-grid p,.search-results p { margin: .4rem 0 0; color: var(--aw-muted); }.search-results { margin-top: 1.2rem; display: grid; gap: .55rem; }
@media (max-width: 900px) { .document-layout { grid-template-columns: 1fr; }.document-rail { max-height: 18rem; border-right: 0; border-bottom: 1px solid var(--aw-border); }.privacy-strip { align-items: flex-start; flex-wrap: wrap; }.privacy-strip label { margin-left: 0; width: 100%; } }
</style>
