<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { FAILURE_STATUSES, useAgentRun } from '../../composables/useAgentRun'
import { useAssistantChat } from '../../composables/useAssistantChat'
import type { AgentRun, WorkflowStage, WorkflowUnit, WorkflowUnitStatus } from '../../types'
import AgentActionList from './AgentActionList.vue'
import AgentTaskList from './AgentTaskList.vue'
import { capabilityClause } from './capabilityLabels'
import { plural, verb } from '../../format'

/**
 * The run's capability graph, at the pace it is being worked.
 *
 * This is the real plan, not a progress bar: stages come from
 * `AgentWorkflow.stages`, which the runtime materialized from the dependency
 * closure.
 *
 * The plan shown is the one the open conversation asked for. The agent store
 * holds a single workspace-wide run, so reading the rail from it meant every
 * chat showed whichever run happened to be newest — switching chats left the
 * previous chat's plan standing next to the wrong transcript. The run is
 * resolved from the active chat instead, and the live record is used whenever
 * that is the run the agent store is streaming, so liveness costs nothing.
 *
 * The rail is also the only place the stage-by-stage detail lives. The
 * transcript used to carry a second, expandable copy of the same graph, which
 * meant the plan was stated twice on one screen and the reading order of the
 * conversation was broken by a control that unfolded a wall of unit rows.
 *
 * Not every run has a graph. Action and intake runs, and a workflow run whose
 * route is still being decided, get a plain statement of what is happening
 * instead of an empty timeline — followed by whatever list of work that engine
 * does keep.
 */

const props = defineProps<{ workspaceId: string; overlay?: boolean }>()
const agent = useAgentRun(props.workspaceId)
const chats = useAssistantChat(props.workspaceId)

const ACTIVE_STATUSES = new Set([
  'queued', 'interpreting', 'executing', 'awaiting_approval',
  'awaiting_input', 'verifying', 'paused', 'interrupted',
])

// The chat's own runs, newest first. A run still working outranks a newer one
// that has already ended: that is the one the auditor is watching.
const target = computed(() => {
  const runs = chats.state.chat?.runs ?? []
  return runs.find(item => ACTIVE_STATUSES.has(item.status)) ?? runs[0] ?? null
})

