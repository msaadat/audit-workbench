<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { AuditReport, MarkdownTemplate, ReportContext, ReportQuality, ReportQualityIssue, WorkspaceSummary } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import ReportReconcileDialog from './ReportReconcileDialog.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const router = useRouter()
const agent = useAgentRun(props.workspace.id)
const report = ref<AuditReport | null>(null)
const context = ref<ReportContext | null>(null)
const view = ref<'editor' | 'quality'>('editor')
const busy = ref(false)
const reconcileOpen = ref(false)
const reconcileCurrent = ref('')
const reconcileGenerated = ref('')
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)

const views = [
  { label: 'Editor', value: 'editor' },
  { label: 'Sources & quality', value: 'quality' },
]
const issueTone: Record<string, string> = { error: 'danger', warning: 'warn', info: 'info' }

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function reload() {
  [report.value, context.value] = await Promise.all([
    api.get<AuditReport>(`/api/workspaces/${props.workspace.id}/report`),
    api.get<ReportContext>(`/api/workspaces/${props.workspace.id}/report/context`),
  ])
}
onMounted(() => void reload().catch(error => fail('Could not load the report', error)))
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void reload().catch(error => fail('Could not refresh the report', error))
})
onUnmounted(unsubscribe)

async function save(showToast = true) {
  if (!report.value) return
  busy.value = true
  try {
    report.value = await api.patch<AuditReport>(`/api/workspaces/${props.workspace.id}/report`, {
      markdown: report.value.markdown,
    })
    emit('changed')
    if (showToast) toast.add({ severity: 'success', summary: 'Report saved', life: 1800 })
  } catch (error) { fail('Could not save the report', error) }
  finally { busy.value = false }
}

async function generate() {
  if (!report.value) return
  if (report.value.markdown) await save(false)
  busy.value = true
  try {
    const result = await api.post<AuditReport>(`/api/workspaces/${props.workspace.id}/report/generate`, { use_model: true })
    report.value = result
    context.value = await api.get<ReportContext>(`/api/workspaces/${props.workspace.id}/report/context`)
    emit('changed')
    if (result.requires_reconcile) {
      reconcileCurrent.value = result.current_markdown ?? result.markdown
      reconcileGenerated.value = result.candidate_markdown ?? result.generated_markdown
      reconcileOpen.value = true
    } else {
      view.value = 'editor'
      toast.add({ severity: 'success', summary: result.used_model ? 'Report draft generated' : 'Deterministic report draft generated', life: 2200 })
    }
  } catch (error) { fail('Could not generate the report', error) }
  finally { busy.value = false }
}

async function reconcile(action: 'keep' | 'replace') {
  busy.value = true
  try {
    report.value = await api.post<AuditReport>(`/api/workspaces/${props.workspace.id}/report/reconcile`, { action })
    reconcileOpen.value = false
    emit('changed')
    toast.add({ severity: 'success', summary: action === 'keep' ? 'Current report retained' : 'Generated report applied', life: 2200 })
  } catch (error) { fail('Could not reconcile the report', error) }
  finally { busy.value = false }
}

async function runQuality(editorial = false) {
  await save(false)
  busy.value = true
  try {
    const quality = await api.post<ReportQuality>(`/api/workspaces/${props.workspace.id}/report/quality`, { editorial })
    if (report.value) report.value.quality = quality
    view.value = 'quality'
  } catch (error) { fail('Could not run report quality checks', error) }
  finally { busy.value = false }
}

async function copy(kind: 'markdown' | 'html') {
  await save(false)
  if (!report.value) return
  await navigator.clipboard.writeText(kind === 'markdown' ? report.value.markdown : report.value.html)
  toast.add({ severity: 'success', summary: `${kind === 'markdown' ? 'Markdown' : 'HTML'} copied`, life: 1600 })
}

async function openTemplate() {
  try { template.value = await api.get(`/api/workspaces/${props.workspace.id}/templates/report`); templateOpen.value = true }
  catch (error) { fail('Could not load the report template', error) }
}
async function saveTemplate(reset = false) {
  if (!template.value) return
  try {
    template.value = await api.put(`/api/workspaces/${props.workspace.id}/templates/report`, reset ? { reset: true } : { markdown: template.value.markdown })
    toast.add({ severity: 'success', summary: reset ? 'Default report template restored' : 'Report template saved', life: 1800 })
  } catch (error) { fail('Could not save the report template', error) }
}

