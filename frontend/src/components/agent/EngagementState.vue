<script setup lang="ts">
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { DashboardPhase } from '../../types'

/**
 * Console right rail: engagement progress from data the shell already loads.
 * `PlanSpine` renders below this, in the same rail.
 */

defineProps<{
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
</script>

<template>
  <div class="engagement-state">
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
  </div>
</template>

<style scoped>
.engagement-state {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.rail-label {
  margin: 0.55rem 0.15rem 0.35rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  font-weight: 700;
}
.rail-label:first-child { margin-top: 0; }

.phase {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  padding: 0.5rem 0.55rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  margin-bottom: 0.3rem;
  background: var(--aw-panel);
  color: inherit;
  text-decoration: none;
}
.phase:hover { border-color: var(--aw-teal); }
.phase > i { padding-top: 0.1rem; font-size: var(--aw-text-sm); }
.phase[data-state='not_started'] > i { color: var(--aw-muted-strong); }
.phase[data-state='in_progress'] > i { color: var(--aw-info); }
.phase[data-state='complete'] > i { color: var(--aw-ok); }
.phase[data-state='attention'] > i { color: var(--aw-warn); }
.phase .body { display: grid; gap: 0.1rem; min-width: 0; }
.phase strong { font-size: var(--aw-text-sm); font-weight: 600; }
.phase small { color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.35; }
.phase small.issue { color: var(--aw-warn); }

.phase-group { cursor: default; }

.sub-buttons { display: flex; gap: 0.3rem; margin-top: 0.3rem; }
.sub-btn {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.15rem 0.4rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-pill);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
  color: inherit;
  text-decoration: none;
  background: var(--aw-canvas);
}
.sub-btn:hover { border-color: var(--aw-teal); }
.sub-btn > i { font-size: var(--aw-text-2xs); }
.sub-btn[data-state='not_started'] > i { color: var(--aw-muted-strong); }
.sub-btn[data-state='in_progress'] > i { color: var(--aw-info); }
.sub-btn[data-state='complete'] > i { color: var(--aw-ok); }
.sub-btn[data-state='attention'] > i { color: var(--aw-warn); }

.empty { padding: 0.5rem 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
</style>
