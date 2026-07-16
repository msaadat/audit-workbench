<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import ProgressBar from 'primevue/progressbar'
import Tag from 'primevue/tag'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type {
  AgentDecision,
  IntakeBatch,
  IntakeBatchItem,
  IntakeClassification,
} from '../types'
import PostImportPlanningOffer from './PostImportPlanningOffer.vue'

const props = defineProps<{ workspaceId: string; modelValue: boolean; documentAiEnabled?: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  imported: []
  'planning-started': []
  'settings-changed': []
}>()
const agent = useAgentRun(props.workspaceId)
const { launchMode } = agent

const step = ref(1)
const files = ref<File[]>([])
const folderName = ref('')
const batch = ref<IntakeBatch | null>(null)
const busy = ref(false)
const error = ref('')
const uploaded = ref(0)
const edits = ref<Record<string, IntakeClassification>>({})
const showAll = ref(false)

const routeOptions = [
  { label: 'Data table', value: 'table' },
  { label: 'Document', value: 'document' },
  { label: 'Unsupported file', value: 'unsupported' },
  { label: 'Leave out', value: 'ignore' },
]
const documentCategories = ['background', 'policy', 'regulation', 'contract', 'minutes', 'voucher', 'evidence', 'prior_report', 'correspondence', 'other'].map((value) => ({ label: value.replace('_', ' '), value }))
const tableRoles = ['population', 'master_lookup', 'prior_period', 'schedule', 'parameters', 'unknown'].map((value) => ({ label: value.replace('_', ' '), value }))
const actionOptions = [
  { label: 'Import file', value: 'import' },
  { label: 'Leave out', value: 'ignore' },
]
const uploadTotal = computed(() => batch.value?.items.filter((item) => item.needs_upload).length ?? 0)
const progress = computed(() => uploadTotal.value ? Math.round(uploaded.value / uploadTotal.value * 100) : 100)
const pendingClassification = computed(() => agent.pendingApproval.value?.kind === 'file_classification' ? agent.pendingApproval.value : null)
const uploadedItems = computed(() => batch.value?.items.filter((item) => item.uploaded) ?? [])
const reviewItems = computed(() => showAll.value
  ? uploadedItems.value
  : uploadedItems.value.filter(item => item.error || !edits.value[item.id] || edits.value[item.id].confidence !== 'high'))
const classificationEditable = computed(() => agent.state.run?.mode === 'permission' && Boolean(pendingClassification.value))
const routeCounts = computed(() => {
  const counts = { table: 0, document: 0, excluded: 0 }
  for (const item of uploadedItems.value) {
    const classification = edits.value[item.id]
    if (classification?.proposed_action === 'ignore' || classification?.route === 'ignore' || classification?.route === 'unsupported') {
      counts.excluded += 1
    } else if (classification?.route === 'table') {
      counts.table += 1
    } else if (classification?.route === 'document') {
      counts.document += 1
    }
  }
  return counts
})
const planningAction = computed(() => (
  batch.value?.suggested_actions?.find(action => action.agent_kind === 'planning') ?? null
))

watch(pendingClassification, (approval) => {
  if (!approval) return
  for (const proposal of approval.items) {
    const spec = proposal.spec as unknown as IntakeClassification & { item_id: string }
    if (spec.item_id) edits.value[spec.item_id] = { ...spec }
  }
})

watch(() => agent.state.run?.status, (status) => {
  if (!batch.value || agent.state.run?.kind !== 'intake') return
  if (status === 'completed' || status === 'failed' || status === 'cancelled') {
    void refreshBatch().then(() => { step.value = 4; if (status === 'completed') emit('imported') })
  }
})

function chooseFolder(event: Event) {
  const input = event.target as HTMLInputElement
  files.value = Array.from(input.files ?? [])
  error.value = ''
  if (!files.value.length) return
  const first = relativePath(files.value[0])
  folderName.value = first.includes('/') ? first.split('/')[0] : ''
  void compareAndUpload()
}

function relativePath(file: File): string {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
}

