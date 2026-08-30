<script setup lang="ts">
import type { DataTest, RcmRow } from '../../types'
import UiTestStatus from '../ui/UiTestStatus.vue'

defineProps<{ tests: DataTest[]; selectedId: string | null; rcmRows: RcmRow[] }>()
defineEmits<{ select: [test: DataTest] }>()
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
        <span class="row-title">{{ test.title }}</span>
        <UiTestStatus :status="test.status" />
      </span>
    </button>
    <p v-if="!tests.length" class="empty">No data test matches this filter.</p>
  </div>
</template>

<style scoped>
.list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.55rem; min-width: 0; }
.row {
  min-width: 0;
  padding: 0.5rem 0.6rem;
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

.row-head { display: flex; align-items: flex-start; gap: 0.45rem; min-width: 0; }
.row-title { display: -webkit-box; flex: 1 1 auto; overflow: hidden; min-width: 0; font-size: var(--aw-text-sm); font-weight: 400; line-height: 1.3; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.empty { padding: 1rem 0.6rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
