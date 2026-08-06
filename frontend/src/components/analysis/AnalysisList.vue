<script setup lang="ts">
import Tag from 'primevue/tag'

import type { SavedAnalysis } from '../../types'
import { classificationMeta, formatExecutedAt, provenance } from './classification'

// The rail of saved procedures. Every row states what the procedure concluded,
// because that is what the triage filter above it filters by — a filtered list
// whose rows do not show their own classification gives no reason for what it
// is showing.
defineProps<{ items: SavedAnalysis[]; selectedId: string | null }>()
defineEmits<{ select: [analysis: SavedAnalysis] }>()
</script>

<template>
  <ul class="analysis-list" role="listbox" aria-label="Saved analysis procedures">
    <li v-for="item in items" :key="item.id">
      <button
        type="button"
        role="option"
        :aria-selected="selectedId === item.id"
        class="row"
        :class="{ active: selectedId === item.id }"
        :data-classification="item.classification"
        @click="$emit('select', item)"
      >
        <span class="row-title">{{ item.title }}</span>
        <span class="row-meta">
          <i :class="provenance(item).icon" aria-hidden="true" />
          {{ provenance(item).label }}
          <template v-if="item.table"> · {{ item.table }}</template>
        </span>
        <span class="row-state">
          <Tag
            :value="classificationMeta(item.classification).label"
            :severity="classificationMeta(item.classification).severity"
          />
          <small v-if="item.last_result?.executed_at">
            {{ formatExecutedAt(item.last_result.executed_at) }}
          </small>
        </span>
      </button>
    </li>
  </ul>
</template>

<style scoped>
.analysis-list { display: flex; flex-direction: column; gap: var(--aw-space-2); margin: 0; padding: 0; list-style: none; }

.row {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  width: 100%;
  text-align: left;
  padding: 0.6rem 0.75rem;
  border: 1px solid var(--aw-border);
  /* The status stripe: the row's outcome is legible before its text is read. */
  border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  font: inherit;
  color: inherit;
  cursor: pointer;
  transition: border-color .15s, background .15s;
}
.row:hover { border-color: var(--aw-border-strong); background: var(--aw-raised); }
.row.active { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.row[data-classification='exception'],
.row[data-classification='execution_error'] { border-left-color: var(--aw-danger); }
.row[data-classification='unusual'],
.row[data-classification='stale'] { border-left-color: var(--aw-warn); }
.row[data-classification='clear'] { border-left-color: var(--aw-ok); }
.row[data-classification='informational'] { border-left-color: var(--aw-teal); }

.row-title {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: var(--aw-text-sm);
  font-weight: 600;
  line-height: 1.3;
}
.row-meta {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  min-width: 0;
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}
.row-state { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.1rem; }
.row-state small { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
</style>
