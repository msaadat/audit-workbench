<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import Textarea from 'primevue/textarea'

import type { AssistantCapabilities, AuditDocument } from '../../types'
import type { AgentMode } from '../../composables/useAgentRun'

const props = defineProps<{
  busy: boolean
  capabilities: AssistantCapabilities | null
  mode: AgentMode
  documents: AuditDocument[]
  selectedIds: string[]
}>()
const emit = defineEmits<{
  send: [string]
  'update:mode': [AgentMode]
  documents: []
  removeDocument: [string]
}>()
const draft = ref('')
const modeOptions = [
  { label: 'Auto', value: 'auto', hint: 'Apply workspace changes automatically' },
  { label: 'Ask', value: 'permission', hint: 'Ask for approval before changes' },
]
const canSend = computed(() => {
  if (!draft.value.trim() || props.busy) return false
  return Boolean(props.capabilities?.ask || props.capabilities?.act)
})
const placeholder = computed(() => {
  // Capabilities are unknown while the chat is still loading; only a loaded
  // "nothing configured" state should show the configuration hint.
  if (props.capabilities && !props.capabilities.ask && !props.capabilities.act) return 'Configure an assistant or agent model first'
  return 'Ask a question or request an action…'
})
function submit() { const value = draft.value.trim(); if (!value || !canSend.value) return; draft.value = ''; emit('send', value) }
function keydown(event: KeyboardEvent) {
  // isComposing: Enter that commits an IME composition must not send.
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit() }
}
function label(id: string) { return props.documents.find(doc => doc.id === id)?.title ?? id }
</script>

<template>
  <div class="composer">
    <div v-if="selectedIds.length" class="chips">
      <span v-for="id in selectedIds" :key="id"><i class="pi pi-file" />{{ label(id) }}<button :aria-label="`Remove ${label(id)}`" @click="emit('removeDocument', id)"><i class="pi pi-times" /></button></span>
    </div>
    <!-- Stays enabled while busy so the next message can be drafted during a long reply. -->
    <Textarea v-model="draft" rows="2" autoResize :placeholder="placeholder" @keydown="keydown" />
    <div class="toolbar">
      <Button icon="pi pi-paperclip" text size="small" severity="secondary" aria-label="Attach documents" @click="emit('documents')" />
      <SelectButton class="mode" :modelValue="mode" :options="modeOptions" optionLabel="label" optionValue="value" :allowEmpty="false" size="small" @update:modelValue="emit('update:mode', $event as AgentMode)">
        <template #option="{ option }"><span :title="option.hint">{{ option.label }}</span></template>
      </SelectButton>
      <span class="grow" />
      <Button icon="pi pi-send" size="small" aria-label="Send" :loading="busy" :disabled="!canSend" @click="submit" />
    </div>
  </div>
</template>

<style scoped>
.composer{display:grid;gap:.4rem;padding:.55rem .75rem;border-top:1px solid var(--aw-border);background:var(--p-surface-0)}.composer :deep(textarea){width:100%;font-size:.8rem;max-height:9rem}.toolbar{display:flex;align-items:center;gap:.3rem}.grow{flex:1}.toolbar :deep(.p-togglebutton){padding:.27rem .42rem;font-size:.64rem}.mode{max-width:10rem}.chips{display:flex;gap:.3rem;overflow:auto}.chips span{display:flex;align-items:center;gap:.25rem;flex:0 0 auto;max-width:11rem;padding:.25rem .4rem;border-radius:999px;background:var(--aw-teal-soft);color:var(--aw-teal);font-size:.65rem}.chips span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.chips button{border:0;background:transparent;color:inherit;cursor:pointer;padding:0}@media(max-width:520px){.toolbar :deep(.p-togglebutton){padding-inline:.3rem}}
</style>
