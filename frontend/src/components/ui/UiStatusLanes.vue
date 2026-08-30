<script setup lang="ts">
import { ref } from 'vue'

import UiFilterMenu from './UiFilterMenu.vue'
import { toggleFilter } from './statusLanes'
import type { StatusAction, StatusDisclosure, StatusFilterGroup, StatusLane } from './statusLanes'

/**
 * The state of a fieldwork surface, above the list it describes.
 *
 * Presentational only: every number arrives already derived, and every filter
 * leaves as an event. The counts double as filters, which is what lets one list
 * serve both the overview and the detail — there is no second view to keep in
 * step with the first.
 *
 * It rests as one line. The three lanes answer a question asked on arrival and
 * when reporting up, not while working, and the expanded card was costing a
 * fifth of the screen on every screen that carries one — most of it spent on
 * lanes with nothing outstanding. The meters survive the collapse because a
 * proportion is the one thing a sentence renders badly.
 *
 * Lane actions are not drawn here. They belong to the page header, beside the
 * other buttons, so that what to do next is not the last thing on the page to
 * be read; the page passes them there through `statusActions`. A disclosure's
 * action stays, because it settles a qualification rather than closing a gap
 * and has never been something the header should shout.
 */

const props = defineProps<{
  lanes: StatusLane[]
  disclosures?: StatusDisclosure[]
  /** The page's whole filter vocabulary. Absent pages keep the plain banner. */
  filters?: StatusFilterGroup[]
  /** The narrowings in force, at most one per axis. */
  filter: string[]
  /** How the active narrowing reads, in the page's words. */
  filterLabel?: string
  /** Work is already in flight; nothing new should be startable. */
  busy?: boolean
  /** False when no assistant is configured or one is mid-run. */
  canRunAgent?: boolean
}>()
const emit = defineEmits<{
  filter: [string[]]
  action: [StatusAction]
}>()

const expanded = ref(false)

/** Clicking the active chip clears it again — the chip is the toggle. */
function toggle(key: string) {
  emit('filter', toggleFilter(props.filter, key, props.filters))
}
function blocked(action: StatusAction) {
  return Boolean(props.busy) || Boolean(action.needsAgent && !props.canRunAgent)
}
</script>

<template>
  <div class="status-lanes">
    <section class="status-card">
      <!-- The resting form. One row, three lanes, and the meters that carry
           the proportions the sentences beside them cannot. -->
      <div class="summary">
        <!-- `24 / 30`, not "24 of 30 tests concluded". The lane label already
             says which population, and the sentence is what Details is for. -->
        <p
          v-for="lane in lanes"
          :key="lane.key"
          class="sum"
          :data-state="lane.state"
          :title="`${lane.label} — ${lane.value} ${lane.caption}`"
        >
          <span class="lane-dot" />
          <span class="sum-label">{{ lane.label }}</span>
          <b v-if="lane.total">{{ lane.value }} <i>/</i> {{ lane.total }}</b>
          <template v-else>
            <b>{{ lane.value }}</b>
            <span class="sum-caption">{{ lane.caption }}</span>
          </template>
          <span v-if="lane.segments.length" class="meter meter-sm">
            <i
              v-for="(segment, index) in lane.segments"
              :key="index"
              :data-tone="segment.tone"
              :style="{ width: `${segment.portion}%` }"
            />
          </span>
        </p>

        <div class="summary-controls">
          <UiFilterMenu
            v-if="filters?.length"
            :groups="filters"
            :active="filter"
            :activeLabel="filterLabel"
            @select="emit('filter', $event)"
          />
          <button
            type="button"
            class="expander"
            :aria-expanded="expanded"
            @click="expanded = !expanded"
          >
            {{ expanded ? 'Hide details' : 'Details' }}
            <i class="pi" :class="expanded ? 'pi-chevron-up' : 'pi-chevron-down'" />
          </button>
        </div>
      </div>

      <div v-if="expanded" class="lanes">
        <article v-for="lane in lanes" :key="lane.key" class="lane" :data-state="lane.state">
          <p class="lane-label"><span class="lane-dot" />{{ lane.label }}</p>
          <p class="lane-count"><b>{{ lane.value }}</b><span>{{ lane.caption }}</span></p>

          <!-- An empty meter is a meter of nothing, not a full one: the track
               shows on its own and the segments simply do not fill it. -->
          <div v-if="lane.segments.length" class="meter">
            <i
              v-for="(segment, index) in lane.segments"
              :key="index"
              :data-tone="segment.tone"
              :style="{ width: `${segment.portion}%` }"
            />
          </div>

          <div v-if="lane.chips.length" class="lane-chips">
            <button
              v-for="chip in lane.chips"
              :key="`${chip.key}-${chip.label}`"
              type="button"
              class="chip"
              :data-tone="chip.tone"
              :aria-pressed="filter.includes(chip.key)"
              @click="toggle(chip.key)"
            >
              <span class="chip-dot" />{{ chip.label }}
            </button>
          </div>

          <p v-if="lane.rest" class="lane-rest">
            <i v-if="lane.state === 'done'" class="pi pi-check" />{{ lane.rest }}
          </p>
        </article>
      </div>

      <!-- Never collapsed. These are what the page is obliged to say about the
           numbers above them, and a qualification behind a disclosure control
           is one the reader has to already suspect to find. -->
      <div v-if="disclosures?.length" class="disclosures">
        <p v-for="item in disclosures" :key="item.key" class="disclosure" :data-tone="item.tone">
          <span class="mark">{{ item.mark }}</span>
          <span class="grow">{{ item.message }}</span>
          <button
            v-if="item.action"
            type="button"
            class="settle"
            :disabled="blocked(item.action)"
            @click="emit('action', item.action)"
          >
            {{ item.action.label }}
          </button>
          <button type="button" class="link" @click="toggle(item.filter)">
            {{ filter.includes(item.filter) ? 'Clear' : 'Show rows' }}
          </button>
        </p>
      </div>
    </section>

    <!-- Pages without a filter menu still need somewhere to read and drop the
         active narrowing; the menu's own button carries it for the rest. -->
    <p v-if="filter.length && filterLabel && !filters?.length" class="filter-banner">
      <i class="pi pi-filter" />
      <span>Showing <strong>{{ filterLabel }}</strong></span>
      <button type="button" @click="emit('filter', [])">Clear filter</button>
    </p>
  </div>
