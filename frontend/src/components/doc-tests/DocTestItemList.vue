<script setup lang="ts">
import type { DocTestSummaryEntry } from '../../types'
import UiTestStatus from '../ui/UiTestStatus.vue'

defineProps<{ items: DocTestSummaryEntry[]; selectedId: string | null }>()
defineEmits<{ select: [item: DocTestSummaryEntry] }>()

function entryId(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.test_id : entry.item_id
}

function entryLabel(entry: DocTestSummaryEntry) {
  return entry.entry_type === 'cycle_test' ? entry.title : (entry.label || entry.item_id)
}

const kindLabel: Record<string, string> = {
  vouching: 'Vouching',
  attribute: 'Attribute',
  review: 'Review',
  qa: 'Cited Q&A',
  cycle_vouch: 'Cycle vouch',
}
</script>

<template>
  <div class="item-list">
    <!-- Flat, severity-ordered, one row per worklist item. Tests carry one or
         two items each, so grouping by test only added a level to click
         through without adding information. -->
    <button
      v-for="item in items"
      :key="`${item.entry_type}:${entryId(item)}`"
      type="button"
      class="row"
      :data-classification="item.classification"
      :class="{ active: entryId(item) === selectedId }"
      @click="$emit('select', item)"
    >
      <!-- Status leads as an eyebrow beside the RCM reference; sharing a flex
           row with the title squeezed it to roughly seventeen characters, so
           labels wrapped to five or six lines and card heights were uneven. -->
      <span class="row-head">
        <UiTestStatus :status="item.classification" showLabel />
        <small v-if="item.rcm_id" class="row-rcm">{{ item.rcm_id }}</small>
        <small v-else class="row-rcm row-rcm--none">Unlinked</small>
      </span>
      <strong class="row-title">{{ entryLabel(item) }}</strong>
      <small class="row-test">
        {{ item.entry_type === 'cycle_test'
          ? `${item.tested_item_count} of ${item.item_count} items tested`
          : item.test_title }}
      </small>
      <small class="row-meta">{{ kindLabel[item.test_kind ?? ''] ?? 'Document work' }}</small>
      <small v-if="item.entry_type === 'cycle_test'" class="row-scope">
        {{ item.assurance_label }} · {{ item.assertion_columns }} assertion columns
      </small>
    </button>
    <p v-if="!items.length" class="empty">No worklist items match this filter.</p>
  </div>
</template>

<style scoped>
.item-list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.55rem; min-width: 0; }
.row {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
  padding: 0.55rem 0.6rem;
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-muted);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.row:hover { background: var(--aw-raised); }
.row.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.row-head { display: flex; align-items: center; gap: 0.35rem; min-width: 0; margin-bottom: 0.1rem; }
.row-rcm { flex: 0 0 auto; margin-left: auto; overflow: hidden; color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }
.row-rcm--none { font-family: inherit; font-style: italic; }
.row-title { display: -webkit-box; overflow: hidden; min-width: 0; font-size: var(--aw-text-sm); line-height: 1.3; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.row-test { min-width: 0; overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.row-meta { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.row-scope { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }

.row[data-classification='exception'] { border-left-color: var(--aw-danger); }
.row[data-classification='needs_review'],
.row[data-classification='awaiting_evidence'] { border-left-color: var(--aw-warn); }
.row[data-classification='confirmed'] { border-left-color: var(--aw-ok); }

.empty { padding: 1rem 0.6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
