import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * A chat response belongs on screen only while its chat is still the open one
 * and no later load has started. Switching quickly used to let the older
 * response win: the screen showed chat A while every action went to chat B.
 */

function chat(id: string, messages: Array<{ request_id: string }> = []) {
  return {
    id, title: id, transcript: [...messages], messages, runs: [], artifacts: {},
    composer_context: { document_ids: [] }, capabilities: { ask: true, act: true, commands: [] },
  }
}

type Deferred = { url: string; resolve: (value: unknown) => void; reject: (error: unknown) => void }
const gets: Deferred[] = []
const posts: Deferred[] = []

function deferred(list: Deferred[], url: string) {
  return new Promise((resolve, reject) => { list.push({ url, resolve, reject }) })
}

vi.mock('../api', () => ({
  api: {
    get: vi.fn((url: string) => url.includes('/assistant/chats?')
      ? Promise.resolve({ chats: [{ id: 'chat_a' }, { id: 'chat_b' }] })
      : deferred(gets, url)),
    post: vi.fn((url: string) => deferred(posts, url)),
  },
}))
vi.mock('./useAgentRun', () => ({ useAgentRun: () => ({ openRun: vi.fn(() => Promise.resolve()) }) }))

function pending(list: Deferred[], fragment: string): Deferred {
  const found = list.find(item => item.url.includes(fragment))
  if (!found) throw new Error(`no request for ${fragment}`)
  list.splice(list.indexOf(found), 1)
  return found
}

describe('chat loads that resolve out of order', () => {
  beforeEach(() => { gets.length = 0; posts.length = 0 })

  it('keeps the chat that was opened last, whichever response lands last', async () => {
    const { useAssistantChat } = await import('./useAssistantChat')
    const chats = useAssistantChat('ws_order')

    const toA = chats.switchChat('chat_a')
    const toB = chats.switchChat('chat_b')
    await vi.waitFor(() => expect(gets).toHaveLength(2))

    pending(gets, '/chats/chat_b').resolve(chat('chat_b'))
    await toB
    pending(gets, '/chats/chat_a').resolve(chat('chat_a'))
    await toA

    expect(chats.state.activeChatId).toBe('chat_b')
    expect(chats.state.chat?.id).toBe('chat_b')
  })

  it('does not erase a message still being sent when a refresh lands', async () => {
    const { useAssistantChat } = await import('./useAssistantChat')
    const chats = useAssistantChat('ws_pending')
    // `init` opens the first chat and marks the store ready, as the panel does.
    const opening = chats.init()
    await vi.waitFor(() => expect(gets).toHaveLength(1))
    pending(gets, '/chats/chat_a').resolve(chat('chat_a'))
    await opening

    const sending = chats.send('hello', 'ask', 'auto', { requestId: 'r1' })
    await vi.waitFor(() => expect(posts).toHaveLength(1))

    // A background refresh returns the chat as the server holds it: the
    // message is not in it yet.
    const refreshing = chats.refresh()
    await vi.waitFor(() => expect(gets).toHaveLength(1))
    pending(gets, '/chats/chat_a').resolve(chat('chat_a'))
    await refreshing
    expect(chats.state.chat?.transcript.some(item => 'request_id' in item && item.request_id === 'r1')).toBe(true)

    pending(posts, '/messages').resolve({ outcome: { kind: 'answer' }, chat: chat('chat_a', [{ request_id: 'r1' }]) })
    await sending
    expect(chats.state.chat?.transcript.filter(item => 'request_id' in item && item.request_id === 'r1')).toHaveLength(1)
  })

  it('does not show a reply in a chat the auditor has switched to', async () => {
    const { useAssistantChat } = await import('./useAssistantChat')
    const chats = useAssistantChat('ws_switch')
    const opening = chats.init()
    await vi.waitFor(() => expect(gets).toHaveLength(1))
    pending(gets, '/chats/chat_a').resolve(chat('chat_a'))
    await opening

    const sending = chats.send('to A', 'ask', 'auto', { requestId: 'r2' })
    await vi.waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].url).toContain('/chats/chat_a/messages')

    const toB = chats.switchChat('chat_b')
    await vi.waitFor(() => expect(gets).toHaveLength(1))
    pending(gets, '/chats/chat_b').resolve(chat('chat_b'))
    await toB

    pending(posts, '/messages').resolve({ outcome: { kind: 'answer' }, chat: chat('chat_a', [{ request_id: 'r2' }]) })
    await sending
    expect(chats.state.chat?.id).toBe('chat_b')
  })

  it('forgets every cached chat when the session resets', async () => {
    const { useAssistantChat, resetAssistantChatState } = await import('./useAssistantChat')
    const before = useAssistantChat('ws_reset')
    const opening = before.switchChat('chat_a')
    await vi.waitFor(() => expect(gets).toHaveLength(1))
    pending(gets, '/chats/chat_a').resolve(chat('chat_a'))
    await opening
    expect(before.state.chat?.id).toBe('chat_a')

    resetAssistantChatState()
    const after = useAssistantChat('ws_reset')
    expect(after.state.chat).toBeNull()
    expect(after.state.initialized).toBe(false)
  })
})
