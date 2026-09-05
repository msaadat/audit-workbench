<script setup lang="ts">
import type { SavedAnalysis } from '../../types'
import { classificationMeta, freshnessMeta } from './classification'
import { classificationTone, foundSummary } from './analysisStatus'

/**
 * One line of title and one of fact, per procedure — the Data Tests row, for
 * the same kind of list of the same kind of thing.
 *
 * It was the title clamped to two lines and a status glyph whose only label
 * was a tooltip, so a reader had to hover thirty rows to find the one that
 * failed. The dot carries the outcome and the meta line says it in words.
 *
 * The outcome leads the meta line rather than the frame, because a joined
 * frame here is named `invoice_data_po_data_joined` — 27 characters that would
 * push the one fact that ranks the row off the end of a 300 px column.
 *
 * Freshness is a marker at the end rather than a word in the line. It is a
 * third state a data test does not have — a result that stands but was
 * recorded against a definition that has since changed — and it is orthogonal
 * to what the result said, so it must not compete for the same words.
 */

defineProps<{ items: SavedAnalysis[]; selectedId: string | null }>()
defineEmits<{ select: [analysis: SavedAnalysis] }>()
</script>

<template>
  <div class="list" role="listbox" aria-label="Saved analysis procedures">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      role="option"
      :aria-selected="item.id === selectedId"
      class="row"
      :class="{ active: item.id === selectedId }"
      @click="$emit('select', item)"
    >
      <span class="dot" :data-tone="classificationTone(item.classification)" aria-hidden="true" />
      <span class="copy">
        <span class="title">{{ item.title }}</span>
        <span class="meta aw-figure">
          {{ foundSummary(item) }}<template v-if="item.table"> · {{ item.table }}</template>
        </span>
      </span>
      <i
        v-if="freshnessMeta(item.state)"
        class="mark"
        :class="freshnessMeta(item.state)!.icon"
        :aria-label="freshnessMeta(item.state)!.label"
        role="img"
        v-tooltip.left="freshnessMeta(item.state)!.label"
      />
      <span class="sr-only">{{ classificationMeta(item.classification).label }}</span>
    </button>
    <p v-if="!items.length" class="empty">No procedure matches this view.</p>
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

.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.title { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-base); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }

.mark { flex: none; color: var(--aw-warn); font-size: var(--aw-text-sm); }

.sr-only {
  position: absolute; width: 1px; height: 1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;
}

.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
