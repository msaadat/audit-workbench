<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import type { AuditFinding, EvidenceRef, FindingsPayload, FindingSeverity, WorkspaceSummary } from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const router = useRouter()
const toast = useToast()

const data = ref<FindingsPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.finding || '') || null)
const saving = ref(false)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const search = ref('')
const severityFilter = ref<string>('all')

const severities: FindingSeverity[] = ['critical', 'high', 'medium', 'low', 'info']
const severityOptions = ['all', ...severities]
const selected = computed(() => data.value?.items.find(item => item.id === selectedId.value) ?? null)
const filtered = computed(() => (data.value?.items ?? []).filter(item => {
  const matchesSeverity = severityFilter.value === 'all' || item.severity === severityFilter.value
  const needle = search.value.trim().toLowerCase()
  return matchesSeverity && (!needle || `${item.id} ${item.title} ${item.condition}`.toLowerCase().includes(needle))
}))
const rcmOptions = computed(() => (data.value?.rcm ?? []).map(item => ({ label: `${item.id} · ${item.risk}`, value: item.id })))
const procedureOptions = computed(() => (data.value?.procedures ?? []).map(item => ({ label: `${item.id} · ${item.objective}`, value: item.id })))
const availableEvidence = computed(() => (data.value?.evidence_options ?? []).filter(
  option => !selected.value?.evidence_refs.some(item => item.id === option.anchor.id),
))

const severityTone: Record<string, string> = { critical: 'danger', high: 'danger', medium: 'warn', low: 'info', info: 'secondary' }

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function reload(preferred?: string) {
  data.value = await api.get<FindingsPayload>(`/api/workspaces/${props.workspace.id}/findings`)
  const requested = preferred || String(route.query.finding || '')
  if (requested && data.value.items.some(item => item.id === requested)) selectedId.value = requested
  else if (!selected.value) selectedId.value = data.value.items[0]?.id ?? null
}

onMounted(() => void reload().catch(error => fail('Could not load findings', error)))
watch(() => route.query.finding, value => {
  const id = String(value || '')
  if (id && data.value?.items.some(item => item.id === id)) selectedId.value = id
})
watch(selectedId, id => {
  if (id && route.query.finding !== id) void router.replace({ query: { ...route.query, tab: 'findings', finding: id } })
})

async function addManual() {
  try {
    const item = await api.post<AuditFinding>(`/api/workspaces/${props.workspace.id}/findings`, {
      title: 'New audit finding', severity: 'medium', status: 'draft',
    })
    await reload(item.id)
    emit('changed')
  } catch (error) { fail('Could not add the finding', error) }
}

async function save() {
  if (!selected.value) return
  saving.value = true
  try {
    const item = selected.value
    await api.patch(`/api/workspaces/${props.workspace.id}/findings/${item.id}`, {
      title: item.title, severity: item.severity, condition: item.condition,
      criteria: item.criteria, cause: item.cause, effect: item.effect,
      recommendation: item.recommendation, management_response: item.management_response,
      status: item.status, rcm_refs: item.rcm_refs, procedure_refs: item.procedure_refs,
      evidence_refs: item.evidence_refs,
    })
    await reload(item.id)
    emit('changed')
    toast.add({ severity: 'success', summary: 'Finding saved', life: 1800 })
  } catch (error) { fail('Could not save the finding', error) }
  finally { saving.value = false }
}

async function remove() {
  if (!selected.value) return
  try {
    await api.del(`/api/workspaces/${props.workspace.id}/findings/${selected.value.id}`)
    selectedId.value = null
    await reload()
    emit('changed')
  } catch (error) { fail('Could not remove the finding', error) }
}

function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }
function addEvidence(value: EvidenceRef) {
  if (selected.value && !selected.value.evidence_refs.some(item => item.id === value.id)) selected.value.evidence_refs.push(value)
}
function removeEvidence(id: string) {
  if (selected.value) selected.value.evidence_refs = selected.value.evidence_refs.filter(item => item.id !== id)
}
function openPlanning(kind: 'rcm' | 'procedure', id: string) {
  void router.replace({ query: { ...route.query, tab: 'planning', view: kind === 'procedure' ? 'program' : 'rcm', ...(kind === 'procedure' ? { procedure: id } : {}) } })
}
function openEvidence(value: EvidenceRef) {
  if (value.source_kind === 'doctest') {
    void router.replace({ query: { ...route.query, tab: 'doc-tests', test: value.source_id, ...(value.item_id ? { item: value.item_id } : {}) } })
  } else if (value.source_kind === 'analysis') {
    void router.replace({ query: { ...route.query, tab: 'analysis' } })
  } else if (value.source_kind === 'ruleset') {
    void router.replace({ query: { ...route.query, tab: 'validation' } })
  } else showAnchor(value)
}
</script>

