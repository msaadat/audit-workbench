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
import RecordBoardView from './record/RecordBoardView.vue'
import RecordPhasesView from './record/RecordPhasesView.vue'
import RecordOverviewView from './record/RecordOverviewView.vue'
import type { RecordLayout, RecordPhaseGroup, RecordRunNow } from './record/recordViewModel'

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

const KNOWN_DESTINATIONS: readonly string[] = [
  'apm', 'cycle', 'rcm', 'chain', 'doc-tests', 'data-tests',
  'findings', 'report', 'documents', 'data', 'query', 'analysis',
]

// A stored stage status that is out of date comes back marked `refreshing`
// while the server draws the next one. The page shows the last states and asks
// again shortly, rather than holding the whole record on the redraw. Bounded,
// so a refresh that keeps failing does not poll for ever.
const REFRESH_POLL_MS = 1500
const REFRESH_POLL_LIMIT = 20
let pollTimer = 0
let polls = 0

async function load(options: { quiet?: boolean } = {}) {
  window.clearTimeout(pollTimer)
  if (!options.quiet) {
    loading.value = true
    polls = 0
  }
  try {
    data.value = await api.get<EngagementRecordPayload>(
      `/api/workspaces/${props.workspace.id}/engagement/record`,
    )
    if (data.value.refreshing && polls < REFRESH_POLL_LIMIT) {
      polls += 1
      pollTimer = window.setTimeout(() => { void load({ quiet: true }) }, REFRESH_POLL_MS)
    }
  } catch (error) {
    if (!options.quiet) {
      toast.add({
        severity: 'error',
        summary: 'Could not load the engagement record',
        detail: error instanceof ApiError ? error.message : String(error),
        life: 6000,
      })
    }
  } finally {
    if (!options.quiet) loading.value = false
  }
}
void load()
onUnmounted(() => window.clearTimeout(pollTimer))

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

/**
 * The run in flight with one progress figure, for layouts that say it once.
 * The run's own unit count (4 of 32) is the figure a reader can check against
 * the work; the stage count is the fallback, and on a one-stage run there is
 * no figure at all rather than a bar that can only read empty or full.
 */
const runNow = computed<RecordRunNow | null>(() => {
  const value = live.value
  if (!value) return null
  const activity = agent.state.run?.activity
  const units = activity?.total ? Math.min(activity.current ?? 0, activity.total) : null
  const parts: string[] = []
  if (units !== null && activity?.total) parts.push(`${units} of ${activity.total}`)
  else if (value.step) parts.push(value.step)
  if (liveElapsed.value) parts.push(liveElapsed.value)
  return {
    title: value.headline,
    status: value.state,
    detail: parts.join(' · '),
    fraction: units !== null && activity?.total ? units / activity.total : value.progress,
    waiting: value.waiting,
  }
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
    const issues = ['completed_with_issues', 'needs_review'].includes(stageStatus(stage))
    return issues || stage.readiness.state !== 'satisfied' ? 'attention' : 'done'
  }
  if (stage.capability === leadStage.value) return 'next'
  return stage.blocked_reason.trim() ? 'waiting' : 'not_started'
}

/** The reason a held row needs attention, for the meta cell. */
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
 * What the stage says about itself right now, used as the one-line fact on a
 * row that has nothing to count (such as the memorandum or report).
 */
function saying(stage: EngagementStage): string {
  return stage.summary || stage.readiness.reasons[0] || ''
}

function stageStatus(stage: EngagementStage): string {
  return stage.status || ''
}

/**
 * Whether anything is drawn under the row's own line: only live run progress
 * and open review points left behind by the stage.
 */
function hasBody(stage: EngagementStage): boolean {
  return Boolean(liveState(stage.capability) || stage.open_points.length)
}

/**
 * A distribution, as the clause after the row's count: `32 rows: 0 critical,
 * 18 high`. Every tier the server sent is said — zero critical is a finding
 * about the matrix — and a bucket whose value is not a number cannot be.
 */
