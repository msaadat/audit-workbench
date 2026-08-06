<script setup lang="ts">
export interface TriageCount {
  key: string
  label: string
  value: number
  tone?: 'danger' | 'warn' | 'ok' | 'info' | 'muted'
}

defineProps<{ counts: TriageCount[]; active?: string | null }>()
defineEmits<{ select: [key: string] }>()
</script>

<template>
  <!-- One row of count chips rather than a band of cards. The counts are a
       filter, not a dashboard: the numbers still need to be scannable, but
       they do not need 90px of vertical space on every fieldwork screen. -->
  <div class="triage" role="group" aria-label="Filter by outcome">
    <button
      v-for="count in counts"
      :key="count.key"
      type="button"
      class="triage-chip"
      :data-tone="count.tone ?? 'muted'"
      :class="{ active: active === count.key }"
      :aria-pressed="active === count.key"
      :disabled="count.value === 0 && active !== count.key"
      @click="$emit('select', count.key)"
    >
      <span v-if="count.tone && count.tone !== 'muted'" class="dot" aria-hidden="true" />
      <span class="label">{{ count.label }}</span>
      <span class="value aw-figure">{{ count.value }}</span>
    </button>
  </div>
</template>

<style scoped>
.triage { display: flex; flex-wrap: wrap; gap: var(--aw-space-2); min-width: 0; }
.triage-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  min-height: var(--aw-control-height-sm);
  padding: 0.25rem 0.7rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-panel);
  color: var(--aw-muted);
  font: inherit;
  font-size: var(--aw-text-xs);
  white-space: nowrap;
  cursor: pointer;
  transition: border-color .15s, background .15s, color .15s;
}
.triage-chip:hover:not(:disabled) { border-color: var(--aw-border-strong); background: var(--aw-raised); }
.triage-chip.active { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); color: var(--aw-teal); }
/* A count of zero is a filter that can only produce an empty list, so it stays
   readable as a count but stops being a dead end to click into. */
.triage-chip:disabled { cursor: default; opacity: 0.55; }

.triage-chip .value { color: var(--aw-ink); font-weight: 700; }
.triage-chip.active .value { color: var(--aw-teal); }
.triage-chip:disabled .value { color: var(--aw-muted); font-weight: 600; }

.dot { width: 0.4rem; height: 0.4rem; border-radius: var(--aw-radius-pill); background: var(--aw-muted); }
.triage-chip[data-tone='danger'] .dot { background: var(--aw-danger); }
.triage-chip[data-tone='warn'] .dot { background: var(--aw-warn); }
.triage-chip[data-tone='ok'] .dot { background: var(--aw-ok); }
.triage-chip[data-tone='info'] .dot { background: var(--aw-teal); }
</style>
