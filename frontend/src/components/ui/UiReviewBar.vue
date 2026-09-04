<script setup lang="ts">
import { computed, ref } from 'vue'
import Popover from 'primevue/popover'

import { toggleFilter } from './statusLanes'
import type { ChipTone, ReviewChip, StatusFilterGroup, StatusLane } from './statusLanes'

/**
 * One row: what is outstanding on this page, and how far the work has got.
 *
 * It replaces `UiStatusLanes` and `UiFilterMenu` together, because they were
 * two controls answering one question. The lanes carried a `Details` expander
 * whose card restated every count as a sentence, three disclosure rows with
 * their own `Show rows` links, and a filter button whose menu held the same
 * numbers again — four surfaces for one tally.
 *
 * The chips *are* the filters. Each one is a count that narrows the list below
 * it, so a reader never has to translate "6 conclusions rest on evidence that
 * cannot establish population compliance" into a click. The whole vocabulary
 * stays behind the pressed chip, so the six that stand permanently are the six
 * worth the width and nothing is lost.
 *
 * Presentational: every number arrives derived, every filter leaves as an
 * event.
 */

const props = defineProps<{
  /** Run, concluded, written up — the three lanes, in that order. */
  lanes: StatusLane[]
  /** Which filters get a permanent chip, in order. */
  chips: ReviewChip[]
  /** The page's whole filter vocabulary; the chips read their counts from it. */
  filters?: StatusFilterGroup[]
  /** What the leading chip counts: `24` `rows`, `7` `items`, `30` `tests`. */
  allLabel: string
  total: number
  /** The narrowings in force, at most one per axis. */
  filter: string[]
}>()
const emit = defineEmits<{ filter: [string[]] }>()

const panel = ref<InstanceType<typeof Popover> | null>(null)

/** `Execution` names a stage; `Run` names what the number is out of. */
const METER_LABELS: Record<string, string> = {
  execution: 'Run',
  conclusion: 'Concluded',
  findings: 'Findings',
}

const options = computed(() => {
  const map = new Map<string, { label: string; value: number }>()
  for (const group of props.filters ?? []) {
    for (const option of group.options) map.set(option.key, option)
  }
  return map
})

/**
 * The chips actually drawn: `All`, then every named filter something matches.
 *
 * A chip counting nothing is a filter that can only produce an empty list, so
 * it is not drawn — which is also what keeps the row short on a healthy
 * engagement rather than showing six zeroes. The active one always survives: a
 * chip must not vanish from under the click that selected it.
 *
 * Six named chips is the cap, beside the `All` chip that is always drawn. Past
 * that the row wraps and stops being scannable, and the rest of the vocabulary
 * is one click away in the popover.
 */
const NAMED_CHIP_LIMIT = 6
const visible = computed(() => {
  const named: Array<{ key: string; label: string; value: number; tone: ChipTone }> = []
  for (const chip of props.chips) {
    const option = options.value.get(chip.filter)
    if (!option) continue
    if (!option.value && !props.filter.includes(chip.filter)) continue
    named.push({
      key: chip.filter,
      label: chip.label ?? option.label,
      value: option.value,
      tone: chip.tone,
    })
  }
  return [
    { key: 'all', label: props.allLabel, value: props.total, tone: 'neutral' as ChipTone },
    ...named.slice(0, NAMED_CHIP_LIMIT),
  ]
})

function pressed(key: string) {
  return key === 'all' ? props.filter.length === 0 : props.filter.includes(key)
}
/**
 * A chip that is not on applies its narrowing. The pressed one opens the whole
 * vocabulary instead of clearing itself: clearing is what `All` is for, and a
 * pressed chip is the only place with somewhere to put the other twenty
 * filters now that the menu button is gone.
 */
function activate(key: string, event: Event) {
  if (pressed(key)) return panel.value?.toggle(event)
  emit('filter', key === 'all' ? [] : toggleFilter(props.filter, key, props.filters))
}
function pick(key: string) {
  emit('filter', toggleFilter(props.filter, key, props.filters))
}

/** An option nothing matched cannot be picked into a non-empty list. */
const menu = computed(() => (props.filters ?? [])
  .map(group => ({
    ...group,
    options: group.options.filter(
      option => option.value > 0 || props.filter.includes(option.key),
    ),
  }))
  .filter(group => group.options.length))

const meters = computed(() => props.lanes.map(lane => ({
  key: lane.key,
  label: METER_LABELS[lane.key] ?? lane.label,
  value: lane.total ? `${lane.value}/${lane.total}` : lane.value,
  // Clamped so a lane whose portions were derived against different
  // denominators cannot paint past the end of its own track.
  segments: lane.segments.filter(segment => segment.portion > 0),
})))
</script>