function fileName(path: string): string {
  return path.split('/').pop() || path
}

function parentPath(path: string): string {
  const parts = path.split('/')
  parts.pop()
  return parts.join(' / ')
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function fileIcon(item: IntakeBatchItem): string {
  const route = edits.value[item.id]?.route
  if (route === 'table') return 'pi pi-table'
  if (route === 'document') return 'pi pi-file'
  return 'pi pi-file'
}

function routeLabel(item: IntakeBatchItem): string {
  const classification = edits.value[item.id]
  if (!classification) return 'Classifying'
  if (classification.proposed_action === 'ignore' || classification.route === 'ignore') return 'Leave out'
  if (classification.route === 'unsupported') return 'Unsupported'
  if (classification.route === 'table') return 'Data table'
  if (classification.route === 'document') return 'Document'
  return classification.route
}

async function compareAndUpload() {
  if (!files.value.length) return
  busy.value = true
  error.value = ''
  try {
    const manifest = files.value.map((file) => ({
      relative_path: relativePath(file),
      size: file.size,
      last_modified: file.lastModified,
      mime: file.type,
    }))
    const compared = await api.post<{ batch: IntakeBatch; upload_paths: string[] }>(
      `/api/workspaces/${props.workspaceId}/folder-imports`,
      { root_name: folderName.value, manifest, mode: launchMode.value },
    )
    batch.value = compared.batch
    step.value = 2
    uploaded.value = 0
    const requested = new Set(compared.upload_paths)
    for (const file of files.value) {
      const path = relativePath(file)
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
    seedEdits(batch.value.items)
    step.value = 3
    await agent.startRun(
      launchMode.value,
      { batch_id: batch.value.id, source_id: batch.value.source_id },
      'intake',
    )
  } catch (cause) {
    error.value = String(cause)
    if (batch.value?.status === 'uploading') {
      try {
        await api.del(`/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}`)
      } catch {
        /* A later folder selection creates a fresh durable batch. */
      }
    }
    step.value = 1
    files.value = []
    batch.value = null
    uploaded.value = 0
  } finally {
    busy.value = false
  }
}

function seedEdits(items: IntakeBatchItem[]) {
  edits.value = {}
  showAll.value = false
  for (const item of items) if (item.classification) edits.value[item.id] = { ...item.classification }
}

async function refreshBatch() {
  if (!batch.value) return
  batch.value = await api.get<IntakeBatch>(`/api/workspaces/${props.workspaceId}/folder-imports/${batch.value.id}`)
}

async function applyClassifications() {
  const approval = pendingClassification.value
  if (!approval) return
  const decisions: AgentDecision[] = approval.items.map((proposal) => {
    const original = proposal.spec as unknown as { item_id: string }
    return { item_id: proposal.id, action: 'edit', spec: { ...proposal.spec, ...edits.value[original.item_id] } }
  })
  busy.value = true
  try {
    await agent.decide(approval.id, decisions)
  } catch (cause) {
    error.value = String(cause)
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
  files.value = []
  folderName.value = ''
  batch.value = null
  uploaded.value = 0
  edits.value = {}
  showAll.value = false
  error.value = ''
}
</script>

<template>
  <Dialog :visible="modelValue" modal header="Import audit folder" class="folder-import-dialog" :style="{ width: 'min(94vw, 78rem)' }" @update:visible="close">
    <div v-if="error" class="inline-error"><i class="pi pi-exclamation-triangle" />{{ error }}</div>
    <nav class="wizard-progress" aria-label="Import progress"><span v-for="(label,index) in ['Choose folder','Upload','Review','Complete']" :key="label" :class="{ active: step === index + 1, done: step > index + 1 }"><i>{{ index + 1 }}</i>{{ label }}</span></nav>

    <section v-if="step === 1" class="select-step">
      <label class="folder-picker">
        <i class="pi pi-folder-open" />
        <strong>Choose folder</strong>
        <span>Import supported data and documents from this folder and its subfolders.</span>
        <input type="file" multiple webkitdirectory @change="chooseFolder" />
      </label>
    </section>

    <section v-else-if="step === 2" class="upload-step">
      <h3>Importing {{ folderName }}</h3>
      <p>{{ uploaded }} of {{ uploadTotal }} requested files uploaded. Unchanged and excluded files stay untouched.</p>
      <ProgressBar :value="progress" />
      <p class="muted">Keep this dialog open until staging finishes. Classification can continue after the upload is durable.</p>
    </section>

    <section v-else-if="step === 3" class="review-step">
      <div class="review-head">
        <div><h3>Review files</h3></div>
        <Tag :value="agent.state.run?.status?.replace('_', ' ') || 'starting'" />
      </div>
      <div class="classification-review" v-if="batch">
        <div class="route-summary" aria-label="Classification summary">
          <span><i class="pi pi-table" /><strong>{{ routeCounts.table }}</strong> data {{ routeCounts.table === 1 ? 'table' : 'tables' }}</span>
          <span><i class="pi pi-file" /><strong>{{ routeCounts.document }}</strong> {{ routeCounts.document === 1 ? 'document' : 'documents' }}</span>
          <span><i class="pi pi-minus-circle" /><strong>{{ routeCounts.excluded }}</strong> left out</span>
        </div>
        <div class="review-filter"><span>{{ showAll ? 'Showing all staged files' : 'Showing files that need attention' }}</span><Button :label="showAll ? 'Show attention only' : `Show all ${uploadedItems.length}`" text size="small" @click="showAll = !showAll" /></div>
        <div class="file-review-list">
          <article class="file-review-card" v-for="item in reviewItems" :key="item.id">
            <header class="file-review-header">
            <span class="file-icon" :class="`route-${edits[item.id]?.route || 'pending'}`"><i :class="fileIcon(item)" /></span>
            <span class="file-identity">
              <strong :title="item.relative_path">{{ fileName(item.relative_path) }}</strong>
              <small v-if="parentPath(item.relative_path)">{{ parentPath(item.relative_path) }}</small>
              <small>{{ formatBytes(item.size) }} · {{ item.state.replace('_', ' ') }}</small>
            </span>
            <span class="route-label" :class="`route-${edits[item.id]?.route || 'pending'}`">{{ routeLabel(item) }}</span>
            </header>
            <div v-if="edits[item.id]" class="classification-fields" :class="{ readonly: !classificationEditable }">
              <label><span>Use as</span><Select v-model="edits[item.id].route" :options="routeOptions" optionLabel="label" optionValue="value" :disabled="!classificationEditable" /></label>
              <label v-if="edits[item.id].route === 'document'"><span>Document type</span><Select v-model="edits[item.id].document_category" :options="documentCategories" optionLabel="label" optionValue="value" :disabled="!classificationEditable" /></label>
              <label v-else-if="edits[item.id].route === 'table'"><span>Data role</span><Select v-model="edits[item.id].table_role" :options="tableRoles" optionLabel="label" optionValue="value" :disabled="!classificationEditable" /></label>
              <label v-if="edits[item.id].route === 'table' || edits[item.id].route === 'document'" class="name-field"><span>Name in workspace</span><InputText v-model="edits[item.id].proposed_name" :disabled="!classificationEditable" /></label>
              <label v-if="classificationEditable"><span>Decision</span><Select v-model="edits[item.id].proposed_action" :options="actionOptions" optionLabel="label" optionValue="value" /></label>
            </div>
            <footer v-if="edits[item.id]?.rationale || item.error" class="classification-basis"><Tag v-if="edits[item.id]?.confidence" :value="`${edits[item.id].confidence} confidence`" /><span>{{ edits[item.id]?.rationale || item.error }}</span></footer>
          </article>
          <p v-if="!reviewItems.length" class="all-clear"><i class="pi pi-check-circle" /> All staged files have high-confidence classifications.</p>
        </div>
      </div>
      <div class="review-actions">
        <span v-if="agent.state.run?.mode === 'permission' && !pendingClassification" class="muted">The assistant is preparing the editable approval batch…</span>
        <span v-if="agent.state.run?.mode === 'auto'" class="muted">Auto mode is applying only locally valid high-confidence agreements.</span>
        <Button v-if="agent.state.run?.mode === 'permission'" label="Apply classifications" icon="pi pi-check" :disabled="!pendingClassification || busy" :loading="busy" @click="applyClassifications" />
      </div>
    </section>

    <section v-else class="summary-step">
      <h3>{{ agent.state.run?.status === 'completed' ? 'Import complete' : 'Import stopped' }}</h3>
      <div class="summary-cards" v-if="batch?.summary">
        <span><strong>{{ batch.summary.imported }}</strong> imported</span>
        <span><strong>{{ batch.summary.unchanged }}</strong> unchanged</span>
        <span><strong>{{ batch.summary.ignored }}</strong> ignored</span>
        <span><strong>{{ batch.summary.ambiguous }}</strong> manual review</span>
      </div>
      <p v-if="agent.state.run?.error" class="inline-error">{{ agent.state.run.error }}</p>
      <PostImportPlanningOffer
        v-if="planningAction"
        :workspaceId="workspaceId"
        :action="planningAction"
        :documentAiEnabled="Boolean(documentAiEnabled)"
        @settings-changed="emit('settings-changed')"
        @planning-started="emit('planning-started'); close()"
      />
      <div class="review-actions"><Button label="Import another folder" severity="secondary" @click="reset" /><Button label="Done" @click="close" /></div>
    </section>
  </Dialog>
</template>

<style scoped>
.select-step { display: grid; gap: 1rem; max-width: 42rem; margin: auto; }
.wizard-progress { display:grid; grid-template-columns:repeat(4,1fr); margin:-.25rem 0 1rem; border-bottom:1px solid var(--aw-border) }.wizard-progress span { display:flex; justify-content:center; align-items:center; gap:.4rem; padding:.55rem; color:var(--aw-muted); font-size:.72rem; font-weight:700 }.wizard-progress i { display:grid; place-items:center; width:1.35rem; height:1.35rem; border-radius:999px; background:var(--aw-raised); font-style:normal }.wizard-progress span.active { color:var(--aw-teal); border-bottom:2px solid var(--aw-teal) }.wizard-progress span.done i,.wizard-progress span.active i { color:white; background:var(--aw-teal) }
.folder-picker { display: flex; flex-direction: column; align-items: center; gap: .4rem; padding: 1.5rem; border: 1px dashed var(--p-surface-300); border-radius: 10px; background: var(--p-surface-50); text-align: center; cursor: pointer; transition: border-color .15s, background .15s, box-shadow .15s; }
.folder-picker:hover { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.folder-picker i { font-size: 2rem; color: var(--aw-teal); }
.folder-picker span { color: var(--p-surface-500); font-size: .75rem; }
.folder-picker input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.selection-summary { display: flex; align-items: center; gap: .65rem; padding: .7rem .85rem; border: 1px solid #b7e3dc; border-radius: 8px; background: var(--aw-teal-soft); }
.selection-summary > i { color: var(--aw-teal); }
.selection-summary span { display: grid; gap: .15rem; }
.selection-summary small { color: var(--p-surface-500); }
.inline-error { display: flex; gap: .45rem; padding: .65rem; margin-bottom: .8rem; color: var(--p-red-700); background: var(--p-red-50); border-radius: 7px; }
.upload-step { max-width: 40rem; margin: 3rem auto; text-align: center; }
.muted { color: var(--p-surface-500); font-size: .75rem; }
.review-step { max-width: 66rem; margin: auto; }
.review-head, .review-actions { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.review-head h3, .review-head p { margin: 0 0 .2rem; }
.classification-review { display: grid; gap: .85rem; margin: 1rem 0; }
.route-summary { display: flex; flex-wrap: wrap; gap: .55rem; }
.route-summary span { display: inline-flex; align-items: center; gap: .4rem; padding: .45rem .7rem; border: 1px solid var(--p-surface-200); border-radius: 999px; color: var(--p-surface-600); background: var(--p-surface-50); font-size: .75rem; }
.route-summary i { color: var(--aw-teal); }
.route-summary strong { color: var(--p-surface-900); }
.review-filter { display:flex; align-items:center; justify-content:space-between; min-height:2.2rem; color:var(--aw-muted); font-size:.75rem }.all-clear { display:flex; align-items:center; justify-content:center; gap:.45rem; padding:2rem; color:var(--aw-ok) }
.file-review-list { display: flex; flex-direction: column; gap: .7rem; max-height: min(58vh, 38rem); overflow-y: auto; padding: .1rem .25rem .1rem .1rem; }
.file-review-card { flex: 0 0 auto; overflow: hidden; border: 1px solid var(--p-surface-200); border-radius: 10px; background: var(--p-surface-0); box-shadow: 0 1px 2px rgb(15 23 42 / 4%); }
.file-review-header { display: flex; align-items: center; gap: .7rem; padding: .75rem .85rem; }
.file-icon { display: grid; flex: 0 0 2.2rem; height: 2.2rem; place-items: center; border-radius: 8px; color: var(--p-surface-600); background: var(--p-surface-100); }
.file-icon.route-table { color: var(--aw-teal); background: var(--aw-teal-soft); }
.file-icon.route-document { color: var(--p-blue-600); background: var(--p-blue-50); }
.file-identity { display: grid; flex: 1; min-width: 0; gap: .08rem; }
.file-identity strong { overflow: hidden; color: var(--p-surface-900); text-overflow: ellipsis; white-space: nowrap; font-size: .85rem; }
.file-identity small { overflow: hidden; color: var(--p-surface-500); text-overflow: ellipsis; white-space: nowrap; font-size: .68rem; }
.route-label { flex: 0 0 auto; padding: .28rem .55rem; border-radius: 999px; color: var(--p-surface-600); background: var(--p-surface-100); font-size: .68rem; font-weight: 700; }
.route-label.route-table { color: var(--aw-teal); background: var(--aw-teal-soft); }
.route-label.route-document { color: var(--p-blue-700); background: var(--p-blue-50); }
.classification-fields { display: grid; grid-template-columns: minmax(8rem, .8fr) minmax(9rem, 1fr) minmax(12rem, 1.35fr) minmax(7rem, .7fr); gap: .65rem; padding: .75rem .85rem; border-top: 1px solid var(--p-surface-100); background: var(--p-surface-50); }
.classification-fields label { display: grid; align-content: start; gap: .3rem; min-width: 0; }
.classification-fields label > span { color: var(--p-surface-600); font-size: .67rem; font-weight: 700; }
.classification-fields :deep(.p-select), .classification-fields :deep(.p-inputtext) { width: 100%; min-width: 0; font-size: .78rem; }
.classification-fields.readonly :deep(.p-disabled) { opacity: .85; }
.classification-basis { display: flex; align-items: center; gap: .55rem; padding: .55rem .85rem; border-top: 1px solid var(--p-surface-100); color: var(--p-surface-500); font-size: .7rem; }
.classification-basis :deep(.p-tag) { flex: 0 0 auto; font-size: .62rem; }
.summary-step { max-width: 44rem; margin: 2rem auto; text-align: center; }
.summary-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: .7rem; margin: 1.5rem 0; }
.summary-cards span { display: flex; flex-direction: column; padding: 1rem; background: var(--p-surface-50); border: 1px solid var(--p-surface-200); border-radius: 8px; }
.summary-cards strong { font-size: 1.4rem; color: var(--aw-teal); }
@media (max-width: 800px) { .classification-fields { grid-template-columns: 1fr 1fr; } }
@media (max-width: 560px) {
  .review-head { align-items: flex-start; }
  .classification-fields { grid-template-columns: 1fr; }
  .route-label { display: none; }
  .summary-cards { grid-template-columns: 1fr 1fr; }
}
</style>
