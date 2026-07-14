<script setup lang="ts">
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import Textarea from 'primevue/textarea'

defineProps<{ modelValue: boolean; current: string; generated: string; busy?: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  choose: ['keep' | 'replace']
}>()
</script>

<template>
  <Dialog :visible="modelValue" modal header="Reconcile regenerated report" :style="{ width: 'min(94vw, 78rem)' }" @update:visible="emit('update:modelValue', $event)">
    <p class="notice"><i class="pi pi-info-circle"/> The report contains auditor edits. Compare both versions and make an explicit choice; no automatic merge is performed.</p>
    <div class="reconcile-grid">
      <label><span>Current auditor-edited report</span><Textarea :modelValue="current" readonly rows="24" spellcheck="false" /></label>
      <label><span>Newly generated report</span><Textarea :modelValue="generated" readonly rows="24" spellcheck="false" /></label>
    </div>
    <template #footer>
      <Button label="Keep current" icon="pi pi-user-edit" severity="secondary" outlined :loading="busy" @click="emit('choose', 'keep')"/>
      <Button label="Replace current with generated" icon="pi pi-sparkles" :loading="busy" @click="emit('choose', 'replace')"/>
    </template>
  </Dialog>
</template>

<style scoped>
.notice { display:flex; gap:.5rem; padding:.75rem; background:var(--p-blue-50); color:var(--p-blue-800); border-radius:var(--aw-radius-sm) }.reconcile-grid { display:grid; grid-template-columns:1fr 1fr; gap:1rem }.reconcile-grid label { display:flex; flex-direction:column; gap:.45rem; min-width:0 }.reconcile-grid span { font-size:.78rem; font-weight:700; color:var(--aw-muted) }.reconcile-grid :deep(textarea) { width:100%; min-height:28rem; resize:vertical; font-family:var(--aw-font-mono, monospace); font-size:.76rem; line-height:1.45 }
@media (max-width:850px) { .reconcile-grid { grid-template-columns:1fr }.reconcile-grid :deep(textarea) { min-height:16rem } }
</style>
