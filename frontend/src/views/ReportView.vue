<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { plural } from '../format'
import { useAgentRun } from '../composables/useAgentRun'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  AuditFinding, AuditReport, FindingsPayload, MarkdownTemplate,
  ReportContext, ReportQualityIssue, WorkspaceSummary,
} from '../types'
import MarkdownEditor from '../components/MarkdownEditor.vue'
import UiDocumentPage from '../components/ui/UiDocumentPage.vue'
import UiEmptyState from '../components/ui/UiEmptyState.vue'
import UiMarkdownDocument from '../components/ui/UiMarkdownDocument.vue'
import UiOverflowMenu from '../components/ui/UiOverflowMenu.vue'
import { markdownOutline } from '../components/ui/markdownOutline'
import { openItems } from '../components/findings/findingsStatus'
import {
  collapseFindings, issueHeading, outlineMarks, placeIssues, reportIssues,
  stamp, staleSentence,
} from '../components/report/reportStatus'

/**
 * The draft report: the document, what is wrong with it, and what it drew on.
 *
 * It used to be a `SelectButton` with two positions — `Editor` and
 * `Sources & quality` — so reading the report and knowing whether it could be
 * relied on were different screens, and the fifty-six things the check found
 * were a column of cards none of which said where in the document to look.
 * Here the document is the page, the problems are attached to the headings
 * they are about, and the rail counts them the way a reader has to act on
 * them: two edits, and eighteen findings fieldwork has to finish.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)

const report = ref<AuditReport | null>(null)
const context = ref<ReportContext | null>(null)
const findings = ref<AuditFinding[]>([])
const busy = ref(false)
const editing = ref(false)
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)
/** Set when `generate` came back with a draft that would overwrite an edit. */
const reconcile = ref<{ current: string; generated: string } | null>(null)

const markdown = computed(() => report.value?.markdown ?? '')
const hasContent = computed(() => Boolean(markdown.value.trim()))
const entries = computed(() => markdownOutline(markdown.value))
/** What the outline lists; the document itself still renders every heading. */
const outlineEntries = computed(() => collapseFindings(entries.value))
/** The document's own title, which is where a fact about the whole draft goes. */
const titleAnchor = computed(() => entries.value[0]?.id ?? '')
const issues = computed(() => reportIssues(report.value))
const placed = computed(() => placeIssues(entries.value, issues.value.aboutReport))
const marks = computed(() => outlineMarks(placed.value.bySection))
const stale = computed(() => staleSentence(context.value, issues.value))
const badges = computed(() => {
  const excluded = context.value?.draft_findings_excluded?.length ?? 0
  if (!excluded) return {}
  const section = entries.value.find(entry => /detailed finding/i.test(entry.text))
  return section ? { [section.id]: 'excluded' } : {}
})

/**
 * The findings the report cannot carry, one row each rather than one per issue.
 *
 * Fifty-four issues over eighteen findings is three sentences about each; the
 * words come from `openItems`, which is what the register's own rows say, so
 * the two pages cannot describe the same finding differently.
 */
const blockedFindings = computed(() => [...issues.value.byFinding.keys()].map(id => {
  const record = findings.value.find(item => item.id === id)
  return {
    id,
    title: record?.title ?? '',
    owed: record ? openItems(record).map(item => item.short) : [],
  }
}))

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function reload() {
  const [next, ctx, register] = await Promise.all([
    api.get<AuditReport>(`/api/workspaces/${props.workspace.id}/report`),
    api.get<ReportContext>(`/api/workspaces/${props.workspace.id}/report/context`),
    api.get<FindingsPayload>(`/api/workspaces/${props.workspace.id}/findings`),
  ])
  report.value = next
  context.value = ctx
  findings.value = register.items
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

async function doneEditing() {
  editing.value = false
  await save()
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
      // Two drafts of one document is a state of this page, not a modal over
      // it: choosing between them means reading both, and a dialog holding two
      // 24-row textareas is the one shape that cannot be read in.
      reconcile.value = {
        current: result.current_markdown ?? result.markdown,
        generated: result.candidate_markdown ?? result.generated_markdown,
      }
    } else {
      editing.value = false
      toast.add({ severity: 'success', summary: result.used_model ? 'Report draft generated' : 'Deterministic report draft generated', life: 2200 })
    }
  } catch (error) { fail('Could not generate the report', error) }
  finally { busy.value = false }
}

