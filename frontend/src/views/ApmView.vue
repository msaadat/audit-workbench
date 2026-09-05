<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import { plural } from '../format'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import type {
  ArtifactProvenance, AuditDocument, MarkdownTemplate, PlanningPayload,
  WorkspaceSummary,
} from '../types'
import MarkdownEditor from '../components/MarkdownEditor.vue'
import UiDocumentPage from '../components/ui/UiDocumentPage.vue'
import UiEmptyState from '../components/ui/UiEmptyState.vue'
import UiMarkdownDocument from '../components/ui/UiMarkdownDocument.vue'
import UiOverflowMenu from '../components/ui/UiOverflowMenu.vue'
import { markdownOutline } from '../components/ui/markdownOutline'
import { stamp } from '../components/report/reportStatus'

/**
 * The planning memorandum: the document, who wrote it, and what still rests
 * on the version they wrote.
 *
 * It was a mode of `PlanningTab` — an always-open Markdown editor beside a
 * provenance rail, with a toolbar of six equal buttons over it. Two things
 * were missing and neither was in the editor: what the memorandum *says*
 * (2,900 words with no way to reach a section) and the fact that the cycle
 * and the thirty-two risks were derived from a particular version of it, so
 * editing it can silently put both out of date.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()
const toast = useToast()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const assistantChat = useAssistantChat(props.workspace.id)

const data = ref<PlanningPayload | null>(null)
const provenance = ref<ArtifactProvenance | null>(null)
const documents = ref<AuditDocument[]>([])
const saving = ref(false)
const exporting = ref(false)
const importing = ref(false)
const editing = ref(false)
const templateOpen = ref(false)
const template = ref<MarkdownTemplate | null>(null)
const importInput = ref<HTMLInputElement>()

const planning = computed(() => data.value?.planning ?? null)
const markdown = computed(() => planning.value?.apm_markdown ?? '')
const hasContent = computed(() => Boolean(markdown.value.trim()))
const entries = computed(() => markdownOutline(markdown.value))
const agentBusy = computed(() => agent.isActive.value || !agent.state.status?.configured)



const attributed = computed(() =>
  provenance.value?.state === 'attributed' ? provenance.value : null)
const context = computed(() => attributed.value?.context ?? null)
const selections = computed(() => context.value?.selections ?? [])

/**
 * The documents the drafting step actually read, named as files.
 *
 * A selection's `source_id` is the selector's id, not the artifact's — the
 * artifact is in `source_ref` as `document:<id>`, which is what resolves
 * against the document list.
 */
function refId(ref: string | null | undefined): string {
  return String(ref ?? '').split(':').slice(1).join(':')
}
const readDocuments = computed(() => selections.value
  .filter(item => String(item.source_ref ?? '').startsWith('document:'))
  .map(item => {
    const id = refId(item.source_ref)
    const record = documents.value.find(doc => doc.id === id)
    return {
      id,
      name: record?.title || record?.file || item.source_ref,
      category: record?.category ?? '',
    }
  }))

/** The manifest names its groups in the plural; a count of one must not. */
const SOURCE_NOUNS: Record<string, [string, string]> = {
  tables: ['table', 'tables'],
  templates: ['template', 'templates'],
  analyses: ['analysis', 'analyses'],
  artifacts: ['artifact', 'artifacts'],
  documents: ['document', 'documents'],
  planning: ['planning record', 'planning records'],
}
const otherSources = computed(() => {
  const groups = new Map<string, number>()
  for (const item of selections.value) {
    if (String(item.source_ref ?? '').startsWith('document:')) continue
    groups.set(item.source_type, (groups.get(item.source_type) ?? 0) + 1)
  }
  return [...groups].map(([type, count]) => {
    const [one, many] = SOURCE_NOUNS[type] ?? [type.replace(/s$/, ''), type]
    return { type, count, label: plural(count, one, many) }
  })
})
const omissions = computed(() => {
  const groups = new Map<string, number>()
  for (const item of context.value?.omissions ?? []) {
    const reason = /did not match|selector item limit/i.test(item.reason ?? '')
      ? "outside this step's scope"
      : /limit/i.test(item.reason ?? '') ? 'held back by a size limit' : 'not available'
    groups.set(reason, (groups.get(reason) ?? 0) + 1)
  }
  return [...groups].map(([reason, count]) => ({ reason, count }))
})

/**
 * Whether the cycle still describes the memorandum as it now reads.
 *
 * Two hashes compared as strings — the backend computes both, because it owns
 * what the identity of a memorandum covers.
 */
