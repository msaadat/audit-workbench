<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import Select from 'primevue/select'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type {
  AnalysisDetail,
  AnalysisExceptions,
  AnalyticsParamMeta,
  AnalyticsResult,
  AnalyticsTest,
  ColumnSchema,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import FrameTable from '../FrameTable.vue'
import AnalysisFooter from './AnalysisFooter.vue'
import AnalysisHead from './AnalysisHead.vue'
import AnalysisVerdict from './AnalysisVerdict.vue'
import UiExecutionStory from '../ui/UiExecutionStory.vue'
import UiOverflowMenu from '../ui/UiOverflowMenu.vue'

// A library analysis: pick a predefined audit test, configure it, and save it as
// a rerunnable procedure. Two verbs, kept distinct:
//
//   Preview — compute the current spec and look at the rows. Records nothing.
//   Run     — execute the saved procedure and record what it concluded, which
//             is what the rail, the triage counts, and the assistant read.
//
// The test catalog is a *picker state*, not a permanent fixture: it shows while
// no test is chosen (or after "Change test") and collapses to a compact strip
// once one is — so the config form and results stay above the fold.
const props = defineProps<{ workspace: WorkspaceSummary; analysis: SavedAnalysis | null }>()
const emit = defineEmits<{ saved: [SavedAnalysis]; deleted: []; changed: [] }>()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)

const table = ref<string | null>(null)
const schema = ref<ColumnSchema[]>([])
const tests = ref<AnalyticsTest[]>([])
const selected = ref<AnalyticsTest | null>(null)
const params = ref<Record<string, unknown>>({})
const result = ref<AnalyticsResult | null>(null)
const detail = ref<AnalysisDetail | null>(null)
const exceptions = ref<AnalysisExceptions | null>(null)
const resultView = ref<'exceptions' | 'chart'>('exceptions')
const baselineParams = ref<string | null>(null)
const title = ref('')
const previewing = ref(false)
const loadingCurrent = ref(false)
const running = ref(false)
const exporting = ref(false)
const saving = ref(false)
const pickerOpen = ref(!props.analysis)
const paramsOpen = ref(true)
/** Saved procedures lead with verdict + results; definition/params stay one click away. */
const definitionOpen = ref(!props.analysis)

const effectiveResult = computed(() => detail.value?.last_result ?? props.analysis?.last_result ?? null)
const effectiveClassification = computed(() => detail.value?.classification ?? props.analysis?.classification)
const scopeLabel = computed(() => {
  if (props.analysis?.alignment?.root) {
    const hops = props.analysis.alignment.joins.map(hop => hop.right).filter(Boolean)
    return hops.length ? `${props.analysis.alignment.root} → ${hops.join(' → ')}` : props.analysis.alignment.root
  }
  return props.analysis?.table || table.value || 'Workspace table'
})
const tracksExceptions = computed(() => {
  const res = effectiveResult.value
  if (effectiveClassification.value === 'exception') return true
  if ((res?.exception_count ?? 0) > 0) return true
  return res?.verdict !== 'info'
})
const humanStory = computed(() => {
  const a = props.analysis
  const res = effectiveResult.value
  if (a?.promotion?.state === 'promoted') {
    return {
      title: `Promoted to ${a.promotion.test_id || 'Data Test'}`,
      detail: a.promotion.rcm_id
        ? `Carried forward into formal testing for ${a.promotion.rcm_id}.`
        : 'Carried forward into formal RCM Data Tests.',
      tone: 'ok' as const,
      badge: 'Auditor decided',
    }
  }
  if (a?.promotion?.state === 'declined') {
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
      detail: 'Click Run above to execute this test over the full local table.',
      tone: 'neutral' as const,
      badge: 'Human review',
    }
  }
  if (res.error) {
    return {
      title: 'Needs auditor review',
      detail: 'Check the test parameters below or rerun after fixing the input table.',
      tone: 'bad' as const,
      badge: 'Action needed',
    }
  }
  if (tracksExceptions.value) {
    const exc = res.exception_count ?? 0
    if (exc > 0) {
      return {
        title: `${exc.toLocaleString()} exception${exc === 1 ? '' : 's'} to assess`,
        detail: 'Review the flagged rows below and decide whether to promote into an RCM Data Test.',
        tone: 'warn' as const,
        badge: 'Human review',
      }
    }
    return {
      title: 'Verified — 0 exceptions',
      detail: 'Deterministic local check found no breaches across the evaluated population.',
      tone: 'ok' as const,
      badge: 'Clean result',
    }
  }
  return {
    title: 'Informational context',
    detail: 'Statistical profile used to guide audit planning and risk assessment.',
    tone: 'neutral' as const,
    badge: 'Planning context',
  }
})

