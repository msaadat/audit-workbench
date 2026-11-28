import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import PrimeVue from 'primevue/config'

import { api } from '../../api'
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

  it('renders a unified diff for backend preview.args shape and emits contextual Request changes feedback', async () => {
    const wrapper = mountCard(clarification({
      type: 'proposal_approval',
      options: [],
      prompt: 'Review and approve: Edit an exact passage.',
      payload: {
        preview: {
          action_type: 'edit_document_text',
          target: 'planning:apm',
          title: 'Audit Planning Memorandum',
          args: {
            old_str: 'Old materiality threshold is $50,000.',
            new_str: 'Updated materiality threshold is $25,000.',
          },
        },
      },
    }))

    expect(wrapper.find('.diff-view .diff-line.del').text()).toContain('$50,000')
    expect(wrapper.find('.diff-view .diff-line.add').text()).toContain('$25,000')

    const reqBtn = wrapper.findAll('button').find(btn => btn.text().includes('Request changes'))
    expect(reqBtn).toBeTruthy()
    await reqBtn!.trigger('click')
    await wrapper.find('.feedback-form textarea').setValue('Use $30,000 instead.')
    await wrapper.find('.feedback-form').trigger('submit')

    expect(wrapper.emitted('respond')).toEqual([[{ decision: 'reject', reason: 'Use $30,000 instead.' }]])
    const emittedFeedback = wrapper.emitted('feedback')?.[0]?.[0] as string
    expect(emittedFeedback).toContain('run R1')
    expect(emittedFeedback).toContain('Audit Planning Memorandum')
    expect(emittedFeedback).toContain('planning:apm')
    expect(emittedFeedback).toContain('Use $30,000 instead.')
  })

  it('loads edit_document_text diff from the sidecar when preview.args is Stored in sidecar', async () => {
    const spy = vi.spyOn(api, 'get').mockResolvedValueOnce({
      action_type: 'edit_document_text',
      args: {
        old_str: 'Long original passage.',
        new_str: 'Long revised passage.',
      },
    })
    try {
      const wrapper = mountCard(clarification({
        type: 'proposal_approval',
        options: [],
        prompt: 'Review and approve: Edit an exact passage.',
        payload: {
          sidecar: { sha1: 'abc123' },
          preview: {
            action_type: 'edit_document_text',
            target: 'report:working',
            title: 'Draft Audit Report',
            args: 'Stored in sidecar',
          },
        },
      }))
      await flushPromises()
      expect(wrapper.find('.diff-view .diff-line.del').text()).toContain('Long original passage.')
      expect(wrapper.find('.diff-view .diff-line.add').text()).toContain('Long revised passage.')
    } finally {
      spy.mockRestore()
    }
  })
})
