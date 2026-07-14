import { computed, reactive, readonly, ref, watch } from 'vue'

import { api } from '../api'
import type {
  AgentDecision,
  AgentRun,
  AgentRunContext,
  AgentRunSummary,
  AssistantStatus,
  WorkspaceChange,
} from '../types'

/**
 * Shared agent-run state, one store per workspace, owned at module scope so
 * the drawer and every tab observe the same run without a global store
 * library. The store owns the EventSource: events drive a debounced refetch
 * of the full run record (simple and always consistent with the backend) and
 * fan `workspace_changed` notifications out to subscribed tabs so they can
 * live-refresh while the agent populates the workspace.
 */

interface AgentState {
  workspaceId: string
  status: AssistantStatus | null
  run: AgentRun | null
  runs: AgentRunSummary[]
  drawerOpen: boolean
  connected: boolean
  starting: boolean
  lastChange: (WorkspaceChange & { at: number }) | null
}

type ChangeListener = (change: WorkspaceChange) => void
export type AgentMode = 'auto' | 'permission'

const MODE_STORAGE_KEY = 'audit-workbench:agent-mode'

function savedMode(): AgentMode {
  try {
    return window.localStorage.getItem(MODE_STORAGE_KEY) === 'permission'
      ? 'permission'
      : 'auto'
  } catch {
    return 'auto'
  }
}

// One launch preference is shared by every assistant entry point. Individual
// runs still persist their selected mode in the backend once they start.
const launchMode = ref<AgentMode>(savedMode())
watch(launchMode, (mode) => {
  try {
    window.localStorage.setItem(MODE_STORAGE_KEY, mode)
  } catch {
    /* Storage can be unavailable in hardened/private browser contexts. */
  }
})

const stores = new Map<string, AgentState>()
const listeners = new Map<string, Set<ChangeListener>>()
const sources = new Map<string, EventSource>()
const refetchTimers = new Map<string, number>()

const ACTIVE_STATUSES = new Set([
  'queued',
  'discovering',
  'planning',
  'executing',
  'awaiting_approval',
  'awaiting_input',
  'verifying',
  'summarizing',
  'paused',
])

function state(workspaceId: string): AgentState {
  let existing = stores.get(workspaceId)
  if (!existing) {
    existing = reactive<AgentState>({
      workspaceId,
      status: null,
      run: null,
      runs: [],
      // Preserve the existing always-visible drawer on first visit; the UI can
      // then collapse it without losing the run or its live connection.
      drawerOpen: true,
      connected: false,
      starting: false,
      lastChange: null,
    })
    stores.set(workspaceId, existing)
  }
  return existing
}

function emitChange(workspaceId: string, change: WorkspaceChange) {
  state(workspaceId).lastChange = { ...change, at: Date.now() }
  for (const listener of listeners.get(workspaceId) ?? []) listener(change)
}

function disconnect(workspaceId: string) {
  sources.get(workspaceId)?.close()
  sources.delete(workspaceId)
  state(workspaceId).connected = false
}

function scheduleRefetch(workspaceId: string, runId: string) {
  // Events arrive in bursts; one refetch ~150ms after the last event of a
  // burst keeps the record consistent without hammering the API.
  const existing = refetchTimers.get(workspaceId)
  if (existing) window.clearTimeout(existing)
  refetchTimers.set(
    workspaceId,
    window.setTimeout(async () => {
      refetchTimers.delete(workspaceId)
      const store = state(workspaceId)
      try {
        const run = await api.get<AgentRun>(
          `/api/workspaces/${workspaceId}/agent/runs/${runId}`,
        )
        if (store.run?.id === runId || !store.run) store.run = run
      } catch {
        /* transient — the next event retries */
      }
    }, 150),
  )
}

function connect(workspaceId: string, runId: string) {
  disconnect(workspaceId)
  const store = state(workspaceId)
  const source = new EventSource(
    `/api/workspaces/${workspaceId}/agent/runs/${runId}/events?cursor=0`,
  )
  sources.set(workspaceId, source)
  source.onopen = () => {
    store.connected = true
  }
  source.onerror = () => {
    store.connected = false
    // EventSource retries automatically with Last-Event-ID; nothing to do.
  }
  const refetch = () => scheduleRefetch(workspaceId, runId)
  for (const type of [
    'run_status',
    'plan_update',
    'task_update',
    'approval_request',
    'approval_resolved',
    'message',
    'discovery',
    'warning',
    'summary_ready',
  ]) {
    source.addEventListener(type, refetch)
  }
  source.addEventListener('workspace_changed', (event) => {
    refetch()
    try {
      const payload = JSON.parse((event as MessageEvent).data)
      emitChange(workspaceId, payload.data as WorkspaceChange)
    } catch {
      /* malformed event — refetch still keeps state consistent */
    }
  })
  source.addEventListener('stream_end', () => {
    disconnect(workspaceId)
    scheduleRefetch(workspaceId, runId)
    void loadRuns(workspaceId)
  })
}

