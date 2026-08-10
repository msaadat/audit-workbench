<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type { DataTestExceptionProfile, FramePayload } from '../../types'
import FrameTable from '../FrameTable.vue'

const props = defineProps<{
  profile: DataTestExceptionProfile
  frame: FramePayload
}>()

// Provenance columns. They describe where a row came from, not what failed, and
// the reason bars above the table already say it better than a column would.
const INTERNAL = ['_step_id', '_step_label', '_reason']
// The reason has its own column or its own filter, and the step id is machinery.
// Which step caught the row is worth keeping, so it stays in the expanded record.
const HIDDEN = ['_step_id', '_reason']

const selected = ref<string | null>(null)
watch(() => props.frame, () => { selected.value = null })

const reasonIndex = computed(() => props.frame.columns.indexOf('_reason'))
const rows = computed(() =>
  selected.value === null || reasonIndex.value < 0
    ? props.frame.rows
    : props.frame.rows.filter(row => row[reasonIndex.value] === selected.value),
)

const scale = computed(() =>
  Math.max(1, ...props.profile.reasons.map(reason => reason.records)),
)
// One reason is not a breakdown, and a bar at full width says only that it is
// the only one. Reasons that are step labels are already listed as steps.
const showReasons = computed(
  () => props.profile.reason_source === 'predicate' && props.profile.reasons.length > 1,
)

/**
 * The columns worth a column: the identifier, and the fields the reasons on
 * show actually read. Everything else about the record is one click away, which
 * is the difference between evidence and a wall of nulls — a stacked exception
 * frame is the union of every step's output, so most of it is empty per row.
 */
const visibleColumns = computed(() => {
  const showing = props.profile.reasons.filter(
    reason => selected.value === null || reason.label === selected.value,
  )
  const read = showing.flatMap(reason => reason.columns)
  const wanted = [props.profile.entity_key, ...read].filter(
    (column): column is string => Boolean(column) && !INTERNAL.includes(column as string),
  )
  const ordered = props.frame.columns.filter(column => wanted.includes(column))
  // The reason earns a column only where it varies: one reason, or a filter down
  // to one, means every row would repeat the same sentence.
  return showing.length > 1 && reasonIndex.value >= 0
    ? [ordered[0], '_reason', ...ordered.slice(1)].filter(Boolean)
    : ordered
})

const narrowedFrame = computed<FramePayload>(() => ({
  columns: props.frame.columns,
  dtypes: props.frame.dtypes,
  rows: rows.value,
}))

const withheldColumns = computed(
  () =>
    props.frame.columns.filter(
      column => !visibleColumns.value.includes(column) && !HIDDEN.includes(column),
    ).length,
)
// The stored frame is capped, so the bars can legitimately count rows the table
// cannot show. Say so rather than letting the two numbers quietly disagree.
const truncated = computed(() => props.profile.row_count - props.frame.rows.length)
const rate = computed(() =>
  props.profile.population
    ? Math.round((props.profile.record_count / props.profile.population) * 100)
    : null,
)
const unit = computed(() =>
  !props.profile.population && props.profile.record_count === 1 ? 'record' : 'records',
)

function pick(label: string) {
  selected.value = selected.value === label ? null : label
}
</script>

