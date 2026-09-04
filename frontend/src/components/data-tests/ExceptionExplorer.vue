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

/**
 * Why the rows failed, and the ruling on each reason.
 *
 * The population figure the header used to lead with is on the verdict bar
 * above this: it is the same fact, and stating it twice is what made a data
 * test read as four different verdicts on one screen. What is left here is the
 * part only this section can say — which check caught which rows, and what
 * anybody has decided about them.
 */

// Provenance columns. They describe where a row came from, not what failed, and
// the reason groups above the table already say it better than a column would.
const INTERNAL = ['_step_id', '_step_label', '_reason']
// The reason has its own column or its own filter, and the step id is machinery.
// Which step caught the row is worth keeping, so it stays in the expanded record.
const HIDDEN = ['_step_id', '_reason']
/** The synthetic column carrying each row's ruling. Never in the stored frame. */
const RULING = '_ruling'

const selected = ref<string | null>(null)
watch(() => props.frame, () => { selected.value = null })

const reasonIndex = computed(() => props.frame.columns.indexOf('_reason'))
const rows = computed(() =>
  selected.value === null || reasonIndex.value < 0
    ? props.frame.rows
    : props.frame.rows.filter(row => row[reasonIndex.value] === selected.value),
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
  const base = showing.length > 1 && reasonIndex.value >= 0
    ? [ordered[0], '_reason', ...ordered.slice(1)].filter(Boolean)
    : ordered
  // The ruling rides last, so the eye reads the record and then what was
  // decided about it — and so a row nobody has ruled on says `Open` rather
  // than saying nothing, which is what sent auditors back to the groups above
  // to work out which rows a ruling had covered.
  return [...base, RULING]
})

/**
 * The frame the table draws: the stored rows, narrowed to the picked reason,
 * carrying one derived column. The ruling is per reason group, so it is a
 * projection of the dispositions onto the rows rather than anything stored.
 */
const narrowedFrame = computed<FramePayload>(() => ({
  columns: [...props.frame.columns, RULING],
  dtypes: [...props.frame.dtypes, 'String'],
  rows: rows.value.map(row => [
    ...row,
    reasonIndex.value < 0 ? RULING_LABELS.pending : RULING_LABELS[state(String(row[reasonIndex.value]))],
  ]),
}))

const withheldColumns = computed(
  () =>
    props.frame.columns.filter(
      column => !visibleColumns.value.includes(column) && !HIDDEN.includes(column),
    ).length,
)
// The stored frame is capped, so the groups can legitimately count rows the
// table cannot show. Say so rather than letting the two numbers quietly
// disagree.
const truncated = computed(() => props.profile.row_count - props.frame.rows.length)

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
const RULING_LABELS: Record<DataTestDispositionState, string> = {
  pending: 'Open',
  accepted: 'Accepted',
  exception: 'Exception',
  needs_review: 'Needs review',
}
const RULINGS: Array<{ value: DataTestDispositionState; label: string; tone: string }> = [
  { value: 'accepted', label: 'Accept', tone: 'ok' },
  { value: 'exception', label: 'Confirm exception', tone: 'bad' },
  { value: 'needs_review', label: 'Needs review', tone: 'warn' },
]
const openRows = computed(() => props.profile.reasons
  .filter(reason => state(reason.label) !== 'accepted')
  .reduce((total, reason) => total + reason.rows, 0))

// Retiring an exception is the ruling that moves the control conclusion, so it
// is the one that has to carry its reasoning. Confirming what the run already
// found needs no argument.
const noting = ref<string | null>(null)
const note = ref('')
function begin(label: string, next: DataTestDispositionState) {
  if (next !== 'accepted') {
    noting.value = null
    emit('rule', { key: label, state: next, note: ruling(label)?.note ?? '' })
    return
  }
  noting.value = label
  note.value = ruling(label)?.note ?? ''
}
function commitAccept() {
  if (!noting.value) return
  emit('rule', { key: noting.value, state: 'accepted', note: note.value.trim() })
  noting.value = null
  note.value = ''
}
function clear(label: string) {
  noting.value = null
  emit('rule', { key: label, state: 'pending', note: ruling(label)?.note ?? '' })
}
</script>

