<script setup lang="ts">
import { computed } from 'vue'

import type { EngagementPhase, WorkspaceProgress } from '../types'

/**
 * Where an engagement stands, in four colours and no figures.
 *
 * The index asks a different question from the console rail. Not "how much of
 * the fieldwork is concluded" — that needs the rail's fractions and the page
 * they link to — but "which of these files is the one I meant to open, and
 * which one wants me". So this keeps the rail's states and its colours, since
 * a reader moving between the two must not have to learn a second meaning for
 * amber, and drops everything that would need a number to stay honest.
 *
 * Data is the strip's own segment rather than one of the backend's phases: the
 * listing already carries the table count, and an engagement with no folder
 * imported is the one case where nothing downstream has begun for a reason the
 * card can state.
 */

type State = EngagementPhase['state'] | 'unknown'

const props = defineProps<{
  tables: number
  progress?: WorkspaceProgress | null
}>()

const PHASES = ['planning', 'fieldwork', 'report'] as const
const PHASE_LABELS: Record<(typeof PHASES)[number], string> = {
  planning: 'Planning',
  fieldwork: 'Fieldwork',
  report: 'Report',
}
const STATE_LABELS: Record<State, string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  complete: 'Complete',
  attention: 'Needs attention',
  unknown: 'Unavailable',
}

const segments = computed(() => [
  {
    key: 'data',
    label: 'Data',
    state: (props.tables ? 'complete' : 'not_started') as State,
    title: props.tables
      ? `Data: ${props.tables} table${props.tables === 1 ? '' : 's'} imported`
      : 'Data: nothing imported yet',
  },
  ...PHASES.map(phase => {
    // A workspace whose status could not be derived still lists; its phases
    // read as unavailable rather than as work nobody has started.
    const state = (props.progress?.[phase] ?? 'unknown') as State
    return {
      key: phase,
      label: PHASE_LABELS[phase],
      state,
      title: `${PHASE_LABELS[phase]}: ${STATE_LABELS[state]}`,
    }
  }),
])
</script>

<template>
  <div class="progress-strip">
    <span v-for="segment in segments" :key="segment.key" class="segment" :data-state="segment.state" :title="segment.title">
      <i aria-hidden="true" />
      <small>{{ segment.label }}</small>
    </span>
  </div>
</template>

<style scoped>
.progress-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.3rem;
  min-width: 0;
}
.segment { display: flex; flex-direction: column; gap: 0.22rem; min-width: 0; }
.segment i {
  height: 4px;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-border-strong);
}
.segment small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
}
/* A phase underway is not a phase in trouble, so `in_progress` keeps its own
   reading rather than joining amber — but it must not be mistaken for done
   either. It differs from complete twice over: in hue, and in being broken
   rather than solid. Four bars read at a glance, and read at all without
   colour, which a green-beside-teal pair did not. The dashes are deliberately
   even: they say "underway", not "this far along". */
.segment[data-state='complete'] i { background: var(--aw-ok); }
.segment[data-state='in_progress'] i {
  background:
    repeating-linear-gradient(90deg, var(--aw-info) 0 7px, transparent 7px 11px),
    var(--aw-border-strong);
}
.segment[data-state='attention'] i { background: var(--aw-warn); }
.segment[data-state='attention'] small { color: var(--aw-warn-ink); }
.segment[data-state='unknown'] i { opacity: 0.45; }
</style>