const cycleStale = computed(() => {
  const cycle = planning.value?.cycle
  const current = planning.value?.apm_sha1
  return Boolean(cycle && current && cycle.apm_sha1 && cycle.apm_sha1 !== current)
})

function fail(summary: string, error: unknown) {
  toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
}

async function reload() {
  data.value = await api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`)
  // Provenance and the document list are what the rail is made of; neither
  // blocks the memorandum, so a failure there leaves the page readable.
  void api.get<ArtifactProvenance>(`/api/workspaces/${props.workspace.id}/provenance?artifact=planning:apm`)
    .then(value => { provenance.value = value })
    .catch(() => { provenance.value = null })
  void api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)
    .then(value => { documents.value = value.items ?? [] })
    .catch(() => { documents.value = [] })
}
onMounted(() => void reload().catch(error => fail('Could not load the memorandum', error)))
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  void reload().catch(error => fail('Could not refresh the memorandum', error))
})
onUnmounted(unsubscribe)

async function save(showToast = true) {
  if (!data.value) return
  saving.value = true
  try {
    data.value.planning = await api.patch(`/api/workspaces/${props.workspace.id}/planning`, {
      apm_markdown: data.value.planning.apm_markdown,
    })
    emit('changed')
    if (showToast) toast.add({ severity: 'success', summary: 'Memorandum saved', life: 1800 })
  } catch (error) { fail('Could not save the memorandum', error) }
  finally { saving.value = false }
}
async function doneEditing() {
  editing.value = false
  await save()
}

async function generate() {
  try {
    await assistantChat.createChat()
    await assistantChat.send(
      'Update the planning context and APM, then create or reconcile the RCM and the Document and Data Tests that cover it. Do not create a separate audit program.',
      'act', agent.launchMode.value,
      { command: 'plan_engagement', source: 'tab_button' },
    )
    agent.openPanel()
  } catch (error) { fail('Could not start planning', error) }
}

async function exportApm() {
  exporting.value = true
  try { await api.downloadGet(`/api/workspaces/${props.workspace.id}/planning/apm/export`, `${props.workspace.name}_APM.md`) }
  catch (error) { fail('Could not export the memorandum', error) }
  finally { exporting.value = false }
}
function triggerImport() { importInput.value?.click() }
async function importApm(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''
  importing.value = true
  try {
    data.value!.planning = await api.uploadOne(`/api/workspaces/${props.workspace.id}/planning/apm/import`, file)
    emit('changed')
    toast.add({ severity: 'success', summary: 'Memorandum imported', life: 1800 })
  } catch (error) { fail('Could not import the memorandum', error) }
  finally { importing.value = false }
}

async function copy() {
  await navigator.clipboard.writeText(markdown.value)
  toast.add({ severity: 'success', summary: 'Markdown copied', life: 1600 })
}

async function openTemplate() {
  try { template.value = await api.get(`/api/workspaces/${props.workspace.id}/templates/apm`); templateOpen.value = true }
  catch (error) { fail('Could not load the APM template', error) }
}
async function saveTemplate(reset = false) {
  if (!template.value) return
  try {
    template.value = await api.put(`/api/workspaces/${props.workspace.id}/templates/apm`, reset ? { reset: true } : { markdown: template.value.markdown })
    toast.add({ severity: 'success', summary: reset ? 'Default APM template restored' : 'APM template saved', life: 1800 })
  } catch (error) { fail('Could not save the APM template', error) }
}

function startApm() {
  if (data.value) data.value.planning.apm_markdown = '# Audit planning memorandum\n\n'
  editing.value = true
}

const menuItems = computed(() => [
  { label: 'Import Markdown', icon: 'pi pi-upload', disabled: importing.value, command: () => triggerImport() },
  { label: 'APM template', icon: 'pi pi-file-edit', command: () => void openTemplate() },
  { label: 'Copy Markdown', icon: 'pi pi-copy', disabled: !hasContent.value, command: () => void copy() },
])

const rcmCount = computed(() => data.value?.rcm.length ?? 0)
const processes = computed(() => new Set((data.value?.rcm ?? []).map(row => row.process).filter(Boolean)).size)
</script>

<template>
  <div v-if="data" class="apm">
    <header class="page-head">
      <h1>Audit planning memorandum</h1>
      <span class="grow" />
      <Button
        v-if="hasContent"
        :label="editing ? 'Done' : 'Edit'"
        :icon="editing ? 'pi pi-check' : 'pi pi-pencil'"
        size="small"
        outlined
        severity="secondary"
        :loading="saving"
        @click="editing ? doneEditing() : (editing = true)"
      />
      <Button
        v-if="hasContent"
        label="Export"
        icon="pi pi-download"
        size="small"
        outlined
        severity="secondary"
        :loading="exporting"
        @click="exportApm"
      />
      <Button v-if="editing" label="Save" icon="pi pi-save" size="small" :loading="saving" @click="save(true)" />
      <Button
        v-else
        :label="hasContent ? 'Regenerate' : 'Generate planning drafts'"
        icon="pi pi-sparkles"
        size="small"
        :disabled="agentBusy"
        @click="generate"
      />
      <UiOverflowMenu :items="menuItems" tooltip="More memorandum actions" />
    </header>
    <input ref="importInput" type="file" accept=".md,.markdown,.txt" hidden @change="importApm" />

    <UiEmptyState
      v-if="!hasContent"
      icon="pi pi-map"
      title="No planning memorandum yet"
      description="The assistant drafts it from the engagement material — the documents you imported, the planning context, and the risks already recorded. You can also write it yourself."
    >
      <Button label="Generate planning drafts" icon="pi pi-sparkles" :disabled="agentBusy" @click="generate" />
      <Button label="Start writing" icon="pi pi-pencil" outlined @click="startApm" />
    </UiEmptyState>

    <template v-else>
      <UiDocumentPage
        :entries="entries"
        outlineLabel="On this memorandum"
        railWidth="18.75rem"
        :fills="editing"
      >
        <div v-if="editing" class="editor">
          <MarkdownEditor v-model="data.planning.apm_markdown" placeholder="Write the planning memorandum, or generate a draft from the engagement material." />
        </div>
        <UiMarkdownDocument
          v-else
          :markdown="markdown"
          :eyebrow="`Audit planning memorandum · ${workspace.name}`"
        />

        <template #rail>
          <section class="card">
            <h3 class="aw-label">Where this came from</h3>
            <template v-if="attributed">
              <button
                v-for="document in readDocuments.slice(0, 4)"
                :key="document.id"
                type="button"
                class="row"
                @click="nav.push('documents', { document: document.id })"
              >
                <i class="pi pi-file" aria-hidden="true" />
                <span class="name">{{ document.name }}</span>
                <span v-if="document.category" class="tag">{{ document.category }}</span>
              </button>
              <p v-if="readDocuments.length > 4" class="more">
                {{ plural(readDocuments.length - 4, 'more document') }}
              </p>
              <p v-for="source in otherSources" :key="source.type" class="line">{{ source.label }}</p>
              <p v-if="context?.supplied_size" class="total aw-figure">
                {{ plural(context.supplied_size.items, 'source') }} ·
                {{ context.supplied_size.characters.toLocaleString() }} characters supplied
              </p>
            </template>
            <p v-else class="none">No context manifest was recorded for this memorandum.</p>
          </section>

          <section v-if="omissions.length" class="card">
            <h3 class="aw-label">Not supplied</h3>
            <p v-for="item in omissions" :key="item.reason" class="line">
              <b class="aw-figure">{{ item.count }}</b> {{ item.count === 1 ? 'source' : 'sources' }} · {{ item.reason }}
            </p>
          </section>

          <!-- Who wrote it and when. It was a band across the top of the
               page restating what this column already answers — the sources it
               read are the card above, and what was derived from it the card
               below. -->
          <section class="card">
            <h3 class="aw-label">Written</h3>
            <dl class="stats">
              <div>
                <dt>Drafted</dt>
                <dd :data-tone="attributed ? 'agent' : undefined">
                  {{ attributed ? `assistant · ${stamp(attributed.unit.finished_at)}` : 'by an auditor' }}
                </dd>
              </div>
              <div v-if="planning?.updated">
                <dt>Edited</dt>
                <dd>{{ planning.created_by === 'user' ? 'auditor · ' : '' }}{{ stamp(planning.updated) }}</dd>
              </div>
              <template v-if="attributed">
                <div><dt>Step</dt><dd>{{ attributed.unit.stage_title || attributed.unit.title }}</dd></div>
                <div><dt>Model</dt><dd>{{ attributed.model.model }}</dd></div>
                <div v-if="attributed.model.usage?.calls"><dt>Calls</dt><dd>{{ attributed.model.usage.calls }}</dd></div>
                <div v-if="attributed.receipt.workspace_revision_after">
                  <dt>Committed revision</dt><dd>{{ attributed.receipt.workspace_revision_after }}</dd>
                </div>
              </template>
            </dl>
          </section>

          <section class="card">
            <h3 class="aw-label">What this feeds</h3>
            <button type="button" class="feeds" :data-tone="cycleStale ? 'warn' : 'ok'" @click="nav.push('cycle')">
              <span class="name">{{ planning?.cycle ? `${plural(planning.cycle.steps.length, 'step')}` : 'No cycle yet' }}</span>
              <span class="note">{{ planning?.cycle
                ? (cycleStale
                  ? 'derived from an earlier version — regenerate it, or the risks are planned against text that no longer exists'
                  : 'derived from this version')
                : 'the cycle is drafted from this memorandum' }}</span>
            </button>
            <button type="button" class="feeds" data-tone="neutral" @click="nav.push('rcm')">
              <span class="name">{{ plural(rcmCount, 'risk') }}</span>
              <span class="note">{{ plural(processes, 'process', 'processes') }}</span>
            </button>
          </section>
        </template>
      </UiDocumentPage>
    </template>

    <Drawer v-model:visible="templateOpen" position="right" header="APM template" :style="{ width: 'min(45rem, 96vw)' }">
      <p class="template-note">Workspace override · placeholders use <code v-pre>{{name}}</code>.</p>
      <Textarea v-if="template" v-model="template.markdown" rows="24" spellcheck="false" class="template-editor" />
      <div class="template-foot">
        <Button label="Restore default" severity="secondary" text size="small" @click="saveTemplate(true)" />
        <Button label="Save override" icon="pi pi-save" size="small" @click="saveTemplate(false)" />
      </div>
    </Drawer>
  </div>
</template>

<style scoped>
.apm { display: flex; flex-direction: column; gap: .75rem; min-width: 0; max-width: 100%; min-height: 0; height: 100%; }

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem; }
.page-head h1 { margin: 0; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; color: var(--aw-ink-strong); }
.headline { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.grow { flex: 1; }
.meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }

/* The editor fills the column and scrolls inside itself. Given a fixed height
   inside a scrolling column it produced two scrollbars for one text, and the
   outer one moved the editor rather than the document in it. */
.editor { display: flex; flex-direction: column; min-height: 24rem; }
.editor :deep(.markdown-editor) { flex: 1; min-height: 0; overflow-y: auto; }

.card { display: flex; flex-direction: column; gap: .3rem; min-width: 0; }
.card h3 { margin: 0 0 .1rem; }
.row {
  display: flex; align-items: center; gap: .4rem;
  width: 100%; min-width: 0;
  padding: .35rem .5rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-ink);
  font: inherit; font-size: var(--aw-text-xs); text-align: left; cursor: pointer;
}
.row:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.row .pi { color: var(--aw-muted); font-size: .7rem; }
.row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag {
  flex: none; padding: 0 .3rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-raised); color: var(--aw-muted);
  font-size: var(--aw-text-2xs); text-transform: uppercase; letter-spacing: .04em;
}
.more, .line, .none { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.5; }
.line b { color: var(--aw-ink); }
.total { margin: .25rem 0 0; color: var(--aw-muted-strong); font-size: var(--aw-text-2xs); }

.stats { display: grid; gap: 0; margin: 0; }
.stats > div { display: flex; align-items: baseline; justify-content: space-between; gap: .75rem; padding: .25rem 0; border-bottom: 1px solid var(--aw-border); }
.stats > div:last-child { border-bottom: 0; }
.stats dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats dd { margin: 0; color: var(--aw-ink); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; text-align: right; }
.stats dd[data-tone='agent'] { color: var(--aw-accent); }

.feeds {
  display: flex; flex-direction: column; gap: .1rem;
  width: 100%; min-width: 0;
  padding: .4rem .55rem;
  border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel); font: inherit; text-align: left; cursor: pointer;
}
.feeds:hover { border-color: var(--aw-teal-line); border-left-color: var(--aw-teal); }
.feeds[data-tone='ok'] { border-left-color: var(--aw-ok); }
.feeds[data-tone='warn'] { border-left-color: var(--aw-warn); }
.feeds .name { color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 600; }
.feeds .note { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.feeds[data-tone='warn'] .note { color: var(--aw-warn-ink); }

.template-note { margin: 0 0 .6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.template-editor { width: 100%; font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); }
.template-foot { display: flex; justify-content: flex-end; gap: .5rem; margin-top: .75rem; }
</style>
