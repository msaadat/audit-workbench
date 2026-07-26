<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AgentRunStatus, WorkflowStage, WorkflowUnitStatus } from '../../types'

const props = defineProps<{ stages: WorkflowStage[]; runStatus?: AgentRunStatus; runError?: string | null }>()

const icons: Record<WorkflowUnitStatus, string> = {
  queued: 'pi pi-circle', running: 'pi pi-spin pi-spinner', succeeded: 'pi pi-check-circle',
  failed: 'pi pi-times-circle', blocked: 'pi pi-lock', awaiting_input: 'pi pi-pause-circle',
  awaiting_confirmation: 'pi pi-user-edit', conflict: 'pi pi-exclamation-triangle',
  skipped: 'pi pi-minus-circle', cancelled: 'pi pi-ban',
}
const SETTLED = ['succeeded', 'skipped']
// Stages the reader opened by hand stay open regardless of what the run does
// next, so a fast-moving run cannot collapse something being read.
const pinned = ref<Set<string>>(new Set())

const stages = computed(() => props.stages.map(stage => ({
  ...stage,
  units: stage.units.map(unit => {
    if (props.runStatus === 'failed' && unit.status === 'running') return { ...unit, status: 'failed' as const, error: unit.error || props.runError || null }
    if (props.runStatus === 'cancelled' && unit.status === 'running') return { ...unit, status: 'cancelled' as const }
    return unit
  }),
})))

// A plan is worth watching when it is revealed at the pace it is worked: the
// stage in flight and anything needing a person are shown in full, finished
// stages shrink to one line, and stages not started yet stay a single hint.
const current = computed(() => stages.value.find(stage => stage.status === 'running')?.id ?? '')
function detailed(stage: { id: string; status: string; units: Array<{ status: string }> }) {
  if (pinned.value.has(stage.id)) return true
  if (stage.status === 'queued') return false
  return stage.id === current.value
    || stage.status !== 'succeeded'
    || stage.units.some(unit => !SETTLED.includes(unit.status))
}
function toggle(stageId: string) {
  const next = new Set(pinned.value)
  next.has(stageId) ? next.delete(stageId) : next.add(stageId)
  pinned.value = next
}
function counts(stage: WorkflowStage) {
  const complete = stage.units.filter(unit => SETTLED.includes(unit.status)).length
  return `${complete}/${stage.units.length}`
}
function elapsed(stage: WorkflowStage) {
  const started = stage.started_at ? Date.parse(stage.started_at) : NaN
  const finished = stage.finished_at ? Date.parse(stage.finished_at) : NaN
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return ''
  const seconds = Math.max(0, Math.round((finished - started) / 1000))
  if (seconds < 1) return ''
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}
const upcoming = computed(() => stages.value.filter(stage => stage.status === 'queued'))
</script>

<template>
  <div class="workflow">
    <template v-for="stage in stages" :key="stage.id">
      <section v-if="stage.status !== 'queued'" class="stage" :class="{ current: stage.id === current }">
        <button class="stage-head" @click="toggle(stage.id)">
          <i :class="stage.status === 'running' ? 'pi pi-spin pi-spinner' : stage.status === 'succeeded' ? 'pi pi-check-circle' : 'pi pi-exclamation-circle'" />
          <strong>{{ stage.title }}</strong>
          <span>{{ counts(stage) }}<template v-if="elapsed(stage)"> · {{ elapsed(stage) }}</template></span>
        </button>
        <template v-if="detailed(stage)">
          <p v-if="stage.readiness_before?.reasons?.length" class="reason">{{ stage.readiness_before.reasons.join('; ') }}</p>
          <div v-for="unit in stage.units" :key="unit.id" class="unit" :class="unit.status">
            <i :class="icons[unit.status]" />
            <span><b>{{ unit.title }}</b><small>{{ unit.status.replaceAll('_', ' ') }}<template v-if="unit.attempts"> · attempt {{ unit.attempts }}</template></small><small v-if="unit.error" class="error">{{ unit.error }}</small></span>
          </div>
          <p v-if="!stage.units.length" class="reason">Units will be resolved after prerequisites complete.</p>
        </template>
      </section>
    </template>
    <!-- Work not started yet is one line of intent, not a pre-ticked checklist. -->
    <p v-if="upcoming.length" class="upcoming">
      <i class="pi pi-arrow-right" /> then {{ upcoming.map(stage => stage.title.toLowerCase()).join(' → ') }}
    </p>
  </div>
</template>

<style scoped>
.workflow{display:grid;gap:.5rem}
.stage{display:grid;gap:.25rem}
.stage-head{display:flex;align-items:center;gap:.4rem;width:100%;padding:.1rem 0;border:0;background:transparent;color:var(--p-surface-500);font-size:.7rem;text-align:left;cursor:pointer}
.stage-head strong{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--p-surface-700);text-transform:uppercase;letter-spacing:.04em;font-size:.68rem}
.stage-head>i{font-size:.7rem;color:var(--p-surface-400)}
.stage.current .stage-head>i{color:var(--aw-teal)}
.unit{display:grid;grid-template-columns:1rem minmax(0,1fr);align-items:center;gap:.5rem;padding:.4rem .5rem;border-radius:6px}
.unit.running{background:var(--p-primary-50)}
.unit>i{display:grid;place-items:center;width:1rem;height:1rem;line-height:1;color:var(--p-surface-400)}
.unit.succeeded>i{color:var(--aw-teal)}
.unit.failed>i,.unit.conflict>i{color:var(--p-red-500)}
.unit.blocked>i,.unit.awaiting_input>i,.unit.awaiting_confirmation>i{color:var(--p-amber-500)}
.unit span{display:grid;min-width:0}
.unit b{font-size:.8rem;font-weight:500}
.unit small,.reason,.upcoming{margin:0;font-size:.68rem;color:var(--p-surface-500)}
.unit .error{color:var(--p-red-500);white-space:normal}
.upcoming{display:flex;align-items:center;gap:.35rem}
.upcoming i{font-size:.6rem}
</style>
