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
</script>

<template>
  <div v-if="visible.length" class="narration">
    <div v-for="(entry, index) in visible" :key="`${entry.at}:${index}`" class="line" :class="{ current: active && index === lastIndex }">
      <i :class="active && index === lastIndex ? 'pi pi-spin pi-spinner' : entry.kind === 'stage_settled' ? 'pi pi-check' : 'pi pi-angle-right'" />
      <span>{{ entry.text }}</span>
    </div>
  </div>
</template>

<style scoped>
.narration{display:grid;gap:.2rem;padding:.1rem 0 .15rem .1rem;border-left:2px solid var(--aw-border);padding-left:.55rem;margin:.1rem 0 .35rem}
.line{display:grid;grid-template-columns:.85rem minmax(0,1fr);gap:.4rem;align-items:baseline;font-size:.71rem;line-height:1.45;color:var(--aw-muted)}
.line>i{font-size:.62rem}
.line.current{color:var(--aw-ink,inherit)}
.line.current>i{color:var(--aw-teal)}
</style>
