<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../api'
import { plural } from '../format'
import { useAgentRun } from '../composables/useAgentRun'
import { useAssistantChat } from '../composables/useAssistantChat'
import { useWorkspaceNav, type WorkspaceDestination } from '../composables/useWorkspaceNavigation'
import type {
  EngagementOpenPoint, EngagementPendingStage, EngagementRecordEntry,
  EngagementRecordPayload, WorkspaceSummary,
} from '../types'
import UiEmptyState from './ui/UiEmptyState.vue'
import UiPageHeader from './ui/UiPageHeader.vue'

/**
 * The engagement record: what this engagement filed, and what it still owes.
 *
 * The backward half is one row per work product — when it settled, what was
 * filed, what the stage established, what every attempt at it cost. The forward
 * half is the same ledger continuing past the present: stages whose work
 * product does not exist yet, drawn as entries not yet written, and the debts
 * that finished stages left hanging off the row that created them.
 *
 * A landing page that only looks backwards asks nothing of the reader. The rule
 * for what it asks first is in `_OPEN_RANK` on the server: reading what the
 * assistant decided outranks running the next stage, because auto mode runs
 * stages by itself and only a person can review.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()
const nav = useWorkspaceNav()
const agent = useAgentRun(props.workspace.id)
const chats = useAssistantChat(props.workspace.id)

const data = ref<EngagementRecordPayload | null>(null)
const loading = ref(true)
const starting = ref('')
const expanded = ref<Set<string>>(new Set())

const KNOWN_DESTINATIONS: readonly string[] = [
  'dashboard', 'apm', 'rcm', 'chain', 'doc-tests', 'data-tests',
  'findings', 'report', 'documents', 'data', 'query', 'analysis',
]

/** An icon per work product, chosen from what the artifact *is*. */
const FILED_ICONS: Record<string, string> = {
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

const entries = computed(() => data.value?.entries ?? [])
const pending = computed(() => data.value?.pending ?? [])
const orphaned = computed(() => data.value?.orphaned_points ?? [])
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

function day(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

/** The date heading a row sits under, or '' when it repeats the row above. */
function dayBreak(index: number): string {
  const current = day(entries.value[index]?.at ?? null)
  const previous = index > 0 ? day(entries.value[index - 1]?.at ?? null) : ''
  return current && current !== previous ? current : ''
}

function destinationFor(target: string): WorkspaceDestination | null {
  return KNOWN_DESTINATIONS.includes(target) ? (target as WorkspaceDestination) : null
}

function destinationOf(entry: EngagementRecordEntry): WorkspaceDestination | null {
  return destinationFor(entry.filed?.destination ?? '')
}

function icon(label: string): string {
  return FILED_ICONS[label] ?? 'pi pi-box'
}

/** `27 rows`, or '' where the work product has no meaningful size. */
function size(entry: EngagementRecordEntry): string {
  const filed = entry.filed
  if (!filed || filed.count == null) return ''
  if (!filed.unit) return String(filed.count)
  return plural(filed.count, filed.unit, filed.unit_plural || undefined)
}

/**
 * What a collapsed row is standing in for. Silent at a single attempt, because
 * "1 attempt" on every row is noise that hides the rows where it matters.
 */
function attemptNote(entry: EngagementRecordEntry): string {
  const tries = entry.attempts.length
  if (tries <= 1) return ''
  const untimed = tries - entry.measured_attempts
  if (!untimed) return `${tries} attempts`
  return `${tries} attempts · ${untimed} not timed`
}

function toggle(entry: EngagementRecordEntry) {
  const next = new Set(expanded.value)
  if (next.has(entry.id)) next.delete(entry.id)
  else next.add(entry.id)
  expanded.value = next
}

function openPoint(point: EngagementOpenPoint) {
  const destination = destinationFor(point.destination)
  if (destination) void nav.push(destination)
}

/**
 * Start a stage that has not run. The assistant owns running work, so this is
 * the same request the guided shortcuts make, and the reader is taken to the
 * thread to watch rather than left on a page that cannot show progress.
 */
async function start(stage: EngagementPendingStage) {
  if (starting.value) return
  starting.value = stage.capability
  try {
    await chats.send(stage.start.prompt, 'act', 'auto', {
      source: 'shortcut',
      requestedOutcomes: stage.start.outcomes,
    })
    await nav.push('console')
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

/** The first runnable stage is the only one drawn as a call to action. */
const leadStage = computed(() => pending.value.find(stage => stage.runnable)?.capability ?? '')

const totalLine = computed(() => {
  const value = totals.value
  if (!value) return ''
  const parts = [plural(value.work_products, 'work product')]
  if (value.attempts > value.work_products) {
    parts.push(`${plural(value.attempts, 'run')} across ${plural(value.runs_that_filed, 'session')}`)
  }
  return parts.join(' · ')
})

// A run that committed nothing filed nothing. Stating it is more honest than a
// record that silently drops a third of the history.
const quietRuns = computed(() => {
  const value = totals.value
  return value ? Math.max(0, value.runs - value.runs_that_filed) : 0
})

const pendingNote = computed(() => {
  const count = pending.value.length
  if (!count) return ''
  return `${plural(count, 'stage')} ${count === 1 ? 'has' : 'have'} not run`
})
</script>

<template>
  <div class="record">
    <UiPageHeader title="Engagement record" eyebrow="What this engagement filed">
      <Button label="Refresh" icon="pi pi-refresh" size="small" severity="secondary" outlined :loading="loading" @click="load" />
    </UiPageHeader>

    <div v-if="loading && !data" class="loading"><i class="pi pi-spin pi-spinner" /> Reading the record…</div>

    <UiEmptyState
      v-else-if="!entries.length && !pending.length"
      icon="pi pi-book"
      title="Nothing filed yet"
      detail="Once the assistant completes a stage, what it produced is recorded here."
    />

    <template v-else>
      <!-- The single most blocking thing, restated as a sentence. Not a fourth
           widget: it is the top-ranked item of the two registers below. -->
      <section v-if="next" class="brief" :data-kind="next.kind">
        <span class="mark"><i :class="next.kind === 'open_point' ? 'pi pi-exclamation-circle' : 'pi pi-play'" /></span>
        <div class="txt">
          <strong>{{ next.kind === 'open_point' ? next.message : next.headline }}</strong>
          <span v-if="next.kind === 'stage'">Nothing is blocking it.</span>
        </div>
        <Button
          v-if="next.kind === 'open_point'"
          :label="next.action"
          size="small"
          @click="openPoint(next)"
        />
        <Button
          v-else
          label="Start"
          icon="pi pi-play"
          size="small"
          :loading="starting === next.capability"
          @click="start(next)"
        />
      </section>

      <header class="summary">
        <div class="stat">
          <strong>{{ duration(totals?.elapsed_ms ?? null) }}</strong>
          <span>of assistant time</span>
        </div>
        <div class="stat">
          <strong>{{ totals?.work_products ?? 0 }}</strong>
          <span>work products filed</span>
        </div>
        <p class="span">
          {{ totalLine }}<template v-if="quietRuns">
            · <span class="quiet">{{ plural(quietRuns, 'run') }} filed nothing</span></template>
        </p>
      </header>

      <ol class="ledger">
        <li class="head" aria-hidden="true">
          <span>Time</span><span></span><span>Filed</span><span>What it did</span><span class="r">Took</span>
        </li>

        <template v-for="(entry, index) in entries" :key="entry.id">
          <li v-if="dayBreak(index)" class="daybreak"><span>{{ dayBreak(index) }}</span></li>
          <li class="row" :data-status="entry.status">
            <span class="tm">{{ clock(entry.at) }}</span>
            <span class="gut"><i /></span>

            <span class="made">
              <component
                :is="destinationOf(entry) ? RouterLink : 'span'"
                v-if="entry.filed"
                :to="destinationOf(entry) ? nav.to(destinationOf(entry)!) : undefined"
                class="card"
                :class="{ linked: !!destinationOf(entry) }"
              >
                <i :class="icon(entry.filed.label)" aria-hidden="true" />
                <span class="mt">
                  <b>{{ entry.filed.label }}</b>
                  <em v-if="size(entry)">{{ size(entry) }}</em>
                </span>
              </component>
              <span v-else class="none">—</span>
            </span>

            <span class="say">
              <b class="ttl">{{ entry.headline }}</b>
              <span class="dsc">{{ entry.summary }}</span>

              <ul v-if="entry.highlights.length" class="hl">
                <li v-for="item in entry.highlights" :key="`${item.label}:${item.detail}`" :data-severity="item.severity">
                  <b>{{ item.label }}</b><span>{{ item.detail }}</span>
                </li>
              </ul>

              <!-- What this stage left open behind it. -->
              <button
                v-for="point in entry.open_points"
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
                v-if="attemptNote(entry)"
                type="button"
                class="tries"
                :aria-expanded="expanded.has(entry.id)"
                @click="toggle(entry)"
              >
                <i :class="expanded.has(entry.id) ? 'pi pi-chevron-down' : 'pi pi-chevron-right'" aria-hidden="true" />
                {{ attemptNote(entry) }}
              </button>
              <ol v-if="expanded.has(entry.id)" class="attempts">
                <li v-for="attempt in entry.attempts" :key="attempt.run_id">
                  <span class="at">{{ when(attempt.at) }}</span>
                  <span class="st" :data-status="attempt.run_status">{{ attempt.run_status.replaceAll('_', ' ') }}</span>
                  <span class="el">{{ duration(attempt.elapsed_ms) }}</span>
                </li>
              </ol>
            </span>

            <span class="took">{{ duration(entry.elapsed_ms) }}</span>
          </li>
        </template>

        <!-- Debts whose stage never filed have no row to sit on, and still
             have to be said. -->
        <li v-for="point in orphaned" :key="point.key" class="row orphan">
          <span class="tm"></span>
          <span class="gut"><i /></span>
          <span class="made"><span class="none">—</span></span>
          <span class="say">
            <button type="button" class="open" @click="openPoint(point)">
              <i class="pi pi-exclamation-triangle" aria-hidden="true" />
              <span class="ot">{{ point.message }}</span>
              <span class="oa">{{ point.action }}<i class="pi pi-arrow-right" aria-hidden="true" /></span>
            </button>
          </span>
          <span class="took"></span>
        </li>

        <!-- The ledger continues past the present into entries not yet written. -->
        <template v-if="pending.length">
          <li class="nowline">
            <span class="lab">Now</span>
            <span class="hr" />
            <span class="lab">{{ pendingNote }}</span>
          </li>
          <li
            v-for="stage in pending"
            :key="stage.id"
            class="row ghost"
            :class="{ lead: stage.capability === leadStage }"
          >
            <span class="tm">—</span>
            <span class="gut"><i /></span>
            <span class="made">
              <span class="card">
                <i :class="icon(stage.filed.label)" aria-hidden="true" />
                <span class="mt"><b>{{ stage.filed.label }}</b><em>not yet</em></span>
              </span>
            </span>
            <span class="say">
              <b class="ttl">{{ stage.headline }}</b>
              <span v-if="stage.blocked_reason" class="dsc">{{ stage.blocked_reason }}</span>
            </span>
            <span class="took">
              <Button
                v-if="stage.runnable"
                label="Run"
                size="small"
                :severity="stage.capability === leadStage ? undefined : 'secondary'"
                :outlined="stage.capability !== leadStage"
                :loading="starting === stage.capability"
                @click="start(stage)"
              />
              <span v-else class="waits">waits</span>
            </span>
          </li>
        </template>
      </ol>
    </template>
  </div>
</template>

<style scoped>
.record { display: flex; flex-direction: column; min-height: 0; }
.loading { display: grid; place-content: center; gap: .4rem; padding: 3rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

/* --- the one thing to do next ------------------------------------------- */
.brief {
  display: flex;
  align-items: center;
  gap: .75rem;
  margin-bottom: .6rem;
  padding: .75rem .9rem;
  border: 1px solid var(--aw-warn-line);
  border-radius: var(--aw-radius-surface);
  background: var(--aw-warn-soft);
}
.brief[data-kind='stage'] { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.brief .mark {
  display: grid; place-items: center; flex: 0 0 auto;
  width: 1.75rem; height: 1.75rem; border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-warn-ink); font-size: var(--aw-text-sm);
}
.brief[data-kind='stage'] .mark { color: var(--aw-teal); }
.brief .txt { flex: 1; min-width: 0; display: grid; gap: .1rem; }
.brief strong { color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600; line-height: 1.35; }
.brief span { color: var(--aw-ink-soft); font-size: var(--aw-text-xs); }

/* --- the headline claim -------------------------------------------------- */
.summary {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: .4rem 2rem;
  margin-bottom: var(--aw-section-gap); padding: .9rem 1rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel);
}
.stat { display: flex; align-items: baseline; gap: .45rem; }
.stat strong { font-size: var(--aw-text-2xl); font-weight: 600; line-height: 1; color: var(--aw-ink-strong); font-variant-numeric: tabular-nums; }
.stat span { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.span { flex: 1; margin: 0; text-align: right; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.quiet { color: var(--aw-muted-strong); }

/* --- the ledger ---------------------------------------------------------- */
.ledger { margin: 0; padding: 0; list-style: none; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); overflow: hidden; }

.head, .row {
  display: grid;
  grid-template-columns: 3.4rem 1rem 13rem minmax(0, 1fr) 4.5rem;
  gap: 0 .6rem;
  align-items: start;
  padding: .7rem 1rem;
}
.head {
  padding-block: .5rem; border-bottom: 1px solid var(--aw-border); background: var(--aw-raised);
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700;
  letter-spacing: .08em; text-transform: uppercase;
}
.head .r { text-align: right; }
.row + .row { border-top: 1px solid var(--aw-border); }

.daybreak {
  padding: .35rem 1rem; border-top: 1px solid var(--aw-border); background: var(--aw-raised);
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700;
  letter-spacing: .07em; text-transform: uppercase;
}

.tm { padding-top: .15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums; }
.took { padding-top: .15rem; text-align: right; color: var(--aw-muted); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* the connecting spine. The gutter stretches or the line reaches 46px down a
   row that can run to 200. */
.gut { position: relative; align-self: stretch; display: flex; justify-content: center; padding-top: .35rem; }
.gut i { z-index: 1; width: .5rem; height: .5rem; border-radius: 50%; background: var(--aw-teal); box-shadow: 0 0 0 2px var(--aw-panel); }
.gut::before { content: ""; position: absolute; top: .8rem; bottom: -1.9rem; left: 50%; width: 1px; background: var(--aw-border-strong); transform: translateX(-.5px); }
.row:last-child .gut::before { display: none; }
.row[data-status="completed_with_issues"] .gut i,
.row[data-status="needs_review"] .gut i { background: var(--aw-warn); }

.made { min-width: 0; }
.card {
  display: flex; align-items: flex-start; gap: .4rem; padding: .4rem .5rem;
  border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-control);
  background: var(--aw-teal-soft); color: var(--aw-teal-strong); text-decoration: none;
}
.card.linked:hover { border-color: var(--aw-teal); background: var(--aw-panel); }
.card.linked:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.card > i { padding-top: .1rem; font-size: var(--aw-text-xs); }
.mt { display: grid; gap: .1rem; min-width: 0; }
.mt b { font-size: var(--aw-text-xs); font-weight: 600; line-height: 1.3; }
.mt em { color: var(--aw-teal); font-size: var(--aw-text-2xs); font-style: normal; font-variant-numeric: tabular-nums; }
.none { display: block; padding-top: .3rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }

.say { display: grid; gap: .2rem; min-width: 0; justify-items: start; }
.ttl { font-size: var(--aw-text-base); font-weight: 600; line-height: 1.3; color: var(--aw-ink-strong); }
.dsc { max-width: 68ch; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.5; }

.hl { display: grid; gap: .25rem; margin: .3rem 0 0; padding: 0; list-style: none; }
.hl li { display: grid; gap: .05rem; padding-left: .6rem; border-left: 2px solid var(--aw-warn-line); }
.hl li[data-severity="error"] { border-left-color: var(--aw-danger-line); }
.hl b { color: var(--aw-warn-ink); font-size: var(--aw-text-xs); font-weight: 600; line-height: 1.35; }
.hl li[data-severity="error"] b { color: var(--aw-danger-ink); }
.hl span { max-width: 64ch; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); line-height: 1.4; }

/* an open point hanging off the row that created it */
.open {
  display: flex; align-items: center; gap: .45rem; width: 100%; max-width: 46rem;
  margin-top: .3rem; padding: .35rem .5rem;
  border: 0; border-left: 2px solid var(--aw-warn-line); border-radius: 0 var(--aw-radius-control) var(--aw-radius-control) 0;
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font: inherit; font-size: var(--aw-text-xs); text-align: left; cursor: pointer;
}
.open:hover { background: var(--aw-panel); border-left-color: var(--aw-warn); }
.open:focus-visible { outline: 2px solid var(--aw-warn); outline-offset: 1px; }
.open > i { font-size: var(--aw-text-2xs); }
.open .ot { flex: 1; min-width: 0; line-height: 1.4; }
.open .oa { display: inline-flex; align-items: center; gap: .25rem; flex: 0 0 auto; font-weight: 600; }
.open .oa i { font-size: var(--aw-text-2xs); }

.tries {
  display: inline-flex; align-items: center; gap: .3rem; margin-top: .2rem; padding: .1rem 0;
  border: 0; background: transparent; color: var(--aw-muted);
  font: inherit; font-size: var(--aw-text-2xs); cursor: pointer;
}
.tries:hover { color: var(--aw-teal); }
.tries:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; border-radius: 2px; }
.tries i { font-size: var(--aw-text-2xs); }

.attempts { display: grid; gap: .2rem; width: 100%; margin: .3rem 0 0; padding: .4rem .55rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); list-style: none; }
.attempts li { display: flex; align-items: baseline; gap: .6rem; font-size: var(--aw-text-2xs); font-variant-numeric: tabular-nums; }
.attempts .at { min-width: 8rem; color: var(--aw-ink-soft); }
.attempts .st { flex: 1; color: var(--aw-muted); }
.attempts .st[data-status="cancelled"], .attempts .st[data-status="failed"] { color: var(--aw-warn-ink); }
.attempts .el { color: var(--aw-muted); }

/* --- the now line and the phantom tail ---------------------------------- */
.nowline {
  display: flex; align-items: center; gap: .6rem;
  padding: .4rem 1rem; border-top: 1px solid var(--aw-border); background: var(--aw-canvas);
}
.nowline .lab { color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.nowline .hr { flex: 1; height: 1px; background: repeating-linear-gradient(90deg, var(--aw-border-strong) 0 4px, transparent 4px 8px); }

.row.ghost { background: var(--aw-canvas); }
.row.ghost .gut i { width: .55rem; height: .55rem; background: var(--aw-panel); border: 1.5px dashed var(--aw-border-strong); box-shadow: none; }
.row.ghost .gut::before { background: repeating-linear-gradient(180deg, var(--aw-border-strong) 0 3px, transparent 3px 7px); }
.row.ghost .card { border-style: dashed; border-color: var(--aw-border-strong); background: transparent; color: var(--aw-muted); }
.row.ghost .mt em { color: var(--aw-muted); }
.row.ghost .ttl { color: var(--aw-muted); }
.row.ghost .took { padding-top: 0; }
.waits { font-style: italic; }

/* Only the first runnable stage is drawn as a call to action: a tail of six
   buttons is a menu, not a next step. */
.row.ghost.lead { background: var(--aw-teal-soft); }
.row.ghost.lead .gut i { border-style: solid; border-color: var(--aw-teal); }
.row.ghost.lead .card { border-style: solid; border-color: var(--aw-teal); background: var(--aw-panel); color: var(--aw-teal-strong); }
.row.ghost.lead .ttl { color: var(--aw-ink-strong); }

.row.orphan .gut i { background: var(--aw-warn); }

@container (max-width: 56rem) {
  .head { display: none; }
  .row { grid-template-columns: 3.4rem 1rem minmax(0, 1fr); }
  .row .took { grid-column: 3; text-align: left; }
  .row .made { grid-column: 3; }
  .row .say { grid-column: 3; }
}
</style>
