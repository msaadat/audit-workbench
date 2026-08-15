<script setup lang="ts">
import { computed, ref } from 'vue'

import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { DashboardPhase, DashboardSection } from '../../types'
import { engagementStatus } from './engagementStatus'
import type { PhaseAction, PhaseRow } from './engagementStatus'

/**
 * Console right rail: where the engagement stands, from data the shell already
 * loads. `PlanSpine` renders below this, in the same rail.
 *
 * Presentational only — every figure, chip and action comes from
 * `engagementStatus`, which is where the counting is tested. What lives here is
 * the one piece of state the derivation cannot own: which resting phase the
 * reader has opened by hand.
 */

const props = defineProps<{
  phases: DashboardPhase[]
  sections?: Record<string, DashboardSection>
  busy?: boolean
}>()

const emit = defineEmits<{ action: [PhaseAction] }>()

const nav = useWorkspaceNav()
const status = computed(() => engagementStatus(props.phases, props.sections ?? {}))

// A phase the reader opened keeps that choice regardless of what the run does
// next, so work completing elsewhere cannot collapse something being read.
const opened = ref<Record<string, boolean>>({})
function isOpen(row: PhaseRow) {
  return opened.value[row.id] ?? row.display === 'open'
}
function toggle(row: PhaseRow) {
  opened.value = { ...opened.value, [row.id]: !isOpen(row) }
}

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

/** The whole issue list, not its first entry — the rest are why it is open. */
function summary(row: PhaseRow) {
  return `${row.label}: ${stateLabel[row.state]}${
    row.issues.length ? ` — ${row.issues.join(' ')}` : ''}`
}
</script>

<template>
  <div class="engagement-state">
    <div class="head">
      <p class="rail-label">Progress</p>
      <span v-if="status.position" class="position">{{ status.position }}</span>
    </div>
    <div v-if="status.arc.length" class="arc" aria-hidden="true">
      <span v-for="(state, index) in status.arc" :key="index" :data-state="state" />
    </div>

    <div v-if="!phases.length" class="empty">Status is unavailable.</div>

    <div
      v-for="row in status.rows"
      :key="row.id"
      class="phase"
      :data-state="row.state"
      :data-display="row.display"
      :class="{ open: isOpen(row) }"
    >
      <i :class="stateIcon[row.state]" aria-hidden="true" />
      <div class="body">
        <!-- The head is the toggle; the label inside it is the link. A resting
             phase must be openable without navigating away from the console. -->
        <button class="head-row" :aria-expanded="isOpen(row)" :title="summary(row)" @click="toggle(row)">
          <router-link :to="nav.target(row.target)" class="name" @click.stop>{{ row.label }}</router-link>
          <!-- The tail is the collapsed row's stand-in for the figure. Drawing
               both would state the same fraction twice on one row. -->
          <span v-if="row.tail && !(isOpen(row) && row.figure)" class="tail">{{ row.tail }}</span>
          <i class="chev" :class="isOpen(row) ? 'pi pi-chevron-up' : 'pi pi-chevron-down'" aria-hidden="true" />
        </button>

        <template v-if="isOpen(row)">
          <p v-if="row.figure || row.caption" class="figure">
            <b v-if="row.figure">{{ row.figure }}</b>
            <span v-if="row.caption">{{ row.caption }}</span>
          </p>
          <div v-if="row.figure" class="meter">
            <i
              v-for="(segment, index) in row.segments"
              :key="index"
              :data-tone="segment.tone"
              :style="{ flexGrow: segment.portion }"
            />
          </div>
          <div v-if="row.chips.length" class="chips">
            <router-link
              v-for="chip in row.chips"
              :key="chip.key"
              :to="nav.target(chip.target)"
              class="chip"
              :data-tone="chip.tone"
            >
              <u v-if="chip.detail">{{ chip.detail }}</u>{{ chip.label }}
            </router-link>
          </div>
          <p v-for="issue in row.issues.slice(0, 2)" :key="issue" class="issue">{{ issue }}</p>
          <span v-if="row.issues.length > 2" class="more">
            {{ row.issues.length - 2 }} more
          </span>
          <div v-if="row.actions.length" class="actions">
            <button
              v-for="action in row.actions"
              :key="action.key"
              class="act"
              :data-tone="action.tone"
              :disabled="busy"
              @click="emit('action', action)"
            >{{ action.label }}</button>
          </div>
        </template>
      </div>
    </div>

    <!-- Disclosures qualify the ticks; they never move them. A muted line under
         a rule is the whole treatment, so a finished file still reads finished. -->
    <div v-if="status.disclosures.length" class="disclosures">
      <p v-for="item in status.disclosures" :key="item.key" class="disclosure">
        <i class="pi pi-info-circle" aria-hidden="true" />
        <span>
          {{ item.message }}
          <router-link :to="nav.target(item.target)">Open</router-link>
        </span>
      </p>
    </div>
  </div>
</template>

