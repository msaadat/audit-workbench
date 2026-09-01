<script setup lang="ts">
/**
 * Add files, upload them, done.
 *
 * There used to be a Review step between upload and completion, where an
 * auditor set each file's route, name and *document category*. Only the last of
 * those was ever a judgement, and it was being made from a filename — which is
 * the one input that cannot settle it. What a document is to this engagement is
 * now read from its opening page by `documents.categorized`, after import, and
 * shows in the Record spine as Document classification.
 *
 * What the step also carried, and what happens to it now: the route is
 * deterministic on the file suffix and reported in the summary; the name is a
 * slug, renameable where documents are listed; duplicates are already left out
 * automatically. The one thing it surfaced that nothing else did is a file the
 * local parser could not read, and those are named in the summary rather than
 * folded into a count.
 */
import { computed, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import ProgressBar from 'primevue/progressbar'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import { collectDroppedFiles, type StagedFile } from '../composables/useFileDrop'
import type { IntakeBatch } from '../types'
import PostImportPlanningOffer from './PostImportPlanningOffer.vue'

const props = defineProps<{ workspaceId: string; modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  imported: []
  'planning-started': []
}>()
const agent = useAgentRun(props.workspaceId)
const { launchMode } = agent

const STEPS = ['Add files', 'Upload', 'Complete'] as const
const COMPLETE_STEP = STEPS.length

const step = ref(1)
const staged = ref<StagedFile[]>([])
const dragActive = ref(false)
const batch = ref<IntakeBatch | null>(null)
const busy = ref(false)
const error = ref('')
const uploaded = ref(0)

const uploadTotal = computed(() => batch.value?.items.filter((item) => item.needs_upload).length ?? 0)
const progress = computed(() => uploadTotal.value ? Math.round(uploaded.value / uploadTotal.value * 100) : 100)
const uploadedItems = computed(() => batch.value?.items.filter((item) => item.uploaded) ?? [])
/** Files the local parser could not read. Named, never counted. */
const unreadable = computed(() => uploadedItems.value.filter((item) => (
  item.error || item.classification?.route === 'unsupported'
)))
const routeCounts = computed(() => {
  const counts = { table: 0, document: 0, excluded: 0 }
  for (const item of uploadedItems.value) {
    const classification = item.classification
    if (!classification || classification.proposed_action === 'ignore'
      || classification.route === 'ignore' || classification.route === 'unsupported') {
      counts.excluded += 1
    } else if (classification.route === 'table') {
      counts.table += 1
    } else if (classification.route === 'document') {
      counts.document += 1
    }
  }
  return counts
})
const planningAction = computed(() => (
  batch.value?.suggested_actions?.find(action => action.agent_kind === 'planning') ?? null
))
// Everything from a single dropped/selected folder keeps that folder as its
// incremental source; loose files and mixed batches share "Direct uploads".
const rootName = computed(() => {
  const roots = new Set(staged.value.map(({ relativePath }) => relativePath.includes('/') ? relativePath.split('/')[0] : ''))
  return roots.size === 1 ? [...roots][0] : ''
})
const stagedSize = computed(() => formatBytes(staged.value.reduce((total, { file }) => total + file.size, 0)))

watch(() => props.modelValue, (open) => {
  if (!open) return
  if (!agent.state.status) void agent.refreshStatus()
  // A finished batch stays visible until close; reopening starts fresh.
  if (step.value === COMPLETE_STEP) reset()
})

function addStaged(files: StagedFile[]) {
  if (!files.length) return
  const merged = new Map(staged.value.map((entry) => [entry.relativePath, entry]))
  for (const entry of files) merged.set(entry.relativePath, entry)
  staged.value = [...merged.values()]
  error.value = ''
}

function stageExternal(files: StagedFile[]) {
  // A workspace-wide drop during an active upload must not clobber it.
  if (step.value === COMPLETE_STEP) reset()
  if (step.value === 1) addStaged(files)
}
defineExpose({ stageExternal })

/** Enter and Space open the file picker the label wraps. */
function activatePicker(event: KeyboardEvent) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  ;(event.currentTarget as HTMLElement).querySelector('input')?.click()
}

function chooseFiles(event: Event) {
  const input = event.target as HTMLInputElement
  addStaged(Array.from(input.files ?? []).map((file) => ({ file, relativePath: file.name })))
  input.value = ''
}

function chooseFolder(event: Event) {
  const input = event.target as HTMLInputElement
  addStaged(Array.from(input.files ?? []).map((file) => ({ file, relativePath: relativePath(file) })))
  input.value = ''
}

async function onDrop(event: DragEvent) {
  dragActive.value = false
  addStaged(await collectDroppedFiles(event))
}

function relativePath(file: File): string {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
}

