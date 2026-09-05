<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
import SplitButton from 'primevue/splitbutton'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import EvidenceAnchorDialog from '../components/EvidenceAnchorDialog.vue'
import ProvenanceRail from '../components/agent/ProvenanceRail.vue'
import RcmControlAttributesEditor from '../components/planning/RcmControlAttributesEditor.vue'
import UiEmptyState from '../components/ui/UiEmptyState.vue'
import UiOverflowMenu from '../components/ui/UiOverflowMenu.vue'
import type {
  AuditDocument, AuditObservation, CriterionRef, CycleVouchMetadata,
  DocumentSchemaCatalogEntry, PlanningPayload, TestRollup, WorkingPaper,
} from '../types'
import { plural } from '../format'

/**
 * One RCM row, as a page.
 *
 * The row detail was a 1120px modal with a 980px working-paper modal opening
 * over it, and neither had a URL a reviewer could be sent. Everything a row
 * holds is here instead, split across tabs by the question being asked:
 * what the row says, what it asserts, what covers it, what was written up
 * about it, and where it came from.
 *
 * The matrix keeps a drawer for the quick edits — process, rating, the two
 * statements, sign-off — so walking the matrix does not mean leaving it.
 */

const props = defineProps<{ id: string; rowId: string }>()
const route = useRoute()
const router = useRouter()
const nav = useWorkspaceNav()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.id)
const assistantChat = useAssistantChat(props.id)
const { launchMode } = agent
const context = inject(workspaceContextKey, null)

const TABS = [
  { key: 'definition', label: 'Definition' },
  { key: 'attributes', label: 'Attributes' },
  { key: 'tests', label: 'Tests' },
  { key: 'paper', label: 'Working paper' },
  { key: 'provenance', label: 'Where this came from' },
] as const
type TabKey = typeof TABS[number]['key']

const data = ref<PlanningPayload | null>(null)
const cycleMeta = ref<CycleVouchMetadata | null>(null)
const documentSchemas = ref<DocumentSchemaCatalogEntry[]>([])
const documents = ref<AuditDocument[]>([])
const workingPaper = ref<WorkingPaper | null>(null)
const paperLoading = ref(false)
const saving = ref(false)
const regenerating = ref(false)
const criterionOpen = ref(false)
const criterion = ref<CriterionRef | null>(null)
const currentSection = ref('')
const addMenu = ref<InstanceType<typeof Menu> | null>(null)

const ratings = ['low', 'medium', 'high', 'critical']
const CONCLUSIONS: Record<string, { label: string; tone: string }> = {
  effective: { label: 'Effective', tone: 'ok' },
  partially_effective: { label: 'Partially effective', tone: 'warn' },
  ineffective: { label: 'Ineffective', tone: 'bad' },
  not_applicable: { label: 'Not applicable', tone: 'neutral' },
}

const rows = computed(() => data.value?.rcm ?? [])
const row = computed(() => rows.value.find(item => item.id === props.rowId) ?? null)
const position = computed(() => rows.value.findIndex(item => item.id === props.rowId))
const tab = computed<TabKey>(() => {
  const value = String(route.query.tab || 'definition')
  return (TABS.some(item => item.key === value) ? value : 'definition') as TabKey
})
const tests = computed<TestRollup[]>(() => row.value?.execution_rollup.test_rollups ?? [])
const openExceptions = computed(
  () => tests.value.reduce((total, item) => total + (item.open_exception_count ?? 0), 0),
)
const findings = computed(() => data.value?.finding_rollups.by_rcm[props.rowId] ?? [])
const observations = computed<AuditObservation[]>(
  () => (data.value?.observations ?? []).filter(item => item.rcm_id === props.rowId),
)
const conclusion = computed(() => {
  const key = String(row.value?.execution_rollup.control_conclusion ?? '') || 'no_conclusion'
  return CONCLUSIONS[key] ?? { label: 'No conclusion', tone: 'neutral' }
})
const agentSet = computed(
  () => row.value?.created_by === 'agent' && row.value.review_status !== 'reviewed',
)
const reviewed = computed({
  get: () => row.value?.review_status === 'reviewed',
  set: value => { if (row.value) row.value.review_status = value ? 'reviewed' : 'draft' },
})

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}
function goTab(key: TabKey) {
  void router.replace({ query: key === 'definition' ? {} : { tab: key } })
}

