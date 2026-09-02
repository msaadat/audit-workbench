<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { plural } from '../format'
import { useAgentRun } from '../composables/useAgentRun'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AuditReport, MarkdownTemplate, ReportContext, ReportQuality, ReportQualityIssue, WorkspaceSummary } from '../types'
import MarkdownEditor from './MarkdownEditor.vue'
import ReportReconcileDialog from './ReportReconcileDialog.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const nav = useWorkspaceNav()
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

async function copy() {
  await save(false)
  if (!report.value) return
  await navigator.clipboard.writeText(report.value.markdown)
  toast.add({ severity: 'success', summary: 'Markdown copied', life: 1600 })
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
  if (kind === 'finding') void nav.replace('findings', { finding: id })
  else if (kind === 'doctest') void nav.replace('doc-tests', { test: id })
  else if (kind === 'datatest') void nav.replace('data-tests', { test: id })
  else if (kind === 'observation') void nav.replace('rcm', { observation: id })
  else if (kind === 'rcm') void nav.replace('rcm', { rcm: id })
  else if (kind === 'analysis' || kind === 'ruleset') void nav.replace('data-tests')
}
const hasContent = computed(() => Boolean(report.value?.markdown?.trim()))
/** Open the editor on a report the auditor intends to write by hand. */
function startReport() {
  if (report.value) report.value.markdown = '# Internal audit report\n\n'
}

function allIssues(): ReportQualityIssue[] { return [...(report.value?.quality.issues ?? []), ...(report.value?.quality.editorial ?? [])] }

/**
 * Quality codes are enum identifiers. Title-casing them ("Stale Evidence",
 * "Finding Draft") made them legible but not meaningful — "Finding Draft" does
 * not name the problem. Anything unmapped still degrades to the humanised code.
 */
const ISSUE_HEADINGS: Record<string, string> = {
  broken_evidence: 'A finding cites evidence that cannot be resolved',
  broken_rcm_ref: 'A finding references an RCM row that no longer exists',
  broken_report_citation: 'The report cites a finding that no longer exists',
  broken_test_ref: 'A finding references a test that no longer exists',
  duplicate_finding: 'Two findings report nearly the same thing',
  editorial_unavailable: 'Editorial review could not run',
  finding_draft: 'A finding is still a draft',
  finding_missing_from_report: 'A confirmed finding is not cited in the report',
  missing_limitations: 'Recorded scope limitations are not disclosed',
  preliminary_label_missing: 'The report is not labelled as a preliminary draft',
  report_arithmetic: "The report's finding count does not match the register",
  report_empty: 'The report has not been drafted yet',
  report_rating_unsupported: 'The report asserts a rating nothing supports',
  report_risk_arithmetic: "The report's risk counts disagree with the RCM",
  stale_evidence: 'Evidence has changed since it was cited',
  unresolved_exception: 'An exception has no recorded disposition',
  unsupported_finding: 'A finding has no supporting test result',
}
function issueHeading(code: string): string {
  return ISSUE_HEADINGS[code] ?? code.replaceAll('_', ' ')
}

/**
 * The panel used to iterate every key of `context.statistics` and print the
 * value, which rendered `risk_distribution` — an object — as serialised JSON
 * under an auto-capitalised "Rcm Rows"-style label. Naming the figures worth
 * showing is the fix; a new backend key now needs a decision here rather than
 * appearing on screen unannounced.
 */
const REPORTED_STATISTICS: Array<{ key: string; label: string; tone?: 'warn' }> = [
  { key: 'rcm_rows', label: 'Risks in the matrix' },
  { key: 'tests', label: 'Tests run' },
  { key: 'findings', label: 'Findings confirmed' },
  { key: 'draft_findings', label: 'Findings still in draft', tone: 'warn' },
  { key: 'exceptions', label: 'Exceptions recorded', tone: 'warn' },
  { key: 'manual_review', label: 'Items awaiting review', tone: 'warn' },
]
function statisticValue(key: string): number {
  const value = context.value?.statistics?.[key]
  return typeof value === 'number' ? value : 0
}
// Zeros are suppressed unless the figure is one an auditor reads as a positive
// assurance — a report drawing on zero tests is worth stating.
const reportedStatistics = computed(() =>
  REPORTED_STATISTICS.filter(item => statisticValue(item.key) > 0 || !item.tone),
)

