<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import { useAssistantChat } from '../../composables/useAssistantChat'
import type { AgentApproval, AgentDecision, AgentInteraction, AssistantChat, AssistantMessageIntent, AssistantSuggestion, AuditDocument, WorkspaceSummary } from '../../types'
import ChatComposer from './ChatComposer.vue'
import ChatHistoryPanel from './ChatHistoryPanel.vue'
import ChatTranscript from './ChatTranscript.vue'
import DocumentContextPicker from './DocumentContextPicker.vue'

/**
 * The assistant thread, independent of the frame around it. The console renders
 * it at full content width; `AgentDrawer` renders the same component inside a
 * resizable sidecar. Both observe the same workspace-scoped run and chat stores,
 * so a run started in one is live in the other.
 */

const props = defineProps<{ workspace: WorkspaceSummary; dockedHistory?: boolean }>()
const toast = useToast()
const confirm = useConfirm()
const agent = useAgentRun(props.workspace.id)
const chats = useAssistantChat(props.workspace.id)
const showHistory = ref(false)
const pickerOpen = ref(false)
const actionBusy = ref(false)
const renameOpen = ref(false)
const renameTitle = ref('')
const documents = ref<AuditDocument[]>([])
const mode = agent.launchMode

const activeChat = computed(() => chats.state.chat)
const status = computed(() => activeChat.value?.active_workspace_run?.status ?? '')
const runActive = computed(() => agent.isActive.value)
const displayChat = computed<AssistantChat | null>(() => {
  const chat = activeChat.value
  const global = chat?.active_workspace_run
  if (!chat || !global || chat.runs.some(item => item.run_id === global.run_id)) return chat
  // A run owned by another chat is appended at the bottom, next to the
  // composer, and flagged so its card shows its own attention items.
  return { ...chat, transcript: [...chat.transcript, { ...global, foreign: true }] }
})

onMounted(async () => {
  await Promise.all([agent.init(), chats.init(), loadDocuments()]).catch(error => fail('Could not open the assistant', error))
})

watch(() => [
  agent.state.run?.id,
  agent.state.run?.status,
  agent.state.run?.activity_revision ?? 0,
  agent.state.run?.graph_revision ?? 0,
  agent.state.run?.warnings.length ?? 0,
  agent.state.run?.pending_commands?.length ?? 0,
  // Narration and the agent's own turns are the live content of the transcript,
  // so they have to pull a chat refresh the same way status changes do.
  agent.state.run?.narration?.length ?? 0,
  agent.state.run?.milestones?.length ?? 0,
  agent.state.run?.messages?.length ?? 0,
].join(':'), () => {
  if (chats.state.activeChatId) void chats.refresh()
})

