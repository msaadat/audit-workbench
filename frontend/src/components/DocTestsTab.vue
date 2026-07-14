<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { AuditDocument, DocTest, DocTestItem, DocTestKind, EvidenceRef, WorkspaceSummary, WorkingPaper } from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const route = useRoute()
const router = useRouter()
const toast = useToast()
const agent = useAgentRun(props.workspace.id)
const { launchMode } = agent

const tests = ref<DocTest[]>([])
const current = ref<DocTest | null>(null)
const documents = ref<AuditDocument[]>([])
const selectedItemId = ref<string | null>(String(route.query.item || '') || null)
const view = ref<'worklist' | 'working-paper'>('worklist')
const createOpen = ref(false)
const creating = ref(false)
const running = ref(false)
const anchorOpen = ref(false)
const anchor = ref<EvidenceRef | null>(null)
const attachId = ref<string | null>(null)
const workingPaper = ref<WorkingPaper | null>(null)
const procedureId = ref<string | null>(null)
const draft = ref({ kind: 'vouching' as DocTestKind, title: '', procedureRefs: '', rcmRefs: '', direction: 'vouching', table: '', size: 10, seed: 42, frozenFields: '', attributes: '', documentId: '', pages: '', questions: '' })

const kinds = [
  { label: 'Vouching / tracing', value: 'vouching' },
  { label: 'Attribute test', value: 'attribute' },
  { label: 'Document review', value: 'review' },
  { label: 'Cited Q&A', value: 'qa' },
]
const methods = ['exact', 'normalized', 'fuzzy', 'numeric_tolerance', 'date_tolerance']
const selectedItem = computed(() => current.value?.items.find(item => item.id === selectedItemId.value) ?? null)
const tableOptions = computed(() => props.workspace.tables.map(table => table.name))
const documentOptions = computed(() => documents.value.map(doc => ({ label: `${doc.title} · v${doc.version}`, value: doc.id })))
const procedureOptions = computed(() => current.value?.procedure_refs ?? [])

function severity(state: string) {
  return state === 'confirmed' || state === 'match' || state === 'completed' ? 'success' : state === 'exception' || state === 'mismatch' ? 'danger' : state === 'manual_review' || state === 'in_progress' ? 'warn' : 'secondary'
}
function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function loadList(preferred?: string) {
  tests.value = (await api.get<{ items: DocTest[] }>(`/api/workspaces/${props.workspace.id}/doc-tests`)).items
  const target = preferred || String(route.query.test || '') || current.value?.id || tests.value[0]?.id
  if (target) await selectTest(target)
  else current.value = null
}
async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function selectTest(id: string) {
  current.value = await api.get<DocTest>(`/api/workspaces/${props.workspace.id}/doc-tests/${id}`)
  if (!current.value.items.some(item => item.id === selectedItemId.value)) selectedItemId.value = current.value.items[0]?.id ?? null
  procedureId.value = current.value.procedure_refs[0] ?? null
  await syncUrl()
}
async function syncUrl() {
  const query = { ...route.query, tab: 'doc-tests', test: current.value?.id, item: selectedItemId.value || undefined }
  if (route.query.test !== query.test || route.query.item !== query.item) await router.replace({ query })
}
watch(selectedItemId, () => void syncUrl())

async function createTest() {
  creating.value = true
  try {
    let created: DocTest
    const common = {
      title: draft.value.title || `New ${draft.value.kind} test`,
      procedure_refs: draft.value.procedureRefs.split(',').map(value => value.trim()).filter(Boolean),
      rcm_refs: draft.value.rcmRefs.split(',').map(value => value.trim()).filter(Boolean),
    }
    if (draft.value.kind === 'vouching') {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/vouching`, {
        ...common, table: draft.value.table, direction: draft.value.direction,
        size: draft.value.size, seed: draft.value.seed,
        frozen_fields: draft.value.frozenFields.split(',').map(value => value.trim()).filter(Boolean),
      })
    } else if (draft.value.kind === 'attribute') {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/attribute`, {
        ...common, attributes: draft.value.attributes.split(',').map(name => ({ name: name.trim(), expected: 'present' })).filter(value => value.name),
      })
    } else if (draft.value.kind === 'review') {
      const pages = draft.value.pages.split(',').map(value => Number(value.trim())).filter(Boolean)
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/review`, {
        ...common, document_id: draft.value.documentId,
        ...(pages.length ? { pages } : {}),
      })
    } else {
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/build/qa`, {
        ...common, document_ids: draft.value.documentId ? [draft.value.documentId] : [],
        questions: draft.value.questions.split('\n').map(value => value.trim()).filter(Boolean),
      })
    }
    createOpen.value = false
    await loadList(created.id)
  } catch (error) { fail('Could not create document test', error) }
  finally { creating.value = false }
}

