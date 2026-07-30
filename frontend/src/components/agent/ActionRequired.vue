<script setup lang="ts">
import { computed, ref } from 'vue'
import Tag from 'primevue/tag'

import { useAgentRun } from '../../composables/useAgentRun'
import type { AgentApproval, AgentDecision, AgentInteraction } from '../../types'
import AgentApprovalCard from './AgentApprovalCard.vue'
import AgentInteractionCard from './AgentInteractionCard.vue'

/** The Console's actionable queue; recorded audit results stay on their owning screens. */
const props = defineProps<{ workspaceId: string }>()
const agent = useAgentRun(props.workspaceId)
const busy = ref(false)

const run = computed(() => agent.state.run)
const approvals = computed(() => (run.value?.approvals ?? []).filter(item => item.status === 'pending'))
const interactions = computed(() => (run.value?.interactions ?? []).filter(item => item.status === 'pending'))
const blockers = computed(() => {
  const stopped = new Set(['blocked', 'awaiting_input', 'awaiting_confirmation', 'conflict', 'failed'])
  const groups = new Map<string, { id: string; stage: string; count: number; failed: boolean }>()
  for (const stage of run.value?.workflow?.stages ?? []) {
    for (const unit of stage.units) {
      if (!stopped.has(unit.status)) continue
      const failed = unit.status === 'failed'
      const key = `${stage.id}:${failed ? 'failed' : 'waiting'}`
      const group = groups.get(key) ?? { id: key, stage: stage.title, count: 0, failed }
      group.count += 1
      groups.set(key, group)
    }
  }
  return [...groups.values()].map(item => ({
    ...item,
    title: item.failed
      ? `${item.count} error${item.count === 1 ? '' : 's'} during ${item.stage}`
      : `${item.count} item${item.count === 1 ? '' : 's'} waiting during ${item.stage}`,
    detail: item.failed
      ? 'Review the Console transcript to retry or adjust the request.'
      : 'The agent is waiting for the next step before it can continue.',
  }))
})
const total = computed(() => approvals.value.length + interactions.value.length + blockers.value.length)

async function decide(approval: AgentApproval, decisions: AgentDecision[]) {
  busy.value = true
  try { await agent.decide(approval.id, decisions) }
  finally { busy.value = false }
}

async function respond(interaction: AgentInteraction, response: Record<string, unknown>) {
  busy.value = true
  try { await agent.respond(interaction, response) }
  finally { busy.value = false }
}
</script>

<template>
  <section class="action-required" :class="{ clear: !total }">
    <div class="head">
      <div>
        <p class="rail-label">Action required</p>
        <p class="summary">{{ total ? 'Review these run issues and continue where needed.' : 'Nothing is holding the current run.' }}</p>
      </div>
      <Tag v-if="total" :value="String(total)" severity="warn" />
      <i v-else class="pi pi-check-circle" aria-hidden="true" />
    </div>

    <AgentApprovalCard
      v-for="approval in approvals"
      :key="approval.id"
      :approval="approval"
      :busy="busy"
      @decide="decide(approval, $event)"
    />
    <AgentInteractionCard
      v-for="interaction in interactions"
      :key="interaction.id"
      :interaction="interaction"
      :busy="busy"
      :workspaceId="workspaceId"
      :runId="run?.id ?? ''"
      @respond="respond(interaction, $event)"
    />
    <div v-for="item in blockers" :key="item.id" class="blocker" :class="{ failed: item.failed }">
      <i :class="item.failed ? 'pi pi-times-circle' : 'pi pi-pause-circle'" aria-hidden="true" />
      <span><strong>{{ item.title }}</strong><small>{{ item.detail }}</small></span>
    </div>
  </section>
</template>

<style scoped>
.action-required { display: grid; gap: .5rem; padding-bottom: .8rem; border-bottom: 1px solid var(--aw-border); }
.head { display: flex; align-items: flex-start; gap: .5rem; }
.head > div { min-width: 0; flex: 1; }
.rail-label { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
.summary { margin: .18rem 0 0; color: #50617c; font-size: .7rem; line-height: 1.35; }
.head > i { padding-top: .08rem; color: var(--aw-ok); }
.blocker { display: flex; gap: .45rem; padding: .5rem; border: 1px solid var(--p-amber-300); border-radius: var(--aw-radius-sm); background: var(--p-amber-50); }
.blocker.failed { border-color: var(--p-red-300); background: var(--p-red-50); }
.blocker > i { padding-top: .08rem; color: var(--p-amber-600); font-size: .8rem; }
.blocker.failed > i { color: var(--p-red-500); }
.blocker span { display: grid; gap: .12rem; min-width: 0; }
.blocker strong { font-size: .74rem; line-height: 1.3; }
.blocker small { color: var(--aw-muted); font-size: .68rem; line-height: 1.35; }
.clear { padding-bottom: .65rem; }
.action-required :deep(.approval), .action-required :deep(.interaction) { font-size: .82rem; }
</style>