<template>
  <section class="explorer">
    <!-- Each group is both the breakdown and the unit an auditor rules on. -->
    <div v-if="profile.reasons.length" class="reasons">
      <p class="block-head">
        <span class="aw-label">Why they failed</span>
        <span class="tally aw-figure" :data-open="openRows > 0">
          {{ plural(profile.reasons.length, 'reason') }}<template v-if="openRows">
            · {{ openRows }} {{ openRows === 1 ? 'row' : 'rows' }} still open</template>
          <template v-else> · all accepted</template>
        </span>
      </p>

      <article
        v-for="reason in profile.reasons"
        :key="reason.label"
        class="reason-card"
        :data-state="state(reason.label)"
      >
        <div class="reason-body">
          <button
            type="button"
            class="reason-name"
            :aria-pressed="selected === reason.label"
            @click="pick(reason.label)"
          >
            <span class="reason-label">{{ reason.label }}</span>
            <span class="count aw-figure">
              {{ plural(reason.rows, 'row') }}
              <template v-if="reason.records !== reason.rows">· {{ reason.records }} records</template>
            </span>
          </button>

          <!-- One line, whatever the state: what was decided, and what
               qualifies it. The old block stacked a verdict word, a staleness
               note, an authorship note and a quoted reason as four rows. -->
          <p class="ruling-line">
            <template v-if="state(reason.label) === 'pending'">
              Not ruled on.
              <span v-if="ruling(reason.label)?.stale" class="qualifier">
                An earlier ruling was made against evidence that has since changed.
              </span>
            </template>
            <template v-else>
              <b :data-state="state(reason.label)">{{ RULING_LABELS[state(reason.label)] }}</b>
              <span v-if="ruling(reason.label)?.source === 'agent'" class="by-agent">
                by an unattended run; no auditor has read it.
              </span>
              <span v-else-if="ruling(reason.label)?.note" class="quoted">
                “{{ ruling(reason.label)?.note }}”
              </span>
              <button type="button" class="link" :disabled="busy" @click="clear(reason.label)">
                Clear
              </button>
            </template>
          </p>
        </div>

        <!-- One control with three positions, not three buttons: the rulings
             are mutually exclusive, and a row of equal buttons never said
             which one was in force. -->
        <span class="rulings" role="group" :aria-label="`Rule on ${reason.label}`">
          <button
            v-for="option in RULINGS"
            :key="option.value"
            type="button"
            :data-tone="option.tone"
            :aria-pressed="state(reason.label) === option.value"
            :disabled="busy"
            @click="begin(reason.label, option.value)"
          >{{ option.label }}</button>
        </span>

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
      </article>

      <p v-if="selected" class="filtered">
        Showing the {{ plural(rows.length, 'row') }} that failed on “{{ selected }}”.
        <button type="button" class="link" @click="selected = null">Show all</button>
      </p>
    </div>

    <div class="table">
      <FrameTable
        :frame="narrowedFrame"
        :visibleColumns="visibleColumns"
        :columnLabels="{ _reason: 'Reason', _step_label: 'Check', [RULING]: 'Ruling' }"
        :hiddenColumns="HIDDEN"
        expandable
        scrollHeight="22rem"
      />
      <p v-if="withheldColumns > 0 || truncated > 0 || profile.row_count !== profile.record_count" class="note">
        <template v-if="withheldColumns > 0">
          Open a row for the other {{ plural(withheldColumns, 'field') }} of the record.
        </template>
        <template v-if="profile.row_count !== profile.record_count">
          A record that fails more than one check returns one row per check.
        </template>
        <template v-if="truncated > 0">
          The stored result keeps the first {{ frame.rows.length }} rows; {{ truncated }} more are counted above but not listed.
        </template>
      </p>
    </div>
  </section>
</template>

<style scoped>
.explorer { display: flex; flex-direction: column; gap: 1rem; min-width: 0; }