</template>

<style scoped>
.status-lanes { display:flex; flex-direction:column; gap:.6rem; min-width:0 }
.status-card { border:1px solid var(--aw-border); border-radius:var(--aw-radius-surface); background:var(--aw-panel) }

/* --- resting form ----------------------------------------------------- */
.summary { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem 1.1rem; padding:.45rem .55rem .45rem .9rem; min-width:0 }
.sum { display:flex; align-items:center; gap:.4rem; margin:0; min-width:0; color:var(--aw-ink-soft); font-size:var(--aw-text-sm) }
.sum-label { color:var(--aw-muted-strong); font-size:var(--aw-text-2xs); font-weight:700; letter-spacing:.09em; text-transform:uppercase; white-space:nowrap }
.sum b { display:flex; align-items:baseline; gap:.22rem; color:var(--aw-ink-strong); font-family:var(--aw-font-mono); font-weight:700; font-variant-numeric:tabular-nums; white-space:nowrap }
.sum b i { color:var(--aw-border-strong); font-style:normal; font-weight:400 }
.sum-caption { white-space:nowrap; overflow:hidden; text-overflow:ellipsis }
.summary-controls { display:flex; align-items:center; gap:.35rem; margin-left:auto; min-width:0 }

.expander {
  display:inline-flex; align-items:center; gap:.3rem; flex:none;
  min-height:var(--aw-control-height-sm); padding:.2rem .55rem;
  border:1px solid transparent; border-radius:var(--aw-radius-control);
  background:none; color:var(--aw-teal);
  font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); font-weight:600; cursor:pointer;
}
.expander:hover { border-color:var(--aw-teal-line); background:var(--aw-teal-soft) }
.expander:focus-visible { outline:2px solid var(--aw-teal); outline-offset:1px }
.expander .pi { font-size:.6rem }

/* --- expanded form ---------------------------------------------------- */
/* Three explicit tracks rather than auto-fit: a wrapped lane would keep the
   left divider it no longer sits beside, and every page here has exactly the
   three questions. Below the breakpoint they stack and the divider moves. */
.lanes { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); border-top:1px solid var(--aw-border) }
.lane { display:flex; flex-direction:column; gap:.5rem; min-width:0; padding:.8rem 1rem .9rem; border-left:1px solid var(--aw-border) }
.lane:first-child { border-left:0 }

.lane-label { display:flex; align-items:center; gap:.35rem; margin:0; color:var(--aw-muted-strong); font-size:var(--aw-text-2xs); font-weight:600; letter-spacing:.1em; text-transform:uppercase }
.lane-dot { width:6px; height:6px; flex:none; border-radius:50%; background:var(--aw-border-strong) }
[data-state='done'] .lane-dot { background:var(--aw-ok) }
[data-state='gap'] .lane-dot { background:var(--aw-warn) }
[data-state='alarm'] .lane-dot { background:var(--aw-danger) }

.lane-count { display:flex; align-items:baseline; flex-wrap:wrap; gap:.4rem; margin:0 }
.lane-count b { color:var(--aw-ink-strong); font-family:var(--aw-font-mono); font-size:var(--aw-text-xl); font-weight:650; line-height:1.05; letter-spacing:-.02em; font-variant-numeric:tabular-nums }
.lane-count span { color:var(--aw-ink-soft); font-size:var(--aw-text-sm) }

