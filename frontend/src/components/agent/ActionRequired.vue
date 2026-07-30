<script setup lang="ts">
import { computed, ref } from 'vue'
import Tag from 'primevue/tag'

import { useAgentRun } from '../../composables/useAgentRun'
import type { AgentApproval, AgentDecision, AgentInteraction, WorkflowUnit } from '../../types'
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
  const seen = new Set<string>()
  const items: Array<{ id: string; title: string; detail: string; failed: boolean }> = []
  for (const stage of run.value?.workflow?.stages ?? []) {
    for (const unit of stage.units) {
      if (!stopped.has(unit.status) || seen.has(unit.id)) continue
      seen.add(unit.id)
      items.push(blocker(stage.title, unit))
    }
  }
  return items
})
const total = computed(() => approvals.value.length + interactions.value.length + blockers.value.length)

function blocker(stageTitle: string, unit: WorkflowUnit) {
  return {
    id: unit.id,
    title: unit.title || stageTitle,
    detail: unit.error || `${stageTitle} is waiting for the next step.`,
    failed: unit.status === 'failed',
  }
}

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
        <p class="summary">{{ total ? 'The agent needs your input to continue.' : 'Nothing is holding the current run.' }}</p>
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