function fileName(path: string): string {
  return path.split('/').pop() || path
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

async function startImport() {
  if (!staged.value.length || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const manifest = staged.value.map(({ file, relativePath: path }) => ({
      relative_path: path,
      size: file.size,
      last_modified: file.lastModified,
      mime: file.type,
    }))
    const compared = await api.post<{ batch: IntakeBatch; upload_paths: string[] }>(
      `/api/workspaces/${props.workspaceId}/folder-imports`,
      { root_name: rootName.value, manifest, mode: launchMode.value },
    )
    batch.value = compared.batch
    step.value = 2
    uploaded.value = 0
    const requested = new Set(compared.upload_paths)
    for (const { file, relativePath: path } of staged.value) {
      if (!requested.has(path)) continue
      await api.uploadOne(
        `/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}/files`,
        file,
        { relative_path: path },
      )
      uploaded.value += 1
    }
    batch.value = await api.post<IntakeBatch>(
      `/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}/complete-upload`,
    )
    // Nothing left to decide: routing is settled by the suffix and the category
    // is read after import. Apply immediately rather than staging a review of
    // answers the auditor cannot usefully change.
    batch.value = await api.post<IntakeBatch>(
      `/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}/apply`,
      { decisions: [] },
    )
    step.value = COMPLETE_STEP
    emit('imported')
  } catch (cause) {
    error.value = String(cause)
    if (batch.value?.status === 'uploading') {
      try {
        await api.del(`/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}`)
      } catch {
        /* A later selection creates a fresh durable batch. */
      }
    }
    step.value = 1
    batch.value = null
    uploaded.value = 0
  } finally {
    busy.value = false
  }
}

function close() {
  if (busy.value) return
  emit('update:modelValue', false)
}

function reset() {
  step.value = 1
  staged.value = []
  batch.value = null
  uploaded.value = 0
  error.value = ''
}
</script>

<template>
  <Dialog :visible="modelValue" modal header="Import files and folders" class="folder-import-dialog" :style="{ width: 'min(94vw, 78rem)' }" @update:visible="close">
    <div v-if="error" class="inline-error"><i class="pi pi-exclamation-triangle" />{{ error }}</div>
    <nav class="wizard-progress" aria-label="Import progress"><span v-for="(label,index) in STEPS" :key="label" :class="{ active: step === index + 1, done: step > index + 1 }"><i>{{ index + 1 }}</i>{{ label }}</span></nav>

    <section v-if="step === 1" class="select-step">
      <div
        class="dropzone"
        :class="{ active: dragActive }"
        @dragover.prevent="dragActive = true"
        @dragleave.prevent="dragActive = false"
        @drop.prevent="onDrop"
      >
        <i class="pi pi-cloud-upload" />
        <strong>Drop files or a folder here</strong>
        <span>Spreadsheets and CSVs become data tables; PDFs, Word files and images become documents. What each document is gets read from its opening page after import.</span>
        <div class="picker-actions">
          <label class="picker-button" tabindex="0" @keydown="activatePicker">
            <i class="pi pi-file" />Choose files
            <input type="file" multiple @change="chooseFiles" />
          </label>
          <label class="picker-button" tabindex="0" @keydown="activatePicker">
            <i class="pi pi-folder-open" />Choose a folder
            <input type="file" webkitdirectory multiple @change="chooseFolder" />
          </label>
        </div>
      </div>
      <div v-if="staged.length" class="staged-summary">
        <i class="pi pi-check-circle" />
        <span class="staged-copy">
          <strong>{{ staged.length }} file{{ staged.length === 1 ? '' : 's' }} ready</strong>
          <small>{{ rootName ? `Folder "${rootName}"` : 'Selected files' }} · {{ stagedSize }}. Unchanged files are skipped automatically.</small>
        </span>
        <Button label="Clear" text size="small" severity="secondary" :disabled="busy" @click="staged = []" />
        <Button :label="`Import ${staged.length} file${staged.length === 1 ? '' : 's'}`" icon="pi pi-arrow-right" iconPos="right" :loading="busy" @click="startImport" />
      </div>
    </section>

    <section v-else-if="step === 2" class="upload-step">
      <h3>Uploading</h3>
      <ProgressBar :value="progress" />
      <p class="muted">{{ uploaded }} of {{ uploadTotal }} file{{ uploadTotal === 1 ? '' : 's' }} uploaded.</p>
    </section>

    <section v-else class="summary-step">
      <h3>Import complete</h3>
      <div class="route-summary" aria-label="What was imported">
        <span><i class="pi pi-table" /><strong>{{ routeCounts.table }}</strong> data {{ routeCounts.table === 1 ? 'table' : 'tables' }}</span>
        <span><i class="pi pi-file" /><strong>{{ routeCounts.document }}</strong> {{ routeCounts.document === 1 ? 'document' : 'documents' }}</span>
        <span><i class="pi pi-minus-circle" /><strong>{{ routeCounts.excluded }}</strong> left out</span>
      </div>
      <div class="summary-cards" v-if="batch?.summary">
        <span><strong>{{ batch.summary.imported }}</strong> imported</span>
        <span><strong>{{ batch.summary.unchanged }}</strong> unchanged</span>
        <span><strong>{{ batch.summary.ignored }}</strong> ignored</span>
      </div>
      <div v-if="unreadable.length" class="unreadable">
        <i class="pi pi-exclamation-triangle" />
        <span>
          <strong>{{ unreadable.length }} file{{ unreadable.length === 1 ? '' : 's' }} could not be read and {{ unreadable.length === 1 ? 'was' : 'were' }} left out</strong>
          <small v-for="item in unreadable" :key="item.id">
            {{ fileName(item.relative_path) }}<template v-if="item.error || item.classification?.rationale"> — {{ item.error || item.classification?.rationale }}</template>
          </small>
        </span>
      </div>
      <div v-if="batch?.indexing_job?.document_ids.length" class="selection-summary">
        <i class="pi pi-spin pi-spinner" />
        <span><strong>Search indexing continues in the background</strong><small>{{ batch.indexing_job.document_ids.length }} imported document{{ batch.indexing_job.document_ids.length === 1 ? '' : 's' }} will become searchable automatically.</small></span>
      </div>
      <PostImportPlanningOffer
        v-if="planningAction"
        :workspaceId="workspaceId"
        :action="planningAction"
        @planning-started="emit('planning-started'); close()"
      />
      <div class="review-actions"><Button label="Import more" severity="secondary" @click="reset" /><Button label="Done" @click="close" /></div>
    </section>
  </Dialog>
</template>

<style scoped>
.select-step { display: grid; gap: 1rem; max-width: 46rem; margin: auto; }
.wizard-progress { display:grid; grid-template-columns:repeat(3,1fr); margin:-.25rem 0 1rem; border-bottom:1px solid var(--aw-border) }.wizard-progress span { display:flex; justify-content:center; align-items:center; gap:.4rem; padding:.55rem; color:var(--aw-muted); font-size:var(--aw-text-xs); font-weight:700 }.wizard-progress i { display:grid; place-items:center; width:1.35rem; height:1.35rem; border-radius:var(--aw-radius-pill); background:var(--aw-raised); font-style:normal }.wizard-progress span.active { color:var(--aw-teal); border-bottom:2px solid var(--aw-teal) }.wizard-progress span.done i,.wizard-progress span.active i { color:white; background:var(--aw-teal) }
.dropzone { display: flex; flex-direction: column; align-items: center; gap: .45rem; padding: 2.2rem 1.5rem; border: 2px dashed var(--aw-border-strong); border-radius: var(--aw-radius-surface); background: var(--aw-canvas); text-align: center; transition: border-color .15s, background .15s; }
.dropzone.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.dropzone > i { font-size: var(--aw-text-3xl); color: var(--aw-teal); }
.dropzone > span { max-width: 30rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.picker-actions { display: flex; gap: .65rem; margin-top: .7rem; flex-wrap: wrap; justify-content: center; }
.picker-button { position: relative; display: inline-flex; align-items: center; gap: .45rem; padding: .5rem .95rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer; transition: border-color .15s, color .15s; }
.picker-button:hover { border-color: var(--aw-teal); color: var(--aw-teal); }
.picker-button i { color: var(--aw-teal); }
.picker-button input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.staged-summary { display: flex; align-items: center; gap: .7rem; padding: .7rem .85rem; border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-control); background: var(--aw-teal-soft); }
.staged-summary > i { color: var(--aw-teal); }
.staged-copy { display: grid; flex: 1; gap: .15rem; min-width: 0; }
.staged-copy small { color: var(--aw-muted); }
.selection-summary { display: flex; align-items: center; gap: .65rem; padding: .7rem .85rem; border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-control); background: var(--aw-teal-soft); }
.selection-summary > i { color: var(--aw-teal); }
.selection-summary span { display: grid; gap: .15rem; }
.selection-summary small { color: var(--aw-muted); }
.unreadable { display: flex; gap: .65rem; padding: .7rem .85rem; border: 1px solid var(--aw-warn-line, var(--aw-border-strong)); border-radius: var(--aw-radius-control); background: var(--aw-warn-soft, var(--aw-raised)); }
.unreadable > i { color: var(--aw-danger); }
.unreadable span { display: grid; gap: .15rem; min-width: 0; }
.unreadable small { color: var(--aw-muted); overflow-wrap: anywhere; }
.inline-error { display: flex; gap: .45rem; padding: .65rem; margin-bottom: .8rem; color: var(--aw-danger); background: var(--aw-danger-soft); border-radius: var(--aw-radius-control); }
.upload-step { max-width: 40rem; margin: 3rem auto; text-align: center; }
.muted { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.summary-step { display: grid; gap: 1rem; max-width: 46rem; margin: auto; }
.summary-cards { display: flex; gap: 1.2rem; flex-wrap: wrap; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.summary-cards strong { color: var(--aw-ink); font-size: var(--aw-text-base); }
.route-summary { display: flex; gap: 1.2rem; flex-wrap: wrap; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.route-summary i { margin-right: .35rem; color: var(--aw-teal); }
.route-summary strong { color: var(--aw-ink); }
.review-actions { display: flex; gap: .6rem; justify-content: flex-end; align-items: center; }
</style>