// A run the agent store is not streaming has to be fetched, and refetched when
// the chat projection says it moved on. Chat refreshes are already driven by
// the run's own events, so this stays in step without a second subscription.
const fetched = ref<AgentRun | null>(null)
watch(
  () => {
    const item = target.value
    if (!item || agent.state.run?.id === item.run_id) return ''
    return [item.run_id, item.status, item.activity_revision ?? 0,
      item.task_counts.completed, item.task_counts.failed].join(':')
  },
  async key => {
    if (!key) return
    const runId = key.split(':')[0]
    try { fetched.value = await api.get<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${runId}`) }
    catch { /* transient — the next chat refresh retries */ }
  },
  { immediate: true },
)

const run = computed<AgentRun | null>(() => {
  const runId = target.value?.run_id
  if (!runId) return null
  if (agent.state.run?.id === runId) return agent.state.run
  return fetched.value?.id === runId ? fetched.value : null
})
const workflow = computed(() => run.value?.workflow ?? null)

const SETTLED = ['succeeded', 'skipped']

type SpineState = 'done' | 'running' | 'gate' | 'failed' | 'queued'

const STATE_BY_STATUS: Record<string, SpineState> = {
  succeeded: 'done', skipped: 'done',
  running: 'running',
  blocked: 'gate', review_required: 'gate',
  failed: 'failed', cancelled: 'failed',
  queued: 'queued',
}

const UNIT_ICONS: Record<WorkflowUnitStatus, string> = {
  queued: 'pi pi-circle', running: 'pi pi-spin pi-spinner', succeeded: 'pi pi-check-circle',
  failed: 'pi pi-times-circle', blocked: 'pi pi-lock', awaiting_input: 'pi pi-pause-circle',
  awaiting_confirmation: 'pi pi-user-edit', conflict: 'pi pi-exclamation-triangle',
  skipped: 'pi pi-minus-circle', cancelled: 'pi pi-ban',
}

/**
 * A stage is only really running while the run is. Once the run has failed,
 * been cancelled, interrupted, or paused, a stage still marked `running`
 * stopped part-way — showing it as in-flight claims progress that is not
 * happening.
 */
const HALTED_RUN_STATE: Record<string, SpineState> = {
  failed: 'failed',
  // The run finished; a stage still marked running stopped part-way with it.
  completed_with_failures: 'failed',
  cancelled: 'failed',
  interrupted: 'gate',
  paused: 'gate',
}

const stages = computed<WorkflowStage[]>(() => workflow.value?.stages ?? [])

function elapsed(stage: WorkflowStage) {
  const started = stage.started_at ? Date.parse(stage.started_at) : NaN
  const finished = stage.finished_at ? Date.parse(stage.finished_at) : NaN
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return ''
  const seconds = Math.max(0, Math.round((finished - started) / 1000))
  if (seconds < 1) return ''
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

const rows = computed(() => {
  const status = run.value?.status ?? ''
  const halted = HALTED_RUN_STATE[status]
  const runError = run.value?.error ?? null
  return stages.value.map(stage => {
    const mapped = STATE_BY_STATUS[stage.status] ?? 'queued'
    const state = mapped === 'running' && halted ? halted : mapped
    // A unit left marked `running` by a run that died never finished; saying so
    // is the whole reason someone opens the stage.
    //
    // Pipeline-backed stages run their units sorted by id (workflow_runner's
    // `_run_pipeline_units`), not in the declaration order `stage.units`
    // arrives in. Sorting here too keeps the rail's row order matching the
    // order units actually flip to running/succeeded.
    const units: WorkflowUnit[] = [...stage.units]
      .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
      .map(unit => {
        if ((FAILURE_STATUSES as string[]).includes(status) && unit.status === 'running') {
          return { ...unit, status: 'failed' as const, error: unit.error || runError }
        }
        if (status === 'cancelled' && unit.status === 'running') return { ...unit, status: 'cancelled' as const }
        return unit
      })
    const total = units.length
    const settled = units.filter(unit => SETTLED.includes(unit.status)).length
    const attention = units.filter(unit =>
      ['failed', 'blocked', 'awaiting_input', 'awaiting_confirmation', 'conflict'].includes(unit.status)).length
    const blockingOn = stage.readiness_before?.blocking_on ?? []
    return {
      id: stage.id,
      capability: stage.capability,
      title: stage.title,
      state,
      units,
      // A single-unit stage counting "1/1" is noise; only fan-out is worth a count.
      count: total > 1 ? `${settled}/${total}` : '',
      attention,
      elapsed: elapsed(stage),
      waitingOn: blockingOn.length ? capabilityClause(blockingOn) : '',
      // Readiness is what the runtime saw *before* the stage began, so it is
      // only current for a stage that has not started. On a stage that already
      // ran it is stale, and it contradicts what the run went on to do.
      reason: stage.started_at ? '' : stage.readiness_before?.reasons?.[0] ?? '',
    }
  })
})

type SpineRow = (typeof rows.value)[number]

// The stage in flight and anything holding a person up open themselves; a stage
// the reader opened or closed by hand keeps that choice regardless of what the
// run does next, so a fast-moving run cannot collapse something being read.
const overrides = ref<Record<string, boolean>>({})
// Stage ids are capability names, so they repeat across runs; carrying an
// override into a different run would open a stage nobody opened.
watch(() => run.value?.id, () => { overrides.value = {} })
function isOpen(row: SpineRow) {
  return overrides.value[row.id] ?? (row.state === 'running' || row.attention > 0)
}
function toggle(row: SpineRow) {
  overrides.value = { ...overrides.value, [row.id]: !isOpen(row) }
}

const warnings = computed(() => run.value?.warnings ?? [])

// Runs without a capability graph still have work worth listing: the action
// engine keeps typed actions, and older/plan-based runs keep staged tasks.
const actions = computed(() => run.value?.actions ?? [])
const planStages = computed(() => (run.value?.plan?.stages ?? []).filter(stage => stage.tasks.length))

const reused = computed(() => {
  const ids = workflow.value?.reused_capabilities ?? []
  return ids.length ? capabilityClause(ids) : ''
})

/**
 * What to say when there is no capability graph to draw. Every branch names
 * something true about the run rather than falling through to a blank rail.
 */
const note = computed(() => {
  const value = run.value
  if (!value) {
    return target.value
      ? { title: 'Loading the plan', detail: 'Fetching the run this chat started.' }
      : { title: 'No run yet', detail: 'Ask the agent to start, and its plan appears here.' }
  }
  if (value.route?.status === 'pending') {
    return { title: 'Working out the plan', detail: 'Deciding how to handle this request.' }
  }
  if (value.engine === 'intake') {
    return { title: 'Importing files', detail: 'Staging and routing an audit folder. This run has no capability graph.' }
  }
  if (value.engine === 'action') {
    return {
      title: 'Direct action',
      detail: value.route?.action_intent
        ? `Running ${value.route.action_intent.replaceAll('_', ' ')} rather than a workflow.`
        : 'A single action, not a workflow.',
    }
  }
  if (workflow.value && !stages.value.length) {
    return { title: 'Nothing to schedule', detail: 'Every requested outcome was already up to date.' }
  }
  return { title: 'No plan yet', detail: 'This run has not resolved a workflow.' }
})
</script>

<template>
  <aside class="plan-spine" :class="{ overlay }">
    <p class="rail-label">Plan</p>

    <ol v-if="rows.length" class="spine">
      <li v-for="row in rows" :key="row.id" class="spine-row" :data-state="row.state" :class="{ open: isOpen(row) }">
        <button class="row-head" :aria-expanded="isOpen(row)" @click="toggle(row)">
          <i class="dot" aria-hidden="true" />
          <span class="cap" :title="row.title">{{ row.title }}</span>
          <span v-if="row.attention" class="attention" :title="`${plural(row.attention, 'item')} ${verb(row.attention)} attention`">{{ row.attention }}</span>
          <span v-else-if="row.count" class="n">{{ row.count }}</span>
          <i class="chevron" :class="isOpen(row) ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" aria-hidden="true" />
        </button>
        <p v-if="row.waitingOn" class="detail">Waiting on {{ row.waitingOn }}.</p>
        <p v-else-if="row.reason" class="detail">{{ row.reason }}</p>

        <!-- The stage-by-stage detail the transcript used to unfold: every unit,
             its status, its attempts and the error it stopped on. -->
        <div v-if="isOpen(row)" class="units">
          <div v-for="unit in row.units" :key="unit.id" class="unit" :class="unit.status">
            <i :class="UNIT_ICONS[unit.status] ?? 'pi pi-circle'" aria-hidden="true" />
            <span>
              <b>{{ unit.title }}</b>
              <small>{{ unit.status.replaceAll('_', ' ') }}<template v-if="unit.attempts"> · attempt {{ unit.attempts }}</template></small>
              <small v-if="unit.error" class="error">{{ unit.error }}</small>
            </span>
          </div>
          <p v-if="!row.units.length" class="unit-note">Units will be resolved after prerequisites complete.</p>
          <p class="unit-meta">
            <code>{{ row.capability }}</code>
            <span v-if="row.elapsed">{{ row.elapsed }}</span>
          </p>
        </div>
      </li>
    </ol>

    <template v-else>
      <div class="spine-note">
        <strong>{{ note.title }}</strong>
        <p>{{ note.detail }}</p>
      </div>
      <!-- A run with no graph still has to say what it did, and this rail is now
           the only place that detail lives. -->
      <div v-if="actions.length || planStages.length" class="fallback-list">
        <AgentActionList v-if="actions.length" :actions="actions" :runStatus="run?.status" />
        <AgentTaskList v-else :stages="planStages" :runStatus="run?.status" :runError="run?.error" />
      </div>
    </template>

    <details v-if="warnings.length" class="warnings">
      <summary>{{ plural(warnings.length, 'warning') }}</summary>
      <ul><li v-for="warning in warnings" :key="warning">{{ warning }}</li></ul>
    </details>

    <!-- Why the agent skipped work is as much a part of the plan as what it ran. -->
    <p v-if="reused" class="reused"><i class="pi pi-history" /> Reused {{ reused }}.</p>
  </aside>
</template>

<style scoped>
.plan-spine {
  display: flex;
  flex-direction: column;
  margin-top: 0.9rem;
  padding-top: 0.9rem;
  border-top: 1px solid var(--aw-border);
}
.rail-label {
  margin: 0 0.15rem 0.6rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-weight: 700;
}

.spine { margin: 0; padding: 0; list-style: none; }
.spine-row {
  position: relative;
  display: grid;
  padding: 0.34rem 0.45rem 0.34rem 1.5rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-ink-soft);
  font-size: var(--aw-text-sm);
}
/* One continuous rule behind the dots, broken at the ends. */
.spine-row::before {
  content: "";
  position: absolute;
  left: 0.6rem;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--aw-border-strong);
}
.spine-row:first-child::before { top: 50%; }
.spine-row:last-child::before { bottom: 50%; }
.spine-row.open:last-child::before { bottom: auto; height: 0.9rem; }

.row-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 0 0.35rem;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.dot {
  position: absolute;
  left: 0.3rem;
  top: 0.55rem;
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 50%;
  background: var(--aw-panel);
  box-shadow: inset 0 0 0 1.5px var(--aw-border-strong);
}
.cap { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.n { color: var(--aw-muted); font-size: var(--aw-text-xs); font-variant-numeric: tabular-nums; }
.chevron { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.attention {
  min-width: 1.15rem;
  padding: 0.02rem 0.3rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-warn-soft);
  color: var(--aw-warn);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
  text-align: center;
}
.detail {
  margin: 0.1rem 0 0.15rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  line-height: 1.35;
}

.units { display: grid; gap: 0.15rem; margin: 0.3rem 0 0.15rem; }
.unit {
  display: grid;
  grid-template-columns: 0.85rem minmax(0, 1fr);
  align-items: start;
  gap: 0.35rem;
  padding: 0.25rem 0.3rem;
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
.unit > i {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 0.884rem; /* matches .unit b line-height so the icon sits on the first text line */
  color: var(--aw-muted-strong);
  font-size: var(--aw-text-2xs);
}
.unit.succeeded > i { color: var(--aw-ok); }
.unit.running > i { color: var(--aw-teal); }
.unit.failed > i, .unit.conflict > i { color: var(--aw-danger); }
.unit.blocked > i, .unit.awaiting_input > i, .unit.awaiting_confirmation > i { color: var(--aw-warn); }
.unit.skipped, .unit.cancelled { opacity: 0.7; }
.unit span { display: grid; min-width: 0; gap: 0.05rem; }
.unit b { color: var(--aw-ink-soft); font-size: var(--aw-text-xs); font-weight: 500; line-height: 1.3; overflow-wrap: anywhere; }
.unit small { color: var(--aw-muted); font-size: var(--aw-text-2xs); line-height: 1.3; }
.unit small.error { color: var(--aw-danger); overflow-wrap: anywhere; }
.unit-note { margin: 0.1rem 0; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.unit-meta {
  display: flex;
  justify-content: space-between;
  gap: 0.4rem;
  margin: 0.15rem 0 0;
  color: var(--aw-muted-strong);
  font-size: var(--aw-text-2xs);
}
.unit-meta code { min-width: 0; font-family: var(--aw-font-mono); overflow-wrap: anywhere; }

.spine-row[data-state='done'] .dot { background: var(--aw-ok); box-shadow: inset 0 0 0 1.5px var(--aw-ok); }
.spine-row[data-state='running'] {
  background: var(--aw-panel);
  color: var(--aw-teal);
  font-weight: 600;
  box-shadow: var(--aw-shadow-sm);
}
.spine-row[data-state='running'] .dot { background: var(--aw-teal-600); box-shadow: 0 0 0 3px rgb(13 148 136 / 20%); }
.spine-row[data-state='gate'] { background: var(--aw-warn-soft); color: var(--aw-warn-ink); font-weight: 600; }
.spine-row[data-state='gate'] .dot { background: var(--aw-warn); box-shadow: 0 0 0 3px rgb(180 83 9 / 18%); }
.spine-row[data-state='failed'] { color: var(--aw-danger); }
.spine-row[data-state='failed'] .dot { background: var(--aw-danger); box-shadow: inset 0 0 0 1.5px var(--aw-danger); }
.spine-row[data-state='gate'] .detail,
.spine-row[data-state='running'] .detail { color: inherit; opacity: 0.85; }

.spine-note {
  padding: 0.6rem 0.65rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
.spine-note strong { display: block; font-size: var(--aw-text-sm); }
.spine-note p { margin: 0.2rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.4; }
.fallback-list { margin-top: 0.6rem; font-size: var(--aw-text-xs); }

.warnings { margin: 0.7rem 0.15rem 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.warnings summary { cursor: pointer; }
.warnings ul { margin: 0.25rem 0 0; padding-left: 1rem; line-height: 1.4; }

.reused {
  display: flex;
  gap: 0.35rem;
  margin: 0.7rem 0.15rem 0;
  padding-top: 0.6rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  line-height: 1.4;
}
.reused i { padding-top: 0.15rem; font-size: var(--aw-text-2xs); }

.plan-spine.overlay{width:100%;min-height:auto;max-height:70vh;margin-top:0;padding-top:0;border-top:0}
</style>
