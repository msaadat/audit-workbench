import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PrimeVue from 'primevue/config'

import type { AgentApproval } from '../../types'
import AgentApprovalCard from './AgentApprovalCard.vue'

globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

/**
 * A revision batch is read decision by decision: what happens to each row,
 * and what a revise rewrites. A section rewrite shows a before/after diff,
 * not a grid of fields with a page of Markdown squeezed into one cell.
 */

function approval(kind: string, items: AgentApproval['items']): AgentApproval {
  return { id: 'a1', kind, task_id: 't1', status: 'pending', created: '2026-10-29T09:00:00Z', items }
}

function item(id: string, title: string, spec: Record<string, unknown>) {
  return { id, title, rationale: 'Because.', spec, evidence: {}, decision: null, edited_spec: null }
}

function render(value: AgentApproval, runId?: string) {
  return mount(AgentApprovalCard, {
    props: { approval: value, busy: false, ...(runId ? { runId } : {}) },
    global: { plugins: [PrimeVue], directives: { tooltip: {} } },
  })
}

describe('AgentApprovalCard', () => {
  it('labels a matrix revision and shows before/after field diffs', () => {
    const wrapper = render(approval('rcm_revision', [
      item('i1', 'Retire RCM-1: Duplicate payments', { rcm_id: 'RCM-1', decision: 'retire', reason: 'Out of scope.', tests: 2 }),
      item('i2', 'Revise RCM-2: Unapproved payments', {
        rcm_id: 'RCM-2',
        decision: 'revise',
        reason: 'Widened.',
        before_fields: { risk: 'Unapproved payments' },
        changes: { risk: 'Payments are not approved within limits', control_attributes: [] },
      }),
    ]))

    expect(wrapper.text()).toContain('Matrix revision')
    expect(wrapper.text()).toContain('Retire')
    expect(wrapper.text()).toContain('Revise')
    expect(wrapper.find('.changes del').text()).toBe('Unapproved payments')
    expect(wrapper.find('.changes ins').text()).toBe('Payments are not approved within limits')
    expect(wrapper.find('.changes').text()).not.toContain('control attributes')
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).not.toContain('decision')
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).not.toContain('before fields')
  })

  it('renders a unified diff for a rewritten memorandum section instead of dumping fields', () => {
    const wrapper = render(approval('apm_revision', [
      item('i1', 'Rewrite section: Fraud risk', {
        heading: 'Fraud risk',
        markdown: 'Management override is presumed.',
        before: 'A long earlier body.',
        reason: 'Asked.',
      }),
    ]))

    expect(wrapper.text()).toContain('Memorandum revision')
    expect(wrapper.find('.diff-view .diff-line.del').text()).toContain('A long earlier body.')
    expect(wrapper.find('.diff-view .diff-line.add').text()).toContain('Management override is presumed.')
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).not.toContain('before')
  })

  it('rejects the batch and emits contextual feedback when requesting changes', async () => {
    const wrapper = render(approval('apm_revision', [
      item('i1', 'Rewrite section: Fraud risk', { heading: 'Fraud risk', markdown: 'New body.' }),
    ]), 'run-42')

    const requestBtn = wrapper.findAll('button').find(btn => btn.text().includes('Request changes'))
    expect(requestBtn).toBeTruthy()
    await requestBtn!.trigger('click')
    await wrapper.find('.feedback-form textarea').setValue('Keep it to two bullet points.')
    await wrapper.find('.feedback-form').trigger('submit')

    expect(wrapper.emitted('decide')).toEqual([[ [{ item_id: 'i1', action: 'reject' }] ]])
    const feedback = wrapper.emitted('feedback')?.[0]?.[0] as string
    expect(feedback).toContain('Memorandum revision')
    expect(feedback).toContain('run run-42')
    expect(feedback).toContain('Rewrite section: Fraud risk')
    expect(feedback).toContain('Keep it to two bullet points.')
  })

  it('leaves other batches as they were', () => {
    const wrapper = render(approval('rcm', [item('i1', 'Duplicate payments', { risk: 'Duplicate payments', decision: 'revise' })]))
    expect(wrapper.find('.changes').exists()).toBe(false)
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).toContain('decision')
  })
})