function openIssueRef(ref: string) {
  const [kind, id] = ref.split(':', 2)
  if (kind === 'finding') void router.replace({ query: { tab: 'findings', finding: id } })
  else if (kind === 'doctest') void router.replace({ query: { tab: 'doc-tests', test: id } })
  else if (kind === 'datatest') void router.replace({ query: { tab: 'data-tests', test: id } })
  else if (kind === 'observation') void router.replace({ query: { tab: 'rcm', observation: id } })
  else if (kind === 'rcm') void router.replace({ query: { tab: 'rcm', rcm: id } })
  else if (kind === 'analysis' || kind === 'ruleset') void router.replace({ query: { tab: 'data-tests' } })
}
function allIssues(): ReportQualityIssue[] { return [...(report.value?.quality.issues ?? []), ...(report.value?.quality.editorial ?? [])] }
const secondaryActions = computed(() => [
  { label: 'Quality checks', icon: 'pi pi-check-circle', command: () => void runQuality(false) },
  { label: 'Edit report template', icon: 'pi pi-file-edit', command: () => void openTemplate() },
  { separator: true },
  { label: 'Copy Markdown', icon: 'pi pi-copy', command: () => void copy('markdown') },
  { label: 'Copy HTML', icon: 'pi pi-copy', command: () => void copy('html') },
])
</script>

<template>
  <div v-if="report" class="report-tab">
    <UiPageHeader title="Draft audit report" description="Generate, edit, and review the engagement report">
        <Tag v-if="report.edited" value="auditor edited" severity="info"/>
        <Button :label="report.generated_at ? 'Regenerate' : 'Generate report'" icon="pi pi-sparkles" size="small" :loading="busy" @click="generate"/>
        <UiOverflowMenu :items="secondaryActions" />
    </UiPageHeader>
    <div v-if="report.generation_warnings.length" class="generation-warning"><i class="pi pi-info-circle"/><span>{{ report.generation_warnings.join(' ') }}</span></div>
    <div class="report-nav"><SelectButton v-model="view" :options="views" optionLabel="label" optionValue="value" :allowEmpty="false"/><span v-if="report.generated_at" class="muted generated-at">Generated {{ report.generated_at.slice(0,16).replace('T',' ') }}</span></div>

    <section v-if="view === 'editor'" class="editor-view">
      <div class="editor-pane card"><div class="pane-title"><strong>Report editor</strong><Button label="Save" icon="pi pi-save" size="small" :loading="busy" @click="save(true)"/></div><MarkdownEditor v-model="report.markdown" /></div>
    </section>
    <section v-else class="quality-view">
      <div class="quality-card card">
        <div class="quality-head"><div><strong>Deterministic checks</strong><p>Advisory only — warnings never disable editing, generation, or copy-out.</p></div><div class="quality-counts"><Tag :value="`${report.quality.counts.error} errors`" :severity="report.quality.counts.error ? 'danger' : 'success'"/><Tag :value="`${report.quality.counts.warning} warnings`" :severity="report.quality.counts.warning ? 'warn' : 'secondary'"/><Button label="Optional editorial review" icon="pi pi-sparkles" size="small" outlined :loading="busy" @click="runQuality(true)"/></div></div>
        <div v-if="allIssues().length" class="issue-list"><article v-for="(issue,index) in allIssues()" :key="`${issue.source}:${issue.code}:${index}`"><Tag :value="issue.severity" :severity="issueTone[issue.severity]"/><div><strong>{{ issue.code.replaceAll('_',' ') }}</strong><p>{{ issue.message }}</p><span v-if="issue.source === 'editorial'" class="muted">Optional editorial suggestion</span><div v-if="issue.refs.length" class="refs"><button v-for="ref in issue.refs" :key="ref" @click="openIssueRef(ref)">{{ ref }}</button></div></div></article></div>
        <p v-else class="quality-ok"><i class="pi pi-check-circle"/> No quality issues were identified.</p>
      </div>
      <aside class="sources-card card"><h3>Live report sources</h3><div v-if="context" class="stats"><span v-for="(value,key) in context.statistics" :key="key"><strong>{{ value }}</strong>{{ String(key).replaceAll('_',' ') }}</span></div><h4>Traceability</h4><div v-for="finding in context?.findings ?? []" :key="String(finding.id)" class="trace-source"><a :href="`?tab=findings&finding=${finding.id}`">{{ finding.id }} · {{ finding.title }}</a><small>RCM {{ (finding.rcm_refs as string[] ?? []).join(', ') }} → tests {{ (finding.test_refs as string[] ?? []).join(', ') }} → execution {{ (finding.execution_refs as string[] ?? []).join(', ') }}</small></div><p v-if="!context?.findings.length" class="muted">No auditor-confirmed findings are available.</p><h4>Scope limitations</h4><p v-for="item in context?.scope_limitations ?? []" :key="item.test_id"><strong>{{ item.rcm_id }} / {{ item.test_id }}</strong> · {{ item.text }}</p><p v-if="!context?.scope_limitations.length" class="muted">No limitations recorded.</p></aside>
    </section>

    <ReportReconcileDialog v-model="reconcileOpen" :current="reconcileCurrent" :generated="reconcileGenerated" :busy="busy" @choose="reconcile"/>
    <Dialog v-model:visible="templateOpen" modal header="Report template" :style="{width:'min(900px,94vw)'}"><p class="muted">The template controls the generated deliverable structure. Section comments provide model instructions.</p><Textarea v-if="template" v-model="template.markdown" rows="22" spellcheck="false" class="template-editor"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
  </div>