const RISK_BANDS = [
  { key: 'critical', label: 'critical', colour: 'var(--aw-danger)' },
  { key: 'high', label: 'high', colour: '#d97706' },
  { key: 'medium', label: 'medium', colour: 'var(--aw-low)' },
  { key: 'low', label: 'low', colour: 'var(--aw-ok)' },
]
const riskBands = computed(() => {
  const distribution = (context.value?.statistics?.risk_distribution ?? {}) as Record<string, number>
  const bands = RISK_BANDS
    .map(band => ({ ...band, count: Number(distribution[band.key] ?? 0) }))
    .filter(band => band.count > 0)
  const total = bands.reduce((sum, band) => sum + band.count, 0)
  return { bands: bands.map(band => ({ ...band, share: (band.count / total) * 100 })), total }
})
const secondaryActions = computed(() => [
  { label: 'Quality checks', icon: 'pi pi-check-circle', command: () => void runQuality(false) },
  { label: 'Edit report template', icon: 'pi pi-file-edit', command: () => void openTemplate() },
  { separator: true },
  { label: 'Copy Markdown', icon: 'pi pi-copy', command: () => void copy() },
])
</script>

<template>
  <div v-if="report" class="report-tab">
    <UiPageHeader title="Draft audit report">
        <Tag v-if="report.edited" value="auditor edited" severity="info"/>
        <Button v-if="view === 'editor' && hasContent" label="Save" icon="pi pi-save" size="small" outlined :loading="busy" @click="save(true)"/>
        <Button :label="report.generated_at ? 'Regenerate' : 'Generate report'" icon="pi pi-sparkles" size="small" :loading="busy" @click="generate"/>
        <UiOverflowMenu :items="secondaryActions" />
    </UiPageHeader>
    <div class="report-nav"><SelectButton v-model="view" :options="views" optionLabel="label" optionValue="value" :allowEmpty="false"/><span v-if="report.generated_at" class="muted generated-at">Generated {{ report.generated_at.slice(0,16).replace('T',' ') }}</span></div>

    <section v-if="view === 'editor'" class="editor-view">
      <!-- Before generation there is nothing to edit and nothing to save, so the
           section says what it will draw on instead of showing an empty editor
           under a redundant "Report editor / Save" band. -->
      <UiEmptyState
        v-if="!hasContent"
        icon="pi pi-file-edit"
        title="No report drafted yet"
        description="The draft is written from the audit file: the confirmed findings, the tests behind them, and the scope limitations recorded during fieldwork. Every figure it states is checked against the register."
      >
        <Button label="Generate report" icon="pi pi-sparkles" :loading="busy" @click="generate"/>
        <Button label="Write it myself" icon="pi pi-pencil" outlined @click="startReport"/>
      </UiEmptyState>
      <div v-else class="editor-pane card"><MarkdownEditor v-model="report.markdown" placeholder="Write the report, or generate a draft from the audit file."/></div>
    </section>
    <section v-else class="quality-view">
      <div class="quality-card card">
        <div class="quality-head"><div><strong>Deterministic checks</strong><p>Advisory only — warnings never disable editing, generation, or copy-out.</p></div><div class="quality-counts"><Tag :value="plural(report.quality.counts.error, 'error')" :severity="report.quality.counts.error ? 'danger' : 'success'"/><Tag :value="plural(report.quality.counts.warning, 'warning')" :severity="report.quality.counts.warning ? 'warn' : 'secondary'"/><Button label="Optional editorial review" icon="pi pi-sparkles" size="small" outlined :loading="busy" @click="runQuality(true)"/></div></div>
        <div v-if="allIssues().length" class="issue-list"><article v-for="(issue,index) in allIssues()" :key="`${issue.source}:${issue.code}:${index}`"><Tag :value="issue.severity" :severity="issueTone[issue.severity]"/><div><strong>{{ issueHeading(issue.code) }}</strong><p>{{ issue.message }}</p><span v-if="issue.source === 'editorial'" class="muted">Optional editorial suggestion</span><div v-if="issue.refs.length" class="refs"><button v-for="ref in issue.refs" :key="ref" @click="openIssueRef(ref)">{{ ref }}</button></div></div></article></div>
        <p v-else class="quality-ok"><i class="pi pi-check-circle"/> No quality issues were identified.</p>
      </div>
      <aside class="sources-card card">
        <h3>What this report draws on</h3>

        <template v-if="riskBands.total">
          <h4>Risk coverage</h4>
          <div class="risk-bar" role="img" :aria-label="`${plural(riskBands.total, 'risk')} in the matrix`">
            <i v-for="band in riskBands.bands" :key="band.key" :style="{ width: `${band.share}%`, background: band.colour }" />
          </div>
          <p class="risk-legend">
            <span v-for="band in riskBands.bands" :key="band.key"><s :style="{ background: band.colour }" />{{ band.count }} {{ band.label }}</span>
            <span class="risk-total">{{ plural(riskBands.total, 'risk') }} in total</span>
          </p>
        </template>

        <dl v-if="context" class="stats">
          <div v-for="item in reportedStatistics" :key="item.key">
            <dt>{{ item.label }}</dt>
            <dd :data-tone="item.tone && statisticValue(item.key) ? item.tone : undefined">{{ statisticValue(item.key).toLocaleString() }}</dd>
          </div>
        </dl>

        <h4>Traceability</h4>
        <div v-for="finding in context?.findings ?? []" :key="String(finding.id)" class="trace-source">
          <button type="button" @click="openIssueRef(`finding:${finding.id}`)">{{ finding.title }}</button>
          <!-- The identifiers behind this line are on the finding itself; here
               the point is what supports the conclusion, not its primary keys. -->
          <small>
            {{ plural((finding.rcm_refs as string[] ?? []).length, 'RCM row') }} ·
            {{ plural((finding.test_refs as string[] ?? []).length, 'test') }} ·
            {{ plural((finding.execution_refs as string[] ?? []).length, 'recorded result') }}
          </small>
        </div>
        <p v-if="!context?.findings.length" class="muted">No auditor-confirmed findings are available.</p>

        <h4>Scope limitations</h4>
        <p v-for="item in context?.scope_limitations ?? []" :key="item.test_id"><strong>{{ item.rcm_id }} / {{ item.test_id }}</strong> · {{ item.text }}</p>
        <p v-if="!context?.scope_limitations.length" class="muted">No limitations recorded.</p>
      </aside>
    </section>

    <ReportReconcileDialog v-model="reconcileOpen" :current="reconcileCurrent" :generated="reconcileGenerated" :busy="busy" @choose="reconcile"/>
    <Dialog v-model:visible="templateOpen" modal header="Report template" :style="{width:'min(900px,94vw)'}"><p class="muted">The template controls the generated deliverable structure. Section comments provide model instructions.</p><Textarea v-if="template" v-model="template.markdown" rows="22" spellcheck="false" class="template-editor"/><template #footer><Button label="Restore default" severity="secondary" text @click="saveTemplate(true)"/><Button label="Save override" icon="pi pi-save" @click="saveTemplate(false)"/></template></Dialog>
  </div>