<template>
  <section class="review-bar">
    <div class="chips" role="group" aria-label="Narrow the list">
      <button
        v-for="chip in visible"
        :key="chip.key"
        type="button"
        class="chip"
        :data-tone="chip.tone"
        :aria-pressed="pressed(chip.key)"
        @click="activate(chip.key, $event)"
      >
        <b class="aw-figure">{{ chip.value }}</b>{{ chip.label }}
      </button>
      <!-- The settle action the page owns, beside the chip that counts what it
           settles. It writes to the file, so it stays a button rather than
           becoming a seventh chip. -->
      <slot name="settle" />
    </div>

    <div class="meters">
      <p v-for="meter in meters" :key="meter.key" class="meter">
        <span class="meter-label">{{ meter.label }} <b class="aw-figure">{{ meter.value }}</b></span>
        <span class="meter-track">
          <i
            v-for="(segment, index) in meter.segments"
            :key="index"
            :data-tone="segment.tone"
            :style="{ width: `${segment.portion}%` }"
          />
        </span>
      </p>
    </div>

    <Popover ref="panel">
      <div class="menu" role="group" aria-label="Filter the list">
        <section v-for="group in menu" :key="group.key">
          <h4>{{ group.label }}</h4>
          <button
            v-for="option in group.options"
            :key="option.key"
            type="button"
            class="row"
            :data-tone="option.tone"
            :aria-pressed="filter.includes(option.key)"
            @click="pick(option.key)"
          >
            <span class="dot" aria-hidden="true" />
            <span class="label">{{ option.label }}</span>
            <span class="value aw-figure">{{ option.value }}</span>
          </button>
        </section>
        <button v-if="filter.length" type="button" class="clear" @click="emit('filter', [])">
          Show all {{ allLabel.toLowerCase() }}
        </button>
      </div>
    </Popover>
  </section>
</template>

<style scoped>
.review-bar {
  display: flex; align-items: center; gap: .75rem 1.25rem; flex-wrap: wrap; min-width: 0;
  padding: .625rem .875rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.chips { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; flex: 1; min-width: 0; }

/* The count leads: the chip is a number that happens to name itself, not a
   label that happens to carry one. */
.chip {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: .25rem .6875rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill);
  background: var(--aw-panel); color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600;
  white-space: nowrap; cursor: pointer;
}
.chip b { color: var(--aw-ink-strong); font-weight: 700; font-variant-numeric: tabular-nums; }
.chip:hover { border-color: var(--aw-border-strong); }
.chip:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.chip[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.chip[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.chip[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); color: var(--aw-ok); }
.chip[data-tone='agent'] { border-color: var(--aw-accent-line); background: var(--aw-accent-soft); color: var(--aw-accent); }
.chip[data-tone='bad'] b, .chip[data-tone='warn'] b, .chip[data-tone='ok'] b, .chip[data-tone='agent'] b { color: inherit; }
/* One treatment for "this is what you are looking at", whatever the chip's own
   tone: the row has to say which of six is in force at a glance. */
.chip[aria-pressed='true'] { border-color: var(--aw-teal); background: var(--aw-teal-soft); color: var(--aw-teal-strong); }
.chip[aria-pressed='true'] b { color: inherit; }

.meters { display: flex; align-items: center; gap: 1.125rem; flex: none; }
.meter { display: flex; flex-direction: column; gap: .25rem; margin: 0; }
.meter-label {
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600;
  letter-spacing: .06em; text-transform: uppercase; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.meter-label b { color: var(--aw-ink-strong); }
.meter-track { display: flex; width: 4rem; height: 4px; overflow: hidden; border-radius: var(--aw-radius-pill); background: var(--aw-border); }
.meter-track i { display: block; height: 100%; }
.meter-track i[data-tone='ok'] { background: var(--aw-ok); }
.meter-track i[data-tone='warn'] { background: var(--aw-warn); }
.meter-track i[data-tone='bad'] { background: var(--aw-danger); }
.meter-track i[data-tone='neutral'] { background: var(--aw-border-strong); }

.menu { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0 var(--aw-space-4); min-width: 12rem; max-width: 40rem; }
.menu section { display: flex; flex-direction: column; gap: .1rem; min-width: 0; padding: .2rem 0 .5rem; }
.menu h4 {
  margin: 0 0 .25rem; padding: 0 .4rem;
  color: var(--aw-muted-strong); font-size: var(--aw-text-2xs); font-weight: 700;
  letter-spacing: .09em; text-transform: uppercase;
}
.menu .row {
  display: grid; grid-template-columns: .45rem minmax(0, 1fr) auto;
  align-items: center; gap: .45rem;
  padding: .3rem .4rem; border: 0; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer;
}
.menu .row:hover { background: var(--aw-raised); }
.menu .row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.menu .row[aria-pressed='true'] { background: var(--aw-teal-soft); color: var(--aw-teal); font-weight: 600; }
.menu .dot { width: .45rem; height: .45rem; border-radius: var(--aw-radius-pill); background: var(--aw-border-strong); }
.menu .row[data-tone='ok'] .dot { background: var(--aw-ok); }
.menu .row[data-tone='warn'] .dot { background: var(--aw-warn); }
.menu .row[data-tone='bad'] .dot { background: var(--aw-danger); }
.menu .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.menu .value { color: var(--aw-ink); font-weight: 700; font-variant-numeric: tabular-nums; }
.menu .clear {
  grid-column: 1 / -1; padding: .35rem .4rem; border: 0; border-top: 1px solid var(--aw-border);
  background: none; color: var(--aw-teal);
  font: inherit; font-size: var(--aw-text-sm); font-weight: 600; text-align: left; cursor: pointer;
}

@container workspace-panel (max-width: 60rem) {
  .meters { flex-wrap: wrap; }
}
</style>
