import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import CycleTab from './CycleTab.vue'
import type { CycleGraph, CycleShape } from '../../types'

/**
 * The page has two states and both are legitimate: a workspace whose cycle has
 * not been designed, and one whose strip is drawn. It must say which it is in
 * rather than showing an empty canvas either way — the shape and the bindings
 * arrive at different times by design, and a reader should not have to guess
 * whether a strip with no arrows is broken or merely early.
 */

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))
vi.mock('../../composables/useSession', () => ({
  useSession: () => ({ user: { value: { email: 'auditor@example.com' } } }),
}))

const responses: Record<string, unknown> = {}
const patched: Array<{ url: string; body: unknown }> = []

vi.mock('../../api', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    get: (url: string) =>
      Promise.resolve(
        url.endsWith('/planning/cycle/graph') ? responses.graph : responses.planning,
      ),
    patch: (url: string, body: unknown) => {
      patched.push({ url, body })
      return Promise.resolve({})
    },
  },
}))

const SHAPE: CycleShape = {
  name: 'Procure-to-pay',
  steps: [
    {
      name: 'Purchase order',
      roles: [{ name: 'order', document_type: 'purchase_order' }],
      populations: [{ table: 'po_data', anchor: true }],
      themes: [],
    },
  ],
  cross_cutting: { name: 'Procurement operations', themes: [] },
  created_by: 'agent',
  agent_run_id: 'run-1',
  apm_sha1: 'abc',
  updated: null,
}

function graph(overrides: Partial<CycleGraph> = {}): CycleGraph {
  return {
    name: 'Procure-to-pay',
    steps: [
      {
        name: 'Purchase order',
        documents: [{
          node: 'order', document_type: 'purchase_order', label: 'Purchase order',
          count: 14, fields: [], bound: null,
        }],
        populations: [{
          table: 'po_data', rows: 52, columns: ['PO_NUMBER'],
          borrowed: false, anchor: true,
        }],
        themes: [], stated: [],
      },
      {
        name: 'Invoice processing',
        documents: [{
          node: 'invoice', document_type: 'vendor_invoice', label: 'Vendor invoice',
          count: 15, fields: [], bound: null,
        }],
        populations: [], themes: [], stated: [],
      },
    ],
    edges: [],
    cross_cutting: { name: 'Procurement operations', themes: [] },
    ruleset: null,
    ...overrides,
  }
}

async function render(drawn: CycleGraph, shape: CycleShape | null = SHAPE) {
  responses.graph = drawn
  responses.planning = { planning: { cycle: shape } }
  patched.length = 0
  const wrapper = mount(CycleTab, {
    props: { workspace: { id: 'procurement' } as never },
    global: { stubs: { CycleRulesetReview: true, CycleStepsEditor: true } },
  })
  await flushPromises()
  return wrapper
}

describe('CycleTab', () => {
  it('says no cycle has been designed rather than drawing an empty strip', async () => {
    const wrapper = await render(
      { name: '', steps: [], edges: [], cross_cutting: null, ruleset: null },
      null,
    )

    expect(wrapper.text()).toContain('No cycle has been designed')
    expect(wrapper.find('[data-testid="cycle-strip"]').exists()).toBe(false)
  })

  it('draws the strip and counts what it holds', async () => {
    const wrapper = await render(graph())

    expect(wrapper.find('[data-testid="cycle-strip"]').exists()).toBe(true)
    const counts = wrapper.get('[data-testid="cycle-counts"]').text()
    expect(counts).toContain('2 steps')
    expect(counts).toContain('2 document types')
    expect(counts).toContain('1 population')
  })

  it('says the field half has not been written yet', async () => {
    const wrapper = await render(graph())

    expect(wrapper.get('[data-testid="cycle-counts"]').text()).toContain(
      'no cycle rules yet',
    )
  })

  it('says the rules are proposed once they are', async () => {
    const wrapper = await render(
      graph({ ruleset: { ruleset_id: 'lnk-1', status: 'proposed', cycle_label: 'P2P' } }),
    )

    expect(wrapper.get('[data-testid="cycle-counts"]').text()).toContain('rules proposed')
  })

  it('draws a node per document role and per population', async () => {
    const wrapper = await render(graph())

    expect(wrapper.find('[data-testid="cycle-node-order"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cycle-node-invoice"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="cycle-node-po_data"]').exists()).toBe(true)
  })

  it('offers Edit steps and Review rules once there is a cycle', async () => {
    const drawn = await render(graph())
    expect(drawn.text()).toContain('Edit steps')
    expect(drawn.text()).toContain('Review rules')

    const empty = await render(
      { name: '', steps: [], edges: [], cross_cutting: null, ruleset: null },
      null,
    )
    // Nothing to review before there is a shape to review it against.
    expect(empty.text()).not.toContain('Review rules')
  })

  it('saves an edited shape as one PATCH of the whole cycle', async () => {
    const wrapper = await render(graph())
    const edited: CycleShape = { ...SHAPE, name: 'Purchase to pay' }

    await wrapper.findComponent({ name: 'CycleStepsEditor' }).vm.$emit('save', edited)
    await flushPromises()

    expect(patched).toHaveLength(1)
    expect(patched[0].url).toContain('/planning')
    expect(patched[0].body).toEqual({ cycle: edited })
  })
})
