import { computed, reactive, readonly } from 'vue'

import { api } from '../api'
import type { DecisionsPayload } from '../types'
import { useAgentRun } from './useAgentRun'

/**
 * The decision queue, one store per workspace so the surface switcher's badge
 * and the queue itself never disagree about how much is waiting.
 *
 * The queue is derived from durable workspace state, so any agent commit can
 * change it. Subscribing to the same invalidation signal the shell uses keeps
 * the count live while a run works without polling.
 */

interface DecisionsState {
  payload: DecisionsPayload | null
  loading: boolean
  error: string | null
}

const stores = new Map<string, DecisionsState>()
const subscribed = new Set<string>()

function state(workspaceId: string): DecisionsState {
  let value = stores.get(workspaceId)
  if (!value) {
    value = reactive<DecisionsState>({ payload: null, loading: false, error: null })
    stores.set(workspaceId, value)
  }
  return value
}

export function useDecisions(workspaceId: string) {
  const store = state(workspaceId)

  async function load() {
    store.loading = true
    try {
      store.payload = await api.get<DecisionsPayload>(`/api/workspaces/${workspaceId}/decisions`)
      store.error = null
    } catch (error) {
      store.error = error instanceof Error ? error.message : String(error)
    } finally {
      store.loading = false
    }
  }

  /** Attach once per workspace; later callers reuse the same subscription. */
  function watchWorkspace() {
    if (subscribed.has(workspaceId)) return
    subscribed.add(workspaceId)
    useAgentRun(workspaceId).onWorkspaceInvalidated(() => { void load() })
  }

  return {
    state: readonly(store) as Readonly<DecisionsState>,
    items: computed(() => store.payload?.items ?? []),
    total: computed(() => store.payload?.total ?? 0),
    load,
    watchWorkspace,
  }
}
