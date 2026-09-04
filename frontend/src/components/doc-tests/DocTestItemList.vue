<script setup lang="ts">
import type { DocTestSummaryEntry } from '../../types'

defineProps<{
  items: DocTestSummaryEntry[]
  selectedId: string | null
  /** Item ids ticked for bulk sign-off. Cycle rows are never selectable. */
  checkedIds?: string[]
  /** The list header's `Select` toggle; without it there are no checkboxes. */
  selecting?: boolean
}>()
defineEmits<{
  select: [item: DocTestSummaryEntry]
  toggle: [itemId: string]
}>()

/**
 * Flat, severity-ordered, one row per worklist item.
 *
 * Tests carry one or two items each, so grouping by test only added a level to
 * click through. What the row says is the dot and the meta line: the status
 * icon chip it replaces carried the same fact behind a tooltip, and left the
 * row saying nothing about what kind of work it was or who had answered for it.
 */

const KIND_LABELS: Record<string, string> = {
  vouching: 'Vouching',
  attribute: 'Attribute test',
  review: 'Document review',
  qa: 'Cited Q&A',
  cycle_vouch: 'Cycle vouch',
}
const TONES: Record<string, string> = {
  exception: 'bad',
  needs_review: 'warn',
  awaiting_evidence: 'warn',
  confirmed: 'ok',
}
const DISPOSITIONS: Record<string, string> = {
  confirmed: 'confirmed',
  exception: 'exception',
  needs_review: 'needs review',
}

function entryId(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.test_id : entry.item_id
}
function entryLabel(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.title : (entry.label || entry.item_id)
}
function kind(entry: DocTestSummaryEntry) {
  return KIND_LABELS[entry.test_kind ?? ''] ?? 'Document work'
}
/** What anybody decided, in the auditor's words rather than the runner's. */
function call(entry: DocTestSummaryEntry) {
  if (entry.entry_type === 'cycle_test') {
    const pending = entry.disposition_counts.pending ?? 0
    return pending ? `${pending} call${pending === 1 ? '' : 's'} not recorded` : 'all calls recorded'
  }
  if (entry.disposition.stale) return 'call out of date'
  return DISPOSITIONS[entry.disposition.state] ?? 'call not recorded'
}

/**
 * Kind, state and call — minus whatever two of them say twice.
 *
 * The classification is the joint reading of the run and the call, so on a
 * settled item it *is* the call: "confirmed · confirmed" spent half the line
 * agreeing with itself.
 */
function meta(entry: DocTestSummaryEntry) {
  const state = entry.classification.replaceAll('_', ' ')
  const decided = call(entry)
  return [kind(entry), state, decided === state ? '' : decided].filter(Boolean).join(' · ')
}
</script>

<template>
  <div class="item-list">
    <div
      v-for="item in items"
      :key="`${item.entry_type}:${entryId(item)}`"
      class="row"
      :class="{ active: entryId(item) === selectedId }"
    >
      <!-- The tick sits outside the navigation button so selecting rows for a
           bulk sign-off never moves the detail pane out from under you. -->
      <input
        v-if="selecting && item.entry_type === 'item'"
        type="checkbox"
        class="row-check"
        :checked="checkedIds?.includes(item.item_id)"
        :aria-label="`Select ${entryLabel(item)} for bulk sign-off`"
        @change="$emit('toggle', item.item_id)"
      >
      <span class="dot" :data-tone="TONES[item.classification] ?? 'neutral'" aria-hidden="true" />
      <button type="button" class="row-body" @click="$emit('select', item)">
        <span class="title">{{ entryLabel(item) }}</span>
        <span class="meta">
          {{ meta(item) }}<template v-if="item.conclusion_state === 'agent'"> · <span class="agent">agent-set</span></template>
        </span>
      </button>
    </div>
    <p v-if="!items.length" class="empty">No worklist item matches this filter.</p>
  </div>
</template>

<style scoped>
.item-list { display: flex; flex-direction: column; min-width: 0; }
.row {
  display: flex; align-items: center; gap: .625rem;
  min-width: 0;
  padding: .625rem .75rem;
  border-top: 1px solid var(--aw-border); border-left: 3px solid transparent;
}
.row:first-child { border-top: 0; }
.row:hover:not(.active) { background: var(--aw-raised); }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft); }

.row-check { flex: 0 0 auto; accent-color: var(--aw-teal); cursor: pointer; }
.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }

.row-body {
  display: flex; flex-direction: column; gap: 2px;
  flex: 1 1 auto; min-width: 0;
  padding: 0; border: 0; background: none; color: inherit; font: inherit;
  text-align: left; cursor: pointer;
}
.row-body:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 2px; }
.title { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-base); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.meta .agent { color: var(--aw-accent); }

.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
