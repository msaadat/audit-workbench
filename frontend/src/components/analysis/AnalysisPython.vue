<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type {
  AnalysisDetail,
  FramePayload,
  RunPythonResult,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import CodeEditor from '../CodeEditor.vue'
import AnalysisFooter from './AnalysisFooter.vue'
import AnalysisHead from './AnalysisHead.vue'
import AnalysisVerdict from './AnalysisVerdict.vue'
import UiOverflowMenu from '../ui/UiOverflowMenu.vue'

// A saved Python analysis (AI-written or hand-written): editable Polars that
// runs in the local sandbox. Two verbs, as in the library editor — Preview
// computes the code on screen and records nothing; Run executes the saved
// procedure and records what it concluded.
const props = defineProps<{ workspace: WorkspaceSummary; analysis: SavedAnalysis }>()
const emit = defineEmits<{ deleted: []; changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)

const title = ref('')
const code = ref('')
const policy = ref<'exception_rows' | 'informational'>('informational')
const frame = ref<FramePayload | null>(null)
const totalRows = ref(0)
const stdout = ref<string | null>(null)
const runError = ref<string | null>(null)
const detail = ref<AnalysisDetail | null>(null)
const previewing = ref(false)
const loadingCurrent = ref(false)
const running = ref(false)
const saving = ref(false)
const exporting = ref(false)

// What returned rows mean. The auditor declares it, because only the auditor
// knows whether this procedure hunts exceptions or produces context — and the
// declaration is what turns a row count into a verdict. It sits in the verdict
// bar, beside the reading it decides, rather than as a form row below it.

const savedCode = computed(() => String((props.analysis.spec as { code?: string })?.code ?? ''))
const savedPolicy = computed(() => props.analysis.outcome_policy?.mode ?? 'informational')
const dirty = computed(
  () => code.value !== savedCode.value
    || title.value.trim() !== props.analysis.title
    || policy.value !== savedPolicy.value,
)

watch(
  () => props.analysis.id,
  (id) => {
    title.value = props.analysis.title
    code.value = savedCode.value
    policy.value = savedPolicy.value === 'exception_rows' ? 'exception_rows' : 'informational'
    frame.value = null
    totalRows.value = 0
    stdout.value = null
    runError.value = null
    detail.value = null
    void loadCurrent(id)
  },
  { immediate: true },
)

/**
 * Show what this code currently returns, the moment the procedure is opened
 * — not only after an explicit Preview or Run. Nothing is recorded; the
 * outcome banner above stays the durable conclusion.
 */
async function loadCurrent(analysisId: string) {
  loadingCurrent.value = true
  try {
    const current = await api.get<AnalysisDetail>(
      `/api/workspaces/${props.workspace.id}/analyses/${analysisId}`,
    )
    frame.value = current.frame ?? null
    totalRows.value = current.total_rows ?? 0
    stdout.value = current.stdout ?? null
  } catch {
    // The recorded outcome banner still stands; a failed recompute here is
    // not fatal, and Run surfaces the same error explicitly if retried.
  } finally {
    loadingCurrent.value = false
  }
}

/** Compute the code on screen. Records nothing. */
async function preview() {
  previewing.value = true
  runError.value = null
  try {
    const result = props.analysis.alignment
      ? await api.post<AnalysisDetail>(
        `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/preview`,
        { spec: { code: code.value } },
      )
      : await api.post<RunPythonResult>(
        `/api/workspaces/${props.workspace.id}/run-python`,
        { code: code.value },
      )
    if ('error' in result && result.error) throw new Error(result.error)
    frame.value = result.frame ?? null
    totalRows.value = result.total_rows ?? 0
    stdout.value = result.stdout ?? null
    detail.value = null
  } catch (error) {
    runError.value = error instanceof ApiError ? error.message : String(error)
  } finally {
    previewing.value = false
  }
}

/** Execute the saved procedure and record its conclusion. */
async function run() {
  if (dirty.value) await save()
  running.value = true
  runError.value = null
  try {
    const executed = await api.post<AnalysisDetail>(
      `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/execute`, {},
    )
    detail.value = executed
    frame.value = executed.frame ?? null
    totalRows.value = executed.total_rows ?? 0
    stdout.value = executed.stdout ?? null
    emit('changed')
    const recorded = executed.last_result
    toast.add({
      severity: recorded?.status === 'error' ? 'warn' : 'success',
      summary: recorded?.status === 'error' ? 'Recorded an execution issue' : 'Result recorded',
      detail: recorded?.error || recorded?.verdict_text || title.value,
      life: 4000,
    })
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
      {
        title: name,
        spec: { code: code.value },
        viz: props.analysis.viz,
        outcome_policy: { mode: policy.value },
      },
    )
    emit('changed')
    toast.add({ severity: 'success', summary: 'Saved', detail: name, life: 2500 })
  } catch (error) {
    fail('Save failed', error)
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  confirm.require({
    header: 'Delete analysis',
    message: `Delete "${props.analysis.title}"?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}`)
        emit('deleted')
      } catch (error) {
        fail('Delete failed', error)
      }
    },
  })
}