.meter { display:flex; height:6px; overflow:hidden; border-radius:var(--aw-radius-pill); background:var(--aw-raised) }
/* Short and fixed, so three of them line up down a row instead of stretching
   to whatever text sits beside them. */
.meter-sm { flex:none; width:3.25rem; height:4px }
.meter i { display:block; height:100% }
.meter i[data-tone='ok'] { background:var(--aw-ok) }
.meter i[data-tone='warn'] { background:var(--aw-warn) }
.meter i[data-tone='bad'] { background:var(--aw-danger) }
.meter i[data-tone='neutral'] { background:var(--aw-border-strong) }

.lane-chips { display:flex; flex-wrap:wrap; align-items:center; gap:.3rem }
.chip { display:inline-flex; align-items:center; gap:.3rem; padding:.14rem .45rem; border:1px solid transparent; border-radius:var(--aw-radius-pill); background:var(--aw-raised); color:var(--aw-ink-soft); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); font-weight:550; font-variant-numeric:tabular-nums; cursor:pointer }
.chip:hover { border-color:var(--aw-border-strong) }
.chip:focus-visible { outline:2px solid var(--aw-teal); outline-offset:1px }
.chip[data-tone='ok'] { background:var(--aw-ok-soft); color:var(--aw-ok) }
.chip[data-tone='warn'] { background:var(--aw-warn-soft); color:var(--aw-warn-ink) }
.chip[data-tone='bad'] { background:var(--aw-danger-soft); color:var(--aw-danger) }
/* The pressed chip carries its own colour as the ring, so a selected filter
   reads as selected without a second accent competing with the tone. */
.chip[aria-pressed='true'] { border-color:currentColor; box-shadow:inset 0 0 0 1px currentColor }
.chip-dot { width:5px; height:5px; flex:none; border-radius:50%; background:currentColor; opacity:.75 }

.lane-rest { display:flex; align-items:center; gap:.3rem; margin:.1rem 0 0; color:var(--aw-muted); font-size:var(--aw-text-xs) }
.lane-rest .pi { color:var(--aw-ok); font-size:var(--aw-text-xs); font-weight:700 }

.disclosures { border-top:1px solid var(--aw-border); background:var(--aw-canvas); border-radius:0 0 var(--aw-radius-surface) var(--aw-radius-surface) }
.disclosure { display:flex; align-items:center; gap:.5rem; margin:0; padding:.42rem 1rem; color:var(--aw-ink-soft); font-size:var(--aw-text-sm) }
.disclosure + .disclosure { border-top:1px solid var(--aw-border) }
.disclosure .mark { flex:none; padding:.08rem .34rem; border-radius:4px; background:var(--aw-accent-soft); color:var(--aw-accent); font-family:var(--aw-font-mono); font-size:var(--aw-text-2xs); font-weight:700; letter-spacing:.05em }
.disclosure[data-tone='warn'] .mark { background:var(--aw-warn-soft); color:var(--aw-warn-ink) }
.disclosure[data-tone='muted'] .mark { background:var(--aw-raised); color:var(--aw-muted) }
.disclosure .grow { flex:1; min-width:0 }
.disclosure .link { flex:none; padding:.1rem .25rem; border:0; border-radius:4px; background:none; color:var(--aw-teal); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); font-weight:600; cursor:pointer; white-space:nowrap }
.disclosure .link:hover { background:var(--aw-teal-soft) }
/* Bordered rather than bare, because it writes to the file while the link
   beside it only changes what is on screen. Still quieter than a header
   action: the strip states a qualification, it does not report a gap. */
.disclosure .settle { flex:none; padding:.14rem .45rem; border:1px solid var(--aw-border-strong); border-radius:var(--aw-radius-control); background:var(--aw-panel); color:var(--aw-ink-soft); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); font-weight:600; cursor:pointer; white-space:nowrap }
.disclosure .settle:hover:not(:disabled) { border-color:var(--aw-teal); color:var(--aw-teal) }
.disclosure .settle:focus-visible { outline:2px solid var(--aw-teal); outline-offset:1px }
.disclosure .settle:disabled { opacity:.5; cursor:not-allowed }

.filter-banner { display:flex; align-items:center; gap:.5rem; margin:0; padding:.4rem .7rem; border:1px solid var(--aw-teal-line); border-radius:var(--aw-radius-control); background:var(--aw-teal-soft); color:var(--aw-teal); font-size:var(--aw-text-sm) }
.filter-banner button { margin-left:auto; padding:.1rem .3rem; border:0; border-radius:4px; background:none; color:var(--aw-teal); font-family:var(--aw-font-sans); font-size:var(--aw-text-xs); font-weight:600; cursor:pointer }
.filter-banner button:hover { background:var(--aw-panel) }

@container workspace-panel (max-width: 60rem) {
  .lanes { grid-template-columns:minmax(0, 1fr) }
  .lane { border-left:0; border-top:1px solid var(--aw-border) }
  .lane:first-child { border-top:0 }
  .summary-controls { margin-left:0 }
}
</style>
