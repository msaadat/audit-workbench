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
  Sources: 'pi pi-folder-open',
  'Audit planning memorandum': 'pi pi-map',
  'Cycle design': 'pi pi-sitemap',
  'Risk and control matrix': 'pi pi-table',
  'Control conclusions': 'pi pi-check-square',
  'Test programme': 'pi pi-shield',
  'Document test results': 'pi pi-verified',
  'Fieldwork results': 'pi pi-briefcase',
  'Findings register': 'pi pi-flag',
  'Document analyses': 'pi pi-file',
  'Analysis library': 'pi pi-chart-bar',
  Report: 'pi pi-file-edit',
  Verification: 'pi pi-th-large',
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
  agent.openDrawer()
}

function destinationFor(target: string): WorkspaceDestination | null {
  return KNOWN_DESTINATIONS.includes(target) ? (target as WorkspaceDestination) : null
}

function destinationOf(stage: EngagementStage): WorkspaceDestination | null {
  return destinationFor(stage.filed?.destination ?? '')
}

function icon(label: string): string {
  return FILED_ICONS[label] ?? 'pi pi-box'
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
 * What an owed stage is waiting for, in the words the meta cell takes:
 * `after the memorandum`. The server states it as a sentence — `Waits for the
 * memorandum.` — which under a `not yet` pill read as the row's whole content,
 * on nine rows at once. Beside the row it is one fact, so it is set as a
 * phrase. A reason not written that way is carried through as it stands.
 */
function dependency(stage: EngagementStage): string {
  const reason = stage.blocked_reason.trim()
  const match = /^Waits for (.+?)\.?$/.exec(reason)
  return match ? `after ${match[1]}` : reason
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
  return stage.readiness.reasons[0] ?? ''
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
    || attemptNote(stage) || remaining(stage),
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
      || stage.highlights.length || attemptNote(stage)))
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
    agent.openDrawer()
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

/**
 * How a phase is named *from* the phase after it — `4 stages · after planning`.
 * The titles are imperatives, and "after Plan the engagement" is not English,
 * so the short form is written here rather than derived from one. A phase this
 * map has not heard of falls back to its id, which is the same word in every
 * case the default plan produces.
 */
const PHASE_SHORT: Record<string, string> = {
  sources: 'the sources',
  documents: 'the documents',
  planning: 'planning',
  fieldwork: 'fieldwork',
  writeup: 'the write-up',
}

type PhaseState = 'done' | 'current' | 'later'

interface PhaseGroup {
  id: string
  title: string
  stages: EngagementStage[]
  state: PhaseState
  held: number
  total: number
  /** Whether the lead stage is one of this phase's, which is what makes its
      header able to say that nothing is blocking it. */
  lead: boolean
  /** The phase before this one, in the words `after …` takes. */
  after: string
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