</template>

<style scoped>
.report-tab { min-width:0 }.report-tab > .ui-page-header { margin-bottom:.6rem }.report-head,.report-actions,.report-nav,.quality-head,.quality-counts { display:flex; align-items:center }.report-head { justify-content:space-between; gap:1rem; margin-bottom:.8rem }.report-head h2 { margin:.1rem 0 }.report-actions { justify-content:flex-end; gap:.4rem; flex-wrap:wrap }.report-nav { gap:.4rem; margin-bottom:.6rem }.grow { flex:1 }.editor-view { min-height:34rem }.editor-pane { display:flex; flex-direction:column; padding:0; overflow:hidden; min-width:0 }.editor-pane :deep(.markdown-editor) { flex:1; min-height:32rem; border:0; border-radius:0 }.quality-view { display:grid; grid-template-columns:minmax(0,1fr) 20rem; gap: var(--aw-section-gap) }.quality-card,.sources-card { padding:1rem }.quality-head { justify-content:space-between; gap:1rem; border-bottom:1px solid var(--aw-border); padding-bottom:.75rem }.quality-head p { margin:.2rem 0 0; color:var(--aw-muted); font-size:var(--aw-text-sm) }.quality-counts { gap:.4rem; flex-wrap:wrap }.issue-list { display:flex; flex-direction:column; gap:.55rem; margin-top:.8rem }.issue-list article { display:flex; align-items:flex-start; gap:.6rem; padding:.7rem; border:1px solid var(--aw-border); border-radius:var(--aw-radius-control) }.issue-list article > div { flex:1 }.issue-list p { margin:.2rem 0 }.refs { display:flex; flex-wrap:wrap; gap:.3rem; margin-top:.35rem }.refs button { border:1px solid var(--aw-border); background:var(--aw-teal-soft); color:var(--aw-teal); border-radius:var(--aw-radius-pill); padding:.2rem .45rem; cursor:pointer }.quality-ok { color:var(--aw-ok); padding:1rem }.sources-card h3 { margin-top:0 }.sources-card h4 { margin:.9rem 0 .35rem }.sources-card a { display:block; color:var(--aw-teal); margin:.35rem 0; text-decoration:none }.stats { display:grid; grid-template-columns:repeat(2,1fr); gap:.4rem }.stats span { display:flex; flex-direction:column; padding:.55rem; background:var(--aw-canvas); border-radius:var(--aw-radius-control); font-size:var(--aw-text-xs); color:var(--aw-muted); text-transform:capitalize }.stats strong { color:var(--aw-ink); font-size:var(--aw-text-md) }.template-editor { width:100%; font-family:var(--aw-font-mono,monospace) }.muted { color:var(--aw-muted) }
.editor-view { min-height:34rem }
.generated-at { margin-left:auto }

