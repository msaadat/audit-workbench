<script setup lang="ts">
import { ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Tag from 'primevue/tag'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { IntakeSuggestedAction } from '../types'

const props = defineProps<{
  workspaceId: string
  action: IntakeSuggestedAction
  documentAiEnabled: boolean
}>()
const emit = defineEmits<{ 'planning-started': []; 'settings-changed': [] }>()
const agent = useAgentRun(props.workspaceId)
const { launchMode } = agent

const confirmOpen = ref(false)
const starting = ref(false)
const error = ref('')
const enabledForRun = ref(props.documentAiEnabled)
watch(() => props.documentAiEnabled, value => { enabledForRun.value = value })

async function startPlanningUpdate() {
  starting.value = true
  error.value = ''
  try {
    await agent.refreshStatus()
    if (!agent.state.status?.configured) {
      throw new Error('The planning assistant is not configured. Set an assistant provider and model before running this update.')
    }
    if (!enabledForRun.value) {
      await api.patch(`/api/workspaces/${props.workspaceId}/settings`, { doc_llm_optin: true })
      enabledForRun.value = true
      emit('settings-changed')
    }
    await agent.startCommand(
      launchMode.value,
      `Review newly imported documents ${props.action.document_ids.join(', ')} and update planning context, APM, RCM, and audit procedures.`,
      'planning',
      'tab_button',
    )
    confirmOpen.value = false
    emit('planning-started')
  } catch (cause) {
    error.value = String(cause)
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <article class="next-action">
    <div class="next-action-icon"><i class="pi pi-sparkles" /></div>
    <div>
      <Tag value="Recommended next step" severity="info" />
      <h4>{{ action.title }}</h4>
      <p>{{ action.reason }}</p>
      <small>
        {{ action.documents.map(document => document.title).join(', ') }}
        <template v-if="action.omitted_document_count"> and {{ action.omitted_document_count }} more</template>
      </small>
    </div>
    <Button
      :label="enabledForRun ? 'Review and run planning' : 'Enable document AI and review'"
      icon="pi pi-arrow-right"
      iconPos="right"
      @click="confirmOpen = true"
    />
  </article>

  <Dialog v-model:visible="confirmOpen" modal header="Update planning from these documents?" :style="{ width: 'min(38rem, 94vw)' }" :closable="!starting">
    <div class="planning-confirmation">
      <p>
        The planning assistant will use the selected documents to propose updates to the
        engagement context, APM, RCM, and audit program. It will use the global
        {{ launchMode }} mode selected in the audit assistant.
      </p>
      <div class="disclosure-preview">
        <strong><i class="pi pi-cloud-upload" /> Document disclosure</strong>
        <p>Purpose: planning update · up to the first 8 extracted pages per document.</p>
        <ul><li v-for="document in action.documents" :key="document.id">{{ document.title }} <small>({{ document.category }})</small></li></ul>
        <p>Every disclosed page and model response is logged. Structured data rows remain local.</p>
      </div>
      <p v-if="!enabledForRun" class="optin-note"><i class="pi pi-lock" /> Document AI is currently off. Continuing explicitly enables it for this engagement.</p>
      <p v-if="agent.state.status && !agent.state.status.configured" class="optin-note"><i class="pi pi-key" /> Configure an assistant provider and model before running this update.</p>
      <p v-if="error" class="inline-error"><i class="pi pi-exclamation-triangle" />{{ error }}</p>
    </div>
    <template #footer>
      <Button label="Not now" severity="secondary" text :disabled="starting" @click="confirmOpen = false" />
      <Button :label="enabledForRun ? 'Run planning assistant' : 'Enable and run'" icon="pi pi-play" :loading="starting" @click="startPlanningUpdate" />
    </template>
  </Dialog>
</template>

<style scoped>
.next-action { display: grid; grid-template-columns: auto minmax(0,1fr) auto; gap: .85rem; align-items: center; margin: 1rem 0; padding: 1rem; border: 1px solid #b7d7ef; border-radius: 9px; background: var(--p-blue-50); text-align: left; }
.next-action-icon { display: grid; place-items: center; width: 2.5rem; height: 2.5rem; border-radius: 50%; background: #fff; color: var(--aw-teal); }
.next-action h4 { margin: .35rem 0 .2rem; }.next-action p { margin: 0 0 .25rem; color: var(--p-surface-600); }.next-action small { color: var(--p-surface-500); }
.planning-confirmation { display: grid; gap: .8rem; }.planning-confirmation > p { margin: 0; line-height: 1.5; }
.disclosure-preview { padding: .85rem; border: 1px solid #f0cf9f; border-radius: 8px; background: var(--aw-warn-soft); }.disclosure-preview strong { display: flex; align-items: center; gap: .45rem; }.disclosure-preview p { margin: .4rem 0; }.disclosure-preview ul { margin: .55rem 0; padding-left: 1.25rem; }.disclosure-preview li { margin: .2rem 0; }
.optin-note { display: flex; gap: .5rem; padding: .7rem; border-radius: 7px; background: var(--p-surface-100); color: var(--p-surface-600); }.optin-note i { color: var(--aw-teal); }
.inline-error { display: flex; gap: .45rem; padding: .65rem; color: var(--p-red-700); background: var(--p-red-50); border-radius: 7px; }
@media (max-width: 700px) { .next-action { grid-template-columns: auto 1fr; }.next-action > .p-button { grid-column: 1 / -1; } }
</style>
