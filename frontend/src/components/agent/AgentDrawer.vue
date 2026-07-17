<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
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
const agent = useAgentRun(props.workspace.id)
const chats = useAssistantChat(props.workspace.id)
const showHistory = ref(false)
const pickerOpen = ref(false)
const documents = ref<AuditDocument[]>([])
const mode = agent.launchMode
const drawerWidth = ref(416)
const resizing = ref(false)
const MIN_WIDTH = 340
const MAX_WIDTH = 680
const WIDTH_KEY = `audit-workbench:agent-drawer-width:${props.workspace.id}`

const activeChat = computed(() => chats.state.chat)
const status = computed(() => activeChat.value?.active_workspace_run?.status ?? '')
const runActive = computed(() => agent.isActive.value)
const displayChat = computed<AssistantChat | null>(() => {
  const chat = activeChat.value
  const global = chat?.active_workspace_run
  if (!chat || !global || chat.runs.some(item => item.run_id === global.run_id)) return chat
  return { ...chat, transcript: [global, ...chat.transcript] }
})

onMounted(async () => {
  const saved = Number(window.localStorage.getItem(WIDTH_KEY))
  if (Number.isFinite(saved) && saved > 0) drawerWidth.value = clamp(saved)
  await Promise.all([agent.init(), chats.init(), loadDocuments()]).catch(error => fail('Could not open the assistant', error))
})
onUnmounted(stopResize)

watch(() => `${agent.state.run?.id}:${agent.state.run?.status}:${agent.state.run?.graph_revision ?? 0}:${agent.state.run?.pending_commands?.length ?? 0}`, () => {
  if (chats.state.activeChatId) void chats.refresh()
})

async function loadDocuments() {
  documents.value = (await api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)).items
}
async function send(content: string, sendIntent: AssistantMessageIntent = 'auto', goalTemplate?: string, source: 'composer'|'shortcut'|'tab_button'|'folder_intake' = 'composer') {
  try {
    const result = await chats.send(content, sendIntent, mode.value, { goalTemplate, source })
    const runId = String(result?.outcome?.run_id ?? '')
    if (runId) await agent.openRun(runId).catch(() => undefined)
  } catch (error) { fail('Message failed', error) }
}
function shortcut(label: string, template: string) {
  const text = template === 'full_audit_working_draft' ? 'Prepare a full audit working draft.' : `Start ${label.toLowerCase()} work for this engagement.`
  void send(text, 'act', template, 'shortcut')
}
async function rename() {
  const current = activeChat.value?.title ?? ''
  const title = window.prompt('Rename chat', current)
  if (title?.trim()) await chats.rename(title.trim())
}
async function remove() {
  if (!window.confirm('Delete this chat and its local Q&A artifacts? Linked runs and workspace work products will remain.')) return
  await chats.deleteActive()
  showHistory.value = false
}
async function applyDocuments(values: AuditDocument[]) { await chats.setDocuments(values) }
async function respond(runId: string, interaction: AgentInteraction, response: Record<string, unknown>) {
  await api.post(`/api/workspaces/${props.workspace.id}/agent/runs/${runId}/interactions/${interaction.id}/respond`, response)
  await chats.refresh()
}
async function decide(runId: string, approval: AgentApproval, decisions: AgentDecision[]) {
  await api.post(`/api/workspaces/${props.workspace.id}/agent/runs/${runId}/approvals/${approval.id}`, { decisions })
  await chats.refresh()
}
function clamp(value: number) { return Math.min(Math.max(value, MIN_WIDTH), MAX_WIDTH, Math.max(MIN_WIDTH, window.innerWidth - 480)) }
function startResize(event: PointerEvent) { if (!agent.state.drawerOpen || window.innerWidth <= 900) return; event.preventDefault(); resizing.value = true; document.body.classList.add('agent-drawer-resizing'); window.addEventListener('pointermove', resize); window.addEventListener('pointerup', stopResize) }
function resize(event: PointerEvent) { drawerWidth.value = clamp(window.innerWidth - event.clientX) }
function stopResize() { if (!resizing.value) return; resizing.value = false; document.body.classList.remove('agent-drawer-resizing'); window.removeEventListener('pointermove', resize); window.removeEventListener('pointerup', stopResize); window.localStorage.setItem(WIDTH_KEY, String(drawerWidth.value)) }
function fail(summary: string, error: unknown) { toast.add({ severity: 'error', summary, detail: error instanceof ApiError ? error.message : String(error), life: 6500 }) }
</script>

