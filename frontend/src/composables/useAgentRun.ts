import { computed, reactive, readonly, ref, watch } from 'vue'

import { api } from '../api'
import type {
  AgentDecision,
  AgentInteraction,
  AgentRun,
  AgentRunStatus,
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
  drawerAutoOpened: boolean
  connected: boolean
  starting: boolean
  lastChange: (WorkspaceChange & { at: number }) | null
  /**
   * Live text from the model call in flight. Transient by design: it is never
   * part of the run record, is dropped when the call ends, and exists only so a
   * long generation shows movement instead of a frozen label.
   */
  stream: { callId: string; stage: string; label: string; text: string } | null
  /**
   * Structured read of the same call — headings drafted so far, rows drafted
   * so far — for a response whose raw text (JSON, mostly) is not worth
   * showing a reader directly. Same lifecycle as `stream`: transient, cleared
   * the moment the call settles.
   */
  progress: { callId: string; stage: string; items: AgentProgressItem[] } | null
}

export interface AgentProgressItem {
  label: string
  state: 'done' | 'current' | string
}

// Enough of the tail to show the model is producing sense, bounded so a long
// generation cannot grow this without limit.
const STREAM_TAIL_CHARS = 480

type ChangeListener = (change: WorkspaceChange) => void
type InvalidationListener = () => void
export type AgentMode = 'auto' | 'permission'

const MODE_STORAGE_KEY = 'audit-workbench:agent-mode'
const PANEL_STORAGE_PREFIX = 'audit-workbench:agent-panel-open:'

function savedPanelState(workspaceId: string): boolean {
  try {
    return window.localStorage.getItem(`${PANEL_STORAGE_PREFIX}${workspaceId}`) === 'true'
  } catch {
    return false
  }
}

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
const invalidationListeners = new Map<string, Set<InvalidationListener>>()
const sources = new Map<string, EventSource>()
const refetchTimers = new Map<string, number>()
const invalidationTimers = new Map<string, number>()

const ACTIVE_STATUSES = new Set([
  'queued',
  'interpreting',
  'executing',
  'awaiting_approval',
  'awaiting_input',
  'verifying',
  'paused',
])

/**
 * The run-status vocabulary, owned here so a new status is added in one place
 * rather than in each list that happens to enumerate it.
 * `completed_with_failures` is a run that committed real work and left some
 * units unsettled — finished, but not cleanly, and worth the user's attention.
 */
export const COMPLETED_STATUSES: AgentRunStatus[] = [
  'completed',
  'completed_with_open_items',
  'completed_with_issues',
  'completed_with_failures',
]
export const FAILURE_STATUSES: AgentRunStatus[] = ['failed', 'completed_with_failures']
export const TERMINAL_STATUSES: AgentRunStatus[] = [
  ...COMPLETED_STATUSES,
  'failed',
  'cancelled',
]

function state(workspaceId: string): AgentState {
  let existing = stores.get(workspaceId)
  if (!existing) {
    existing = reactive<AgentState>({
      workspaceId,
      status: null,
      run: null,
      runs: [],
      // The assistant is a contextual sidecar. It starts compact unless the
      // user explicitly left it open or a run requires their attention.
      drawerOpen: savedPanelState(workspaceId),
      drawerAutoOpened: false,
      connected: false,
      starting: false,
      lastChange: null,
      stream: null,
      progress: null,
    })
    stores.set(workspaceId, existing)
  }
  return existing
}

function emitChange(workspaceId: string, change: WorkspaceChange) {
  state(workspaceId).lastChange = { ...change, at: Date.now() }
  for (const listener of listeners.get(workspaceId) ?? []) listener(change)
  scheduleInvalidation(workspaceId)
}

