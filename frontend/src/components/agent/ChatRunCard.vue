<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api } from '../../api'
import type { AgentDecision, AgentInteraction, AgentRun, AssistantRunProjection } from '../../types'
import AgentActionList from './AgentActionList.vue'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'
import AgentSummary from './AgentSummary.vue'
import AgentTaskList from './AgentTaskList.vue'

const props = defineProps<{ workspaceId: string; projection: AssistantRunProjection }>()
const emit = defineEmits<{ changed: [] }>()
const expanded = ref(false)
const run = ref<AgentRun | null>(null)
const busy = ref(false)
const active = computed(() => ['queued','interpreting','discovering','planning','executing','awaiting_approval','awaiting_input','verifying','summarizing','paused','interrupted'].includes(props.projection.status))
const severity: Record<string, 'success'|'warn'|'danger'|'secondary'|'info'> = { completed:'success',completed_with_issues:'warn',failed:'danger',cancelled:'secondary',paused:'secondary',interrupted:'warn',awaiting_approval:'warn',awaiting_input:'warn' }

async function load() {
  run.value = await api.get<AgentRun>(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}`)
}
async function toggle() { expanded.value = !expanded.value; if (expanded.value) await load() }
async function control(action: 'pause'|'resume'|'cancel') {
  busy.value = true
  try { await api.post(`/api/workspaces/${props.workspaceId}/agent/runs/${props.projection.run_id}/${action}`); await load(); emit('changed') }
  finally { busy.value = false }
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
      <span class="identity"><strong>{{ projection.title }}</strong><small>{{ projection.mode === 'permission' ? 'Ask before changes' : 'Auto' }} · {{ projection.current_activity }}</small></span>
      <Tag :value="projection.status.replaceAll('_',' ')" :severity="severity[projection.status] ?? 'info'" />
      <i :class="expanded ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" />
    </button>
    <div class="progress"><span>{{ projection.task_counts.completed }}/{{ projection.task_counts.total }} complete</span><span v-if="projection.task_counts.failed">{{ projection.task_counts.failed }} failed</span><span v-if="projection.task_counts.blocked">{{ projection.task_counts.blocked }} blocked</span><strong v-if="projection.pending_attention">Needs attention</strong></div>
    <p v-if="projection.summary_line" class="summary-line">{{ projection.summary_line }}</p>
    <div v-if="expanded" class="run-detail">
      <div v-if="run" class="controls">
        <Button v-if="active && !['paused','interrupted'].includes(run.status)" icon="pi pi-pause" text size="small" :disabled="busy" @click="control('pause')" />
        <Button v-if="['paused','interrupted'].includes(run.status)" icon="pi pi-play" text size="small" :disabled="busy" @click="control('resume')" />
        <Button v-if="active" icon="pi pi-stop-circle" text size="small" severity="danger" :disabled="busy" @click="control('cancel')" />
        <span class="grow" /><Button icon="pi pi-refresh" text size="small" :loading="busy" @click="load" />
      </div>
      <template v-if="run">
        <AgentApprovalCard v-for="approval in run.approvals.filter(item => item.status === 'pending')" :key="approval.id" :approval="approval" :busy="busy" @decide="decide(approval.id, $event)" />
        <AgentInteractionCard v-for="interaction in (run.interactions ?? []).filter(item => item.status === 'pending')" :key="interaction.id" :interaction="interaction" :busy="busy" :workspaceId="workspaceId" :runId="run.id" @respond="respond(interaction, $event)" />
        <AgentActionList v-if="run.actions?.length" :actions="run.actions" :runStatus="run.status" />
        <AgentTaskList v-else :stages="run.plan.stages" :runStatus="run.status" :runError="run.error" />
        <details v-if="run.warnings.length"><summary>{{ run.warnings.length }} warning(s)</summary><ul><li v-for="warning in run.warnings" :key="warning">{{ warning }}</li></ul></details>
        <AgentSummary v-if="run.summary_markdown" :markdown="run.summary_markdown" :findings="run.findings" :workspaceId="workspaceId" :runId="run.id" />
      </template>
    </div>
  </article>
</template>

<style scoped>
.run-card{border:1px solid var(--aw-border);border-radius:10px;background:var(--aw-canvas);overflow:hidden}.run-card.attention{border-color:var(--p-amber-400);box-shadow:0 0 0 2px rgb(245 158 11/8%)}.run-head{display:flex;align-items:center;gap:.55rem;width:100%;padding:.65rem;border:0;background:transparent;text-align:left;cursor:pointer;color:inherit}.icon{display:grid;place-items:center;width:1.9rem;height:1.9rem;border-radius:7px;background:var(--aw-teal-soft);color:var(--aw-teal)}.identity{display:grid;gap:.12rem;min-width:0;flex:1}.identity strong,.identity small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.identity strong{font-size:.78rem}.identity small,.progress,.summary-line{font-size:.68rem;color:var(--aw-muted)}.progress{display:flex;gap:.65rem;padding:0 .65rem .55rem}.progress strong{color:var(--p-amber-700)}.summary-line{margin:0;padding:0 .65rem .55rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.run-detail{padding:.65rem;border-top:1px solid var(--aw-border);background:var(--p-surface-0)}.controls{display:flex;align-items:center}.grow{flex:1}.run-detail details{font-size:.72rem;color:var(--aw-muted)}
</style>