/* Sources panel: named figures in a definition list rather than a grid of
   auto-labelled tiles, so a zero never competes with a number that matters. */
.sources-card .stats { display:grid; gap:0; margin:.6rem 0 0; grid-template-columns:none }
.sources-card .stats > div { display:flex; align-items:baseline; justify-content:space-between; gap:.75rem; padding:.3rem 0; border-bottom:1px solid var(--aw-border) }
.sources-card .stats > div:last-child { border-bottom:0 }
.sources-card .stats dt { color:var(--aw-muted); font-size:var(--aw-text-xs) }
.sources-card .stats dd { margin:0; font-family:var(--aw-font-mono); font-variant-numeric:tabular-nums; font-weight:600; color:var(--aw-ink) }
.sources-card .stats dd[data-tone='warn'] { color:var(--aw-warn) }

.risk-bar { display:flex; height:.5rem; margin:.35rem 0 .3rem; border-radius:var(--aw-radius-pill); overflow:hidden }
.risk-bar i { display:block }
.risk-legend { display:flex; flex-wrap:wrap; gap:.1rem .6rem; margin:0; color:var(--aw-muted); font-size:var(--aw-text-2xs) }
.risk-legend span { display:inline-flex; align-items:center; gap:.25rem }
.risk-legend s { width:.4rem; height:.4rem; border-radius:2px; text-decoration:none }
.risk-total { color:var(--aw-muted-strong) }

.trace-source { margin:.45rem 0 }
.trace-source button { display:block; padding:0; border:0; background:none; color:var(--aw-teal); font:inherit; font-size:var(--aw-text-sm); text-align:left; cursor:pointer }
.trace-source button:hover { text-decoration:underline }
.trace-source small { color:var(--aw-muted); font-size:var(--aw-text-2xs) }
@media (max-width:1050px) { .quality-view { grid-template-columns:1fr }.report-head { align-items:flex-start; flex-direction:column }.report-actions { justify-content:flex-start }.sources-card { order:-1 } }
@media (max-width:700px) { .report-nav { align-items:flex-start; flex-wrap:wrap }.report-nav .grow { display:none }.quality-head { align-items:flex-start; flex-direction:column } }
</style>
