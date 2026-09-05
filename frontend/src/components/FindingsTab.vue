<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useConfirm } from 'primevue/useconfirm'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import Listbox from 'primevue/listbox'
import Popover from 'primevue/popover'
import Select from 'primevue/select'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type { AuditFinding, EvidenceRef, FindingsPayload, WorkspaceSummary } from '../types'
import EvidenceAnchorDialog from './EvidenceAnchorDialog.vue'
import MarkdownEditor from './MarkdownEditor.vue'
import ProvenanceRail from './agent/ProvenanceRail.vue'
import FindingNarrative from './findings/FindingNarrative.vue'
import FindingsList from './findings/FindingsList.vue'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiOverflowMenu from './ui/UiOverflowMenu.vue'
import UiReviewBar from './ui/UiReviewBar.vue'
import UiVerdictBar from './ui/UiVerdictBar.vue'
import {
  FINDING_CHIPS, SEVERITY_ORDER, filterFindings, findingsStatus, openItems,
} from './findings/findingsStatus'
import type { FindingsFilter } from './findings/findingsStatus'
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
const reaffirming = ref(false)
const anchor = ref<EvidenceRef | null>(null)
const anchorOpen = ref(false)
const search = ref('')
const statusFilter = ref<FindingsFilter[]>([])
const template = ref<{ markdown: string; source: string } | null>(null)
const templateOpen = ref(false)
/** The narrative is a document until somebody says otherwise. */
const editingNarrative = ref(false)
/** Management's own words are recorded, not drafted, so the box opens on ask. */
const editingResponse = ref(false)
const riskPicker = ref<InstanceType<typeof Popover> | null>(null)
const testPicker = ref<InstanceType<typeof Popover> | null>(null)
const evidencePicker = ref<InstanceType<typeof Popover> | null>(null)

const selected = computed(() => data.value?.items.find(item => item.id === selectedId.value) ?? null)
const items = computed(() => data.value?.items ?? [])
// The bar counts the whole register, not the filtered list: a count that shrank
// as you filtered by it could never be clicked back out of.
const status = computed(() => findingsStatus(items.value))
const statusBusy = computed(() => generatingFindings.value || confirmingAll.value)
const scoped = computed(() => statusFilter.value.reduce<AuditFinding[]>(
  (rows, key) => filterFindings(rows, key), items.value,
))
const filtered = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return scoped.value
  return scoped.value.filter(
    item => `${item.id} ${item.title} ${item.narrative}`.toLowerCase().includes(needle),
  )
})
const rcmOptions = computed(() => (data.value?.rcm ?? []).map(item => ({ label: `${item.id} · ${item.risk}`, value: item.id })))
const testOptions = computed(() => [
  ...(data.value?.data_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: item.id })),
  ...(data.value?.document_tests ?? []).map(item => ({ label: `${item.id} · ${item.title}`, value: item.id })),
])
const availableEvidence = computed(() => (data.value?.evidence_options ?? []).filter(
  option => !selected.value?.evidence_refs.some(item => item.id === option.anchor.id),
))
const unconfirmed = computed(() => items.value.filter(item => !item.auditor_confirmed))
const agentBusy = computed(() => isActive.value || !agent.state.status?.configured)

/** The risks a finding names, resolved against the matrix this payload carries. */
const riskLinks = computed(() => (selected.value?.rcm_refs ?? []).map(id => ({
  id, risk: (data.value?.rcm ?? []).find(row => row.id === id)?.risk ?? '',
})))
const owed = computed(() => (selected.value ? openItems(selected.value) : []))
const authorship = computed(() => {
  const item = selected.value
  if (!item) return ''
  if (item.source === 'agent') return 'drafted by the assistant'
  return item.source === 'promoted' ? 'promoted from an observation' : 'added by an auditor'
})