</template>

<style scoped>
.report-tab { min-width:0 }.report-head,.report-actions,.report-nav,.pane-title,.quality-head,.quality-counts { display:flex; align-items:center }.report-head { justify-content:space-between; gap:1rem; margin-bottom:.8rem }.report-head h2 { margin:.1rem 0 }.report-actions { justify-content:flex-end; gap:.4rem; flex-wrap:wrap }.generation-warning { display:flex; gap:.5rem; padding:.65rem .8rem; margin-bottom:.7rem; color:var(--p-blue-800); background:var(--p-blue-50); border-radius:var(--aw-radius-sm) }.report-nav { gap:.4rem; margin-bottom:.8rem }.grow { flex:1 }.editor-view { min-height:34rem }.editor-pane { display:flex; flex-direction:column; padding:1rem; min-width:0 }.pane-title { justify-content:space-between; padding-bottom:.65rem; border-bottom:1px solid var(--aw-border); margin-bottom:.7rem }.editor-pane :deep(.markdown-editor) { flex:1; min-height:32rem }.quality-view { display:grid; grid-template-columns:minmax(0,1fr) 20rem; gap:1rem }.quality-card,.sources-card { padding:1rem }.quality-head { justify-content:space-between; gap:1rem; border-bottom:1px solid var(--aw-border); padding-bottom:.75rem }.quality-head p { margin:.2rem 0 0; color:var(--aw-muted); font-size:.78rem }.quality-counts { gap:.4rem; flex-wrap:wrap }.issue-list { display:flex; flex-direction:column; gap:.55rem; margin-top:.8rem }.issue-list article { display:flex; align-items:flex-start; gap:.6rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-sm) }.issue-list article > div { flex:1 }.issue-list strong { text-transform:capitalize }.issue-list p { margin:.2rem 0 }.refs { display:flex; flex-wrap:wrap; gap:.3rem; margin-top:.35rem }.refs button { border:1px solid var(--aw-border); background:var(--p-primary-50); color:var(--aw-teal); border-radius:999px; padding:.2rem .45rem; cursor:pointer }.quality-ok { color:var(--p-green-700); padding:1rem }.sources-card h3 { margin-top:0 }.sources-card h4 { margin:.9rem 0 .35rem }.sources-card a { display:block; color:var(--aw-teal); margin:.35rem 0; text-decoration:none }.stats { display:grid; grid-template-columns:repeat(2,1fr); gap:.4rem }.stats span { display:flex; flex-direction:column; padding:.55rem; background:var(--aw-canvas); border-radius:var(--aw-radius-sm); font-size:.68rem; color:var(--aw-muted); text-transform:capitalize }.stats strong { color:var(--aw-ink); font-size:1rem }.template-editor { width:100%; font-family:var(--aw-font-mono,monospace) }.muted { color:var(--aw-muted) }
.editor-view { min-height:34rem }
.generated-at { margin-left:auto }
@media (max-width:1050px) { .quality-view { grid-template-columns:1fr }.report-head { align-items:flex-start; flex-direction:column }.report-actions { justify-content:flex-start }.sources-card { order:-1 } }
@media (max-width:700px) { .report-nav { align-items:flex-start; flex-wrap:wrap }.report-nav .grow { display:none }.quality-head { align-items:flex-start; flex-direction:column } }
</style>
