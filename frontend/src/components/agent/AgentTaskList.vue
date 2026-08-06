<script setup lang="ts">
import { computed } from 'vue'

import type { AgentRunStatus, AgentStage, AgentTask, AgentTaskStatus } from '../../types'

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

function taskDuration(task: AgentTask): string {
  if (!task.started_at || !task.finished_at) return ''
  const seconds = Math.max(0, Math.floor((Date.parse(task.finished_at) - Date.parse(task.started_at)) / 1000))
  if (seconds < 1) return ''
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m${seconds % 60 ? ` ${seconds % 60}s` : ''}`
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
          <small v-if="taskDuration(task)" class="muted">{{ taskDuration(task) }}</small>
          <div v-if="summarizeRefs(task.result_refs).length" class="refs">
            <span v-for="ref in summarizeRefs(task.result_refs)" :key="ref" class="ref">
              {{ ref }}
            </span>
          </div>
        </div>
      </div>
    </div>
    <p v-if="!visibleStages.length" class="muted empty">
      The plan appears here once the run resolves its work.
    </p>
  </div>
</template>

<style scoped>
.tasks { display: flex; flex-direction: column; gap: 0.55rem; }
.stage-title {
  margin: 0 0 0.25rem;
  font-size: var(--aw-text-xs);
  font-weight: 700;
  color: var(--aw-muted);
}
.task {
  display: flex;
  gap: 0.55rem;
  align-items: flex-start;
  padding: 0.4rem 0.5rem;
  border-radius: var(--aw-radius-control);
}
.task > i { margin-top: 0.15rem; font-size: var(--aw-text-base); color: var(--aw-border-strong); }
.task.running { background: var(--aw-teal-soft); }
.task.running > i { color: var(--aw-teal-600); }
.task.awaiting_approval { background: var(--aw-warn-soft); }
.task.awaiting_approval > i { color: var(--aw-warn); }
.task.completed > i { color: var(--aw-teal); }
.task.failed > i { color: var(--aw-danger); }
.task.cancelled > i { color: var(--aw-muted); }
.task.cancelled { opacity: 0.65; }
.task.skipped { opacity: 0.65; }
.task-body { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; }
.task-title { font-size: var(--aw-text-base); line-height: 1.3; }
.task.completed .task-title { color: var(--aw-ink-soft); }
.task-error { color: var(--aw-danger); font-size: var(--aw-text-xs); }
.muted { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.refs { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.2rem; }
.ref {
  font-size: var(--aw-text-2xs);
  padding: 0.05rem 0.4rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-raised);
  color: var(--aw-ink-soft);
}
.empty { padding: 0.5rem; }
</style>