<style scoped>
.engagement-state { display: flex; flex-direction: column; }
.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
  margin: 0 0.15rem 0.4rem;
}
.rail-label { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.position {
  color: var(--aw-muted);
  font-family: var(--aw-font-mono);
  font-size: var(--aw-text-2xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.arc { display: flex; gap: 2px; margin: 0 0.15rem 0.6rem; }
.arc span { flex: 1; height: 4px; border-radius: var(--aw-radius-pill); background: var(--aw-border-strong); }
.arc span[data-state='complete'] { background: var(--aw-ok); }
.arc span[data-state='in_progress'] { background: var(--aw-teal-600); }
.arc span[data-state='attention'] { background: var(--aw-warn); }

.phase {
  display: grid;
  grid-template-columns: 0.85rem minmax(0, 1fr);
  gap: 0 0.45rem;
  padding: 0.45rem 0.55rem;
  margin-bottom: 0.3rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
/* Complete work gives its vertical budget to the phase in flight and to Plan
   below — but the chevron gets all of it back. */
.phase[data-display='collapsed']:not(.open),
.phase[data-display='pending']:not(.open) {
  padding-top: 0.3rem;
  padding-bottom: 0.3rem;
  border-color: transparent;
  background: transparent;
}
.phase[data-display='pending']:not(.open) .name { color: var(--aw-muted); }
.phase[data-display='open'] { border-color: var(--aw-teal-600); box-shadow: var(--aw-shadow-sm); }
.phase > i { padding-top: 0.2rem; font-size: var(--aw-text-sm); }
.phase[data-state='not_started'] > i { color: var(--aw-muted-strong); }
.phase[data-state='in_progress'] > i { color: var(--aw-info); }
.phase[data-state='complete'] > i { color: var(--aw-ok); }
.phase[data-state='attention'] > i { color: var(--aw-warn); }
.body { min-width: 0; }

.head-row {
  display: flex;
  align-items: baseline;
  gap: 0.35rem;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.name {
  color: var(--aw-ink);
  font-size: var(--aw-text-sm);
  font-weight: 600;
  text-decoration: none;
}
.name:hover { color: var(--aw-teal); }
.tail {
  margin-left: auto;
  color: var(--aw-muted);
  font-family: var(--aw-font-mono);
  font-size: var(--aw-text-2xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.chev { color: var(--aw-muted-strong); font-size: var(--aw-text-2xs); }
.tail + .chev { margin-left: 0; }
.head-row > .chev:nth-child(2) { margin-left: auto; }

.figure { display: flex; align-items: baseline; gap: 0.3rem; margin: 0.15rem 0 0; }
.figure b {
  color: var(--aw-ink-strong);
  font-size: var(--aw-text-md);
  font-weight: 650;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.figure span { color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.35; }

.meter {
  display: flex;
  gap: 1px;
  height: 5px;
  margin: 0.35rem 0 0;
  border-radius: var(--aw-radius-pill);
  overflow: hidden;
  background: var(--aw-border);
}
.meter i { display: block; height: 100%; min-width: 0; }
.meter i[data-tone='ok'] { background: var(--aw-ok); }
.meter i[data-tone='warn'] { background: var(--aw-warn); }
.meter i[data-tone='bad'] { background: var(--aw-danger); }
.meter i[data-tone='neutral'] { background: var(--aw-border-strong); }

.chips { display: flex; flex-wrap: wrap; gap: 0.25rem; margin-top: 0.35rem; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.1rem 0.36rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-canvas);
  color: var(--aw-ink-soft);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
  text-decoration: none;
}
.chip:hover { border-color: var(--aw-teal); }
.chip u {
  text-decoration: none;
  color: var(--aw-muted);
  font-family: var(--aw-font-mono);
  font-variant-numeric: tabular-nums;
}
.chip[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); color: var(--aw-ok); }
.chip[data-tone='ok'] u { color: inherit; }
.chip[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.chip[data-tone='warn'] u { color: inherit; }
.chip[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger); }
.chip[data-tone='bad'] u { color: inherit; }

.issue {
  margin: 0.35rem 0 0;
  color: var(--aw-warn-ink);
  font-size: var(--aw-text-xs);
  line-height: 1.35;
}
.more {
  display: inline-block;
  margin-top: 0.25rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  font-weight: 600;
}

.actions { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: 0.45rem; }
.act {
  padding: 0.22rem 0.5rem;
  border: 1px solid var(--aw-teal);
  border-radius: var(--aw-radius-control);
  background: var(--aw-teal);
  color: var(--aw-on-dark);
  font-size: var(--aw-text-xs);
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}
.act[data-tone='ghost'] { background: transparent; color: var(--aw-teal); }
.act:disabled { opacity: 0.55; cursor: not-allowed; }

.disclosures {
  margin: 0.5rem 0.15rem 0;
  padding-top: 0.5rem;
  border-top: 1px solid var(--aw-border);
}
.disclosure {
  display: flex;
  align-items: flex-start;
  gap: 0.35rem;
  margin: 0.3rem 0 0;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
  line-height: 1.35;
}
.disclosure:first-child { margin-top: 0; }
.disclosure > i { padding-top: 0.15rem; font-size: var(--aw-text-2xs); }
.disclosure a { color: var(--aw-teal); font-weight: 600; text-decoration: none; white-space: nowrap; }

.empty { padding: 0.5rem 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
</style>
