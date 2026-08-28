import { computed, reactive } from 'vue'

import { api, resetApiState, setUnauthorizedHandler } from '../api'
import { resetAgentState } from './useAgentRun'

export interface SessionUser {
  id: string
  email: string
  display_name: string
  is_admin: boolean
  status: string
  created_at: string
}

export interface SessionIdentity {
  user: SessionUser | null
  auth_mode: string
  single_user: boolean
}

interface SessionState {
  user: SessionUser | null
  singleUser: boolean
  ready: boolean
}

const state = reactive<SessionState>({ user: null, singleUser: true, ready: false })

/**
 * Everything module-global that is keyed by workspace id or holds a live
 * connection. Two accounts can own workspaces with the same name, and an
 * EventSource opened under one session keeps streaming under the next, so this
 * has to run on every sign-out and on every 401.
 */
function clearClientState(): void {
  resetApiState()
  resetAgentState()
}

let bootstrap: Promise<SessionIdentity> | null = null

/**
 * Ask the server who we are. Deduplicated: the router guard and the app shell
 * both want this on first paint, and it must not become two requests.
 */
export function loadIdentity(force = false): Promise<SessionIdentity> {
  if (force) bootstrap = null
  if (!bootstrap) {
    bootstrap = api
      .get<SessionIdentity>('/api/auth/me')
      .then((identity) => {
        state.user = identity.user
        state.singleUser = identity.single_user
        state.ready = true
        return identity
      })
      .catch((error) => {
        // A failed bootstrap must not latch: the next navigation retries rather
        // than stranding the app on a blank screen.
        bootstrap = null
        state.ready = true
        throw error
      })
  }
  return bootstrap
}

export async function signIn(email: string, password: string): Promise<SessionUser> {
  const result = await api.post<{ user: SessionUser }>('/api/auth/login', { email, password })
  clearClientState()
  state.user = result.user
  bootstrap = null
  return result.user
}

export async function acceptInvite(
  token: string,
  password: string,
  displayName: string,
): Promise<SessionUser> {
  const result = await api.post<{ user: SessionUser }>(`/api/auth/invite/${token}`, {
    password,
    display_name: displayName,
  })
  clearClientState()
  state.user = result.user
  bootstrap = null
  return result.user
}

export async function signOut(): Promise<void> {
  try {
    await api.post('/api/auth/logout')
  } finally {
    // Local state is dropped even if the request failed: leaving a signed-out
    // user looking at another account's cached workspace list would be worse
    // than an unreported network error.
    clearClientState()
    state.user = null
    bootstrap = null
  }
}

/** Called by the API client when any request comes back 401. */
function onSessionLost(): void {
  clearClientState()
  state.user = null
  bootstrap = null
}

setUnauthorizedHandler(onSessionLost)

export function useSession() {
  return {
    state,
    user: computed(() => state.user),
    singleUser: computed(() => state.singleUser),
    signedIn: computed(() => state.user !== null),
    isAdmin: computed(() => state.user?.is_admin === true),
    loadIdentity,
    signIn,
    signOut,
    acceptInvite,
  }
}