async function attachDocument() {
  if (!current.value || !selectedItem.value || !attachId.value) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}/documents`, { document_id: attachId.value })
    await selectTest(current.value.id)
  } catch (error) { fail('Could not attach evidence', error) }
}
async function saveChecks() {
  if (!current.value || !selectedItem.value) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}/comparisons`, { checks: selectedItem.value.checks ?? [] })
    toast.add({ severity: 'success', summary: 'Comparison settings saved', life: 1800 })
  } catch (error) { fail('Could not save comparisons', error) }
}
async function disposition(value: DocTestItem['auditor_disposition']) {
  if (!current.value || !selectedItem.value) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}`, { auditor_disposition: value, auditor_note: selectedItem.value.auditor_note })
    await selectTest(current.value.id)
  } catch (error) { fail('Could not save auditor disposition', error) }
}
async function runTest() {
  if (!current.value) return
  running.value = true
  try {
    await agent.startRun(launchMode.value, { test_id: current.value.id }, 'doc_test')
    toast.add({ severity: 'info', summary: 'Document test started', detail: 'Progress is visible in the assistant drawer.', life: 3000 })
  } catch (error) { fail('Could not start document test', error) }
  finally { running.value = false }
}
function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }
async function openWorkingPaper() {
  if (!procedureId.value) { workingPaper.value = null; return }
  try { workingPaper.value = await api.get(`/api/workspaces/${props.workspace.id}/procedures/${procedureId.value}/working-paper`) }
  catch (error) { fail('Could not render working paper', error) }
}
async function draftResults() {
  if (!procedureId.value) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/procedures/${procedureId.value}/draft-results`)
    await openWorkingPaper()
    toast.add({ severity: 'success', summary: 'Procedure result draft refreshed', life: 1800 })
  } catch (error) { fail('Could not draft procedure results', error) }
}
async function copyPaper(kind: 'markdown' | 'html') {
  if (workingPaper.value) await navigator.clipboard.writeText(workingPaper.value[kind])
}

onMounted(() => void Promise.all([loadList(), loadDocuments()]).catch(error => fail('Could not load document tests', error)))
const unsubscribe = agent.onWorkspaceChanged(change => { if (change.kind === 'doctest') void loadList(current.value?.id) })
onUnmounted(unsubscribe)
</script>

