<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { api } from '../api'
import JsonTree from '../components/debug/JsonTree.vue'

type AnyRecord = Record<string, any>
const props = defineProps<{ id: string }>()
const route = useRoute(); const router = useRouter(); const toast = useToast(); const confirm = useConfirm()
const views = [
  { id: 'overview', label: 'Overview', icon: 'pi pi-gauge' },
  { id: 'steps', label: 'Steps', icon: 'pi pi-check-square' },
  { id: 'timeline', label: 'Timeline', icon: 'pi pi-chart-bar' },
  { id: 'graph', label: 'Plan graph', icon: 'pi pi-sitemap' },
  { id: 'calls', label: 'LLM calls', icon: 'pi pi-comments' },
  { id: 'events', label: 'Raw events', icon: 'pi pi-list' },
  { id: 'state', label: 'State', icon: 'pi pi-code' },
]
const view = ref(String(route.query.view || 'overview'))
const overview = ref<AnyRecord | null>(null); const runs = ref<AnyRecord[]>([])
const selectedRunId = ref(String(route.query.run || '')); const detail = ref<AnyRecord | null>(null)
const calls = ref<AnyRecord[]>([]); const selectedCall = ref<AnyRecord | null>(null)
const events = ref<AnyRecord[]>([]); const selectedTransition = ref<AnyRecord | null>(null)
const steps = ref<AnyRecord | null>(null); const rollingBack = ref('')
const beforeSnapshot = ref<AnyRecord | null>(null); const afterSnapshot = ref<AnyRecord | null>(null)
const jsonSearch = ref(''); const artifactFilter = ref(''); const loading = ref(true); const live = ref(false)
let source: EventSource | null = null; let refreshTimer: number | undefined

function runLabel(item: AnyRecord) {
  const match = String(item.id || '').match(/^\d{8}-(\d{2})(\d{2})(\d{2})-([a-z0-9]+)$/i)
  const time = match ? `${match[1]}:${match[2]}:${match[3]}` : String(item.id || '').slice(0, 16)
  return `${time} · ${String(item.status || 'unknown').replaceAll('_', ' ')}`
}
const runOptions = computed(() => [
  { label: 'All activity', value: '', title: 'All workspace activity' },
  ...runs.value.map(item => ({ label: runLabel(item), value: item.id, title: `${item.id} · ${item.status}` })),
])
const selectedRunTitle = computed(() => runOptions.value.find(item => item.value === selectedRunId.value)?.title || 'All workspace activity')
const run = computed(() => detail.value?.run)
const metrics = computed(() => detail.value?.metrics || {})
const visibleCalls = computed(() => selectedRunId.value
  ? calls.value.filter(item => item.correlation?.run_id === selectedRunId.value)
  : calls.value)
const historicalTurnCount = computed(() => selectedRunId.value
  ? Number(run.value?.usage?.llm_turns || 0)
  : runs.value.reduce((total, item) => total + Number(item.usage?.llm_turns || 0), 0))
const telemetryStartedAt = computed(() => events.value[0]?.at || null)
const graphActions = computed(() => (run.value?.actions || []).slice(0, 60))
const selectedAction = computed(() => graphActions.value.find((item: AnyRecord) => item.id === route.query.action) || null)
const transitions = computed(() => detail.value?.state_transitions || overview.value?.recent_transitions || [])
const filteredChanges = computed(() => (selectedTransition.value?.changes || []).filter((item: AnyRecord) => {
  const text = `${item.path} ${JSON.stringify(item)}`.toLowerCase()
  return (!jsonSearch.value || text.includes(jsonSearch.value.toLowerCase())) && (!artifactFilter.value || text.includes(artifactFilter.value.toLowerCase()))
}))

