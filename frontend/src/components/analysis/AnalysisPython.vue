<script setup lang="ts">
import { ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../../api'
import type { FramePayload, RunPythonResult, SavedAnalysis, WorkspaceSummary } from '../../types'
import ChartView from '../ChartView.vue'
import PinDialog from '../PinDialog.vue'

// A saved AI-code analysis: editable Polars that runs in the local sandbox.
// The frame arrives already computed on the payload; Run re-executes the edits.
const props = defineProps<{ workspace: WorkspaceSummary; analysis: SavedAnalysis }>()
const emit = defineEmits<{ deleted: []; changed: [] }>()
const toast = useToast()

const title = ref('')
const code = ref('')
const frame = ref<FramePayload | null>(null)
const totalRows = ref(0)
const stdout = ref<string | null>(null)
const running = ref(false)
const saving = ref(false)
const showPin = ref(false)
const pinning = ref(false)

watch(
  () => props.analysis.id,
  () => {
    title.value = props.analysis.title
    code.value = props.analysis.code ?? ''
    frame.value = props.analysis.frame ?? null
    totalRows.value = props.analysis.total_rows ?? 0
    stdout.value = props.analysis.stdout ?? null
  },
  { immediate: true },
)

async function run() {
  running.value = true
  try {
    const result = await api.post<RunPythonResult>(
      `/api/workspaces/${props.workspace.id}/run-python`,
      { code: code.value },
    )
    frame.value = result.frame
    totalRows.value = result.total_rows
    stdout.value = result.stdout
  } catch (error) {
    fail('Run failed', error)
  } finally {
    running.value = false
  }
}

async function save() {
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return
  }
  saving.value = true
  try {
    await api.patch<SavedAnalysis>(
      `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}`,
      { title: name, spec: { code: code.value }, viz: props.analysis.viz },
    )
    emit('changed')
    toast.add({ severity: 'success', summary: 'Saved', detail: name, life: 2500 })
  } catch (error) {
    fail('Save failed', error)
  } finally {
    saving.value = false
  }
}

async function removeAnalysis() {
  try {
    await api.del(`/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}`)
    emit('deleted')
  } catch (error) {
    fail('Delete failed', error)
  }
}

async function pinTile({ title: pinTitle, note }: { title: string; note: string }) {
  pinning.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/tiles`, {
      kind: 'python',
      table: props.analysis.table,
      title: pinTitle,
      note,
      spec: { code: code.value },
      viz: props.analysis.viz,
    })
    showPin.value = false
    toast.add({ severity: 'success', summary: 'Pinned to dashboard', detail: pinTitle, life: 3000 })
  } catch (error) {
    fail('Pin failed', error)
  } finally {
    pinning.value = false
  }
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}
</script>

<template>
  <div class="detail-head">
    <InputText v-model="title" placeholder="Analysis title" class="title-input" />
    <Tag v-if="analysis.table" :value="analysis.table" severity="secondary" />
    <Tag value="AI code" severity="info" />
    <span class="grow" />
    <Button label="Save" icon="pi pi-save" size="small" :loading="saving" @click="save" />
    <Button label="Pin" icon="pi pi-thumbtack" severity="secondary" size="small" @click="showPin = true" />
    <Button icon="pi pi-trash" severity="danger" text size="small" v-tooltip.bottom="'Delete analysis'" @click="removeAnalysis" />
  </div>

  <div v-if="analysis.error" class="err">
    <i class="pi pi-exclamation-triangle" /> {{ analysis.error }}
  </div>

  <div class="code-block">
    <div class="code-head">
      <span><i class="pi pi-code" /> Python — editable, runs in the local sandbox</span>
      <Button label="Run" icon="pi pi-play" size="small" text :loading="running" @click="run" />
    </div>
    <Textarea v-model="code" class="code" spellcheck="false" autoResize />
    <pre v-if="stdout" class="stdout">{{ stdout }}</pre>
  </div>

  <div v-if="frame" class="result">
    <p class="muted rows">{{ totalRows.toLocaleString() }} row{{ totalRows === 1 ? '' : 's' }}</p>
    <ChartView :frame="frame" :viz="analysis.viz" height="320px" />
  </div>

  <p class="disclosure"><i class="pi pi-shield" /> Metadata-only — raw rows stayed on this machine.</p>

  <PinDialog
    v-model:visible="showPin"
    :defaultTitle="title || analysis.title || 'Python result'"
    :saving="pinning"
    @pin="pinTile"
  />
</template>

<style scoped>
.detail-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.detail-head .grow { flex: 1; }
.title-input { min-width: 16rem; font-weight: 600; }

.err {
  color: var(--p-red-600);
  background: var(--p-red-50);
  border: 1px solid var(--p-red-200);
  border-radius: 8px;
  padding: 0.6rem 0.85rem;
  margin-bottom: 0.85rem;
}

.code-block { margin-bottom: 1rem; }
.code-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.78rem;
  color: var(--p-surface-500);
  margin-bottom: 0.25rem;
}
.code {
  width: 100%;
  font-family: ui-monospace, 'Cascadia Code', Consolas, monospace;
  font-size: 0.82rem;
}
.stdout {
  background: var(--p-surface-900);
  color: var(--p-surface-0);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  font-size: 0.78rem;
  overflow-x: auto;
  margin: 0.4rem 0 0;
}
.rows { font-size: 0.8rem; margin: 0 0 0.5rem; }
.disclosure {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  margin: 0.75rem 0 0;
  font-size: 0.75rem;
  color: var(--p-surface-500);
}
</style>
