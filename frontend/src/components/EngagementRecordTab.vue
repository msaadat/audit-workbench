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
  'apm', 'rcm', 'chain', 'doc-tests', 'data-tests',
  'findings', 'report', 'documents', 'data', 'query', 'analysis',
]

/** An icon per work product, chosen from what the artifact *is*. */
const FILED_ICONS: Record<string, string> = {
  Sources: 'pi pi-folder-open',
  'Audit planning memorandum': 'pi pi-map',
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
 * What the pill says: the bare number where the work product has a size, and
 * what state it is in otherwise. The unit belongs to the work product, not to
 * the count — `Analysis library · 24` is a unit, `Analysis library · 24
 * analyses` is a sentence — so the spelled form moves to the pill's title.
 */
function tally(stage: EngagementStage): string {
  const live = liveState(stage.capability)
  if (live === 'running') return 'being written'
  if (live === 'queued') return 'queued'
  // An empty register is what "not yet" means, and says it in the words the
  // rest of the row is written in. A bare 0 reads as a measurement.
  if (!stage.held) return 'not yet'
  return stage.filed?.count == null ? '' : String(stage.filed.count)
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
 */
const totalLine = computed(() => {
  const value = totals.value
  if (!value) return ''
  const parts = [plural(value.work_products, 'work product')]
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

/** The stages still owed, which is what the ledger's forward half amounts to. */
const owedStages = computed(() => stages.value.filter(stage => !stage.held))

/**
 * Where the ledger crosses from what is held into what is owed. -1 on an
 * engagement that holds everything, which draws no divider at all rather than
 * one with nothing under it.
 */
const nowIndex = computed(() => stages.value.findIndex(stage => !stage.held))

const pendingNote = computed(() => {
  const count = owedStages.value.length
  if (!count) return ''
  const underway = owedStages.value.filter(stage => liveState(stage.capability)).length
  if (underway === count) return `${plural(count, 'stage')} under way`
  if (underway) return `${underway} of ${count} under way`
  return `${plural(count, 'stage')} ${count === 1 ? 'has' : 'have'} not run`
})
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
      <Button label="Refresh" icon="pi pi-refresh" size="small" severity="secondary" outlined :loading="loading" @click="load" />
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
      <!-- While a run is in flight it, not the next step, is the news. The two
           never show together: proposing work that is under way is exactly the
           staleness this band exists to remove. -->
      <section v-if="live" class="brief live" :data-wait="live.waiting ? '1' : null">
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
        <Button
          :label="live.waiting ? 'Respond' : 'Watch it'"
          :icon="live.waiting ? 'pi pi-reply' : 'pi pi-sparkles'"
          size="small"
          :severity="live.waiting ? undefined : 'secondary'"
          :outlined="!live.waiting"
          @click="watchRun"
        />
      </section>

      <ol class="ledger">
        <li class="head" aria-hidden="true">
          <span>Time</span><span></span><span>Filed</span><span>What it did</span><span class="r">Took</span><span></span>
        </li>

        <template v-for="(stage, index) in stages" :key="stage.id">
          <!-- The ledger crosses from what the engagement holds into what it
               still owes exactly once, and says so where it happens. -->
          <li v-if="index === nowIndex" class="nowline">
            <span class="lab">Now</span>
            <span class="hr" />
            <span class="lab">{{ pendingNote }}</span>
          </li>
          <li
            class="row"
            :class="{ shut: !isOpen(stage), ghost: !stage.held, lead: stage.capability === leadStage }"
            :data-status="stage.history?.status || null"
            :data-live="liveState(stage.capability) || null"
            @click="rowClick(stage, $event)"
          >
            <span class="tm">{{ liveState(stage.capability) === 'running'
              ? 'now'
              : (stage.history ? clock(stage.history.at) : '—') }}</span>
            <span class="gut"><i /></span>

            <span class="made">
              <component
                :is="destinationOf(stage) ? RouterLink : 'span'"
                v-if="stage.filed"
                :to="destinationOf(stage) ? nav.to(destinationOf(stage)!) : undefined"
                class="card"
                :class="{ linked: !!destinationOf(stage) }"
                :title="size(stage) || null"
              >
                <i :class="icon(stage.filed.label)" aria-hidden="true" />
                <span class="mt">
                  <b>{{ stage.filed.label }}</b>
                  <em v-if="tally(stage)">{{ tally(stage) }}</em>
                </span>
              </component>
              <span v-else class="none">—</span>

              <!-- Doors beside the card, for a stage that opens more than one
                   thing. A tool is drawn differently from an artifact on
                   purpose: nothing is filed by running a query, and a teal pill
                   here would have the record claim the engagement holds one. -->
              <span v-if="stage.links.length" class="doors">
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
              </span>
            </span>

            <span class="say">
              <span class="saytop">
                <b class="ttl">{{ title(stage) }}</b>
                <!-- The folded body, counted. Colour survives the fold. -->
                <template v-if="!isOpen(stage)">
                  <span
                    v-for="chip in chips(stage)"
                    :key="chip.label"
                    class="sig"
                    :data-severity="chip.severity || null"
                  ><b>{{ chip.value }}</b>{{ chip.label }}</span>
                </template>
              </span>
              <span v-if="isOpen(stage) && saying(stage)" class="dsc">{{ saying(stage) }}</span>
              <!-- A stage that is owed says what is holding it, open or shut:
                   it is the whole content of the row. -->
              <span v-else-if="!stage.held && stage.blocked_reason" class="dsc">
                {{ stage.blocked_reason }}
              </span>

              <!-- What a stage that has already filed still owes. The count
                   beside it is not contradicted: thirty findings are filed and
                   two observations are undrafted, and both are true. -->
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

              <!-- What this stage left open behind it. Every debt has a row to
                   sit on now, because the stage that owes it is always drawn. -->
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

            <span class="took">
              <!-- A stage the run in flight already owns must not offer to be
                   started a second time. -->
              <span v-if="liveState(stage.capability) === 'running'" class="going">
                <i class="pi pi-spin pi-spinner" aria-hidden="true" />{{ liveSince(stage.capability) }}
              </span>
              <span v-else-if="liveState(stage.capability) === 'queued'" class="waits">queued</span>
              <!-- A stage offering narrower runs draws them under the button,
                   never beside it: the click stays the complete answer. -->
              <SplitButton
                v-else-if="stage.runnable && stage.start?.alternates.length"
                label="Run"
                size="small"
                :severity="stage.capability === leadStage ? undefined : 'secondary'"
                :outlined="stage.capability !== leadStage"
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
                v-else-if="stage.runnable"
                label="Run"
                size="small"
                :severity="stage.capability === leadStage ? undefined : 'secondary'"
                :outlined="stage.capability !== leadStage"
                :loading="starting === stage.capability"
                @click="start(stage)"
              />
              <span v-else-if="!stage.held && stage.blocked_reason" class="waits">waits</span>
              <template v-else-if="stage.history">{{ duration(stage.history.elapsed_ms) }}</template>
            </span>

            <!-- The row is the hit target; this is what says so. Hidden under
                 Full, where every row is open and nothing can be shut. -->
            <button
              v-if="density === 'concise' && foldable(stage)"
              type="button"
              class="chev"
              :aria-expanded="isOpen(stage)"
              :aria-label="`${isOpen(stage) ? 'Collapse' : 'Expand'} ${title(stage) || stage.filed?.label || stage.capability}`"
              @click="toggleRow(stage)"
            >
              <i class="pi pi-chevron-right" aria-hidden="true" />
            </button>
            <span v-else></span>
          </li>
        </template>

        <!-- Debts whose stage never filed have no row to sit on, and still
             have to be said. -->
      </ol>

      <!-- The single most blocking thing, restated as a sentence. It sits under
           the ledger rather than over it: the ledger is what the reader came
           for, and every debt in this line is already drawn on the row that
           owes it. A run in flight is not one of these — that is news, and it
           stays at the top. -->
      <section v-if="!live && next" class="brief" :data-kind="next.kind">
        <span class="mark"><i :class="next.kind === 'open_point' ? 'pi pi-exclamation-circle' : 'pi pi-play'" /></span>
        <div class="txt">
          <strong>{{ next.kind === 'open_point' ? next.message : next.headline }}</strong>
          <span v-if="next.kind === 'stage'">Nothing is blocking it.</span>
        </div>
        <Button
          v-if="next.kind === 'open_point'"
          :label="next.action"
          size="small"
          severity="secondary"
          outlined
          @click="openPoint(next)"
        />
        <Button
          v-else
          label="Start"
          icon="pi pi-play"
          size="small"
          severity="secondary"
          outlined
          :loading="starting === next.capability"
          @click="start(next)"
        />
      </section>

      <!-- What the whole engagement cost, as a footnote to the ledger it is a
           footnote to. It answered no question anyone arrives with, and it was
           the first thing on the page. -->
      <footer class="summary">
        <span><b>{{ totals?.work_products ?? 0 }}</b> work products held</span>
        <span><b>{{ duration(totals?.elapsed_ms ?? null) }}</b> of assistant time</span>
        <span class="grow"></span>
        <span>
          {{ totalLine }}<template v-if="quietRuns">
            · <span class="quiet">{{ plural(quietRuns, 'run') }} filed nothing</span></template>
        </span>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.record { display: flex; flex-direction: column; gap: .55rem; min-height: 0; }
.loading { display: grid; place-content: center; gap: .4rem; padding: 3rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- the strip above the ledger ------------------------------------------ */
.bar { display: flex; align-items: center; gap: .5rem; }
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

/* --- what is still owed, under the ledger -------------------------------- */
/* Below the ledger it is a note, not a banner, so it is drawn as one: half the
   padding it had, and its wording at the size of the rows it refers to. */
.brief {
  display: flex;
  align-items: center;
  gap: .6rem;
  padding: .45rem .6rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft);
}
.brief[data-kind='stage'] { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.brief .mark { flex: 0 0 auto; display: grid; place-items: center; color: var(--aw-warn-ink); font-size: var(--aw-text-sm); }
.brief[data-kind='stage'] .mark { color: var(--aw-teal); }
.brief .txt { flex: 1; min-width: 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: .1rem .4rem; }
.brief strong { color: var(--aw-warn-ink); font-size: var(--aw-text-sm); font-weight: 600; line-height: 1.4; }
.brief[data-kind='stage'] strong { color: var(--aw-teal-strong); }
.brief span { color: var(--aw-ink-soft); font-size: var(--aw-text-xs); }

/* --- what the whole engagement cost, as a footnote ----------------------- */
.summary {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: .2rem 1.2rem;
  padding: 0 .2rem;
  color: var(--aw-muted); font-size: var(--aw-text-xs);
}
.summary b { color: var(--aw-ink-soft); font-weight: 600; font-variant-numeric: tabular-nums; }
.quiet { color: var(--aw-muted-strong); }

/* --- the ledger ---------------------------------------------------------- */
.ledger { margin: 0; padding: 0; list-style: none; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); overflow: hidden; }

.head, .row {
  display: grid;
  grid-template-columns: 4rem 1rem 17.5rem minmax(0, 1fr) 5rem 1.2rem;
  gap: 0 .6rem;
  align-items: start;
  padding: .7rem 1rem;
}
/* A shut row is one line, so its padding is the line's own breathing room
   rather than the block of a row that runs to two hundred pixels. */
.row.shut { padding-block: .42rem; cursor: pointer; }
.row.shut:hover { background: color-mix(in srgb, var(--aw-raised) 55%, transparent); }
.head {
  padding-block: .55rem; border-bottom: 1px solid var(--aw-border); background: var(--aw-raised);
  color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
}
.head .r { text-align: right; }
.row + .row { border-top: 1px solid var(--aw-border); }

/* What a filed stage still owes. Amber like the debts it sits among, but a
   line rather than a button: there is nothing here to click, only something
   the next run will pick up. */
.left {
  display: flex; gap: .4rem; align-items: baseline;
  color: var(--aw-warn-ink, var(--aw-muted)); font-size: var(--aw-text-sm);
}
.left i { font-size: .7rem; }

.tm { padding-top: .2rem; color: var(--aw-muted); font-size: var(--aw-text-sm); font-variant-numeric: tabular-nums; }
.took { padding-top: .2rem; text-align: right; color: var(--aw-muted); font-size: var(--aw-text-sm); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* the connecting spine. The gutter stretches or the line reaches 46px down a
   row that can run to 200. */
.gut { position: relative; align-self: stretch; display: flex; justify-content: center; padding-top: .35rem; }
.gut i { z-index: 1; width: .5rem; height: .5rem; border-radius: 50%; background: var(--aw-teal); box-shadow: 0 0 0 2px var(--aw-panel); }
.gut::before { content: ""; position: absolute; top: .8rem; bottom: -1.9rem; left: 50%; width: 1px; background: var(--aw-border-strong); transform: translateX(-.5px); }
.row:last-child .gut::before { display: none; }
.row[data-status="completed_with_issues"] .gut i,
.row[data-status="needs_review"] .gut i { background: var(--aw-warn); }

/* The work product is a pill: one unit, one line, the same object whether the
   row is open or shut. Opening a row changes what the row says about the work,
   never what was filed. */
.made { min-width: 0; }
.card {
  display: inline-flex; align-items: center; gap: .45rem; max-width: 100%;
  padding: .32rem .75rem .32rem .6rem;
  border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-pill);
  background: var(--aw-teal-soft); color: var(--aw-teal-strong);
  text-decoration: none; white-space: nowrap;
}
.card.linked:hover { border-color: var(--aw-teal); background: var(--aw-panel); box-shadow: var(--aw-shadow-sm); }
.card.linked:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.card > i { font-size: var(--aw-text-sm); }
.mt { display: inline-flex; align-items: baseline; gap: .4rem; min-width: 0; }
.mt b { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-size: var(--aw-text-base); font-weight: 600; line-height: 1.4; }
/* The count belongs to the same unit, hung off a hairline rather than set
   loose on a second line. */
.mt em {
  flex: 0 0 auto; padding-left: .4rem; border-left: 1px solid var(--aw-teal-line);
  color: var(--aw-teal); font-size: var(--aw-text-sm); font-style: normal;
  font-weight: 700; font-variant-numeric: tabular-nums;
}
.none { display: block; padding-top: .3rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- doors beside the card ----------------------------------------------- */
/* An artifact door is a quieter relative of the card above it: same family,
   less weight, because it opens a part of what the row filed rather than the
   row itself. A tool door is deliberately outside that family — neutral, with
   a wrench — because running one files nothing, and drawing it in the teal the
   record uses for held work would be a claim the engagement cannot support. */
.doors { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .3rem; }
.door {
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

.say { display: grid; gap: .2rem; min-width: 0; justify-items: start; }
.saytop { display: flex; align-items: baseline; flex-wrap: wrap; gap: .3rem .5rem; min-width: 0; }
.ttl { font-size: var(--aw-text-md); font-weight: 600; line-height: 1.3; color: var(--aw-ink-strong); }
.row.shut .ttl { font-size: var(--aw-text-base); font-weight: 500; color: var(--aw-ink); }
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

/* --- the now line and the phantom tail ---------------------------------- */
.nowline {
  display: flex; align-items: center; gap: .6rem;
  padding: .4rem 1rem; border-top: 1px solid var(--aw-border); background: var(--aw-canvas);
}
.nowline .lab { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.nowline .hr { flex: 1; height: 1px; background: repeating-linear-gradient(90deg, var(--aw-border-strong) 0 4px, transparent 4px 8px); }

.row.ghost { background: var(--aw-canvas); }
.row.ghost .gut i { width: .55rem; height: .55rem; background: var(--aw-panel); border: 1.5px dashed var(--aw-border-strong); box-shadow: none; }
.row.ghost .gut::before { background: repeating-linear-gradient(180deg, var(--aw-border-strong) 0 3px, transparent 3px 7px); }
.row.ghost .card { border-style: dashed; border-color: var(--aw-border-strong); background: transparent; color: var(--aw-muted); }
.row.ghost .mt em { color: var(--aw-muted); }
.row.ghost .ttl { color: var(--aw-muted); }
.row.ghost .took { padding-top: 0; }
.waits { font-style: italic; }

/* The note is the half that keeps "defer" from reading as "skip", so it is set
   as a second line rather than a tooltip. */
.alt { display: grid; gap: .1rem; padding: .4rem .75rem; text-align: left; white-space: normal; }
.alt small { color: var(--aw-muted); font-size: var(--aw-text-xs); max-width: 18rem; }

/* Only the first runnable stage is drawn as a call to action: a tail of six
   buttons is a menu, not a next step. */
.row.ghost.lead { background: var(--aw-teal-soft); }
.row.ghost.lead .gut i { border-style: solid; border-color: var(--aw-teal); }
.row.ghost.lead .card { border-style: solid; border-color: var(--aw-teal); background: var(--aw-panel); color: var(--aw-teal-strong); }
.row.ghost.lead .ttl { color: var(--aw-ink-strong); }


/* --- the run in flight ---------------------------------------------------- */
/* Blue, deliberately: teal is what the engagement has filed and amber is what
   it owes. Work happening right now is neither, and reusing either colour made
   a live row read as already settled. */
.brief.live { border-color: var(--aw-info-line); background: var(--aw-info-soft); }
.brief.live .mark { color: var(--aw-info); }
.brief.live[data-wait] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); }
.brief.live[data-wait] .mark { color: var(--aw-warn-ink); }

/* A work product that is already filed and is being produced again. */
.again {
  display: inline-flex; align-items: center; gap: .35rem;
  margin-top: .3rem; padding: .15rem .45rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-info-soft); color: var(--aw-info);
  font-size: var(--aw-text-xs); font-weight: 600;
}
.again i { font-size: var(--aw-text-xs); }

.going {
  display: inline-flex; align-items: center; gap: .3rem; justify-content: flex-end;
  color: var(--aw-info); font-size: var(--aw-text-xs); font-weight: 600;
}

.row.ghost[data-live] { background: var(--aw-info-soft); }
.row.ghost[data-live] .card { border-style: solid; border-color: var(--aw-info-line); background: var(--aw-panel); color: var(--aw-info); }
.row.ghost[data-live] .mt em { color: var(--aw-info); }
.row.ghost[data-live] .ttl { color: var(--aw-ink-strong); }
.row.ghost[data-live] .dsc { color: var(--aw-ink-soft); }
.row.ghost[data-live] .gut i { border-style: solid; border-color: var(--aw-info); background: var(--aw-info); }
.row.ghost[data-live] .gut::before { background: var(--aw-info-line); }

/* The running row is the only one that moves. A queued row is scheduled, not
   under way, and a tail of pulsing dots says nothing about which is which. */
.row.ghost[data-live='running'] .tm { color: var(--aw-info); font-weight: 700; }
.row.ghost[data-live='running'] .gut i { animation: aw-record-pulse 1.8s ease-out infinite; }
.row.ghost[data-live='queued'] .gut i { background: var(--aw-panel); border-color: var(--aw-info-line); }

/* The ring is mixed from the token rather than written out, because the blue
   inverts between themes and a fixed rgba() would glow dark-on-dark. */
@keyframes aw-record-pulse {
  0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--aw-info) 45%, transparent); }
  70% { box-shadow: 0 0 0 .4rem transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}
@media (prefers-reduced-motion: reduce) {
  .row.ghost[data-live='running'] .gut i { animation: none; }
}

@container (max-width: 56rem) {
  .head { display: none; }
  .row { grid-template-columns: 4rem 1rem minmax(0, 1fr) 1.2rem; }
  .row .took { grid-column: 3; text-align: left; }
  .row .made { grid-column: 3; }
  .row .say { grid-column: 3; }

  /* A shut row stays one line here too: the pill shrink-wraps to its own text
     and the headline takes what is left. */
  .row.shut { grid-template-columns: 4rem 1rem auto minmax(5rem, 1fr) 4rem 1.2rem; }
  .row.shut .made { grid-column: 3; }
  .row.shut .say { grid-column: 4; }
  .row.shut .took { grid-column: 5; text-align: right; }
}
</style>