  return phases.map((phase, index) => {
    const rows = byPhase.get(phase.id) ?? []
    const previous = phases[index - 1]
    return {
      id: phase.id,
      title: phase.title,
      stages: rows,
      state: phase.id === currentId
        ? 'current'
        : rows.every(stage => stage.held) ? 'done' : 'later',
      held: rows.filter(stage => stage.held).length,
      total: rows.length,
      lead: rows.some(stage => stage.capability === leadStage.value),
      after: previous ? PHASE_SHORT[previous.id] ?? previous.id : '',
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
 * What a phase amounts to, at the end of its header.
 *
 * A fraction, because the header is read down a column of five and `2 of 2
 * filed` five times over is the same word four times too many. A phase of one
 * stage says nothing at all: `1/1` is a fraction with nothing to compare, and
 * the row under it already says whether it filed.
 *
 * A phase that has not started is counted in stages and named by what it waits
 * for, because `0/4` on work that cannot begin yet reads as a failure rather
 * than as a plan.
 */
function phaseTally(group: PhaseGroup): string {
  if (group.state === 'later') {
    return group.after
      ? `${plural(group.total, 'stage')} · after ${group.after}`
      : plural(group.total, 'stage')
  }
  return group.total > 1 ? `${group.held}/${group.total}` : ''
}

/* --- the whole plan, as one strip ------------------------------------------ */

/**
 * One segment per stage, grouped into the phases, in plan order.
 *
 * It is the only place the whole engagement is visible at once now that the
 * phases fold: five headers say where the work is, and this says how much of
 * it there is. Segment order inside a phase is the plan's, so a reader can
 * count along the strip and land on the row they are looking at.
 */
type SegmentState = 'held' | 'live' | 'lead' | 'owed'

const strip = computed(() => groups.value.map(group => ({
  id: group.id,
  title: group.title,
  current: group.state === 'current',
  segments: group.stages.map((stage): SegmentState => {
    // Work under way outranks work filed: a stage being written again is
    // news, and drawing it in the teal of settled work hides that.
    if (liveState(stage.capability)) return 'live'
    if (stage.held) return 'held'
    return stage.capability === leadStage.value ? 'lead' : 'owed'
  }),
})))

/**
 * The phases share the strip's width in proportion to how many stages they
 * hold, so a segment is the same width everywhere and the strip is a count
 * rather than five bars that happen to sit side by side.
 */
const stripColumns = computed(
  () => strip.value.map(phase => `${phase.segments.length}fr`).join(' '),
)

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
        icon="pi pi-refresh"
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
        <i class="pi pi-sitemap" aria-hidden="true" />Chain
      </RouterLink>
    </div>

    <div v-if="loading && !data" class="loading"><i class="pi pi-spin pi-spinner" /> Reading the record…</div>

    <!-- Only a record with no stages at all is empty, which means the graph
         itself could not be read. A workspace at the very start still draws
         every stage it is going to do, and one whose run history is gone still
         draws everything it holds. -->
    <UiEmptyState
      v-else-if="!stages.length"
      icon="pi pi-book"
      title="No stages to show"
      detail="The engagement plan could not be read, so there is nothing to lay out yet."
    />

    <template v-else>
      <!-- The whole plan at once, which is the one thing the phases cannot
           show while four of the five are folded. -->
      <section class="strip">
        <div class="segs" :style="{ gridTemplateColumns: stripColumns }">
          <div v-for="phase in strip" :key="phase.id" class="sphase">
            <span
              v-for="(segment, index) in phase.segments"
              :key="index"
              class="seg"
              :data-state="segment"
            ></span>
          </div>
        </div>
        <div class="slabels" :style="{ gridTemplateColumns: stripColumns }">
          <span v-for="phase in strip" :key="phase.id" :data-current="phase.current || null">
            {{ phase.title }}
          </span>
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
          <i :class="live.waiting ? 'pi pi-question-circle' : 'pi pi-spin pi-spinner'" />
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
          :icon="live.waiting ? 'pi pi-reply' : 'pi pi-sparkles'"
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
      <div class="phases">
        <section
          v-for="(group, index) in groups"
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
            <span class="pn">{{ index + 1 }}</span>
            <span class="pt">{{ group.title }}</span>
            <span v-if="group.state === 'current'" class="pnext">Next</span>
            <!-- A shut phase says what is inside it, so folding one away never
                 hides which work products it covers. -->
            <template v-if="!phaseOpen(group) && group.total">
              <span class="prule" aria-hidden="true"></span>
              <span class="pnames">{{ phaseNames(group) }}</span>
            </template>
            <span class="grow"></span>
            <span v-if="group.state === 'current' && group.lead" class="pel">
              Nothing is blocking it.
            </span>
            <span v-if="phaseTally(group)" class="pst">{{ phaseTally(group) }}</span>
            <i v-if="!phaseOpen(group)" class="pi pi-chevron-down pchev" aria-hidden="true" />
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
                     reader scanning the phase is looking down. -->
                <span class="dot" aria-hidden="true"></span>

                <span class="name">
                  <template v-if="stage.filed">
                    <i :class="icon(stage.filed.label)" class="wpi" aria-hidden="true" />
                    <!-- The work product is the link. It used to be a pill
                         inside a row inside a card, three borders deep, and the
                         pill said nothing the label did not. -->
                    <component
                      :is="destinationOf(stage) ? RouterLink : 'span'"
                      :to="destinationOf(stage) ? nav.to(destinationOf(stage)!) : undefined"
                      class="wp"
                      :class="{ linked: !!destinationOf(stage) }"
                    >{{ stage.filed.label }}</component>
                    <b v-if="count(stage)" class="ct" :title="size(stage) || undefined">{{ count(stage) }}</b>
                  </template>
                  <span v-else class="none">&#8212;</span>

                  <span v-if="title(stage)" class="sen">{{ title(stage) }}</span>

                  <!-- Doors beside the label, for a stage that opens more than
                       one thing. A tool is drawn differently from an artifact on
                       purpose: nothing is filed by running a query, and a teal
                       door here would have the record claim otherwise. -->
                  <template v-if="stage.links.length">
                    <span class="nrule" aria-hidden="true"></span>
                    <component
                      v-for="link in stage.links"
                      :key="link.label"
                      :is="destinationFor(link.destination) ? RouterLink : 'span'"
                      :to="destinationFor(link.destination) ? nav.to(destinationFor(link.destination)!) : undefined"
                      class="door"
                      :data-kind="link.kind"
                    >
                      <i v-if="link.kind === 'tool'" class="pi pi-wrench" aria-hidden="true" />
                      {{ link.label }}<b v-if="link.count !== null">{{ link.count }}</b>
                    </component>
                  </template>
                </span>

                <span class="meta">
                  <!-- A stage that has not run is measured by what it waits for,
                       which is the only thing it has to report. -->
                  <em v-if="!stage.held && dependency(stage)" class="dep">{{ dependency(stage) }}</em>
                  <template v-else>
                    <!-- The folded body, counted. Colour survives the fold. -->
                    <template v-if="!isOpen(stage)">
                      <span
                        v-for="chip in chips(stage)"
                        :key="chip.label"
                        class="sig"
                        :data-severity="chip.severity || null"
                      ><b>{{ chip.value }}</b>{{ chip.label }}</span>
                    </template>
                    <span v-if="stamp(stage)" class="stamp">{{ stamp(stage) }}</span>
                  </template>
                </span>

                <span class="act">
                  <!-- Sources is the one stage the assistant cannot begin.
                       Bringing in the audit file is the auditor's own act, so
                       the row hands back the shell's dialog. -->
                  <Button
                    v-if="stage.action === 'import'"
                    :label="stage.held ? 'Import more' : 'Import'"
                    size="small"
                    :severity="stage.capability === leadStage ? undefined : 'secondary'"
                    :outlined="stage.capability !== leadStage"
                    @click="start(stage)"
                  />
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
                    <i class="pi pi-chevron-right" aria-hidden="true" />
                  </button>
                </span>

                <span v-if="hasBody(stage)" class="body">
                  <span v-if="isOpen(stage) && saying(stage)" class="dsc">{{ saying(stage) }}</span>

                  <!-- What a stage that has already filed still owes. The count
                       beside it is not contradicted: thirty findings are filed
                       and two observations are undrafted, and both are true. -->
                  <span v-if="isOpen(stage) && remaining(stage)" class="left">
                    <i class="pi pi-hourglass" aria-hidden="true" />{{ remaining(stage) }}
                  </span>

                  <!-- Being produced right now, whether or not it was filed before. -->
                  <span v-if="liveState(stage.capability)" class="again" :data-live="liveState(stage.capability)">
                    <i :class="liveState(stage.capability) === 'running' ? 'pi pi-spin pi-spinner' : 'pi pi-clock'" aria-hidden="true" />
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
                    <i class="pi pi-exclamation-triangle" aria-hidden="true" />
                    <span class="ot">{{ point.message }}</span>
                    <span class="oa">{{ point.action }}<i class="pi pi-arrow-right" aria-hidden="true" /></span>
                  </button>

                  <button
                    v-if="isOpen(stage) && attemptNote(stage)"
                    type="button"
                    class="tries"
                    :aria-expanded="expanded.has(stage.id)"
                    @click="toggle(stage)"
                  >
                    <i :class="expanded.has(stage.id) ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" aria-hidden="true" />
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
      </div>

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
        <span class="mark"><i class="pi pi-exclamation-circle" /></span>
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
  margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700;
  letter-spacing: .1em; text-transform: uppercase;
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
.chain .pi { font-size: var(--aw-text-sm); }

/* --- the whole plan, as one strip ---------------------------------------- */
/* Twelve stages, drawn once. It carried a sentence saying the same thing in
   words, which is two readings of one fact and the taller of the two. */
.strip {
  display: flex; flex-direction: column; gap: .375rem;
  padding: .625rem 1rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}

/* A phase takes the width its stage count earns, so one segment is one stage
   everywhere on the strip and the whole bar can be counted. */
.segs, .slabels { display: grid; gap: 6px; }
.sphase { display: flex; gap: 3px; }
.seg { flex: 1; height: 8px; border-radius: 3px; background: var(--aw-border); }
.seg[data-state='held'] { background: var(--aw-teal); }
/* Work under way outranks work filed: teal is what the engagement has, blue is
   what is happening, and a live stage drawn in teal reads as already settled. */
.seg[data-state='live'] { background: var(--aw-info); }
.seg[data-state='lead'] { background: var(--aw-teal-line); }

.slabels { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.slabels [data-current] { color: var(--aw-teal-strong); }

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

/* --- one card per phase --------------------------------------------------- */
/* The card carries the state, and every part of the header reads it off the
   section rather than being told twice: a done phase is a plain panel, the
   phase being worked is ringed and tinted, and a phase whose turn has not come
   is dashed and transparent — drawn as a plan rather than as work missing. */
.phases { display: flex; flex-direction: column; gap: .75rem; }
.phase {
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel); overflow: hidden;
}
.phase[data-state='current'] { border-color: var(--aw-teal-line); }
.phase[data-state='later'] { border-style: dashed; border-color: var(--aw-border-strong); background: transparent; }

.phead {
  display: flex; align-items: center; gap: .75rem; width: 100%;
  padding: .55rem 1rem;
  border: 0; background: var(--aw-raised);
  font: inherit; text-align: left; cursor: pointer;
}
.phase[data-state='current'] .phead { background: var(--aw-teal-soft); }
.phase[data-state='later'] .phead { background: transparent; }
.phead:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }

/* The numeral says where in the audit this is without a word, which is what
   lets the rest of the header be about this phase rather than about the five. */
.pn {
  flex: 0 0 auto; display: grid; place-items: center; width: 22px; height: 22px;
  border-radius: 50%; background: var(--aw-teal); color: var(--aw-on-accent);
  font-size: var(--aw-text-xs); font-weight: 700; font-variant-numeric: tabular-nums;
}
.phase[data-state='current'] .pn {
  border: 2px solid var(--aw-teal); background: var(--aw-panel); color: var(--aw-teal);
}
.phase[data-state='later'] .pn {
  border: 1.5px dashed var(--aw-border-strong); background: var(--aw-panel); color: var(--aw-muted);
}

.pt { flex: 0 0 auto; color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600; }
.phase[data-state='later'] .pt { color: var(--aw-ink-soft); }

.pnext {
  flex: 0 0 auto; padding: 1px .5rem; border-radius: var(--aw-radius-pill);
  background: var(--aw-teal); color: var(--aw-on-accent);
  font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
}

.pst {
  flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm);
  font-variant-numeric: tabular-nums;
}
.phase[data-state='done'] .pst { color: var(--aw-teal-strong); font-weight: 600; }

/* What a shut phase is standing in for. It is the one thing folding away a
   phase could hide, so it is said on the header rather than behind it. */
.prule { flex: 0 0 auto; width: 1px; height: 14px; background: var(--aw-border); }
.pnames {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-muted); font-size: var(--aw-text-sm);
}
.pel {
  flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.pchev { flex: 0 0 auto; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- the ledger ---------------------------------------------------------- */
/* The phase card is the surface now, so the list inside it is only a list. */
.ledger { margin: 0; padding: 0; list-style: none; }

/* One line per work product: what state it is in, what it is, what it amounts
   to, and the one thing to do about it. The four columns are fixed so a reader
   scans down them rather than across each row. */
.row {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto 120px;
  gap: 0 .875rem;
  align-items: center;
  padding: .7rem 1rem;
  /* Ruled off from what is above it, which is the phase header on the first. */
  border-top: 1px solid var(--aw-border);
}
.row.shut { cursor: pointer; }
.row.shut:hover { background: color-mix(in srgb, var(--aw-raised) 55%, transparent); }

/* The state of the stage, said once. It replaces a time column that read "—"
   on nine rows of twelve and a spine whose dot said the same thing twice. */
.dot {
  box-sizing: border-box; width: 10px; height: 10px; margin: 0 auto;
  border-radius: 50%; background: var(--aw-teal);
}
.row[data-status="completed_with_issues"] .dot,
.row[data-status="needs_review"] .dot { background: var(--aw-warn); }
.row.ghost .dot {
  width: 9px; height: 9px;
  border: 1.5px dashed var(--aw-border-strong); background: var(--aw-panel);
}
.row.ghost.lead .dot {
  width: 10px; height: 10px;
  border: 2px solid var(--aw-teal); background: var(--aw-panel);
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

/* --- what the row is, on one line ---------------------------------------- */
.name { display: flex; align-items: center; gap: .625rem; min-width: 0; }
.wpi { flex: 0 0 auto; font-size: var(--aw-text-base); color: var(--aw-teal); }
.row.ghost .wpi { color: var(--aw-muted); }
/* The work product is the link. It used to be a pill inside a row inside a
   card — three borders around a label that was already the whole answer. */
.wp {
  flex: 0 0 auto;
  color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600;
  text-decoration: none; white-space: nowrap;
}
a.wp:hover { color: var(--aw-teal-strong); text-decoration: underline; }
a.wp:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 2px; }
.row.ghost .wp { color: var(--aw-muted); }
.ct {
  flex: 0 0 auto;
  color: var(--aw-teal); font-size: var(--aw-text-sm); font-weight: 700;
  font-variant-numeric: tabular-nums;
}
/* The row's own sentence, and the first thing to give way when the row is
   narrow: the label and the count above it are the reading that must survive. */
.sen {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--aw-ink-soft); font-size: var(--aw-text-sm);
}
.row.ghost .sen { color: var(--aw-muted); }
/* The lead stage is the one being asked for, and a stage under way is being
   answered, so neither is written in the grey the rows around them are. Their
   dots carry the rest of the difference. */
.row.ghost.lead .wp,
.row[data-live] .wp { color: var(--aw-ink-strong); }
.row.ghost.lead .wpi,
.row[data-live] .wpi { color: var(--aw-teal); }
.row.ghost.lead .sen,
.row[data-live] .sen { color: var(--aw-ink-soft); }
.nrule { flex: 0 0 auto; width: 1px; height: 14px; background: var(--aw-border); }
.none { color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- what the row amounts to --------------------------------------------- */
.meta {
  display: flex; align-items: center; justify-content: flex-end; gap: .5rem;
  white-space: nowrap;
}
.stamp { color: var(--aw-muted); font-size: var(--aw-text-sm); font-variant-numeric: tabular-nums; }
/* A stage that has not run is measured by what it waits for. Set as a phrase,
   because `Waits for the memorandum.` under a `not yet` pill was the whole
   content of nine rows at once. */
.dep { color: var(--aw-muted); font-size: var(--aw-text-sm); font-style: italic; }

.act { display: flex; align-items: center; justify-content: flex-end; }

/* Everything the row says under its own line: the folded body, and the two
   things that are said whether it is folded or not. */
.body { grid-column: 2 / -1; display: grid; gap: .2rem; min-width: 0; justify-items: start; margin-top: .35rem; }

/* What a filed stage still owes. Amber like the debts it sits among, but a
   line rather than a button: there is nothing here to click, only something
   the next run will pick up. */
.left {
  display: flex; gap: .4rem; align-items: baseline;
  color: var(--aw-warn-ink); font-size: var(--aw-text-sm);
}
.left i { font-size: .7rem; }

/* --- doors beside the label ---------------------------------------------- */
/* An artifact door is a quieter relative of the label beside it: same family,
   less weight, because it opens a part of what the row filed rather than the
   row itself. A tool door is deliberately outside that family — neutral, with
   a wrench — because running one files nothing, and drawing it in the teal the
   record uses for held work would be a claim the engagement cannot support. */
.door {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .16rem .5rem;
  border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-control);
  color: var(--aw-teal); background: transparent;
  font-size: var(--aw-text-sm); font-weight: 600; text-decoration: none; white-space: nowrap;
}
.door b { font-weight: 700; font-variant-numeric: tabular-nums; }
a.door:hover { background: var(--aw-teal-soft); }
a.door:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.door[data-kind='tool'] {
  border-color: var(--aw-border-strong); border-style: dashed; color: var(--aw-muted);
}
a.door[data-kind='tool']:hover { background: var(--aw-raised); color: var(--aw-ink-soft); }
.door .pi { font-size: var(--aw-text-xs); }

.dsc { max-width: 72ch; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.55; }

/* What the folded body is standing in for, counted. A row with nothing to say
   carries no chip, which is what makes the rows that do carry one findable. */
.sig {
  display: inline-flex; align-items: baseline; gap: .28rem;
  padding: .08rem .45rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill);
  background: var(--aw-raised); color: var(--aw-muted-strong);
  font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap;
}
.sig b { font-variant-numeric: tabular-nums; }
.sig[data-severity="warning"] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.sig[data-severity="error"] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger-ink); }

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
.tally span { font-size: var(--aw-text-xs); letter-spacing: .04em; text-transform: uppercase; }
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
/* Narrow, what the row amounts to drops under what it is. The one thing that
   stays on the first line is the thing to do about it: an action that scrolls
   away from the row it belongs to is one nobody takes. */
@container (max-width: 56rem) {
  /* A phase title is allowed to take two lines here. The segments under it are
     not: the strip is a count, and a count that reflows is not one. */
  .slabels { line-height: 1.25; }

  .row { grid-template-columns: 16px minmax(0, 1fr) 120px; }
  .row .dot { grid-column: 1; grid-row: 1; }
  .row .name { grid-column: 2; grid-row: 1; }
  .row .act { grid-column: 3; grid-row: 1; }
  .row .meta { grid-column: 2; grid-row: 2; justify-content: flex-start; margin-top: .2rem; }
  .row .body { grid-column: 2 / -1; grid-row: 3; }
}
</style>
