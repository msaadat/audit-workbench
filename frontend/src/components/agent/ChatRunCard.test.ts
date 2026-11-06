import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import PrimeVue from 'primevue/config'

import type { AssistantQueuedCommand, AssistantRunProjection } from '../../types'
import ChatRunCard from './ChatRunCard.vue'

const post = vi.fn(async () => ({}))
vi.mock('../../api', () => ({ api: { get: vi.fn(async () => ({})), post: (...args: unknown[]) => post(...(args as [])) } }))
vi.mock('../../composables/useAgentRun', () => ({ useAgentRun: () => ({ openRun: vi.fn() }) }))

function projection(status: string, queued?: Partial<AssistantQueuedCommand>): AssistantRunProjection {
  return {
    id: 'run:R1:command:cmd_1', type: 'run', derived: true, run_id: 'R1',
    engine: 'workflow', workspace_id: 'WS-1', parent_run_id: null, kind: 'audit', mode: 'auto',
    status: status as AssistantRunProjection['status'],
    created: '2026-11-04T07:00:00Z', created_at: '2026-11-04T07:00:00Z',
    started: null, finished: null, error: null, has_summary: false,
    task_counts: { total: 0, completed: 0, failed: 0 },
    title: 'Queued for the agent', current_activity: 'waiting', pending_attention: false,
    summary_line: '', status_label: status === 'queued' ? 'Queued' : "Didn't run",
    plan_line: '', narration: [], blockers: [],
    queued_command: queued
      ? {
          command_id: 'cmd_1', state: 'queued', position: 1, waiting_on_run_id: 'R1',
          waiting_on_status: 'executing', resend_text: 'Draft the findings.', ...queued,
        }
      : undefined,
  }
}

function mountCard(item: AssistantRunProjection) {
  return mount(ChatRunCard, {
    props: { workspaceId: 'WS-1', projection: item },
    global: { plugins: [PrimeVue], stubs: { AgentNarration: true } },
  })
}

describe('a request queued behind another run', () => {
  it('does not draw the run ahead as its own: no spinner, no Pause or Stop', () => {
    const wrapper = mountCard(projection('queued', {}))

    expect(wrapper.find('[aria-label="Pause the run"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Stop the run"]').exists()).toBe(false)
    expect(wrapper.find('.aw-icon-loader-circle').exists()).toBe(false)
    expect(wrapper.find('.run-card').classes()).not.toContain('live')
  })

  it('offers to resume the paused run it is waiting behind', async () => {
    post.mockClear()
    const wrapper = mountCard(projection('queued', { waiting_on_status: 'paused' }))

    const button = wrapper.findAll('button').find(item => item.text() === 'Resume that run')
    expect(button).toBeTruthy()
    await button!.trigger('click')
    expect(post).toHaveBeenCalledWith('/api/workspaces/WS-1/agent/runs/R1/resume')
  })

  it('sends its own words again once it can no longer run', async () => {
    const wrapper = mountCard(projection('cancelled', { state: 'not_run' }))

    const button = wrapper.findAll('button').find(item => item.text() === 'Send again')
    expect(button).toBeTruthy()
    await button!.trigger('click')
    expect(wrapper.emitted('command')).toEqual([['Draft the findings.']])
  })

  it('leaves an ordinary live run its own controls', () => {
    const wrapper = mountCard(projection('executing'))

    expect(wrapper.find('[aria-label="Pause the run"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Stop the run"]').exists()).toBe(true)
  })
})