function scheduleInvalidation(workspaceId: string) {
  // A single commit can emit an artifact event, a revision event, and several
  // run-projection events. Collapse that burst into one workspace reload.
  const existing = invalidationTimers.get(workspaceId)
  if (existing) window.clearTimeout(existing)
  invalidationTimers.set(
    workspaceId,
    window.setTimeout(() => {
      invalidationTimers.delete(workspaceId)
      for (const listener of invalidationListeners.get(workspaceId) ?? []) listener()
    }, 180),
  )
}

function disconnect(workspaceId: string) {
  sources.get(workspaceId)?.close()
  sources.delete(workspaceId)
  state(workspaceId).connected = false
  state(workspaceId).stream = null
  state(workspaceId).progress = null
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
        if (store.run?.id === runId || !store.run) {
          store.run = run
          if (
            run.status === 'awaiting_approval' ||
            run.status === 'awaiting_input' ||
            run.status === 'failed'
          ) {
            store.drawerOpen = true
            store.drawerAutoOpened = true
          } else if (
            store.drawerAutoOpened &&
            [...COMPLETED_STATUSES, 'cancelled'].includes(run.status)
          ) {
            store.drawerOpen = savedPanelState(workspaceId)
            store.drawerAutoOpened = false
          }
        }
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
    'task_update',
    'approval_request',
    'approval_resolved',
    'message',
    'narration',
    'milestone',
    'warning',
    'summary_ready',
    'command_queued',
    'graph_update',
    'action_update',
    'interaction_request',
    'interaction_resolved',
    'action_conflict',
    'activity_update',
    'workflow_resolved',
    'workflow_explanation',
    'stage_update',
    'unit_update',
    'unit_retry',
    'stage_summary',
    'checkpoint_request',
    'checkpoint_resolved',
    'interaction_response_stored',
    'approval_response_stored',
    'evidence_available',
  ]) {
    source.addEventListener(type, refetch)
  }
  // Deliberately not in the list above: streamed text carries its own payload
  // and must never trigger a refetch of the run record, which is exactly the
  // cost this event exists to avoid.
  source.addEventListener('model_stream', (event) => {
    try {
      const payload = JSON.parse((event as MessageEvent).data)
      const { call_id: callId, stage, label, text } = payload.data as {
        call_id: string
        stage: string
        label: string
        text: string
      }
      // Accumulate within one call only. A new call id starts a new tail, so
      // two calls of the same stage never run together into one block.
      const current = store.stream?.callId === callId ? store.stream.text : ''
      store.stream = {
        callId,
        stage,
        label,
        text: (current + text).slice(-STREAM_TAIL_CHARS),
      }
    } catch {
      /* malformed frame — the next one replaces it */
    }
  })
  // Deliberately not in the refetch list either: a structured progress read is
  // a projection of the same in-flight call the text stream carries, not a
  // change to durable run state.
  source.addEventListener('model_progress', (event) => {
    try {
      const payload = JSON.parse((event as MessageEvent).data)
      const { call_id: callId, stage, items } = payload.data as {
        call_id: string
        stage: string
        items: AgentProgressItem[]
      }
      store.progress = { callId, stage, items }
    } catch {
      /* malformed frame — the next one replaces it */
    }
  })
  // A settled unit or stage means the call that was streaming is over; leaving
  // its text on screen would attribute finished work to work still in flight.
  for (const type of ['unit_update', 'stage_update', 'run_status', 'stream_end']) {
    source.addEventListener(type, () => {
      store.stream = null
      store.progress = null
    })
  }
  source.addEventListener('workspace_revision', () => {
    refetch()
    scheduleInvalidation(workspaceId)
  })
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

/**
 * Drop every live stream and cached store. Called on sign-out: these maps are
 * module-global and keyed by workspace id, so without this the next account to
 * sign in on the same page would inherit the previous one's open EventSources
 * and agent state.
 */
export function resetAgentState() {
  for (const workspaceId of [...sources.keys()]) disconnect(workspaceId)
  for (const timer of refetchTimers.values()) window.clearTimeout(timer)
  for (const timer of invalidationTimers.values()) window.clearTimeout(timer)
  refetchTimers.clear()
  invalidationTimers.clear()
  stores.clear()
  listeners.clear()
  invalidationListeners.clear()
}

