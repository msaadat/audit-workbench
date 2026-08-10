<script setup lang="ts">
import type { DataTest, RcmRow } from '../../types'
import UiTestStatus from '../ui/UiTestStatus.vue'

const props = defineProps<{ tests: DataTest[]; selectedId: string | null; rcmRows: RcmRow[] }>()
defineEmits<{ select: [test: DataTest] }>()

// The rail used to read "polars · RCM-104366". Neither half told the auditor
// anything: the engine is an implementation detail and the id is opaque.
function context(test: DataTest) {
  if (!test.rcm_id) return 'Exploratory — not RCM coverage'
  const row = props.rcmRows.find(item => item.id === test.rcm_id)
  return row?.risk || test.rcm_id
}
</script>

<template>
  <div class="list">
    <button
      v-for="test in tests"
      :key="test.id"
      type="button"
      class="row"
      :data-status="test.status"
      :class="{ active: test.id === selectedId }"
      @click="$emit('select', test)"
    >
      <span class="row-head">
        <strong>{{ test.title }}</strong>
        <UiTestStatus :status="test.status" showLabel />
      </span>
      <small class="row-context" :class="{ exploratory: !test.rcm_id }">{{ context(test) }}</small>
      <!-- "N exceptions · N open" restated one number as two. Nothing in a Data
           Test ever dispositions an exception, so the second was always a copy. -->
      <small v-if="test.exception_count" class="row-meta">
        {{ test.exception_count }} exception row(s)
      </small>
    </button>
    <p v-if="!tests.length" class="empty">No data test matches this filter.</p>
  </div>
</template>

<style scoped>
.list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.55rem; min-width: 0; }
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
.row[data-status='completed_with_exception'] { border-left-color: var(--aw-danger); }
.row[data-status='completed_no_exception'] { border-left-color: var(--aw-ok); }
.row[data-status='review_required'], .row[data-status='blocked'] { border-left-color: var(--aw-warn); }

.row-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.4rem; min-width: 0; }
.row-head strong { min-width: 0; font-size: var(--aw-text-sm); line-height: 1.3; }
.row-context { display: -webkit-box; overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.row-context.exploratory { font-style: italic; }
.row-meta { color: var(--aw-danger); font-size: var(--aw-text-xs); font-weight: 600; }
.empty { padding: 1rem 0.6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
