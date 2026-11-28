import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import PrimeVue from 'primevue/config'

import type { AssistantCapabilities } from '../../types'
import ChatComposer from './ChatComposer.vue'
import ChatList from './ChatList.vue'
import RunContextCard from './RunContextCard.vue'

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

const push = vi.fn()
vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ push, to: vi.fn() }),
}))

const capabilities: AssistantCapabilities = {
  ask: true,
  act: true,
  assistant: { configured: true, backend: 'fake', model: 'fake', base_url: '' },
  agent: { configured: true, backend: 'fake', model: 'fake', base_url: '' },
  commands: [
    { id: 'generate_apm', slash: 'generate apm', label: 'Generate APM', description: 'Draft APM', accepts_arguments: false },
    { id: 'revise_rcm', slash: 'revise rcm', label: 'Revise RCM', description: 'Revise RCM', accepts_arguments: true },
  ],
}

describe('ChatComposer argument-bearing slash commands', () => {
  it('inserts a trailing space and closes the menu while typing an instruction', async () => {
    const wrapper = mount(ChatComposer, {
      props: {
        busy: false,
        capabilities,
        mode: 'auto',
        documents: [],
        selectedIds: [],
      },
      global: { plugins: [PrimeVue] },
    })

    const textarea = wrapper.find('textarea')
    await textarea.setValue('/revise')
    await nextTick()
    expect(wrapper.find('.command-menu').exists()).toBe(true)

    await wrapper.find('.command-menu li').trigger('mousedown')
    await nextTick()
    expect((textarea.element as HTMLTextAreaElement).value).toBe('/revise rcm ')
    expect(wrapper.find('.command-menu').exists()).toBe(false)

    await textarea.setValue('/revise rcm merge duplicate vendor risks')
    await nextTick()
    expect(wrapper.find('.command-menu').exists()).toBe(false)
  })

  it('offers @-mention autocomplete, inserts typed refs, shows active view context, and exposes cancel when busy', async () => {
    const wrapper = mount(ChatComposer, {
      props: {
        busy: true,
        capabilities,
        mode: 'auto',
        documents: [],
        selectedIds: [],
        activeContext: { surface: 'rcm', label: 'RCM-4', ref: 'rcm:RCM-4' },
        mentions: [
          { ref: 'rcm:RCM-4', kind: 'rcm', label: 'Vendor approval risk', detail: 'RCM row' },
          { ref: 'table:invoices', kind: 'table', label: 'invoices', detail: 'Table' },
        ],
      },
      global: { plugins: [PrimeVue] },
    })

    expect(wrapper.find('.view-chip').text()).toContain('Viewing: RCM-4')
    expect(wrapper.find('[aria-label="Cancel reply"]').exists()).toBe(true)

    const textarea = wrapper.find('textarea')
    await textarea.setValue('Check @inv')
    await nextTick()
    expect(wrapper.find('.mention-menu').exists()).toBe(true)
    expect(wrapper.find('.mention-menu').text()).toContain('@table:invoices')

    await wrapper.find('.mention-menu li').trigger('mousedown')
    await nextTick()
    expect((textarea.element as HTMLTextAreaElement).value).toBe('Check @table:invoices ')
    await textarea.setValue('Check @RCM-1')
    expect(wrapper.emitted('mention-query')?.at(-1)).toEqual(['rcm-1'])
  })
})

describe('ChatList active status indicator', () => {
  it('marks executing, interpreting, and verifying runs as running', () => {
    const wrapper = mount(ChatList, {
      props: {
        activeId: 'c1',
        chats: [
          { id: 'c1', workspace_id: 'WS-1', title: 'Live audit', title_source: 'auto', created_at: '2026-11-28T10:00:00Z', updated_at: '2026-11-28T10:01:00Z', message_count: 2, last_run_status: 'executing', failed_run_count: 0, run_count: 1 },
        ],
      },
      global: { plugins: [PrimeVue], stubs: { UiOverflowMenu: true } },
    })

    expect(wrapper.find('.dot').attributes('data-tone')).toBe('info')
    expect(wrapper.find('.meta').text()).toContain('running')
  })
})

describe('RunContextCard document navigation', () => {
  it('navigates to the clicked document', async () => {
    push.mockClear()
    const wrapper = mount(RunContextCard, {
      props: {
        context: {
          at: '2026-11-28T10:00:00Z',
          stage_title: 'APM',
          sentence: 'Reading 1 document',
          artifacts: [],
          documents: [{ document_id: 'DOC-42', name: 'policy.pdf', category: 'policy', pages: 4 }],
          withheld: [],
          supporting: [],
          unavailable: [],
        },
      },
    })

    await wrapper.find('button.row').trigger('click')
    expect(push).toHaveBeenCalledWith('documents', { doc: 'DOC-42' })
  })
})