<template>
  <div class="doc-tests">
    <header class="test-head">
      <div><p class="eyebrow">Fieldwork</p><h2>Document tests</h2><p class="muted">Freeze worklists, compare source evidence locally, and record an auditor disposition.</p></div>
      <div class="actions"><Button label="New test" icon="pi pi-plus" outlined @click="createOpen = true"/><Button label="Run selected" icon="pi pi-play" :loading="running" :disabled="!current || agent.isActive.value" @click="runTest"/></div>
    </header>

    <div class="test-layout">
      <aside class="test-rail card">
        <div class="rail-title"><strong>Tests</strong><Tag :value="String(tests.length)" severity="secondary"/></div>
        <button v-for="test in tests" :key="test.id" :class="{ active: current?.id === test.id }" @click="selectTest(test.id)">
          <span><strong>{{ test.title }}</strong><Tag :value="test.status.replace('_', ' ')" :severity="severity(test.status)"/></span>
          <small>{{ test.kind }} · {{ test.item_count }} item(s)</small>
        </button>
        <p v-if="!tests.length" class="empty">No document tests yet.</p>
      </aside>

      <main v-if="current" class="test-detail">
        <div class="detail-title card"><div><span class="eyebrow">{{ current.id }} · {{ current.kind }}</span><h3>{{ current.title }}</h3></div><div class="rollups"><Tag :value="`${current.rollup?.matched ?? 0} matched`" severity="success"/><Tag :value="`${current.rollup?.mismatched ?? 0} mismatch / missing`" :severity="current.rollup?.mismatched ? 'danger' : 'secondary'"/><Tag :value="`${current.rollup?.manual_review ?? 0} manual`" :severity="current.rollup?.manual_review ? 'warn' : 'secondary'"/></div></div>
        <SelectButton v-model="view" :options="[{label:'Worklist & setup',value:'worklist'},{label:'Working paper',value:'working-paper'}]" optionLabel="label" optionValue="value" :allowEmpty="false" @change="view === 'working-paper' && openWorkingPaper()"/>

        <div v-if="view === 'worklist'" class="work-layout">
          <section class="worklist card">
            <button v-for="item in current.items" :key="item.id" :class="{ active: selectedItemId === item.id }" @click="selectedItemId = item.id">
              <span><strong>{{ item.label }}</strong><Tag :value="item.state.replace('_', ' ')" :severity="severity(item.state)"/></span>
              <small v-if="item.frozen">{{ Object.entries(item.frozen).slice(0, 3).map(([key,value]) => `${key}: ${value}`).join(' · ') }}</small>
              <small v-else-if="item.attributes">{{ item.attributes.length }} attribute(s)</small>
              <small v-else-if="item.page">Page {{ item.page }} · {{ item.review_kind }}</small>
              <small v-else>{{ item.question }}</small>
            </button>
            <p v-if="!current.items.length" class="empty">This test has no items yet.</p>
          </section>

          <section v-if="selectedItem" class="item-detail card">
            <div class="item-title"><div><span class="eyebrow">{{ selectedItem.id }}</span><h3>{{ selectedItem.label }}</h3></div><Tag :value="selectedItem.auditor_disposition.replaceAll('_',' ')" :severity="severity(selectedItem.state)"/></div>
            <p v-if="selectedItem.runner_note" class="runner-note"><i class="pi pi-info-circle"/> {{ selectedItem.runner_note }}</p>
            <div v-if="selectedItem.document_conflicts?.duplicate_documents.length || selectedItem.document_conflicts?.version_conflicts.length" class="conflict"><strong>Document conflict</strong><span>Duplicate evidence or multiple versions are attached. Resolve this manually before accepting.</span></div>
            <div class="attach"><Select v-model="attachId" :options="documentOptions" optionLabel="label" optionValue="value" filter placeholder="Attach a document"/><Button label="Attach" icon="pi pi-paperclip" outlined @click="attachDocument"/></div>
            <div class="attached"><Tag v-for="docId in selectedItem.document_ids" :key="docId" :value="documents.find(doc => doc.id === docId)?.title || docId" severity="info"/></div>

            <div v-if="selectedItem.checks" class="checks">
              <article v-for="check in selectedItem.checks" :key="check.field">
                <div class="check-head"><strong>{{ check.field }}</strong><Tag :value="check.verdict" :severity="severity(check.verdict)"/></div>
                <div class="comparison-settings"><span>Expected: <code>{{ check.expected }}</code></span><Select v-model="check.method" :options="methods"/><InputText :modelValue="String(check.tolerance ?? '')" @update:modelValue="check.tolerance = $event" placeholder="Tolerance"/></div>
                <div v-for="result in check.comparisons" :key="`${result.document_id}:${result.page}`" class="result-row"><span>{{ documents.find(doc => doc.id === result.document_id)?.title || result.document_id }} · page {{ result.page || '—' }}</span><code>{{ result.expected }} ↔ {{ result.found ?? 'missing' }}</code><Tag :value="result.result" :severity="severity(result.result)"/><Button v-if="result.evidence" icon="pi pi-link" text rounded aria-label="Open evidence" @click="showAnchor(result.evidence)"/></div>
              </article>
              <Button label="Save comparison methods" icon="pi pi-save" size="small" outlined @click="saveChecks"/>
            </div>
            <div v-else-if="selectedItem.attributes" class="attributes"><article v-for="attribute in selectedItem.attributes" :key="attribute.name"><strong>{{ attribute.name }}</strong><Tag :value="attribute.verdict" :severity="severity(attribute.verdict)"/><InputText v-model="attribute.note" placeholder="Auditor note"/></article></div>
            <blockquote v-else-if="selectedItem.excerpt">{{ selectedItem.excerpt }}</blockquote>
            <div v-else-if="selectedItem.question" class="qa"><strong>{{ selectedItem.question }}</strong><p>{{ selectedItem.response || 'No response recorded yet.' }}</p><div class="attached"><Button v-for="citation in selectedItem.citations" :key="citation.id" :label="`Page ${citation.page || '—'}`" icon="pi pi-link" size="small" text @click="showAnchor(citation)"/></div></div>

            <label>Auditor note<Textarea v-model="selectedItem.auditor_note" rows="3" autoResize/></label>
            <div class="dispositions"><Button label="Accept result" icon="pi pi-check" severity="success" outlined @click="disposition('accepted')"/><Button label="Needs manual check" icon="pi pi-eye" severity="warn" outlined @click="disposition('needs_manual_check')"/><Button label="Mark exception" icon="pi pi-exclamation-triangle" severity="danger" outlined @click="disposition('exception')"/></div>
          </section>
          <section v-else class="card empty">Select a worklist item.</section>
        </div>

        <section v-else class="paper card">
          <div class="paper-actions"><Select v-model="procedureId" :options="procedureOptions" placeholder="Linked procedure" @change="openWorkingPaper"/><Button label="Draft results" icon="pi pi-sparkles" outlined :disabled="!procedureId" @click="draftResults"/><Button label="Copy Markdown" icon="pi pi-copy" text :disabled="!workingPaper" @click="copyPaper('markdown')"/><Button label="Copy HTML" icon="pi pi-copy" text :disabled="!workingPaper" @click="copyPaper('html')"/></div>
          <p v-if="!procedureOptions.length" class="empty">Link this test to an audit-program procedure to generate its working-paper draft.</p>
          <div v-else-if="workingPaper" class="paper-preview" v-html="workingPaper.html"/>
          <p v-else class="empty">Choose a linked procedure.</p>
        </section>
      </main>
      <main v-else class="card empty">Create a document test to begin fieldwork.</main>
    </div>

    <Dialog v-model:visible="createOpen" modal header="New document test" :style="{width:'min(44rem,94vw)'}">
      <div class="create-form"><label>Test kind<Select v-model="draft.kind" :options="kinds" optionLabel="label" optionValue="value"/></label><label>Title<InputText v-model="draft.title" placeholder="e.g. Invoice vouching"/></label><div class="two"><label>Procedure IDs (comma separated)<InputText v-model="draft.procedureRefs" placeholder="PROC-..."/></label><label>RCM IDs (comma separated)<InputText v-model="draft.rcmRefs" placeholder="RCM-..."/></label></div>
        <template v-if="draft.kind === 'vouching'"><div class="two"><label>Direction<Select v-model="draft.direction" :options="['vouching','tracing']"/></label><label>Population table<Select v-model="draft.table" :options="tableOptions"/></label></div><div class="two"><label>Sample size<InputNumber v-model="draft.size" :min="1"/></label><label>Seed<InputNumber v-model="draft.seed"/></label></div><label>Frozen fields (comma separated)<InputText v-model="draft.frozenFields" placeholder="invoice_no, amount, tx_date"/></label></template>
        <label v-else-if="draft.kind === 'attribute'">Attributes (comma separated)<InputText v-model="draft.attributes" placeholder="approval, signature, date"/></label>
        <template v-else-if="draft.kind === 'review'"><label>Document<Select v-model="draft.documentId" :options="documentOptions" optionLabel="label" optionValue="value" filter/></label><label>Pages (comma separated; blank = all)<InputText v-model="draft.pages" placeholder="1, 3, 4"/></label></template>
        <template v-else><label>Document<Select v-model="draft.documentId" :options="documentOptions" optionLabel="label" optionValue="value" filter/></label><label>Questions (one per line)<Textarea v-model="draft.questions" rows="5"/></label></template>
      </div><template #footer><Button label="Cancel" text severity="secondary" @click="createOpen=false"/><Button label="Create worklist" icon="pi pi-plus" :loading="creating" @click="createTest"/></template>
    </Dialog>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor"/>
  </div>
