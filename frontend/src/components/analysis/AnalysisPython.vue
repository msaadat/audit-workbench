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
import FrameTable from '../FrameTable.vue'
import AnalysisFooter from './AnalysisFooter.vue'
import AnalysisHead from './AnalysisHead.vue'
import AnalysisVerdict from './AnalysisVerdict.vue'
import UiExecutionStory from '../ui/UiExecutionStory.vue'
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
/** Code starts folded on saved procedures so results lead; expands on edit/error. */
const codeOpen = ref(false)
const resultView = ref<'rows' | 'chart'>('rows')
const hasChart = computed(() => Boolean(props.analysis.viz?.type && props.analysis.viz.type !== 'table'))

const effectiveResult = computed(() => detail.value?.last_result ?? props.analysis.last_result ?? null)
const effectiveClassification = computed(() => detail.value?.classification ?? props.analysis.classification)
const scopeLabel = computed(() => {
  if (props.analysis.alignment?.root) {
    const hops = props.analysis.alignment.joins.map(hop => hop.right).filter(Boolean)
    return hops.length ? `${props.analysis.alignment.root} → ${hops.join(' → ')}` : props.analysis.alignment.root
  }
  return props.analysis.table || 'Workspace tables'
})
const humanStory = computed(() => {
  const a = props.analysis
  const res = effectiveResult.value
  if (a.promotion?.state === 'promoted') {
    return {
      title: `Promoted to ${a.promotion.test_id || 'Data Test'}`,
      detail: a.promotion.rcm_id
        ? `Carried forward into formal testing for ${a.promotion.rcm_id}.`
        : 'Carried forward into formal RCM Data Tests.',
      tone: 'ok' as const,
      badge: 'Auditor decided',
    }
  }
  if (a.promotion?.state === 'declined') {
    return {
      title: 'Reviewed — not promoted',
      detail: a.promotion.reason || 'Auditor recorded why no formal control test is needed.',
      tone: 'neutral' as const,
      badge: 'Auditor decided',
    }
  }
  if (!res) {
    return {
      title: 'Awaiting local run',
      detail: 'Click Run above to execute this Polars script over the full local table.',
      tone: 'neutral' as const,
      badge: 'Human review',
    }
  }
  if (res.error) {
    return {
      title: 'Needs auditor review',
      detail: 'Inspect the Polars code below or ask the assistant to revise the procedure.',
      tone: 'bad' as const,
      badge: 'Action needed',
    }
  }
  if (policy.value === 'exception_rows') {
    const exc = res.exception_count ?? 0
    if (exc > 0) {
      return {
        title: `${exc.toLocaleString()} exception${exc === 1 ? '' : 's'} to assess`,
        detail: 'Inspect flagged rows below, or carry into formal RCM Data Tests.',
        tone: 'warn' as const,
        badge: 'Human review',
      }
    }
    return {
      title: 'Verified — 0 exceptions',
      detail: 'Local execution found no breaches across the evaluated population.',
      tone: 'ok' as const,
      badge: 'Clean result',
    }
  }
  return {
    title: 'Informational context',
    detail: 'Returned rows inform audit planning and risk assessment (not exception-flagged).',
    tone: 'neutral' as const,
    badge: 'Planning context',
  }
})

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
    codeOpen.value = false
    resultView.value = policy.value === 'exception_rows' ? 'rows' : 'chart'
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
  // Running after a failed save records a result against the definition the
  // server still holds while the screen shows another.
  if (dirty.value && !(await save())) return
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