<template>
  <div class="findings-tab">
    <header class="findings-head">
      <div><p class="eyebrow">Audit output</p><h2>Findings</h2><p class="muted">Develop evidence-linked observations into auditor-owned findings.</p></div>
      <Button label="Add manual finding" icon="pi pi-plus" size="small" @click="addManual" />
    </header>
    <div class="findings-layout">
      <aside class="finding-rail card">
        <div class="rail-filters"><InputText v-model="search" placeholder="Search findings" /><Select v-model="severityFilter" :options="severityOptions" /></div>
        <button v-for="item in filtered" :key="item.id" :class="{ active: item.id === selectedId }" @click="selectedId = item.id">
          <span class="rail-title"><strong>{{ item.id }}</strong><Tag :value="item.severity" :severity="severityTone[item.severity]" /></span>
          <span>{{ item.title }}</span><small>{{ item.status }} · {{ item.source }}</small>
        </button>
        <p v-if="!filtered.length" class="empty">No findings match this view.</p>
      </aside>

      <section v-if="selected" class="finding-detail card">
        <div class="detail-toolbar">
          <div class="provenance"><strong>{{ selected.id }}</strong><Tag :value="selected.status" :severity="selected.status === 'final' ? 'success' : 'warn'"/><Tag :value="selected.source" severity="secondary"/><span v-if="selected.agent_run_id" class="muted">Run {{ selected.agent_run_id }}</span></div>
          <span class="grow"/><Button icon="pi pi-trash" text severity="danger" @click="remove"/><Button label="Save finding" icon="pi pi-save" size="small" :loading="saving" @click="save"/>
        </div>
        <div class="top-fields"><label>Title<InputText v-model="selected.title" /></label><label>Severity<Select v-model="selected.severity" :options="severities" /></label><label>State<Select v-model="selected.status" :options="['draft','final']" /></label></div>
        <div class="iia-grid">
          <label>Condition<Textarea v-model="selected.condition" rows="4" autoResize placeholder="What was observed?" /></label>
          <label>Criteria<Textarea v-model="selected.criteria" rows="4" autoResize placeholder="What should have occurred?" /></label>
          <label>Cause<Textarea v-model="selected.cause" rows="4" autoResize placeholder="Why did it occur?" /></label>
          <label>Effect<Textarea v-model="selected.effect" rows="4" autoResize placeholder="What is the impact or exposure?" /></label>
          <label class="wide">Recommendation<Textarea v-model="selected.recommendation" rows="4" autoResize /></label>
          <label class="wide">Management response<Textarea v-model="selected.management_response" rows="4" autoResize /></label>
        </div>
        <div class="link-grid">
          <label>RCM links<MultiSelect v-model="selected.rcm_refs" :options="rcmOptions" optionLabel="label" optionValue="value" display="chip" filter placeholder="Select risks and controls" /></label>
          <label>Procedure links<MultiSelect v-model="selected.procedure_refs" :options="procedureOptions" optionLabel="label" optionValue="value" display="chip" filter placeholder="Select performed procedures" /></label>
        </div>
        <div class="source-links">
          <h3>Traceability and evidence</h3>
          <div class="chips"><button v-for="id in selected.rcm_refs" :key="`rcm:${id}`" @click="openPlanning('rcm', id)"><i class="pi pi-map"/> {{ id }}</button><button v-for="id in selected.procedure_refs" :key="`procedure:${id}`" @click="openPlanning('procedure', id)"><i class="pi pi-list-check"/> {{ id }}</button></div>
          <p v-if="!selected.evidence_refs.length" class="warning"><i class="pi pi-exclamation-triangle"/> No typed evidence is linked. Add evidence through a promoted agent observation or an evidence-enabled workflow before finalizing.</p>
          <div v-else class="evidence-list"><div v-for="value in selected.evidence_refs" :key="value.id"><button @click="openEvidence(value)"><i class="pi pi-link"/><span>{{ value.source_kind }}:{{ value.source_id }}<small v-if="value.page">page {{ value.page }}</small></span><code>{{ value.source_sha1?.slice(0, 10) }}</code></button><Button icon="pi pi-times" text rounded severity="danger" size="small" aria-label="Remove evidence link" @click="removeEvidence(value.id)"/></div></div>
          <details v-if="availableEvidence.length" class="evidence-picker"><summary>Add evidence already captured in fieldwork</summary><button v-for="option in availableEvidence" :key="option.anchor.id" @click="addEvidence(option.anchor)"><i class="pi pi-plus"/>{{ option.label }}</button></details>
        </div>
      </section>
      <section v-else class="empty-detail card"><i class="pi pi-flag"/><p>Select a finding or add a manual finding.</p></section>
    </div>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
  </div>