</template>

<style scoped>
.doc-tests { display:flex; flex-direction:column; gap:1rem; min-height:100%; }.test-head,.actions,.detail-title,.rollups,.rail-title,.item-title,.check-head,.attach,.paper-actions,.dispositions { display:flex; align-items:center; gap:.55rem }.test-head,.detail-title,.rail-title,.item-title,.check-head { justify-content:space-between }.test-head h2,.detail-title h3,.item-title h3 { margin:.1rem 0 }.test-head p { margin:0 }.test-layout { display:grid; grid-template-columns:18rem minmax(0,1fr); gap:1rem }.card { background:#fff; border:1px solid var(--aw-border); border-radius:var(--aw-radius); box-shadow:var(--aw-shadow-sm); padding:1rem }.test-rail { padding:.55rem; align-self:start }.test-rail button,.worklist button { border:0; background:transparent; width:100%; text-align:left; padding:.65rem; border-radius:7px; cursor:pointer }.test-rail button:hover,.test-rail button.active,.worklist button:hover,.worklist button.active { background:var(--p-primary-50) }.test-rail button span,.worklist button span { display:flex; justify-content:space-between; gap:.5rem; align-items:center }.test-rail small,.worklist small { display:block; margin-top:.25rem; color:var(--aw-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap }.test-detail { min-width:0; display:flex; flex-direction:column; gap:.8rem }.work-layout { display:grid; grid-template-columns:minmax(15rem,.8fr) minmax(24rem,1.6fr); gap:1rem }.worklist { padding:.5rem; max-height:calc(100vh - 22rem); overflow:auto }.item-detail { display:flex; flex-direction:column; gap:.8rem }.muted,.empty { color:var(--aw-muted); font-size:.8rem }.attach :deep(.p-select) { flex:1 }.attached,.rollups { display:flex; gap:.35rem; flex-wrap:wrap }.runner-note,.conflict { padding:.7rem; border-radius:7px; background:var(--p-blue-50); margin:0 }.conflict { display:grid; background:var(--p-orange-50); color:var(--p-orange-800) }.checks { display:grid; gap:.75rem }.checks article,.attributes article { border:1px solid var(--aw-border); border-radius:7px; padding:.7rem }.comparison-settings { display:grid; grid-template-columns:1fr 12rem 9rem; gap:.5rem; align-items:center; margin-top:.5rem }.result-row { display:grid; grid-template-columns:1fr 1fr auto auto; gap:.5rem; align-items:center; padding:.45rem 0; border-top:1px solid var(--aw-border); font-size:.78rem }code { font-family:var(--aw-font-mono); font-size:.75rem }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.dispositions { flex-wrap:wrap }.paper-actions { flex-wrap:wrap }.paper-preview { max-width:58rem; margin:1rem auto; line-height:1.6 }.create-form { display:grid; gap:.8rem }.two { display:grid; grid-template-columns:1fr 1fr; gap:.8rem }blockquote { margin:0; padding:.8rem; border-left:3px solid var(--aw-teal); background:var(--aw-canvas) }
@media(max-width:1100px){.test-layout,.work-layout{grid-template-columns:1fr}.test-rail{display:flex;overflow:auto}.test-rail button{min-width:15rem}.worklist{max-height:16rem}}@media(max-width:700px){.test-head,.detail-title{align-items:flex-start;flex-direction:column}.comparison-settings,.result-row,.two{grid-template-columns:1fr}}
</style>