async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function send(content: string, sendIntent: AssistantMessageIntent = 'auto', command?: string, source: 'composer'|'shortcut'|'tab_button'|'folder_intake' = 'composer', requestedOutcomes?: string[]) {
  try {
    await chats.send(content, sendIntent, mode.value, { command, source, requestedOutcomes })
  } catch (error) { fail('Message failed', error) }
}
function nextStep(suggestion: AssistantSuggestion) {
  void send(suggestion.command, 'act', undefined, 'shortcut', suggestion.requested_outcomes)
}
function stopRun() {
  confirm.require({
    header: 'Stop the run',
    message: 'Stop the agent here? Work already committed to the workspace is kept.',
    icon: 'pi pi-stop-circle',
    acceptProps: { label: 'Stop', severity: 'danger' },
    rejectProps: { label: 'Keep going', severity: 'secondary', outlined: true },
    accept: async () => {
      try { await agent.cancel() } catch (error) { fail('Could not stop the run', error) }
    },
  })
}
function shortcut(label: string, commandId: string) {
  const text = commandId === 'full_audit' ? 'Prepare a full audit working draft.' : `Start ${label.toLowerCase()} work for this engagement.`
  void send(text, 'act', commandId, 'shortcut')
}
function rename() {
  renameTitle.value = activeChat.value?.title ?? ''
  renameOpen.value = true
}
async function saveRename() {
  const title = renameTitle.value.trim()
  if (!title) return
  renameOpen.value = false
  try { await chats.rename(title) } catch (error) { fail('Rename failed', error) }
}
function remove() {
  confirm.require({
    header: 'Delete chat',
    message: 'Delete this chat and its local Q&A artifacts? Linked runs and workspace work products will remain.',
    icon: 'pi pi-trash',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await chats.deleteActive()
        showHistory.value = false
      } catch (error) { fail('Delete failed', error) }
    },
  })
}
async function applyDocuments(values: AuditDocument[]) { await chats.setDocuments(values) }
async function respond(runId: string, interaction: AgentInteraction, response: Record<string, unknown>) {
  actionBusy.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/agent/runs/${runId}/interactions/${interaction.id}/respond`, response)
    await chats.refresh()
  } catch (error) { fail('Response failed', error) }
  finally { actionBusy.value = false }
}
async function decide(runId: string, approval: AgentApproval, decisions: AgentDecision[]) {
  actionBusy.value = true
  try {
    await api.post(`/api/workspaces/${props.workspace.id}/agent/runs/${runId}/approvals/${approval.id}`, { decisions })
    await chats.refresh()
  } catch (error) { fail('Decision failed', error) }
  finally { actionBusy.value = false }
}
function fail(summary: string, error: unknown) { toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6500 }) }
// The console docks the chat list in its own left pane instead of this
// dropdown, but rename/remove stay owned here — the dialogs they need live in
// this template either way.
function toggleHistory() { if (!props.dockedHistory) showHistory.value = !showHistory.value }
defineExpose({ rename, remove })
</script>

<template>
  <div class="console-thread">
    <header class="thread-head">
      <i class="pi pi-sparkles" />
      <button class="title" :class="{ static: dockedHistory }" @click="toggleHistory"><strong>{{ activeChat?.title ?? 'Audit assistant' }}</strong><i v-if="!dockedHistory" class="pi pi-chevron-down" /></button>
      <Tag v-if="status" :value="status.replaceAll('_',' ')" :severity="['failed'].includes(status) ? 'danger' : ['awaiting_approval','awaiting_input','interrupted'].includes(status) ? 'warn' : 'info'" />
      <span v-if="runActive" class="live-dot" :class="{ off: !agent.state.connected }" :title="agent.state.connected ? 'Live updates connected' : 'Live updates reconnecting…'" />
      <span class="grow" />
      <Button icon="pi pi-plus" text size="small" severity="secondary" aria-label="New chat" @click="chats.createChat()" />
      <slot name="head-actions" />
    </header>
    <ChatHistoryPanel v-if="showHistory && !dockedHistory" :chats="chats.state.summaries" :activeId="chats.state.activeChatId" @select="chats.switchChat($event); showHistory = false" @create="chats.createChat(); showHistory = false" @rename="rename" @remove="remove" @close="showHistory = false" />
    <div v-if="chats.state.loading && !displayChat" class="loading"><i class="pi pi-spin pi-spinner" /> Loading chat…</div>
    <ChatTranscript v-else-if="displayChat" :workspaceId="workspace.id" :chat="displayChat" :documents="documents" :actionBusy="actionBusy" :busy="chats.state.busy" @shortcut="shortcut" @suggestion="nextStep" @command="send($event, 'act')" @retry="chats.retry($event, mode).catch(error => fail('Message failed', error))" @changed="chats.refresh" @respond="respond" @decide="decide" />
    <ChatComposer
      v-if="activeChat"
      v-model:mode="mode"
      :busy="chats.state.busy"
      :capabilities="chats.state.capabilities"
      :documents="documents"
      :selectedIds="activeChat.composer_context.document_ids"
      :runActive="runActive"
      @send="send"
      @documents="pickerOpen = true"
      @removeDocument="chats.removeDocument"
      @stop="stopRun"
    />
  </div>

  <DocumentContextPicker
    v-model:visible="pickerOpen"
    :workspaceId="workspace.id"
    :selectedIds="activeChat?.composer_context.document_ids ?? []"
    @apply="applyDocuments"
  />

  <Dialog v-model:visible="renameOpen" modal header="Rename chat" :style="{ width: '22rem' }">
    <InputText v-model="renameTitle" class="rename-input" autofocus maxlength="120" @keydown.enter="saveRename" />
    <template #footer>
      <Button label="Cancel" severity="secondary" outlined size="small" @click="renameOpen = false" />
      <Button label="Save" size="small" :disabled="!renameTitle.trim()" @click="saveRename" />
    </template>
  </Dialog>
</template>

<style scoped>
/* Positioned so the chat-history panel, which pins itself below the head, has a
   containing block in both the console and the drawer. */
.console-thread{position:relative;flex:1;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden;background:var(--aw-panel)}
.thread-head{display:flex;align-items:center;gap:.45rem;min-height:3.1rem;padding:.55rem .7rem;border-bottom:1px solid var(--aw-border)}
.thread-head>i{color:var(--aw-teal)}
.title{display:flex;align-items:center;gap:.3rem;min-width:0;padding:.2rem;border:0;background:transparent;color:inherit;cursor:pointer}
.title.static{cursor:default}
.title strong{max-width:12rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:var(--aw-text-sm)}
.title i{font-size:var(--aw-text-2xs)}
.grow{flex:1}
.live-dot{width:.5rem;height:.5rem;border-radius:50%;background:var(--aw-ok)}
.live-dot.off{background:var(--aw-warn)}
.loading{display:grid;place-content:center;place-items:center;gap:.4rem;flex:1;color:var(--aw-muted);font-size:var(--aw-text-sm)}
.rename-input{width:100%}
</style>
