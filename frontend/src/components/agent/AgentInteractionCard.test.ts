import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PrimeVue from 'primevue/config'

import type { AgentInteraction } from '../../types'
import AgentInteractionCard from './AgentInteractionCard.vue'

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

function clarification(overrides: Partial<AgentInteraction> = {}): AgentInteraction {
  return {
    id: 'int_1',
    action_id: 'agent:loop',
    type: 'clarification',
    prompt: 'Which approach should I take?',
    options: [
      { id: '0', label: 'Keep existing matrix' },
      { id: '1', label: 'Merge duplicate risks' },
    ],
    payload: {},
    policy_reason: 'The answer changes what the agent does next.',
    status: 'pending',
    response: null,
    actor: null,
    created_at: '2026-11-28T10:00:00Z',
    resolved_at: null,
    ...overrides,
  }
}

function mountCard(interaction: AgentInteraction) {
  return mount(AgentInteractionCard, {
    props: { interaction, busy: false, workspaceId: 'WS-1', runId: 'R1' },
    global: { plugins: [PrimeVue] },
  })
}

describe('AgentInteractionCard clarification options', () => {
  it('renders clickable option buttons and emits choice + option_id + text on click', async () => {
    const wrapper = mountCard(clarification())
    const choices = wrapper.findAll('button.choice')
    expect(choices).toHaveLength(2)
    expect(choices[0].text()).toContain('Keep existing matrix')
    expect(choices[1].text()).toContain('Merge duplicate risks')

    await choices[1].trigger('click')
    expect(wrapper.emitted('respond')).toEqual([
      [{ choice: '1', option_id: '1', text: 'Merge duplicate risks' }],
    ])
  })

  it('renders no option buttons when clarification has no options', () => {
    const wrapper = mountCard(clarification({ options: [] }))
    expect(wrapper.findAll('button.choice')).toHaveLength(0)
    expect(wrapper.find('textarea').exists()).toBe(true)
  })

  it('formats resolved summary for option_id responses', () => {
    const wrapper = mountCard(clarification({
      status: 'resolved',
      response: { option_id: '1' },
    }))
    expect(wrapper.text()).toContain('Selected: Merge duplicate risks')
  })
})
