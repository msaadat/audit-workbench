<script setup lang="ts">
import { ref } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'

defineProps<{ modelValue: boolean; current: string; generated: string; busy?: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  choose: ['keep' | 'replace']
}>()
const currentPane = ref<HTMLElement | null>(null)
const generatedPane = ref<HTMLElement | null>(null)
let syncing = false

function synchronizeScroll(targetPane: HTMLElement | null, event: Event) {
  if (syncing) return
  const source = event.target as HTMLTextAreaElement
  const target = targetPane?.querySelector('textarea')
  if (!target) return
  syncing = true
  const sourceRange = Math.max(1, source.scrollHeight - source.clientHeight)
  const targetRange = Math.max(0, target.scrollHeight - target.clientHeight)
  target.scrollTop = (source.scrollTop / sourceRange) * targetRange
  target.scrollLeft = source.scrollLeft
  requestAnimationFrame(() => { syncing = false })
}
</script>

<template>
  <Dialog :visible="modelValue" modal header="Reconcile regenerated report" :style="{ width: 'min(94vw, 78rem)' }" @update:visible="emit('update:modelValue', $event)">
    <p class="notice"><i class="pi pi-info-circle"/> The report contains auditor edits. Compare both versions and make an explicit choice; no automatic merge is performed.</p>
    <div class="reconcile-grid">
      <label ref="currentPane"><span>Current report <Tag value="Auditor edited" severity="info" /></span><Textarea :modelValue="current" readonly rows="24" spellcheck="false" @scroll="synchronizeScroll(generatedPane, $event)" /></label>
      <label ref="generatedPane"><span>Generated report <Tag value="New draft" severity="secondary" /></span><Textarea :modelValue="generated" readonly rows="24" spellcheck="false" @scroll="synchronizeScroll(currentPane, $event)" /></label>
    </div>
    <template #footer>
      <Button label="Keep current" icon="pi pi-user-edit" severity="secondary" outlined :loading="busy" @click="emit('choose', 'keep')"/>
      <Button label="Replace current with generated" icon="pi pi-sparkles" :loading="busy" @click="emit('choose', 'replace')"/>
    </template>
  </Dialog>
</template>

<style scoped>
.notice { display:flex; gap:.5rem; padding:.75rem; background:var(--aw-info-soft); color:var(--aw-info); border-radius:var(--aw-radius-control) }.reconcile-grid { display:grid; grid-template-columns:1fr 1fr; gap:1rem }.reconcile-grid label { display:flex; flex-direction:column; gap:.45rem; min-width:0 }.reconcile-grid span { display:flex; align-items:center; justify-content:space-between; gap:.5rem; font-size:var(--aw-text-sm); font-weight:700; color:var(--aw-muted) }.reconcile-grid :deep(textarea) { width:100%; min-height:28rem; resize:vertical; font-family:var(--aw-font-mono, monospace); font-size:var(--aw-text-sm); line-height:1.45 }
@media (max-width:850px) { .reconcile-grid { grid-template-columns:1fr }.reconcile-grid :deep(textarea) { min-height:16rem } }
</style>