const tableOptions = computed(() => [...new Set([
  ...props.workspace.tables.map((t) => t.name),
  ...(props.analysis?.alignment && props.analysis.table ? [props.analysis.table] : []),
])])

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', info: 'info',
}

const testGroups = computed(() => {
  const groups: { name: string; tests: AnalyticsTest[] }[] = []
  for (const test of tests.value) {
    const name = test.group || 'Other'
    let group = groups.find((g) => g.name === name)
    if (!group) {
      group = { name, tests: [] }
      groups.push(group)
    }
    group.tests.push(test)
  }
  return groups
})

// Only chart when the saved viz actually matches the summary frame — otherwise
// ChartView's table fallback would duplicate the summary table below it.
const chartable = computed(() => {
  const r = result.value
  if (!r?.viz || r.viz.type === 'table' || !r.summary) return false
  const columns = r.summary.columns
  return !!r.viz.x && columns.includes(r.viz.x) && (r.viz.y ?? []).some((c) => columns.includes(c))
})

async function loadTests() {
  tests.value = await api.get<AnalyticsTest[]>('/api/analytics')
  // The registry may arrive after the analysis prop — sync once we have it.
  if (props.analysis && !selected.value) preloadFromAnalysis()
}
loadTests()

async function loadSchema() {
  if (!table.value) return
  schema.value = (
    await api.get<{ columns: ColumnSchema[] }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/schema`,
    )
  ).columns
}

function columnOptions(meta: AnalyticsParamMeta): string[] {
  const preferred = meta.column_kind
    ? schema.value.filter((c) => c.kind === meta.column_kind).map((c) => c.name)
    : []
  const rest = schema.value.map((c) => c.name).filter((name) => !preferred.includes(name))
  return [...preferred, ...rest]
}

function pick(test: AnalyticsTest, presetParams?: Record<string, unknown>) {
  selected.value = test
  result.value = null
  pickerOpen.value = false
  paramsOpen.value = true
  const defaults: Record<string, unknown> = {}
  for (const param of test.params) {
    if (presetParams && param.name in presetParams) defaults[param.name] = presetParams[param.name]
    else if (param.default !== undefined) defaults[param.name] = param.default
    else if (param.kind === 'columns') defaults[param.name] = []
    else defaults[param.name] = null
  }
  params.value = defaults
  if (!title.value) title.value = test.label
}

const ready = computed(() => {
  if (!selected.value || !table.value) return false
  return selected.value.params.every((p) => {
    if (p.optional) return true
    const value = params.value[p.name]
    if (p.kind === 'columns') return Array.isArray(value) && value.length > 0
    return value !== null && value !== undefined && value !== ''
  })
})

// The definition on screen differs from the one that produced the recorded
// result, so running it now would conclude something else. Saying so is more
// useful than silently letting the two drift. Compare against the hydrated
// parameter baseline so key order or default-filled optional fields never
// falsely mark a freshly opened procedure as edited.
const dirty = computed(() => {
  if (!props.analysis || !selected.value) return false
  const saved = (props.analysis.spec ?? {}) as { test?: string; params?: Record<string, unknown> }
  const paramsChanged = baselineParams.value !== null
    ? JSON.stringify(params.value) !== baselineParams.value
    : false
  return (
    saved.test !== selected.value.id
    || paramsChanged
    || title.value.trim() !== props.analysis.title
  )
})

async function preview() {
  if (!selected.value || !table.value) return
  previewing.value = true
  result.value = null
  try {
    result.value = await api.post<AnalyticsResult>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/analytics/${selected.value.id}`,
      params.value,
    )
    if (!title.value) title.value = result.value.title
  } catch (error) {
    fail('Preview failed', error)
  } finally {
    previewing.value = false
  }
}

