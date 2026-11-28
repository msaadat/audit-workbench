import { computed, reactive, readonly } from 'vue'

import { api } from '../api'
import type {
  AssistantArtifact,
  AssistantCapabilities,
  AssistantChat,
  AssistantChatMessage,
  AssistantChatSummary,
  AssistantMessageIntent,
  AssistantStep,
  AssistantUiContext,
  AuditDocument,
} from '../types'
import { useAgentRun, type AgentMode } from './useAgentRun'

export interface LiveTurnState {
  steps: AssistantStep[]
  text: string
}

interface ChatState {
  workspaceId: string
  summaries: AssistantChatSummary[]
  activeChatId: string | null
  chat: AssistantChat | null
  capabilities: AssistantCapabilities | null
  initialized: boolean
  loading: boolean
  busy: boolean
  liveTurn: LiveTurnState | null
  error: string | null
}

const ACTIVE_KEY = 'audit-workbench:assistant-chat:'
const stores = new Map<string, ChatState>()
const initializers = new Map<string, Promise<void>>()

/**
 * Bookkeeping that must not be reactive: the latest request issued per kind,
 * so an older response never overwrites a newer one, and the messages a send
 * has shown but the server has not yet returned, so a refresh that lands
 * mid-send does not erase them.
 */
interface ChatTracking {
  chatSeq: number
  summarySeq: number
  pending: Map<string, AssistantChatMessage[]>
  abortController: AbortController | null
}
const tracking = new Map<string, ChatTracking>()

function track(workspaceId: string): ChatTracking {
  let value = tracking.get(workspaceId)
  if (!value) {
    value = { chatSeq: 0, summarySeq: 0, pending: new Map(), abortController: null }
    tracking.set(workspaceId, value)
  }
  return value
}

/**
 * Forget every cached chat. Called on sign-out and on every 401: a store
 * initialised under one account would otherwise be shown, as already loaded,
 * to the next. Composables still holding an old store write into an object
 * nothing reads any more, so an in-flight response cannot leak across.
 */
export function resetAssistantChatState(): void {
  stores.clear()
  initializers.clear()
  tracking.clear()
}

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
      capabilities: null, initialized: false, loading: false, busy: false, liveTurn: null, error: null,
    })
    stores.set(workspaceId, value)
  }
  return value
}

/**
 * A message refused before it reached the server. Distinct from a failed POST,
 * which the transcript keeps as a failed bubble with Retry: this one left no
 * trace, so the composer must get its text back.
 */
export class ChatNotSentError extends Error {}

function requestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `request-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function useAssistantChat(workspaceId: string) {
  const store = state(workspaceId)
  const tracked = track(workspaceId)
  const agent = useAgentRun(workspaceId)

  async function loadSummaries() {
    const seq = ++tracked.summarySeq
    const result = await api.get<{ chats: AssistantChatSummary[] }>(
      `/api/workspaces/${workspaceId}/assistant/chats?limit=100`,
    )
    if (seq === tracked.summarySeq) store.summaries = result.chats
    return result.chats
  }

  /**
   * Install a chat the server returned, if it is still the one on screen.
   * Messages a send has shown and the server has not yet recorded are carried
   * over, so a refresh landing mid-send does not make them vanish.
   */
  function applyChat(chatId: string, chat: AssistantChat): boolean {
    if (store.activeChatId !== chatId) return false
    const pending = (tracked.pending.get(chatId) ?? [])
      .filter(item => !chat.messages.some(message => message.request_id === item.request_id))
    if (pending.length) chat.transcript = [...chat.transcript, ...pending]
    // Mid-run lightweight refreshes omit suggestions/guided_workflows to skip
    // recomputing 34-capability readiness; keep the previous ones intact.
    if (chat.suggestions == null) {
      chat.suggestions = store.chat?.id === chatId ? (store.chat.suggestions ?? []) : []
    }
    if (chat.guided_workflows == null) {
      chat.guided_workflows = store.chat?.id === chatId ? (store.chat.guided_workflows ?? []) : []
    }
    store.chat = chat
    store.capabilities = chat.capabilities
    return true
  }

  async function fetchChat(chatId: string, includeSuggestions = true) {
    const seq = ++tracked.chatSeq
    const suffix = includeSuggestions ? '' : '?include_suggestions=false'
    const chat = await api.get<AssistantChat>(`/api/workspaces/${workspaceId}/assistant/chats/${chatId}${suffix}`)
    if (seq === tracked.chatSeq) applyChat(chatId, chat)
  }

  async function refreshTranscript(options: { includeSuggestions?: boolean } = {}) {
    if (!store.activeChatId) return
    await fetchChat(store.activeChatId, options.includeSuggestions ?? false)
  }

  async function refresh() {
    if (!store.activeChatId) return
    await fetchChat(store.activeChatId, true)
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
    // A newer chat supersedes whatever load was in flight for the old one.
    tracked.chatSeq++
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
    const chatId = store.activeChatId
    if (!chatId) return
    const chat = await api.patch<AssistantChat>(
      `/api/workspaces/${workspaceId}/assistant/chats/${chatId}`, { title },
    )
    applyChat(chatId, chat)
    await loadSummaries()
  }

  async function deleteActive() {
    const chatId = store.activeChatId
    if (!chatId) return
    await api.del(`/api/workspaces/${workspaceId}/assistant/chats/${chatId}`)
    if (store.activeChatId !== chatId) {
      await loadSummaries()
      return
    }
    store.activeChatId = null
    store.chat = null
    remember(workspaceId, null)
    const summaries = await loadSummaries()
    if (summaries[0]) await switchChat(summaries[0].id)
    else await createChat()
  }

  async function setDocuments(values: Array<AuditDocument | string>) {
    await init()
    const chatId = store.activeChatId
    if (!chatId) return
    const document_ids = values.map(value => typeof value === 'string' ? value : value.id)
    const chat = await api.patch<AssistantChat>(
      `/api/workspaces/${workspaceId}/assistant/chats/${chatId}`, { document_ids },
    )
    applyChat(chatId, chat)
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

  function abortSend() {
    if (!tracked.abortController) return
    tracked.abortController.abort()
    tracked.abortController = null
  }

  async function send(
    content: string,
    intent: AssistantMessageIntent = 'auto',
    mode: AgentMode = 'auto',
    options: {
      command?: string | null
      goalTemplate?: string | null
      source?: 'composer' | 'shortcut' | 'tab_button' | 'folder_intake'
      requestedOutcomes?: string[]
      runKind?: 'intake'
      runContext?: Record<string, unknown>
      uiContext?: AssistantUiContext | null
      requestId?: string
    } = {},
  ) {
    await init()
    // Never a silent return: the composer has already cleared its draft by the
    // time this runs, so returning nothing here lost the auditor's message
    // with no bubble, no error and no Retry. Refusing says why, and the caller
    // hands the text back to the composer.
    if (!store.activeChatId) await createChat()
    if (store.busy) throw new ChatNotSentError('The previous message is still being sent. Send this one again in a moment.')
    // The chat this message belongs to, fixed now: switching chats while it is
    // in flight must neither post it to the new chat nor show its reply there.
    const chatId = store.activeChatId
    if (!chatId) throw new ChatNotSentError('No chat is open. Send this again in a moment.')
    store.busy = true
    store.error = null
    store.liveTurn = { steps: [], text: '' }
    const controller = new AbortController()
    tracked.abortController = controller
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
    const pending = tracked.pending.get(chatId) ?? []
    tracked.pending.set(chatId, [...pending, optimistic])
    const settle = () => {
      const left = (tracked.pending.get(chatId) ?? []).filter(item => item.id !== optimistic.id)
      if (left.length) tracked.pending.set(chatId, left)
      else tracked.pending.delete(chatId)
    }
    const payload = {
      content, intent, mode, request_id: rid,
      command: options.command ?? null,
      goal_template: options.goalTemplate ?? null, source: options.source ?? 'composer',
      requested_outcomes: options.requestedOutcomes ?? [],
      run_kind: options.runKind ?? null, run_context: options.runContext ?? null,
      ...(options.uiContext ? { ui_context: options.uiContext } : {}),
    }
    const onStreamEvent = (eventType: string, data: unknown) => {
      if (store.activeChatId !== chatId || !store.liveTurn) return
      const record = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>
      if (eventType === 'tool_start') {
        // Clear any short preamble emitted before this tool call so text from
        // separate model steps never runs together.
        store.liveTurn.text = ''
        store.liveTurn.steps.push({
          tool: String(record.tool ?? ''),
          label: typeof record.label === 'string' ? record.label : undefined,
          args: (record.args as Record<string, unknown>) ?? {},
          ok: true,
          running: true,
        })
      } else if (eventType === 'tool_end') {
        const index = typeof record.index === 'number' ? record.index : store.liveTurn.steps.length - 1
        const updated: AssistantStep = {
          tool: String(record.tool ?? ''),
          label: typeof record.label === 'string' ? record.label : undefined,
          args: (record.args as Record<string, unknown>) ?? {},
          ok: Boolean(record.ok),
          duration_ms: typeof record.duration_ms === 'number' ? record.duration_ms : undefined,
          summary: typeof record.summary === 'string' ? record.summary : undefined,
          error: typeof record.error === 'string' ? record.error : null,
          running: false,
        }
        if (index >= 0 && index < store.liveTurn.steps.length) store.liveTurn.steps[index] = updated
        else store.liveTurn.steps.push(updated)
      } else if (eventType === 'delta' && typeof record.text === 'string') {
        store.liveTurn.text += record.text
      }
    }
    try {
      const result = typeof api.postStream === 'function'
        ? await api.postStream<{ outcome: Record<string, unknown>; chat: AssistantChat }>(
            `/api/workspaces/${workspaceId}/assistant/chats/${chatId}/messages/stream`,
            payload,
            { signal: controller.signal, onEvent: onStreamEvent },
          )
        : await api.post<{ outcome: Record<string, unknown>; chat: AssistantChat }>(
            `/api/workspaces/${workspaceId}/assistant/chats/${chatId}/messages`,
            payload,
          )
      settle()
      tracked.chatSeq++
      applyChat(chatId, result.chat)
      const runId = String(result.outcome?.run_id ?? '')
      if (runId) await agent.openRun(runId).catch(() => undefined)
      const outcome = result.outcome as { kind?: string; message?: string } | undefined
      if (outcome?.kind === 'error') {
        throw new Error(outcome.message || 'The assistant could not process this message.')
      }
      await loadSummaries()
      return result
    } catch (error) {
      settle()
      const aborted = error instanceof DOMException && error.name === 'AbortError'
      if (aborted) {
        if (store.activeChatId === chatId) {
          await refresh().catch(() => undefined)
          const stillPending = () => Boolean(
            store.activeChatId === chatId
            && store.chat?.messages.some(item => item.request_id === rid && item.state === 'pending'),
          )
          if (stillPending()) {
            void (async () => {
              for (const delay of [250, 500, 1000, 1500]) {
                await new Promise(resolve => window.setTimeout(resolve, delay))
                if (store.activeChatId !== chatId || store.busy || !stillPending()) break
                await refreshTranscript({ includeSuggestions: false }).catch(() => undefined)
              }
              if (store.activeChatId === chatId && !store.busy) {
                await refresh().catch(() => undefined)
              }
            })()
          }
        }
        return undefined
      }
      const detail = error instanceof Error ? error.message : String(error)
      if (store.activeChatId !== chatId) throw error
      store.error = detail
      await refresh().catch(() => undefined)
      // If the server never recorded the message (network/validation error),
      // keep a failed copy in the transcript so Retry stays available.
      if (store.chat && store.activeChatId === chatId) {
        store.chat.transcript = store.chat.transcript.filter(item => item.id !== optimistic.id)
        if (!store.chat.messages.some(item => item.request_id === rid)) {
          store.chat.transcript.push({ ...optimistic, state: 'failed', error: detail })
        }
      }
      throw error
    } finally {
      if (tracked.abortController === controller) tracked.abortController = null
      store.liveTurn = null
      store.busy = false
    }
  }

  async function retry(message: AssistantChatMessage, mode: AgentMode) {
    return send(message.content, message.requested_intent ?? 'auto', mode, {
      source: 'composer', requestId: message.request_id ?? undefined,
    })
  }

  async function updateArtifact(artifact: AssistantArtifact, changes: Record<string, unknown>) {
    const chatId = store.activeChatId
    if (!chatId) return
    const updated = await api.patch<AssistantArtifact>(
      `/api/workspaces/${workspaceId}/assistant/chats/${chatId}/artifacts/${artifact.id}`,
      { revision: artifact.revision, changes },
    )
    if (store.chat && store.activeChatId === chatId) store.chat.artifacts[artifact.id] = updated
    return updated
  }

  async function rerunArtifact(artifact: AssistantArtifact) {
    const chatId = store.activeChatId
    if (!chatId) return
    const updated = await api.post<AssistantArtifact>(
      `/api/workspaces/${workspaceId}/assistant/chats/${chatId}/artifacts/${artifact.id}/rerun`,
      { revision: artifact.revision },
    )
    if (store.chat && store.activeChatId === chatId) store.chat.artifacts[artifact.id] = updated
    return updated
  }

  return {
    state: readonly(store) as Readonly<ChatState>,
    activeChat: computed(() => store.chat),
    documentIds: computed(() => store.chat?.composer_context.document_ids ?? []),
    init, refresh, refreshTranscript, createChat, switchChat, rename, deleteActive,
    setDocuments, addDocument, removeDocument, send, abortSend, retry,
    updateArtifact, rerunArtifact,
  }
}
