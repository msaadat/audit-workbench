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
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceQuery } from '../composables/useWorkspaceNavigation'
import type { AuditDocument, DocTest, DocTestItem, DocTestKind, EvidenceRef, PlanningPayload, WorkspaceSummary, WorkingPaper } from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const router = useRouter()
const toast = useToast()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)
const { launchMode } = agent

const tests = ref<DocTest[]>([])
const current = ref<DocTest | null>(null)
const documents = ref<AuditDocument[]>([])
const selectedItemId = ref<string | null>(String(route.query.item || '') || null)
const view = ref<'worklist' | 'working-paper'>('worklist')
const createOpen = ref(false)
const createStep = ref(1)
const creating = ref(false)
const running = ref(false)
const anchorOpen = ref(false)
const anchor = ref<EvidenceRef | null>(null)
const attachId = ref<string | null>(null)
const workingPaper = ref<WorkingPaper | null>(null)
const planning = ref<PlanningPayload | null>(null)
const draft = ref({ kind: 'vouching' as DocTestKind, title: '', rcmId: '', plannedTestId: '', direction: 'vouching', table: '', size: 10, seed: 42, frozenFields: '', identifierFields: '', requiredDocumentTypes: '', evidenceAware: true, attributes: '', documentId: '', pages: '', questions: '' })
const createRequested = route.query.create === '1'
const requestedCreateRcm = String(route.query.rcm || '')
const requestedCreatePlannedTest = String(route.query.planned_test || '')

const kinds = [
  { label: 'Vouching / tracing', value: 'vouching' },
  { label: 'Attribute test', value: 'attribute' },
  { label: 'Document review', value: 'review' },
  { label: 'Cited Q&A', value: 'qa' },
]
const methods = ['exact', 'normalized', 'fuzzy', 'numeric_tolerance', 'date_tolerance']
const selectedItem = computed(() => current.value?.items.find(item => item.id === selectedItemId.value) ?? null)
const tableOptions = computed(() => props.workspace.tables.map(table => table.name))
const documentOptions = computed(() => documents.value.map(doc => ({ label: doc.title, value: doc.id })))
const rcmOptions = computed(() => (planning.value?.rcm ?? []).map(item => ({ label: `${item.id} · ${item.risk}`, value: item.id })))
const plannedTestOptions = computed(() => (planning.value?.rcm.find(item => item.id === draft.value.rcmId)?.planned_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: item.id })))
const assistantUnavailable = computed(() => agent.isActive.value || assistantChat.state.busy)
const draftReady = computed(() => {
  if (!draft.value.rcmId || !draft.value.plannedTestId) return false
  if (draft.value.kind === 'vouching') return Boolean(draft.value.table)
  if (draft.value.kind === 'review' || draft.value.kind === 'qa') return Boolean(draft.value.documentId)
  return true
})

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
async function loadPlanning() {
  planning.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
}
async function selectTest(id: string) {
  current.value = await api.get<DocTest>(`/api/workspaces/${props.workspace.id}/doc-tests/${id}`)
  if (!current.value.items.some(item => item.id === selectedItemId.value)) selectedItemId.value = current.value.items[0]?.id ?? null
  await syncUrl()
}
async function syncUrl() {
  const query = workspaceQuery('doc-tests', { test: current.value?.id, item: selectedItemId.value || undefined })
  await router.replace({ query })
}
watch(selectedItemId, () => void syncUrl())