function breakdown(stage: EngagementStage): FactPart[] {
  return stage.stats
    .map(item => ({ item, count: typeof item.value === 'number' ? item.value : Number(item.value) }))
    .filter(({ count }) => Number.isFinite(count))
    .map(({ item, count }) => ({
      figure: String(count),
      text: item.label,
      // Colour only a severe tier that holds something: nothing is wrong at zero.
      severity: count > 0 && (item.severity === 'error' || item.severity === 'warning') ? item.severity : '',
    }))
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
  /** Open human review gates attached to stages in this phase. */
  openPoints: number
}

/**
 * The ledger, cut into the phases an auditor recognises.
 */
const groups = computed<PhaseGroup[]>(() => {
  const phases = data.value?.phases ?? []
  const byPhase = new Map<string, EngagementStage[]>()
  for (const stage of stages.value) {
    const rows = byPhase.get(stage.phase)
    if (rows) rows.push(stage)
    else byPhase.set(stage.phase, [stage])
  }

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
      openPoints: rows.reduce((sum, stage) => sum + stage.open_points.length, 0),
    }
  })
})

const currentPhase = computed(() => groups.value.find(group => group.state === 'current')?.id ?? '')

/**
 * Open only the phases that need attention right now: the active phase, any
 * phase with a live run, and any phase carrying an open human review gate.
 * Completed phases with no open gates start folded to a single summary line,
 * keeping the active work above the fold. When every phase is finished with no
 * open gates, all phases open so the completed record reads immediately.
 */