/** Export the saved procedure's full result — not the previewed slice of it. */
async function exportExcel() {
  exporting.value = true
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/export`,
      {},
      `${props.analysis.title.replace(/[^A-Za-z0-9._-]+/g, '_') || 'analysis'}.xlsx`,
    )
  } catch (error) {
    fail('Export failed', error)
  } finally {
    exporting.value = false
  }
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

/** Everything that is not this procedure's next act. */
const menuItems = computed(() => [
  {
    label: 'Delete procedure',
    icon: 'pi pi-trash',
    command: () => confirmDelete(),
  },
])

/** Open the agent run that recorded this result, in the assistant drawer. */
async function openRun(runId: string) {
  try {
    await agent.openRun(runId)
    agent.openPanel()
  } catch (error) {
    fail('Could not open the run', error)
  }
}
</script>

<template>
  <AnalysisHead :analysis="analysis">
    <template #actions>
      <Button
        label="Save"
        icon="pi pi-save"
        size="small"
        :severity="dirty ? undefined : 'secondary'"
        :outlined="!dirty"
        :loading="saving"
        @click="save"
      />
      <Button v-if="frame" label="Export" icon="pi pi-file-excel" severity="secondary" size="small" outlined :loading="exporting" @click="exportExcel" />
      <UiOverflowMenu :items="menuItems" tooltip="More procedure actions" />
    </template>
  </AnalysisHead>

  <AnalysisVerdict
    :classification="detail?.classification ?? analysis.classification"
    :state="detail?.state ?? analysis.state"
    :result="detail?.last_result ?? analysis.last_result"
    :policy="policy"
    :busy="running"
    @openRun="openRun"
    @run="run"
    @update:policy="policy = $event"
  />

  <p v-if="props.analysis?.alignment" class="muted population">
    Population: {{ props.analysis.alignment.root }}.
    <span v-for="hop in props.analysis.alignment.joins" :key="hop.name">
      {{ hop.left_on.join(', ') }} → {{ hop.right }}.{{ hop.right_on.join(', ') }}.
    </span>
  </p>
  <p v-if="dirty" class="dirty-note">
    <i class="pi pi-pencil" /> Unsaved changes. Running saves them first, so the
    recorded result always matches the definition that produced it.
  </p>

  <div v-if="runError" class="analysis-error">
    <i class="pi pi-exclamation-triangle" /> {{ runError }}
  </div>

  <!-- The definition, title included: renaming a procedure is one edit among
       the edits that change what it does, not a bare input above the page. -->
  <div class="analysis-code-block">
    <div class="analysis-code-head">
      <label class="title-field">
        <span>Title</span>
        <InputText v-model="title" placeholder="What this procedure tests" size="small" />
      </label>
      <span class="grow" />
      <span class="hint"><i class="pi pi-code" /> Python — runs in the local sandbox</span>
      <Button label="Preview" icon="pi pi-eye" size="small" text :loading="previewing" @click="preview" />
    </div>
    <CodeEditor v-model="code" />
    <pre v-if="stdout" class="analysis-stdout">{{ stdout }}</pre>
  </div>

  <div v-if="frame" class="result">
    <p class="muted rows">
      {{ totalRows.toLocaleString() }} row{{ totalRows === 1 ? '' : 's' }}
      <span v-if="!detail"> · current data, not recorded</span>
    </p>
    <ChartView :frame="frame" :viz="analysis.viz" height="320px" />
  </div>
  <div v-else-if="loadingCurrent" class="loading-current">
    <i class="pi pi-spin pi-spinner" /> Loading current result…
  </div>

  <AnalysisFooter :analysis="analysis" />
</template>

<style scoped>
.population { margin: 0 0 var(--aw-space-2); font-size: var(--aw-text-sm); }
.dirty-note {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0 0 1rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}
.muted { color: var(--aw-muted); }
.rows { font-size: var(--aw-text-sm); margin: 0 0 0.5rem; }
.loading-current { display: flex; align-items: center; gap: 0.4rem; padding: 1rem 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
</style>