</template>

<style scoped>
.findings-tab { min-width:0 }.findings-head,.detail-toolbar,.provenance,.rail-title { display:flex; align-items:center }.findings-head { justify-content:space-between; gap:1rem; margin-bottom:1rem }.findings-head h2 { margin:.1rem 0 }.findings-layout { display:grid; grid-template-columns:18rem minmax(0,1fr); gap:1rem }.finding-rail { padding:.55rem; align-self:start; max-height:calc(100vh - 12rem); overflow:auto }.rail-filters { display:grid; grid-template-columns:1fr 6.5rem; gap:.4rem; padding:.2rem .15rem .6rem }.rail-filters :deep(.p-inputtext),.rail-filters :deep(.p-select) { width:100%; font-size:.78rem }.finding-rail > button { width:100%; display:flex; flex-direction:column; gap:.3rem; text-align:left; border:0; border-radius:var(--aw-radius-sm); background:transparent; padding:.7rem; color:var(--aw-ink); cursor:pointer }.finding-rail > button:hover,.finding-rail > button.active { background:var(--p-primary-50) }.rail-title { width:100%; justify-content:space-between }.finding-rail small { color:var(--aw-muted); text-transform:capitalize }.finding-detail { padding:1rem; min-width:0 }.detail-toolbar { gap:.45rem; padding-bottom:.8rem; border-bottom:1px solid var(--aw-border) }.provenance { gap:.45rem; flex-wrap:wrap }.grow { flex:1 }.top-fields { display:grid; grid-template-columns:minmax(0,1fr) 9rem 8rem; gap:.75rem; margin:1rem 0 }.iia-grid,.link-grid { display:grid; grid-template-columns:1fr 1fr; gap:.8rem }.iia-grid .wide { grid-column:1/-1 } label { display:flex; flex-direction:column; gap:.35rem; font-size:.76rem; font-weight:700; color:var(--aw-muted) } label :deep(.p-inputtext),label :deep(.p-textarea),label :deep(.p-select),label :deep(.p-multiselect) { width:100%; font-size:.82rem; color:var(--aw-ink); font-weight:400 }.link-grid { margin-top:.9rem }.source-links { margin-top:1rem; padding-top:.8rem; border-top:1px solid var(--aw-border) }.source-links h3 { margin:0 0 .6rem; font-size:.9rem }.chips,.evidence-list { display:flex; flex-wrap:wrap; gap:.4rem }.chips button,.evidence-list button { border:1px solid var(--aw-border); background:var(--aw-canvas); color:var(--aw-teal); border-radius:999px; padding:.3rem .6rem; cursor:pointer }.evidence-list { flex-direction:column }.evidence-list > div { display:flex; align-items:center; gap:.3rem }.evidence-list > div > button:first-child { flex:1; display:flex; align-items:center; text-align:left; border-radius:var(--aw-radius-sm); gap:.55rem }.evidence-list button span { display:flex; flex-direction:column; flex:1 }.evidence-list small,.evidence-list code { color:var(--aw-muted) }.evidence-picker { margin-top:.6rem; padding:.6rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm) }.evidence-picker summary { cursor:pointer; color:var(--aw-teal); font-weight:700; font-size:.78rem }.evidence-picker > button { display:block; width:100%; margin-top:.35rem; padding:.45rem; border:0; border-radius:var(--aw-radius-sm); background:var(--aw-canvas); color:var(--aw-ink); text-align:left; cursor:pointer }.warning { padding:.7rem; background:var(--p-orange-50); color:var(--p-orange-800); border-radius:var(--aw-radius-sm) }.empty,.empty-detail { color:var(--aw-muted); text-align:center }.empty-detail { min-height:20rem; display:grid; place-content:center }.empty-detail i { font-size:2rem }.muted { color:var(--aw-muted) }
@media (max-width:1050px) { .findings-layout { grid-template-columns:1fr }.finding-rail { max-height:16rem }.iia-grid,.link-grid { grid-template-columns:1fr }.iia-grid .wide { grid-column:auto } }
@media (max-width:700px) { .top-fields { grid-template-columns:1fr }.findings-head { align-items:flex-start; flex-direction:column }.detail-toolbar { flex-wrap:wrap } }
</style>