async function createTest() {
  creating.value = true
  try {
    let created: DocTest
    const common = {
      title: draft.value.title || `New ${draft.value.kind} test`,
      rcm_id: draft.value.rcmId,
      planned_test_id: draft.value.plannedTestId,
      rcm_refs: [draft.value.rcmId],
    }
    if (draft.value.kind === 'vouching') {
      const path = draft.value.evidenceAware ? 'prepare-evidence-aware' : 'build/vouching'
      created = await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/${path}`, {
        ...common, table: draft.value.table, direction: draft.value.direction,
        size: draft.value.size, seed: draft.value.seed,
        frozen_fields: draft.value.frozenFields.split(',').map(value => value.trim()).filter(Boolean),
        identifier_fields: draft.value.identifierFields.split(',').map(value => value.trim()).filter(Boolean),
        required_document_types: draft.value.requiredDocumentTypes.split(',').map(value => value.trim()).filter(Boolean),
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
    emit('changed')
  } catch (error) { fail('Could not create document test', error) }
  finally { creating.value = false }
}

async function attachDocument() {
  if (!current.value || !selectedItem.value || !attachId.value) return
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}/documents`, { document_id: attachId.value })
    await selectTest(current.value.id)
    emit('changed')
  } catch (error) { fail('Could not attach evidence', error) }
}
async function saveChecks() {
  if (!current.value || !selectedItem.value) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}/comparisons`, { checks: selectedItem.value.checks ?? [] })
    emit('changed')
    toast.add({ severity: 'success', summary: 'Comparison settings saved', life: 1800 })
  } catch (error) { fail('Could not save comparisons', error) }
}
async function disposition(value: DocTestItem['auditor_disposition']) {
  if (!current.value || !selectedItem.value) return
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/doc-tests/${current.value.id}/items/${selectedItem.value.id}`, { auditor_disposition: value, auditor_note: selectedItem.value.auditor_note })
    await selectTest(current.value.id)
    emit('changed')
  } catch (error) { fail('Could not save auditor disposition', error) }
}
async function runTest() {
  if (!current.value) return
  running.value = true
  try {
    await assistantChat.send(`Run document test ${current.value.id} and preserve its results.`, 'act', launchMode.value, { goalTemplate: 'document_testing', source: 'tab_button' })
    toast.add({ severity: 'info', summary: 'Document test started', detail: 'Progress is visible in the assistant drawer.', life: 3000 })
  } catch (error) { fail('Could not start document test', error) }
  finally { running.value = false }
}
async function prepareTests() {
  if (assistantUnavailable.value) {
    toast.add({
      severity: 'info', summary: 'Assistant is already working',
      detail: 'Finish or cancel the active run before preparing another document-test batch.',
      life: 3000,
    })
    return
  }
  try {
    await assistantChat.send('Prepare the next required Document Tests from the RCM planned tests, prioritizing imported evidence-covered transactions and creating explicit evidence requests for missing support.', 'act', launchMode.value, { goalTemplate: 'document_testing', source: 'tab_button' })
    toast.add({ severity: 'info', summary: 'Preparing document tests', detail: 'Review progress and any required decisions in the assistant.', life: 3000 })
  } catch (error) { fail('Could not start document test preparation', error) }
}
function openManualCreate() {
  createStep.value = 1
  draft.value.rcmId = String(route.query.rcm || draft.value.rcmId || '')
  draft.value.plannedTestId = String(route.query.planned_test || draft.value.plannedTestId || '')
  createOpen.value = true
}
function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }
async function openWorkingPaper() {
  if (!current.value?.rcm_id) { workingPaper.value = null; return }
  try { workingPaper.value = await api.get(`/api/workspaces/${props.workspace.id}/rcm/${current.value.rcm_id}/working-paper`) }
  catch (error) { fail('Could not render working paper', error) }
}
async function draftResults() {
  if (!current.value?.rcm_id) return
  try {
    workingPaper.value = await api.post(`/api/workspaces/${props.workspace.id}/rcm/${current.value.rcm_id}/working-paper`)
    await openWorkingPaper()
    emit('changed')
    toast.add({ severity: 'success', summary: 'RCM working paper refreshed', life: 1800 })
  } catch (error) { fail('Could not generate RCM working paper', error) }
}
async function copyPaper(kind: 'markdown' | 'html') {
  if (workingPaper.value) await navigator.clipboard.writeText(workingPaper.value[kind])
}

onMounted(() => void Promise.all([loadList(), loadDocuments(), loadPlanning()]).then(() => {
  if (createRequested) {
    draft.value.rcmId = requestedCreateRcm
    draft.value.plannedTestId = requestedCreatePlannedTest
    openManualCreate()
  }
}).catch(error => fail('Could not load document tests', error)))
const unsubscribe = agent.onWorkspaceChanged(change => { if (change.kind === 'doctest') void loadList(current.value?.id) })
onUnmounted(unsubscribe)
</script>