/** True once the server holds what is on screen; a run must not follow a false. */
async function save(): Promise<boolean> {
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return false
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
    return true
  } catch (error) {
    fail('Save failed', error)
    return false
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  confirm.require({
    header: 'Delete analysis',
    message: `Delete "${props.analysis.title}"?`,
    icon: 'aw-icon aw-icon-triangle-alert',
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
    icon: 'aw-icon aw-icon-trash-2',
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
        icon="aw-icon aw-icon-save"
        size="small"
        :severity="dirty ? undefined : 'secondary'"
        :outlined="!dirty"
        :loading="saving"
        @click="save"
      />
      <Button v-if="frame" label="Export" icon="aw-icon aw-icon-file-spreadsheet" severity="secondary" size="small" outlined :loading="exporting" @click="exportExcel" />
      <UiOverflowMenu :items="menuItems" tooltip="More procedure actions" />
    </template>
  </AnalysisHead>

  <AnalysisVerdict
    :classification="effectiveClassification"
    :state="detail?.state ?? analysis.state"
    :result="effectiveResult"
    :policy="policy"
    :busy="running"
    @openRun="openRun"
    @run="run"
    @update:policy="policy = $event"
  />

  <UiExecutionStory
    :ai-authored="analysis.source === 'ai'"
    engine-label="Polars Python"
    :rule-title="policy === 'exception_rows' ? 'Custom exception rule' : 'Custom analytical profile'"
    :scope-label="scopeLabel"
    :ran="Boolean(effectiveResult)"
    :population="effectiveResult?.population ?? null"
    :tested="effectiveResult?.tested ?? null"
    :exceptions="effectiveResult?.exception_count ?? 0"
    :tracks-exceptions="policy === 'exception_rows'"
    :verdict-text="effectiveResult?.verdict_text ?? null"
    :execution-error="effectiveResult?.error ?? runError"
    :human-title="humanStory.title"
    :human-detail="humanStory.detail"
    :human-tone="humanStory.tone"
    :human-badge="humanStory.badge"
  />

  <p v-if="dirty" class="dirty-note">
    <i class="aw-icon aw-icon-pencil" /> Unsaved changes. Running saves them first, so the
    recorded result always matches the definition that produced it.
  </p>

  <div v-if="runError" class="analysis-error">
    <i class="aw-icon aw-icon-triangle-alert" /> {{ runError }}
  </div>

  <!-- Results lead: executives and auditors see the computed rows before the Python script. -->
  <div v-if="frame" class="result">
    <div class="result-bar">
      <div v-if="hasChart" class="result-tabs" role="tablist" aria-label="Result view">
        <button
          type="button"
          role="tab"
          :aria-selected="resultView === 'rows'"
          class="result-tab"
          :class="{ active: resultView === 'rows' }"
          @click="resultView = 'rows'"
        >
          <i class="aw-icon aw-icon-table" aria-hidden="true" />
          {{ policy === 'exception_rows' ? 'Flagged rows' : 'Rows' }} ({{ totalRows.toLocaleString() }})
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="resultView === 'chart'"
          class="result-tab"
          :class="{ active: resultView === 'chart' }"
          @click="resultView = 'chart'"
        >
          <i class="aw-icon aw-icon-chart-column" aria-hidden="true" />
          Chart
        </button>
      </div>
      <p v-else class="muted rows">
        {{ totalRows.toLocaleString() }} row{{ totalRows === 1 ? '' : 's' }} returned
        <span v-if="!detail"> · current data, not recorded</span>
      </p>
    </div>
    <FrameTable v-if="hasChart && resultView === 'rows'" :frame="frame" scrollHeight="38vh" />
    <ChartView v-else :frame="frame" :viz="analysis.viz" height="320px" />
  </div>
  <div v-else-if="loadingCurrent" class="loading-current">
    <i class="aw-icon aw-icon-spin aw-icon-loader-circle" /> Loading current result…
  </div>

  <!-- The Polars definition, folded away until inspected or edited. -->
  <div class="code-disclosure">
    <div class="code-disclosure-head">
      <button type="button" class="section-toggle" :aria-expanded="codeOpen || dirty || Boolean(runError)" @click="codeOpen = !codeOpen">
        <i :class="(codeOpen || dirty || Boolean(runError)) ? 'aw-icon aw-icon-chevron-down' : 'aw-icon aw-icon-chevron-right'" />
        <i class="aw-icon aw-icon-code" aria-hidden="true" />
        <span>Polars script {{ analysis.source === 'ai' ? 'written by AI' : '' }}</span>
        <span class="hint">· runs in the local sandbox</span>
      </button>
      <span class="grow" />
      <Button v-if="codeOpen || dirty" label="Preview" icon="aw-icon aw-icon-eye" size="small" text :loading="previewing" @click="preview" />
    </div>
    <div v-if="codeOpen || dirty || Boolean(runError)" class="analysis-code-block">
      <div class="analysis-code-head">
        <label class="title-field">
          <span>Title</span>
          <InputText v-model="title" placeholder="What this procedure tests" size="small" />
        </label>
      </div>
      <p v-if="props.analysis?.alignment" class="muted population">
        Population: {{ props.analysis.alignment.root }}.
        <span v-for="hop in props.analysis.alignment.joins" :key="hop.name">
          {{ hop.left_on.join(', ') }} → {{ hop.right }}.{{ hop.right_on.join(', ') }}.
        </span>
      </p>
      <CodeEditor v-model="code" />
      <pre v-if="stdout" class="analysis-stdout">{{ stdout }}</pre>
    </div>
  </div>

  <AnalysisFooter :analysis="analysis" />
</template>

<style scoped>
.population { margin: 0 0 var(--aw-space-2); font-size: var(--aw-text-xs); }
.dirty-note {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}
.muted { color: var(--aw-muted); }
.result { display: flex; flex-direction: column; gap: .6rem; flex: none; min-width: 0; }
.result-bar { display: flex; align-items: center; justify-content: space-between; gap: .6rem; flex-wrap: wrap; }
.result-tabs {
  display: inline-flex;
  align-items: center;
  gap: .2rem;
  padding: .2rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-raised);
}
.result-tab {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  padding: .28rem .65rem;
  border: 0;
  border-radius: var(--aw-radius-control);
  background: transparent;
  color: var(--aw-ink-soft);
  font: inherit;
  font-size: var(--aw-text-xs);
  font-weight: 600;
  cursor: pointer;
}
.result-tab:hover:not(.active) { color: var(--aw-ink-strong); }
.result-tab.active {
  background: var(--aw-panel);
  color: var(--aw-ink-strong);
  box-shadow: 0 1px 2px color-mix(in srgb, var(--aw-ink-strong) 8%, transparent);
}
.rows { font-size: var(--aw-text-sm); margin: 0; }
.loading-current { display: flex; align-items: center; gap: 0.4rem; padding: 1rem 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.code-disclosure {
  display: flex;
  flex-direction: column;
  flex: none;
  gap: .65rem;
  padding: .65rem .85rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
}
.code-disclosure-head { display: flex; align-items: center; gap: .5rem; }
.code-disclosure-head .grow { flex: 1; }
.section-toggle {
  display: inline-flex;
  align-items: center;
  gap: .4rem;
  padding: 0;
  border: 0;
  background: none;
  color: var(--aw-ink-strong);
  font: inherit;
  font-size: var(--aw-text-sm);
  font-weight: 600;
  cursor: pointer;
}
.section-toggle:hover { color: var(--aw-teal); }
.section-toggle .hint { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 500; }
</style>