<template>
  <section class="explorer">
    <!-- Records against their population. The row count is the derived figure,
         not the headline: a record that fails two checks returns two rows. -->
    <header class="headline">
      <p class="lead">
        <strong class="aw-figure">{{ profile.record_count }}</strong>
        <template v-if="profile.population">
          of <span class="aw-figure">{{ profile.population }}</span> {{ unit }} in
          {{ profile.population_table }} failed
        </template>
        <template v-else>{{ unit }} failed</template>
        <span v-if="rate !== null" class="rate" :data-heavy="rate >= 10">{{ rate }}%</span>
      </p>
      <p v-if="profile.row_count !== profile.record_count" class="sub">
        {{ profile.row_count }} exception rows — a record that fails more than one check
        returns one row per check.
      </p>
    </header>

    <!-- Only conditions earn this block. Where the reasons are just the steps,
         the step list below already says it, and saying it twice is the noise
         this view exists to remove. -->
    <div v-if="showReasons" class="reasons">
      <p class="block-head">Why they failed</p>
      <button
        v-for="reason in profile.reasons"
        :key="reason.label"
        type="button"
        class="reason"
        :class="{ active: selected === reason.label }"
        :aria-pressed="selected === reason.label"
        @click="pick(reason.label)"
      >
        <span class="reason-label">{{ reason.label }}</span>
        <span class="bar" aria-hidden="true">
          <span class="fill" :style="{ width: `${(reason.records / scale) * 100}%` }" />
        </span>
        <span class="count aw-figure">
          {{ reason.records }}
          <small v-if="reason.rows !== reason.records">/ {{ reason.rows }} rows</small>
        </span>
      </button>
      <p v-if="selected" class="filtered">
        Showing the {{ rows.length }} row(s) that failed on “{{ selected }}”.
        <button type="button" class="link" @click="selected = null">Show all</button>
      </p>
    </div>

    <FrameTable
      :frame="narrowedFrame"
      :visibleColumns="visibleColumns"
      :columnLabels="{ _reason: 'Reason', _step_label: 'Check' }"
      :hiddenColumns="HIDDEN"
      expandable
      scrollHeight="22rem"
    />
    <p class="note">
      <template v-if="withheldColumns > 0">
        Showing the identifier and the fields this test reads; open a row for the
        other {{ withheldColumns }} field(s) of the record.
      </template>
      <template v-if="truncated > 0">
        The stored result keeps the first {{ frame.rows.length }} rows; {{ truncated }} more are counted above but not listed.
      </template>
    </p>
  </section>
</template>

<style scoped>
.explorer { display: flex; flex-direction: column; gap: 0.7rem; min-width: 0; }

/* Two sizes and two weights in the whole summary: the count, and the sentence
   it sits in. Everything secondary is the same small muted line. */
.headline { min-width: 0; }
.lead { margin: 0; font-size: var(--aw-text-md); font-weight: 400; line-height: 1.4; }
.lead strong { font-size: var(--aw-text-xl); font-weight: 700; }
.rate { margin-left: 0.5rem; color: var(--aw-muted); font-weight: 700; }
.rate[data-heavy='true'] { color: var(--aw-danger); }
.sub { margin: 0.25rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.45; }

.block-head { margin: 0 0 0.35rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }

/* The frame's own header row is the loudest thing in a narrow panel by default.
   Match it to the cells it labels. */
.explorer :deep(th) { font-size: var(--aw-text-sm); }

.reasons { display: flex; flex-direction: column; gap: 0.25rem; min-width: 0; }
.reason {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 8rem auto;
  align-items: center;
  gap: 0.7rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid transparent;
  border-radius: var(--aw-radius-control);
  background: none;
  color: inherit;
  font: inherit;
  font-size: var(--aw-text-sm);
  text-align: left;
  cursor: pointer;
}
.reason:hover { background: var(--aw-raised); }
.reason.active { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.reason-label { min-width: 0; overflow-wrap: anywhere; }
.bar { height: 0.5rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); }
.reason.active .bar { background: var(--aw-panel); }
.fill { display: block; height: 100%; border-radius: var(--aw-radius-pill); background: var(--aw-danger); }
.count { flex: 0 0 auto; font-weight: 700; white-space: nowrap; }
.count small { color: var(--aw-muted); font-weight: 400; }

.filtered { margin: 0.2rem 0 0; padding: 0 0.5rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: inherit; text-decoration: underline; cursor: pointer; }
.note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.5; }

@container master-detail-content (max-width: 32rem) {
  .reason { grid-template-columns: minmax(0, 1fr) auto; }
  .bar { display: none; }
}
</style>
