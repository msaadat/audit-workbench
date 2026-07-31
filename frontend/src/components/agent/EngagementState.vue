<script setup lang="ts">
import { computed } from 'vue'

import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { DashboardPhase, WorkspaceSummary } from '../../types'
import ActionRequired from './ActionRequired.vue'

/**
 * Console right rail: immediate run actions, followed by engagement progress
 * from data the shell already loads.
 */

const props = defineProps<{
  workspace: WorkspaceSummary
  phases: DashboardPhase[]
}>()

const nav = useWorkspaceNav()

const stateIcon: Record<DashboardPhase['state'], string> = {
  not_started: 'pi pi-circle',
  in_progress: 'pi pi-clock',
  complete: 'pi pi-check-circle',
  attention: 'pi pi-exclamation-triangle',
}
const stateLabel: Record<DashboardPhase['state'], string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  complete: 'Complete',
  attention: 'Needs attention',
}

const counts = computed(() => [
  { label: 'Tables', value: props.workspace.tables.length, destination: 'data' as const },
  { label: 'Documents', value: props.workspace.document_count ?? 0, destination: 'documents' as const },
  { label: 'Findings', value: props.workspace.finding_count ?? 0, destination: 'findings' as const },
])
</script>

<template>
  <aside class="engagement-state">
    <ActionRequired :workspaceId="workspace.id" />

    <p class="rail-label">Engagement</p>
    <div class="counts">
      <router-link v-for="item in counts" :key="item.label" :to="nav.to(item.destination)" class="count">
        <strong>{{ item.value }}</strong>
        <small>{{ item.label }}</small>
      </router-link>
    </div>

    <p class="rail-label">Progress</p>
    <div v-if="!phases.length" class="empty">Status is unavailable.</div>
    <template v-for="phase in phases" :key="phase.id">
      <div v-if="phase.sub.length" class="phase phase-group" :data-state="phase.state">
        <i :class="stateIcon[phase.state]" aria-hidden="true" />
        <span class="body">
          <strong>{{ phase.label }}</strong>
          <small>{{ phase.summary || stateLabel[phase.state] }}</small>
          <small v-if="phase.issues.length" class="issue">{{ phase.issues[0] }}</small>
          <span class="sub-buttons">
            <router-link
              v-for="sub in phase.sub"
              :key="sub.id"
              :to="nav.target(sub.target)"
              class="sub-btn"
              :data-state="sub.state"
            >
              <i :class="stateIcon[sub.state]" aria-hidden="true" />
              {{ sub.label }}
            </router-link>
          </span>
        </span>
      </div>
      <router-link v-else :to="nav.target(phase.target)" class="phase" :data-state="phase.state">
        <i :class="stateIcon[phase.state]" aria-hidden="true" />
        <span class="body">
          <strong>{{ phase.label }}</strong>
          <small>{{ phase.summary || stateLabel[phase.state] }}</small>
          <small v-if="phase.issues.length" class="issue">{{ phase.issues[0] }}</small>
        </span>
      </router-link>
    </template>
  </aside>
</template>

<style scoped>
.engagement-state {
  flex: 0 0 17rem;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-height: 0;
  overflow-y: auto;
  padding: 0.9rem 0.8rem;
  border-left: 1px solid var(--aw-border);
  background: var(--aw-raised);
}
.rail-label {
  margin: 0.55rem 0.15rem 0.35rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}
.rail-label:first-child { margin-top: 0; }

.counts { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.35rem; }
.count {
  display: grid;
  gap: 0.1rem;
  padding: 0.45rem 0.5rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-sm);
  background: var(--p-surface-0);
  color: inherit;
  text-decoration: none;
}
.count:hover { border-color: var(--aw-teal); }
.count strong { font-size: 1.05rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.count small { color: var(--aw-muted); font-size: 0.66rem; }

.phase {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  padding: 0.5rem 0.55rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-sm);
  margin-bottom: 0.3rem;
  background: var(--p-surface-0);
  color: inherit;
  text-decoration: none;
}
.phase:hover { border-color: var(--aw-teal); }
.phase > i { padding-top: 0.1rem; font-size: 0.8rem; }
.phase[data-state='not_started'] > i { color: #94a3b8; }
.phase[data-state='in_progress'] > i { color: #2563eb; }
.phase[data-state='complete'] > i { color: var(--aw-ok); }
.phase[data-state='attention'] > i { color: var(--aw-warn); }
.phase .body { display: grid; gap: 0.1rem; min-width: 0; }
.phase strong { font-size: 0.78rem; font-weight: 600; }
.phase small { color: var(--aw-muted); font-size: 0.68rem; line-height: 1.35; }
.phase small.issue { color: var(--aw-warn); }

.phase-group { cursor: default; }

.sub-buttons { display: flex; gap: 0.3rem; margin-top: 0.3rem; }
.sub-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.15rem 0.4rem;
  border: 1px solid var(--aw-border);
  border-radius: 999px;
  font-size: 0.65rem;
  font-weight: 600;
  color: inherit;
  text-decoration: none;
  background: var(--aw-canvas);
}
.sub-btn:hover { border-color: var(--aw-teal); }
.sub-btn > i { font-size: 0.6rem; }
.sub-btn[data-state='not_started'] > i { color: #94a3b8; }
.sub-btn[data-state='in_progress'] > i { color: #2563eb; }
.sub-btn[data-state='complete'] > i { color: var(--aw-ok); }
.sub-btn[data-state='attention'] > i { color: var(--aw-warn); }

.empty { padding: 0.5rem 0.15rem; color: var(--aw-muted); font-size: 0.72rem; }

/* The console sizes against its own box, not the viewport: the drawer is gone
   here, but the surface rail still takes width out of it. */
@container console-body (max-width: 62rem) {
  .engagement-state { display: none; }
}
</style>