function when(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

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
  // A different finding is a different document; neither editor stays open
  // across the change, or the next finding opens in a mode nobody chose.
  editingNarrative.value = false
  editingResponse.value = false
  if (id && route.query.finding !== id) void nav.replace('findings', { finding: id })
})
// A finding filtered out from under the selection leaves the detail showing a
// row the list no longer has.
watch(filtered, rows => {
  if (!rows.length || rows.some(item => item.id === selectedId.value)) return
  selectedId.value = rows[0].id
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

async function save(changes?: Partial<AuditFinding>) {
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
      ...changes,
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

/**
 * Confirmation is a decision about the file, not a field on a form: it is
 * written as soon as it is made rather than waiting for whatever else the
 * detail happens to be holding.
 */
async function setConfirmed(value: boolean) {
  if (!selected.value) return
  selected.value.auditor_confirmed = value
  await save({ auditor_confirmed: value })
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
 * Re-read the evidence against the run it has moved to. The hash the finding
 * was drafted from is recomputed on the server, which is the only place that
 * knows what an evidentiary projection covers.
 */
async function reaffirmEvidence() {
  const item = selected.value
  if (!item) return
  reaffirming.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/findings/${item.id}/evidence/reaffirm`)
    await reload(item.id)
    emit('changed')
    toast.add({ severity: 'success', summary: 'Evidence re-affirmed against the current run', life: 2500 })
  } catch (error) { fail('Could not re-affirm the evidence', error) }
  finally { reaffirming.value = false }
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

async function draftFromRcm() {
  generatingFindings.value = true
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Draft all eligible findings from the RCM observations.',
      'act', launchMode.value,
      { command: 'draft_findings', source: 'tab_button' },
    )
    agent.openPanel()
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

function copyMarkdown() {
  const item = selected.value
  if (!item) return
  void navigator.clipboard?.writeText(`# ${item.title}\n\n${item.narrative}`)
    .then(() => toast.add({ severity: 'success', summary: 'Finding copied as Markdown', life: 1800 }))
    .catch(error => fail('Could not copy the finding', error))
}

const menuItems = computed(() => [
  {
    label: 'Generate all findings',
    icon: 'pi pi-sparkles',
    disabled: statusBusy.value || agentBusy.value,
    command: () => void draftFromRcm(),
  },
  { label: 'Finding template', icon: 'pi pi-file-edit', command: () => void openTemplate() },
  {
    label: 'Copy Markdown',
    icon: 'pi pi-copy',
    disabled: !selected.value,
    command: copyMarkdown,
  },
  {
    label: 'Remove finding',
    icon: 'pi pi-trash',
    disabled: !selected.value,
    command: remove,
  },
])

/** The cause is recorded in the narrative, so recording it opens the editor. */
function recordCause() { editingNarrative.value = true }

async function doneEditingNarrative() {
  editingNarrative.value = false
  await save()
}
async function doneEditingResponse() {
  editingResponse.value = false
  await save()
}

function showAnchor(value: EvidenceRef) { anchor.value = value; anchorOpen.value = true }
async function addEvidence(value: EvidenceRef) {
  if (!selected.value || selected.value.evidence_refs.some(item => item.id === value.id)) return
  selected.value.evidence_refs.push(value)
  evidencePicker.value?.hide()
  await save()
}
async function removeEvidence(id: string) {
  if (!selected.value) return
  selected.value.evidence_refs = selected.value.evidence_refs.filter(item => item.id !== id)
  await save()
}
function openPlanning(rcmId: string) {
  void nav.replace('rcm', { rcm: rcmId })
}
/**
 * A test link resolved against the tests this payload already carries. The id
 * prefixes (`DAT-` for data tests, `DT-` for document tests) are too close to
 * parse by hand, so membership decides which surface answers for the id.
 */
type TestLink = { id: string; destination: 'data-tests' | 'doc-tests'; title: string; icon: string; exceptions: number }
function resolveTest(id: string): TestLink | null {
  const dataTest = (data.value?.data_tests ?? []).find(item => item.id === id)
  if (dataTest) {
    return {
      id, destination: 'data-tests', title: dataTest.title, icon: 'pi pi-chart-bar',
      exceptions: dataTest.open_exception_count || dataTest.evaluation?.exception_count || 0,
    }
  }
  const docTest = (data.value?.document_tests ?? []).find(item => item.id === id)
  if (docTest) return { id, destination: 'doc-tests', title: docTest.title, icon: 'pi pi-file-check', exceptions: 0 }
  return null
}
const testLinks = computed(() => (selected.value?.test_refs ?? [])
  .map(resolveTest)
  .filter((link): link is TestLink => link !== null))
/** `push`, not `replace`: leaving a finding for its test has to be walkable back. */
function openTest(id: string) {
  const link = resolveTest(id)
  if (link) void nav.push(link.destination, { test: id })
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
/** Whether one anchor is the one the finding's warning is about. */
function anchorMoved(value: EvidenceRef): boolean {
  return (selected.value?.evidence_warnings ?? []).some(
    warning => warning.includes(`${value.source_kind}:${value.source_id}`),
  )
}
/**
 * The stale strip's sentence, naming the source that moved rather than
 * restating the server's warning list. It is the only place the page says the
 * evidence has drifted.
 */
const staleSentence = computed(() => {
  const item = selected.value
  if (!item?.evidence_warnings?.length) return undefined
  const moved = item.evidence_refs.filter(anchorMoved)
  const names = moved.map(value => value.source_id).join(', ')
  return `${names ? `The evidence this finding cites (${names})` : 'The evidence this finding cites'} has changed since the narrative was drafted. Re-read the condition against the source, then re-affirm the evidence.`
})
</script>

<template>
  <div class="findings">
    <!-- One title, one count sentence, at most one primary. What is
         outstanding takes the primary slot when anything is. -->
    <header class="page-head">
      <h1>Findings</h1>
      <span class="grow" />
      <Button
        label="Draft from the RCM"
        icon="pi pi-sparkles"
        size="small"
        outlined
        severity="secondary"
        :disabled="statusBusy || agentBusy"
        @click="draftFromRcm"
      />
      <Button
        v-if="unconfirmed.length"
        label="Add finding"
        icon="pi pi-plus"
        size="small"
        outlined
        severity="secondary"
        @click="addManual"
      />
      <Button
        v-if="unconfirmed.length"
        :label="`Confirm ${unconfirmed.length}`"
        icon="pi pi-check"
        size="small"
        severity="warn"
        :loading="confirmingAll"
        :disabled="statusBusy"
        @click="confirmAll"
      />
      <Button v-else label="Add finding" icon="pi pi-plus" size="small" @click="addManual" />
      <UiOverflowMenu :items="menuItems" tooltip="More findings actions" />
    </header>

    <UiReviewBar
      v-if="items.length"
      :lanes="status.lanes"
      :chips="FINDING_CHIPS"
      :filters="status.filters"
      allLabel="All findings"
      :total="items.length"
      :filter="statusFilter"
      @filter="statusFilter = ($event as FindingsFilter[])"
    />

    <div v-if="items.length" class="layout">
      <section class="list-panel">
        <div class="list-head">
          <IconField>
            <InputIcon class="pi pi-search" />
            <InputText v-model="search" size="small" placeholder="Search findings" />
          </IconField>
        </div>
        <div class="list-body">
          <FindingsList :findings="filtered" :selectedId="selectedId" @select="selectedId = $event.id" />
        </div>
      </section>

      <section v-if="selected" class="detail">
        <header class="detail-head">
          <div class="detail-copy">
            <p class="eyebrow aw-figure">
              <span class="id">{{ selected.id }}</span>
              · <span class="authorship" :data-agent="selected.source === 'agent'">{{ authorship }}</span>
              <template v-if="when(selected.created)"> · {{ when(selected.created) }}</template>
            </p>
            <InputText v-model="selected.title" class="title-field" unstyled aria-label="Finding title" />
            <p class="lede">
              {{ selected.source === 'agent'
                ? 'Drafted by the assistant. The narrative is copied into the report unchanged.'
                : 'The narrative is copied into the report unchanged.' }}
            </p>
          </div>
          <Select
            v-model="selected.severity"
            :options="SEVERITY_ORDER"
            size="small"
            class="severity-select"
            :data-tone="selected.severity"
            aria-label="Severity"
          />
          <Button label="Save" icon="pi pi-save" size="small" :loading="saving" @click="save()" />
        </header>

        <!-- What is recorded, and what the report can do with it. The two
             checkboxes under the old editor said neither. -->
        <UiVerdictBar :tone="selected.auditor_confirmed ? 'ok' : 'neutral'" :stale="staleSentence">
          <template #found>
            <template v-if="selected.auditor_confirmed">
              <span>Confirmed for reporting</span>
              <span class="meta aw-figure">· {{ when(selected.updated) }}</span>
            </template>
            <span v-else>Not confirmed for reporting</span>
          </template>

          <template #recorded>
            <template v-if="!owed.length">In the report.</template>
            <template v-else>
              Left out of the report until it is supported:
              <template v-for="(item, index) in owed" :key="item.key">
                <span v-if="index" aria-hidden="true"> · </span>
                <span :data-tone="item.tone">{{ item.label }}</span>
              </template>
            </template>
          </template>

          <template #actions>
            <Button
              v-if="selected.evidence_warnings?.length"
              label="Re-affirm"
              icon="pi pi-verified"
              size="small"
              severity="warn"
              :loading="reaffirming"
              @click="reaffirmEvidence"
            />
            <Button
              v-if="!selected.rcm_refs.length"
              label="Link to a risk"
              icon="pi pi-map"
              iconPos="left"
              size="small"
              @click="riskPicker?.toggle($event)"
            />
            <Button
              v-if="selected.auditor_confirmed"
              label="Withdraw confirmation"
              size="small"
              text
              severity="secondary"
              :loading="saving"
              @click="setConfirmed(false)"
            />
            <Button
              v-else
              label="Confirm for reporting"
              icon="pi pi-check"
              size="small"
              :loading="saving"
              @click="setConfirmed(true)"
            />
          </template>
        </UiVerdictBar>

        <div class="body">
          <div class="main">
            <section>
              <div class="section-head">
                <h3 class="aw-label">Narrative</h3>
                <Button
                  :label="editingNarrative ? 'Done' : 'Edit'"
                  :icon="editingNarrative ? 'pi pi-check' : 'pi pi-pencil'"
                  size="small"
                  text
                  severity="secondary"
                  :loading="saving"
                  @click="editingNarrative ? doneEditingNarrative() : (editingNarrative = true)"
                />
              </div>
              <div v-if="editingNarrative" class="narrative-editor">
                <MarkdownEditor v-model="selected.narrative" />
                <label class="cause-flag">
                  <input v-model="selected.cause_pending" type="checkbox" />
                  The root cause is still pending auditor follow-up
                </label>
              </div>
              <FindingNarrative
                v-else
                :markdown="selected.narrative"
                :causePending="selected.cause_pending"
                @recordCause="recordCause"
              />
            </section>

            <section>
              <div class="section-head">
                <h3 class="aw-label">Management response</h3>
                <Button
                  v-if="!editingResponse"
                  label="Record as received"
                  icon="pi pi-pencil"
                  size="small"
                  text
                  severity="secondary"
                  @click="editingResponse = true"
                />
                <Button
                  v-else
                  label="Done"
                  icon="pi pi-check"
                  size="small"
                  text
                  severity="secondary"
                  :loading="saving"
                  @click="doneEditingResponse"
                />
              </div>
              <Textarea
                v-if="editingResponse"
                v-model="selected.management_response"
                rows="4"
                autoResize
                class="response-editor"
                placeholder="Management's own response, recorded as received."
              />
              <p v-else class="response" :class="{ none: !selected.management_response.trim() }">
                {{ selected.management_response.trim() || 'None received.' }}
              </p>
            </section>
          </div>

          <aside class="rail">
            <section>
              <div class="section-head">
                <h3 class="aw-label">Risk</h3>
                <Button
                  v-if="riskLinks.length"
                  label="Change"
                  size="small"
                  text
                  severity="secondary"
                  @click="riskPicker?.toggle($event)"
                />
              </div>
              <template v-if="riskLinks.length">
                <button
                  v-for="link in riskLinks"
                  :key="link.id"
                  type="button"
                  class="card"
                  @click="openPlanning(link.id)"
                >
                  <span class="card-id">{{ link.id }}</span>
                  <span class="clamp">{{ link.risk }}</span>
                </button>
              </template>
              <!-- The one gap the report cannot write around, said where the
                   link is made rather than as a lane count. -->
              <div v-else class="missing">
                <p>Not linked to a risk. The report cannot place this finding in a process until it names the row it answers.</p>
                <Button label="Choose a row" size="small" @click="riskPicker?.toggle($event)" />
              </div>
            </section>

            <section>
              <div class="section-head">
                <h3 class="aw-label">Tests</h3>
                <Button label="Add" icon="pi pi-plus" size="small" text severity="secondary" @click="testPicker?.toggle($event)" />
              </div>
              <button
                v-for="link in testLinks"
                :key="link.id"
                type="button"
                class="card"
                @click="openTest(link.id)"
              >
                <span class="card-top">
                  <span class="card-id"><i :class="link.icon" aria-hidden="true" />{{ link.id }}</span>
                  <span v-if="link.exceptions" class="pill warn aw-figure">{{ plural(link.exceptions, 'exception') }}</span>
                </span>
                <span class="clamp">{{ link.title }}</span>
              </button>
              <p v-if="!testLinks.length" class="none">No test linked.</p>
            </section>

            <section>
              <div class="section-head">
                <h3 class="aw-label">Evidence</h3>
                <Button
                  label="Add"
                  icon="pi pi-plus"
                  size="small"
                  text
                  severity="secondary"
                  :disabled="!availableEvidence.length"
                  @click="evidencePicker?.toggle($event)"
                />
              </div>
              <div v-for="value in selected.evidence_refs" :key="value.id" class="card-row">
                <button type="button" class="card" @click="openEvidence(value)">
                  <span class="card-top">
                    <span class="card-id">{{ value.id }}</span>
                    <span v-if="anchorMoved(value)" class="pill warn">changed</span>
                  </span>
                  <span class="clamp">{{ value.source_kind }} · {{ value.source_id }}</span>
                  <span v-if="value.source_sha1" class="drafted aw-figure">
                    Drafted against {{ value.source_sha1.slice(0, 8) }}
                  </span>
                </button>
                <Button
                  icon="pi pi-times"
                  text
                  rounded
                  severity="danger"
                  size="small"
                  aria-label="Remove evidence link"
                  @click="removeEvidence(value.id)"
                />
              </div>
              <p v-if="!selected.evidence_refs.length" class="none">No evidence anchored.</p>
            </section>

            <section class="provenance">
              <details>
                <summary>Where this came from</summary>
                <ProvenanceRail :key="selected.id" :workspaceId="workspace.id" :artifactRef="`finding:${selected.id}`" />
              </details>
              <p v-if="selected.agent_run_id" class="run-id aw-figure">Run {{ selected.agent_run_id }}</p>
            </section>
          </aside>
        </div>
      </section>
      <UiEmptyState v-else icon="pi pi-flag" title="No finding selected" description="Select a finding or add one." />
    </div>
    <UiEmptyState v-else icon="pi pi-flag" title="Start the findings register" description="Add a finding when fieldwork identifies a reportable issue.">
      <Button label="Add finding" icon="pi pi-plus" @click="addManual" />
    </UiEmptyState>

    <!-- The three multiselects the detail used to carry, moved to where the
         card they fill is read. -->
    <template v-if="selected">
      <Popover ref="riskPicker">
        <Listbox
          v-model="selected.rcm_refs"
          :options="rcmOptions"
          optionLabel="label"
          optionValue="value"
          multiple
          checkmark
          filter
          filterPlaceholder="Search risks"
          class="picker"
          @change="save()"
        />
      </Popover>
      <Popover ref="testPicker">
        <Listbox
          v-model="selected.test_refs"
          :options="testOptions"
          optionLabel="label"
          optionValue="value"
          multiple
          checkmark
          filter
          filterPlaceholder="Search tests"
          class="picker"
          @change="save()"
        />
      </Popover>
      <Popover ref="evidencePicker">
        <div class="picker evidence-picker">
          <button v-for="option in availableEvidence" :key="option.anchor.id" type="button" @click="addEvidence(option.anchor)">
            <i class="pi pi-plus" aria-hidden="true" />{{ option.label }}
          </button>
          <p v-if="!availableEvidence.length" class="none">Nothing further has been captured in fieldwork.</p>
        </div>
      </Popover>
    </template>

    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" />
    <Drawer
      v-model:visible="templateOpen"
      position="right"
      header="Finding template"
      :style="{ width: 'min(45rem, 96vw)' }"
    >
      <p class="template-note">
        The <code>##</code> headings below are the sections every finding must complete before it can
        be confirmed for formal reporting. Comments are guidance for the auditor and the agent, and
        never appear in a finding or the report.
      </p>
      <Textarea v-if="template" v-model="template.markdown" rows="24" spellcheck="false" class="template-editor" />
      <div class="template-foot">
        <Button label="Restore default" severity="secondary" text size="small" @click="saveTemplate(true)" />
        <Button label="Save override" icon="pi pi-save" size="small" @click="saveTemplate(false)" />
      </div>
    </Drawer>
  </div>
</template>

<style scoped>
.findings { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }

.layout { display: grid; grid-template-columns: 18.75rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem; }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.list-head { display: flex; flex-direction: column; gap: .5rem; padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border); }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext) { width: 100%; }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }

.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-width: 100%; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  container: master-detail-content / inline-size;
  overflow-y: auto;
}
/* The column scrolls; nothing in it is squeezed to fit the viewport. Without
   this the verdict bar — a flex child with no intrinsic minimum — collapsed to
   a 2px line under a long narrative. */
.detail > * { flex: none; }
.detail-head { display: flex; align-items: flex-start; gap: .75rem; min-width: 0; }
.detail-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.eyebrow { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.eyebrow .id { font-family: var(--aw-font-mono); font-weight: 600; }
.eyebrow .authorship[data-agent='true'] { color: var(--aw-accent); }
/* The title edits where it is read: a `Title` field in a form above the
   document restated the heading the document already has. */
.title-field {
  width: 100%; padding: .1rem .25rem; margin-left: -.25rem;
  border: 1px solid transparent; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink-strong);
  font: inherit; font-size: var(--aw-text-lg); font-weight: 600; letter-spacing: -0.01em;
}
.title-field:hover { border-color: var(--aw-border); }
.title-field:focus { outline: 0; border-color: var(--aw-teal); background: var(--aw-canvas); }
.lede { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.45; }

.severity-select :deep(.p-select-label) { font-weight: 600; text-transform: capitalize; }
.severity-select[data-tone='critical'] { border-color: var(--aw-danger-line); color: var(--aw-danger-ink); }
.severity-select[data-tone='high'] { border-color: var(--aw-danger-line); color: var(--aw-danger); }
.severity-select[data-tone='medium'] { border-color: var(--aw-warn-line); color: var(--aw-warn-ink); }
.severity-select[data-tone='low'] { border-color: var(--aw-low); color: var(--aw-low-ink); }

.meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }
[data-tone='bad'] { color: var(--aw-danger); }
[data-tone='warn'] { color: var(--aw-warn-ink); }

