<script setup lang="ts">
import { computed } from 'vue'

import type { AgentRunStatus, AgentStage, AgentTaskStatus } from '../../types'

// The Codex-style live plan: stages with their tasks, statuses updating as
// SSE events land. Result refs surface as small "what this produced" chips.
const props = defineProps<{
  stages: AgentStage[]
  runStatus?: AgentRunStatus
  runError?: string | null
}>()

const statusIcon: Record<AgentTaskStatus, string> = {
  queued: 'pi pi-circle',
  running: 'pi pi-spin pi-spinner',
  awaiting_approval: 'pi pi-pause-circle',
  completed: 'pi pi-check-circle',
  skipped: 'pi pi-minus-circle',
  failed: 'pi pi-times-circle',
  cancelled: 'pi pi-ban',
}

const visibleStages = computed(() => props.stages
  .filter((stage) => stage.tasks.length)
  .map((stage) => ({
    ...stage,
    tasks: stage.tasks.map((task) => {
      // Older persisted runs can contain a running task if the process or LLM
      // transport failed before task cleanup.  Never show a spinner beneath a
      // terminal run status.
      if (props.runStatus === 'failed' && task.status === 'running') {
        return { ...task, status: 'failed' as const, error: task.error || props.runError || 'The run ended before this task completed.' }
      }
      if (props.runStatus === 'cancelled' && task.status === 'running') {
        return { ...task, status: 'cancelled' as const }
      }
      return task
    }),
  })))

// Singular / plural noun per artifact kind, so a task that produced many refs
// collapses to one informative pill ("12 risks") instead of a row of identical
// kind labels ("rcm rcm rcm …").
const refNouns: Record<string, [string, string]> = {
  rcm: ['risk', 'risks'],
  procedure: ['procedure', 'procedures'],
  finding: ['finding', 'findings'],
  join: ['join', 'joins'],
  ruleset: ['ruleset', 'rulesets'],
  analysis: ['analysis', 'analyses'],
  tile: ['tile', 'tiles'],
  check: ['check', 'checks'],
}

function summarizeRefs(refs: string[]): string[] {
  const counts = new Map<string, number>()
  for (const ref of refs) {
    const kind = ref.split(':')[0]
    // The task title already says "planning context" / "audit planning
    // memorandum", so a "planning" pill adds nothing — skip it.
    if (kind === 'planning') continue
    counts.set(kind, (counts.get(kind) ?? 0) + 1)
  }
  return [...counts].map(([kind, count]) => {
    const [one, many] = refNouns[kind] ?? [kind, `${kind}s`]
    return `${count} ${count === 1 ? one : many}`
  })
}
</script>

<template>
  <div class="tasks">
    <div v-for="stage in visibleStages" :key="stage.id" class="stage">
      <p class="stage-title">{{ stage.title }}</p>
      <div
        v-for="task in stage.tasks"
        :key="task.id"
        class="task"
        :class="task.status"
      >
        <i :class="statusIcon[task.status] ?? 'pi pi-circle'" />
        <div class="task-body">
          <span class="task-title">{{ task.title }}</span>
          <small v-if="task.status === 'failed' && task.error" class="task-error">
            {{ task.error }}
          </small>
          <small
            v-else-if="task.detail && (task.status === 'queued' || task.status === 'running')"
            class="muted"
          >
            {{ task.detail }}
          </small>
          <small v-for="note in task.context_notes ?? []" :key="note" class="muted context-note">
            {{ note }}
          </small>
          <div v-if="summarizeRefs(task.result_refs).length" class="refs">
            <span v-for="ref in summarizeRefs(task.result_refs)" :key="ref" class="ref">
              {{ ref }}
            </span>
          </div>
        </div>
      </div>
    </div>
    <p v-if="!visibleStages.length" class="muted empty">
      The plan appears here once discovery finishes.
    </p>
  </div>
</template>

<style scoped>
.tasks { display: flex; flex-direction: column; gap: 0.55rem; }
.stage-title {
  margin: 0 0 0.25rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--p-surface-500);
}
.task {
  display: flex;
  gap: 0.55rem;
  align-items: flex-start;
  padding: 0.4rem 0.5rem;
  border-radius: 6px;
}
.task > i { margin-top: 0.15rem; font-size: 0.85rem; color: var(--p-surface-400); }
.task.running { background: var(--p-primary-50); }
.task.running > i { color: var(--p-primary-500); }
.task.awaiting_approval { background: #fff7e6; }
.task.awaiting_approval > i { color: var(--p-amber-500); }
.task.completed > i { color: var(--aw-teal, #0b625c); }
.task.failed > i { color: var(--p-red-500); }
.task.cancelled > i { color: var(--p-surface-500); }
.task.cancelled { opacity: 0.65; }
.task.skipped { opacity: 0.65; }
.task-body { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
.task-title { font-size: 0.85rem; line-height: 1.3; }
.task.completed .task-title { color: var(--p-surface-600); }
.task-error { color: var(--p-red-500); font-size: 0.72rem; }
.muted { color: var(--p-surface-500); font-size: 0.72rem; }
.context-note { line-height: 1.35; word-break: break-word; }
.refs { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem; }
.ref {
  font-size: 0.65rem;
  padding: 0.05rem 0.4rem;
  border-radius: 999px;
  background: var(--p-surface-100);
  color: var(--p-surface-600);
}
.empty { padding: 0.5rem; }
</style>
