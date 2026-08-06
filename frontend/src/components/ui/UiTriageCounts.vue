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
  <div class="triage" role="group" aria-label="Filter by outcome">
    <button
      v-for="count in counts"
      :key="count.key"
      type="button"
      class="triage-card"
      :data-tone="count.tone ?? 'muted'"
      :class="{ active: active === count.key }"
      :aria-pressed="active === count.key"
      @click="$emit('select', count.key)"
    >
      <strong>{{ count.value }}</strong>
      <span>{{ count.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.triage {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr));
  gap: 0.55rem;
}
.triage-card {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  padding: 0.6rem 0.7rem;
  border: 1px solid var(--aw-border);
  border-top: 3px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color .15s, background .15s;
}
.triage-card strong { font-size: var(--aw-text-xl); line-height: 1.1; }
.triage-card span { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.triage-card:hover { background: var(--aw-raised); }
.triage-card.active { background: var(--aw-teal-soft); border-color: var(--aw-teal); }

.triage-card[data-tone='danger'] { border-top-color: var(--aw-danger); }
.triage-card[data-tone='warn'] { border-top-color: var(--aw-warn); }
.triage-card[data-tone='ok'] { border-top-color: var(--aw-ok); }
.triage-card[data-tone='info'] { border-top-color: var(--aw-teal); }
</style>
