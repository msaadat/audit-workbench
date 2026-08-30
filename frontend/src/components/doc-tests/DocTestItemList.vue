<script setup lang="ts">
import type { DocTestSummaryEntry } from '../../types'
import UiTestStatus from '../ui/UiTestStatus.vue'

defineProps<{
  items: DocTestSummaryEntry[]
  selectedId: string | null
  /** Item ids ticked for bulk sign-off. Cycle rows are never selectable. */
  checkedIds?: string[]
}>()
defineEmits<{
  select: [item: DocTestSummaryEntry]
  toggle: [itemId: string]
}>()

function entryId(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.test_id : entry.item_id
}

function entryLabel(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.title : (entry.label || entry.item_id)
}

</script>

<template>
  <div class="item-list">
    <!-- Flat, severity-ordered, one row per worklist item. Tests carry one or
         two items each, so grouping by test only added a level to click
         through without adding information. -->
    <div
      v-for="item in items"
      :key="`${item.entry_type}:${entryId(item)}`"
      class="row"
      :data-classification="item.classification"
      :class="{ active: entryId(item) === selectedId }"
    >
      <!-- The tick sits outside the navigation button so selecting rows for a
           bulk sign-off never moves the detail pane out from under you. -->
      <input
        v-if="item.entry_type === 'item'"
        type="checkbox"
        class="row-check"
        :checked="checkedIds?.includes(item.item_id)"
        :aria-label="`Select ${entryLabel(item)} for bulk sign-off`"
        @change="$emit('toggle', item.item_id)"
      >
      <button type="button" class="row-body" @click="$emit('select', item)">
        <span class="row-head">
          <span class="row-title">{{ entryLabel(item) }}</span>
          <UiTestStatus :status="item.classification" />
        </span>
      </button>
    </div>
    <p v-if="!items.length" class="empty">No worklist items match this filter.</p>
  </div>
</template>

<style scoped>
.item-list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.55rem; min-width: 0; }
.row {
  display: flex;
  align-items: flex-start;
  gap: 0.45rem;
  min-width: 0;
  padding: 0.55rem 0.6rem;
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-muted);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
.row-check { flex: 0 0 auto; margin-top: 0.3rem; accent-color: var(--aw-teal); cursor: pointer; }
.row-body {
  display: block;
  flex: 1 1 auto;
  min-width: 0;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.row:hover { background: var(--aw-raised); }
.row.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.row-head { display: flex; align-items: flex-start; gap: 0.45rem; min-width: 0; }
.row-title { display: -webkit-box; flex: 1 1 auto; overflow: hidden; min-width: 0; font-size: var(--aw-text-sm); font-weight: 400; line-height: 1.3; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }

.row[data-classification='exception'] { border-left-color: var(--aw-danger); }
.row[data-classification='needs_review'],
.row[data-classification='awaiting_evidence'] { border-left-color: var(--aw-warn); }
.row[data-classification='confirmed'] { border-left-color: var(--aw-ok); }

.empty { padding: 1rem 0.6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
