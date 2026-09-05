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
  AnalyticsParamMeta,
  AnalyticsResult,
  AnalyticsTest,
  ColumnSchema,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import FrameTable from '../FrameTable.vue'
import AnalysisOutcome from './AnalysisOutcome.vue'

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
const title = ref('')
const previewing = ref(false)
const loadingCurrent = ref(false)
const running = ref(false)
const exporting = ref(false)
const saving = ref(false)
const pickerOpen = ref(!props.analysis)
const paramsOpen = ref(true)

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
// useful than silently letting the two drift.
const dirty = computed(() => {
  if (!props.analysis || !selected.value) return false
  const saved = (props.analysis.spec ?? {}) as { test?: string; params?: Record<string, unknown> }
  return (
    saved.test !== selected.value.id
    || JSON.stringify(saved.params ?? {}) !== JSON.stringify(params.value)
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
  if (dirty.value) await save()
  running.value = true
  try {
    detail.value = await api.post<AnalysisDetail>(
      `/api/workspaces/${props.workspace.id}/analyses/${props.analysis.id}/execute`, {},
    )
    result.value = null
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

async function save() {
  if (!selected.value || !table.value) return
  const name = title.value.trim()
  if (!name) {
    toast.add({ severity: 'warn', summary: 'A title is required', life: 3000 })
    return
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
  } catch (error) {
    fail('Save failed', error)
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  if (!props.analysis) return
  confirm.require({
    header: 'Delete analysis',
    message: `Delete "${props.analysis.title}"?`,
    icon: 'pi pi-exclamation-triangle',
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
    agent.state.drawerOpen || agent.toggleDrawer()
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
    // The recorded outcome is stated at the top of the pane; the parameters
    // that produced it stay one click away rather than filling the screen.
    paramsOpen.value = false
  }
  await loadCurrent(a.id)
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
    detail.value = await api.get<AnalysisDetail>(
      `/api/workspaces/${props.workspace.id}/analyses/${analysisId}`,
    )
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
  <p v-if="props.analysis?.alignment" class="muted">
    Population: {{ props.analysis.alignment.root }}.
    <span v-for="hop in props.analysis.alignment.joins" :key="hop.name">
      {{ hop.left_on.join(', ') }} → {{ hop.right }}.{{ hop.right_on.join(', ') }}.
    </span>
  </p>
  <div class="analysis-editor-head">
    <InputText v-model="title" placeholder="Analysis title" class="title-input" />
    <div class="field">
      <Select v-model="table" :disabled="!!props.analysis?.alignment" :options="tableOptions" placeholder="Table" style="min-width: 12rem" />
    </div>
    <span class="grow" />
    <Button
      v-if="analysis"
      label="Run"
      icon="pi pi-play"
      size="small"
      :disabled="!ready"
      :loading="running"
      v-tooltip.bottom="'Execute this procedure and record what it concludes'"
      @click="run"
    />
    <Button
      label="Save"
      icon="pi pi-save"
      size="small"
      :severity="analysis && !dirty ? 'secondary' : undefined"
      :outlined="Boolean(analysis) && !dirty"
      :disabled="!ready"
      :loading="saving"
      @click="save"
    />
    <Button v-if="result || detail" label="Export" icon="pi pi-file-excel" severity="secondary" size="small" outlined :loading="exporting" @click="exportExcel" />
    <Button v-if="analysis" icon="pi pi-trash" severity="danger" text size="small" v-tooltip.bottom="'Delete analysis'" @click="confirmDelete" />
  </div>

  <!-- What this procedure concluded. Absent while creating: nothing has run. -->
  <AnalysisOutcome
    v-if="analysis"
    :classification="detail?.classification ?? analysis.classification"
    :result="detail?.last_result ?? analysis.last_result"
    @openRun="openRun"
  />
  <p v-if="analysis && dirty" class="dirty-note">
    <i class="pi pi-pencil" /> Unsaved changes. Running saves them first, so the
    recorded result always matches the definition that produced it.
  </p>

  <!-- Compact strip once a test is chosen; the full catalog only while picking. -->
  <div v-if="selected && !pickerOpen" class="test-strip">
    <i :class="selected.icon" />
    <div class="test-strip-text">
      <strong>{{ selected.label }}</strong>
      <span>{{ selected.description }}</span>
    </div>
    <Button label="Change test" icon="pi pi-th-large" severity="secondary" size="small" outlined @click="pickerOpen = true" />
  </div>

  <div v-if="pickerOpen" class="picker">
    <div v-if="selected" class="picker-head">
      <Button label="Keep current" icon="pi pi-times" text size="small" @click="pickerOpen = false" />
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
      <i :class="paramsOpen ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" />
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
      <Button label="Preview" icon="pi pi-eye" severity="secondary" outlined :disabled="!ready" :loading="previewing" @click="preview" />
      <span class="muted run-hint">
        A preview shows what these parameters return now; it records nothing.
      </span>
    </div>
  </div>

  <div v-if="result" class="result">
    <div class="result-head" :data-verdict="result.verdict">
      <span class="result-icon"><i :class="result.verdict === 'ok' ? 'pi pi-check-circle' : result.verdict === 'fail' ? 'pi pi-times-circle' : result.verdict === 'warn' ? 'pi pi-exclamation-triangle' : 'pi pi-info-circle'" /></span>
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

  <!-- What this procedure currently returns — loaded the moment it's opened,
       not only after Run. Nothing here is recorded; the outcome banner above
       is the durable conclusion. The test's own suggested chart (bar, line,
       ...) travels on `detail.viz`, computed fresh each time — never the
       static "table" default a saved definition is created with — so
       ChartView renders it directly and only falls back to a plain table
       when the chart doesn't apply. -->
  <div v-else-if="detail?.frame" class="result">
    <h4>Current result <span class="muted">({{ (detail.total_rows ?? 0).toLocaleString() }} row{{ (detail.total_rows ?? 0) === 1 ? '' : 's' }})</span></h4>
    <ChartView :frame="detail.frame" :viz="detail.viz" height="280px" />
  </div>
  <p v-else-if="detail?.error" class="analysis-error">
    <i class="pi pi-exclamation-triangle" /> {{ detail.error }}
  </p>
  <div v-else-if="loadingCurrent" class="loading-current">
    <i class="pi pi-spin pi-spinner" /> Loading current result…
  </div>

</template>

<style scoped>
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