async function loadRuns(workspaceId: string) {
  const store = state(workspaceId)
  store.runs = (
    await api.get<{ runs: AgentRunSummary[] }>(
      `/api/workspaces/${workspaceId}/agent/runs`,
    )
  ).runs
  return store.runs
}

export function useAgentRun(workspaceId: string) {
  const store = state(workspaceId)

  const isActive = computed(
    () => !!store.run && ACTIVE_STATUSES.has(store.run.status),
  )
  const pendingApproval = computed(
    () => store.run?.approvals.find((a) => a.status === 'pending') ?? null,
  )

  async function refreshStatus() {
    try {
      store.status = await api.get<AssistantStatus>('/api/agent/status')
    } catch {
      store.status = {
        configured: false,
        backend: '',
        model: '',
        base_url: '',
      }
    }
  }

  /** Load history and reattach to a still-active run (e.g. after a reload). */
  async function init() {
    await refreshStatus()
    try {
      const runs = await loadRuns(workspaceId)
      const active = runs.find((r) => ACTIVE_STATUSES.has(r.status))
      const latest = active ?? runs[0]
      if (latest) await openRun(latest.id)
      if (active) store.drawerOpen = true
    } catch {
      /* agent endpoints unavailable — the drawer shows the launch panel */
    }
  }

  async function openRun(runId: string) {
    store.run = await api.get<AgentRun>(
      `/api/workspaces/${workspaceId}/agent/runs/${runId}`,
    )
    if (ACTIVE_STATUSES.has(store.run.status)) connect(workspaceId, runId)
    else disconnect(workspaceId)
  }

  async function startRun(
    mode: AgentMode,
    context: AgentRunContext,
    kind: AgentRun['kind'] = 'analysis',
  ) {
    store.starting = true
    try {
      const run = await api.post<AgentRun>(
        `/api/workspaces/${workspaceId}/agent/runs`,
        { mode, context, kind },
      )
      store.run = run
      store.drawerOpen = true
      connect(workspaceId, run.id)
      void loadRuns(workspaceId)
      return run
    } finally {
      store.starting = false
    }
  }

  async function pause() {
    if (!store.run) return
    await api.post(`/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/pause`)
  }

  async function resume() {
    if (!store.run) return
    await api.post(`/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/resume`)
    connect(workspaceId, store.run.id)
  }

  async function cancel() {
    if (!store.run) return
    await api.post(`/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/cancel`)
  }

  /** Steer a live run, or spawn a linked follow-up run after completion. */
  async function sendMessage(content: string) {
    if (!store.run) return
    const response = await api.post<{ handled: string; run: AgentRun }>(
      `/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/messages`,
      { content },
    )
    if (response.handled === 'follow_up_run') {
      store.run = response.run
      connect(workspaceId, response.run.id)
      void loadRuns(workspaceId)
    } else {
      scheduleRefetch(workspaceId, store.run.id)
    }
    return response
  }

  async function decide(approvalId: string, decisions: AgentDecision[]) {
    if (!store.run) return
    await api.post(
      `/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/approvals/${approvalId}`,
      { decisions },
    )
    scheduleRefetch(workspaceId, store.run.id)
  }

  /** Tabs subscribe to live workspace changes; returns an unsubscribe. */
  function onWorkspaceChanged(listener: ChangeListener): () => void {
    let set = listeners.get(workspaceId)
    if (!set) {
      set = new Set()
      listeners.set(workspaceId, set)
    }
    set.add(listener)
    return () => set!.delete(listener)
  }

  return {
    state: readonly(store) as Readonly<AgentState>,
    launchMode,
    isActive,
    pendingApproval,
    init,
    refreshStatus,
    loadRuns: () => loadRuns(workspaceId),
    openRun,
    startRun,
    pause,
    resume,
    cancel,
    sendMessage,
    decide,
    onWorkspaceChanged,
    toggleDrawer: () => {
      store.drawerOpen = !store.drawerOpen
    },
  }
}
