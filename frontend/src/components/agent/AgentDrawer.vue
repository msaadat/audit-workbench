<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import { useAgentRun } from '../../composables/useAgentRun'
import { useAssistantChat } from '../../composables/useAssistantChat'
import type { AgentApproval, AgentDecision, AgentInteraction, AssistantChat, AssistantMessageIntent, AuditDocument, WorkspaceSummary } from '../../types'
import ChatComposer from './ChatComposer.vue'
import ChatHistoryPanel from './ChatHistoryPanel.vue'
import ChatTranscript from './ChatTranscript.vue'
import DocumentContextPicker from './DocumentContextPicker.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
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
const drawerWidth = ref(416)
const resizing = ref(false)
const MIN_WIDTH = 340
const MAX_WIDTH = 680
// 12.5rem nav rail + a two-pane tab layout that still reads.
const MIN_CONTENT_WIDTH = 880
const WIDTH_KEY = `audit-workbench:agent-drawer-width:${props.workspace.id}`

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
  const saved = Number(window.localStorage.getItem(WIDTH_KEY))
  if (Number.isFinite(saved) && saved > 0) drawerWidth.value = clamp(saved)
  await Promise.all([agent.init(), chats.init(), loadDocuments()]).catch(error => fail('Could not open the assistant', error))
})
onUnmounted(stopResize)

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
  agent.state.run?.messages?.length ?? 0,
].join(':'), () => {
  if (chats.state.activeChatId) void chats.refresh()
})

async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function send(content: string, sendIntent: AssistantMessageIntent = 'auto', goalTemplate?: string, source: 'composer'|'shortcut'|'tab_button'|'folder_intake' = 'composer') {
  try {
    await chats.send(content, sendIntent, mode.value, { goalTemplate, source })
  } catch (error) { fail('Message failed', error) }
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
function shortcut(label: string, template: string) {
  const text = template === 'full_audit_working_draft' ? 'Prepare a full audit working draft.' : `Start ${label.toLowerCase()} work for this engagement.`
  void send(text, 'act', template, 'shortcut')
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
// The drawer pushes rather than overlays, so its width is capped to leave the
// nav rail plus a workable two-pane content area standing.
function clamp(value: number) { return Math.min(Math.max(value, MIN_WIDTH), MAX_WIDTH, Math.max(MIN_WIDTH, window.innerWidth - MIN_CONTENT_WIDTH)) }
function startResize(event: PointerEvent) { if (!agent.state.drawerOpen) return; event.preventDefault(); resizing.value = true; document.body.classList.add('agent-drawer-resizing'); window.addEventListener('pointermove', resize); window.addEventListener('pointerup', stopResize) }
function resize(event: PointerEvent) { drawerWidth.value = clamp(window.innerWidth - event.clientX) }
function stopResize() { if (!resizing.value) return; resizing.value = false; document.body.classList.remove('agent-drawer-resizing'); window.removeEventListener('pointermove', resize); window.removeEventListener('pointerup', stopResize); window.localStorage.setItem(WIDTH_KEY, String(drawerWidth.value)) }
function fail(summary: string, error: unknown) { toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6500 }) }
</script>

<template>
  <aside class="agent-drawer" :class="{ collapsed: !agent.state.drawerOpen, resizing }" :style="agent.state.drawerOpen ? { flexBasis: `${drawerWidth}px`, width: `${drawerWidth}px` } : undefined">
    <!-- The entire collapsed rail expands the assistant, not just the icon. -->
    <button v-if="!agent.state.drawerOpen" class="collapsed-toggle" aria-label="Expand audit assistant" @click="agent.toggleDrawer()">
      <i class="pi pi-sparkles" />
    </button>

    <template v-else>
    <div class="resize-handle" role="separator" aria-label="Resize audit assistant" @pointerdown="startResize" />
    <header class="drawer-head">
      <i class="pi pi-sparkles" />
      <button class="title" @click="showHistory = !showHistory"><strong>{{ activeChat?.title ?? 'Audit assistant' }}</strong><i class="pi pi-chevron-down" /></button>
      <Tag v-if="status" :value="status.replaceAll('_',' ')" :severity="['failed'].includes(status) ? 'danger' : ['awaiting_approval','awaiting_input','interrupted'].includes(status) ? 'warn' : 'info'" />
      <span v-if="runActive" class="live-dot" :class="{ off: !agent.state.connected }" :title="agent.state.connected ? 'Live updates connected' : 'Live updates reconnecting…'" />
      <span class="grow" />
      <Button icon="pi pi-plus" text size="small" severity="secondary" aria-label="New chat" @click="chats.createChat()" />
      <Button icon="pi pi-angle-right" text size="small" severity="secondary" aria-label="Collapse assistant" @click="agent.toggleDrawer()" />
    </header>
      <ChatHistoryPanel v-if="showHistory" :chats="chats.state.summaries" :activeId="chats.state.activeChatId" @select="chats.switchChat($event); showHistory = false" @create="chats.createChat(); showHistory = false" @rename="rename" @remove="remove" @close="showHistory = false" />
      <div v-if="chats.state.loading && !displayChat" class="loading"><i class="pi pi-spin pi-spinner" /> Loading chat…</div>
      <ChatTranscript v-else-if="displayChat" :workspaceId="workspace.id" :chat="displayChat" :documents="documents" :actionBusy="actionBusy" :busy="chats.state.busy" @shortcut="shortcut" @command="send($event, 'act')" @retry="chats.retry($event, mode).catch(error => fail('Message failed', error))" @changed="chats.refresh" @respond="respond" @decide="decide" />
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
    </template>
  </aside>

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
/* The drawer is always a real layout column: it reserves its own width and
   pushes the tab content instead of covering it. Overlaying it hid the right
   edge of every master/detail tab at ordinary laptop widths. */
.agent-drawer{position:relative;flex:0 0 auto;display:flex;flex-direction:column;min-width:0;overflow:hidden;border-left:1px solid var(--aw-border);background:var(--p-surface-0)}.agent-drawer.collapsed{flex:0 0 3.25rem}.agent-drawer.resizing{transition:none;user-select:none}:global(body.agent-drawer-resizing){cursor:col-resize;user-select:none}.resize-handle{position:absolute;z-index:7;inset:0 auto 0 -.3rem;width:.6rem;cursor:col-resize}.drawer-head{display:flex;align-items:center;gap:.45rem;min-height:3.1rem;padding:.55rem .7rem;border-bottom:1px solid var(--aw-border)}.collapsed-toggle{flex:1;display:grid;place-items:center;width:100%;border:0;background:transparent;color:var(--aw-teal);font-size:1.05rem;cursor:pointer}.collapsed-toggle:hover{background:var(--aw-teal-soft)}.drawer-head>i{color:var(--aw-teal)}.title{display:flex;align-items:center;gap:.3rem;min-width:0;padding:.2rem;border:0;background:transparent;color:inherit;cursor:pointer}.title strong{max-width:12rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.82rem}.title i{font-size:.6rem}.grow{flex:1}.live-dot{width:.5rem;height:.5rem;border-radius:50%;background:#22c55e}.live-dot.off{background:var(--p-amber-500)}.loading{display:grid;place-content:center;place-items:center;gap:.4rem;flex:1;color:var(--aw-muted);font-size:.78rem}.rename-input{width:100%}
</style>