.body { display: grid; grid-template-columns: minmax(0, 1fr) 20rem; gap: 1.375rem; min-width: 0; }
.main { display: flex; flex-direction: column; gap: 1.25rem; min-width: 0; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: .5rem; min-height: 1.75rem; }
.section-head h3 { margin: 0; }

.narrative-editor { display: flex; flex-direction: column; gap: .6rem; }
.narrative-editor :deep(.markdown-editor) { min-height: 24rem; }
.cause-flag { display: flex; align-items: center; gap: .4rem; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }
.response-editor { width: 100%; font-size: var(--aw-text-sm); }
.response {
  margin: 0; padding: .625rem .75rem;
  border-radius: var(--aw-radius-control); background: var(--aw-raised);
  color: var(--aw-ink); font-size: var(--aw-text-sm); line-height: 1.5; white-space: pre-wrap;
}
.response.none, .none { color: var(--aw-muted); }
.none { margin: 0; font-size: var(--aw-text-sm); }

.rail { display: flex; flex-direction: column; gap: 1.25rem; min-width: 0; }
.rail section { display: flex; flex-direction: column; gap: .4rem; min-width: 0; }
.card {
  display: flex; flex-direction: column; gap: .2rem;
  width: 100%; min-width: 0;
  padding: .5rem .625rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-ink);
  font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer;
}
.card:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.card:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.card-top { display: flex; align-items: center; justify-content: space-between; gap: .4rem; }
.card-id { display: inline-flex; align-items: center; gap: .3rem; color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 700; }
.card-row { display: flex; align-items: flex-start; gap: .2rem; }
.card-row .card { flex: 1; }
.clamp { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; color: var(--aw-ink-soft); line-height: 1.4; }
.drafted { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.pill {
  flex: none; padding: 0 .375rem;
  border-radius: var(--aw-radius-pill);
  font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
}
.pill.warn { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }

.missing {
  display: flex; flex-direction: column; align-items: flex-start; gap: .5rem;
  padding: .625rem .75rem;
  border: 1px dashed var(--aw-danger-line); border-radius: var(--aw-radius-control);
  background: var(--aw-danger-soft);
}
.missing p { margin: 0; color: var(--aw-danger-ink); font-size: var(--aw-text-sm); line-height: 1.45; }

.provenance summary { color: var(--aw-teal); font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer; }
.provenance :deep(.provenance-rail) { margin-top: .5rem; }
.run-id { margin: .4rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-family: var(--aw-font-mono); }

.picker { width: min(26rem, 80vw); }
.picker :deep(.p-listbox-list-container) { max-height: 18rem; }
.evidence-picker { display: flex; flex-direction: column; gap: .2rem; max-height: 20rem; overflow-y: auto; }
.evidence-picker button {
  display: flex; align-items: center; gap: .4rem;
  padding: .4rem .5rem; border: 0; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink); font: inherit; font-size: var(--aw-text-sm);
  text-align: left; cursor: pointer;
}
.evidence-picker button:hover { background: var(--aw-raised); }

.template-note { margin: 0 0 .6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.5; }
.template-editor { width: 100%; font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); }
.template-foot { display: flex; justify-content: flex-end; gap: .5rem; margin-top: .75rem; }

@container workspace-panel (max-width: 60rem) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .list-body { max-height: 18rem; }
}
@container master-detail-content (max-width: 46rem) {
  .body { grid-template-columns: minmax(0, 1fr); }
}
</style>
