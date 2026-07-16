<script setup lang="ts">
import { computed } from 'vue'
import Tag from 'primevue/tag'
import type { AgentAction, AgentActionStatus, AgentRunStatus } from '../../types'

const props = defineProps<{
  actions: AgentAction[]
  runStatus?: AgentRunStatus
}>()
const counts = computed(() => {
  const values: Record<string, number> = {}
  for (const action of props.actions) values[action.status] = (values[action.status] ?? 0) + 1
  return values
})
const severity: Partial<Record<AgentActionStatus, string>> = {
  succeeded: 'success', failed: 'danger', blocked: 'danger', running: 'info',
  awaiting_input: 'warn', awaiting_confirmation: 'warn', skipped: 'secondary', cancelled: 'secondary',
}
const emptyMessage = computed(() => {
  if (props.runStatus === 'failed') return 'The run ended before an action plan was created.'
  if (props.runStatus === 'cancelled') return 'The run was cancelled before an action plan was created.'
  if (props.runStatus === 'completed' || props.runStatus === 'completed_with_issues') {
    return 'No command actions were needed.'
  }
  return 'The command is being interpreted.'
})
</script>

<template>
  <div class="graph">
    <div v-if="actions.length" class="counts">
      <span>{{ actions.length }} actions</span>
      <span v-if="counts.ready">{{ counts.ready }} ready</span>
      <span v-if="counts.blocked">{{ counts.blocked }} blocked</span>
      <span v-if="counts.succeeded">{{ counts.succeeded }} committed</span>
    </div>
    <p v-else class="muted">{{ emptyMessage }}</p>
    <div v-for="action in actions" :key="action.id" class="action" :class="action.status">
      <i
        :class="action.status === 'succeeded' ? 'pi pi-check-circle'
          : action.status === 'running' ? 'pi pi-spin pi-spinner'
          : action.status === 'failed' || action.status === 'blocked' ? 'pi pi-exclamation-circle'
          : 'pi pi-circle'"
      />
      <div class="body">
        <strong>{{ action.type.replaceAll('_', ' ') }}</strong>
        <small v-if="action.resolution?.title">{{ action.resolution.title }}</small>
        <small v-if="action.result_refs.length">{{ action.result_refs.join(' · ') }}</small>
        <small v-if="action.error" class="error">{{ action.error }}</small>
      </div>
      <Tag :value="action.status.replaceAll('_', ' ')" :severity="severity[action.status] ?? 'secondary'" />
    </div>
  </div>
</template>

<style scoped>
.graph { display: grid; gap: .45rem; }
.counts { display: flex; flex-wrap: wrap; gap: .65rem; color: var(--p-text-muted-color); font-size: .75rem; }
.action { display: flex; align-items: flex-start; gap: .55rem; padding: .55rem; border: 1px solid var(--aw-border, #d5dde7); border-radius: .45rem; }
.action > i { margin-top: .15rem; color: var(--p-text-muted-color); }
.action.succeeded > i { color: var(--p-green-600); }
.action.failed > i, .action.blocked > i, .error { color: var(--p-red-600); }
.action.running { border-color: var(--aw-teal, #0b625c); }
.body { min-width: 0; flex: 1; display: grid; gap: .15rem; }
.body strong { font-size: .82rem; font-weight: 600; }
.body small { overflow-wrap: anywhere; color: var(--p-text-muted-color); }
.muted { margin: 0; color: var(--p-text-muted-color); font-size: .8rem; }
</style>
