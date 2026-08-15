<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AuditFinding, EvidenceRef, FindingsPayload, FindingSeverity, WorkspaceSummary } from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import MarkdownEditor from './MarkdownEditor.vue'
import UiAdvancedSection from './ui/UiAdvancedSection.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiPageHeader from './ui/UiPageHeader.vue'
import UiStatusLanes from './ui/UiStatusLanes.vue'
import type { StatusAction } from './ui/statusLanes'
import { FINDINGS_FILTER_LABELS, filterFindings, findingsStatus } from './findings/findingsStatus'
import type { FindingsActionKey, FindingsFilter } from './findings/findingsStatus'
import { plural } from '../format'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const route = useRoute()
const nav = useWorkspaceNav()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)
const { isActive, launchMode } = agent
const assistantChat = useAssistantChat(props.workspace.id)

const data = ref<FindingsPayload | null>(null)
const selectedId = ref<string | null>(String(route.query.finding || '') || null)
const saving = ref(false)
const confirmingAll = ref(false)
const generatingFindings = ref(false)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const search = ref('')
const severityFilter = ref<string>('all')
// What the status bar has asked the rail to show. A view over the same list the
// lanes counted, so the counts above and the rail below cannot disagree.
const statusFilter = ref<FindingsFilter | null>(null)
const template = ref<{ markdown: string; source: string } | null>(null)
const templateOpen = ref(false)

const severities: FindingSeverity[] = ['critical', 'high', 'medium', 'low', 'info']
const severityOptions = ['all', ...severities]
const selected = computed(() => data.value?.items.find(item => item.id === selectedId.value) ?? null)
const filtered = computed(() => filterFindings(data.value?.items ?? [], statusFilter.value).filter(item => {
  const matchesSeverity = severityFilter.value === 'all' || item.severity === severityFilter.value
  const needle = search.value.trim().toLowerCase()
  return matchesSeverity && (!needle || `${item.id} ${item.title} ${item.narrative}`.toLowerCase().includes(needle))
}))
// The lanes count the whole file, not the filtered rail: a count that shrank as
// you filtered by it could never be clicked back out of.
const status = computed(() => findingsStatus(data.value?.items ?? []))
const statusFilterLabel = computed(() => (statusFilter.value ? FINDINGS_FILTER_LABELS[statusFilter.value] : ''))
const rcmOptions = computed(() => (data.value?.rcm ?? []).map(item => ({ label: `${item.id} · ${item.risk}`, value: item.id })))
const testOptions = computed(() => [
  ...(data.value?.data_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: item.id })),
  ...(data.value?.document_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: item.id })),
])
const executionOptions = computed(() => [
  ...(data.value?.data_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: `datatest:${item.id}${item.last_run ? `:${item.last_run.id}` : ''}` })),
  ...(data.value?.document_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: `doctest:${item.id}` })),
])
const availableEvidence = computed(() => (data.value?.evidence_options ?? []).filter(
  option => !selected.value?.evidence_refs.some(item => item.id === option.anchor.id),
))
const unconfirmed = computed(() => (data.value?.items ?? []).filter(item => !item.auditor_confirmed))
const agentBusy = computed(() => isActive.value || !agent.state.status?.configured)

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
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void reload().catch(error => fail('Could not refresh findings', error))
})
onUnmounted(unsubscribe)
watch(() => route.query.finding, value => {
  const id = String(value || '')
  if (id && data.value?.items.some(item => item.id === id)) selectedId.value = id
})
watch(selectedId, id => {
  if (id && route.query.finding !== id) void nav.replace('findings', { finding: id })
})

