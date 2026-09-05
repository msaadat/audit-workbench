<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { useAgentRun } from '../../composables/useAgentRun'
import { useAssistantChat } from '../../composables/useAssistantChat'
import PlanSpine from './PlanSpine.vue'

/**
 * One line of plan while a run works, and the whole plan when you ask for it.
 *
 * The drawer's answer was a `Plan` button that opened `PlanSpine` in a modal —
 * a dialog over a sidecar, so reading the plan meant covering the transcript
 * the plan was about. The strip says the one thing that changes minute to
 * minute, and unfolds in place for the rest.
 *
 * It exists only while a run of the open chat is working. Once the run settles
 * the strip goes, and the receipt at the end of the transcript holds the plan.
 */

const props = defineProps<{ workspaceId: string }>()
const agent = useAgentRun(props.workspaceId)
const chats = useAssistantChat(props.workspaceId)
const open = ref(false)

const ACTIVE = new Set([
  'queued', 'interpreting', 'executing', 'awaiting_approval',
  'awaiting_input', 'verifying', 'paused', 'interrupted',
])

/** The run this chat is watching, which is the working one where there is one. */
const working = computed(() => {
  const runs = chats.state.chat?.runs ?? []
  return runs.find(item => ACTIVE.has(item.status)) ?? null
})
/** The live record when the store is streaming it, else the chat's projection. */
const run = computed(() => {
  const live = agent.state.run
  return live && live.id === working.value?.run_id ? live : null
})

const stages = computed(() => run.value?.workflow?.stages ?? [])
const position = computed(() => {
  const list = stages.value
  if (!list.length) return ''
  const index = list.findIndex(stage => stage.status === 'running')
  const settled = list.filter(stage => ['succeeded', 'skipped', 'failed'].includes(stage.status)).length
  return `stage ${Math.max(1, index >= 0 ? index + 1 : settled)} of ${list.length}`
})
const stage = computed(() => {
  const list = stages.value
  return list.find(item => item.status === 'running')?.title
    ?? agent.state.stream?.label
    ?? 'Working'
})

/** How long the run has been going, in the units a person reads it in. */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`
}
// A clock the strip owns, so the elapsed time moves without the run emitting.
const tick = ref(0)
let timer: number | undefined
watch(working, value => {
  window.clearInterval(timer)
  if (!value) return
  const started = new Date(value.started ?? value.created_at ?? '').valueOf()
  const update = () => { tick.value = Number.isNaN(started) ? 0 : Date.now() - started }
  update()
  timer = window.setInterval(update, 1000)
}, { immediate: true })
const elapsedText = computed(() => (tick.value ? formatElapsed(Math.round(tick.value / 1000)) : ''))
</script>

<template>
  <div v-if="working" class="plan-strip">
    <div class="line">
      <i class="pi pi-spin pi-spinner" aria-hidden="true" />
      <span class="stage">{{ stage }}</span>
      <span class="position aw-figure">
        <template v-if="position">{{ position }}</template>
        <template v-if="position && elapsedText"> · </template>{{ elapsedText }}
      </span>
      <button type="button" class="more" :aria-expanded="open" @click="open = !open">
        Plan<i class="pi" :class="open ? 'pi-chevron-up' : 'pi-chevron-down'" aria-hidden="true" />
      </button>
    </div>
    <!-- In place, not in a modal: the plan qualifies the transcript under it. -->
    <div v-if="open" class="unfolded"><PlanSpine :workspaceId="workspaceId" card /></div>
  </div>
</template>

<style scoped>
.plan-strip { flex: none; border-bottom: 1px solid var(--aw-border); background: var(--aw-raised); }
.line { display: flex; align-items: center; gap: .5rem; min-height: 2.25rem; padding: 0 .75rem; }
.line .pi-spinner { color: var(--aw-teal); font-size: .7rem; }
.stage { flex: none; color: var(--aw-ink-strong); font-size: var(--aw-text-xs); font-weight: 600; }
.position { flex: 1; min-width: 0; overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }
.more { display: inline-flex; align-items: center; gap: .25rem; padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: var(--aw-text-2xs); font-weight: 600; cursor: pointer; }
.more .pi { font-size: .55rem; }
.unfolded { max-height: 18rem; overflow-y: auto; border-top: 1px solid var(--aw-border); }
</style>
