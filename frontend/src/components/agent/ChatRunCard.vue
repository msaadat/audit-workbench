<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import type { AgentDecision, AgentInteraction, AgentRun, AssistantRunProjection } from '../../types'
import AgentActionList from './AgentActionList.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import AgentSummary from './AgentSummary.vue'
import AgentTaskList from './AgentTaskList.vue'
import AgentWorkflowList from './AgentWorkflowList.vue'

// showAttention: pending interactions/approvals normally render as their own
// transcript items; only a foreign run card (another chat's active run, not
// projected into this transcript) shows them inline.
const props = defineProps<{ workspaceId: string; projection: AssistantRunProjection; showAttention?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const agent = useAgentRun(props.workspaceId)
const expanded = ref(false)
const run = ref<AgentRun | null>(null)
const busy = ref(false)
const clock = ref(Date.now())
let clockTimer: number | undefined
const active = computed(() => ['queued','interpreting','executing','awaiting_approval','awaiting_input','verifying','paused','interrupted'].includes(props.projection.status))
const severity: Record<string, 'success'|'warn'|'danger'|'secondary'|'info'> = { completed:'success',completed_with_open_items:'warn',completed_with_issues:'warn',failed:'danger',cancelled:'secondary',paused:'secondary',interrupted:'warn',awaiting_approval:'warn',awaiting_input:'warn' }

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
const modelElapsedMs = computed(() => {
  const started = props.projection.activity?.model_started_at
  return started ? Math.max(0, clock.value - Date.parse(started)) : null
})
const activityText = computed(() => {
  const base = props.projection.current_activity
  if (props.projection.activity?.waiting_on !== 'model') return base
  const elapsed = duration(modelElapsedMs.value)
  const waiting = (modelElapsedMs.value ?? 0) >= 60_000 ? 'still waiting for model' : 'waiting for model'
  return `${base} · ${waiting}${elapsed ? ` ${elapsed}` : ''}`
})
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

async function load() {
  run.value = await api.get<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}`)
}
async function toggle() { expanded.value = !expanded.value; if (expanded.value) await load() }
// The projection updates with every chat refresh; keep the expanded detail in
// step so the header tag and the task list never show contradictory states.
watch(
  () => [props.projection.status, props.projection.activity_revision ?? 0, props.projection.task_counts.completed, props.projection.task_counts.failed, props.projection.pending_attention].join(':'),
  () => { if (expanded.value) void load().catch(() => undefined) },
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
  <article class="run-card" :class="{ attention: projection.pending_attention }">
    <button class="run-head" @click="toggle">
      <span class="icon"><i class="pi pi-sparkles" /></span>
      <span class="identity"><strong>{{ projection.title }}</strong><small>{{ projection.mode === 'permission' ? 'Ask first' : 'Auto' }} · {{ activityText }}</small></span>
      <Tag :value="projection.status.replaceAll('_',' ')" :severity="severity[projection.status] ?? 'info'" />
      <i :class="expanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
    </button>
    <div class="progress"><span>{{ projection.task_counts.completed }}/{{ projection.task_counts.total }} complete</span><span v-if="projection.task_counts.failed">{{ projection.task_counts.failed }} failed</span><span v-if="projection.task_counts.blocked">{{ projection.task_counts.blocked }} blocked</span><span v-if="runElapsedMs != null">{{ duration(runElapsedMs) }}</span><strong v-if="projection.pending_attention">Needs attention</strong></div>
    <p v-if="projection.summary_line" class="summary-line">{{ projection.summary_line }}</p>
    <div v-if="expanded" class="run-detail">
      <div v-if="run" class="controls">
        <Button v-if="active && !['paused','interrupted'].includes(run.status)" icon="pi pi-pause" text size="small" :disabled="busy" @click="control('pause')" />
        <Button v-if="['paused','interrupted'].includes(run.status)" icon="pi pi-play" text size="small" :disabled="busy" @click="control('resume')" />
        <Button v-if="active" icon="pi pi-stop-circle" text size="small" severity="danger" :disabled="busy" @click="control('cancel')" />
        <Button v-if="run.status === 'failed'" label="Retry run" icon="pi pi-refresh" size="small" :loading="busy" @click="retryRun" />
        <Button v-if="run.status === 'completed_with_open_items' && run.workflow?.next_outcomes.length" label="Continue audit" icon="pi pi-arrow-right" size="small" :loading="busy" @click="continueAudit" />
        <span class="grow" /><Button icon="pi pi-refresh" text size="small" :loading="busy" @click="load" />
      </div>
      <template v-if="run">
        <template v-if="showAttention">
          <AgentApprovalCard v-for="approval in run.approvals.filter(item => item.status === 'pending')" :key="approval.id" :approval="approval" :busy="busy" @decide="decide(approval.id, $event)" />
          <AgentInteractionCard v-for="interaction in (run.interactions ?? []).filter(item => item.status === 'pending')" :key="interaction.id" :interaction="interaction" :busy="busy" :workspaceId="workspaceId" :runId="run.id" @respond="respond(interaction, $event)" />
        </template>
        <p v-if="run.workflow?.workflow_explanation" class="workflow-explanation">{{ run.workflow.workflow_explanation }}</p>
        <AgentWorkflowList v-if="run.workflow?.definition === 'audit_workflow_v2'" :stages="run.workflow.stages" :runStatus="run.status" :runError="run.error" />
        <AgentActionList v-else-if="run.actions?.length" :actions="run.actions" :runStatus="run.status" />
        <AgentTaskList v-else :stages="run.plan.stages" :runStatus="run.status" :runError="run.error" />
        <details v-if="run.warnings.length"><summary>{{ run.warnings.length }} warning(s)</summary><ul><li v-for="warning in run.warnings" :key="warning">{{ warning }}</li></ul></details>
        <AgentSummary v-if="run.summary_markdown" :markdown="run.summary_markdown" :findings="run.findings" :auditOutcome="run.audit_outcome" :workspaceId="workspaceId" :runId="run.id" />
      </template>
    </div>
  </article>
</template>

<style scoped>
.run-card{border:1px solid var(--aw-border);border-radius:10px;background:var(--aw-canvas);overflow:hidden}.run-card.attention{border-color:var(--p-amber-400);box-shadow:0 0 0 2px rgb(245 158 11/8%)}.run-head{display:flex;align-items:center;gap:.55rem;width:100%;padding:.65rem;border:0;background:transparent;text-align:left;cursor:pointer;color:inherit}.icon{display:grid;place-items:center;width:1.9rem;height:1.9rem;border-radius:7px;background:var(--aw-teal-soft);color:var(--aw-teal)}.identity{display:grid;gap:.12rem;min-width:0;flex:1}.identity strong,.identity small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.identity strong{font-size:.78rem}.identity small,.progress,.summary-line{font-size:.68rem;color:var(--aw-muted)}.progress{display:flex;gap:.65rem;padding:0 .65rem .55rem}.progress strong{color:var(--p-amber-700)}.summary-line{margin:0;padding:0 .65rem .55rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.run-detail{padding:.65rem;border-top:1px solid var(--aw-border);background:var(--p-surface-0)}.controls{display:flex;align-items:center}.grow{flex:1}.run-detail details{font-size:.72rem;color:var(--aw-muted)}.run-detail summary{cursor:pointer}
.workflow-explanation{margin:.35rem 0 .7rem;padding:.55rem;border-radius:6px;background:var(--p-surface-50);font-size:.72rem;line-height:1.4;color:var(--p-surface-600)}
</style>