<template>
  <aside class="agent-drawer" :class="{ collapsed: !agent.state.drawerOpen, resizing }" :style="agent.state.drawerOpen ? { flexBasis: `${drawerWidth}px`, width: `${drawerWidth}px` } : undefined">
    <div v-if="agent.state.drawerOpen" class="resize-handle" role="separator" aria-label="Resize audit assistant" @pointerdown="startResize" />
    <header class="drawer-head">
      <Button v-if="!agent.state.drawerOpen" icon="pi pi-sparkles" text size="small" severity="secondary" aria-label="Expand audit assistant" @click="agent.toggleDrawer()" />
      <template v-else>
        <i class="pi pi-sparkles" />
        <button class="title" @click="showHistory = !showHistory"><strong>{{ activeChat?.title ?? 'Audit assistant' }}</strong><i class="pi pi-chevron-down" /></button>
        <Tag v-if="status" :value="status.replaceAll('_',' ')" :severity="['failed'].includes(status) ? 'danger' : ['awaiting_approval','awaiting_input','interrupted'].includes(status) ? 'warn' : 'info'" />
        <span v-if="runActive" class="live-dot" :class="{ off: !agent.state.connected }" />
        <span class="grow" />
        <Button icon="pi pi-plus" text size="small" severity="secondary" aria-label="New chat" @click="chats.createChat()" />
        <Button icon="pi pi-angle-right" text size="small" severity="secondary" aria-label="Collapse assistant" @click="agent.toggleDrawer()" />
      </template>
    </header>

    <template v-if="agent.state.drawerOpen">
      <ChatHistoryPanel v-if="showHistory" :chats="chats.state.summaries" :activeId="chats.state.activeChatId" @select="chats.switchChat($event); showHistory = false" @create="chats.createChat(); showHistory = false" @rename="rename" @remove="remove" @close="showHistory = false" />
      <div v-if="chats.state.loading && !displayChat" class="loading"><i class="pi pi-spin pi-spinner" /> Loading chat…</div>
      <ChatTranscript v-else-if="displayChat" :workspaceId="workspace.id" :chat="displayChat" :documents="documents" @shortcut="shortcut" @retry="chats.retry($event, mode)" @changed="chats.refresh" @respond="respond" @decide="decide" />
      <ChatComposer
        v-if="activeChat"
        v-model:mode="mode"
        :busy="chats.state.busy"
        :capabilities="chats.state.capabilities"
        :documents="documents"
        :selectedIds="activeChat.composer_context.document_ids"
        @send="send"
        @documents="pickerOpen = true"
        @removeDocument="chats.removeDocument"
      />
    </template>
  </aside>

  <DocumentContextPicker
    v-model:visible="pickerOpen"
    :workspaceId="workspace.id"
    :selectedIds="activeChat?.composer_context.document_ids ?? []"
    @apply="applyDocuments"
  />
</template>

<style scoped>
.agent-drawer{position:relative;display:flex;flex-direction:column;min-width:0;overflow:hidden;border-left:1px solid var(--aw-border);background:var(--p-surface-0)}.agent-drawer.collapsed{flex:0 0 3.25rem}.agent-drawer.resizing{transition:none;user-select:none}:global(body.agent-drawer-resizing){cursor:col-resize;user-select:none}.resize-handle{position:absolute;z-index:7;inset:0 auto 0 -.3rem;width:.6rem;cursor:col-resize}.drawer-head{display:flex;align-items:center;gap:.45rem;min-height:3.1rem;padding:.55rem .7rem;border-bottom:1px solid var(--aw-border)}.collapsed .drawer-head{justify-content:center;height:100%;padding:.55rem .2rem;border:0}.drawer-head>i{color:var(--aw-teal)}.title{display:flex;align-items:center;gap:.3rem;min-width:0;padding:.2rem;border:0;background:transparent;color:inherit;cursor:pointer}.title strong{max-width:12rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.82rem}.title i{font-size:.6rem}.grow{flex:1}.live-dot{width:.5rem;height:.5rem;border-radius:50%;background:#22c55e}.live-dot.off{background:var(--p-amber-500)}.loading{display:grid;place-items:center;gap:.4rem;flex:1;color:var(--aw-muted);font-size:.78rem}@media(max-width:1536px) and (min-width:901px){.agent-drawer:not(.collapsed){position:absolute;z-index:30;inset:0 0 0 auto;max-width:min(38rem,calc(100% - 12.5rem));box-shadow:-10px 0 30px rgb(7 22 43/14%)}}@media(max-width:900px){.agent-drawer{width:100%;flex-basis:auto!important}.agent-drawer.collapsed{height:3.25rem}.resize-handle{display:none}}
</style>
