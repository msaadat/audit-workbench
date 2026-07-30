<script setup lang="ts">
import type { DocTestSummaryItem } from '../../types'
import UiTestStatus from '../ui/UiTestStatus.vue'

defineProps<{ items: DocTestSummaryItem[]; selectedId: string | null }>()
defineEmits<{ select: [item: DocTestSummaryItem] }>()

const kindLabel: Record<string, string> = {
  vouching: 'Vouching',
  attribute: 'Attribute',
  review: 'Review',
  qa: 'Cited Q&A',
}
</script>

<template>
  <div class="item-list">
    <!-- Flat, severity-ordered, one row per worklist item. Tests carry one or
         two items each, so grouping by test only added a level to click
         through without adding information. -->
    <button
      v-for="item in items"
      :key="item.item_id"
      type="button"
      class="row"
      :data-classification="item.classification"
      :class="{ active: item.item_id === selectedId }"
      @click="$emit('select', item)"
    >
      <span class="row-head">
        <strong>{{ item.label || item.item_id }}</strong>
        <UiTestStatus :status="item.classification" showLabel />
      </span>
      <small class="row-test">{{ item.test_title }}</small>
      <small class="row-meta">
        {{ kindLabel[item.test_kind ?? ''] ?? 'Document work' }}
        <template v-if="item.rcm_id"> · {{ item.rcm_id }}</template>
        <template v-else> · unlinked</template>
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
  border-radius: var(--aw-radius-sm);
  background: #fff;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.row:hover { background: var(--aw-raised); }
.row.active { border-color: var(--aw-teal); background: var(--aw-teal-soft); }
.row-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.4rem; min-width: 0; }
.row-head strong { min-width: 0; font-size: 0.82rem; line-height: 1.3; }
.row-test { min-width: 0; overflow: hidden; color: var(--aw-ink); font-size: 0.74rem; text-overflow: ellipsis; white-space: nowrap; }
.row-meta { color: var(--aw-muted); font-size: 0.7rem; }

.row[data-classification='exception'] { border-left-color: var(--aw-danger); }
.row[data-classification='needs_review'],
.row[data-classification='awaiting_evidence'] { border-left-color: var(--aw-warn); }
.row[data-classification='confirmed'] { border-left-color: var(--aw-ok); }

.empty { padding: 1rem 0.6rem; color: var(--aw-muted); font-size: 0.78rem; text-align: center; }
</style>