async function settle(action: 'keep' | 'replace') {
  busy.value = true
  try {
    report.value = await api.post<AuditReport>(`/api/workspaces/${props.workspace.id}/report/reconcile`, { action })
    reconcile.value = null
    await reload()
    emit('changed')
    toast.add({ severity: 'success', summary: action === 'keep' ? 'Current report retained' : 'Generated report applied', life: 2200 })
  } catch (error) { fail('Could not reconcile the report', error) }
  finally { busy.value = false }
}

async function check(editorial = false) {
  if (hasContent.value) await save(false)
  busy.value = true
  try {
    const quality = await api.post(`/api/workspaces/${props.workspace.id}/report/quality`, { editorial })
    if (report.value) report.value.quality = quality as AuditReport['quality']
  } catch (error) { fail('Could not run report quality checks', error) }
  finally { busy.value = false }
}

/**
 * Write the label the generator would have written, rather than telling the
 * auditor to. The wording is the backend's `_ensure_preliminary_label`.
 */
async function addPreliminaryLabel() {
  if (!report.value) return
  const banner = '> **Preliminary working draft:** fieldwork, evidence, review, or auditor '
    + 'judgment remains open. This document is not a final audit opinion.'
  const body = report.value.markdown.replace(/^#\s+.*$/m, '# Preliminary Internal Audit Working Draft')
  const first = body.indexOf('\n')
  report.value.markdown = first < 0
    ? `${body}\n\n${banner}\n`
    : `${body.slice(0, first)}\n\n${banner}${body.slice(first)}`
  await save(false)
  await check(false)
}

async function copy() {
  if (hasContent.value) await save(false)
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

function openRef(ref: string) {
  const [kind, id] = ref.split(':', 2)
  if (kind === 'finding') void nav.push('findings', { finding: id })
  else if (kind === 'doctest') void nav.push('doc-tests', { test: id })
  else if (kind === 'datatest' || kind === 'test') void nav.push('data-tests', { test: id })
  else if (kind === 'observation') void nav.push('rcm', { observation: id })
  else if (kind === 'rcm') void nav.push('rcm', { rcm: id })
  else if (kind === 'analysis' || kind === 'ruleset') void nav.push('data-tests')
}

/** Open the editor on a report the auditor intends to write by hand. */
function startReport() {
  if (report.value) report.value.markdown = '# Internal audit report\n\n'
  editing.value = true
}

const menuItems = computed(() => [
  { label: 'Editorial review', icon: 'pi pi-sparkles', disabled: busy.value, command: () => void check(true) },
  { label: 'Report template', icon: 'pi pi-file-edit', command: () => void openTemplate() },
  { label: 'Copy Markdown', icon: 'pi pi-copy', disabled: !hasContent.value, command: () => void copy() },
])

const REPORTED_STATISTICS: Array<{ key: string; label: string; tone?: 'warn' }> = [
  { key: 'rcm_rows', label: 'Risks in the matrix' },
  { key: 'tests', label: 'Tests run' },
  { key: 'findings', label: 'Findings included' },
  { key: 'draft_findings', label: 'Excluded until supported', tone: 'warn' },
  { key: 'exceptions', label: 'Exceptions recorded', tone: 'warn' },
  { key: 'manual_review', label: 'Items awaiting review', tone: 'warn' },
]
function statistic(key: string): number {
  const value = context.value?.statistics?.[key]
  return typeof value === 'number' ? value : 0
}
const statistics = computed(() => REPORTED_STATISTICS.filter(item => statistic(item.key) > 0 || !item.tone))

const RISK_BANDS = [
  { key: 'critical', label: 'critical', colour: 'var(--aw-danger)' },
  { key: 'high', label: 'high', colour: 'var(--aw-danger-ink)' },
  { key: 'medium', label: 'medium', colour: 'var(--aw-low)' },
  { key: 'low', label: 'low', colour: 'var(--aw-ok)' },
]
const riskBands = computed(() => {
  const distribution = (context.value?.statistics?.risk_distribution ?? {}) as unknown as Record<string, number>
  const bands = RISK_BANDS
    .map(band => ({ ...band, count: Number(distribution[band.key] ?? 0) }))
    .filter(band => band.count > 0)
  const total = bands.reduce((sum, band) => sum + band.count, 0)
  return { bands: bands.map(band => ({ ...band, share: (band.count / total) * 100 })), total }
})

/** The action a strip offers, where the page can actually take it. */
function stripAction(issue: ReportQualityIssue): string {
  return issue.code === 'preliminary_label_missing' ? 'Add the label' : ''
}
function runStrip(issue: ReportQualityIssue) {
  if (issue.code === 'preliminary_label_missing') void addPreliminaryLabel()
}
</script>

<template>
  <div v-if="report" class="report">
    <header class="page-head">
      <h1>Draft audit report</h1>
      <span class="grow" />
      <template v-if="reconcile">
        <Button label="Keep current" size="small" outlined severity="secondary" :loading="busy" @click="settle('keep')" />
        <Button label="Use generated" icon="pi pi-check" size="small" :loading="busy" @click="settle('replace')" />
      </template>
      <template v-else>
        <Button
          v-if="hasContent"
          :label="editing ? 'Done' : 'Edit'"
          :icon="editing ? 'pi pi-check' : 'pi pi-pencil'"
          size="small"
          outlined
          severity="secondary"
          :loading="busy"
          @click="editing ? doneEditing() : (editing = true)"
        />
        <Button
          v-if="hasContent"
          label="Check quality"
          icon="pi pi-check-circle"
          size="small"
          outlined
          severity="secondary"
          :loading="busy"
          @click="check(false)"
        />
        <Button
          v-if="editing"
          label="Save"
          icon="pi pi-save"
          size="small"
          :loading="busy"
          @click="save(true)"
        />
        <Button
          v-else
          :label="report.generated_at ? 'Regenerate' : 'Generate report'"
          icon="pi pi-sparkles"
          size="small"
          :loading="busy"
          @click="generate"
        />
        <UiOverflowMenu :items="menuItems" tooltip="More report actions" />
      </template>
    </header>

    <UiEmptyState
      v-if="!hasContent && !reconcile"
      icon="pi pi-file-edit"
      title="No report drafted yet"
      description="The draft is written from the audit file: the confirmed findings, the tests behind them, and the scope limitations recorded during fieldwork. Every figure it states is checked against the register."
    >
      <Button label="Generate report" icon="pi pi-sparkles" :loading="busy" @click="generate" />
      <Button label="Write it myself" icon="pi pi-pencil" outlined @click="startReport" />
    </UiEmptyState>

    <template v-else>
      <!-- Reconcile is a state of the page: both drafts at full width, and the
           choice in the header where the page's other decisions are. -->
      <section v-if="reconcile" class="reconcile">
        <div>
          <p class="aw-label">Current · edited by an auditor</p>
          <UiMarkdownDocument :markdown="reconcile.current" />
        </div>
        <div>
          <p class="aw-label">Generated · new draft</p>
          <UiMarkdownDocument :markdown="reconcile.generated" />
        </div>
      </section>

      <UiDocumentPage
        v-else
        :entries="outlineEntries"
        outlineLabel="On this report"
        :marks="marks"
        :badges="badges"
        railWidth="20rem"
        :fills="editing"
      >
        <div v-if="editing" class="editor">
          <MarkdownEditor v-model="report.markdown" placeholder="Write the report, or generate a draft from the audit file." />
        </div>
        <UiMarkdownDocument v-else :markdown="report.markdown">
          <template #after-heading="{ entry }">
            <template v-for="issue in (entry && entry.id !== titleAnchor ? placed.bySection.get(entry.id) : placed.title) ?? []" :key="issue.code">
              <p class="issue-strip" :data-tone="issue.severity">
                <span><b>{{ issueHeading(issue.code) }}.</b> {{ issue.message }}</span>
                <button v-if="stripAction(issue)" type="button" @click="runStrip(issue)">{{ stripAction(issue) }}</button>
              </p>
            </template>
          </template>
        </UiMarkdownDocument>

        <template #rail>
          <!-- The one fact that decides what this draft may claim. It was the
               band across the top of the page; nothing else on the page says
               it, so it leads the rail instead. -->
          <section v-if="stale" class="card preliminary">
            <h3 class="aw-label">Preliminary</h3>
            <p>{{ stale }}</p>
          </section>

          <section class="card">
            <header>
              <h3 class="aw-label">Issues</h3>
              <span class="count aw-figure">{{ issues.all.length }}<template v-if="issues.checkedAt"> · checked {{ stamp(issues.checkedAt) }}</template></span>
            </header>
            <p v-if="!issues.all.length" class="none">Nothing outstanding.</p>

            <template v-if="issues.aboutReport.length">
              <p class="group">About the report · <b class="aw-figure">{{ issues.aboutReport.length }}</b></p>
              <button
                v-for="(issue, index) in issues.aboutReport"
                :key="`${issue.code}:${index}`"
                type="button"
                class="issue-row"
                @click="issue.refs.length ? openRef(issue.refs[0]) : undefined"
              >
                <span class="dot" :data-tone="issue.severity" aria-hidden="true" />
                <span>{{ issueHeading(issue.code) }}</span>
              </button>
            </template>

            <template v-if="blockedFindings.length">
              <p class="group">Findings it cannot include · <b class="aw-figure">{{ blockedFindings.length }}</b></p>
              <p class="group-note">
                {{ blockedFindings.length === (findings.length || blockedFindings.length)
                  ? 'Every finding in the register is unsupported.'
                  : `${plural(blockedFindings.length, 'finding')} cannot be carried until fieldwork closes what each one owes.` }}
              </p>
              <button
                v-for="item in blockedFindings"
                :key="item.id"
                type="button"
                class="issue-row finding"
                @click="openRef(`finding:${item.id}`)"
              >
                <span class="dot" data-tone="error" aria-hidden="true" />
                <span class="aw-figure"><b>{{ item.id }}</b><template v-if="item.owed.length"> · {{ item.owed.join(' · ') }}</template></span>
              </button>
            </template>
          </section>

          <section class="card">
            <header>
              <h3 class="aw-label">Drawn from</h3>
              <span v-if="report.generated_at" class="count aw-figure">{{ stamp(report.generated_at) }}</span>
            </header>
            <template v-if="riskBands.total">
              <div class="risk-bar" role="img" :aria-label="`${plural(riskBands.total, 'risk')} in the matrix`">
                <i v-for="band in riskBands.bands" :key="band.key" :style="{ width: `${band.share}%`, background: band.colour }" />
              </div>
              <p class="risk-legend">
                <span v-for="band in riskBands.bands" :key="band.key"><s :style="{ background: band.colour }" />{{ band.count }} {{ band.label }}</span>
              </p>
            </template>
            <dl class="stats">
              <div v-for="item in statistics" :key="item.key">
                <dt>{{ item.label }}</dt>
                <dd :data-tone="item.tone && statistic(item.key) ? item.tone : undefined">{{ statistic(item.key).toLocaleString() }}</dd>
              </div>
              <div>
                <dt>Scope limitations recorded</dt>
                <dd>{{ (context?.scope_limitations ?? []).length }}</dd>
              </div>
            </dl>
          </section>

          <!-- Who wrote it and when, where the rest of the provenance is. -->
          <section class="card">
            <h3 class="aw-label">Written</h3>
            <dl class="stats">
              <div>
                <dt>Generated</dt>
                <dd :data-tone="report.generated_at ? 'agent' : undefined">
                  {{ report.generated_at ? `assistant · ${stamp(report.generated_at)}` : 'by an auditor' }}
                </dd>
              </div>
              <div>
                <dt>Edited</dt>
                <dd>{{ report.edited ? `auditor · ${stamp(report.updated)}` : 'not since' }}</dd>
              </div>
              <div v-if="issues.checkedAt">
                <dt>Checked</dt>
                <dd>{{ stamp(issues.checkedAt) }}</dd>
              </div>
            </dl>
          </section>

          <section v-if="report.generation_warnings.length" class="card">
            <header>
              <h3 class="aw-label">Generation notes</h3>
              <span class="count aw-figure">{{ report.generation_warnings.length }}</span>
            </header>
            <p v-for="warning in report.generation_warnings" :key="warning" class="note">{{ warning }}</p>
          </section>
        </template>
      </UiDocumentPage>
    </template>

    <Drawer v-model:visible="templateOpen" position="right" header="Report template" :style="{ width: 'min(45rem, 96vw)' }">
      <p class="template-note">The template controls the generated deliverable structure. Section comments provide model instructions.</p>
      <Textarea v-if="template" v-model="template.markdown" rows="24" spellcheck="false" class="template-editor" />
      <div class="template-foot">
        <Button label="Restore default" severity="secondary" text size="small" @click="saveTemplate(true)" />
        <Button label="Save override" icon="pi pi-save" size="small" @click="saveTemplate(false)" />
      </div>
    </Drawer>
  </div>
</template>

<style scoped>
.report { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

/* The editor fills the column and scrolls inside itself. Given a fixed height
   inside a scrolling column it produced two scrollbars for one text, and the
   outer one moved the editor rather than the document in it. */
.editor { display: flex; flex-direction: column; min-height: 24rem; }
.editor :deep(.markdown-editor) { flex: 1; min-height: 0; overflow-y: auto; }

.reconcile { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; flex: 1; min-height: 0; overflow-y: auto; }
.reconcile > div { display: flex; flex-direction: column; gap: .5rem; min-width: 0; }

/* The problem, under the heading it is about. A 3px rule rather than a full
   border: it is an annotation on the document, not a card in it. */
.issue-strip {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem;
  margin: .5rem 0 .75rem;
  padding: .5rem .75rem;
  border-left: 3px solid var(--aw-danger);
  background: var(--aw-danger-soft); color: var(--aw-danger-ink);
  font-size: var(--aw-text-sm); line-height: 1.45;
}
.issue-strip[data-tone='warning'] { border-left-color: var(--aw-warn); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.issue-strip[data-tone='info'] { border-left-color: var(--aw-border-strong); background: var(--aw-raised); color: var(--aw-ink-soft); }
.issue-strip button {
  flex: none; padding: 0; border: 0; background: none;
  color: inherit; font: inherit; font-weight: 700;
  text-decoration: underline; text-underline-offset: 2px; cursor: pointer;
}

.card { display: flex; flex-direction: column; gap: .35rem; min-width: 0; }
.card header { display: flex; align-items: baseline; justify-content: space-between; gap: .5rem; }
.card header h3 { margin: 0; }
.count { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.none, .note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.45; }
.group { margin: .5rem 0 .1rem; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); font-weight: 600; }
.group b { color: var(--aw-ink-strong); }
.group-note { margin: 0 0 .25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.4; }

.issue-row {
  display: flex; align-items: baseline; gap: .4rem;
  width: 100%; min-width: 0;
  padding: .25rem .35rem;
  border: 0; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-xs); line-height: 1.4; text-align: left; cursor: pointer;
}
.issue-row:hover { background: var(--aw-panel); color: var(--aw-ink-strong); }
.issue-row.finding b { color: var(--aw-teal); font-family: var(--aw-font-mono); }
.issue-row .dot { width: 6px; height: 6px; flex: none; border-radius: 50%; background: var(--aw-warn); transform: translateY(-1px); }
.issue-row .dot[data-tone='error'] { background: var(--aw-danger); }
.issue-row .dot[data-tone='info'] { background: var(--aw-border-strong); }

.risk-bar { display: flex; height: .5rem; margin: .35rem 0 .3rem; border-radius: var(--aw-radius-pill); overflow: hidden; }
.risk-bar i { display: block; }
.risk-legend { display: flex; flex-wrap: wrap; gap: .1rem .6rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.risk-legend span { display: inline-flex; align-items: center; gap: .25rem; }
.risk-legend s { width: .4rem; height: .4rem; border-radius: 2px; text-decoration: none; }

.stats { display: grid; gap: 0; margin: .4rem 0 0; }
.stats > div { display: flex; align-items: baseline; justify-content: space-between; gap: .75rem; padding: .3rem 0; border-bottom: 1px solid var(--aw-border); }
.stats > div:last-child { border-bottom: 0; }
.stats dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats dd { margin: 0; font-family: var(--aw-font-mono); font-variant-numeric: tabular-nums; font-weight: 600; color: var(--aw-ink); }
.stats dd[data-tone='warn'] { color: var(--aw-warn); }
.stats dd[data-tone='agent'] { color: var(--aw-accent); }
.preliminary { padding: .5rem .625rem; border-left: 3px solid var(--aw-warn); border-radius: var(--aw-radius-control); background: var(--aw-warn-soft); }
.preliminary p { margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); line-height: 1.45; }

.template-note { margin: 0 0 .6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.5; }
.template-editor { width: 100%; font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); }
.template-foot { display: flex; justify-content: flex-end; gap: .5rem; margin-top: .75rem; }

@container workspace-panel (max-width: 60rem) {
  .reconcile { grid-template-columns: minmax(0, 1fr); }
}
</style>
