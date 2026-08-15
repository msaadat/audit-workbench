<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type {
  DataTestExceptionDisposition,
  DataTestExceptionProfile,
  DataTestDispositionState,
  FramePayload,
} from '../../types'
import FrameTable from '../FrameTable.vue'
import { plural } from '../../format'

const props = defineProps<{
  profile: DataTestExceptionProfile
  frame: FramePayload
  dispositions: DataTestExceptionDisposition[]
  busy?: boolean
}>()
const emit = defineEmits<{
  (event: 'rule', payload: { key: string; state: DataTestDispositionState; note: string }): void
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
// the only one. The rows themselves always show, because each one is now what
// an auditor rules on — hiding them would hide the only control there is.
const showBars = computed(
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

const byKey = computed(
  () => new Map(props.dispositions.map(item => [item.key, item])),
)
function ruling(label: string): DataTestExceptionDisposition | null {
  return byKey.value.get(label) ?? null
}
/** A stale ruling reads as undecided, because that is how it now counts. */
function state(label: string): DataTestDispositionState {
  const value = ruling(label)
  return !value || value.stale ? 'pending' : value.state
}
const openGroups = computed(
  () => props.profile.reasons.filter(reason => state(reason.label) !== 'accepted').length,
)

// Retiring an exception is the ruling that moves the control conclusion, so it
// is the one that has to carry its reasoning. Confirming what the run already
// found needs no argument.
const noting = ref<string | null>(null)
const note = ref('')
function beginAccept(label: string) {
  noting.value = label
  note.value = ruling(label)?.note ?? ''
}
function commitAccept() {
  if (!noting.value) return
  emit('rule', { key: noting.value, state: 'accepted', note: note.value.trim() })
  noting.value = null
  note.value = ''
}
function rule(label: string, next: DataTestDispositionState) {
  noting.value = null
  emit('rule', { key: label, state: next, note: ruling(label)?.note ?? '' })
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

    <!-- Each group is both the breakdown and the unit an auditor rules on. -->
    <div v-if="profile.reasons.length" class="reasons">
      <p class="block-head">
        Why they failed
        <span v-if="openGroups" class="open-tally">{{ openGroups }} still open</span>
        <span v-else class="open-tally settled">all accepted</span>
      </p>
      <div
        v-for="reason in profile.reasons"
        :key="reason.label"
        class="reason-row"
        :data-state="state(reason.label)"
      >
        <button
          type="button"
          class="reason"
          :class="{ active: selected === reason.label, bars: showBars }"
          :aria-pressed="selected === reason.label"
          @click="pick(reason.label)"
        >
          <span class="reason-label">{{ reason.label }}</span>
          <span v-if="showBars" class="bar" aria-hidden="true">
            <span class="fill" :style="{ width: `${(reason.records / scale) * 100}%` }" />
          </span>
          <span class="count aw-figure">
            {{ reason.records }}
            <small v-if="reason.rows !== reason.records">/ {{ reason.rows }} rows</small>
          </span>
        </button>

        <div class="ruling">
          <span class="verdict" :data-state="state(reason.label)">
            {{
              state(reason.label) === 'accepted' ? 'Accepted'
              : state(reason.label) === 'exception' ? 'Exception'
              : state(reason.label) === 'needs_review' ? 'Needs review'
              : 'Not ruled on'
            }}
          </span>
          <small v-if="ruling(reason.label)?.stale" class="stale">
            Ruled against evidence that has since changed.
          </small>
          <small
            v-else-if="ruling(reason.label)?.source === 'agent' && state(reason.label) !== 'pending'"
            class="by-agent"
          >
            Recorded by an unattended run; no auditor has reviewed it.
          </small>
          <small v-else-if="ruling(reason.label)?.note" class="note-text">
            “{{ ruling(reason.label)?.note }}”
          </small>
          <span class="actions">
            <button
              type="button" class="link" :disabled="busy"
              @click="beginAccept(reason.label)"
            >Accept</button>
            <button
              type="button" class="link" :disabled="busy"
              @click="rule(reason.label, 'exception')"
            >Confirm exception</button>
            <button
              type="button" class="link" :disabled="busy"
              @click="rule(reason.label, 'needs_review')"
            >Needs review</button>
            <button
              v-if="state(reason.label) !== 'pending'"
              type="button" class="link" :disabled="busy"
              @click="rule(reason.label, 'pending')"
            >Clear</button>
          </span>
        </div>

        <!-- Accepting retires exceptions from the control conclusion, so the
             note is worth asking for. Asked for, not required: an auditor who
             has decided can record it and write the reasoning up afterwards. -->
        <form v-if="noting === reason.label" class="accept" @submit.prevent="commitAccept">
          <label>
            Why these are not a control failure <span class="optional">(optional)</span>
            <textarea v-model="note" rows="2" />
          </label>
          <span class="accept-actions">
            <button type="submit" class="link" :disabled="busy">Accept group</button>
            <button type="button" class="link" @click="noting = null">Cancel</button>
          </span>
        </form>
      </div>
      <p v-if="selected" class="filtered">
        Showing the {{ plural(rows.length, 'row') }} that failed on “{{ selected }}”.
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
        other {{ plural(withheldColumns, 'field') }} of the record.
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
.block-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.7rem; }
.open-tally { color: var(--aw-danger); font-weight: 700; }
.open-tally.settled { color: var(--aw-ok); }

/* The left rule is the ruling: it is the one thing worth seeing when scanning a
   column of groups for what still needs a decision. */
.reason-row { min-width: 0; padding-left: 0.55rem; border-left: 3px solid var(--aw-border-strong); }
.reason-row + .reason-row { margin-top: 0.35rem; }
.reason-row[data-state='accepted'] { border-left-color: var(--aw-ok); }
.reason-row[data-state='exception'] { border-left-color: var(--aw-danger); }
.reason-row[data-state='needs_review'] { border-left-color: var(--aw-warn); }

.reason {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.7rem;
  width: 100%;
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
.reason.bars { grid-template-columns: minmax(0, 1fr) 8rem auto; }
.reason:hover { background: var(--aw-raised); }
.reason.active { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }

.ruling { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.35rem 0.6rem; padding: 0 0.5rem 0.3rem; min-width: 0; }
.verdict { font-size: var(--aw-text-xs); font-weight: 700; }
.verdict[data-state='pending'] { color: var(--aw-muted); }
.verdict[data-state='accepted'] { color: var(--aw-ok); }
.verdict[data-state='exception'] { color: var(--aw-danger); }
.verdict[data-state='needs_review'] { color: var(--aw-warn); }
.ruling small { min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); overflow-wrap: anywhere; }
.ruling .stale { color: var(--aw-warn); }
.actions { display: flex; flex-wrap: wrap; gap: 0.6rem; margin-left: auto; }
.actions .link { font-size: var(--aw-text-xs); }
.link[disabled] { color: var(--aw-muted); cursor: default; text-decoration: none; }

.accept { display: flex; flex-direction: column; gap: 0.4rem; padding: 0 0.5rem 0.5rem; }
.accept label { display: flex; flex-direction: column; gap: 0.25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.accept textarea {
  width: 100%;
  padding: 0.4rem 0.5rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  font-size: var(--aw-text-sm);
  resize: vertical;
}
.optional { color: var(--aw-muted); font-weight: 400; text-transform: none; }
.accept-actions { display: flex; gap: 0.7rem; }
.accept-actions .link { font-size: var(--aw-text-xs); font-weight: 700; }
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
  .reason, .reason.bars { grid-template-columns: minmax(0, 1fr) auto; }
  .bar { display: none; }
  .actions { margin-left: 0; }
}
</style>
