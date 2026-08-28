import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Signing out has to drop every piece of module-global state that is keyed by
 * workspace id or holds a live connection. Two accounts on one machine can own
 * workspaces with the same name, and an EventSource opened under one session
 * keeps streaming under the next — so a missed teardown leaks one user's data
 * into another user's screen, or produces revision conflicts that look like
 * corruption.
 */

const closed: string[] = []

class FakeEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  constructor(public url: string) {}
  addEventListener() {}
  close() {
    closed.push(this.url)
  }
}

vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return {
    ok: (init.status ?? 200) < 400,
    status: init.status ?? 200,
    headers: { get: (key: string) => init.headers?.[key] ?? null },
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response
}

describe('sign-out teardown', () => {
  beforeEach(() => {
    closed.length = 0
    vi.resetModules()
  })

  it('forgets workspace revisions, so the next user does not inherit a stale ETag', async () => {
    const { api } = await import('../api')
    const { useSession } = await import('./useSession')

    const fetchMock = vi.fn()
    // A workspace read that publishes its revision, then the logout call.
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: 'ws_1' }, { headers: { 'X-Workspace-Revision': '7' } }),
    )
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }))
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api.get('/api/workspaces/ws_1')
    // The remembered revision is sent back as If-Match on the next mutation.
    await api.post('/api/workspaces/ws_1/joins', {})
    const beforeHeaders = fetchMock.mock.calls[1][1].headers as Record<string, string>
    expect(beforeHeaders['If-Match']).toBe('"rev-7"')

    await useSession().signOut()

    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }))
    await api.post('/api/workspaces/ws_1/joins', {})
    const afterHeaders = fetchMock.mock.calls[3][1].headers as Record<string, string>
    expect(afterHeaders['If-Match']).toBeUndefined()
  })

  it('closes every open event stream', async () => {
    const { useAgentRun } = await import('./useAgentRun')
    const { useSession } = await import('./useSession')

    // An active run is what opens a stream, so both workspaces answer with one.
    const activeRun = {
      id: 'run_1', status: 'executing', approvals: [], interactions: [],
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(activeRun)))

    await useAgentRun('ws_1').openRun('run_1')
    await useAgentRun('ws_2').openRun('run_1')
    expect(closed).toEqual([])

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ ok: true })))
    await useSession().signOut()
    expect(closed).toHaveLength(2)
  })

  it('drops the local session even when the logout request fails', async () => {
    const { useSession } = await import('./useSession')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))

    const session = useSession()
    session.state.user = {
      id: 'u_1', email: 'a@b.com', display_name: 'A',
      is_admin: false, status: 'active', created_at: '2026-01-01',
    }

    await expect(session.signOut()).rejects.toThrow('offline')
    // Leaving a signed-out user looking at the previous account's cached
    // workspace list would be worse than an unreported network error.
    expect(session.state.user).toBeNull()
  })
})
