import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import CycleRulesetReview from './CycleRulesetReview.vue'

// Message renders its content in a slot; stubbing it away would hide exactly
// what these assertions are about — the concerns raised against a rule.
vi.mock('primevue/message', () => ({
  default: { template: '<aside><slot /></aside>' },
}))

vi.mock('primevue/dialog', () => ({
  default: {
    props: ['visible'], emits: ['update:visible'],
    template: '<section v-if="visible"><slot /><footer><slot name="footer" /></footer></section>',
  },
}))

function ruleset(overrides: Record<string, unknown> = {}) {
  return {
    ruleset_id: 'lnk-1',
    status: 'proposed',
    cycle_label: 'Procure to pay',
    roles: [
      { name: 'invoice', document_type: 'vendor_invoice', cardinality: 'one', required: true },
      { name: 'order', document_type: 'purchase_order', cardinality: 'one', required: true },
    ],
    anchor: { table: 'register', column: 'INVOICE_NO', role: 'invoice', field: 'invoice_number' },
    join_keys: [{
      id: 'jk', match: 'normalized_equal',
      left: { role: 'invoice', field: 'order_number' },
      right: { role: 'order', field: 'order_number' },
      rationale: 'An invoice cites the order it bills against.',
    }],
    assertions: [{
      id: 'as_total', label: 'Totals agree',
      left: { role: 'invoice', field: 'total_amount' },
      right: { role: 'order', field: 'total_amount' },
      operator: 'numeric_within', tolerance: { absolute: 1 },
      rationale: 'The amount billed must be the amount ordered.',
    }],
    schema_refs: [], ruleset_hash: 'sha256:x', proposed_by: 'agent',
    approved_by: null, approved_at: null, created: '', updated: '',
    measured: {
      join_keys: {
        jk: {
          left_documents: 3, right_documents: 3, left_stating_key: 3,
          matched_pairs: 3, left_unmatched: 0,
          fan_out_p50: 1, fan_out_p95: 1, fan_out_max: 1,
        },
      },
      assertions: {
        as_total: { left_stating: 3, right_stating: 3, evaluable_records: 3, silent: false },
      },
      records_measured: 6,
    },
    concerns: [],
    ...overrides,
  }
}

function stubApi(record = ruleset()) {
  return vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url.endsWith('/cycle-rulesets')) {
      return {
        items: [record], effective_ruleset_id: null, schemas: [], types_present: [],
      } as never
    }
    return record as never
  })
}

function render() {
  return mount(CycleRulesetReview, {
    props: { workspaceId: 'ws-1', approverId: 'auditor@example.com', modelValue: true },
    global: { stubs: { Button: true, Tag: true } },
  })
}

afterEach(() => vi.restoreAllMocks())

describe('CycleRulesetReview', () => {
  it('shows each rule with the reason it was proposed', async () => {
    stubApi()
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('An invoice cites the order it bills against.')
    expect(wrapper.text()).toContain('The amount billed must be the amount ordered.')
  })

  it('shows join keys as the document types they actually relate', async () => {
    stubApi()
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('vendor_invoice.order_number')
    expect(wrapper.text()).toContain('purchase_order.order_number')
  })

  it('surfaces fan-out, which is what a join key is approved on', async () => {
    stubApi()
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('Reaches per value (p95)')
    expect(wrapper.text()).toContain('Unmatched')
  })

  it('marks a runaway fan-out rather than leaving it as one number among many', async () => {
    stubApi(ruleset({
      measured: {
        join_keys: {
          jk: {
            left_documents: 6, right_documents: 6, left_stating_key: 6,
            matched_pairs: 36, left_unmatched: 0,
            fan_out_p50: 6, fan_out_p95: 6, fan_out_max: 6,
          },
        },
        assertions: {},
        records_measured: 12,
      },
      concerns: [{
        rule: 'jk', kind: 'join_key', concern: 'entity_fan_out',
        detail: 'Values of this key reach 6 records at the 95th percentile.',
      }],
    }))
    const wrapper = render()
    await flushPromises()

    expect(wrapper.find('.rule__measured--alarming').exists()).toBe(true)
    expect(wrapper.html()).toContain('reach 6 records')
  })

  it('approves with the reviewer identity, never anonymously', async () => {
    stubApi()
    const post = vi.spyOn(api, 'post').mockResolvedValue(ruleset({ status: 'approved' }) as never)
    const wrapper = render()
    await flushPromises()

    await (wrapper.vm as never as { approve: () => Promise<void> }).approve()

    expect(post).toHaveBeenCalledWith(
      '/api/workspaces/ws-1/cycle-rulesets/lnk-1/approve',
      { approved_by: 'auditor@example.com' },
    )
    expect(wrapper.emitted('approved')).toHaveLength(1)
  })

  it('reports a failed approval instead of implying the rules took effect', async () => {
    stubApi()
    vi.spyOn(api, 'post').mockRejectedValue(new Error('nope'))
    const wrapper = render()
    await flushPromises()

    await (wrapper.vm as never as { approve: () => Promise<void> }).approve()

    expect(wrapper.emitted('error')).toHaveLength(1)
    expect(wrapper.emitted('approved')).toBeUndefined()
  })

  it('offers nothing to review when no proposal is outstanding', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      items: [ruleset({ status: 'superseded' })],
      effective_ruleset_id: 'lnk-0', schemas: [], types_present: [],
    } as never)
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('No cycle rules are waiting for review')
  })
})
