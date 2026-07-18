import { computed, reactive, readonly } from 'vue'

import { api } from '../api'
import type {
  AssistantArtifact,
  AssistantCapabilities,
  AssistantChat,
  AssistantChatMessage,
  AssistantChatSummary,
  AssistantMessageIntent,
  AuditDocument,
} from '../types'
import type { AgentMode } from './useAgentRun'

interface ChatState {
  workspaceId: string
  summaries: AssistantChatSummary[]
  activeChatId: string | null
  chat: AssistantChat | null
  capabilities: AssistantCapabilities | null
  initialized: boolean
  loading: boolean
  busy: boolean
  error: string | null
}

const ACTIVE_KEY = 'audit-workbench:assistant-chat:'
const stores = new Map<string, ChatState>()
const initializers = new Map<string, Promise<void>>()

function savedChatId(workspaceId: string): string | null {
  try { return window.localStorage.getItem(`${ACTIVE_KEY}${workspaceId}`) }
  catch { return null }
}

function remember(workspaceId: string, chatId: string | null) {
  try {
    if (chatId) window.localStorage.setItem(`${ACTIVE_KEY}${workspaceId}`, chatId)
    else window.localStorage.removeItem(`${ACTIVE_KEY}${workspaceId}`)
  } catch { /* hardened browser storage */ }
}

function state(workspaceId: string): ChatState {
  let value = stores.get(workspaceId)
  if (!value) {
    value = reactive<ChatState>({
      workspaceId, summaries: [], activeChatId: null, chat: null,
      capabilities: null, initialized: false, loading: false, busy: false, error: null,
    })
    stores.set(workspaceId, value)
  }
  return value
}

function requestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `request-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function useAssistantChat(workspaceId: string) {
  const store = state(workspaceId)

  async function loadSummaries() {
    const result = await api.get<{ chats: AssistantChatSummary[] }>(
      `/api/workspaces/${workspaceId}/assistant/chats?limit=100`,
    )
    store.summaries = result.chats
    return result.chats
  }

  async function refresh() {
    if (!store.activeChatId) return
    store.chat = await api.get<AssistantChat>(
      `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}`,
    )
    store.capabilities = store.chat.capabilities
    await loadSummaries()
  }

  async function switchChat(chatId: string) {
    store.loading = true
    try {
      store.activeChatId = chatId
      remember(workspaceId, chatId)
      await refresh()
    } finally { store.loading = false }
  }

  async function createChat() {
    const chat = await api.post<AssistantChat>(`/api/workspaces/${workspaceId}/assistant/chats`)
    store.chat = chat
    store.capabilities = chat.capabilities
    store.activeChatId = chat.id
    remember(workspaceId, chat.id)
    await loadSummaries()
    return chat
  }

  async function init() {
    // Idempotent: sends and document changes await this freely without
    // re-fetching summaries and the active chat on every call.
    if (store.initialized) return
    const existing = initializers.get(workspaceId)
    if (existing) return existing
    const task = (async () => {
      store.loading = true
      try {
        const summaries = await loadSummaries()
        const remembered = savedChatId(workspaceId)
        const target = summaries.find(item => item.id === remembered)?.id ?? summaries[0]?.id
        if (target) await switchChat(target)
        else await createChat()
        store.initialized = true
      } finally { store.loading = false }
    })()
    initializers.set(workspaceId, task)
    try { await task }
    finally { initializers.delete(workspaceId) }
  }

  async function rename(title: string) {
    if (!store.activeChatId) return
    store.chat = await api.patch<AssistantChat>(
      `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}`, { title },
    )
    await loadSummaries()
  }

  async function deleteActive() {
    if (!store.activeChatId) return
    await api.del(`/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}`)
    store.activeChatId = null
    store.chat = null
    remember(workspaceId, null)
    const summaries = await loadSummaries()
    if (summaries[0]) await switchChat(summaries[0].id)
    else await createChat()
  }

  async function setDocuments(values: Array<AuditDocument | string>) {
    await init()
    if (!store.activeChatId) return
    const document_ids = values.map(value => typeof value === 'string' ? value : value.id)
    store.chat = await api.patch<AssistantChat>(
      `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}`, { document_ids },
    )
  }

  async function addDocument(document: AuditDocument) {
    await init()
    const ids = new Set(store.chat?.composer_context.document_ids ?? [])
    ids.add(document.id)
    await setDocuments([...ids])
  }

  async function removeDocument(documentId: string) {
    await setDocuments((store.chat?.composer_context.document_ids ?? []).filter(id => id !== documentId))
  }

  async function send(
    content: string,
    intent: AssistantMessageIntent = 'auto',
    mode: AgentMode = 'auto',
    options: { goalTemplate?: string | null; source?: 'composer' | 'shortcut' | 'tab_button' | 'folder_intake'; runKind?: 'intake'; runContext?: Record<string, unknown>; requestId?: string } = {},
  ) {
    await init()
    if (!store.activeChatId || store.busy) return
    store.busy = true
    store.error = null
    const rid = options.requestId ?? requestId()
    // Show the user's message immediately; the server response replaces it.
    const optimistic: AssistantChatMessage = {
      id: `optimistic-${rid}`, ordinal: store.chat?.next_ordinal ?? 0, type: 'message',
      derived: false, role: 'user', kind: 'text', content,
      created_at: new Date().toISOString(), request_id: rid, state: 'pending',
      requested_intent: intent, resolved_intent: null, reply_to_id: null,
      artifact_ids: [], outcome: null, error: null,
    }
    store.chat?.transcript.push(optimistic)
    try {
      const result = await api.post<{ outcome: Record<string, unknown>; chat: AssistantChat }>(
        `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}/messages`,
        {
          content, intent, mode, request_id: rid,
          goal_template: options.goalTemplate ?? null, source: options.source ?? 'composer',
          run_kind: options.runKind ?? null, run_context: options.runContext ?? null,
        },
      )
      store.chat = result.chat
      store.capabilities = result.chat.capabilities
      const outcome = result.outcome as { kind?: string; message?: string } | undefined
      if (outcome?.kind === 'error') {
        throw new Error(outcome.message || 'The assistant could not process this message.')
      }
      await loadSummaries()
      return result
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      store.error = detail
      await refresh().catch(() => undefined)
      // If the server never recorded the message (network/validation error),
      // keep a failed copy in the transcript so Retry stays available.
      if (store.chat) {
        store.chat.transcript = store.chat.transcript.filter(item => item.id !== optimistic.id)
        if (!store.chat.messages.some(item => item.request_id === rid)) {
          store.chat.transcript.push({ ...optimistic, state: 'failed', error: detail })
        }
      }
      throw error
    } finally { store.busy = false }
  }

  async function retry(message: AssistantChatMessage, mode: AgentMode) {
    return send(message.content, message.requested_intent ?? 'auto', mode, {
      source: 'composer', requestId: message.request_id ?? undefined,
    })
  }

  async function updateArtifact(artifact: AssistantArtifact, changes: Record<string, unknown>) {
    if (!store.activeChatId) return
    const updated = await api.patch<AssistantArtifact>(
      `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}/artifacts/${artifact.id}`,
      { revision: artifact.revision, changes },
    )
    if (store.chat) store.chat.artifacts[artifact.id] = updated
    return updated
  }

  async function rerunArtifact(artifact: AssistantArtifact) {
    if (!store.activeChatId) return
    const updated = await api.post<AssistantArtifact>(
      `/api/workspaces/${workspaceId}/assistant/chats/${store.activeChatId}/artifacts/${artifact.id}/rerun`,
      { revision: artifact.revision },
    )
    if (store.chat) store.chat.artifacts[artifact.id] = updated
    return updated
  }

  return {
    state: readonly(store) as Readonly<ChatState>,
    activeChat: computed(() => store.chat),
    documentIds: computed(() => store.chat?.composer_context.document_ids ?? []),
    init, refresh, createChat, switchChat, rename, deleteActive,
    setDocuments, addDocument, removeDocument, send, retry,
    updateArtifact, rerunArtifact,
  }
}