async function reload() {
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.id}/planning`)
}
onMounted(() => {
  void reload().catch(error => fail('Could not load the RCM row', error))
  void api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.id}/documents`)
    .then(result => { documents.value = result.items })
    .catch(() => { documents.value = [] })
  void api.get<{ cycle_vouch: CycleVouchMetadata }>(`/api/workspaces/${props.id}/doc-tests/meta`)
    .then(result => { cycleMeta.value = result.cycle_vouch })
    .catch(() => { cycleMeta.value = null })
  void api.get<{ items: DocumentSchemaCatalogEntry[] }>(`/api/workspaces/${props.id}/documents/schemas`)
    .then(result => { documentSchemas.value = result.items ?? [] })
    .catch(() => { documentSchemas.value = [] })
})
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void reload().catch(error => fail('Could not refresh the RCM row', error))
})
onUnmounted(unsubscribe)

// The paper is rendered on demand: it is one of five tabs, and rendering it on
// arrival would spend a request on a tab most visits never open.
watch([tab, row], () => {
  if (tab.value === 'paper' && row.value && !workingPaper.value) void loadPaper()
}, { immediate: true })

async function loadPaper() {
  paperLoading.value = true
  try {
    workingPaper.value = await api.get(`/api/workspaces/${props.id}/rcm/${props.rowId}/working-paper`)
  } catch (error) { fail('Could not render the working paper', error) }
  finally { paperLoading.value = false }
}
async function regeneratePaper() {
  regenerating.value = true
  try {
    workingPaper.value = await api.post(`/api/workspaces/${props.id}/rcm/${props.rowId}/working-paper`, {})
    toast.add({ severity: 'success', summary: 'Working paper regenerated', life: 1800 })
  } catch (error) { fail('Could not regenerate the working paper', error) }
  finally { regenerating.value = false }
}

/**
 * The paper, with its headings addressable.
 *
 * The section list is built from the rendered markdown rather than declared,
 * so a paper whose template changes keeps a contents list that matches it.
 * `h2` is the section level the renderer emits; `h3` is the fallback for a
 * paper written one level down.
 */
const paper = computed(() => {
  if (!workingPaper.value) return { html: '', sections: [] as Array<{ id: string; label: string }> }
  const parsed = new DOMParser().parseFromString(workingPaper.value.html, 'text/html')
  const found = parsed.querySelectorAll('h2')
  const headings = Array.from(found.length ? found : parsed.querySelectorAll('h3'))
  const sections = headings.map((node, index) => {
    const id = `paper-section-${index}`
    node.setAttribute('id', id)
    return { id, label: node.textContent?.trim() ?? `Section ${index + 1}` }
  })
  return { html: parsed.body.innerHTML, sections }
})
/**
 * There is no PDF endpoint, and inventing one would put a second renderer
 * beside the one that already produces the paper. The browser's own print
 * pipeline saves a PDF from the page it is looking at, and the print rules
 * below strip everything that is not the paper.
 */
function printPaper() { window.print() }
function goSection(id: string) {
  currentSection.value = id
  document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
}
const paperGenerated = computed(() => (workingPaper.value?.generated_at
  ? new Date(workingPaper.value.generated_at).toLocaleString()
  : null))

