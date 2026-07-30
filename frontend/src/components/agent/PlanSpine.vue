<script setup lang="ts">
import { computed } from 'vue'

import { useAgentRun } from '../../composables/useAgentRun'
import type { WorkflowStage } from '../../types'
import { capabilityClause } from './capabilityLabels'

/**
 * The run's capability graph, at the pace it is being worked.
 *
 * This is the real plan, not a progress bar: stages come from
 * `AgentWorkflow.stages`, which the runtime materialized from the dependency
 * closure. Liveness is free — `useAgentRun` refetches the run on every
 * `stage_update` / `unit_update` event and the store is reactive.
 *
 * Not every run has a graph. Action and intake runs, and a workflow run whose
 * route is still being decided, get a plain statement of what is happening
 * instead of an empty timeline.
 */

const props = defineProps<{ workspaceId: string; overlay?: boolean }>()
const agent = useAgentRun(props.workspaceId)

const run = computed(() => agent.state.run)
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

/**
 * A stage is only really running while the run is. Once the run has failed,
 * been cancelled, interrupted, or paused, a stage still marked `running`
 * stopped part-way — showing it as in-flight claims progress that is not
 * happening.
 */
const HALTED_RUN_STATE: Record<string, SpineState> = {
  failed: 'failed',
  cancelled: 'failed',
  interrupted: 'gate',
  paused: 'gate',
}

const stages = computed<WorkflowStage[]>(() => workflow.value?.stages ?? [])

const rows = computed(() => {
  const halted = HALTED_RUN_STATE[run.value?.status ?? '']
  return stages.value.map(stage => {
    const mapped = STATE_BY_STATUS[stage.status] ?? 'queued'
    const state = mapped === 'running' && halted ? halted : mapped
    const total = stage.units.length
    const settled = stage.units.filter(unit => SETTLED.includes(unit.status)).length
    const attention = stage.units.filter(unit =>
      ['failed', 'blocked', 'awaiting_input', 'awaiting_confirmation', 'conflict'].includes(unit.status)).length
    const blockingOn = stage.readiness_before?.blocking_on ?? []
    return {
      id: stage.id,
      capability: stage.capability,
      title: stage.title,
      state,
      // A single-unit stage counting "1/1" is noise; only fan-out is worth a count.
      count: total > 1 ? `${settled}/${total}` : '',
      attention,
      waitingOn: blockingOn.length ? capabilityClause(blockingOn) : '',
      // Readiness is what the runtime saw *before* the stage began, so it is
      // only current for a stage that has not started. On a stage that already
      // ran it is stale, and it contradicts what the run went on to do.
      reason: stage.started_at ? '' : stage.readiness_before?.reasons?.[0] ?? '',
    }
  })
})

const currentId = computed(() => rows.value.find(row => row.state === 'running')?.id ?? '')

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
  if (!value) return { title: 'No run yet', detail: 'Ask the agent to start, and its plan appears here.' }
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
      <li v-for="row in rows" :key="row.id" class="spine-row" :data-state="row.state">
        <i class="dot" aria-hidden="true" />
        <span class="cap" :title="row.title">{{ row.title }}</span>
        <span v-if="row.attention" class="attention" :title="`${row.attention} item(s) need attention`">{{ row.attention }}</span>
        <span v-else-if="row.count" class="n">{{ row.count }}</span>
        <p v-if="row.waitingOn" class="detail">Waiting on {{ row.waitingOn }}.</p>
        <p v-else-if="row.reason" class="detail">{{ row.reason }}</p>
        <code v-if="row.id === currentId" class="capability-id">{{ row.capability }}</code>
      </li>
    </ol>

    <div v-else class="spine-note">
      <strong>{{ note.title }}</strong>
      <p>{{ note.detail }}</p>
    </div>

    <!-- Why the agent skipped work is as much a part of the plan as what it ran. -->
    <p v-if="reused" class="reused"><i class="pi pi-history" /> Reused {{ reused }}.</p>
  </aside>
</template>

<style scoped>
.plan-spine {
  flex: 0 0 14.5rem;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
  padding: 0.9rem 0.7rem;
  border-right: 1px solid var(--aw-border);
  background: var(--aw-raised);
}
.rail-label {
  margin: 0 0.15rem 0.6rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}

.spine { margin: 0; padding: 0; list-style: none; }
.spine-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 0 0.4rem;
  padding: 0.34rem 0.45rem 0.34rem 1.5rem;
  border-radius: var(--aw-radius-sm);
  color: #46587a;
  font-size: 0.76rem;
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

.dot {
  position: absolute;
  left: 0.3rem;
  top: 0.55rem;
  width: 0.55rem;
  height: 0.55rem;
  border-radius: 50%;
  background: var(--aw-panel);
  box-shadow: inset 0 0 0 1.5px #b7c5d6;
}
.cap { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.n { color: var(--aw-muted); font-size: 0.68rem; font-variant-numeric: tabular-nums; }
.attention {
  min-width: 1.15rem;
  padding: 0.02rem 0.3rem;
  border-radius: 999px;
  background: var(--aw-warn-soft);
  color: var(--aw-warn);
  font-size: 0.66rem;
  font-weight: 700;
  text-align: center;
}
.detail {
  grid-column: 1 / -1;
  margin: 0.1rem 0 0.15rem;
  color: var(--aw-muted);
  font-size: 0.68rem;
  line-height: 1.35;
}
.capability-id {
  grid-column: 1 / -1;
  color: #8195ae;
  font-family: var(--aw-font-mono);
  font-size: 0.62rem;
  word-break: break-all;
}

.spine-row[data-state='done'] .dot { background: var(--aw-ok); box-shadow: inset 0 0 0 1.5px var(--aw-ok); }
.spine-row[data-state='running'] {
  background: var(--aw-panel);
  color: var(--aw-teal);
  font-weight: 600;
  box-shadow: var(--aw-shadow-sm);
}
.spine-row[data-state='running'] .dot { background: var(--aw-teal-600); box-shadow: 0 0 0 3px rgb(13 148 136 / 20%); }
.spine-row[data-state='gate'] { background: var(--aw-warn-soft); color: #8a4308; font-weight: 600; }
.spine-row[data-state='gate'] .dot { background: var(--aw-warn); box-shadow: 0 0 0 3px rgb(180 83 9 / 18%); }
.spine-row[data-state='failed'] { color: var(--aw-danger); }
.spine-row[data-state='failed'] .dot { background: var(--aw-danger); box-shadow: inset 0 0 0 1.5px var(--aw-danger); }
.spine-row[data-state='gate'] .detail,
.spine-row[data-state='running'] .detail { color: inherit; opacity: 0.85; }

.spine-note {
  padding: 0.6rem 0.65rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-sm);
  background: var(--aw-panel);
}
.spine-note strong { display: block; font-size: 0.78rem; }
.spine-note p { margin: 0.2rem 0 0; color: var(--aw-muted); font-size: 0.7rem; line-height: 1.4; }

.reused {
  display: flex;
  gap: 0.35rem;
  margin: 0.7rem 0.15rem 0;
  padding-top: 0.6rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-muted);
  font-size: 0.68rem;
  line-height: 1.4;
}
.reused i { padding-top: 0.15rem; font-size: 0.62rem; }

@container console-body (max-width: 52rem) {
  .plan-spine:not(.overlay) { display: none; }
}

.plan-spine.overlay{width:100%;min-height:auto;max-height:70vh;border-right:0}
</style>
