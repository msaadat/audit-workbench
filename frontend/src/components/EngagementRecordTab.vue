<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import SplitButton from 'primevue/splitbutton'
import type { MenuItem } from 'primevue/menuitem'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../api'
import { plural } from '../format'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav, type WorkspaceDestination } from '../composables/useWorkspaceNavigation'
import type {
  EngagementOpenPoint, EngagementRecordPayload, EngagementStage, WorkspaceSummary,
} from '../types'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiStateIcon, { type UiState } from './ui/UiStateIcon.vue'

/**
 * The engagement record: what this engagement holds, and what it still owes.
 *
 * One list, one row per work product, in the order the audit plan runs them.
 * The row exists because the graph says the stage exists — not because a run
 * filed a milestone for it — and it states what the engagement holds now. Run
 * history is layered on where there is any: what it cost, how many attempts,
 * and the milestone's own account of what it did.
 *
 * That is what makes the ledger survive things it used to disappear for. A
 * workspace whose run folder was lost rendered "Nothing filed yet" over eleven
 * real work products. A stage that produces its artifact without narrating —
 * the report does exactly this — appeared in neither the filed half nor the
 * owed half. A stage that was *running* appeared in neither either, and had to
 * be synthesized from a published vocabulary the server shipped for the
 * purpose.
 *
 * A landing page that only looks backwards asks nothing of the reader. The rule
 * for what it asks first is in `_OPEN_RANK` on the server: reading what the
 * assistant decided outranks running the next stage, because auto mode runs
 * stages by itself and only a person can review.
 *
 * A workflow's stages are keyed by the same capability ids the rows are, so the
 * run in flight lays over the ledger directly: the row being written says so,
 * and the band at the top reports the run instead of proposing work already
 * under way.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ 'import-requested': [] }>()
const toast = useToast()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const chats = useAssistantChat(props.workspace.id)

const data = ref<EngagementRecordPayload | null>(null)
const loading = ref(true)
const starting = ref('')
const expanded = ref<Set<string>>(new Set())

const KNOWN_DESTINATIONS: readonly string[] = [
  'apm', 'cycle', 'rcm', 'chain', 'doc-tests', 'data-tests',
  'findings', 'report', 'documents', 'data', 'query', 'analysis',
]

/** An icon per work product, chosen from what the artifact *is*. */
const FILED_ICONS: Record<string, string> = {
  Sources: 'aw-icon aw-icon-folder-open',
  'Audit planning memorandum': 'aw-icon aw-icon-map',
  'Cycle design': 'aw-icon aw-icon-network',
  'Risk and control matrix': 'aw-icon aw-icon-table',
  'Control conclusions': 'aw-icon aw-icon-square-check',
  'Test programme': 'aw-icon aw-icon-shield',
  'Fieldwork results': 'aw-icon aw-icon-briefcase',
  'Findings register': 'aw-icon aw-icon-flag',
  'Document analyses': 'aw-icon aw-icon-file',
  'Analysis library': 'aw-icon aw-icon-chart-column',
  Report: 'aw-icon aw-icon-file-pen',
  Verification: 'aw-icon aw-icon-layout-grid',
}

async function load() {
  loading.value = true
  try {
    data.value = await api.get<EngagementRecordPayload>(
      `/api/workspaces/${props.workspace.id}/engagement/record`,
    )
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Could not load the engagement record',
      detail: error instanceof ApiError ? error.message : String(error),
      life: 6000,
    })
  } finally {
    loading.value = false
  }
}
void load()

// The record is a projection of committed runs, so a commit while it is on
// screen has to refresh it.
const unsubscribe = agent.onWorkspaceInvalidated(() => { void load() })
onUnmounted(unsubscribe)

// The agent store is normally woken by the assistant thread, which is not
// mounted while the sidecar is collapsed. Without this, opening the record
// during a run — or reloading the page mid-run — shows a ledger that has never
// heard of it.
onMounted(() => { void agent.init() })

