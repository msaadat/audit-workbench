import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * A message the chat cannot send must say so. The composer clears its draft on
 * submit, and `send` used to return nothing while a previous message was still
 * in flight — the auditor's text vanished with no bubble, no error and no Retry.
 */

const chat = {
  id: 'chat_1', title: 'Chat', transcript: [], messages: [], runs: [],
  composer_context: { document_ids: [] }, capabilities: { ask: true, act: true, commands: [] },
}
let releasePost: (value: unknown) => void = () => {}
const post = vi.fn(() => new Promise(resolve => { releasePost = resolve }))

vi.mock('../api', () => ({
  api: {
    get: vi.fn((url: string) => Promise.resolve(
      url.includes('/assistant/chats?') ? { chats: [{ id: 'chat_1' }] } : chat,
    )),
    post: (...args: unknown[]) => post(...(args as [])),
  },
}))
vi.mock('./useAgentRun', () => ({ useAgentRun: () => ({ openRun: vi.fn(() => Promise.resolve()) }) }))

describe('sending a chat message', () => {
  beforeEach(() => { post.mockClear() })

  it('refuses a second message while the first is in flight, instead of dropping it', async () => {
    const { useAssistantChat, ChatNotSentError } = await import('./useAssistantChat')
    const chats = useAssistantChat('ws_send')

    const first = chats.send('first message')
    await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1))

    await expect(chats.send('second message')).rejects.toBeInstanceOf(ChatNotSentError)
    expect(post).toHaveBeenCalledTimes(1)

    releasePost({ outcome: { kind: 'answer' }, chat })
    await first
    expect(chats.state.busy).toBe(false)
  })
})