const openPhases = ref<Set<string>>(new Set())
const phaseSeed = computed(
  () => `${currentPhase.value}|${groups.value.map(group => `${group.id}:${group.openPoints}`).join(',')}`,
)
watch(phaseSeed, () => {
  const active = groups.value.filter(
    group => group.state === 'current' || group.live || group.openPoints > 0,
  )
  openPhases.value = new Set(
    (active.length ? active : groups.value).map(group => group.id),
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
/** One headline figure per phase, folded directly into the top phase bar. */
function phaseHeadline(phaseId: string): string {
  const counts = data.value?.counts ?? {}
  if (phaseId === 'sources') {
    const parts: string[] = []
    if (counts.documents) parts.push(plural(counts.documents, 'doc'))
    if (counts.tables) parts.push(plural(counts.tables, 'table'))
    if (counts.analyses) parts.push(plural(counts.analyses, 'analysis', 'analyses'))
    return parts.length ? parts.join(' · ') : 'No sources yet'
  }
  if (phaseId === 'planning') {
    const parts: string[] = []
    if (counts.rcm) parts.push(plural(counts.rcm, 'control'))
    if (counts.tests) parts.push(plural(counts.tests, 'test'))
    return parts.length ? parts.join(' · ') : 'Not planned yet'
  }
  if (phaseId === 'fieldwork') {
    const tests = counts.tests ?? 0
    const results = (counts.data_test_results ?? 0) + (counts.document_test_results ?? 0)
    if (tests) return `${results} of ${plural(tests, 'test')} run`
    return 'Not started'
  }
  if (phaseId === 'writeup') {
    const findingsCount = counts.findings ?? 0
    if (findingsCount) return plural(findingsCount, 'finding')
    const reportHeld = stages.value.some(stage => stage.capability === 'report.working_draft' && stage.held)
    return reportHeld ? 'Report drafted' : 'No findings yet'
  }
  return ''
}

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
    headline: phaseHeadline(group.id),
    openPoints: group.openPoints,
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

interface FactPart { figure: string; text: string; severity: string }

interface Fact {
  kind: FactKind
  /** The figure, drawn in the ledger face. Empty on a fact with none. */
  figure: string
  text: string
  title?: string
  to?: WorkspaceDestination | null
  /** A distribution the count is cut into, said after it. */
  parts?: FactPart[]
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
  const parts = breakdown(stage)
  if (count(stage)) {
    out.push({
      kind: 'count',
      figure: count(stage),
      text: parts.length ? `${unitWord(stage)}:` : unitWord(stage),
      title: size(stage) || undefined,
      parts,
    })
  } else if (parts.length) {
    out.push({ kind: 'count', figure: '', text: '', parts })
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
    const text = stage.held ? firstSentence(saying(stage)) || stage.headline : stage.headline
    if (text) out.push({ kind: 'text', figure: '', text })
  }
  return [...out, ...tools]
}

/** What a shut phase is standing in for, which is the work products it covers. */
function phaseNames(group: PhaseGroup): string {
  return group.stages.map(stage => stage.filed?.label || stage.capability).join(' · ')
}

function jumpToPhase(phaseId: string) {
  if (!openPhases.value.has(phaseId)) {
    const next = new Set(openPhases.value)
    next.add(phaseId)
    openPhases.value = next
  }
  const el = document.querySelector(`[data-phase-section="${phaseId}"]`)
  el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

/* --- switchable record layouts -------------------------------------------- */

const LAYOUT_KEY = 'aw.record.layout'
const LAYOUTS: ReadonlyArray<{ id: RecordLayout; label: string }> = [
  { id: 'list', label: 'List' },
  { id: 'board', label: 'Board' },
  { id: 'phases', label: 'Phases' },
  { id: 'overview', label: 'Overview' },
]

function storedLayout(): RecordLayout {
  try {
    const saved = window.localStorage?.getItem(LAYOUT_KEY)
    if (saved === 'board' || saved === 'phases' || saved === 'overview' || saved === 'list') {
      return saved
    }
  } catch { /* private mode */ }
  return 'list'
}

const layout = ref<RecordLayout>(storedLayout())
function setLayout(value: RecordLayout) {
  layout.value = value
  try {
    window.localStorage?.setItem(LAYOUT_KEY, value)
  } catch { /* private mode */ }
}

const openPoints = computed(() => data.value?.open_points ?? [])

function primaryStatFor(stage: EngagementStage): string {
  if (!stage.held) return dependency(stage) || 'Not started'
  if (size(stage)) return size(stage)
  const doorSummary = stage.links
    .filter(link => link.kind !== 'tool' && link.count != null)
    .map(link => {
      const noun = doorNoun(link.label, link.count)
      return link.total ? `${link.count}/${link.total} ${noun}` : `${link.count} ${noun}`
    })
  if (doorSummary.length) return doorSummary.join(' · ')
  return firstSentence(saying(stage)) || 'Complete'
}

const viewGroups = computed<RecordPhaseGroup[]>(() =>
  groups.value.map((group, index) => {
    const done = group.total > 0 && group.held === group.total
    return {
      id: group.id,
      index,
      title: group.title,
      state: group.state,
      statusLabel: group.live ? 'Running' : done ? 'Done' : phaseTally(group),
      held: group.held,
      total: group.total,
      fill: group.total ? group.held / group.total : 0,
      live: group.live,
      openPoints: group.openPoints,
      headline: phaseHeadline(group.id),
      items: group.stages.map((stage, step) => ({
        stage,
        stepLabel: `${index + 1}.${step + 1}`,
        state: rowState(stage),
        live: liveState(stage.capability),
        liveElapsed: liveSince(stage.capability),
        liveActivity: soleRunning.value === stage.capability ? activityLine.value : '',
        isLead: stage.capability === leadStage.value,
        destination: destinationOf(stage),
        facts: facts(stage),
        primaryStat: primaryStatFor(stage),
        attentionReason: attentionReason(stage),
      })),
    }
  }),
)
</script>

<template>
  <div class="record">
    <div class="bar">
      <div class="bar-title">
        <h2>Engagement record</h2>
        <span v-if="layout !== 'phases'" class="subtitle">Local Polars compute · Human-gated sign-off</span>
      </div>
      <span v-if="data?.refreshing" class="updating" role="status">
        <i class="aw-icon aw-icon-spin aw-icon-loader-circle" aria-hidden="true" />Updating status…
      </span>
      <span class="grow"></span>
      <div class="layout-switch" role="group" aria-label="Record layout">
        <button
          v-for="option in LAYOUTS"
          :key="option.id"
          type="button"
          :aria-pressed="layout === option.id"
          @click="setLayout(option.id)"
        >{{ option.label }}</button>
      </div>
      <Button
        class="refresh"
        icon="aw-icon aw-icon-refresh-cw"
        size="small"
        severity="secondary"
        outlined
        aria-label="Refresh the record"
        :loading="loading"
        @click="load()"
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
      <!-- Unified 4-phase deck (only for the List layout; Board, Phases and
           Overview each show phase structure natively). -->
      <section v-if="layout === 'list'" class="progress" aria-label="Progress through the engagement">
        <div
          v-for="(phase, idx) in progress"
          :key="phase.id"
          class="pcol"
          :data-state="phase.state"
          :data-review="phase.openPoints ? '1' : null"
          role="button"
          tabindex="0"
          @click="jumpToPhase(phase.id)"
          @keydown.enter.space.prevent="jumpToPhase(phase.id)"
        >
          <div class="phd">
            <span class="pnum" aria-hidden="true">{{ idx + 1 }}</span>
            <span class="ptl">{{ phase.title }}</span>
            <span class="pss">{{ phase.status }}</span>
          </div>
          <div class="pstat">
            <span class="pmetric">{{ phase.headline }}</span>
            <span v-if="phase.openPoints" class="pgate">
              {{ plural(phase.openPoints, 'review') }}
            </span>
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

      <!-- While a run is in flight it, not the next step, is the news. -->
      <!-- The Phases layout says the run once, in its own Now row. -->
      <section
        v-if="live && layout !== 'phases'"
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

      <RecordBoardView
        v-if="layout === 'board'"
        :groups="viewGroups"
        :starting="starting"
        @start="start"
        @open-point="openPoint"
      />

      <RecordPhasesView
        v-else-if="layout === 'phases'"
        :groups="viewGroups"
        :starting="starting"
        :open-points="openPoints"
        :run="runNow"
        @start="start"
        @open-point="openPoint"
        @watch="watchRun"
      />

      <RecordOverviewView
        v-else-if="layout === 'overview'"
        :groups="viewGroups"
        :openPoints="openPoints"
        :starting="starting"
        @start="start"
        @open-point="openPoint"
      />

      <!-- The spine: every phase in plan order (List layout). -->
      <section v-else class="spine" aria-label="The engagement file">
        <section
          v-for="(group, index) in groups"
          :key="group.id"
          class="phase"
          :data-state="group.state"
          :data-phase-section="group.id"
        >
          <span class="rail" aria-hidden="true">
            <span class="disc">{{ index + 1 }}</span>
            <span v-if="index < groups.length - 1" class="track"></span>
          </span>

          <div class="pbody">
          <button
            type="button"
            class="phead"
            :aria-expanded="phaseOpen(group)"
            @click="togglePhase(group)"
          >
            <span class="pt aw-type-section">{{ group.title }}</span>
            <i
              v-if="group.state === 'done'"
              class="aw-icon aw-icon-circle-check pdone"
              role="img"
              :aria-label="PHASE_STATE_LABEL.done"
              :title="PHASE_STATE_LABEL.done"
            />
            <span v-else-if="group.state === 'current'" class="pnow">{{ PHASE_STATE_LABEL.current }}</span>
            <span v-if="!phaseOpen(group) && group.openPoints" class="pgate">
              {{ plural(group.openPoints, 'review') }}
            </span>
            <span v-if="!phaseOpen(group) && group.total" class="pnames">{{ phaseNames(group) }}</span>
            <span class="grow"></span>
            <i class="aw-icon aw-icon-chevron-down pchev" aria-hidden="true" />
          </button>

          <ol v-if="phaseOpen(group)" class="ledger">
            <template v-for="(stage, step) in group.stages" :key="stage.id">
              <li
                class="row"
                :class="{ ghost: !stage.held, lead: stage.capability === leadStage }"
                :data-status="stageStatus(stage) || null"
                :data-live="liveState(stage.capability) || null"
              >
                <span v-if="liveState(stage.capability)" class="dot" aria-hidden="true"></span>
                <UiStateIcon v-else class="state" :state="rowState(stage)" />

                <span class="name">
                  <span class="sn" aria-hidden="true">{{ index + 1 }}.{{ step + 1 }}</span>
                  <component
                    :is="destinationOf(stage) ? RouterLink : 'span'"
                    v-if="stage.filed"
                    :to="destinationOf(stage) ? nav.to(destinationOf(stage)!) : undefined"
                    class="wp"
                    :class="{ linked: !!destinationOf(stage) }"
                  >{{ stage.filed.label }}</component>
                  <span v-else class="none">&#8212;</span>
                </span>

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
                    >{{ fact.figure }}</b>{{ fact.figure ? ' ' : '' }}{{ fact.text }}<template
                      v-for="(part, at) in fact.parts ?? []"
                      :key="part.text"
                    >{{ at ? ', ' : fact.text ? ' ' : '' }}<b
                      class="n"
                      :data-severity="part.severity || null"
                    >{{ part.figure }}</b> {{ part.text }}</template></component>
                  </template>
                </span>

                <span class="act">
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
                </span>

                <span v-if="hasBody(stage)" class="body">
                  <span v-if="liveState(stage.capability)" class="again" :data-live="liveState(stage.capability)">
                    <i :class="liveState(stage.capability) === 'running' ? 'aw-icon aw-icon-spin aw-icon-loader-circle' : 'aw-icon aw-icon-clock'" aria-hidden="true" />
                    <template v-if="liveState(stage.capability) === 'running'">
                      {{ stage.held ? 'Running again' : 'The assistant is working on it now.'
                      }}{{ liveSince(stage.capability) ? ` · ${liveSince(stage.capability)}` : '' }}<template
                        v-if="soleRunning === stage.capability && activityLine"> · {{ activityLine }}</template>
                    </template>
                    <template v-else>
                      {{ stage.held ? 'Queued to run again' : 'Scheduled by the run in progress.' }}
                    </template>
                  </span>

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
                </span>
              </li>
            </template>
          </ol>
          </div>
        </section>
      </section>

      <!-- What the engagement cost is bookkeeping; the Phases layout leaves it to Debug. -->
      <footer v-if="layout !== 'phases'" class="summary">
        <span>{{ totalLine }}</span>
        <span class="grow"></span>
        <span v-if="quietRuns" class="quiet">{{ plural(quietRuns, 'run') }} filed nothing</span>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.record { display: flex; flex-direction: column; gap: .875rem; min-height: 0; container-type: inline-size; }
.updating { display: inline-flex; align-items: center; gap: .35rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.loading { display: grid; place-content: center; gap: .4rem; padding: 3rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- the toolbar --------------------------------------------------------- */
.bar { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem .875rem; }
.bar-title { display: flex; align-items: baseline; flex-wrap: wrap; gap: .25rem .75rem; min-width: 0; }
.bar h2 {
  margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: var(--aw-weight-title);
  letter-spacing: -.01em;
}
.subtitle { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.grow { flex: 1; }

.layout-switch {
  display: inline-flex; gap: 2px; padding: 2px;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-raised);
}
.layout-switch button {
  padding: .25rem .6rem; border: 0; border-radius: 6px; background: transparent;
  color: var(--aw-muted); font: inherit; font-size: var(--aw-text-xs); font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.layout-switch button:hover { color: var(--aw-ink); }
.layout-switch button[aria-pressed='true'] {
  background: var(--aw-panel); color: var(--aw-teal-strong); box-shadow: var(--aw-shadow-sm);
}
.layout-switch button:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }

.bar :deep(.refresh) { width: 30px; height: 30px; padding: 0; }

/* --- the chain, which is a lens rather than a work product --------------- */
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

/* --- unified 4-phase deck ------------------------------------------------ */
.progress {
  display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr);
  padding: .5rem .25rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.pcol {
  display: grid; gap: .375rem; min-width: 0; padding: .375rem .875rem;
  border-radius: var(--aw-radius-control); cursor: pointer;
  transition: background-color .14s ease;
}
.pcol:hover { background: color-mix(in srgb, var(--aw-raised) 65%, transparent); }
.pcol:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.pcol + .pcol { border-left: 1px solid var(--aw-raised); border-top-left-radius: 0; border-bottom-left-radius: 0; }
.phd { display: flex; align-items: center; gap: .4rem; min-width: 0; }
.pnum {
  flex: 0 0 auto; display: grid; place-items: center;
  width: 1.15rem; height: 1.15rem; border-radius: 50%;
  background: var(--aw-raised); color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); font-weight: 700;
}
.pcol[data-state='done'] .pnum { background: var(--aw-ok-soft); color: var(--aw-ok); }
.pcol[data-state='current'] .pnum { background: var(--aw-teal-soft); color: var(--aw-teal-strong); }
.pcol[data-state='live'] .pnum { background: var(--aw-info-soft); color: var(--aw-info); }
.ptl {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 600;
}
.pcol[data-state='later'] .ptl { color: var(--aw-ink-soft); }
.pss {
  flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-xs); white-space: nowrap;
}
.pcol[data-state='done'] .pss { color: var(--aw-ok); font-weight: 600; }
.pcol[data-state='current'] .pss {
  color: var(--aw-teal-strong); font-family: var(--aw-font-mono); font-variant-numeric: tabular-nums;
}
.pcol[data-state='live'] .pss { color: var(--aw-info); font-weight: 600; }

.pstat { display: flex; align-items: center; justify-content: space-between; gap: .4rem; min-width: 0; }
.pmetric {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-ink-soft); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums;
}
.pcol[data-state='later'] .pmetric { color: var(--aw-muted); }
.pgate {
  flex: 0 0 auto; display: inline-flex; align-items: center;
  padding: .05rem .45rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-size: var(--aw-text-2xs); font-weight: 600; white-space: nowrap;
}

.ptrack { display: block; height: 5px; border-radius: 3px; background: var(--aw-border); overflow: hidden; }
.ptrack i { display: block; height: 100%; border-radius: inherit; background: var(--aw-teal); }
.pcol[data-state='done'] .ptrack i { background: var(--aw-ok); }
.pcol[data-state='live'] .ptrack i { background: var(--aw-info); }

/* --- live run band ------------------------------------------------------- */
.brief {
  display: flex;
  align-items: center;
  gap: .75rem;
  padding: .6rem .875rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-surface);
  background: var(--aw-warn-soft);
}
.brief .mark { flex: 0 0 auto; display: grid; place-items: center; color: var(--aw-warn-ink); font-size: var(--aw-text-sm); }
.brief .txt { flex: 1; min-width: 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: .15rem .5rem; }
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
/* The plan as a run of numbered phases. A phase heading sits on the page, not
   in the card, with its number on a rail down the left; its rows sit in a card
   of their own beside the rail. The heading and the rows are different kinds
   of thing, so they no longer share a band, a size or a background. */
.spine { display: flex; flex-direction: column; }

.phase {
  display: grid;
  grid-template-columns: 2.25rem minmax(0, 1fr);
  column-gap: 1.125rem;
}

.rail { display: flex; flex-direction: column; align-items: center; }
.disc {
  box-sizing: border-box; display: grid; place-items: center; flex: 0 0 auto;
  width: 2.25rem; height: 2.25rem; border-radius: 50%;
  border: 2px solid var(--aw-border-strong); background: var(--aw-panel); color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-md); font-weight: 600;
}
.phase[data-state='done'] .disc { border-color: var(--aw-teal); background: var(--aw-teal); color: var(--aw-on-accent); }
.phase[data-state='current'] .disc {
  border-color: var(--aw-teal); color: var(--aw-teal);
  box-shadow: 0 0 0 5px var(--aw-teal-soft);
}
.track {
  flex: 1 1 auto; width: 2px; min-height: 1.5rem; margin: .375rem 0;
  border-radius: 2px; background: var(--aw-border-strong);
}
.phase[data-state='done'] .track { background: var(--aw-teal); }

.pbody { min-width: 0; padding-bottom: 1.75rem; }
.phase:last-child .pbody { padding-bottom: 0; }

.phead {
  display: flex; align-items: center; gap: .625rem; width: 100%;
  min-height: 2.25rem; margin-bottom: .625rem; padding: 0 .25rem 0 0;
  border: 0; border-radius: var(--aw-radius-control); background: transparent;
  font: inherit; text-align: left; cursor: pointer;
}
.phead:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.phead:hover .pchev { color: var(--aw-teal); }
/* A folded phase has no card under it, so it needs no gap for one. */
.phead[aria-expanded='false'] { margin-bottom: 0; }

.pt { flex: 0 0 auto; }
.phase[data-state='later'] .pt { color: var(--aw-ink-soft); }
.pdone { flex: 0 0 auto; color: var(--aw-teal); font-size: var(--aw-text-lg); }
.pnow {
  flex: 0 0 auto; display: inline-flex; align-items: center; height: 1.375rem; padding: 0 .5625rem;
  border-radius: var(--aw-radius-pill); background: var(--aw-teal-soft); color: var(--aw-teal-strong);
  font-size: var(--aw-text-xs); font-weight: 600;
}

/* What a shut phase is standing in for. It is the one thing folding away a
   phase could hide, so it is said on the header rather than behind it. */
.pnames {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 400;
}
.pchev { flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm); transition: transform .16s ease; }
.phead[aria-expanded='true'] .pchev { transform: rotate(180deg); }
@media (prefers-reduced-motion: reduce) { .pchev { transition: none; } }