/** Execute the saved procedure and record its conclusion. */
async function run() {
  if (!props.analysis) return
  // Running after a failed save records a result against the definition the
  // server still holds while the screen shows another.
  if (dirty.value && !(await save())) return
  running.value = true
  try {
    detail.value = await api.post<AnalysisDetail>(
      `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/execute`, {},
    )
    result.value = null
    await loadExceptions(props.analysis.id)
    emit('changed')
    const recorded = detail.value.last_result
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

function spec() {
  return { test: selected.value!.id, params: params.value }
}

/** True once the server holds what is on screen; a run must not follow a false. */
async function save(): Promise<boolean> {
  if (!selected.value || !table.value) return false
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return false
  }
  saving.value = true
  try {
    // No viz is sent: the chosen test's own chart is authoritative, and the
    // server derives (or re-derives, on a spec change) exactly that — never a
    // stale chart left over from whatever the test used to be.
    if (props.analysis) {
      await api.patch<SavedAnalysis>(
        `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}`,
        { title: name, spec: spec() },
      )
      baselineParams.value = JSON.stringify(params.value)
      emit('changed')
      toast.add({ severity: 'success', summary: 'Saved', detail: name, life: 2500 })
    } else {
      const created = await api.post<SavedAnalysis>(
        `/api/workspaces/${props.workspace.id}/analyses`,
        {
          kind: 'analytics' as const,
          table: table.value,
          title: name,
          spec: spec(),
          source: 'library' as const,
        },
      )
      emit('saved', created)
      toast.add({ severity: 'success', summary: 'Saved to analyses', detail: name, life: 2500 })
    }
    return true
  } catch (error) {
    fail('Save failed', error)
    return false
  } finally {
    saving.value = false
  }
}

/** Everything that is not this procedure's next act. */
const menuItems = computed(() => [
  { label: 'Delete procedure', icon: 'aw-icon aw-icon-trash-2', command: () => confirmDelete() },
])

function confirmDelete() {
  if (!props.analysis) return
  confirm.require({
    header: 'Delete analysis',
    message: `Delete "${props.analysis.title}"?`,
    icon: 'aw-icon aw-icon-triangle-alert',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/analyses/${props.analysis!.id}`)
        emit('deleted')
      } catch (error) {
        fail('Delete failed', error)
      }
    },
  })
}

async function exportExcel() {
  if (!selected.value || !table.value) return
  exporting.value = true
  try {
    // A saved procedure exports itself; an unsaved configuration exports the
    // parameters on screen, which is the only definition it has.
    if (props.analysis) {
      await api.download(
        `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/export`,
        {},
        `${props.analysis.title.replace(/[^A-Za-z0-9._-]+/g, '_') || 'analysis'}.xlsx`,
      )
    } else {
      await api.download(
        `/api/workspaces/${props.workspace.id}/tables/${table.value}/analytics/${selected.value.id}/export`,
        params.value,
        `${table.value}_${selected.value.id}.xlsx`,
      )
    }
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

/** Open the agent run that recorded this result, in the assistant drawer. */
async function openRun(runId: string) {
  try {
    await agent.openRun(runId)
    agent.openPanel()
  } catch (error) {
    fail('Could not open the run', error)
  }
}

async function preloadFromAnalysis() {
  const a = props.analysis
  if (!a) return
  title.value = a.title
  table.value = a.table
  await loadSchema()
  const saved = (a.spec ?? {}) as { test?: string; params?: Record<string, unknown> }
  const test = tests.value.find((t) => t.id === saved.test)
  if (test) {
    pick(test, saved.params ?? {})
    baselineParams.value = JSON.stringify(params.value)
    // The recorded outcome is stated at the top of the pane; the parameters
    // that produced it stay one click away rather than filling the screen.
    paramsOpen.value = false
  }
  await loadCurrent(a.id)
}

/** Read back the retained exception rows for the recorded result (no recompute). */
async function loadExceptions(analysisId: string) {
  try {
    const payload = await api.get<AnalysisExceptions>(
      `/api/workspaces/${props.workspace.id}/analyses/${analysisId}/exceptions`,
    )
    exceptions.value = payload
    resultView.value = payload.frame?.rows.length ? 'exceptions' : 'chart'
  } catch {
    exceptions.value = null
    resultView.value = 'chart'
  }
}

/**
 * Show what this procedure currently returns, the moment it's opened — not
 * only after an explicit Preview or Run. Nothing is recorded; a saved
 * analysis is a rerunnable spec, and seeing what it returns is why an
 * auditor opened it.
 */
async function loadCurrent(analysisId: string) {
  loadingCurrent.value = true
  try {
    const [loadedDetail] = await Promise.all([
      api.get<AnalysisDetail>(
        `/api/workspaces/${props.workspace.id}/analyses/${analysisId}`,
      ),
      loadExceptions(analysisId),
    ])
    detail.value = loadedDetail
  } catch {
    // The recorded outcome banner above still stands; a failed recompute here
    // is not fatal, and Run surfaces the same error explicitly if retried.
  } finally {
    loadingCurrent.value = false
  }
}

// Editing a saved analysis: preload it. Creating new: default to the first table.
watch(
  () => props.analysis?.id,
  () => {
    detail.value = null
    exceptions.value = null
    baselineParams.value = null
    definitionOpen.value = !props.analysis
    if (props.analysis) {
      preloadFromAnalysis()
    } else {
      selected.value = null
      result.value = null
      title.value = ''
      pickerOpen.value = true
      paramsOpen.value = true
      if (!table.value && tableOptions.value.length) {
        table.value = tableOptions.value[0]
        loadSchema()
      }
    }
  },
  { immediate: true },
)
watch(table, () => {
  if (!props.analysis) { result.value = null; loadSchema() }
})
</script>

<template>
  <AnalysisHead :analysis="analysis" placeholder="New library test">
    <template #actions>
      <Button
        label="Save"
        icon="aw-icon aw-icon-save"
        size="small"
        :severity="analysis && !dirty ? 'secondary' : undefined"
        :outlined="Boolean(analysis) && !dirty"
        :disabled="!ready"
        :loading="saving"
        @click="save"
      />
      <Button v-if="result || detail" label="Export" icon="aw-icon aw-icon-file-spreadsheet" severity="secondary" size="small" outlined :loading="exporting" @click="exportExcel" />
      <UiOverflowMenu v-if="analysis" :items="menuItems" tooltip="More procedure actions" />
    </template>
  </AnalysisHead>

  <!-- What this procedure concluded. Absent while creating: nothing has run. -->
  <AnalysisVerdict
    v-if="analysis"
    :classification="detail?.classification ?? analysis.classification"
    :state="detail?.state ?? analysis.state"
    :result="effectiveResult"
    :busy="running"
    @openRun="openRun"
    @run="run"
  />

  <UiExecutionStory
    v-if="analysis"
    :ai-authored="analysis.source === 'ai'"
    engine-label="Library test"
    :rule-title="selected?.label || 'Predefined audit test'"
    :scope-label="scopeLabel"
    :ran="Boolean(effectiveResult)"
    :population="effectiveResult?.population ?? null"
    :tested="effectiveResult?.tested ?? null"
    :exceptions="effectiveResult?.exception_count ?? 0"
    :tracks-exceptions="tracksExceptions"
    :verdict-text="effectiveResult?.verdict_text ?? null"
    :execution-error="effectiveResult?.error ?? detail?.error ?? null"
    :human-title="humanStory.title"
    :human-detail="humanStory.detail"
    :human-tone="humanStory.tone"
    :human-badge="humanStory.badge"
  />

  <p v-if="analysis && dirty" class="dirty-note">
    <i class="aw-icon aw-icon-pencil" /> Unsaved changes. Running saves them first, so the
    recorded result always matches the definition that produced it.
  </p>

  <!-- What this saved procedure found — leads above the folded definition so
       executives and auditors see the flagged rows (and chart) immediately. -->
  <div v-if="analysis && !result && (exceptions?.frame?.rows.length || detail?.frame)" class="result">
    <div class="result-bar">
      <div v-if="exceptions?.frame?.rows.length && detail?.frame" class="result-tabs" role="tablist" aria-label="Result view">
        <button
          type="button"
          role="tab"
          :aria-selected="resultView === 'exceptions'"
          class="result-tab"
          :class="{ active: resultView === 'exceptions' }"
          @click="resultView = 'exceptions'"
        >
          <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />
          Flagged rows ({{ (exceptions.exception_count || exceptions.frame.rows.length).toLocaleString() }})
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
      <h4 v-else-if="exceptions?.frame?.rows.length" class="result-title">
        Flagged rows <span class="muted">({{ (exceptions.exception_count || exceptions.frame.rows.length).toLocaleString() }})</span>
      </h4>
      <h4 v-else class="result-title">
        Current result <span class="muted">({{ (detail?.total_rows ?? 0).toLocaleString() }} row{{ (detail?.total_rows ?? 0) === 1 ? '' : 's' }})</span>
      </h4>
      <span
        v-if="resultView === 'exceptions' && exceptions && exceptions.exception_count > exceptions.retained"
        class="muted retention-note"
      >
        Showing {{ exceptions.retained.toLocaleString() }} of {{ exceptions.exception_count.toLocaleString() }} — Export for all
      </span>
    </div>

    <FrameTable
      v-if="resultView === 'exceptions' && exceptions?.frame?.rows.length"
      :frame="exceptions.frame"
      scrollHeight="38vh"
    />
    <ChartView
      v-else-if="detail?.frame"
      :frame="detail.frame"
      :viz="detail.viz"
      height="280px"
    />
  </div>
  <p v-else-if="analysis && !result && detail?.error" class="analysis-error">
    <i class="aw-icon aw-icon-triangle-alert" /> {{ detail.error }}
  </p>
  <div v-else-if="analysis && !result && loadingCurrent" class="loading-current">
    <i class="aw-icon aw-icon-spin aw-icon-loader-circle" /> Loading current result…
  </div>

  <!-- The definition: folded behind a single toggle on saved procedures so the
       verdict, execution story, and chart lead; always open while creating. -->
  <div :class="analysis ? 'definition-disclosure' : 'definition-create'">
    <div v-if="analysis" class="definition-disclosure-head">
      <button
        type="button"
        class="section-toggle"
        :aria-expanded="definitionOpen || pickerOpen || dirty"
        @click="definitionOpen = !definitionOpen"
      >
        <i :class="(definitionOpen || pickerOpen || dirty) ? 'aw-icon aw-icon-chevron-down' : 'aw-icon aw-icon-chevron-right'" />
        <i class="aw-icon aw-icon-sliders-horizontal" aria-hidden="true" />
        <span>Test definition &amp; parameters</span>
        <span v-if="selected" class="hint">· {{ selected.label }}</span>
      </button>
    </div>

    <div v-if="!analysis || definitionOpen || pickerOpen || dirty" class="definition-body">
      <p v-if="props.analysis?.alignment" class="muted population">
        Population: {{ props.analysis.alignment.root }}.
        <span v-for="hop in props.analysis.alignment.joins" :key="hop.name">
          {{ hop.left_on.join(', ') }} → {{ hop.right }}.{{ hop.right_on.join(', ') }}.
        </span>
      </p>

      <div class="definition-head">
        <label class="title-field">
          <span>Title</span>
          <InputText v-model="title" placeholder="What this procedure tests" size="small" />
        </label>
        <span class="grow" />
        <Select v-model="table" :disabled="!!props.analysis" :options="tableOptions" placeholder="Table" size="small" style="min-width: 12rem" />
      </div>
      <div v-if="selected && !pickerOpen" class="test-strip">
        <i :class="selected.icon" />
        <div class="test-strip-text">
          <strong>{{ selected.label }}</strong>
          <span>{{ selected.description }}</span>
        </div>
        <Button label="Change test" icon="aw-icon aw-icon-layout-grid" severity="secondary" size="small" outlined @click="pickerOpen = true" />
      </div>

      <div v-if="pickerOpen" class="picker">
        <div v-if="selected" class="picker-head">
          <Button label="Keep current" icon="aw-icon aw-icon-x" text size="small" @click="pickerOpen = false" />
        </div>
        <template v-for="group in testGroups" :key="group.name">
          <p class="group-title">{{ group.name }}</p>
          <div class="test-grid">
            <button
              v-for="test in group.tests"
              :key="test.id"
              class="test-card"
              :class="{ active: selected?.id === test.id }"
              @click="pick(test)"
            >
              <i :class="test.icon" />
              <strong>{{ test.label }}</strong>
              <span>{{ test.description }}</span>
            </button>
          </div>
        </template>
      </div>

      <div v-if="selected && !pickerOpen" class="run-panel">
        <button class="section-toggle" @click="paramsOpen = !paramsOpen">
          <i :class="paramsOpen ? 'aw-icon aw-icon-chevron-down' : 'aw-icon aw-icon-chevron-right'" />
          Parameters
        </button>
        <div v-show="paramsOpen" class="param-form">
          <div v-for="param in selected.params" :key="param.name" class="field">
            <label>{{ param.label }}</label>
            <Select
              v-if="param.kind === 'column'"
              v-model="params[param.name] as string | null"
              :options="columnOptions(param)"
              :showClear="!!param.optional"
              placeholder="Pick a column"
              filter
              style="min-width: 14rem"
            />
            <MultiSelect
              v-else-if="param.kind === 'columns'"
              v-model="params[param.name] as string[]"
              :options="columnOptions(param)"
              display="chip"
              filter
              placeholder="Pick columns"
              style="min-width: 16rem"
            />
            <Select
              v-else-if="param.kind === 'select'"
              v-model="params[param.name] as string | number | null"
              :options="param.options"
              optionLabel="label"
              optionValue="value"
              style="min-width: 12rem"
            />
            <InputNumber
              v-else-if="param.kind === 'number'"
              v-model="params[param.name] as number"
              :useGrouping="false"
              style="width: 8rem"
            />
          </div>
          <Button label="Preview" icon="aw-icon aw-icon-eye" severity="secondary" outlined :disabled="!ready" :loading="previewing" @click="preview" />
          <span class="muted run-hint">
            A preview shows what these parameters return now; it records nothing.
          </span>
        </div>
      </div>
    </div>
  </div>

  <div v-if="result" class="result">
    <div class="result-head" :data-verdict="result.verdict">
      <span class="result-icon"><i :class="result.verdict === 'ok' ? 'aw-icon aw-icon-circle-check' : result.verdict === 'fail' ? 'aw-icon aw-icon-circle-x' : result.verdict === 'warn' ? 'aw-icon aw-icon-triangle-alert' : 'aw-icon aw-icon-info'" /></span>
      <div><p class="eyebrow">Preview — not recorded</p><h3>{{ result.title }}</h3></div>
      <span class="grow" />
      <Tag :value="result.verdict_text || result.verdict" :severity="verdictSeverity[result.verdict]" />
    </div>

    <div class="stat-cards" style="margin: 0.75rem 0 1rem">
      <div v-for="stat in result.stats" :key="stat.label" class="stat-card">
        <div class="label">{{ stat.label }}</div>
        <div class="value">{{ stat.value }}</div>
      </div>
    </div>

    <ChartView v-if="chartable" :frame="result.summary!" :viz="result.viz!" height="280px" />

    <template v-if="result.summary">
      <h4>Summary <span class="muted" v-if="result.summary_rows > result.summary.rows.length">(first {{ result.summary.rows.length }} of {{ result.summary_rows.toLocaleString() }})</span></h4>
      <FrameTable :frame="result.summary" scrollHeight="34vh" />
    </template>

    <template v-if="result.detail">
      <h4>
        Detail rows
        <span class="muted" v-if="result.detail_rows > result.detail.rows.length">
          (previewing {{ result.detail.rows.length }} of {{ result.detail_rows.toLocaleString() }} — export for all)
        </span>
      </h4>
      <FrameTable :frame="result.detail" scrollHeight="34vh" />
    </template>
  </div>

  <AnalysisFooter v-if="analysis" :analysis="analysis" />
</template>

<style scoped>
.result { display: flex; flex-direction: column; gap: .6rem; flex: none; min-width: 0; }
.result-bar { display: flex; align-items: center; justify-content: space-between; gap: .6rem; flex-wrap: wrap; }
.result-title { margin: 0; }
.retention-note { font-size: var(--aw-text-xs); }
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
.result-tab .aw-icon-triangle-alert { color: var(--aw-danger); }

.definition-disclosure {
  display: flex;
  flex-direction: column;
  flex: none;
  gap: .75rem;
  padding: .65rem .85rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
}
.definition-disclosure-head { display: flex; align-items: center; gap: .5rem; }
.definition-body { display: flex; flex-direction: column; }
.section-toggle .hint { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 500; }
.section-toggle:hover { color: var(--aw-teal); }

.definition-head {
  display: flex; align-items: center; gap: var(--aw-space-2); flex-wrap: wrap;
  margin-bottom: var(--aw-space-3);
}
.definition-head .grow { flex: 1; }
.population { margin: 0 0 var(--aw-space-2); font-size: var(--aw-text-sm); }

.dirty-note {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: -0.5rem 0 1rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}

.test-strip {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.6rem 0.9rem;
  margin-bottom: 1rem;
}
.test-strip > i { color: var(--aw-teal-600); font-size: var(--aw-text-lg); }
.test-strip-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}
.test-strip-text span {
  font-size: var(--aw-text-sm);
  color: var(--aw-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.picker { margin-bottom: 1rem; }
.picker-head {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.6rem;
  margin-bottom: 0.5rem;
}
.group-title {
  margin: 0.9rem 0 0.45rem;
  font-size: var(--aw-text-sm);
  font-weight: 600;
  color: var(--aw-muted);
}
.group-title:first-of-type { margin-top: 0; }

.test-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 0.75rem;
}
.test-card {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  text-align: left;
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.9rem 1rem;
  cursor: pointer;
  font: inherit;
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.test-card:hover { border-color: var(--aw-teal-line); }
.test-card.active { border-color: var(--aw-teal-600); box-shadow: 0 0 0 1px var(--aw-teal-600); }
.test-card i { color: var(--aw-teal-600); font-size: var(--aw-text-lg); }
.test-card span { font-size: var(--aw-text-sm); color: var(--aw-muted); line-height: 1.35; }

.run-panel {
  background: var(--aw-panel);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
}
.section-toggle {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-size: var(--aw-text-sm);
  font-weight: 600;
  color: var(--aw-ink-soft);
  cursor: pointer;
}
.section-toggle i { font-size: var(--aw-text-xs); }
.param-form {
  display: flex;
  flex-wrap: wrap;
  gap: 0.9rem;
  align-items: flex-end;
  margin-top: 0.75rem;
}
.run-hint { font-size: var(--aw-text-sm); align-self: center; }
.muted { color: var(--aw-muted); }

.result-head { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; padding: .85rem 1rem; border: 1px solid var(--aw-border); border-left: 4px solid var(--aw-info); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.result-head[data-verdict='ok'] { border-left-color: var(--aw-ok); background: var(--aw-teal-soft); }
.result-head[data-verdict='warn'] { border-left-color: var(--aw-warn); background: var(--aw-warn-soft); }
.result-head[data-verdict='fail'] { border-left-color: var(--aw-danger); background: var(--aw-danger-soft); }
.result-head .eyebrow { margin-bottom: .1rem; }
.result-head .grow { flex: 1; }
.result-icon { font-size: var(--aw-text-xl); color: var(--aw-info); }
.result-head[data-verdict='ok'] .result-icon { color: var(--aw-ok); }
.result-head[data-verdict='warn'] .result-icon { color: var(--aw-warn); }
.result-head[data-verdict='fail'] .result-icon { color: var(--aw-danger); }
.result-head h3 { margin: 0; }
h4 { margin: 1rem 0 0.5rem; }
.loading-current { display: flex; align-items: center; gap: 0.4rem; padding: 1rem 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
</style>
