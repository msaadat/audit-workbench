<script setup lang="ts">
import { computed } from 'vue'

import type { AgentNarrationEntry } from '../../types'

// The live "what I'm doing" log. It accumulates rather than replacing itself,
// which is the whole difference between watching an agent work and watching a
// progress counter tick.
const props = defineProps<{ entries: AgentNarrationEntry[]; active?: boolean; limit?: number }>()

const visible = computed(() => {
  const limit = props.limit ?? (props.active ? 6 : 4)
  return props.entries.slice(-limit)
})
const lastIndex = computed(() => visible.value.length - 1)

// A settled stage used to draw the same tick whatever became of it, so a
// blocked stage and a finished one were indistinguishable at a glance. Keyed on
// the status the entry now carries; entries written before it was recorded have
// none, and keep the tick they were shown with.
const SETTLED_ICONS: Record<string, string> = {
  failed: 'pi pi-times-circle',
  cancelled: 'pi pi-ban',
  blocked: 'pi pi-lock',
  review_required: 'pi pi-pause-circle',
  skipped: 'pi pi-minus-circle',
}

function icon(entry: AgentNarrationEntry, current: boolean) {
  if (current) return 'pi pi-spin pi-spinner'
  // A repair is work being redone, not progress being made. It reads as its own
  // kind of line so a reader can tell a stage that struggled from one that ran.
  if (entry.kind === 'repair') return 'pi pi-wrench'
  if (entry.kind !== 'stage_settled') return 'pi pi-angle-right'
  return SETTLED_ICONS[entry.status ?? ''] ?? 'pi pi-check'
}

function tone(entry: AgentNarrationEntry) {
  const status = entry.kind === 'stage_settled' ? entry.status ?? '' : ''
  if (status === 'failed' || status === 'cancelled') return 'bad'
  if (status === 'blocked' || status === 'review_required') return 'gate'
  return ''
}
</script>

<template>
  <div v-if="visible.length" class="narration">
    <div v-for="(entry, index) in visible" :key="`${entry.at}:${index}`" class="line" :class="[tone(entry), { current: active && index === lastIndex }]">
      <i :class="icon(entry, !!active && index === lastIndex)" />
      <span>{{ entry.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.narration{display:grid;gap:.2rem;padding:.1rem 0 .15rem .1rem;border-left:2px solid var(--aw-border);padding-left:.55rem;margin:.1rem 0 .35rem}
.line{display:grid;grid-template-columns:.85rem minmax(0,1fr);gap:.4rem;align-items:baseline;font-size:var(--aw-text-xs);line-height:1.45;color:var(--aw-muted)}
.line>i{font-size:var(--aw-text-2xs)}
.line.current{color:var(--aw-ink,inherit)}
.line.current>i{color:var(--aw-teal)}
.line.bad>i{color:var(--aw-danger)}
.line.gate>i{color:var(--aw-warn)}
</style>
