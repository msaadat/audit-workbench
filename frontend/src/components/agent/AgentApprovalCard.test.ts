import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentApproval } from '../../types'
import AgentApprovalCard from './AgentApprovalCard.vue'

/**
 * A revision batch is read decision by decision: what happens to each row,
 * and what a revise rewrites. A section rewrite shows its new text, not a
 * grid of fields with a page of Markdown squeezed into one cell.
 */

function approval(kind: string, items: AgentApproval['items']): AgentApproval {
  return { id: 'a1', kind, task_id: 't1', status: 'pending', created: '2026-10-29T09:00:00Z', items }
}

function item(id: string, title: string, spec: Record<string, unknown>) {
  return { id, title, rationale: 'Because.', spec, evidence: {}, decision: null, edited_spec: null }
}

function render(value: AgentApproval) {
  return mount(AgentApprovalCard, {
    props: { approval: value, busy: false },
    global: { directives: { tooltip: {} } },
  })
}

describe('AgentApprovalCard', () => {
  it('labels a matrix revision and tags each decision', () => {
    const wrapper = render(approval('rcm_revision', [
      item('i1', 'Retire RCM-1: Duplicate payments', { rcm_id: 'RCM-1', decision: 'retire', reason: 'Out of scope.', tests: 2 }),
      item('i2', 'Revise RCM-2: Unapproved payments', {
        rcm_id: 'RCM-2',
        decision: 'revise',
        reason: 'Widened.',
        changes: { risk: 'Payments are not approved within limits', control_attributes: [] },
      }),
    ]))

    expect(wrapper.text()).toContain('Matrix revision')
    expect(wrapper.text()).toContain('Retire')
    expect(wrapper.text()).toContain('Revise')
    // The rewritten field is listed; the attributes and the raw decision key are not.
    expect(wrapper.find('.changes').text()).toContain('Payments are not approved within limits')
    expect(wrapper.find('.changes').text()).not.toContain('control attributes')
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).not.toContain('decision')
  })

  it('previews a rewritten memorandum section instead of dumping its fields', () => {
    const wrapper = render(approval('apm_revision', [
      item('i1', 'Rewrite section: Fraud risk', {
        heading: 'Fraud risk',
        markdown: 'Management override is presumed.',
        before: 'A long earlier body.',
        reason: 'Asked.',
      }),
    ]))

    expect(wrapper.text()).toContain('Memorandum revision')
    expect(wrapper.find('.section-preview').text()).toBe('Management override is presumed.')
    expect(wrapper.text()).not.toContain('A long earlier body.')
  })

  it('leaves other batches as they were', () => {
    const wrapper = render(approval('rcm', [item('i1', 'Duplicate payments', { risk: 'Duplicate payments', decision: 'revise' })]))
    expect(wrapper.find('.changes').exists()).toBe(false)
    expect(wrapper.findAll('.spec-summary dt').map((node) => node.text())).toContain('decision')
  })
})