<template>
  <div class="doc-tests">
    <UiPageHeader title="Document tests" description="Execute and document evidence-based fieldwork">
      <Button v-if="tests.length" label="Prepare with assistant" icon="pi pi-sparkles" :disabled="assistantUnavailable" @click="prepareTests"/>
      <Button label="Create manually" icon="pi pi-plus" outlined @click="openManualCreate"/>
      <Button v-if="current" label="Run selected" icon="pi pi-play" severity="secondary" :loading="running" :disabled="agent.isActive.value" @click="runTest"/>
    </UiPageHeader>

    <div v-if="tests.length" class="test-layout">
      <aside class="test-rail card">
        <div class="rail-title"><strong>Tests</strong><Tag :value="String(tests.length)" severity="secondary"/></div>
        <button v-for="test in tests" :key="test.id" :class="{ active: current?.id === test.id }" @click="selectTest(test.id)">
          <span><strong>{{ test.title }}</strong><Tag :value="test.status.replace('_', ' ')" :severity="severity(test.status)"/></span>
          <small>{{ test.kind }} · {{ test.item_count }} item(s)</small>
        </button>
        <p v-if="!tests.length" class="empty">No document tests yet.</p>
      </aside>

      <main v-if="current" class="test-detail">
        <div class="detail-title card"><div><span class="eyebrow">{{ current.id }} · {{ current.kind }}</span><h3>{{ current.title }}</h3><small class="muted">Parent {{ current.rcm_id || 'unassigned' }} · {{ current.planned_test_id || 'coverage not satisfied' }}</small></div><div class="rollups"><Tag :value="`${current.rollup?.matched ?? 0} matched`" severity="success"/><Tag :value="`${current.rollup?.mismatched ?? 0} mismatch / missing`" :severity="current.rollup?.mismatched ? 'danger' : 'secondary'"/><Tag :value="`${current.rollup?.manual_review ?? 0} manual`" :severity="current.rollup?.manual_review ? 'warn' : 'secondary'"/></div></div>
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
            <div v-if="selectedItem.evidence_coverage" class="coverage"><strong>Evidence coverage</strong><span>{{ selectedItem.evidence_coverage.document_ids.length }} attached · {{ selectedItem.evidence_coverage.missing_document_types.length }} missing type(s)</span><small v-if="selectedItem.evidence_coverage.missing_document_types.length">Requested: {{ selectedItem.evidence_coverage.missing_document_types.join(', ') }}</small><Tag v-if="selectedItem.evidence_coverage.image_only" value="OCR / manual review" severity="warn"/></div>
            <div v-if="selectedItem.document_conflicts?.duplicate_documents.length" class="conflict"><strong>Document conflict</strong><span>Duplicate evidence is attached. Resolve this manually before accepting.</span></div>
            <div class="attach"><Select v-model="attachId" :options="documentOptions" optionLabel="label" optionValue="value" filter placeholder="Attach a document"/><Button label="Attach" icon="pi pi-paperclip" outlined @click="attachDocument"/></div>
            <div class="attached"><Tag v-for="docId in selectedItem.document_ids" :key="docId" :value="documents.find(doc => doc.id === docId)?.title || docId" severity="info"/></div>

            <div v-if="selectedItem.checks" class="checks">
              <article v-for="check in selectedItem.checks" :key="check.field">
                <div class="check-head"><strong>{{ check.field }}</strong><Tag :value="check.verdict" :severity="severity(check.verdict)"/></div>
                <UiAdvancedSection title="Comparison method" description="Matching rule and optional tolerance"><div class="comparison-settings"><span>Expected: <code>{{ check.expected }}</code></span><Select v-model="check.method" :options="methods"/><InputText :modelValue="String(check.tolerance ?? '')" @update:modelValue="check.tolerance = $event" placeholder="Tolerance"/></div></UiAdvancedSection>
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
          <div class="paper-actions"><Button label="Generate RCM working paper" icon="pi pi-sparkles" outlined :disabled="!current.rcm_id" @click="draftResults"/><Button label="Copy Markdown" icon="pi pi-copy" text :disabled="!workingPaper" @click="copyPaper('markdown')"/><Button label="Copy HTML" icon="pi pi-copy" text :disabled="!workingPaper" @click="copyPaper('html')"/></div>
          <p v-if="!current.rcm_id" class="empty">Assign this test to an RCM planned test before generating a working paper.</p>
          <div v-else-if="workingPaper" class="paper-preview" v-html="workingPaper.html"/>
          <p v-else class="empty">Generate the parent RCM working paper to include all linked execution.</p>
        </section>
      </main>
      <main v-else><UiEmptyState icon="pi pi-verified" title="Loading test" description="Preparing the selected worklist." /></main>
    </div>
    <UiEmptyState v-else icon="pi pi-verified" title="Prepare document fieldwork" description="Create Document Tests from RCM planned tests and prioritize transactions with imported evidence."><Button label="Prepare with assistant" icon="pi pi-sparkles" :disabled="assistantUnavailable" @click="prepareTests"/><Button label="Create manually" icon="pi pi-plus" severity="secondary" outlined @click="openManualCreate"/></UiEmptyState>

    <Dialog v-model:visible="createOpen" modal header="New document test" :style="{width:'min(44rem,94vw)'}">
      <div class="wizard-steps"><span v-for="n in 3" :key="n" :class="{ active: createStep === n, done: createStep > n }"><i>{{ n }}</i>{{ ['Type','Source & scope','Review'][n-1] }}</span></div>
      <div v-if="createStep === 1" class="create-form"><label>Test kind<Select v-model="draft.kind" :options="kinds" optionLabel="label" optionValue="value"/></label><label>Title<InputText v-model="draft.title" placeholder="e.g. Invoice vouching"/></label></div>
      <div v-else-if="createStep === 2" class="create-form">
        <template v-if="draft.kind === 'vouching'"><div class="two"><label>Direction<Select v-model="draft.direction" :options="['vouching','tracing']"/></label><label>Population table<Select v-model="draft.table" :options="tableOptions"/></label></div><div class="two"><label>Sample size<InputNumber v-model="draft.size" :min="1"/></label><label>Seed<InputNumber v-model="draft.seed"/></label></div><label><span><input v-model="draft.evidenceAware" type="checkbox"/> Prioritize evidence-covered transactions</span></label><label>Identifier fields (comma separated)<InputText v-model="draft.identifierFields" placeholder="requisition_id, po_id, grn_id, invoice_id"/></label><label>Required document types (comma separated)<InputText v-model="draft.requiredDocumentTypes" placeholder="requisition, purchase_order, goods_receipt, invoice"/></label><label>Frozen fields (comma separated)<InputText v-model="draft.frozenFields" placeholder="invoice_no, amount, tx_date"/></label></template>
        <label v-else-if="draft.kind === 'attribute'">Attributes (comma separated)<InputText v-model="draft.attributes" placeholder="approval, signature, date"/></label>
        <template v-else-if="draft.kind === 'review'"><label>Document<Select v-model="draft.documentId" :options="documentOptions" optionLabel="label" optionValue="value" filter/></label><label>Pages (comma separated; blank = all)<InputText v-model="draft.pages" placeholder="1, 3, 4"/></label></template>
        <template v-else><label>Document<Select v-model="draft.documentId" :options="documentOptions" optionLabel="label" optionValue="value" filter/></label><label>Questions (one per line)<Textarea v-model="draft.questions" rows="5"/></label></template>
      </div>
      <div v-else class="create-form"><div class="review-summary"><strong>{{ draft.title || `New ${draft.kind} test` }}</strong><span>{{ kinds.find(item => item.value === draft.kind)?.label }}</span><span v-if="draft.table">Population: {{ draft.table }}</span><span v-if="draft.documentId">Document: {{ documentOptions.find(item => item.value === draft.documentId)?.label }}</span></div><div class="create-form parent-links"><strong>Required RCM linkage</strong><label>Risk and control<Select v-model="draft.rcmId" :options="rcmOptions" optionLabel="label" optionValue="value" filter @change="draft.plannedTestId = ''"/></label><label>Planned test<Select v-model="draft.plannedTestId" :options="plannedTestOptions" optionLabel="label" optionValue="value" filter/></label><small class="muted">Unassigned tests do not satisfy engagement coverage.</small></div></div>
      <template #footer><Button label="Cancel" text severity="secondary" @click="createOpen=false"/><span class="grow"/><Button v-if="createStep > 1" label="Back" severity="secondary" outlined @click="createStep--"/><Button v-if="createStep < 3" label="Next" icon="pi pi-arrow-right" iconPos="right" :disabled="createStep === 2 && !draftReady" @click="createStep++"/><Button v-else label="Create worklist" icon="pi pi-plus" :loading="creating" :disabled="!draftReady" @click="createTest"/></template>
    </Dialog>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor"/>
  </div>
