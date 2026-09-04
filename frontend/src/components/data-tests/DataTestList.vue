<script setup lang="ts">
import type { DataTest } from '../../types'

defineProps<{ tests: DataTest[]; selectedId: string | null }>()
defineEmits<{ select: [test: DataTest] }>()

/**
 * One line of title and one of fact, per test.
 *
 * The status icon chip and its tooltip are gone: the dot and the meta line say
 * the same thing, and the line says it in words rather than asking the reader
 * to hover a glyph. What the meta carries is what distinguishes one row from
 * the next in a list of thirty — which table, how much failed, how much of it
 * is still open.
 */

const TONES: Record<string, string> = {
  completed_with_exception: 'bad',
  completed_no_exception: 'ok',
  review_required: 'warn',
  blocked: 'warn',
}
function tone(test: DataTest) { return TONES[test.status] ?? 'neutral' }
function hasWarning(test: DataTest) {
  return test.semantic_warnings.length > 0 || test.last_run?.semantic_valid === false
}
/**
 * The frame the test runs over, where the definition names one.
 *
 * A generated Polars test names its tables inside its step code rather than in
 * `table_refs`, so most of an agent-written programme has none. Leading every
 * row with "no table" would make the least useful word in the line the first
 * one read; the outcome leads instead.
 */
function table(test: DataTest) { return test.table_refs[0] || '' }
</script>

<template>
  <div class="list">
    <button
      v-for="test in tests"
      :key="test.id"
      type="button"
      class="row"
      :class="{ active: test.id === selectedId }"
      @click="$emit('select', test)"
    >
      <span class="dot" :data-tone="tone(test)" aria-hidden="true" />
      <span class="copy">
        <span class="title">{{ test.title }}</span>
        <span class="meta aw-figure">
          <template v-if="table(test)">{{ table(test) }} · </template>
          <template v-if="!test.last_run">not run</template>
          <template v-else-if="test.evaluation.exception_count">
            {{ test.evaluation.exception_count }} failed<template v-if="test.open_exception_count">
              · <span class="open">{{ test.open_exception_count }} open</span></template>
          </template>
          <template v-else>no exceptions</template>
          <template v-if="hasWarning(test)"> · <span class="warning">warning</span></template>
        </span>
      </span>
    </button>
    <p v-if="!tests.length" class="empty">No data test matches this filter.</p>
  </div>
</template>

<style scoped>
.list { display: flex; flex-direction: column; min-width: 0; }
.row {
  display: flex; align-items: center; gap: .625rem;
  width: 100%; min-width: 0;
  padding: .625rem .75rem;
  border: 0; border-top: 1px solid var(--aw-border); border-left: 3px solid transparent;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.row:first-child { border-top: 0; }
.row:hover:not(.active) { background: var(--aw-raised); }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft); }

.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }

.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.title { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-base); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.meta .open { color: var(--aw-danger); }
.meta .warning { color: var(--aw-warn-ink); }

.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