function formatMs(value: unknown) {
  const ms = Number(value || 0); if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${(ms / 60000).toFixed(1)} min`
}
function severity(status: string) {
  if (['completed', 'succeeded'].includes(status)) return 'success'
  if (['failed', 'interrupted', 'blocked'].includes(status)) return 'danger'
  if (['running', 'executing'].includes(status)) return 'info'
  return 'secondary'
}
async function loadAll() {
  loading.value = true
  try {
    const [summary, runPage, callPage, eventPage] = await Promise.all([
      api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/overview`),
      api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/runs?limit=250`),
      api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/calls?limit=250`),
      api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/events?limit=250`),
    ])
    overview.value = summary; runs.value = runPage.items; calls.value = callPage.items; events.value = eventPage.items
    if (!selectedRunId.value && runs.value.length) selectedRunId.value = runs.value[0].id
    await loadRun()
    const callId = String(route.query.call || '')
    if (callId) await inspectCall(callId)
    const transitionId = String(route.query.transition || '')
    if (transitionId) await inspectTransition(transitionId)
  } catch (error) { toast.add({ severity: 'error', summary: 'Debug console', detail: String(error), life: 6000 }) }
  finally { loading.value = false }
}
async function loadRun() {
  if (!selectedRunId.value) { detail.value = null; steps.value = null; return }
  ;[detail.value, steps.value] = await Promise.all([
    api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/runs/${selectedRunId.value}`),
    api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/runs/${selectedRunId.value}/steps`),
  ])
}

function formatBytes(value: unknown) {
  const bytes = Number(value || 0)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * Roll one completed step back to the checkpoint taken before it ran.
 *
 * The plan is fetched first and shown in full, because an exact restore also
 * *removes* whatever was created after the step — including a document an
 * auditor uploaded since. Confirming a count is a decision; confirming an
 * unexplained "roll back?" is not.
 */
async function rollBack(step: AnyRecord) {
  const checkpoint = step.checkpoint
  if (!checkpoint) return
  let plan: AnyRecord
  try {
    plan = await api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/checkpoints/${checkpoint.id}/plan`)
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Rollback unavailable', detail: String(error), life: 8000 })
    return
  }
  const parts = [
    plan.changed.length ? `${plan.changed.length} file${plan.changed.length === 1 ? '' : 's'} reverted` : '',
    plan.restored.length ? `${plan.restored.length} restored` : '',
    plan.removed.length ? `${plan.removed.length} created since then deleted` : '',
  ].filter(Boolean)
  const sample = plan.removed.slice(0, 3).join(', ')
  confirm.require({
    header: `Roll back “${step.title || step.capability}”?`,
    icon: 'pi pi-history',
    message: [
      `The engagement returns to the state it was in before this step ran: ${parts.join(', ')}.`,
      plan.removed.length ? ` Deleted: ${sample}${plan.removed.length > 3 ? ` and ${plan.removed.length - 3} more` : ''}.` : '',
      ' Imported data, agent run history and chats are not affected.',
    ].filter(Boolean).join(''),
    acceptLabel: 'Roll back',
    rejectLabel: 'Cancel',
    acceptProps: { severity: 'danger' },
    accept: async () => {
      rollingBack.value = String(step.stage_id || '')
      try {
        const result = await api.post<AnyRecord>(
          `/api/workspaces/${props.id}/debug/checkpoints/${checkpoint.id}/restore?confirm=${encodeURIComponent(props.id)}`,
        )
        toast.add({
          severity: 'success', summary: 'Step rolled back',
          detail: `${result.files_written} file${result.files_written === 1 ? '' : 's'} restored, ${result.files_deleted} removed. Workspace is now at revision ${result.revision}.`,
          life: 8000,
        })
        await loadAll()
      } catch (error) {
        toast.add({ severity: 'error', summary: 'Rollback failed', detail: String(error), life: 10000 })
      } finally { rollingBack.value = '' }
    },
  })
}
async function inspectCall(id: string) {
  selectedCall.value = await api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/calls/${id}`)
  view.value = 'calls'; await syncQuery({ call: id })
}
async function inspectTransition(id: string) {
  selectedTransition.value = await api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/transitions/${id}`)
  const before = selectedTransition.value?.before_ref?.sha1; const after = selectedTransition.value?.after_ref?.sha1
  ;[beforeSnapshot.value, afterSnapshot.value] = await Promise.all([
    before ? api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/snapshots/${before}`) : Promise.resolve(null),
    after ? api.get<AnyRecord>(`/api/workspaces/${props.id}/debug/snapshots/${after}`) : Promise.resolve(null),
  ])
  view.value = 'state'; await syncQuery({ transition: id })
}
async function syncQuery(extra: AnyRecord = {}) {
  await nextTick()
  const query: AnyRecord = { view: view.value, ...(selectedRunId.value ? { run: selectedRunId.value } : {}), ...extra }
  if (view.value === 'graph' && route.query.action) query.action = route.query.action
  await router.replace({ query })
}
async function chooseView(value: string) { view.value = value; await syncQuery() }
async function chooseAction(id: string) { await router.replace({ query: { view: 'graph', run: selectedRunId.value, action: id } }) }
function scheduleRefresh() { window.clearTimeout(refreshTimer); refreshTimer = window.setTimeout(() => void loadAll(), 250) }
function connect() {
  source = new EventSource(`/api/workspaces/${props.id}/debug/events/stream`)
  source.onopen = () => { live.value = true }
  source.onerror = () => { live.value = false }
  for (const name of ['llm_call_started', 'llm_call_finished', 'state_transition', 'structural_snapshot']) source.addEventListener(name, scheduleRefresh)
}
function clearTelemetry() {
  confirm.require({
    header: 'Clear debug telemetry?', icon: 'pi pi-exclamation-triangle',
    message: 'This permanently removes all local debug calls, events, snapshots, and transitions for this workspace.',
    acceptLabel: 'Clear telemetry', rejectLabel: 'Keep telemetry',
    accept: async () => { await api.del(`/api/workspaces/${props.id}/debug?confirm=${encodeURIComponent(props.id)}`); selectedRunId.value = ''; detail.value = null; selectedCall.value = null; await loadAll() },
  })
}

const timeline = computed<{ items: AnyRecord[]; start: number; span: number }>(() => {
  const items: AnyRecord[] = []
  for (const action of run.value?.actions || []) items.push({ id: action.id, kind: 'action', label: action.type || action.title || action.id, start: action.started_at, end: action.finished_at, status: action.status })
  for (const call of detail.value?.calls || []) items.push({ id: call.id, kind: 'call', label: `${call.correlation?.stage || 'LLM'} · ${call.model || ''}`, start: call.started_at, end: call.finished_at, status: call.status })
  const valid = items.filter(item => item.start); if (!valid.length) return { items: [], start: 0, span: 1 }
  const start = Math.min(...valid.map(item => Date.parse(item.start))); const end = Math.max(...valid.map(item => Date.parse(item.end || new Date().toISOString())))
  return { items: valid.map((item, lane) => ({ ...item, lane, left: ((Date.parse(item.start) - start) / Math.max(1, end - start)) * 100, width: Math.max(.6, ((Date.parse(item.end || new Date().toISOString()) - Date.parse(item.start)) / Math.max(1, end - start)) * 100) })), start, span: end - start }
})
const graphLayout = computed(() => {
  const actions = graphActions.value; const byId = new Map<string, AnyRecord>(actions.map((item: AnyRecord) => [item.id, item]))
  const depths = new Map<string, number>()
  function depth(item: AnyRecord, seen = new Set<string>()): number { if (depths.has(item.id)) return depths.get(item.id)!; if (seen.has(item.id)) return 0; seen.add(item.id); const refs = item.depends_on || item.dependencies || []; const value = refs.length ? 1 + Math.max(0, ...refs.map((id: string) => byId.has(id) ? depth(byId.get(id)!, seen) : 0)) : 0; depths.set(item.id, value); return value }
  actions.forEach((item: AnyRecord) => depth(item)); const rows = new Map<number, AnyRecord[]>()
  actions.forEach((item: AnyRecord) => rows.set(depths.get(item.id) || 0, [...(rows.get(depths.get(item.id) || 0) || []), item]))
  const nodes = actions.map((item: AnyRecord) => { const d = depths.get(item.id) || 0; const row = rows.get(d)!; const index = row.indexOf(item); return { ...item, x: 45 + d * 220, y: 35 + index * 86 } })
  const positions = new Map(nodes.map((item: AnyRecord) => [item.id, item])); const edges: AnyRecord[] = []
  nodes.forEach((item: AnyRecord) => (item.depends_on || item.dependencies || []).forEach((id: string) => { if (positions.has(id)) edges.push({ from: positions.get(id), to: item }) }))
  return { nodes, edges, width: Math.max(900, 300 + Math.max(0, ...nodes.map((item: AnyRecord) => item.x))), height: Math.max(360, 130 + Math.max(0, ...nodes.map((item: AnyRecord) => item.y))) }
})

watch(selectedRunId, async () => { selectedCall.value = null; await loadRun(); await syncQuery() })
watch(() => route.query.view, value => { if (value) view.value = String(value) })
onMounted(async () => { await loadAll(); connect() })
onUnmounted(() => { source?.close(); window.clearTimeout(refreshTimer) })
</script>

<template>
  <div class="debug-page">
    <header class="debug-header">
      <router-link :to="`/workspace/${id}`" class="back"><i class="pi pi-arrow-left" /> Engagement</router-link>
      <div><small>LOCAL DIAGNOSTICS</small><h1>{{ overview?.workspace?.name || id }} Debug Console</h1></div>
      <span class="spacer"/><span class="live" :class="{ on: live }"><i/>{{ live ? 'Live' : 'Reconnecting' }}</span>
      <Button icon="pi pi-trash" label="Clear" severity="danger" outlined size="small" @click="clearTelemetry" />
    </header>
    <div class="debug-shell">
      <aside>
        <nav><button v-for="item in views" :key="item.id" :class="{ active: view === item.id }" @click="chooseView(item.id)"><i :class="item.icon"/><span>{{ item.label }}</span></button></nav>
        <label>Run<Select v-model="selectedRunId" :options="runOptions" optionLabel="label" optionValue="value" class="run-select" :title="selectedRunTitle" fluid /></label>
        <div class="retention"><i class="pi pi-lock"/><span>Stored only in this workspace until you clear it.</span></div>
      </aside>
      <main v-if="!loading">
        <section v-if="view === 'overview'" class="stack">
          <div class="hero"><div><small>TELEMETRY HEALTH</small><h2>{{ overview?.counts?.calls || 0 }} model calls captured</h2><p>Complete safe requests, provider attempts, state changes, and run timing remain local.</p></div><i class="pi pi-wave-pulse"/></div>
          <div class="metric-grid"><article><small>Runs</small><strong>{{ overview?.counts?.runs || 0 }}</strong></article><article><small>State transitions</small><strong>{{ overview?.counts?.transitions || 0 }}</strong></article><article><small>Failed calls</small><strong>{{ overview?.counts?.failed_calls || 0 }}</strong></article><article><small>Parallelism</small><strong>{{ metrics.parallelism_factor || 0 }}×</strong></article></div>
          <div v-if="detail" class="two-col"><article class="panel"><h3>Run timing</h3><dl><template v-for="(value, key) in metrics" :key="key"><template v-if="typeof value === 'number'"><dt>{{ String(key).replaceAll('_',' ') }}</dt><dd>{{ String(key) === 'parallelism_factor' || String(key) === 'retry_waste_ratio' ? value : formatMs(value) }}</dd></template></template></dl></article><article class="panel"><h3>Deterministic cause</h3><pre>{{ JSON.stringify(detail.causal_analysis, null, 2) }}</pre><p v-for="gap in detail.telemetry_gaps" :key="gap" class="notice"><i class="pi pi-info-circle"/>{{ gap }}</p></article></div>
          <article class="panel"><h3>Recent calls</h3><button v-for="call in overview?.recent_calls" :key="call.id" class="row" @click="inspectCall(call.id)"><Tag :value="call.status" :severity="severity(call.status)"/><span><strong>{{ call.correlation?.stage || call.correlation?.purpose || 'Model call' }}</strong><small>{{ call.provider }} / {{ call.model }}</small></span><time>{{ formatMs(call.duration_ms) }}</time><i class="pi pi-chevron-right"/></button><p v-if="!overview?.recent_calls?.length" class="empty">No calls have been recorded yet.</p></article>
        </section>

        <section v-else-if="view === 'steps'" class="stack">
          <div class="section-head">
            <div>
              <h2>Steps</h2>
              <p>Each workflow step, and the checkpoint taken before it ran. Rolling one back returns the engagement's artifacts to that state.</p>
            </div>
            <Tag
              :value="steps?.checkpointing_enabled ? `${steps?.usage?.checkpoints || 0} checkpoints · ${formatBytes(steps?.usage?.blob_bytes)}` : 'Checkpointing off'"
              :severity="steps?.checkpointing_enabled ? 'info' : 'warn'"
            />
          </div>
          <p v-if="steps?.notice" class="notice"><i class="pi pi-info-circle"/>{{ steps.notice }}</p>
          <article class="panel step-list">
            <div v-for="step in steps?.steps || []" :key="step.stage_id" class="step" :class="{ settled: step.settled }">
              <span class="step-index">{{ step.index + 1 }}</span>
              <div class="step-body">
                <strong>{{ step.title || step.capability }}</strong>
                <small>
                  <code>{{ step.capability }}</code>
                  · {{ step.unit_count }} unit{{ step.unit_count === 1 ? '' : 's' }}
                  <template v-if="step.result_refs.length"> · {{ step.result_refs.length }} artifact{{ step.result_refs.length === 1 ? '' : 's' }}</template>
                  <template v-if="step.duration_ms"> · {{ formatMs(step.duration_ms) }}</template>
                </small>
                <small v-if="step.error" class="step-error"><i class="pi pi-exclamation-triangle"/>{{ step.error }}</small>
              </div>
              <Tag :value="step.status" :severity="severity(step.status)"/>
              <Button
                v-if="step.checkpoint"
                icon="pi pi-history"
                label="Roll back"
                severity="danger"
                outlined
                size="small"
                :loading="rollingBack === step.stage_id"
                v-tooltip.left="`Restore the ${step.checkpoint.file_count} files captured before this step ran`"
                @click="rollBack(step)"
              />
              <!-- Never a disabled button with no reason: a step without a
                   restore point says why it has none, because that is the fact
                   an operator needs rather than a control that does nothing. -->
              <span v-else class="no-checkpoint" v-tooltip.left="'This step ran before checkpointing, or its restore point has aged out under the retention cap.'">
                <i class="pi pi-ban"/>No checkpoint
              </span>
            </div>
            <p v-if="!(steps?.steps || []).length && !steps?.notice" class="empty">This run scheduled no workflow steps.</p>
          </article>
        </section>

        <section v-else-if="view === 'timeline'" class="stack"><div class="section-head"><div><h2>Execution timeline</h2><p>Calls share one wall-clock scale, so overlap and sequential gaps are visible.</p></div><Tag :value="`${metrics.parallelism_factor || 0}× parallelism`" severity="info"/></div><div class="timeline panel"><div v-for="item in timeline.items" :key="item.id" class="timeline-row"><button class="timeline-label" @click="item.kind === 'call' ? inspectCall(item.id) : chooseAction(item.id)"><i :class="item.kind === 'call' ? 'pi pi-comments' : 'pi pi-bolt'"/><span>{{ item.label }}</span></button><div class="track"><button class="bar" :class="[item.kind, item.status]" :style="{ left: `${item.left}%`, width: `${item.width}%` }" :title="`${item.label} · ${item.status}`" @click="item.kind === 'call' ? inspectCall(item.id) : chooseAction(item.id)"/></div></div><p v-if="!timeline.items.length" class="empty">This run has no timestamped actions or calls. Historical telemetry is never fabricated.</p></div></section>

        <section v-else-if="view === 'graph'" class="stack"><div class="section-head"><div><h2>Plan graph</h2><p>Immutable revisions are used when available; the console caps rendering at 60 actions.</p></div><Tag :value="detail?.graph_telemetry?.available ? `${detail.graph_snapshots.length} revisions` : 'Historical gap'" :severity="detail?.graph_telemetry?.available ? 'success' : 'warn'"/></div><p v-if="detail?.graph_telemetry?.legacy_notice" class="notice"><i class="pi pi-info-circle"/>{{ detail.graph_telemetry.legacy_notice }}</p><div class="graph panel"><svg :viewBox="`0 0 ${graphLayout.width} ${graphLayout.height}`" :style="{ minWidth: `${graphLayout.width}px`, height: `${graphLayout.height}px` }"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs><line v-for="(edge,index) in graphLayout.edges" :key="index" :x1="edge.from.x + 170" :y1="edge.from.y + 25" :x2="edge.to.x" :y2="edge.to.y + 25" marker-end="url(#arrow)"/><g v-for="node in graphLayout.nodes" :key="node.id" :transform="`translate(${node.x} ${node.y})`" role="button" tabindex="0" @click="chooseAction(node.id)"><rect width="170" height="52" rx="8" :class="[node.status, { selected: route.query.action === node.id }]"/><text x="10" y="20">{{ (node.type || node.id).slice(0,22) }}</text><text x="10" y="39" class="status-text">{{ node.status }} · {{ node.id.slice(-8) }}</text></g></svg><p v-if="!graphLayout.nodes.length" class="empty">Schema-v1 history is represented by its stage/task tree in the raw run record.</p></div><article v-if="selectedAction" class="panel"><div class="section-head"><h3>{{ selectedAction.id }}</h3><Tag :value="selectedAction.status" :severity="severity(selectedAction.status)"/></div><JsonTree :value="selectedAction" path="$" :search="jsonSearch"/><InputText v-model="jsonSearch" placeholder="Search action JSON or correlations" class="json-search"/></article></section>

        <section v-else-if="view === 'calls'" class="inspector-layout">
          <div class="list-pane">
            <div class="section-head"><div><h2>LLM calls</h2><p>{{ selectedRunId ? 'Calls correlated to this run.' : 'All safe raw transport records.' }}</p></div></div>
            <button v-for="call in visibleCalls" :key="call.id" class="call-card" :class="{ selected: selectedCall?.id === call.id }" @click="inspectCall(call.id)"><span><Tag :value="call.status" :severity="severity(call.status)"/><small>{{ call.attempt_count }} attempt{{ call.attempt_count === 1 ? '' : 's' }}</small></span><strong>{{ call.correlation?.stage || call.correlation?.purpose || call.id }}</strong><small>{{ call.provider }} / {{ call.model }} · {{ formatMs(call.duration_ms) }}</small></button>
            <div v-if="!visibleCalls.length" class="call-gap"><i class="pi pi-history"/><strong>No captured call records</strong><p v-if="historicalTurnCount">The durable run ledger reports {{ historicalTurnCount }} model turn{{ historicalTurnCount === 1 ? '' : 's' }}, but this work predates full Debug tracing. Raw prompts and responses cannot be reconstructed.</p><p v-else>No model call has been made in this {{ selectedRunId ? 'run' : 'workspace' }} since full Debug tracing was enabled.</p><small v-if="telemetryStartedAt">Workspace Debug telemetry begins at {{ telemetryStartedAt }}.</small></div>
          </div>
          <article class="json-inspector panel"><div class="section-head"><h3>{{ selectedCall?.id || (visibleCalls.length ? 'Select a call' : 'Historical telemetry gap') }}</h3><Tag v-if="selectedCall" :value="selectedCall.status" :severity="severity(selectedCall.status)"/></div><JsonTree v-if="selectedCall" :value="selectedCall" path="$" :search="jsonSearch"/><InputText v-if="selectedCall" v-model="jsonSearch" placeholder="Search JSON paths or values" class="json-search"/><div v-else-if="!visibleCalls.length" class="gap-detail"><i class="pi pi-info-circle"/><h3>Nothing is hidden by a filter</h3><p>Complete request and response records only exist for calls made after the Debug store was installed. The console labels this gap rather than fabricating historical data.</p><p>Start a new model-backed assistant question or audit action; its call will appear here live.</p></div></article>
        </section>

        <section v-else-if="view === 'events'" class="stack"><div class="section-head"><div><h2>Raw events</h2><p>Append-only workspace debug feed. Agent-run events remain included in run detail.</p></div></div><article class="panel event-list"><div v-for="event in events" :key="event.id" class="event"><time>#{{ event.seq }} · {{ event.at }}</time><Tag :value="event.type" severity="secondary"/><pre>{{ JSON.stringify(event.data, null, 2) }}</pre></div><p v-if="!events.length" class="empty">No debug events recorded.</p></article></section>

        <section v-else-if="view === 'state'" class="state-layout"><div class="list-pane"><div class="section-head"><div><h2>State transitions</h2><p>Click a change to answer “why did this change?”</p></div></div><button v-for="item in transitions" :key="item.id" class="transition-card" :class="{ selected: selectedTransition?.id === item.id }" @click="inspectTransition(item.id)"><strong>{{ item.trigger }}</strong><small>{{ item.at }}</small><span>{{ item.changed_paths?.length || 0 }} paths · {{ item.correlation?.action_id || item.correlation?.run_id || 'workspace' }}</span></button></div><div class="state-detail"><div class="state-tools"><InputText v-model="jsonSearch" placeholder="JSON-path search"/><InputText v-model="artifactFilter" placeholder="Artifact/provenance filter"/></div><article v-if="selectedTransition" class="panel"><div class="section-head"><div><h3>{{ selectedTransition.trigger }}</h3><p>{{ selectedTransition.correlation?.run_id }} {{ selectedTransition.correlation?.action_id }}</p></div><Tag :value="selectedTransition.kind" severity="info"/></div><div class="change-list"><details v-for="change in filteredChanges" :key="change.path"><summary><Tag :value="change.change" :severity="change.change === 'removed' ? 'danger' : change.change === 'added' ? 'success' : 'secondary'"/><code>{{ change.path }}</code></summary><pre>{{ JSON.stringify(change, null, 2) }}</pre></details></div><div class="snapshot-compare"><div><h4>Before snapshot</h4><JsonTree :value="beforeSnapshot?.payload ?? null" path="$" :search="jsonSearch"/></div><div><h4>After snapshot</h4><JsonTree :value="afterSnapshot?.payload ?? null" path="$" :search="jsonSearch"/></div></div></article><p v-else class="empty panel">Select a transition to inspect its provenance, path-level diff, and before/after snapshot trees.</p></div></section>
      </main>
      <main v-else class="loading"><i class="pi pi-spin pi-spinner"/> Loading local telemetry…</main>
    </div>
  </div>
</template>

<style scoped>
.debug-page{height:100%;min-height:0;display:flex;flex-direction:column;background:var(--aw-canvas);color:var(--aw-ink)}.debug-header{height:4.4rem;display:flex;align-items:center;gap:1rem;padding:0 1.25rem;background:var(--aw-ink-strong);color:var(--aw-on-dark);border-bottom:1px solid var(--aw-navy-800)}.debug-header h1{margin:.08rem 0 0;font-size:var(--aw-text-lg)}.debug-header small{color:var(--aw-mint);font-size:var(--aw-text-2xs);font-weight:800;letter-spacing:.14em}.back{display:flex;align-items:center;gap:.45rem;color:var(--aw-on-navy);text-decoration:none;font-size:var(--aw-text-sm);padding-right:1rem;border-right:1px solid var(--aw-navy-800)}.spacer{flex:1}.live{display:flex;gap:.4rem;align-items:center;color:var(--aw-on-navy-muted);font-size:var(--aw-text-sm)}.live i{width:.48rem;height:.48rem;border-radius:50%;background:var(--aw-warn)}.live.on i{background:var(--aw-ok);box-shadow:0 0 0 4px rgb(52 211 153/12%)}.debug-shell{flex:1;min-height:0;display:grid;grid-template-columns:13rem 1fr}.debug-shell>aside{display:flex;flex-direction:column;gap:1.25rem;padding:1rem .75rem;border-right:1px solid var(--aw-border);background:var(--aw-panel)}.debug-shell nav{display:grid;gap:.2rem}.debug-shell nav button{display:flex;align-items:center;gap:.65rem;border:0;border-radius:var(--aw-radius-control);padding:.62rem .7rem;background:transparent;color:var(--aw-ink-soft);text-align:left;cursor:pointer}.debug-shell nav button.active{background:var(--aw-teal-soft);color:var(--aw-teal);font-weight:700}.debug-shell aside label{display:grid;gap:.4rem;color:var(--aw-muted);font-size:var(--aw-text-xs);font-weight:700;}.retention{display:flex;align-items:flex-start;gap:.45rem;margin-top:auto;padding:.65rem;border-radius:var(--aw-radius-control);background:var(--aw-canvas);color:var(--aw-muted);font-size:var(--aw-text-xs);line-height:1.4}.debug-shell>main{min-width:0;overflow:auto;padding:1.2rem}.stack{display:grid;gap:1rem;max-width:1500px;margin:auto}.hero{display:flex;align-items:center;padding:1.3rem 1.5rem;border-radius:var(--aw-radius-surface);background:linear-gradient(130deg,var(--aw-ink-strong),var(--aw-teal-strong));color:var(--aw-on-dark)}.hero h2{margin:.2rem 0;font-size:var(--aw-text-xl)}.hero p{margin:.2rem 0;color:var(--aw-on-navy-muted)}.hero>i{margin-left:auto;font-size:var(--aw-text-3xl);color:var(--aw-mint)}.metric-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:.8rem}.metric-grid article,.panel{border:1px solid var(--aw-border);border-radius:var(--aw-radius-surface);background:var(--aw-panel);box-shadow:var(--aw-shadow-sm)}.metric-grid article{display:grid;gap:.25rem;padding:1rem}.metric-grid small{color:var(--aw-muted);font-size:var(--aw-text-2xs);font-weight:800;}.metric-grid strong{font-size:var(--aw-text-2xl)}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.panel{padding:1rem;min-width:0}.panel h3,.section-head h2{margin:0}.panel pre,.event pre{overflow:auto;max-height:28rem;padding:.75rem;border-radius:var(--aw-radius-control);background:var(--aw-ink-strong);color:var(--aw-on-navy);font:11px/1.5 'JetBrains Mono Variable',monospace}.panel dl{display:grid;grid-template-columns:1fr auto;gap:.5rem;margin:0}.panel dt{color:var(--aw-muted);text-transform:capitalize}.panel dd{margin:0;font-weight:700}.row{width:100%;display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;gap:.7rem;padding:.65rem .1rem;border:0;border-top:1px solid var(--aw-border);background:transparent;text-align:left;cursor:pointer}.row span{display:grid}.row small,.call-card small,.transition-card small{color:var(--aw-muted-strong)}.row time{font:11px 'JetBrains Mono Variable',monospace}.notice{display:flex;gap:.45rem;padding:.65rem;border:1px solid var(--aw-info-line);border-radius:var(--aw-radius-control);background:var(--aw-info-soft);color:var(--aw-info)}.section-head{display:flex;align-items:center;justify-content:space-between;gap:1rem}.section-head p{margin:.25rem 0 0;color:var(--aw-muted)}.timeline{display:grid;gap:.25rem}.timeline-row{display:grid;grid-template-columns:16rem 1fr;min-height:2.1rem;align-items:center}.timeline-label{display:flex;gap:.45rem;align-items:center;min-width:0;border:0;background:transparent;text-align:left;cursor:pointer}.timeline-label span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.track{position:relative;height:1.25rem;border-left:1px solid var(--aw-border-strong);background:repeating-linear-gradient(90deg,var(--aw-raised) 0,var(--aw-raised) 1px,transparent 1px,transparent 10%)}.bar{position:absolute;top:.15rem;height:.95rem;min-width:3px;border:0;border-radius:var(--aw-radius-control);background:var(--aw-teal-600);cursor:pointer}.bar.call{background:var(--aw-info)}.bar.failed,.bar.interrupted,.bar.blocked{background:var(--aw-danger)}.graph{overflow:auto;padding:.25rem}.graph svg line{stroke:var(--aw-border-strong);stroke-width:1.5}.graph svg marker path{fill:var(--aw-border-strong)}.graph svg g{cursor:pointer}.graph rect{fill:var(--aw-panel);stroke:var(--aw-border-strong);stroke-width:1.5}.graph rect.succeeded{stroke:var(--aw-ok)}.graph rect.failed,.graph rect.blocked{stroke:var(--aw-danger)}.graph rect.running{stroke:var(--aw-info)}.graph rect.selected{stroke-width:4}.graph text{font:11px 'JetBrains Mono Variable',monospace;fill:var(--aw-ink-strong)}.graph .status-text{font-size:9px;fill:var(--aw-muted)}.inspector-layout,.state-layout{display:grid;grid-template-columns:minmax(18rem,25rem) 1fr;gap:1rem;min-height:calc(100vh - 7rem)}.list-pane{display:flex;flex-direction:column;gap:.5rem}.call-card,.transition-card{display:grid;gap:.28rem;padding:.7rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-panel);text-align:left;cursor:pointer}.call-card>span{display:flex;justify-content:space-between;align-items:center}.call-card.selected,.transition-card.selected{border-color:var(--aw-teal-600);box-shadow:0 0 0 2px rgb(13 148 136/12%)}.json-inspector{position:relative;overflow:auto}.json-search{position:sticky;bottom:0;width:100%;margin-top:1rem}.event-list{display:grid;gap:.8rem}.event{display:grid;grid-template-columns:auto auto 1fr;gap:.6rem;align-items:start;border-bottom:1px solid var(--aw-border);padding-bottom:.8rem}.event time{font:10px 'JetBrains Mono Variable',monospace;color:var(--aw-muted)}.event pre{grid-column:1/-1;width:100%;margin:0}.state-detail{display:grid;align-content:start;gap:.7rem}.state-tools{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}.change-list{display:grid;gap:.35rem;max-height:20rem;overflow:auto}.change-list details{border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);padding:.4rem}.change-list summary{display:flex;align-items:center;gap:.55rem;cursor:pointer}.change-list code{font-size:var(--aw-text-xs);overflow-wrap:anywhere}.snapshot-compare{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin-top:1rem}.snapshot-compare>div{min-width:0;max-height:45rem;overflow:auto;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);padding:.7rem}.snapshot-compare h4{position:sticky;top:0;margin:0 0 .6rem;background:var(--aw-panel)}.empty{padding:2rem;text-align:center;color:var(--aw-muted)}.loading{display:grid!important;place-items:center;align-content:center;gap:.5rem;color:var(--aw-muted)}@media(max-width:950px){.debug-shell{grid-template-columns:4rem 1fr}.debug-shell>aside{padding:.7rem .4rem}.debug-shell nav button{justify-content:center}.debug-shell nav span,.debug-shell aside label,.retention{display:none}.metric-grid{grid-template-columns:1fr 1fr}.two-col,.inspector-layout,.state-layout,.snapshot-compare{grid-template-columns:1fr}.timeline-row{grid-template-columns:8rem 1fr}}
.debug-header h1,.hero h2{color:var(--aw-on-dark)}
.debug-shell{grid-template-columns:15rem minmax(0,1fr)}
.debug-shell>aside,.debug-shell aside label{min-width:0}
.run-select{width:100%;max-width:100%;min-width:0;text-transform:none}
.run-select :deep(.p-select-label){min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.call-gap,.gap-detail{display:grid;justify-items:start;gap:.55rem;padding:1rem;border:1px dashed var(--aw-border-strong);border-radius:var(--aw-radius-control);background:var(--aw-canvas);color:var(--aw-ink-soft)}
.call-gap>i,.gap-detail>i{font-size:var(--aw-text-xl);color:var(--aw-teal-600)}
.call-gap p,.gap-detail p{margin:0;line-height:1.5}.call-gap small{color:var(--aw-muted-strong)}
.gap-detail{max-width:36rem;margin:4rem auto;border:0;background:transparent;text-align:left}.gap-detail h3{margin:0;color:var(--aw-ink)}
@media(max-width:950px){.debug-shell{grid-template-columns:4rem minmax(0,1fr)}}
.step-list{display:grid;gap:0}
.step{display:grid;grid-template-columns:1.75rem minmax(0,1fr) auto auto;align-items:center;gap:.75rem;padding:.7rem .1rem;border-top:1px solid var(--aw-border)}
.step:first-child{border-top:0}
.step-index{display:grid;place-items:center;width:1.6rem;height:1.6rem;border-radius:50%;background:var(--aw-canvas);color:var(--aw-muted);font:11px 'JetBrains Mono Variable',monospace}
.step.settled .step-index{background:var(--aw-teal-soft);color:var(--aw-teal)}
.step-body{display:grid;gap:.15rem;min-width:0}
.step-body small{color:var(--aw-muted-strong);overflow-wrap:anywhere}
.step-body code{font-size:var(--aw-text-xs)}
.step-error{color:var(--aw-danger)!important;display:flex;gap:.35rem;align-items:baseline}
.no-checkpoint{display:flex;gap:.35rem;align-items:center;color:var(--aw-muted);font-size:var(--aw-text-xs);white-space:nowrap}
@media(max-width:950px){.step{grid-template-columns:1.75rem minmax(0,1fr);row-gap:.4rem}.step>:nth-child(3),.step>:nth-child(4){grid-column:2}}
</style>