/** Ticks only while a run is in flight, so elapsed time on screen moves. */
const now = ref(Date.now())
let ticker = 0
watch(agent.isActive, (active) => {
  window.clearInterval(ticker)
  ticker = 0
  if (!active) return
  now.value = Date.now()
  ticker = window.setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onUnmounted(() => window.clearInterval(ticker))

const stages = computed(() => data.value?.stages ?? [])
const totals = computed(() => data.value?.totals ?? null)
const next = computed(() => data.value?.next ?? null)

/**
 * `2h 14m`, `47s`. Sub-minute work is stated in seconds rather than rounded to
 * "0m", which reads as a broken clock on the two stages that genuinely settle
 * the instant their run starts.
 */
function duration(ms: number | null): string {
  if (ms == null) return '—'
  const seconds = Math.round(ms / 1000)
  if (seconds < 1) return '<1s'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    const rest = seconds % 60
    return rest && minutes < 10 ? `${minutes}m ${rest}s` : `${minutes}m`
  }
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}h ${rest}m` : `${hours}h`
}

function when(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

function clock(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}


/* --- the run in flight ---------------------------------------------------- */

/** What a run's own status is called, for a reader who is not watching it. */
const RUN_STATUS_LABEL: Record<string, string> = {
  queued: 'Queued',
  interpreting: 'Working out what to run',
  executing: 'Running',
  verifying: 'Verifying',
  awaiting_approval: 'Waiting for your approval',
  awaiting_input: 'Waiting for your answer',
  paused: 'Paused',
}

const SETTLED_STAGES = new Set(['succeeded', 'skipped', 'failed', 'cancelled'])

interface LiveStage { status: string; title: string; startedAt: string | null }

/**
 * Capability → what the run in flight is doing with it. A workflow's stages and
 * the record's rows are keyed by the same capability ids, so the live run maps
 * onto the ledger without inventing a second vocabulary for it. Empty whenever
 * no run is active, which is what makes every consumer below a no-op then.
 */
const liveStages = computed(() => {
  const map = new Map<string, LiveStage>()
  if (!agent.isActive.value) return map
  for (const stage of agent.state.run?.workflow?.stages ?? []) {
    map.set(stage.capability, {
      status: stage.status,
      title: stage.title,
      startedAt: stage.started_at ?? null,
    })
  }
  return map
})

/**
 * The stage this page just asked for. A run exists before its route resolves,
 * so for a second or two after the click the stage is in no workflow yet — and
 * the row would offer its Run button again. Held until the run lists it or ends.
 */
const justStarted = ref('')
watch([liveStages, agent.isActive], () => {
  if (!agent.isActive.value || liveStages.value.has(justStarted.value)) justStarted.value = ''
})

/**
 * '' unless the run in flight has this capability still owing. A stage it has
 * already finished is dropped deliberately: the commit that settled it has
 * reloaded the record, and the row is a filed one now.
 */
function liveState(capability: string): '' | 'queued' | 'running' {
  const status = liveStages.value.get(capability)?.status
  if (status === 'running') return 'running'
  if (status === 'queued') return 'queued'
  return capability && capability === justStarted.value ? 'queued' : ''
}

/** How long the stage on this row has been running. */
function liveSince(capability: string): string {
  const startedAt = liveStages.value.get(capability)?.startedAt
  if (!startedAt) return ''
  const started = new Date(startedAt).valueOf()
  return Number.isNaN(started) ? '' : duration(Math.max(0, now.value - started))
}

/**
 * The run's live activity line, attributed to a row only when exactly one stage
 * is running. Activity is reported per run, not per stage, so with two in
 * flight it belongs to the band and to neither row.
 */
const soleRunning = computed(() => {
  const running = [...liveStages.value.entries()].filter(([, stage]) => stage.status === 'running')
  return running.length === 1 ? running[0][0] : ''
})

const activityLine = computed(() => {
  const activity = agent.state.run?.activity
  if (!activity) return ''
  const label = activity.detail || activity.label || ''
  if (activity.total) return `${label}${label ? ' — ' : ''}${activity.current ?? 0} of ${activity.total}`
  return label
})

/**
 * The run in flight, restated for the top of the record. It takes the brief
 * band's place rather than sitting beside it: proposing the next step while a
 * step is running is the staleness this is here to fix.
 */
const live = computed(() => {
  const run = agent.isActive.value ? agent.state.run : null
  if (!run) return null
  const runStages = run.workflow?.stages ?? []
  const running = runStages.find(stage => stage.status === 'running')
  const settled = runStages.filter(stage => SETTLED_STAGES.has(stage.status)).length
  const row = running
    ? stages.value.find(stage => stage.capability === running.capability)
    : undefined
  return {
    status: run.status,
    waiting: run.status === 'awaiting_approval' || run.status === 'awaiting_input',
    headline: row?.headline || row?.filed?.label || running?.title || run.activity?.label
      || RUN_STATUS_LABEL[run.status] || 'Working',
    state: RUN_STATUS_LABEL[run.status] || 'Running',
    step: runStages.length > 1
      ? `step ${Math.min(settled + 1, runStages.length)} of ${runStages.length}`
      : '',
    // How much of the run is behind it, as a fraction. null on a workflow of
    // one stage, where a bar can only read empty or full and says nothing the
    // spinner beside it has not already said.
    progress: runStages.length > 1 ? settled / runStages.length : null,
    since: run.started || run.created,
  }
})

const liveElapsed = computed(() => {
  const since = live.value?.since
  if (!since) return ''
  const started = new Date(since).valueOf()
  return Number.isNaN(started) ? '' : duration(Math.max(0, now.value - started))
})

/** The thread is where a run is watched in detail; the ledger stays on screen. */
function watchRun() {
  agent.openPanel()
}

function destinationFor(target: string): WorkspaceDestination | null {
  return KNOWN_DESTINATIONS.includes(target) ? (target as WorkspaceDestination) : null
}

function destinationOf(stage: EngagementStage): WorkspaceDestination | null {
  return destinationFor(stage.filed?.destination ?? '')
}

function icon(label: string): string {
  return FILED_ICONS[label] ?? 'aw-icon aw-icon-box'
}

/** `27 rows`, or '' where the work product has no meaningful size. */
function size(stage: EngagementStage): string {
  const filed = stage.filed
  if (!filed || filed.count == null) return ''
  if (!filed.unit) return String(filed.count)
  return plural(filed.count, filed.unit, filed.unit_plural || undefined)
}

/**
 * The bare size of what the row holds, or '' where it holds nothing yet. The
 * unit belongs to the work product, not to the count — `Analysis library 24`
 * is a unit, `Analysis library 24 analyses` is a sentence — so the spelled
 * form is the number's title rather than the number.
 *
 * A row that holds nothing states that with its dot and its muted label. It
 * used to say `not yet` where the count goes, which put a word in the one
 * position on the row a reader scans as a number.
 */
function count(stage: EngagementStage): string {
  return stage.held && stage.filed?.count != null ? String(stage.filed.count) : ''
}

/**
 * When the work settled and what it took, as one reading: `10:54 · 6m 54s`.
 * A stage nothing timed keeps its dash rather than being given a zero.
 */
function stamp(stage: EngagementStage): string {
  const past = stage.history
  if (!past) return ''
  const at = clock(past.at)
  const took = duration(past.elapsed_ms)
  return at ? `${at} · ${took}` : took
}

/**
 * Which of the five states the row is in. A run in flight is drawn by the
 * live dot instead, because it is about to change the answer.
 *
 * Holding the work product is not the same as being done: the analysis
 * library holds its definitions while none has a result, and the graph says
 * so in `readiness`. Such a row needs attention, and its reason is printed
 * beside it — a filled "done" dot over sixty-three unrun analyses was the
 * record contradicting itself.
 */
function rowState(stage: EngagementStage): UiState {
  if (stage.held) {
    const issues = ['completed_with_issues', 'needs_review'].includes(stage.history?.status ?? '')
    return issues || stage.readiness.state !== 'satisfied' ? 'attention' : 'done'
  }
  if (stage.capability === leadStage.value) return 'next'
  return stage.blocked_reason.trim() ? 'waiting' : 'not_started'
}

/** The reason a held row needs attention, for the meta cell while it is shut. */
function attentionReason(stage: EngagementStage): string {
  return rowState(stage) === 'attention' ? stage.readiness.reasons[0] ?? '' : ''
}

/**
 * What an owed stage is waiting for, as the first fact on its line: `Waits for
 * the memorandum`. The server writes it as a sentence; beside the row it is one
 * fact among others, so it loses its full stop.
 */
function dependency(stage: EngagementStage): string {
  return stage.blocked_reason.trim().replace(/\.$/, '')
}

/**
 * The line under the title: what the stage says about itself right now.
 *
 * A held stage with run history states what the run recorded. A held stage
 * without it — the run folder is gone, or the stage never narrated — still has
 * the graph's own sentence about what is left, which is the whole reason the
 * row can stand on its own.
 */
function saying(stage: EngagementStage): string {
  return stage.summary || stage.readiness.reasons[0] || ''
}

/**
 * The title of a row: what the run said it did, or what the stage is for.
 *
 * Empty where a stage has neither — a held stage that never narrated says
 * everything it has to say in the card beside this, and repeating the label
 * across two columns states nothing twice.
 */
function title(stage: EngagementStage): string {
  return stage.history?.headline || stage.headline || ''
}

/**
 * What a stage still owes, on a row that already holds its work product. The
 * graph's answer, and deliberately not allowed to contradict the count beside
 * it: thirty findings are filed *and* two observations are undrafted.
 */
function remaining(stage: EngagementStage): string {
  if (!stage.held || !stage.history) return ''
  const reason = stage.readiness.reasons[0] ?? ''
  // Already the row's own warning, on its face; saying it twice is noise.
  return reason === attentionReason(stage) ? '' : reason
}

/**
 * What a collapsed row is standing in for. Silent at a single attempt, because
 * "1 attempt" on every row is noise that hides the rows where it matters.
 */
function attemptNote(stage: EngagementStage): string {
  const history = stage.history
  if (!history) return ''
  const tries = history.attempts.length
  if (tries <= 1) return ''
  const untimed = tries - history.measured_attempts
  if (!untimed) return `${tries} attempts`
  return `${tries} attempts · ${untimed} not timed`
}

/**
 * The summary under an open row, unless the row's own line already said all of
 * it: a one-sentence summary is the fact on the face of a row with nothing to
 * count, and drawing it twice says nothing new.
 */
function bodySaying(stage: EngagementStage): string {
  const text = saying(stage)
  if (!text) return ''
  const shown = facts(stage).some(fact => fact.kind === 'text' && fact.text === text.trim().replace(/\.$/, ''))
  return shown ? '' : text
}

function toggle(stage: EngagementStage) {
  const next = new Set(expanded.value)
  if (next.has(stage.id)) next.delete(stage.id)
  else next.add(stage.id)
  expanded.value = next
}

/* --- how much of a row is drawn -------------------------------------------- */

/**
 * A milestone's body — the sentence, the distribution, the highlights — folds
 * away behind the row, leaving one line per work product. Ten stages of this
 * engagement drew a ledger three screens tall; the same ten fit on one.
 *
 * `full` is the ledger as it was before the fold, kept because reading the
 * record end to end is a real thing to want and clicking ten chevrons to do it
 * is not. The choice is a display preference, so it outlives the workspace.
 */
type Density = 'concise' | 'full'
const DENSITY_KEY = 'aw.record.density'

function storedDensity(): Density {
  try {
    return window.localStorage.getItem(DENSITY_KEY) === 'full' ? 'full' : 'concise'
  } catch {
    // Private-mode storage throws rather than returning null.
    return 'concise'
  }
}

const density = ref<Density>(storedDensity())
const openRows = ref<Set<string>>(new Set())

function setDensity(value: Density) {
  density.value = value
  // Rows opened by hand under one density have nothing to say about the other.
  openRows.value = new Set()
  try {
    window.localStorage.setItem(DENSITY_KEY, value)
  } catch {
    // A preference that cannot be stored is still a preference for this visit.
  }
}

/**
 * Whether this row has anything behind the fold. A stage that has not run has
 * its whole content on the surface — a sentence and a button — so a chevron on
 * it opens nothing, and a row that cannot open should not offer to.
 */
function foldable(stage: EngagementStage): boolean {
  // Deliberately not `saying`, which falls back to a readiness reason. That
  // sentence is already on the face of an owed row, so folding it away would
  // put a chevron on every stage that has not run, opening onto what the row
  // already said.
  return Boolean(
    stage.summary || stage.stats.length || stage.highlights.length
    || attemptNote(stage) || remaining(stage) || stamp(stage),
  )
}

function isOpen(stage: EngagementStage): boolean {
  return density.value === 'full' || openRows.value.has(stage.id)
}

/**
 * Whether anything is drawn under the row's own line. Most of it is the folded
 * body, but a run in flight and a debt left behind are said whether the row is
 * open or shut — they are news, not detail — so the block exists for those too.
 */
function hasBody(stage: EngagementStage): boolean {
  const open = isOpen(stage)
  return Boolean(
    (open && (saying(stage) || remaining(stage) || stage.stats.length
      || stage.highlights.length || attemptNote(stage) || stamp(stage)))
    || liveState(stage.capability)
    || stage.open_points.length,
  )
}

function toggleRow(stage: EngagementStage) {
  if (density.value === 'full' || !foldable(stage)) return
  const next = new Set(openRows.value)
  if (next.has(stage.id)) next.delete(stage.id)
  else next.add(stage.id)
  openRows.value = next
}

/**
 * The row is the hit target, because a 30px line whose only handle is a 16px
 * chevron is a row you miss. Anything that already does something on click —
 * the pill, an open point — keeps its own job.
 */
function rowClick(stage: EngagementStage, event: MouseEvent) {
  const target = event.target as HTMLElement | null
  if (target?.closest('a, button')) return
  toggleRow(stage)
}

interface RowChip { label: string; value: string; severity: string }

/**
 * What the folded body is standing in for, counted. Each chip keeps the colour
 * the block it replaces had, so the rows worth opening are the amber ones; a
 * row with nothing to say carries no chip, which is what makes that legible.
 */
function chips(stage: EngagementStage): RowChip[] {
  const out: RowChip[] = []

  // A distribution is led by its most severe non-zero tier, which is the order
  // the tiers already arrive in. Leading by volume instead made the shut row
  // and the open one disagree in front of the reader: a matrix of 5 high and 12
  // medium showed "12 medium" collapsed and a strip led by 5 high expanded, and
  // the larger number was the less serious one. Zero critical is worth saying in
  // the open row and worth nothing in a one-line summary, and a bucket whose
  // value is not a number cannot be counted at all.
  const counted = stage.stats
    .map(item => ({ ...item, count: typeof item.value === 'number' ? item.value : Number(item.value) }))
    .filter(item => Number.isFinite(item.count) && item.count > 0)[0]
  if (counted) {
    out.push({ label: counted.label, value: String(counted.count), severity: counted.severity })
  }

  if (stage.highlights.length) {
    const severity = stage.highlights.some(item => item.severity === 'error') ? 'error' : 'warning'
    out.push({
      label: stage.highlights.length === 1 ? 'flag' : 'flags',
      value: String(stage.highlights.length),
      severity,
    })
  }

  const attempts = stage.history?.attempts.length ?? 0
  if (attempts > 1) {
    out.push({ label: 'attempts', value: String(attempts), severity: '' })
  }

  return out
}

function openPoint(point: EngagementOpenPoint) {
  const destination = destinationFor(point.destination)
  if (destination) void nav.push(destination)
}

/**
 * Start a stage that has not run. The assistant owns running work, so this is
 * the same request the guided shortcuts make. It used to hand the reader to the
 * console, because the record could not show progress; now that it can, the
 * ledger stays on screen and lights up, with the thread beside it in the
 * sidecar for anyone who wants the detail.
 */
type StartRequest = { prompt: string; outcomes: string[] }

function startOptions(stage: EngagementStage): MenuItem[] {
  return (stage.start?.alternates ?? []).map(alternate => ({
    label: alternate.label,
    note: alternate.note,
    command: () => void start(stage, alternate),
  }))
}

async function start(stage: EngagementStage, alternate?: StartRequest) {
  if (starting.value) return
  // Sources is the one stage the assistant cannot begin. Bringing in the audit
  // file is the auditor's act, so the row hands the shell's import dialog back
  // rather than sending a command nothing would answer.
  if (stage.action === 'import') {
    emit('import-requested')
    return
  }
  if (!stage.start) return
  // A stage may offer narrower outcome sets under its button. The primary
  // click is always the complete one.
  const asked = alternate ?? stage.start
  starting.value = stage.capability
  try {
    await chats.send(asked.prompt, 'act', 'auto', {
      source: 'shortcut',
      requestedOutcomes: asked.outcomes,
    })
    justStarted.value = stage.capability
    agent.openPanel()
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Could not start the work',
      detail: error instanceof ApiError ? error.message : String(error),
      life: 6000,
    })
  } finally {
    starting.value = ''
  }
}

/**
 * The first runnable stage is the only one drawn as a call to action — and
 * nothing is while a run is in flight, which is about to change the answer.
 */
const leadStage = computed(
  () => (agent.isActive.value ? '' : stages.value.find(stage => stage.runnable)?.capability ?? ''),
)

/**
 * Three different numbers, each called what it is. This line read "13 runs
 * across 12 sessions" on an engagement with 14 runs, 13 attempts and 9 chats —
 * every noun shifted one place along, which hid a whole run.
 *
 * The strip above draws what the engagement holds; what it cost to get there
 * is a footnote, and reads as one.
 */
const totalLine = computed(() => {
  const value = totals.value
  if (!value) return ''
  const parts: string[] = []
  if (value.elapsed_ms !== null) parts.push(`${duration(value.elapsed_ms)} of assistant time`)
  if (value.runs) parts.push(plural(value.runs, 'run'))
  if (value.attempts > value.work_products) {
    parts.push(`${plural(value.attempts, 'attempt')} at ${plural(value.runs_that_filed, 'stage')}`)
  }
  return parts.join(' · ')
})

// A run that committed nothing filed nothing. Stating it is more honest than a
// record that silently drops a third of the history.
const quietRuns = computed(() => {
  const value = totals.value
  return value ? Math.max(0, value.runs - value.runs_that_filed) : 0
})

/* --- the phases the record is drawn in ------------------------------------- */

type PhaseState = 'done' | 'current' | 'later'

interface PhaseGroup {
  id: string
  title: string
  stages: EngagementStage[]
  state: PhaseState
  held: number
  total: number
  /** Whether the run in flight is writing one of this phase's stages. */
  live: boolean
}

/**
 * The ledger, cut into the five phases an auditor recognises.
 *
 * The sections come from the payload's `phases` rather than from a list of
 * titles kept here, so a phase renamed or reordered on the server moves on the
 * screen by itself. Row order inside a phase is the plan's order, untouched:
 * the phase groups the ledger, it does not re-sort it.
 */
const groups = computed<PhaseGroup[]>(() => {
  const phases = data.value?.phases ?? []
  const byPhase = new Map<string, EngagementStage[]>()
  for (const stage of stages.value) {
    const rows = byPhase.get(stage.phase)
    if (rows) rows.push(stage)
    else byPhase.set(stage.phase, [stage])
  }

  // Exactly one phase is current, and which one is decided once over the whole
  // list rather than per phase: work in flight names it, the first runnable
  // stage names it otherwise, and an engagement with nothing left owed and
  // nothing running has none. Deciding it per phase is how two of them end up
  // wearing the NEXT badge.
  const owns = (id: string, test: (stage: EngagementStage) => boolean) =>
    (byPhase.get(id) ?? []).some(test)
  const currentId =
    phases.find(phase => owns(
      phase.id,
      stage => stage.capability === leadStage.value || Boolean(liveState(stage.capability)),
    ))?.id
    ?? phases.find(phase => owns(phase.id, stage => !stage.held))?.id
    ?? ''

  return phases.map((phase) => {
    const rows = byPhase.get(phase.id) ?? []
    return {
      id: phase.id,
      title: phase.title,
      stages: rows,
      state: phase.id === currentId
        ? 'current'
        : rows.every(stage => stage.held) ? 'done' : 'later',
      held: rows.filter(stage => stage.held).length,
      total: rows.length,
      live: rows.some(stage => Boolean(liveState(stage.capability))),
    }
  })
})

const currentPhase = computed(() => groups.value.find(group => group.state === 'current')?.id ?? '')

/**
 * Which phases are open: everything except the ones whose turn has not come.
 *
 * A phase that is done is worth reading — it is what the engagement holds, and
 * what the reader came to check. A phase that cannot start yet has nothing to
 * read: four rows of work not done was three quarters of the old screen, and
 * its header already names the stages and what they wait for.
 *
 * Seeded when the record first arrives and reseeded when the current phase
 * moves — finishing a phase should open the phase that follows it — but not on
 * every load, so the toggles a reader makes survive the refresh that follows
 * every commit. The choice is for the visit; nothing stores it.
 */
const openPhases = ref<Set<string>>(new Set())
const phaseSeed = computed(
  () => `${currentPhase.value}|${groups.value.map(group => group.id).join(',')}`,
)
watch(phaseSeed, () => {
  openPhases.value = new Set(
    groups.value.filter(group => group.state !== 'later').map(group => group.id),
  )
}, { immediate: true })

function phaseOpen(group: PhaseGroup): boolean {
  return openPhases.value.has(group.id)
}

function togglePhase(group: PhaseGroup) {
  const next = new Set(openPhases.value)
  if (next.has(group.id)) next.delete(group.id)
  else next.add(group.id)
  openPhases.value = next
}

/**
 * What a phase amounts to: `1 of 4`, or `Not started`.
 *
 * `0 of 4` on work that cannot begin yet reads as a failure rather than as a
 * plan, so a phase whose turn has not come and that holds nothing says so in
 * words. The phase being worked keeps its fraction even at nought: it is the
 * one being counted.
 */
function phaseTally(group: PhaseGroup): string {
  if (group.state !== 'current' && group.held === 0) return 'Not started'
  return `${group.held} of ${group.total}`
}

/** The glyph a phase header carries: settled, half way, or not begun. */
const PHASE_GLYPH: Record<PhaseState, string> = {
  done: 'aw-icon-circle-check',
  current: 'aw-icon-contrast',
  later: 'aw-icon-circle',
}

const PHASE_STATE_LABEL: Record<PhaseState, string> = {
  done: 'Done',
  current: 'In progress',
  later: 'Not started',
}

/* --- the whole plan, as one bar per phase ----------------------------------- */

type ProgressState = 'done' | 'current' | 'live' | 'later'

/**
 * One column per phase: its name, how far through it is, and a bar that is the
 * same fraction. It is the only place the whole engagement is visible at once
 * while the phases below are folded, and it reads in the words the phase
 * headers use, so the two never describe one phase differently.
 *
 * A phase the run is writing is drawn in the run's blue rather than the teal
 * of settled work: work under way is news, and teal would hide it.
 */
const progress = computed(() => groups.value.map(group => {
  const done = group.total > 0 && group.held === group.total
  const state: ProgressState = group.live
    ? 'live'
    : done ? 'done' : group.state === 'current' ? 'current' : 'later'
  return {
    id: group.id,
    title: group.title,
    state,
    status: group.live ? 'Running' : done ? 'Done' : phaseTally(group),
    fill: group.total ? group.held / group.total : 0,
  }
}))

/* --- what a row amounts to, on one line ------------------------------------ */

/**
 * The facts a row states beside its name, in reading order: what it waits for,
 * how much it holds, the parts it opens, and what is wrong with it.
 *
 * The doors used to be bordered chips beside the label, so a Sources row read
 * as three buttons. They are the same links, set as the phrase they are —
 * `84 documents · 25 tables` — with the figure in the ledger face.
 */
type FactKind = 'dep' | 'count' | 'door' | 'owed' | 'text' | 'tool'

interface Fact {
  kind: FactKind
  /** The figure, drawn in the ledger face. Empty on a fact with none. */
  figure: string
  text: string
  title?: string
  to?: WorkspaceDestination | null
}

/** `Documents` → `documents`; `1` → `document`, since the labels are plural. */
function doorNoun(label: string, count: number | null): string {
  const noun = label ? label[0].toLowerCase() + label.slice(1) : label
  return count === 1 && /[^s]s$/.test(noun) ? noun.slice(0, -1) : noun
}

/** The unit beside a count — `analyses` in `63 analyses`. */
function unitWord(stage: EngagementStage): string {
  return size(stage).replace(/^\S+\s*/, '')
}

/** A summary's opening sentence, which is what fits on the row's one line. */
function firstSentence(text: string): string {
  const match = /^.+?[.!?](?=\s|$)/.exec(text.trim())
  return (match ? match[0] : text.trim()).replace(/\.$/, '')
}

function facts(stage: EngagementStage): Fact[] {
  const out: Fact[] = []
  if (!stage.held && dependency(stage)) out.push({ kind: 'dep', figure: '', text: dependency(stage) })
  if (count(stage)) {
    out.push({ kind: 'count', figure: count(stage), text: unitWord(stage), title: size(stage) || undefined })
  }
  const tools: Fact[] = []
  for (const link of stage.links) {
    const to = destinationFor(link.destination)
    if (link.kind === 'tool') {
      tools.push({ kind: 'tool', figure: '', text: link.label, to })
      continue
    }
    const noun = doorNoun(link.label, link.count)
    out.push({
      kind: 'door',
      figure: link.count == null ? '' : String(link.count),
      // A register that holds nothing has no denominator: `0 of 0` is a ratio
      // over nothing.
      text: link.count != null && link.total ? `of ${link.total} ${noun}` : link.count == null ? link.label : noun,
      to,
    })
  }
  const reason = attentionReason(stage)
  if (reason) out.push({ kind: 'owed', figure: '', text: reason })
  if (!out.length) {
    // A row with nothing to count says what it is, or what it is for.
    const text = stage.held ? firstSentence(saying(stage)) || title(stage) : title(stage)
    if (text) out.push({ kind: 'text', figure: '', text })
  }
  return [...out, ...tools]
}

/** What a shut phase is standing in for, which is the work products it covers. */
function phaseNames(group: PhaseGroup): string {
  return group.stages.map(stage => stage.filed?.label || stage.capability).join(' · ')
}
</script>

<template>
  <div class="record">
    <!-- A page title the size of a headline, above a ledger, competes with the
         ledger and wins. The tab bar already says which surface this is, so
         what is left is the controls and a label small enough to be one. -->
    <div class="bar">
      <h2>Engagement record</h2>
      <span class="grow"></span>
      <div class="dens" role="group" aria-label="Row density">
        <button
          type="button"
          :aria-pressed="density === 'concise'"
          @click="setDensity('concise')"
        >Concise</button>
        <button
          type="button"
          :aria-pressed="density === 'full'"
          @click="setDensity('full')"
        >Full</button>
      </div>
      <Button
        class="refresh"
        icon="aw-icon aw-icon-refresh-cw"
        size="small"
        severity="secondary"
        outlined
        aria-label="Refresh the record"
        :loading="loading"
        @click="load"
      />
      <!-- The one thing the record cannot draw as a row. Every other work
           product is a row here, because a row is something the engagement
           holds; the chain is a way of reading one risk across all of them,
           so it files nothing and has nowhere on the ledger to sit. It is a
           link rather than a button because it is a place, not an action. -->
      <RouterLink :to="nav.to('chain')" class="chain">
        <i class="aw-icon aw-icon-network" aria-hidden="true" />Chain
      </RouterLink>
    </div>

    <div v-if="loading && !data" class="loading"><i class="aw-icon aw-icon-spin aw-icon-loader-circle" /> Reading the record…</div>

    <!-- Only a record with no stages at all is empty, which means the graph
         itself could not be read. A workspace at the very start still draws
         every stage it is going to do, and one whose run history is gone still
         draws everything it holds. -->
    <UiEmptyState
      v-else-if="!stages.length"
      icon="aw-icon aw-icon-book-open"
      title="No stages to show"
      detail="The engagement plan could not be read, so there is nothing to lay out yet."
    />

    <template v-else>
      <!-- The whole plan at once, which is the one thing the phases cannot
           show while the later ones are folded. -->
      <section class="progress" aria-label="Progress through the engagement">
        <div v-for="phase in progress" :key="phase.id" class="pcol" :data-state="phase.state">
          <div class="phd">
            <span class="ptl">{{ phase.title }}</span>
            <span class="pss">{{ phase.status }}</span>
          </div>
          <span
            class="ptrack"
            role="progressbar"
            :aria-label="phase.title"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-valuenow="Math.round(phase.fill * 100)"
          ><i :style="{ width: `${Math.round(phase.fill * 100)}%` }" /></span>
        </div>
      </section>

      <!-- While a run is in flight it, not the next step, is the news. The two
           never show together: proposing work that is under way is exactly the
           staleness this band exists to remove. -->
      <section
        v-if="live"
        class="brief live"
        :class="{ stepped: live.progress !== null }"
        :data-wait="live.waiting ? '1' : null"
      >
        <span class="mark">
          <i :class="live.waiting ? 'aw-icon aw-icon-circle-help' : 'aw-icon aw-icon-spin aw-icon-loader-circle'" />
        </span>
        <div class="txt">
          <strong>{{ live.headline }}</strong>
          <span>
            {{ live.state }}<template v-if="live.step"> · {{ live.step }}</template
            ><template v-if="liveElapsed"> · {{ liveElapsed }} so far</template>
          </span>
        </div>
        <!-- How much of the run is behind it. A step count is a reading; the
             bar is the same fact at a glance, and the two are the same number. -->
        <span v-if="live.progress !== null" class="pbar">
          <i :style="{ width: `${Math.round(live.progress * 100)}%` }" />
        </span>
        <Button
          :label="live.waiting ? 'Respond' : 'Watch it'"
          :icon="live.waiting ? 'aw-icon aw-icon-reply' : 'aw-icon aw-icon-sparkles'"
          size="small"
          :severity="live.waiting ? undefined : 'secondary'"
          :outlined="!live.waiting"
          @click="watchRun"
        />
      </section>

      <!-- One card per phase, in plan order. The three states are the whole
           point: a phase that is done gets out of the way, the phase being
           worked is open and says so, and the phases after it are drawn as
           the plan they are rather than as nine rows of "not yet". -->
      <!-- The spine: every phase in one card, in plan order. A phase header is
           a ruled band inside it rather than a card of its own, so the record
           reads as one file and the rows line up down its whole length. A
           phase whose turn has not come folds to its header, which names the
           work products it covers. -->
      <section class="spine" aria-label="The engagement file">
        <section
          v-for="group in groups"
          :key="group.id"
          class="phase"
          :data-state="group.state"
        >
          <button
            type="button"
            class="phead"
            :aria-expanded="phaseOpen(group)"
            @click="togglePhase(group)"
          >
            <i
              class="aw-icon pico"
              :class="PHASE_GLYPH[group.state]"
              role="img"
              :aria-label="PHASE_STATE_LABEL[group.state]"
              :title="PHASE_STATE_LABEL[group.state]"
            />
            <span class="pt">{{ group.title }}</span>
            <span v-if="!phaseOpen(group) && group.total" class="pnames">{{ phaseNames(group) }}</span>
            <span class="grow"></span>
            <span class="pst">{{ phaseTally(group) }}</span>
            <i class="aw-icon aw-icon-chevron-down pchev" aria-hidden="true" />
          </button>

          <ol v-if="phaseOpen(group)" class="ledger">
            <template v-for="stage in group.stages" :key="stage.id">
              <li
                class="row"
                :class="{ shut: !isOpen(stage), ghost: !stage.held, lead: stage.capability === leadStage }"
                :data-status="stage.history?.status || null"
                :data-live="liveState(stage.capability) || null"
                @click="rowClick(stage, $event)"
              >
                <!-- What state the stage is in, said once, in the one column a
                     reader scanning the phase is looking down. A live run keeps
                     its dot: it is the one state that moves. -->
                <span v-if="liveState(stage.capability)" class="dot" aria-hidden="true"></span>
                <UiStateIcon v-else class="state" :state="rowState(stage)" />

                <!-- What the work product is, beside what it is: the icon is
                     the artifact's, the state icon before it is the stage's. -->
                <i v-if="stage.filed" :class="icon(stage.filed.label)" class="wpi" aria-hidden="true" />
                <span v-else class="wpi" aria-hidden="true"></span>

                <span class="name">
                  <component
                    :is="destinationOf(stage) ? RouterLink : 'span'"
                    v-if="stage.filed"
                    :to="destinationOf(stage) ? nav.to(destinationOf(stage)!) : undefined"
                    class="wp"
                    :class="{ linked: !!destinationOf(stage) }"
                  >{{ stage.filed.label }}</component>
                  <span v-else class="none">&#8212;</span>
                </span>

                <!-- What it amounts to, as one line of facts. The doors are the
                     same links they were, set as the phrase they are. -->
                <span class="meta">
                  <template v-for="(fact, index) in facts(stage)" :key="`${fact.kind}:${index}`">
                    <span v-if="index" class="sep" aria-hidden="true">·</span>
                    <component
                      :is="fact.to ? RouterLink : 'span'"
                      :to="fact.to ? nav.to(fact.to) : undefined"
                      :class="fact.kind === 'tool' ? 'door' : fact.kind"
                      :data-kind="fact.kind === 'door' ? 'artifact' : fact.kind === 'tool' ? 'tool' : undefined"
                    ><i v-if="fact.kind === 'tool'" class="aw-icon aw-icon-wrench" aria-hidden="true" /><b
                      v-if="fact.figure"
                      :class="fact.kind === 'count' ? 'ct' : 'n'"
                      :title="fact.title"
                    >{{ fact.figure }}</b>{{ fact.figure ? ' ' : '' }}{{ fact.text }}</component>
                  </template>
                </span>

                <span class="end">
                  <!-- The folded body, counted. Colour survives the fold. -->
                  <template v-if="!isOpen(stage)">
                    <span
                      v-for="chip in chips(stage)"
                      :key="chip.label"
                      class="sig"
                      :data-severity="chip.severity || null"
                    ><b>{{ chip.value }}</b>{{ chip.label }}</span>
                  </template>

                  <span class="act">
                    <!-- Sources is the one stage the assistant cannot begin.
                         Bringing in the audit file is the auditor's own act, so
                         the row hands back the shell's dialog. Drawn as a link
                         unless it is the next step, because importing more is
                         always possible and never the thing to do. -->
                    <Button
                      v-if="stage.action === 'import' && stage.capability === leadStage"
                      label="Import"
                      size="small"
                      @click="start(stage)"
                    />
                    <button
                      v-else-if="stage.action === 'import'"
                      type="button"
                      class="rowlink"
                      @click="start(stage)"
                    >{{ stage.held ? 'Import more' : 'Import' }}</button>
                    <!-- Only the lead stage is drawn as a call to action: a tail
                         of six buttons is a menu, not a next step. A stage
                         offering narrower runs draws them under the button, never
                         beside it - the click stays the complete answer. -->
                    <SplitButton
                      v-else-if="stage.capability === leadStage && stage.start?.alternates.length"
                      label="Run"
                      size="small"
                      :disabled="starting === stage.capability"
                      :model="startOptions(stage)"
                      @click="start(stage)"
                    >
                      <template #item="{ item, props }">
                        <a class="alt" v-bind="props.action">
                          <span>{{ item.label }}</span>
                          <small v-if="item.note">{{ item.note }}</small>
                        </a>
                      </template>
                    </SplitButton>
                    <Button
                      v-else-if="stage.capability === leadStage"
                      label="Run"
                      size="small"
                      :loading="starting === stage.capability"
                      @click="start(stage)"
                    />
                    <!-- The row is the hit target; this is what says so. Hidden
                         under Full, where every row is open and nothing shuts. -->
                    <button
                      v-else-if="density === 'concise' && foldable(stage)"
                      type="button"
                      class="chev"
                      :aria-expanded="isOpen(stage)"
                      :aria-label="`${isOpen(stage) ? 'Collapse' : 'Expand'} ${stage.filed?.label || stage.capability}`"
                      @click="toggleRow(stage)"
                    >
                      <i class="aw-icon aw-icon-chevron-right" aria-hidden="true" />
                    </button>
                  </span>
                </span>

                <span v-if="hasBody(stage)" class="body">
                  <span v-if="isOpen(stage) && stage.history?.headline" class="sen">{{ stage.history.headline }}</span>
                  <span v-if="isOpen(stage) && bodySaying(stage)" class="dsc">{{ bodySaying(stage) }}</span>

                  <!-- What a stage that has already filed still owes. The count
                       beside it is not contradicted: thirty findings are filed
                       and two observations are undrafted, and both are true. -->
                  <span v-if="isOpen(stage) && remaining(stage)" class="left">
                    <i class="aw-icon aw-icon-hourglass" aria-hidden="true" />{{ remaining(stage) }}
                  </span>

                  <!-- Being produced right now, whether or not it was filed before. -->
                  <span v-if="liveState(stage.capability)" class="again" :data-live="liveState(stage.capability)">
                    <i :class="liveState(stage.capability) === 'running' ? 'aw-icon aw-icon-spin aw-icon-loader-circle' : 'aw-icon aw-icon-clock'" aria-hidden="true" />
                    <template v-if="liveState(stage.capability) === 'running'">
                      <!-- A stage that already filed is being produced *again*,
                           which is a different thing from one being produced. -->
                      {{ stage.held ? 'Running again' : 'The assistant is working on it now.'
                      }}{{ liveSince(stage.capability) ? ` · ${liveSince(stage.capability)}` : '' }}<template
                        v-if="soleRunning === stage.capability && activityLine"> · {{ activityLine }}</template>
                    </template>
                    <template v-else>
                      {{ stage.held ? 'Queued to run again' : 'Scheduled by the run in progress.' }}
                    </template>
                  </span>

                  <!-- A stage whose result is a distribution states it as one. A
                       matrix is read as "one critical, eight high" before any
                       single row is, and a paragraph cannot say that at a glance. -->
                  <ul v-if="isOpen(stage) && stage.stats.length" class="tally">
                    <li
                      v-for="stat in stage.stats"
                      :key="stat.label"
                      :data-severity="stat.severity"
                      :data-zero="stat.value ? null : '1'"
                    >
                      <b>{{ stat.value }}</b><span>{{ stat.label }}</span>
                    </li>
                  </ul>

                  <ul v-if="isOpen(stage) && stage.highlights.length" class="hl">
                    <li v-for="item in stage.highlights" :key="`${item.label}:${item.detail}`" :data-severity="item.severity">
                      <b>{{ item.label }}</b><span>{{ item.detail }}</span>
                    </li>
                  </ul>

                  <!-- What this stage left open behind it. Every debt has a row
                       to sit on now, because the stage that owes it is always
                       drawn. -->
                  <button
                    v-for="point in stage.open_points"
                    :key="point.key"
                    type="button"
                    class="open"
                    @click="openPoint(point)"
                  >
                    <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />
                    <span class="ot">{{ point.message }}</span>
                    <span class="oa">{{ point.action }}<i class="aw-icon aw-icon-arrow-right" aria-hidden="true" /></span>
                  </button>

                  <!-- When the work settled and what it took. A footnote to the
                       row, so it is behind the fold rather than on its face. -->
                  <span v-if="isOpen(stage) && stamp(stage)" class="stamp">
                    <i class="aw-icon aw-icon-clock" aria-hidden="true" />{{ stamp(stage) }}
                  </span>

                  <button
                    v-if="isOpen(stage) && attemptNote(stage)"
                    type="button"
                    class="tries"
                    :aria-expanded="expanded.has(stage.id)"
                    @click="toggle(stage)"
                  >
                    <i :class="expanded.has(stage.id) ? 'aw-icon aw-icon-chevron-down' : 'aw-icon aw-icon-chevron-right'" aria-hidden="true" />
                    {{ attemptNote(stage) }}
                  </button>
                  <ol v-if="isOpen(stage) && expanded.has(stage.id) && stage.history" class="attempts">
                    <li v-for="attempt in stage.history.attempts" :key="attempt.run_id">
                      <span class="at">{{ when(attempt.at) }}</span>
                      <span class="st" :data-status="attempt.run_status">{{ attempt.run_status.replaceAll('_', ' ') }}</span>
                      <span class="el">{{ duration(attempt.elapsed_ms) }}</span>
                    </li>
                  </ol>
                </span>
              </li>
            </template>
          </ol>
        </section>
      </section>

      <!-- What the whole engagement cost, as a footnote to the ledger it is a
           footnote to. It answered no question anyone arrives with, and it was
           the first thing on the page. -->
      <!-- A review debt sits under the record, because it is a note about what
           is on the screen and a note above the thing it annotates is a banner.
           It stands whether or not a run is in flight: only a person can read
           what the assistant decided, and a run does not do it for them. The
           `stage` next step has no band any more — the phase being worked says
           it, on its own header. -->
      <section v-if="next && next.kind === 'open_point'" class="brief" data-kind="open_point">
        <span class="mark"><i class="aw-icon aw-icon-circle-alert" /></span>
        <div class="txt"><strong>{{ next.message }}</strong></div>
        <Button
          :label="next.action"
          size="small"
          severity="secondary"
          outlined
          @click="openPoint(next)"
        />
      </section>

      <footer class="summary">
        <span>{{ totalLine }}</span>
        <span class="grow"></span>
        <span v-if="quietRuns" class="quiet">{{ plural(quietRuns, 'run') }} filed nothing</span>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.record { display: flex; flex-direction: column; gap: .75rem; min-height: 0; }
.loading { display: grid; place-content: center; gap: .4rem; padding: 3rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- the toolbar --------------------------------------------------------- */
.bar { display: flex; align-items: center; gap: .875rem; }
.bar h2 {
  margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600;
}
.grow { flex: 1; }

/* --- how much of each row is drawn --------------------------------------- */
.dens {
  display: inline-flex; gap: 2px; padding: 2px;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-raised);
}
.dens button {
  padding: .3rem .7rem; border: 0; border-radius: 6px; background: transparent;
  color: var(--aw-muted); font: inherit; font-size: var(--aw-text-sm); font-weight: 600; cursor: pointer;
}
.dens button[aria-pressed="true"] { background: var(--aw-panel); color: var(--aw-teal-strong); box-shadow: var(--aw-shadow-sm); }
.dens button:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }

/* The word goes. Beside a toggle that says what the page is showing and a link
   that says where it goes, a third label on the one control that changes
   nothing about either is the noisiest thing in the bar. */
.bar :deep(.refresh) { width: 30px; height: 30px; padding: 0; }

/* --- the chain, which is a lens rather than a work product --------------- */
/* Sized and weighted like the Refresh button beside it so the bar reads as one
   row of controls, but drawn as a link because it navigates. */
.chain {
  display: inline-flex; align-items: center; gap: .4rem;
  padding: .3rem .7rem;
  border: 1px solid var(--aw-teal); border-radius: var(--aw-radius-control);
  color: var(--aw-teal); background: transparent;
  font-size: var(--aw-text-sm); font-weight: 600; text-decoration: none;
  white-space: nowrap;
}
.chain:hover { background: var(--aw-teal-soft); }
.chain:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.chain .aw-icon { font-size: var(--aw-text-sm); }

/* --- the whole plan, as one bar per phase -------------------------------- */
/* One column per phase, ruled off from the next: the phase, how far through it
   is in the words its header below uses, and the same fraction as a bar. */
.progress {
  display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
  padding: .875rem .25rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.pcol { display: grid; gap: .5rem; min-width: 0; padding: 0 .875rem; }
.pcol + .pcol { border-left: 1px solid var(--aw-raised); }
.phd { display: flex; align-items: baseline; justify-content: space-between; gap: .5rem; min-width: 0; }
.ptl {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 600;
}
.pcol[data-state='later'] .ptl { color: var(--aw-ink-soft); }
.pss {
  flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm); white-space: nowrap;
}
.pcol[data-state='done'] .pss { color: var(--aw-ok); font-weight: 600; }
.pcol[data-state='current'] .pss {
  color: var(--aw-teal-strong); font-family: var(--aw-font-mono); font-variant-numeric: tabular-nums;
}
.pcol[data-state='live'] .pss { color: var(--aw-info); font-weight: 600; }
.ptrack { display: block; height: 6px; border-radius: 3px; background: var(--aw-border); overflow: hidden; }
.ptrack i { display: block; height: 100%; border-radius: inherit; background: var(--aw-teal); }
.pcol[data-state='done'] .ptrack i { background: var(--aw-ok); }
.pcol[data-state='live'] .ptrack i { background: var(--aw-info); }
/* --- the two things that are news ---------------------------------------- */
/* A run under way, and a review debt. Both sit above the phases, both are
   drawn at the size of the rows they refer to rather than as a banner: what
   the reader came for is the record, and these are one line each about it. */
.brief {
  display: flex;
  align-items: center;
  gap: .75rem;
  padding: .55rem .875rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft);
}
.brief .mark { flex: 0 0 auto; display: grid; place-items: center; color: var(--aw-warn-ink); font-size: var(--aw-text-sm); }
.brief .txt { flex: 1; min-width: 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: .1rem .4rem; }
/* The bar takes the width the words do not need, which is what makes it read
   as the run's own length rather than as another chip on the line. */
.brief.stepped .txt { flex: 0 1 auto; }
.brief strong { color: var(--aw-warn-ink); font-size: var(--aw-text-sm); font-weight: 600; line-height: 1.4; }
.brief span { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }

/* --- the run history, as a footnote -------------------------------------- */
/* The strip at the top draws what the engagement holds. What is left down here
   is how it got there, which is a footnote and reads as one. */
.summary {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: .2rem 1.2rem;
  padding: 0 .25rem;
  color: var(--aw-muted); font-size: var(--aw-text-xs);
  font-variant-numeric: tabular-nums;
}
.quiet { color: var(--aw-muted-strong); }

/* --- the spine ------------------------------------------------------------ */
/* One card for the whole file. A phase is a band inside it, so the rows of
   every phase share one set of columns and the file reads top to bottom as one
   list rather than as five boxes. */
.spine {
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel); overflow: hidden;
}

.phead {
  display: flex; align-items: center; gap: .625rem; width: 100%;
  min-height: 2.5rem; padding: .375rem 1rem;
  border: 0; border-top: 1px solid var(--aw-border); background: var(--aw-canvas);
  color: var(--aw-ink-strong);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; text-align: left; cursor: pointer;
}
.phase:first-child .phead { border-top: 0; }
.phead:hover { background: var(--aw-raised); }
.phead:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }

.pico { flex: 0 0 auto; font-size: var(--aw-text-base); }
.phase[data-state='done'] .pico { color: var(--aw-ok); }
.phase[data-state='current'] .pico { color: var(--aw-teal); }
.phase[data-state='later'] .pico { color: var(--aw-muted); }

.pt { flex: 0 0 auto; }
.phase[data-state='later'] .pt { color: var(--aw-ink-soft); }

/* What a shut phase is standing in for. It is the one thing folding away a
   phase could hide, so it is said on the header rather than behind it. */
.pnames {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-muted); font-weight: 400;
}
.pst {
  flex: 0 0 auto; color: var(--aw-muted); font-weight: 400;
  font-family: var(--aw-font-mono); font-variant-numeric: tabular-nums;
}
/* `Not started` is a word, and words are not set in the ledger face. */
.phase[data-state='later'] .pst { font-family: inherit; }
.pchev { flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm); transition: transform .16s ease; }
.phead[aria-expanded='true'] .pchev { transform: rotate(180deg); }
@media (prefers-reduced-motion: reduce) { .pchev { transition: none; } }

/* --- the ledger ---------------------------------------------------------- */
.ledger { margin: 0; padding: 0; list-style: none; }

/* One line per work product, in five fixed columns so a reader scans down
   them rather than across each row: its state, what it is, its name, what it
   amounts to, and the one thing to do about it. */
.row {
  display: grid;
  grid-template-columns: 1rem 1rem minmax(8rem, 13.5rem) minmax(0, 1fr) auto;
  gap: 0 .75rem;
  align-items: center;
  min-height: 2.75rem;
  padding: .375rem 1rem;
  border-top: 1px solid var(--aw-raised);
}
.row.shut { cursor: pointer; }
.row.shut:hover { background: color-mix(in srgb, var(--aw-raised) 45%, transparent); }
/* The lead stage is the one being asked for, so it is the one row tinted. */
.row.lead { background: color-mix(in srgb, var(--aw-teal-soft) 60%, transparent); }
.row.lead.shut:hover { background: var(--aw-teal-soft); }

/* The state of the stage, said once. The five settled states are
   `UiStateIcon`; a run in flight keeps a dot, because it is the one state that
   moves. */
.state { justify-self: center; font-size: var(--aw-text-base); }
.dot {
  box-sizing: border-box; width: 10px; height: 10px; margin: 0 auto;
  border-radius: 50%; background: var(--aw-teal);
}
.row[data-live] .dot {
  width: 10px; height: 10px;
  border: 2px solid var(--aw-info); background: var(--aw-panel);
}
/* The running row is the only one that moves. A queued row is scheduled, not
   under way, and a tail of pulsing dots says nothing about which is which. */
.row[data-live='running'] .dot {
  border: 0; background: var(--aw-info); animation: aw-record-pulse 1.8s ease-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .row[data-live='running'] .dot { animation: none; }
}

/* --- what the row is ------------------------------------------------------ */
/* The artifact's own icon, after the state: the one says what the work
   product is, the other where it stands. */
.wpi { justify-self: center; font-size: var(--aw-text-base); color: var(--aw-teal); }
.row.ghost .wpi { color: var(--aw-muted); }
.row.ghost.lead .wpi,
.row[data-live] .wpi { color: var(--aw-teal); }

.name { display: flex; align-items: center; min-width: 0; }
/* The work product is the link. */
.wp {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600;
  text-decoration: none;
}
a.wp:hover { color: var(--aw-teal-strong); text-decoration: underline; }
a.wp:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 2px; }
.row.ghost .wp { color: var(--aw-ink-soft); }
.row.ghost.lead .wp,
.row[data-live] .wp { color: var(--aw-teal-strong); }
.none { color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- what the row amounts to --------------------------------------------- */
/* One line of facts, separated by a middle dot, with the figures in the ledger
   face. It is the first thing on the row to give way: the name and the action
   are the reading that must survive. */
.meta {
  display: flex; align-items: baseline; gap: .375rem; min-width: 0;
  overflow: hidden; white-space: nowrap;
  color: var(--aw-ink-soft); font-size: var(--aw-text-sm);
}
.meta > * { flex: 0 0 auto; }
.meta > .text, .meta > .owed { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.meta b { font-family: var(--aw-font-mono); font-weight: 500; font-variant-numeric: tabular-nums; color: var(--aw-ink); }
.sep { color: var(--aw-muted); }
.row.ghost .meta { color: var(--aw-muted); }
.row.ghost .meta b { color: var(--aw-ink-soft); }
.row.lead .meta { color: var(--aw-ink-soft); }
/* What a stage that has not run waits for. */
.dep { color: var(--aw-muted); }
/* What a held stage is short of: its own warning, on its own line. */
.owed { color: var(--aw-warn-ink); }

/* The parts a row opens, set as the phrase they are. An artifact door reads as
   a link; a tool door keeps its wrench and stays neutral, because running a
   query files nothing and drawing it like held work would claim otherwise. */
.door { color: inherit; text-decoration: none; }
a.door { text-decoration: underline; text-decoration-color: var(--aw-border-strong); text-underline-offset: 3px; }
a.door:hover { color: var(--aw-teal-strong); text-decoration-color: currentColor; }
a.door:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; border-radius: 2px; }
.door[data-kind='tool'] { color: var(--aw-muted); text-decoration: none; }
.door[data-kind='tool'] .aw-icon { margin-right: .25rem; font-size: var(--aw-text-xs); vertical-align: -.1em; }
a.door[data-kind='tool']:hover { color: var(--aw-teal-strong); }

.end { display: flex; align-items: center; justify-content: flex-end; gap: .5rem; }
.act { display: flex; align-items: center; justify-content: flex-end; }

/* Importing more is always possible and never the next step, so it is a
   link-weight control rather than a second button on the screen. */
.rowlink {
  padding: .25rem .125rem; border: 0; background: transparent;
  color: var(--aw-teal-strong); font: inherit; font-size: var(--aw-text-sm); font-weight: 500;
  cursor: pointer; white-space: nowrap;
}
.rowlink:hover { text-decoration: underline; }
.rowlink:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; border-radius: 2px; }

/* Everything the row says under its own line: the folded body, and the two
   things that are said whether it is folded or not. */
.body { grid-column: 3 / -1; display: grid; gap: .25rem; min-width: 0; justify-items: start; padding: .125rem 0 .375rem; }
.sen { color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 600; }
.stamp {
  display: inline-flex; align-items: center; gap: .3rem;
  color: var(--aw-muted); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums;
}

/* What a filed stage still owes. Amber like the debts it sits among, but a
   line rather than a button: there is nothing here to click, only something
   the next run will pick up. */
.left {
  display: flex; gap: .4rem; align-items: baseline;
  color: var(--aw-warn-ink); font-size: var(--aw-text-sm);
}
.left i { font-size: .7rem; }

.dsc { max-width: var(--aw-measure); color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.55; }

/* What the folded body is standing in for, counted. A row with nothing to say
   carries no chip, which is what makes the rows that do carry one findable. */
.sig {
  display: inline-flex; align-items: center; gap: .25rem; height: 1.25rem; padding: 0 .5rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-raised); color: var(--aw-muted-strong);
  font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap;
}
.sig b { font-variant-numeric: tabular-nums; }
.sig[data-severity="warning"] { background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.sig[data-severity="error"] { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }

/* The affordance for a hit target that is the whole row. */
.chev {
  display: grid; place-items: center; width: 1.1rem; height: 1.1rem; padding: 0;
  border: 0; border-radius: 4px; background: transparent; color: var(--aw-muted); cursor: pointer;
}
.chev i { font-size: var(--aw-text-xs); transition: transform .16s ease; }
.row:not(.shut) .chev i { transform: rotate(90deg); }
.row:hover .chev { color: var(--aw-teal); background: var(--aw-raised); }
.chev:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) { .chev i { transition: none; } }

/* The tally reads left to right as a distribution, so it is not the stacked
   bordered list every other block on this row uses. */
.tally { display: flex; flex-wrap: wrap; gap: .3rem; margin: .35rem 0 .1rem; padding: 0; list-style: none; }
.tally li {
  display: inline-flex; align-items: baseline; gap: .3rem;
  padding: .15rem .45rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill);
  background: var(--aw-raised); color: var(--aw-muted-strong);
}
.tally b { font-size: var(--aw-text-base); font-weight: 700; font-variant-numeric: tabular-nums; color: var(--aw-ink-strong); }
.tally span { font-size: var(--aw-text-xs); }
/* Zero of something severe is worth saying and not worth colouring. */
.tally li[data-severity="warning"]:not([data-zero]) { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.tally li[data-severity="warning"]:not([data-zero]) b { color: var(--aw-warn-ink); }
.tally li[data-severity="error"]:not([data-zero]) { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.tally li[data-severity="error"]:not([data-zero]) b { color: var(--aw-danger-ink); }

.hl { display: grid; gap: .25rem; margin: .3rem 0 0; padding: 0; list-style: none; }
.hl li { display: grid; gap: .05rem; padding-left: .6rem; border-left: 2px solid var(--aw-warn-line); }
.hl li[data-severity="error"] { border-left-color: var(--aw-danger-line); }
.hl b { color: var(--aw-warn-ink); font-size: var(--aw-text-sm); font-weight: 600; line-height: 1.4; }
.hl li[data-severity="error"] b { color: var(--aw-danger-ink); }
.hl span { max-width: 70ch; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.45; }

/* an open point hanging off the row that created it */
.open {
  display: flex; align-items: center; gap: .45rem; width: 100%; max-width: 46rem;
  margin-top: .3rem; padding: .35rem .5rem;
  border: 0; border-left: 2px solid var(--aw-warn-line); border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0;
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer;
}
.open:hover { background: var(--aw-panel); border-left-color: var(--aw-warn); }
.open:focus-visible { outline: 2px solid var(--aw-warn); outline-offset: 1px; }
.open > i { font-size: var(--aw-text-xs); }
.open .ot { flex: 1; min-width: 0; line-height: 1.4; }
.open .oa { display: inline-flex; align-items: center; gap: .25rem; flex: 0 0 auto; font-weight: 600; }
.open .oa i { font-size: var(--aw-text-xs); }

.tries {
  display: inline-flex; align-items: center; gap: .3rem; margin-top: .2rem; padding: .1rem 0;
  border: 0; background: transparent; color: var(--aw-muted);
  font: inherit; font-size: var(--aw-text-xs); cursor: pointer;
}
.tries:hover { color: var(--aw-teal); }
.tries:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 2px; }
.tries i { font-size: var(--aw-text-xs); }

.attempts { display: grid; gap: .2rem; width: 100%; margin: .3rem 0 0; padding: .4rem .55rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); list-style: none; }
.attempts li { display: flex; align-items: baseline; gap: .6rem; font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums; }
.attempts .at { min-width: 8rem; color: var(--aw-ink-soft); }
.attempts .st { flex: 1; color: var(--aw-muted); }
.attempts .st[data-status="cancelled"], .attempts .st[data-status="failed"] { color: var(--aw-warn-ink); }
.attempts .el { color: var(--aw-muted); }

/* The note is the half that keeps "defer" from reading as "skip", so it is set
   as a second line rather than a tooltip. */
.alt { display: grid; gap: .1rem; padding: .4rem .75rem; text-align: left; white-space: normal; }
.alt small { color: var(--aw-muted); font-size: var(--aw-text-xs); max-width: 18rem; }

/* --- the run in flight ---------------------------------------------------- */
/* Blue, deliberately: teal is what the engagement has filed and amber is what
   it owes. Work happening right now is neither, and reusing either colour made
   a live row read as already settled. */
.brief.live { border-color: var(--aw-info-line); background: var(--aw-info-soft); }
.brief.live .mark { color: var(--aw-info); }
.brief.live strong { color: var(--aw-info); }
.pbar {
  flex: 1; min-width: 3rem; height: 4px; border-radius: 2px;
  background: var(--aw-info-line); overflow: hidden;
}
.pbar i { display: block; height: 100%; background: var(--aw-info); }
.brief.live[data-wait] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); }
.brief.live[data-wait] .mark,
.brief.live[data-wait] strong { color: var(--aw-warn-ink); }

/* A work product that is already filed and is being produced again. */
.again {
  display: inline-flex; align-items: center; gap: .35rem;
  margin-top: .3rem; padding: .15rem .45rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-info-soft); color: var(--aw-info);
  font-size: var(--aw-text-xs); font-weight: 600;
}
.again i { font-size: var(--aw-text-xs); }

/* The ring is mixed from the token rather than written out, because the blue
   inverts between themes and a fixed rgba() would glow dark-on-dark. */
@keyframes aw-record-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--aw-info) 45%, transparent); }
  70% { box-shadow: 0 0 0 .4rem transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
/* Narrow, the progress bar takes two rows of two, and what the row amounts to
   drops under its name. The one thing that stays on the first line is the
   thing to do about it: an action that scrolls away from the row it belongs
   to is one nobody takes. */
@container (max-width: 44rem) {
  .progress { grid-auto-flow: row; grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: .875rem; }
  .pcol:nth-child(odd) { border-left: 0; }

  .row { grid-template-columns: 1rem 1rem minmax(0, 1fr) auto; }
  .row .dot, .row .state { grid-column: 1; grid-row: 1; }
  .row .wpi { grid-column: 2; grid-row: 1; }
  .row .name { grid-column: 3; grid-row: 1; }
  .row .end { grid-column: 4; grid-row: 1; }
  .row .meta { grid-column: 3 / -1; grid-row: 2; margin-top: .125rem; }
  .row .body { grid-column: 3 / -1; grid-row: 3; }
  .pnames { display: none; }
}
</style>
