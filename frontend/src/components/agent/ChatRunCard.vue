<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Tag from 'primevue/tag'

import { api } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type { AgentDecision, AgentInteraction, AgentRun, AssistantRunProjection } from '../../types'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentBlockerCard from './AgentBlockerCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'

// showAttention: pending interactions/approvals normally render as their own
// transcript items; only a foreign run card (another chat's active run, not
// projected into this transcript) shows them inline.
const props = defineProps<{ workspaceId: string; projection: AssistantRunProjection; showAttention?: boolean }>()
const emit = defineEmits<{ changed: []; command: [string] }>()
const agent = useAgentRun(props.workspaceId)
// The plan — stages, units, the error each one stopped on — belongs to the left
// rail, which shows it without a click. What is left here is the receipt: one
// strip of identity and status plus the blockers that still need answering.
const run = ref<AgentRun | null>(null)
const busy = ref(false)
const clock = ref(Date.now())
let clockTimer: number | undefined
const active = computed(() => ['queued','interpreting','executing','awaiting_approval','awaiting_input','verifying','paused','interrupted'].includes(props.projection.status))
const working = computed(() => ['queued','interpreting','executing','verifying'].includes(props.projection.status))
function duration(value: number | null | undefined) {
  if (value == null) return ''
  const seconds = Math.max(0, Math.floor(value / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`
}
const runElapsedMs = computed(() => {
  if (props.projection.duration_ms != null && !active.value) return props.projection.duration_ms
  const started = props.projection.started ? Date.parse(props.projection.started) : NaN
  return Number.isFinite(started) ? clock.value - started : null
})
// Status, counts and elapsed on one line. While the run is working the phase
// and its timer are stated directly above the card, so repeating them here just
// gave the same wait two labels and two clocks.
const subline = computed(() => {
  const label = props.projection.status_label || props.projection.status.replaceAll('_', ' ')
  const parts = [label]
  const activity = (props.projection.current_activity || '').trim()
  if (!working.value && activity && activity !== props.projection.status.replaceAll('_', ' ')) {
    parts.push(activity)
  }
  const counts = props.projection.task_counts
  // The status label already carries the verb, so the counts are just counts.
  if (counts.total) parts.push(`${counts.completed}/${counts.total}`)
  if (counts.failed) parts.push(`${counts.failed} failed`)
  if (!working.value && runElapsedMs.value != null) parts.push(duration(runElapsedMs.value))
  return parts.filter(Boolean).join(' · ')
})
// A blocker is already stated in full on the run's closing message; the card
// repeats only the ones the auditor can still act on.
const blockers = computed(() => props.projection.blockers ?? [])
const openBlockers = computed(() => blockers.value.filter(item => item.severity !== 'review'))

// Any finished run that named remaining outcomes can be continued, and both
// failure statuses can be retried. `completed_with_failures` means the run
// committed real work and some units did not settle, so continuing it picks up
// only the remainder.
const canContinue = computed(
  () => !active.value && (props.projection.next_outcomes?.length ?? 0) > 0,
)
const canRetry = computed(
  () => ['failed', 'completed_with_failures'].includes(props.projection.status),
)

function syncClock() {
  if (active.value && !clockTimer) {
    clockTimer = window.setInterval(() => { clock.value = Date.now() }, 1000)
  } else if (!active.value && clockTimer) {
    window.clearInterval(clockTimer)
    clockTimer = undefined
  }
}
onMounted(syncClock)
watch(active, syncClock)
onUnmounted(() => { if (clockTimer) window.clearInterval(clockTimer) })

// The full record is fetched only for a run owned by another chat, whose
// attention items are not projected here.
const needsDetail = computed(() => props.showAttention === true)
async function load() {
  run.value = await api.get<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}`)
}
// The projection updates with every chat refresh; keep the detail in step so
// attention items remain current.
watch(
  () => [needsDetail.value, props.projection.status, props.projection.activity_revision ?? 0, props.projection.task_counts.completed, props.projection.task_counts.failed, props.projection.pending_attention].join(':'),
  () => { if (needsDetail.value) void load().catch(() => undefined) },
  { immediate: true },
)
async function control(action: 'pause'|'resume'|'cancel') {
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/${action}`); await load(); emit('changed') }
  finally { busy.value = false }
}
async function retryRun() {
  busy.value = true
  try {
    const retried = await api.post<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/retry`)
    await agent.openRun(retried.id)
    emit('changed')
  } finally { busy.value = false }
}
async function continueAudit() {
  busy.value = true
  try {
    const next = await api.post<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/continue`)
    await agent.openRun(next.id)
    emit('changed')
  } finally { busy.value = false }
}
async function decide(approvalId: string, decisions: AgentDecision[]) {
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/approvals/${approvalId}`, { decisions }); await load(); emit('changed') }
  finally { busy.value = false }
}
async function respond(interaction: AgentInteraction, response: Record<string, unknown>) {
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/interactions/${interaction.id}/respond`, response); await load(); emit('changed') }
  finally { busy.value = false }
}
</script>

<template>
  <article class="run-card" :class="{ attention: projection.pending_attention, live: active }">
    <!-- One row: identity, one status line, icon controls. Everything else the
         card used to restate is already in the narration above it. -->
    <div class="head-row">
      <span class="run-head">
        <span class="icon"><i :class="active ? 'pi pi-spin pi-spinner' : 'pi pi-sparkles'" /></span>
        <span class="identity">
          <strong>{{ projection.title }}</strong>
          <small>{{ subline }}</small>
        </span>
      </span>
      <Tag v-if="projection.status === 'failed'" :value="projection.status_label" severity="danger" />
      <Tag v-else-if="projection.status === 'completed_with_failures'" :value="projection.status_label" severity="warn" />
      <Tag v-else-if="projection.pending_attention" value="Needs you" severity="warn" />
      <template v-if="active">
        <button v-if="!['paused','interrupted'].includes(projection.status)" class="control" :disabled="busy" title="Pause" aria-label="Pause the run" @click="control('pause')"><i class="pi pi-pause" /></button>
        <button v-else class="control" :disabled="busy" title="Resume" aria-label="Resume the run" @click="control('resume')"><i class="pi pi-play" /></button>
        <button class="control danger" :disabled="busy" title="Stop" aria-label="Stop the run" @click="control('cancel')"><i class="pi pi-stop-circle" /></button>
      </template>
      <!-- Continue is offered first wherever the run named what is left: it
           resumes only the remainder, where retry replays the whole command. -->
      <button v-else-if="canContinue" class="control" :disabled="busy" title="Continue" aria-label="Continue the audit" @click="continueAudit"><i class="pi pi-arrow-right" /></button>
      <button v-else-if="canRetry" class="control" :disabled="busy" title="Retry" aria-label="Retry the run" @click="retryRun"><i class="pi pi-refresh" /></button>
    </div>

    <!-- A run owned by another chat has neither its narration nor its closing
         message projected here, so its card carries the one-line result. -->
    <p v-if="showAttention && projection.summary_line" class="summary-line">{{ projection.summary_line }}</p>

    <div v-if="openBlockers.length" class="blockers">
      <AgentBlockerCard v-for="item in openBlockers" :key="item.unit_id" :blocker="item" :busy="busy" @command="emit('command', $event)" />
    </div>

    <div v-if="needsDetail && run" class="run-detail">
      <template v-if="showAttention">
        <AgentApprovalCard v-for="approval in run.approvals.filter(item => item.status === 'pending')" :key="approval.id" :approval="approval" :busy="busy" @decide="decide(approval.id, $event)" />
        <AgentInteractionCard v-for="interaction in (run.interactions ?? []).filter(item => item.status === 'pending')" :key="interaction.id" :interaction="interaction" :busy="busy" :workspaceId="workspaceId" :runId="run.id" @respond="respond(interaction, $event)" />
      </template>
    </div>
  </article>
</template>

<style scoped>
.run-card{border:1px solid var(--aw-border);border-radius:8px;background:var(--aw-canvas);overflow:hidden}
.run-card.attention{border-color:var(--p-amber-400)}
.run-card.live{border-color:var(--aw-teal)}
.head-row{display:flex;align-items:center;gap:.3rem;padding:.35rem .45rem}
.run-head{display:flex;align-items:center;gap:.45rem;flex:1;min-width:0;padding:.1rem;text-align:left;color:inherit}
.icon{display:grid;place-items:center;width:1.4rem;height:1.4rem;flex:0 0 auto;border-radius:5px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:.66rem}
.identity{display:grid;gap:.05rem;min-width:0;flex:1}
.identity strong,.identity small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.identity strong{font-size:.74rem;font-weight:500}
.identity small,.summary-line{font-size:.66rem;color:var(--aw-muted)}
.summary-line{margin:0;padding:0 .55rem .45rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.run-detail{padding:.6rem;border-top:1px solid var(--aw-border);background:var(--p-surface-0)}
.blockers{display:grid;gap:.4rem;padding:0 .55rem .5rem}
.control{display:grid;place-items:center;width:1.5rem;height:1.5rem;flex:0 0 auto;border:0;border-radius:5px;background:transparent;color:var(--aw-muted);cursor:pointer}
.control:hover:not(:disabled){background:var(--p-surface-100);color:inherit}
.control:disabled{opacity:.4;cursor:default}
.control.danger:hover:not(:disabled){color:var(--p-red-600)}
.control i{font-size:.7rem}
.head-row :deep(.p-tag){flex:0 0 auto;padding:.1rem .35rem;font-size:.6rem}
</style>