/* --- the ledger ---------------------------------------------------------- */
.ledger {
  margin: 0; padding: 0; list-style: none;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel); overflow: hidden;
}

/* One line per work product, in four fixed columns: state, name, facts, and action. */
.row {
  display: grid;
  grid-template-columns: 1rem minmax(9rem, 16rem) minmax(0, 1fr) auto;
  gap: 0 .75rem;
  align-items: center;
  min-height: 2.75rem;
  padding: .375rem 1rem;
  border-top: 1px solid var(--aw-border);
  transition: background-color .12s ease;
}
.row:first-child { border-top: 0; }
.row:hover { background: color-mix(in srgb, var(--aw-raised) 45%, transparent); }
/* The lead stage is the one being asked for, so it is the one row tinted. */
.row.lead { background: color-mix(in srgb, var(--aw-teal-soft) 60%, transparent); }
.row.lead:hover { background: var(--aw-teal-soft); }

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
.name { display: flex; align-items: center; gap: .625rem; min-width: 0; }
/* The step's place in the plan, in the ledger face so 1.1 to 4.2 line up. */
.sn {
  flex: 0 0 1.75rem; color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums;
}
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

/* Live run progress and open review points hanging under a row. */
.body { grid-column: 2 / -1; display: grid; gap: .25rem; min-width: 0; justify-items: start; padding: .125rem 0 .375rem; }

/* A distribution said on the row's own line. A severe tier that holds
   something keeps its colour, so the matrix still reads amber where it should. */
.meta b[data-severity="warning"] { color: var(--aw-warn-ink); }
.meta b[data-severity="error"] { color: var(--aw-danger-ink); }

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
  .progress { grid-auto-flow: row; grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: .625rem; }
  .pcol:nth-child(odd) { border-left: 0; }

  .row { grid-template-columns: 1rem minmax(0, 1fr) auto; }
  .row .dot, .row .state { grid-column: 1; grid-row: 1; }
  .row .name { grid-column: 2; grid-row: 1; }
  .row .act { grid-column: 3; grid-row: 1; }
  .row .meta { grid-column: 2 / -1; grid-row: 2; margin-top: .125rem; }
  .row .body { grid-column: 2 / -1; grid-row: 3; }
  .pnames { display: none; }
  .phase { column-gap: .75rem; grid-template-columns: 1.75rem minmax(0, 1fr); }
  .disc { width: 1.75rem; height: 1.75rem; font-size: var(--aw-text-sm); }
}
</style>
