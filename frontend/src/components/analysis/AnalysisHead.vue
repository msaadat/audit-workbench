<script setup lang="ts">
import { computed } from 'vue'

import type { SavedAnalysis } from '../../types'
import { provenance } from './classification'

/**
 * What this procedure is, at the top of the detail — the data test's head, for
 * the same reason.
 *
 * The pane used to open on an unlabelled `InputText` holding the title, with
 * two tags and four buttons beside it, so the first thing a reader met was a
 * form control rather than the thing they had clicked. Identity is read here;
 * the title is edited with the rest of the definition, where changing it is
 * one edit among the edits that change what the procedure does.
 */

const props = defineProps<{
  analysis: SavedAnalysis | null
  /** Shown while creating, where there is no record to name yet. */
  placeholder?: string
}>()

const origin = computed(() => (props.analysis ? provenance(props.analysis) : null))
</script>

<template>
  <header class="detail-head">
    <div class="detail-copy">
      <p v-if="analysis" class="detail-id">
        <span class="id">{{ analysis.id }}</span>
        <span v-if="origin" class="sep" aria-hidden="true">·</span>
        <span v-if="origin">{{ origin.label }}</span>
        <template v-if="analysis.table">
          <span class="sep" aria-hidden="true">·</span>
          <span class="table">{{ analysis.table }}</span>
        </template>
      </p>
      <h2>{{ analysis?.title || placeholder || 'New procedure' }}</h2>
      <p v-if="analysis?.note" class="objective">{{ analysis.note }}</p>
    </div>
    <slot name="actions" />
  </header>
</template>

<style scoped>
.detail-head { display: flex; align-items: flex-start; gap: 1rem; min-width: 0; }
.detail-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0; }
.detail-id {
  display: flex; align-items: center; gap: .35rem; flex-wrap: wrap;
  margin: 0; color: var(--aw-muted);
  font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600;
}
.detail-id .id { color: var(--aw-ink-soft); }
.detail-id .sep { color: var(--aw-border-strong); }
.detail-head h2 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: 600; letter-spacing: -0.01em; }
.objective { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.45; }
.detail-head :deep(.p-button) { white-space: nowrap; }
</style>
