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

const routeOptions = ['table', 'document', 'unsupported', 'ignore'].map((value) => ({ label: value, value }))
const documentCategories = ['background', 'policy', 'regulation', 'contract', 'minutes', 'voucher', 'evidence', 'prior_report', 'correspondence', 'other'].map((value) => ({ label: value.replace('_', ' '), value }))
const tableRoles = ['population', 'master_lookup', 'prior_period', 'schedule', 'parameters', 'unknown'].map((value) => ({ label: value.replace('_', ' '), value }))
const actionOptions = ['import', 'ignore'].map((value) => ({ label: value, value }))
const uploadTotal = computed(() => batch.value?.items.filter((item) => item.needs_upload).length ?? 0)
const progress = computed(() => uploadTotal.value ? Math.round(uploaded.value / uploadTotal.value * 100) : 100)
const pendingClassification = computed(() => agent.pendingApproval.value?.kind === 'file_classification' ? agent.pendingApproval.value : null)
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
    await agent.startRun(launchMode.value, { batch_id: batch.value.id }, 'intake')
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
  error.value = ''
}
</script>

<template>
  <Dialog :visible="modelValue" modal header="Import audit folder" class="folder-import-dialog" :style="{ width: 'min(94vw, 78rem)' }" @update:visible="close">
    <div v-if="error" class="inline-error"><i class="pi pi-exclamation-triangle" />{{ error }}</div>

    <section v-if="step === 1" class="select-step">
      <label class="folder-picker">
        <i class="pi pi-folder-open" />
        <strong>Choose folder</strong>
        <span>Import supported data and documents from this folder and its subfolders.</span>
        <input type="file" multiple webkitdirectory @change="chooseFolder" />
      </label>
      <p class="local-note"><i class="pi pi-shield" /> The browser grants access only to your selection. Absolute paths are never sent.</p>
    </section>

    <section v-else-if="step === 2" class="upload-step">
      <h3>Importing {{ folderName }}</h3>
      <p>{{ uploaded }} of {{ uploadTotal }} requested files uploaded. Unchanged and excluded files stay untouched.</p>
      <ProgressBar :value="progress" />
      <p class="muted">Keep this dialog open until staging finishes. Classification can continue after the upload is durable.</p>
    </section>

    <section v-else-if="step === 3" class="review-step">
      <div class="review-head">
        <div><h3>Classification</h3><p>Review routing metadata only; no spreadsheet rows or document text are shown here.</p></div>
        <Tag :value="agent.state.run?.status?.replace('_', ' ') || 'starting'" />
      </div>
      <div class="classification-grid" v-if="batch">
        <div class="grid-row grid-head"><span>File</span><span>Route</span><span>Category / role</span><span>Name</span><span>Action</span><span>Basis</span></div>
        <div class="grid-row" v-for="item in batch.items.filter((candidate) => candidate.uploaded)" :key="item.id">
          <span class="file-cell"><strong>{{ item.relative_path }}</strong><small>{{ item.state }} · {{ item.size.toLocaleString() }} bytes</small></span>
          <Select v-if="edits[item.id]" v-model="edits[item.id].route" :options="routeOptions" optionLabel="label" optionValue="value" />
          <Select v-if="edits[item.id]?.route === 'document'" v-model="edits[item.id].document_category" :options="documentCategories" optionLabel="label" optionValue="value" />
          <Select v-else-if="edits[item.id]?.route === 'table'" v-model="edits[item.id].table_role" :options="tableRoles" optionLabel="label" optionValue="value" />
          <span v-else class="muted">Not applicable</span>
          <InputText v-if="edits[item.id]" v-model="edits[item.id].proposed_name" />
          <Select v-if="edits[item.id]" v-model="edits[item.id].proposed_action" :options="actionOptions" optionLabel="label" optionValue="value" />
          <span class="basis"><Tag v-if="edits[item.id]" :value="edits[item.id].confidence" /><small>{{ edits[item.id]?.rationale || item.error }}</small></span>
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
.folder-picker { display: flex; flex-direction: column; align-items: center; gap: .4rem; padding: 1.5rem; border: 1px dashed var(--p-surface-300); border-radius: 10px; background: var(--p-surface-50); text-align: center; cursor: pointer; transition: border-color .15s, background .15s, box-shadow .15s; }
.folder-picker:hover { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.folder-picker i { font-size: 2rem; color: var(--aw-teal); }
.folder-picker span { color: var(--p-surface-500); font-size: .75rem; }
.folder-picker input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.selection-summary { display: flex; align-items: center; gap: .65rem; padding: .7rem .85rem; border: 1px solid #b7e3dc; border-radius: 8px; background: var(--aw-teal-soft); }
.selection-summary > i { color: var(--aw-teal); }
.selection-summary span { display: grid; gap: .15rem; }
.selection-summary small { color: var(--p-surface-500); }
.local-note { display: flex; align-items: center; gap: .45rem; margin: 0; color: var(--p-surface-500); font-size: .75rem; }
.local-note i { color: var(--aw-teal); }
.inline-error { display: flex; gap: .45rem; padding: .65rem; margin-bottom: .8rem; color: var(--p-red-700); background: var(--p-red-50); border-radius: 7px; }
.upload-step { max-width: 40rem; margin: 3rem auto; text-align: center; }
.muted { color: var(--p-surface-500); font-size: .75rem; }
.review-head, .review-actions { display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.review-head h3, .review-head p { margin: 0 0 .2rem; }
.classification-grid { overflow: auto; margin: 1rem 0; border: 1px solid var(--p-surface-200); border-radius: 8px; }
.grid-row { display: grid; grid-template-columns: minmax(15rem, 2fr) 8rem 10rem 11rem 7rem minmax(12rem, 1.4fr); gap: .55rem; align-items: center; min-width: 70rem; padding: .55rem; border-bottom: 1px solid var(--p-surface-200); }
.grid-head { position: sticky; top: 0; z-index: 1; background: var(--p-surface-100); font-size: .7rem; font-weight: 700; text-transform: uppercase; }
.file-cell, .basis { display: flex; flex-direction: column; gap: .2rem; min-width: 0; }
.file-cell strong { overflow: hidden; text-overflow: ellipsis; font-size: .78rem; }
.file-cell small, .basis small { color: var(--p-surface-500); font-size: .68rem; }
.summary-step { max-width: 44rem; margin: 2rem auto; text-align: center; }
.summary-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: .7rem; margin: 1.5rem 0; }
.summary-cards span { display: flex; flex-direction: column; padding: 1rem; background: var(--p-surface-50); border: 1px solid var(--p-surface-200); border-radius: 8px; }
.summary-cards strong { font-size: 1.4rem; color: var(--aw-teal); }
@media (max-width: 700px) { .summary-cards { grid-template-columns: 1fr 1fr; } }
</style>