</template>

<style scoped>
.doc-tests { display:flex; flex-direction:column; gap:1rem; min-height:100%; }.test-head,.actions,.detail-title,.rollups,.rail-title,.item-title,.check-head,.attach,.paper-actions,.dispositions { display:flex; align-items:center; gap:.55rem }.test-head,.detail-title,.rail-title,.item-title,.check-head { justify-content:space-between }.test-head h2,.detail-title h3,.item-title h3 { margin:.1rem 0 }.test-head p { margin:0 }.test-layout { display:grid; grid-template-columns:17rem minmax(0,1fr); gap:1rem }.card { background:#fff; border:1px solid var(--aw-border); border-radius:var(--aw-radius-md); padding:1rem }.test-rail { padding:.55rem; align-self:start }.test-rail button,.worklist button { border:0; background:transparent; width:100%; min-height:var(--aw-row-height); text-align:left; padding:.5rem .6rem; border-radius:7px; cursor:pointer }.test-rail button:hover,.test-rail button.active,.worklist button:hover,.worklist button.active { background:var(--p-primary-50) }.test-rail button span,.worklist button span { display:flex; justify-content:space-between; gap:.5rem; align-items:center }.test-rail small,.worklist small { display:block; margin-top:.25rem; color:var(--aw-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap }.test-detail { min-width:0; display:flex; flex-direction:column; gap:.8rem }.work-layout { display:grid; grid-template-columns:minmax(14rem,.75fr) minmax(22rem,1.6fr); gap:1rem }.worklist { padding:.5rem; max-height:36rem; overflow:auto }.item-detail { display:flex; flex-direction:column; gap:.8rem }.muted,.empty { color:var(--aw-muted); font-size:.8rem }.attach :deep(.p-select) { flex:1 }.attached,.rollups { display:flex; gap:.35rem; flex-wrap:wrap }.runner-note,.conflict { padding:.7rem; border-radius:7px; background:var(--p-blue-50); margin:0 }.conflict { display:grid; background:var(--p-orange-50); color:var(--p-orange-800) }.coverage { display:grid; grid-template-columns:1fr auto; gap:.25rem .5rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:7px; background:var(--aw-canvas) }.coverage small { grid-column:1/-1; color:var(--aw-muted) }.checks { display:grid; gap:.75rem }.checks article,.attributes article { border:1px solid var(--aw-border); border-radius:7px; padding:.7rem }.comparison-settings { display:grid; grid-template-columns:1fr 12rem 9rem; gap:.5rem; align-items:center }.result-row { display:grid; grid-template-columns:1fr 1fr auto auto; gap:.5rem; align-items:center; padding:.45rem 0; border-top:1px solid var(--aw-border); font-size:.78rem }code { font-family:var(--aw-font-mono); font-size:.75rem }label { display:flex; flex-direction:column; gap:.3rem; color:#46576d; font-size:.75rem; font-weight:600 }.dispositions { position:sticky; bottom:-1rem; z-index:2; flex-wrap:wrap; margin:0 -1rem -1rem; padding:.7rem 1rem; border-top:1px solid var(--aw-border); background:#fff }.paper-actions { flex-wrap:wrap }.paper-preview { max-width:58rem; margin:1rem auto; line-height:1.6 }.create-form { display:grid; gap:.8rem }.parent-links { padding:.75rem; border:1px solid var(--aw-border); border-radius:7px; background:var(--aw-canvas) }.two { display:grid; grid-template-columns:1fr 1fr; gap:.8rem }blockquote { margin:0; padding:.8rem; border-left:3px solid var(--aw-teal); background:var(--aw-canvas) }
.grow { flex:1 }
.wizard-steps { display:grid; grid-template-columns:repeat(3,1fr); margin-bottom:1rem; border-bottom:1px solid var(--aw-border) }.wizard-steps span { display:flex; align-items:center; gap:.4rem; padding:.55rem; color:var(--aw-muted); font-size:.72rem; font-weight:700 }.wizard-steps i { display:grid; place-items:center; width:1.45rem; height:1.45rem; border-radius:999px; background:var(--aw-raised); font-style:normal }.wizard-steps span.active { color:var(--aw-teal); border-bottom:2px solid var(--aw-teal) }.wizard-steps span.active i,.wizard-steps span.done i { color:white; background:var(--aw-teal) }.review-summary { display:grid; gap:.2rem; padding:.8rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm); background:var(--aw-canvas) }.review-summary span { color:var(--aw-muted); font-size:.76rem }
@media(max-width:1100px){.test-layout,.work-layout{grid-template-columns:1fr}.test-rail{display:flex;overflow:auto}.test-rail button{min-width:15rem}.worklist{max-height:16rem}}@media(max-width:900px){.dispositions{bottom:2.25rem}}@media(max-width:700px){.test-head,.detail-title{align-items:flex-start;flex-direction:column}.comparison-settings,.result-row,.two{grid-template-columns:1fr}}
</style>
