<script setup lang="ts">
import { computed } from 'vue'
import type { AgentRunStatus, WorkflowStage, WorkflowUnitStatus } from '../../types'

const props = defineProps<{ stages: WorkflowStage[]; runStatus?: AgentRunStatus; runError?: string | null }>()

const icons: Record<WorkflowUnitStatus, string> = {
  queued: 'pi pi-circle', running: 'pi pi-spin pi-spinner', succeeded: 'pi pi-check-circle',
  failed: 'pi pi-times-circle', blocked: 'pi pi-lock', awaiting_input: 'pi pi-pause-circle',
  awaiting_confirmation: 'pi pi-user-edit', conflict: 'pi pi-exclamation-triangle',
  skipped: 'pi pi-minus-circle', cancelled: 'pi pi-ban',
}
const stages = computed(() => props.stages.map(stage => ({
  ...stage,
  units: stage.units.map(unit => {
    if (props.runStatus === 'failed' && unit.status === 'running') return { ...unit, status: 'failed' as const, error: unit.error || props.runError || null }
    if (props.runStatus === 'cancelled' && unit.status === 'running') return { ...unit, status: 'cancelled' as const }
    return unit
  }),
})))
function counts(stage: WorkflowStage) {
  const complete = stage.units.filter(unit => ['succeeded', 'skipped'].includes(unit.status)).length
  return `${complete}/${stage.units.length}`
}
</script>

<template>
  <div class="workflow">
    <section v-for="stage in stages" :key="stage.id" class="stage">
      <header><strong>{{ stage.title }}</strong><span>{{ counts(stage) }} · {{ stage.status.replaceAll('_', ' ') }}</span></header>
      <p v-if="stage.readiness_before?.reasons?.length" class="reason">{{ stage.readiness_before.reasons.join('; ') }}</p>
      <div v-for="unit in stage.units" :key="unit.id" class="unit" :class="unit.status">
        <i :class="icons[unit.status]" />
        <span><b>{{ unit.title }}</b><small>{{ unit.status.replaceAll('_', ' ') }}<template v-if="unit.attempts"> · attempt {{ unit.attempts }}</template></small><small v-if="unit.error" class="error">{{ unit.error }}</small></span>
      </div>
      <p v-if="!stage.units.length" class="reason">Units will be resolved after prerequisites complete.</p>
    </section>
  </div>
</template>

<style scoped>
.workflow{display:grid;gap:.65rem}.stage{display:grid;gap:.3rem}.stage header{display:flex;justify-content:space-between;gap:.5rem;font-size:.7rem;text-transform:uppercase;color:var(--p-surface-500)}.stage header strong{color:var(--p-surface-700)}.unit{display:grid;grid-template-columns:1rem minmax(0,1fr);align-items:center;gap:.5rem;padding:.4rem .5rem;border-radius:6px}.unit.running{background:var(--p-primary-50)}.unit>i{display:grid;place-items:center;width:1rem;height:1rem;line-height:1;color:var(--p-surface-400)}.unit.succeeded>i{color:var(--aw-teal)}.unit.failed>i,.unit.conflict>i{color:var(--p-red-500)}.unit.blocked>i,.unit.awaiting_input>i,.unit.awaiting_confirmation>i{color:var(--p-amber-500)}.unit span{display:grid;min-width:0}.unit b{font-size:.8rem;font-weight:500}.unit small,.reason{margin:0;font-size:.68rem;color:var(--p-surface-500)}.unit .error{color:var(--p-red-500);white-space:normal}
</style>
