import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick, reactive, ref } from 'vue'


import PrimeVue from 'primevue/config'

import { EXPAND_BELOW, affordableMode } from '../../composables/useAgentRun'
import type { PanelMode } from '../../composables/useAgentRun'
import type { WorkspaceSummary } from '../../types'
import AssistantPanel from './AssistantPanel.vue'

const store = reactive({ panelMode: 'docked' as PanelMode, run: null, stream: null, connected: true })
const setPanelMode = vi.fn((mode: PanelMode) => { store.panelMode = mode })
const togglePanel = vi.fn(() => { store.panelMode = store.panelMode === 'closed' ? 'docked' : 'closed' })

vi.mock('../../composables/useAgentRun', async importActual => {
  const actual = await importActual<typeof import('../../composables/useAgentRun')>()
  return {
    ...actual,
    useAgentRun: () => ({
      state: store, isActive: ref(false), launchMode: ref('auto'),
      setPanelMode, togglePanel, openPanel: vi.fn(),
      onWorkspaceInvalidated: () => () => undefined,
    }),
  }
})

const routeState = reactive({ fullPath: '/workspace/WS-1' })
vi.mock('vue-router', async importActual => ({
  ...(await importActual<typeof import('vue-router')>()),
  useRoute: () => routeState,
}))

vi.mock('../../composables/useAssistantChat', () => ({
  useAssistantChat: () => ({
    state: {
      summaries: [
        { id: 'c1', title: 'Generate planned test', updated_at: '2026-09-02T00:08:00Z', message_count: 5, last_run_status: 'running', failed_run_count: 0, run_count: 2 },
        { id: 'c2', title: 'Analyse the imported tables', updated_at: '2026-09-01T23:05:00Z', message_count: 10, last_run_status: 'completed', failed_run_count: 3, run_count: 4 },
      ],
      activeChatId: 'c1',
      chat: { runs: [], transcript: [] },
      loading: false, busy: false, capabilities: [],
    },
    switchChat: vi.fn(), createChat: vi.fn(),
  }),
}))

globalThis.ResizeObserver ??= class {
  observe() {} unobserve() {} disconnect() {}
} as unknown as typeof ResizeObserver

// One panel at a time: they share the agent store, so a panel left mounted
// from an earlier case answers the route change first and the case under test
// then sees a mode it did not set.
let mounted: ReturnType<typeof mount> | null = null
afterEach(() => { mounted?.unmount(); mounted = null })

function mountPanel(mode: PanelMode) {
  store.panelMode = mode
  mounted = mount(AssistantPanel, {
    props: { workspace: { id: 'WS-1', name: 'Procurement', tables: [] } as unknown as WorkspaceSummary },
    global: {
      plugins: [PrimeVue],
      stubs: {
        ConsoleThread: { template: '<div class="thread"><slot name="head-actions" /></div>' },
        PlanSpine: true, PlanStrip: true, EngagementState: true, Popover: true,
      },
    },
  })
  return mounted
}

describe('the assistant in three widths', () => {
  it('mounts nothing at all when it is closed', () => {
    expect(mountPanel('closed').find('.assistant-panel').exists()).toBe(false)
  })

  it('docks as a column beside the page, with the live plan strip', () => {
    const wrapper = mountPanel('docked')

    expect(wrapper.find('.assistant-panel').classes()).not.toContain('expanded')
    expect(wrapper.find('.chats-column').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'PlanStrip' }).exists()).toBe(true)
    // The width is a preference, so the column can be resized.
    expect(wrapper.find('.resize-handle').exists()).toBe(true)
  })

  it('expands to chats, thread and rail, and drops the strip for the rail card', () => {
    const wrapper = mountPanel('expanded')

    expect(wrapper.find('.assistant-panel').classes()).toContain('expanded')
    expect(wrapper.find('.chats-column').exists()).toBe(true)
    expect(wrapper.find('.rail-column').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'PlanStrip' }).exists()).toBe(false)
    // Nothing to resize: it takes the workspace area.
    expect(wrapper.find('.resize-handle').exists()).toBe(false)
  })

  it('goes back to docked on Escape, leaving the page underneath', async () => {
    mountPanel('expanded')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))

    expect(setPanelMode).toHaveBeenCalledWith('docked')
  })

  it('docks when a link out of the transcript navigates, so the page is visible', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: EXPAND_BELOW + 1, configurable: true })
    mountPanel('expanded')
    setPanelMode.mockClear()

    routeState.fullPath = '/workspace/WS-1/findings'
    await Promise.resolve()

    expect(setPanelMode).toHaveBeenCalledWith('docked')
    Object.defineProperty(window, 'innerWidth', { value: original, configurable: true })
  })

  it('closes instead, on a window too narrow to dock into', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: EXPAND_BELOW - 1, configurable: true })
    routeState.fullPath = '/workspace/WS-1'
    mountPanel('expanded')
    await nextTick()
    setPanelMode.mockClear()

    routeState.fullPath = '/workspace/WS-1/data-tests'
    await nextTick()

    // Docking here would be answered with expansion, and the link would once
    // again look like it did nothing.
    expect(setPanelMode).toHaveBeenCalledWith('closed')
    Object.defineProperty(window, 'innerWidth', { value: original, configurable: true })
  })

  it('stays expanded when the shell strips its own query keys', async () => {
    routeState.fullPath = '/workspace/WS-1?assistant=full'
    mountPanel('expanded')
    setPanelMode.mockClear()

    routeState.fullPath = '/workspace/WS-1'
    await Promise.resolve()

    expect(setPanelMode).not.toHaveBeenCalled()
  })

  it('does not take Escape while docked, because the composer owns it', () => {
    mountPanel('docked')
    setPanelMode.mockClear()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))

    expect(setPanelMode).not.toHaveBeenCalled()
  })
})

describe('what the window can accommodate', () => {
  it('answers a request to dock with expansion when the page would be squeezed', () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: EXPAND_BELOW - 1, configurable: true })
    expect(affordableMode('docked')).toBe('expanded')

    Object.defineProperty(window, 'innerWidth', { value: EXPAND_BELOW + 1, configurable: true })
    expect(affordableMode('docked')).toBe('docked')

    // Closed and expanded are always affordable.
    expect(affordableMode('closed')).toBe('closed')
    expect(affordableMode('expanded')).toBe('expanded')
    Object.defineProperty(window, 'innerWidth', { value: original, configurable: true })
  })
})
