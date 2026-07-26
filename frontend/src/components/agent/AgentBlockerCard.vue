<script setup lang="ts">
import Button from 'primevue/button'

import type { AgentBlocker } from '../../types'

// A unit that stopped needing a person, asked as a question. Suggestions are
// ordinary chat commands: answering one steers the agent through the same path
// as anything typed into the composer, so a blocker needs no endpoint of its
// own and the answer stays visible in the transcript.
defineProps<{ blocker: AgentBlocker; busy?: boolean }>()
const emit = defineEmits<{ command: [string] }>()

const destinations: Record<string, string> = {
  documents: 'Documents tab',
  'doc-tests': 'Document Tests tab',
  data: 'Data tab',
}
</script>

<template>
  <!-- The machine code stays reachable for support, on hover, without putting
       an identifier back into the reading surface. -->
  <div class="blocker" :class="blocker.severity" :title="blocker.code ?? undefined">
    <div class="head">
      <i :class="blocker.severity === 'failed' ? 'pi pi-times-circle' : blocker.severity === 'review' ? 'pi pi-eye' : 'pi pi-exclamation-triangle'" />
      <p>{{ blocker.message }}</p>
    </div>
    <small v-if="blocker.where" class="where">Review it in the {{ destinations[blocker.where] ?? blocker.where }}.</small>
    <div v-if="blocker.suggestions.length" class="choices">
      <Button
        v-for="suggestion in blocker.suggestions"
        :key="suggestion.label"
        :label="suggestion.label"
        size="small"
        severity="secondary"
        outlined
        :disabled="busy"
        @click="emit('command', suggestion.command)"
      />
    </div>
  </div>
</template>

<style scoped>
.blocker{display:grid;gap:.45rem;padding:.65rem .7rem;border:1px solid var(--p-amber-300);border-radius:.55rem;background:var(--p-amber-50)}
.blocker.failed{border-color:var(--p-red-300);background:var(--p-red-50)}
.blocker.review{border-color:var(--aw-border);background:var(--p-surface-50)}
.head{display:grid;grid-template-columns:1rem minmax(0,1fr);gap:.5rem;align-items:start}
.head>i{margin-top:.12rem;color:var(--p-amber-600)}
.blocker.failed .head>i{color:var(--p-red-500)}
.blocker.review .head>i{color:var(--aw-muted)}
.head p{margin:0;font-size:.79rem;line-height:1.45}
.where{font-size:.68rem;color:var(--aw-muted)}
.choices{display:flex;flex-wrap:wrap;gap:.35rem}
</style>