.reasons { display: flex; flex-direction: column; gap: .5rem; min-width: 0; }
.block-head { display: flex; align-items: baseline; justify-content: space-between; gap: .7rem; margin: 0; }
.tally { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.tally[data-open='true'] { color: var(--aw-danger); }

/* The left rule is the ruling: it is the one thing worth seeing when scanning a
   column of groups for what still needs a decision. */
.reason-card {
  display: grid; grid-template-columns: minmax(0, 1fr) auto;
  align-items: center; gap: .5rem 1rem; min-width: 0;
  padding: .625rem .875rem;
  border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
}
.reason-card[data-state='accepted'] { border-left-color: var(--aw-ok); }
.reason-card[data-state='exception'] { border-left-color: var(--aw-danger); }
.reason-card[data-state='needs_review'] { border-left-color: var(--aw-warn); }
.reason-body { display: flex; flex-direction: column; gap: .2rem; min-width: 0; }

.reason-name {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: .5rem;
  padding: 0; border: 0; background: none; color: var(--aw-ink-strong);
  font: inherit; font-size: var(--aw-text-base); font-weight: 600;
  text-align: left; cursor: pointer;
}
.reason-name:hover .reason-label { color: var(--aw-teal); }
.reason-name[aria-pressed='true'] .reason-label { color: var(--aw-teal); text-decoration: underline; }
.reason-name:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.reason-label { min-width: 0; overflow-wrap: anywhere; }
.count { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 500; }

.ruling-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: .3rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.45; }
.ruling-line b[data-state='accepted'] { color: var(--aw-ok); }
.ruling-line b[data-state='exception'] { color: var(--aw-danger); }
.ruling-line b[data-state='needs_review'] { color: var(--aw-warn-ink); }
.ruling-line .qualifier { color: var(--aw-warn-ink); }
.ruling-line .by-agent { color: var(--aw-accent); }
.ruling-line .quoted { min-width: 0; overflow-wrap: anywhere; }

/* Three positions of one control. The dividers are what say the three are the
   same question rather than three separate offers. */
.rulings { display: inline-flex; flex: none; overflow: hidden; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.rulings button {
  padding: .3rem .625rem; border: 0; background: none;
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap; cursor: pointer;
}
.rulings button + button { border-left: 1px solid var(--aw-border-strong); }
.rulings button[data-tone='ok'] { color: var(--aw-ok); }
.rulings button[data-tone='bad'] { color: var(--aw-danger); }
.rulings button[data-tone='warn'] { color: var(--aw-warn-ink); }
.rulings button:hover:not(:disabled) { background: var(--aw-raised); }
.rulings button:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.rulings button:disabled { opacity: .5; cursor: not-allowed; }
.rulings button[aria-pressed='true'][data-tone='ok'] { background: var(--aw-ok-soft); }
.rulings button[aria-pressed='true'][data-tone='bad'] { background: var(--aw-danger-soft); }
.rulings button[aria-pressed='true'][data-tone='warn'] { background: var(--aw-warn-soft); }

.accept { grid-column: 1 / -1; display: flex; flex-direction: column; gap: .4rem; }
.accept label { display: flex; flex-direction: column; gap: .25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.accept textarea {
  width: 100%;
  padding: .4rem .5rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  font-size: var(--aw-text-sm);
  resize: vertical;
}
.optional { color: var(--aw-muted); font-weight: 400; text-transform: none; }
.accept-actions { display: flex; gap: .7rem; }
.accept-actions .link { font-size: var(--aw-text-xs); font-weight: 700; }

.filtered { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.link { padding: 0; border: 0; background: none; color: var(--aw-teal); font: inherit; font-size: inherit; text-decoration: underline; cursor: pointer; }
.link[disabled] { color: var(--aw-muted); cursor: default; text-decoration: none; }

.table { display: flex; flex-direction: column; gap: .35rem; min-width: 0; }
/* The identifiers are figures and the header labels them; the frame's own
   header ran a size larger than the cells it names. */
.table :deep(th) { font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.5; }

@container master-detail-content (max-width: 34rem) {
  .reason-card { grid-template-columns: minmax(0, 1fr); }
  .rulings { justify-self: start; }
}
</style>