async function addManual() {
  try {
    const item = await api.post<AuditFinding>(`/api/workspaces/${props.workspace.id}/findings`, {
      title: 'New audit finding', severity: 'medium',
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
    const saved = await api.patch<AuditFinding>(`/api/workspaces/${props.workspace.id}/findings/${item.id}`, {
      title: item.title, severity: item.severity, narrative: item.narrative,
      management_response: item.management_response,
      rcm_refs: item.rcm_refs, test_refs: item.test_refs,
      execution_refs: item.execution_refs, cause_pending: item.cause_pending,
      auditor_confirmed: item.auditor_confirmed,
      evidence_refs: item.evidence_refs,
    })
    await reload(item.id)
    emit('changed')
    if (saved.evidence_warnings?.length) {
      toast.add({ severity: 'warn', summary: 'Finding saved with evidence warning', detail: saved.evidence_warnings.join(' '), life: 7000 })
    } else {
      toast.add({ severity: 'success', summary: 'Finding saved', life: 1800 })
    }
  } catch (error) { fail('Could not save the finding', error) }
  finally { saving.value = false }
}

function remove() {
  const item = selected.value
  if (!item) return
  confirm.require({
    header: 'Remove finding',
    message: `Remove "${item.id} — ${item.title}"? This cannot be undone.`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/findings/${item.id}`)
        selectedId.value = null
        await reload()
        emit('changed')
        toast.add({ severity: 'success', summary: 'Finding removed', life: 1800 })
      } catch (error) { fail('Could not remove the finding', error) }
    },
  })
}

/**
 * The bar names what it wants done; the tab still owns how. Both actions
 * already existed as header buttons — the lane that counts the gap is simply a
 * better place to offer them than a row that is there whether or not it applies.
 */
function runStatusAction(action: StatusAction) {
  switch (action.key as FindingsActionKey) {
    case 'generate_findings': return void generateAllFindings()
    case 'confirm_all': return confirmAll()
  }
}

function confirmAll() {
  const targets = unconfirmed.value
  if (!targets.length) return
  confirm.require({
    header: 'Confirm all findings',
    message: `Mark ${plural(targets.length, 'finding')} as auditor confirmed for formal reporting? Findings missing a complete narrative, required links, or evidence will be skipped.`,
    icon: 'pi pi-check-square',
    acceptProps: { label: `Confirm ${targets.length}` },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      confirmingAll.value = true
      let confirmed = 0
      const skipped: string[] = []
      try {
        for (const item of targets) {
          try {
            await api.patch<AuditFinding>(`/api/workspaces/${props.workspace.id}/findings/${item.id}`, { auditor_confirmed: true })
            confirmed += 1
          } catch (error) {
            skipped.push(`${item.id}: ${error instanceof ApiError ? error.message : String(error)}`)
          }
        }
        await reload(selectedId.value ?? undefined)
        emit('changed')
        if (confirmed) toast.add({ severity: 'success', summary: `${plural(confirmed, 'finding')} confirmed`, life: 2500 })
        if (skipped.length) {
          toast.add({ severity: 'warn', summary: `${plural(skipped.length, 'finding')} could not be confirmed`, detail: skipped.join(' · '), life: 9000 })
        }
      } finally { confirmingAll.value = false }
    },
  })
}

async function generateAllFindings() {
  generatingFindings.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Draft all eligible findings from the RCM observations.',
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button' },
    )
    if (!agent.state.drawerOpen) agent.toggleDrawer()
    toast.add({
      severity: 'success',
      summary: 'Generating all eligible findings',
      detail: 'Exception observations are used directly for finding drafts.',
      life: 4000,
    })
  } catch (error) { fail('Could not start finding generation', error) }
  finally { generatingFindings.value = false }
}

async function openTemplate() {
  try { template.value = await api.get(`/api/workspaces/${props.workspace.id}/templates/finding`); templateOpen.value = true }
  catch (error) { fail('Could not load the finding template', error) }
}
async function saveTemplate(reset = false) {
  if (!template.value) return
  try {
    template.value = await api.put(`/api/workspaces/${props.workspace.id}/templates/finding`, reset ? { reset: true } : { markdown: template.value.markdown })
    await reload(selectedId.value ?? undefined)
    toast.add({ severity: 'success', summary: reset ? 'Default finding template restored' : 'Finding template saved', life: 1800 })
  } catch (error) { fail('Could not save the finding template', error) }
}

function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }
function addEvidence(value: EvidenceRef) {
  if (selected.value && !selected.value.evidence_refs.some(item => item.id === value.id)) selected.value.evidence_refs.push(value)
}
function removeEvidence(id: string) {
  if (selected.value) selected.value.evidence_refs = selected.value.evidence_refs.filter(item => item.id !== id)
}
function openPlanning(rcmId: string) {
  void nav.replace('rcm', { rcm: rcmId })
}
function openEvidence(value: EvidenceRef) {
  if (value.source_kind === 'doctest') {
    void nav.replace('doc-tests', { test: value.source_id, item: value.item_id })
  } else if (value.source_kind === 'datatest') {
    void nav.replace('data-tests', { test: value.source_id.split(':')[0] })
  } else if (value.source_kind === 'analysis' || value.source_kind === 'ruleset') {
    void nav.replace('data-tests')
  } else showAnchor(value)
}
</script>

<template>
  <div class="findings-tab">
    <UiPageHeader title="Findings">
      <Button label="Finding template" icon="pi pi-file-edit" severity="secondary" outlined size="small" @click="openTemplate" />
      <Button label="Add manual finding" icon="pi pi-plus" size="small" @click="addManual" />
    </UiPageHeader>
    <UiStatusLanes
      v-if="data"
      :lanes="status.lanes"
      :disclosures="status.disclosures"
      :filter="statusFilter"
      :filterLabel="statusFilterLabel"
      :busy="generatingFindings || confirmingAll"
      :canRunAgent="!agentBusy"
      @filter="statusFilter = ($event as FindingsFilter | null)"
      @action="runStatusAction"
    />
    <div v-if="data?.items.length" class="findings-layout">
      <aside class="finding-rail card">
        <div class="rail-filters"><InputText v-model="search" placeholder="Search findings" /><Select v-model="severityFilter" :options="severityOptions" /></div>
        <button v-for="item in filtered" :key="item.id" :class="{ active: item.id === selectedId }" @click="selectedId = item.id">
          <span class="rail-title"><strong>{{ item.id }}</strong><Tag :value="item.severity" :severity="severityTone[item.severity]" /></span>
          <span>{{ item.title }}</span><small>{{ item.source }}</small>
        </button>
        <p v-if="!filtered.length" class="empty">No findings match this view.</p>
      </aside>

      <section v-if="selected" class="finding-detail card">
        <div class="detail-toolbar">
          <div class="provenance"><strong>{{ selected.id }}</strong><Tag :value="selected.source" severity="secondary"/><span v-if="selected.agent_run_id" class="muted">Run {{ selected.agent_run_id }}</span></div>
          <span class="grow"/><Button label="Remove" icon="pi pi-trash" severity="danger" outlined size="small" @click="remove"/><Button label="Save finding" icon="pi pi-save" size="small" :loading="saving" @click="save"/>
        </div>
        <h3 class="form-section-title">Finding</h3>
        <div class="top-fields"><label>Title<InputText v-model="selected.title" /></label><label>Severity<Select v-model="selected.severity" :options="severities" /></label></div>
        <p class="narrative-hint">This narrative is copied into the report unchanged, so write it as final report text. Its sections come from the <button type="button" class="linkish" @click="openTemplate">finding template</button>.</p>
        <div class="narrative-editor"><MarkdownEditor v-model="selected.narrative" /></div>
        <div class="narrative-flags"><label><input v-model="selected.cause_pending" type="checkbox"/> Root cause pending auditor follow-up</label><label><input v-model="selected.auditor_confirmed" type="checkbox"/> Auditor confirmed for formal reporting</label></div>
        <h3 class="form-section-title">Management response</h3>
        <Textarea v-model="selected.management_response" rows="4" autoResize class="response-editor" placeholder="Management's own response, recorded as received." />
        <div class="link-grid">
          <label>RCM links<MultiSelect v-model="selected.rcm_refs" :options="rcmOptions" optionLabel="label" optionValue="value" display="chip" filter placeholder="Select risks and controls" /></label>
          <label>Test links<MultiSelect v-model="selected.test_refs" :options="testOptions" optionLabel="label" optionValue="value" display="chip" filter placeholder="Select tests" /></label>
          <label class="wide">Execution sources<MultiSelect v-model="selected.execution_refs" :options="executionOptions" optionLabel="label" optionValue="value" display="chip" filter placeholder="Select durable execution results" /></label>
        </div>
        <UiAdvancedSection title="Evidence and traceability" :description="`${plural(selected.evidence_refs.length, 'evidence link')}`" :open="!selected.evidence_refs.length">
        <div class="source-links">
          <h3>Traceability and evidence</h3>
          <div class="chips"><button v-for="id in selected.rcm_refs" :key="`rcm:${id}`" @click="openPlanning(id)"><i class="pi pi-map"/> {{ id }}</button><button v-for="id in selected.test_refs" :key="`test:${id}`" @click="openPlanning(selected.rcm_refs[0] || '')"><i class="pi pi-list-check"/> {{ id }}</button></div>
          <p v-if="!selected.evidence_refs.length" class="warning"><i class="pi pi-exclamation-triangle"/> No typed evidence is linked. Add evidence through a promoted agent observation or an evidence-enabled workflow.</p>
          <p v-for="warning in selected.evidence_warnings" :key="warning" class="warning"><i class="pi pi-exclamation-triangle"/> {{ warning }}</p>
          <div v-if="selected.evidence_refs.length" class="evidence-list"><div v-for="value in selected.evidence_refs" :key="value.id"><button @click="openEvidence(value)"><i class="pi pi-link"/><span>{{ value.source_kind }}:{{ value.source_id }}<small v-if="value.page">page {{ value.page }}</small></span><code>{{ value.source_sha1?.slice(0, 10) }}</code></button><Button icon="pi pi-times" text rounded severity="danger" size="small" aria-label="Remove evidence link" @click="removeEvidence(value.id)"/></div></div>
          <details v-if="availableEvidence.length" class="evidence-picker"><summary>Add evidence already captured in fieldwork</summary><button v-for="option in availableEvidence" :key="option.anchor.id" @click="addEvidence(option.anchor)"><i class="pi pi-plus"/>{{ option.label }}</button></details>
        </div>
        </UiAdvancedSection>
        <UiAdvancedSection title="Finding administration" description="Source, run provenance, and deletion">
          <div class="admin-row"><span>Source: {{ selected.source }}</span><span v-if="selected.agent_run_id">Agent run: {{ selected.agent_run_id }}</span></div>
        </UiAdvancedSection>
      </section>
      <UiEmptyState v-else icon="pi pi-flag" title="No finding selected" description="Select a finding or add a manual finding." />
    </div>
    <UiEmptyState v-else icon="pi pi-flag" title="Start the findings register" description="Add a finding when fieldwork identifies a reportable issue.">
      <Button label="Add manual finding" icon="pi pi-plus" @click="addManual" />
    </UiEmptyState>
    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
    <Dialog v-model:visible="templateOpen" modal header="Finding template" :style="{width:'min(900px,94vw)'}">
      <p class="muted">The <code>##</code> headings below are the sections every finding must complete before it can be confirmed for formal reporting. Comments are guidance for the auditor and the agent, and never appear in a finding or the report.</p>
      <Textarea v-if="template" v-model="template.markdown" rows="22" spellcheck="false" class="template-editor"/>
      <template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template>
    </Dialog>
  </div>
</template>

<style scoped>
.findings-tab { display:flex; flex-direction:column; gap:var(--aw-section-gap); min-width:0 }.findings-head,.detail-toolbar,.provenance,.rail-title { display:flex; align-items:center }.findings-head { justify-content:space-between; gap:1rem; margin-bottom:1rem }.findings-head h2 { margin:.1rem 0 }.findings-layout { display:grid; grid-template-columns:18rem minmax(0,1fr); gap: var(--aw-section-gap) }.finding-rail { padding:.55rem; align-self:start; max-height:42rem; overflow:auto }.rail-filters { display:grid; grid-template-columns:1fr 6.5rem; gap:.4rem; padding:.2rem .15rem .6rem }.rail-filters :deep(.p-inputtext),.rail-filters :deep(.p-select) { width:100%; font-size:var(--aw-text-sm) }.finding-rail > button { width:100%; display:flex; flex-direction:column; gap:.3rem; text-align:left; border:0; border-radius:var(--aw-radius-control); background:transparent; padding:.7rem; color:var(--aw-ink); cursor:pointer }.finding-rail > button:hover,.finding-rail > button.active { background:var(--aw-teal-soft) }.rail-title { width:100%; justify-content:space-between }.finding-rail small { color:var(--aw-muted); text-transform:capitalize }.finding-detail { padding:1rem; min-width:0 }.detail-toolbar { gap:.45rem; padding-bottom:.8rem; border-bottom:1px solid var(--aw-border) }.provenance { gap:.45rem; flex-wrap:wrap }.grow { flex:1 }.top-fields { display:grid; grid-template-columns:minmax(0,1fr) 9rem 8rem; gap:.75rem; margin:1rem 0 }.link-grid { display:grid; grid-template-columns:1fr 1fr; gap:.8rem }.link-grid .wide { grid-column:1/-1 } label { display:flex; flex-direction:column; gap:.35rem; font-size:var(--aw-text-sm); font-weight:700; color:var(--aw-muted) } label :deep(.p-inputtext),label :deep(.p-textarea),label :deep(.p-select),label :deep(.p-multiselect) { width:100%; font-size:var(--aw-text-sm); color:var(--aw-ink); font-weight:400 }.link-grid { margin-top:.9rem }.source-links { margin-top:1rem; padding-top:.8rem; border-top:1px solid var(--aw-border) }.source-links h3 { margin:0 0 .6rem; font-size:var(--aw-text-base) }.chips,.evidence-list { display:flex; flex-wrap:wrap; gap:.4rem }.chips button,.evidence-list button { border:1px solid var(--aw-border); background:var(--aw-canvas); color:var(--aw-teal); border-radius:var(--aw-radius-pill); padding:.3rem .6rem; cursor:pointer }.evidence-list { flex-direction:column }.evidence-list > div { display:flex; align-items:center; gap:.3rem }.evidence-list > div > button:first-child { flex:1; display:flex; align-items:center; text-align:left; border-radius:var(--aw-radius-control); gap:.55rem }.evidence-list button span { display:flex; flex-direction:column; flex:1 }.evidence-list small,.evidence-list code { color:var(--aw-muted) }.evidence-picker { margin-top:.6rem; padding:.6rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }.evidence-picker summary { cursor:pointer; color:var(--aw-teal); font-weight:700; font-size:var(--aw-text-sm) }.evidence-picker > button { display:block; width:100%; margin-top:.35rem; padding:.45rem; border:0; border-radius:var(--aw-radius-control); background:var(--aw-canvas); color:var(--aw-ink); text-align:left; cursor:pointer }.warning { padding:.7rem; background:var(--aw-warn-soft); color:var(--aw-warn-ink); border-radius:var(--aw-radius-control) }.empty,.empty-detail { color:var(--aw-muted); text-align:center }.empty-detail { min-height:20rem; display:grid; place-content:center }.empty-detail i { font-size:var(--aw-text-3xl) }.muted { color:var(--aw-muted) }
.findings-layout { grid-template-columns:17rem minmax(0,1fr) }
.finding-rail { max-height:42rem }
.detail-toolbar { position:sticky; top:-1rem; z-index:3; margin:-1rem -1rem .8rem; padding:.65rem 1rem; background:var(--aw-panel) }
.top-fields { margin:.5rem 0 .8rem }
.source-links { margin:0; padding:0; border:0 }
.form-section-title { margin:.7rem 0 .5rem; color:var(--aw-muted); font-size:var(--aw-text-xs);}
.narrative-hint { margin:.2rem 0 .6rem; color:var(--aw-muted); font-size:var(--aw-text-sm) }
.linkish { border:0; background:none; padding:0; color:var(--aw-teal); font:inherit; text-decoration:underline; cursor:pointer }
.narrative-editor { min-height:26rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }
.narrative-editor :deep(.markdown-editor) { min-height:26rem }
.narrative-flags { display:flex; gap:1.2rem; flex-wrap:wrap; margin:.7rem 0 .2rem }
.narrative-flags label { flex-direction:row; align-items:center; gap:.4rem; font-weight:400; color:var(--aw-ink); font-size:var(--aw-text-sm) }
.response-editor { width:100%; font-size:var(--aw-text-sm) }
.template-editor { width:100%; font-family:var(--aw-font-mono,monospace) }
.admin-row { display:flex; align-items:center; gap:.7rem; flex-wrap:wrap; color:var(--aw-muted); font-size:var(--aw-text-sm) }.admin-row .p-button { margin-left:auto }
@media (max-width:1050px) { .findings-layout { grid-template-columns:1fr }.finding-rail { max-height:16rem }.link-grid { grid-template-columns:1fr }.link-grid .wide { grid-column:auto } }
@media (max-width:700px) { .top-fields { grid-template-columns:1fr }.findings-head { align-items:flex-start; flex-direction:column }.detail-toolbar { flex-wrap:wrap } }
</style>