export function useAgentRun(workspaceId: string) {
  const store = state(workspaceId)

  const isActive = computed(
    () => !!store.run && ACTIVE_STATUSES.has(store.run.status),
  )
  const pendingApproval = computed(
    () => store.run?.approvals.find((a) => a.status === 'pending') ?? null,
  )
  const pendingInteraction = computed(
    () => store.run?.interactions?.find((item) => item.status === 'pending') ?? null,
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
      if (active && ['awaiting_approval', 'awaiting_input'].includes(active.status)) {
        store.drawerOpen = true
        store.drawerAutoOpened = true
      }
    } catch {
      /* agent endpoints unavailable — the drawer shows the launch panel */
    }
  }

  async function openRun(runId: string) {
    store.run = await api.get<AgentRun>(
      `/api/workspaces/${workspaceId}/agent/runs/${runId}`,
    )
    if (
      store.run.status === 'awaiting_approval' ||
      store.run.status === 'awaiting_input' ||
      store.run.status === 'failed'
    ) {
      store.drawerOpen = true
      store.drawerAutoOpened = true
    }
    if (ACTIVE_STATUSES.has(store.run.status)) connect(workspaceId, runId)
    else disconnect(workspaceId)
  }

  async function startCommand(
    mode: AgentMode,
    text: string,
    goalTemplate?: string,
    source: 'chat' | 'goal_template' | 'tab_button' | 'follow_up' = goalTemplate ? 'goal_template' : 'chat',
  ) {
    store.starting = true
    try {
      const run = await api.post<AgentRun>(
        `/api/workspaces/${workspaceId}/agent/runs`,
        { mode, command: { source, text: text.trim(), goal_template: goalTemplate || null } },
      )
      store.run = run
      store.drawerOpen = true
      store.drawerAutoOpened = true
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
    if (!store.run) return startCommand(launchMode.value, content)
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

  async function respond(interaction: AgentInteraction, response: Record<string, unknown>) {
    if (!store.run) return
    await api.post(
      `/api/workspaces/${workspaceId}/agent/runs/${store.run.id}/interactions/${interaction.id}/respond`,
      response,
    )
    scheduleRefetch(workspaceId, store.run.id)
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

  /**
   * Subscribe to durable workspace mutations. Unlike the detailed change
   * stream, this also fires when a commit only exposes a revision advance.
   * Notifications are debounced across a burst of related agent events.
   */
  function onWorkspaceInvalidated(listener: InvalidationListener): () => void {
    let set = invalidationListeners.get(workspaceId)
    if (!set) {
      set = new Set()
      invalidationListeners.set(workspaceId, set)
    }
    set.add(listener)
    return () => set!.delete(listener)
  }

  return {
    state: readonly(store) as Readonly<AgentState>,
    launchMode,
    isActive,
    pendingApproval,
    pendingInteraction,
    init,
    refreshStatus,
    loadRuns: () => loadRuns(workspaceId),
    openRun,
    startCommand,
    pause,
    resume,
    cancel,
    sendMessage,
    decide,
    respond,
    onWorkspaceChanged,
    onWorkspaceInvalidated,
    /**
     * Show the sidecar without touching the saved preference. A run started
     * from another surface has to be watchable there and then, and the drawer
     * closes again when the run settles rather than becoming the new default —
     * that is what `drawerAutoOpened` means.
     */
    openDrawer: () => {
      if (store.drawerOpen) return
      store.drawerOpen = true
      store.drawerAutoOpened = true
    },
    toggleDrawer: () => {
      store.drawerOpen = !store.drawerOpen
      store.drawerAutoOpened = false
      try {
        window.localStorage.setItem(`${PANEL_STORAGE_PREFIX}${workspaceId}`, String(store.drawerOpen))
      } catch {
        /* Storage can be unavailable in hardened/private browser contexts. */
      }
    },
  }
}