async function save() {
  const current = row.value
  if (!current) return
  saving.value = true
  try {
    await api.patch(`/api/workspaces/${props.id}/rcm/${current.id}`, {
      process: current.process,
      risk: current.risk,
      risk_rating: current.risk_rating,
      // `business_cycle` is a projection of the attributes; the backend derives it.
      control_attributes: current.control_attributes,
      control: current.control,
      control_type: current.control_type,
      control_owner: current.control_owner,
      criteria: current.criteria,
      review_status: current.review_status,
    })
    await reload()
    void context?.reloadStatus()
    toast.add({ severity: 'success', summary: 'RCM row saved', life: 1800 })
  } catch (error) { fail('Could not save the RCM row', error) }
  finally { saving.value = false }
}
/** Agreeing with an agent-set conclusion is a sign-off, not an edit of it. */
async function acceptAndReview() {
  if (!row.value) return
  row.value.review_status = 'reviewed'
  await save()
}
function remove() {
  const current = row.value
  if (!current) return
  const linked = current.test_refs?.length ?? 0
  confirm.require({
    header: 'Remove RCM row',
    message: `Remove "${current.process?.trim() || current.id}"?`
      + (linked
        ? ` Its ${plural(linked, 'linked test')} will be unlinked, not deleted; findings will be unlinked too.`
        : ' Any linked findings will be unlinked.'),
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.id}/rcm/${current.id}`)
        void context?.reloadStatus()
        await router.replace(nav.to('rcm'))
      } catch (error) { fail('Could not remove the risk', error) }
    },
  })
}
async function exportMatrix() {
  try {
    await api.downloadGet(
      `/api/workspaces/${props.id}/rcm/export`,
      `${context?.workspace.value.name ?? 'workspace'}_RCM.xlsx`,
    )
  } catch (error) { fail('Could not export the RCM', error) }
}

function step(delta: number) {
  const next = rows.value[position.value + delta]
  if (next) void router.push(nav.to('rcm-row', { rcm: next.id, tab: route.query.tab as string }))
}
function openTest(rollup: TestRollup) {
  void nav.push(rollup.kind === 'datatest' ? 'data-tests' : 'doc-tests', { test: rollup.test_id })
}
function openFinding(findingId: string) {
  void nav.push('findings', { finding: findingId })
}
function documentName(id: string) {
  const found = documents.value.find(item => item.id === id)
  return found?.source || found?.title || id
}
function openCriterion(value: CriterionRef) {
  criterion.value = value
  criterionOpen.value = true
}
async function addTest(kind: 'data' | 'document' | 'generate') {
  if (kind !== 'generate') {
    void nav.push(kind === 'data' ? 'data-tests' : 'doc-tests', { create: '1', rcm: props.rowId })
    return
  }
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Generate planned tests for RCM row ${props.rowId}.`,
      'act', launchMode.value,
      {
        source: 'tab_button', requestedOutcomes: ['tests.specified'],
        runContext: { target_refs: [`rcm:${props.rowId}`] },
      },
    )
    agent.openPanel()
  } catch (error) { fail('Could not start planned test generation', error) }
}
async function draftFinding() {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Draft findings for RCM row ${props.rowId}.`,
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button', runContext: { rcm_id: props.rowId } },
    )
    agent.openPanel()
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
async function promoteObservation(item: AuditObservation) {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      `Draft a finding from observation ${item.id}.`,
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button', runContext: { observation_id: item.id } },
    )
    agent.openPanel()
  } catch (error) { fail('Could not start the finding-draft workflow', error) }
}
async function copyPaper(kind: 'markdown' | 'html') {
  if (!workingPaper.value) return
  await navigator.clipboard.writeText(workingPaper.value[kind])
  toast.add({ severity: 'success', summary: `${kind === 'markdown' ? 'Markdown' : 'HTML'} copied`, life: 1600 })
}
const copyOptions = [
  { label: 'Copy Markdown', icon: 'pi pi-copy', command: () => void copyPaper('markdown') },
  { label: 'Copy HTML', icon: 'pi pi-code', command: () => void copyPaper('html') },
]
const addOptions = [
  { label: 'Data test', icon: 'pi pi-chart-bar', command: () => void addTest('data') },
  { label: 'Document test', icon: 'pi pi-file-check', command: () => void addTest('document') },
  { separator: true },
  { label: 'Generate with assistant', icon: 'pi pi-sparkles', command: () => void addTest('generate') },
]
const menuItems = computed(() => [
  { label: 'Export the matrix', icon: 'pi pi-download', command: () => void exportMatrix() },
  { label: 'Remove row', icon: 'pi pi-trash', command: () => remove() },
])
</script>

<template>
  <div class="ui-surface ui-surface--stacked">
    <nav class="crumb" aria-label="Breadcrumb">
      <RouterLink :to="nav.to('record')" class="crumb__back">
        <i class="pi pi-arrow-left" aria-hidden="true" />Engagement record
      </RouterLink>
      <span class="crumb__sep" aria-hidden="true">/</span>
      <RouterLink :to="nav.to('rcm')" class="crumb__back">Risk and control matrix</RouterLink>
      <span class="crumb__sep" aria-hidden="true">/</span>
      <span class="crumb__cur" aria-current="page">{{ rowId }}</span>
    </nav>

    <UiEmptyState
      v-if="data && !row"
      icon="pi pi-map"
      title="This row is no longer in the matrix"
      description="It may have been removed, or the link may name a row from another engagement."
    />

    <template v-else-if="row">
      <header class="row-head">
        <div class="identity">
          <span class="row-id">{{ row.id }}</span>
          <span class="rating" :data-rating="row.risk_rating"><span class="rating-dot" />{{ row.risk_rating }}</span>
          <span class="process">{{ row.process }}</span>
          <span class="grow" />

          <!-- Walking the matrix without going back to it: the reviewer's
               commonest movement on this page. -->
          <span class="stepper">
            <button type="button" :disabled="position <= 0" aria-label="Previous row" @click="step(-1)">
              <i class="pi pi-chevron-left" />
            </button>
            <span class="aw-figure">{{ position + 1 }} of {{ rows.length }}</span>
            <button type="button" :disabled="position >= rows.length - 1" aria-label="Next row" @click="step(1)">
              <i class="pi pi-chevron-right" />
            </button>
          </span>

          <template v-if="tab === 'paper'">
            <SplitButton
              label="Copy"
              icon="pi pi-copy"
              size="small"
              outlined
              severity="secondary"
              :model="copyOptions"
              @click="copyPaper('markdown')"
            />
            <Button
              label="Export PDF"
              icon="pi pi-download"
              size="small"
              outlined
              severity="secondary"
              :disabled="!workingPaper"
              @click="printPaper"
            />
            <Button
              label="Regenerate"
              icon="pi pi-refresh"
              size="small"
              :loading="regenerating"
              @click="regeneratePaper"
            />
          </template>
          <template v-else>
            <Button
              label="Add test"
              icon="pi pi-chevron-down"
              iconPos="right"
              size="small"
              outlined
              severity="secondary"
              aria-haspopup="true"
              @click="addMenu?.toggle($event)"
            />
            <Menu ref="addMenu" :model="addOptions" popup />
            <Button label="Save row" icon="pi pi-save" size="small" :loading="saving" @click="save" />
          </template>
          <UiOverflowMenu :items="menuItems" tooltip="More row actions" />
        </div>

        <h1>{{ row.risk }}</h1>

        <nav class="tabs" aria-label="Row sections">
          <button
            v-for="item in TABS"
            :key="item.key"
            type="button"
            class="tab"
            :aria-current="tab === item.key ? 'page' : undefined"
            @click="goTab(item.key)"
          >
            {{ item.label }}
            <span v-if="item.key === 'attributes' && row.control_attributes.length" class="badge">
              {{ row.control_attributes.length }}
            </span>
            <span
              v-else-if="item.key === 'tests' && tests.length"
              class="badge"
              :data-tone="openExceptions ? 'bad' : 'neutral'"
            >
              {{ tests.length }}<template v-if="openExceptions"> · {{ openExceptions }} exc</template>
            </span>
          </button>
        </nav>
      </header>

      <div class="row-body">
        <!-- Definition: what the row says, and what has become of it. -->
        <div v-if="tab === 'definition'" class="definition">
          <div class="record">
            <div class="quad">
              <label>Process<InputText v-model="row.process" size="small" /></label>
              <label>Rating<Select v-model="row.risk_rating" :options="ratings" size="small" /></label>
              <label>Control type<InputText v-model="row.control_type" size="small" /></label>
              <label>Control owner<InputText v-model="row.control_owner" size="small" /></label>
            </div>
            <label>Risk<Textarea v-model="row.risk" rows="2" autoResize /></label>
            <label>Control<Textarea v-model="row.control" rows="3" autoResize placeholder="No control identified" /></label>
            <label>
              Criteria<Textarea v-model="row.criteria" rows="3" autoResize />
              <!-- The sentence the criterion rests on, not a restatement of it.
                   Frozen when the row was written, so opening one shows what
                   the document said then and the hash reveals any drift. -->
              <span v-if="row.criteria_refs?.length" class="citations">
                <button
                  v-for="ref in row.criteria_refs"
                  :key="ref.id"
                  type="button"
                  :title="ref.excerpt"
                  @click="openCriterion(ref)"
                >
                  <i class="pi pi-link" />
                  <span>{{ documentName(ref.source_id) }}</span>
                  <code v-if="ref.page">p.{{ ref.page }}</code>
                </button>
              </span>
            </label>

            <section class="attributes-brief">
              <div class="section-head">
                <p class="aw-label">Attributes · {{ row.control_attributes.length }}</p>
                <span class="grow" />
                <button type="button" class="link" @click="goTab('attributes')">Edit attributes</button>
              </div>
              <div v-for="attribute in row.control_attributes" :key="attribute.key" class="attribute">
                <span class="assertion">{{ attribute.assertion }}</span>
                <span class="requirement">{{ attribute.requirement }}</span>
              </div>
              <p v-if="!row.control_attributes.length" class="muted">
                No attribute is recorded against this control.
              </p>
            </section>
          </div>

          <aside class="side">
            <section class="conclusion" :data-tone="conclusion.tone">
              <div class="section-head">
                <strong>Conclusion: {{ conclusion.label }}</strong>
              </div>
              <p v-if="agentSet" class="by-agent">Set by the agent. No one has read it yet.</p>
              <Button
                v-if="agentSet"
                label="Accept and mark reviewed"
                icon="pi pi-check"
                size="small"
                outlined
                :loading="saving"
                @click="acceptAndReview"
              />
            </section>

            <section class="card">
              <div class="section-head">
                <p class="aw-label">Tests · {{ tests.length }}</p>
                <span class="grow" />
                <span class="aw-figure muted-figure">
                  {{ row.execution_rollup.completed ?? 0 }}/{{ tests.length }} complete
                </span>
              </div>
              <article
                v-for="rollup in tests"
                :key="rollup.test_id"
                class="test"
                :data-open="Boolean(rollup.open_exception_count)"
              >
                <div class="test-head">
                  <span class="test-id">{{ rollup.test_id }}</span>
                  <span class="kind">{{ rollup.kind === 'datatest' ? 'Data' : 'Document' }}</span>
                  <span class="grow" />
                  <span v-if="rollup.exception_count" class="open">
                    {{ plural(rollup.exception_count, 'exception') }} · {{ rollup.open_exception_count }} open
                  </span>
                </div>
                <button type="button" class="test-title" @click="openTest(rollup)">{{ rollup.title }}</button>
                <p v-if="rollup.assurance_label" class="muted">
                  {{ plural(rollup.tested_items ?? 0, 'item') }} tested ·
                  {{ rollup.conclusion_eligible ? 'population conclusion eligible' : 'no population conclusion' }}
                </p>
              </article>
              <p v-if="!tests.length" class="muted">
                This risk has no linked test and cannot pass coverage.
              </p>
            </section>

            <section class="card">
              <p class="aw-label">Finding</p>
              <div class="finding-row">
                <button
                  v-for="finding in findings"
                  :key="finding.id"
                  type="button"
                  class="finding-chip"
                  @click="openFinding(finding.id)"
                >
                  <span class="finding-id">{{ finding.id }}</span>{{ finding.severity }}
                </button>
                <span v-if="!findings.length" class="muted">None yet.</span>
                <span class="grow" />
                <button type="button" class="link" @click="draftFinding">
                  {{ findings.length ? 'Regenerate' : 'Generate finding' }}
                </button>
              </div>
            </section>

            <section class="card reviewed-card">
              <ToggleSwitch v-model="reviewed" inputId="row-reviewed" />
              <label for="row-reviewed">Reviewed by an auditor</label>
              <span class="grow" />
              <span class="muted">{{ reviewed ? 'Reviewed' : 'Draft' }}</span>
            </section>
          </aside>
        </div>

        <!-- Attributes: the full editor, including the cycle comparisons that
             the drawer and the definition tab only summarise. -->
        <div v-else-if="tab === 'attributes'" class="wide-tab">
          <RcmControlAttributesEditor
            v-model="row.control_attributes"
            :metadata="cycleMeta"
            :schemas="documentSchemas"
          />
        </div>

        <div v-else-if="tab === 'tests'" class="wide-tab tests-tab">
          <article v-for="rollup in tests" :key="rollup.test_id" class="test-card">
            <div class="test-head">
              <span class="test-id">{{ rollup.test_id }}</span>
              <span class="kind">{{ rollup.kind === 'datatest' ? 'Data' : 'Document' }}</span>
              <span class="grow" />
              <span v-if="rollup.exception_count" class="open">
                {{ plural(rollup.exception_count, 'exception') }} · {{ rollup.open_exception_count }} open
              </span>
              <Button label="Open test" icon="pi pi-arrow-up-right" size="small" outlined severity="secondary" @click="openTest(rollup)" />
            </div>
            <p class="test-name">{{ rollup.title }}</p>
            <p class="muted">{{ rollup.result_summary || 'Not executed yet.' }}</p>
            <p v-if="rollup.assurance_scope" class="muted">
              <strong>{{ rollup.assurance_label }}</strong> ·
              {{ plural(rollup.tested_items ?? 0, 'tested item') }} ·
              {{ rollup.failed_items ?? 0 }} failed ·
              {{ rollup.incomplete_items ?? 0 }} incomplete ·
              {{ rollup.conclusion_eligible ? 'population conclusion eligible' : 'no population conclusion' }}
            </p>
            <p v-if="rollup.scope_limitations" class="muted">Limitation: {{ rollup.scope_limitations }}</p>
          </article>
          <UiEmptyState
            v-if="!tests.length"
            compact
            icon="pi pi-shield"
            title="No test covers this risk"
            description="Link a data or document test to it, or ask the assistant to write one."
          />
        </div>

        <!-- The working paper, as a document rather than as a dialog. -->
        <div v-else-if="tab === 'paper'" class="paper-tab">
          <aside class="paper-nav">
            <p class="aw-label">On this paper</p>
            <button
              v-for="(section, index) in paper.sections"
              :key="section.id"
              type="button"
              class="paper-link"
              :aria-current="(currentSection || paper.sections[0]?.id) === section.id ? 'true' : undefined"
              @click="goSection(section.id)"
            >
              {{ section.label || `Section ${index + 1}` }}
            </button>
            <div class="generated">
              <p class="aw-label">Generated</p>
              <p v-if="paperGenerated" class="aw-figure">{{ paperGenerated }}</p>
              <p v-else class="muted">Not generated yet.</p>
              <p v-if="agentSet" class="by-agent">Not yet read by a person</p>
            </div>
          </aside>
          <article v-if="workingPaper" class="paper" v-html="paper.html" />
          <UiEmptyState
            v-else
            :icon="paperLoading ? 'pi pi-spinner pi-spin' : 'pi pi-file'"
            :title="paperLoading ? 'Rendering the paper' : 'No working paper yet'"
            description="The paper is drafted from the row and the runs that filed its test results."
          />
        </div>

        <div v-else class="wide-tab provenance-tab">
          <ProvenanceRail :key="row.id" :workspaceId="id" :artifactRef="`rcm:${row.id}`" />
          <section v-if="observations.length" class="observations">
            <p class="aw-label">Exception observations</p>
            <article v-for="item in observations" :key="item.id" class="observation" :data-outcome="item.outcome">
              <div class="observation-head">
                <span class="outcome">{{ item.outcome.replaceAll('_', ' ') }}</span>
                <span class="classification">{{ item.classification.replaceAll('_', ' ') }}</span>
                <span class="grow" />
                <Button
                  label="Draft finding"
                  icon="pi pi-sparkles"
                  size="small"
                  text
                  severity="secondary"
                  :disabled="item.outcome !== 'exception'"
                  @click="promoteObservation(item)"
                />
              </div>
              <p>{{ item.summary }}</p>
            </article>
          </section>
        </div>
      </div>
    </template>

    <EvidenceAnchorDialog v-model="criterionOpen" :anchor="criterion" :documents="documents" />
  </div>
</template>

<style scoped>
.row-head {
  display: flex; flex-direction: column; gap: .625rem;
  flex: none; padding: 1rem 1.5rem 0;
  border-bottom: 1px solid var(--aw-border); background: var(--aw-panel);
}
.identity { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
.grow { flex: 1; }
.row-id { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); font-weight: 600; }
.rating { display: inline-flex; align-items: center; gap: .3125rem; font-size: var(--aw-text-sm); font-weight: 600; text-transform: capitalize; }
.rating-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--aw-muted); }
.rating[data-rating='critical'] { color: var(--aw-danger-ink); }
.rating[data-rating='critical'] .rating-dot { background: var(--aw-danger-ink); }
.rating[data-rating='high'] { color: var(--aw-danger); }
.rating[data-rating='high'] .rating-dot { background: var(--aw-danger); }
.rating[data-rating='medium'] { color: var(--aw-warn-ink); }
.rating[data-rating='medium'] .rating-dot { background: var(--aw-warn); }
.rating[data-rating='low'] { color: var(--aw-low-ink); }
.rating[data-rating='low'] .rating-dot { background: var(--aw-low); }
.process { color: var(--aw-muted); font-size: var(--aw-text-sm); }

.stepper { display: inline-flex; align-items: center; overflow: hidden; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); }
.stepper button { display: grid; place-items: center; width: 1.875rem; height: 1.75rem; padding: 0; border: 0; background: none; color: var(--aw-ink-soft); cursor: pointer; }
.stepper button:hover:not(:disabled) { background: var(--aw-raised); }
.stepper button:disabled { color: var(--aw-border-strong); cursor: not-allowed; }
.stepper > span { padding: 0 .625rem; border-inline: 1px solid var(--aw-border-strong); color: var(--aw-muted); font-size: var(--aw-text-xs); }

.row-head h1 { max-width: 56rem; margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: 600; line-height: 1.35; letter-spacing: -0.01em; }

.tabs { display: flex; gap: .25rem; }
.tab {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: .5rem .875rem; border: 0; border-bottom: 2px solid transparent;
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-base); font-weight: 600; cursor: pointer;
}
.tab:hover { color: var(--aw-ink-strong); }
.tab[aria-current='page'] { border-bottom-color: var(--aw-teal); color: var(--aw-teal-strong); }
.badge { padding: 0 .375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); font-variant-numeric: tabular-nums; }
.badge[data-tone='bad'] { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }

.row-body { flex: 1; min-height: 0; overflow-y: auto; padding: 1.25rem 1.5rem 1.5rem; background: var(--aw-canvas); container: workspace-panel / inline-size; }

.definition { display: grid; grid-template-columns: minmax(0, 1fr) 23.75rem; gap: 1.25rem; align-items: start; }
.record { display: flex; flex-direction: column; gap: 1.125rem; min-width: 0; }
.quad { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .75rem; }
label { display: flex; flex-direction: column; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
label :deep(.p-inputtext), label :deep(.p-textarea), label :deep(.p-select) { width: 100%; min-width: 0; background: var(--aw-panel); color: var(--aw-ink); font-size: var(--aw-text-base); font-weight: 400; letter-spacing: 0; text-transform: none; }

.citations { display: flex; flex-wrap: wrap; gap: .35rem; margin-top: .35rem; }
.citations button { display: inline-flex; align-items: center; gap: .3rem; padding: .2rem .5rem; border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: var(--aw-teal); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-weight: 600; letter-spacing: 0; text-transform: none; cursor: pointer; }
.citations button:hover { background: var(--aw-teal-soft); }
.citations code { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); }

.section-head { display: flex; align-items: center; gap: .5rem; }
.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.link:hover { text-decoration: underline; }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.45; }
.muted-figure { color: var(--aw-muted); font-size: var(--aw-text-xs); }

.attributes-brief { display: flex; flex-direction: column; gap: .375rem; }
.attribute { display: flex; gap: .5rem; padding: .5rem .625rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.assertion { flex: none; align-self: flex-start; padding: .0625rem .4375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.requirement { min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.4; }

.side { display: flex; flex-direction: column; gap: .75rem; min-width: 0; }
.card, .conclusion { display: flex; flex-direction: column; gap: .625rem; padding: .875rem 1rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.conclusion[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); }
.conclusion[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); }
.conclusion[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); }
.conclusion strong { font-size: var(--aw-text-base); }
.conclusion[data-tone='bad'] strong { color: var(--aw-danger-ink); }
.conclusion[data-tone='warn'] strong { color: var(--aw-warn-ink); }
.conclusion[data-tone='ok'] strong { color: var(--aw-ok); }
.by-agent { margin: 0; color: var(--aw-accent); font-size: var(--aw-text-sm); }

.test, .test-card { display: flex; flex-direction: column; gap: .25rem; padding: .625rem .75rem; border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); }
.test[data-open='true'], .test-card[data-open='true'] { border-left-color: var(--aw-danger); }
.test-head { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.test-id { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); font-weight: 600; }
.kind { padding: .0625rem .4375rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 600; }
.open { color: var(--aw-danger); font-size: var(--aw-text-xs); font-weight: 600; }
.test-title { padding: 0; border: 0; background: none; color: var(--aw-ink-strong); font: inherit; font-size: var(--aw-text-base); font-weight: 600; line-height: 1.4; text-align: left; cursor: pointer; }
.test-title:hover { color: var(--aw-teal); }
.test-name { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600; }

.finding-row { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.finding-chip { display: inline-flex; align-items: center; gap: .35rem; padding: .125rem .5rem; border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control); background: var(--aw-warn-soft); color: var(--aw-warn-ink); font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer; }
.finding-chip .finding-id { font-family: var(--aw-font-mono); }
.reviewed-card { flex-direction: row; align-items: center; }
.reviewed-card label { flex-direction: row; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); font-weight: 500; letter-spacing: 0; text-transform: none; }

.wide-tab { display: flex; flex-direction: column; gap: .75rem; min-width: 0; }
.tests-tab .test-card { background: var(--aw-panel); }
.provenance-tab { gap: 1.25rem; max-width: 52rem; }
.observations { display: flex; flex-direction: column; gap: .5rem; }
.observation { display: flex; flex-direction: column; gap: .25rem; padding: .75rem .875rem; border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-warn); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.observation[data-outcome='exception'] { border-left-color: var(--aw-danger); }
.observation-head { display: flex; align-items: center; gap: .5rem; }
.observation .outcome { color: var(--aw-danger); font-size: var(--aw-text-xs); font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
.observation .classification { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.observation p { margin: 0; font-size: var(--aw-text-base); line-height: 1.5; }

/* The paper is a document: a contents list beside it, and a page-width card
   rather than a dialog the width of the window. */
.paper-tab { display: grid; grid-template-columns: 13.75rem minmax(0, 1fr); gap: 2rem; align-items: start; }
.paper-nav { position: sticky; top: 0; display: flex; flex-direction: column; gap: .125rem; }
.paper-nav > .aw-label { padding: 0 .625rem .5rem; }
.paper-link { padding: .375rem .625rem; border: 0; border-left: 2px solid transparent; background: none; color: var(--aw-ink-soft); font: inherit; font-size: var(--aw-text-sm); font-weight: 500; text-align: left; cursor: pointer; }
.paper-link:hover { color: var(--aw-teal); }
.paper-link[aria-current='true'] { border-left-color: var(--aw-teal); color: var(--aw-teal-strong); font-weight: 600; }
.generated { display: flex; flex-direction: column; gap: .25rem; margin-top: 1rem; padding: .625rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.generated p { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); }
.generated .by-agent { font-size: var(--aw-text-xs); }

.paper { max-width: 47.5rem; padding: 2rem 2.5rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.paper :deep(h2) { margin: 1.5rem 0 .5rem; color: var(--aw-ink-strong); font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; line-height: 1.3; }
.paper :deep(h2:first-child) { margin-top: 0; }
.paper :deep(h3) { margin: 1.25rem 0 .35rem; color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600; }
.paper :deep(p), .paper :deep(li) { font-size: var(--aw-text-base); line-height: 1.6; }
.paper :deep(table) { width: 100%; border-collapse: collapse; font-size: var(--aw-text-sm); }
.paper :deep(th), .paper :deep(td) { padding: .5rem .625rem; border-top: 1px solid var(--aw-border); text-align: left; }
.paper :deep(th) { background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }

@container workspace-panel (max-width: 64rem) {
  .definition { grid-template-columns: minmax(0, 1fr); }
  .quad { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .paper-tab { grid-template-columns: minmax(0, 1fr); }
  .paper-nav { position: static; }
}

/* Printing this page means printing the paper: the shell, the tabs and the
   contents list are navigation, and navigation does not belong on a filed
   working paper. */
@media print {
  .row-head, .paper-nav, .crumb { display: none !important; }
  .row-body { overflow: visible; padding: 0; background: none; }
  .paper-tab { display: block; }
  .paper { max-width: none; border: 0; padding: 0; }
}
</style>
