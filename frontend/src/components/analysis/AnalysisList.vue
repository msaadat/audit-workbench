<script setup lang="ts">
import type { SavedAnalysis } from '../../types'
import { classificationMeta } from './classification'

// The rail is deliberately only identity plus an icon: provenance, source
// table, execution time, and the full outcome all live in the open procedure.
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
        <span class="row-head">
          <span class="row-title">{{ item.title }}</span>
          <i
            class="row-status"
            :class="[
              classificationMeta(item.classification).icon,
              `tone-${classificationMeta(item.classification).severity}`,
            ]"
            :aria-label="classificationMeta(item.classification).label"
            role="img"
            v-tooltip.left="classificationMeta(item.classification).label"
          />
        </span>
      </button>
    </li>
  </ul>
</template>

<style scoped>
.analysis-list { display: flex; flex-direction: column; gap: var(--aw-space-2); margin: 0; padding: 0; list-style: none; }

.row {
  width: 100%;
  text-align: left;
  padding: 0.55rem 0.65rem;
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

.row-head { display: flex; align-items: flex-start; gap: 0.45rem; min-width: 0; }
.row-title {
  display: -webkit-box;
  flex: 1 1 auto;
  min-width: 0;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: var(--aw-text-sm);
  font-weight: 400;
  line-height: 1.3;
}
.row-status { display: inline-grid; flex: 0 0 auto; width: 1.6rem; height: 1.6rem; place-items: center; border-radius: 50%; font-size: var(--aw-text-sm); }
.row-status.tone-secondary { color: var(--aw-muted); background: var(--aw-raised); }
.row-status.tone-success { color: var(--aw-ok); background: var(--aw-ok-soft); }
.row-status.tone-warn { color: var(--aw-warn); background: var(--aw-warn-soft); }
.row-status.tone-danger { color: var(--aw-danger); background: var(--aw-danger-soft); }
.row-status.tone-info { color: var(--aw-teal); background: var(--aw-teal-soft); }
</style>
