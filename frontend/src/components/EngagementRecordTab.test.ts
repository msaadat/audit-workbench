import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

import EngagementRecordTab from './EngagementRecordTab.vue'
import type { EngagementRecordEntry, EngagementRecordPayload } from '../types'

/**
 * The record's job is to state what was filed and what it cost without
 * overstating either. These cover the readings the demo engagement actually
 * produces — a sub-second stage, a stage nothing timed, six attempts collapsed
 * into one row — plus the two cases its own data never reaches.
 */

const get = vi.fn()
vi.mock('../api', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
  ApiError: class extends Error {},
}))
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }))
vi.mock('../composables/useAgentRun', () => ({
  useAgentRun: () => ({ onWorkspaceInvalidated: () => () => undefined }),
}))
vi.mock('../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ to: (destination: string) => ({ path: `/${destination}` }) }),
}))

function entry(overrides: Partial<EngagementRecordEntry> = {}): EngagementRecordEntry {
  return {
    id: 'findings.drafted:m1',
    capability: 'findings.drafted',
    at: '2026-08-15T12:10:00+00:00',
    first_at: '2026-08-14T11:05:00+00:00',
    status: 'completed',
    headline: 'Finding drafts prepared',
    summary: 'Prepared 1 evidence-linked finding draft (1 critical).',
    metrics: [],
    highlights: [],
    objective: 'Draft findings.',
    run_id: 'r3',
    chat_id: 'chat_r3',
    attempts: [
      { run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 },
      { run_id: 'r3', run_status: 'completed', at: '2026-08-15T12:10:00+00:00', elapsed_ms: 60_000 },
    ],
    elapsed_ms: 120_000,
    measured_attempts: 2,
    filed: {
      label: 'Findings register',
      destination: 'findings',
      unit: 'finding',
      unit_plural: 'findings',
      count: 35,
    },
    ...overrides,
  }
}

function payload(entries: EngagementRecordEntry[]): EngagementRecordPayload {
  return {
    entries,
    counts: {},
    totals: {
      work_products: entries.length,
      runs: 32,
      runs_that_filed: 19,
      attempts: entries.reduce((sum, item) => sum + item.attempts.length, 0),
      elapsed_ms: 3_345_210,
      first_at: '2026-08-13T19:22:24+00:00',
      last_at: '2026-08-15T12:10:00+00:00',
    },
  }
}

async function render(entries: EngagementRecordEntry[]) {
  get.mockResolvedValue(payload(entries))
  const wrapper = mount(EngagementRecordTab, {
    props: { workspace: { id: 'procurement' } as never },
    global: {
      stubs: {
        UiPageHeader: { template: '<div><slot /></div>' },
        UiEmptyState: { template: '<div class="empty" />' },
        Button: { template: '<button />' },
        RouterLink: { props: ['to'], template: '<a :href="String(to?.path)"><slot /></a>' },
      },
    },
  })
  await new Promise(resolve => setTimeout(resolve, 0))
  await wrapper.vm.$nextTick()
  return wrapper
}

describe('EngagementRecordTab', () => {
  beforeEach(() => get.mockReset())

  it('states an irregular unit from the declared plural, not by appending s', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Analysis library', destination: 'analysis', unit: 'analysis', unit_plural: 'analyses', count: 28 },
    })])

    expect(wrapper.text()).toContain('28 analyses')
    expect(wrapper.text()).not.toContain('analysiss')
  })

  it('renders sub-minute work in seconds rather than rounding it to 0m', async () => {
    // Fieldwork and the roll-up settle the instant their run starts, because
    // the tests they gather already ran. "0m" reads as a broken clock.
    const wrapper = await render([entry({ elapsed_ms: 382, measured_attempts: 1 })])

    expect(wrapper.text()).toContain('<1s')
    expect(wrapper.text()).not.toContain('0m')
  })

  it('leaves the duration unstated when nothing about the work was timed', async () => {
    const wrapper = await render([entry({ elapsed_ms: null, measured_attempts: 0 })])

    expect(wrapper.find('.took').text()).toBe('—')
  })

  it('says how many attempts a collapsed row stands for, and how many were timed', async () => {
    const wrapper = await render([entry({
      attempts: [
        { run_id: 'r1', run_status: 'cancelled', at: '2026-08-14T19:36:20+00:00', elapsed_ms: null },
        { run_id: 'r2', run_status: 'failed', at: '2026-08-14T20:02:09+00:00', elapsed_ms: null },
        { run_id: 'r3', run_status: 'completed', at: '2026-08-14T20:05:00+00:00', elapsed_ms: 382 },
      ],
      measured_attempts: 1,
    })])

    expect(wrapper.text()).toContain('3 attempts · 2 not timed')
  })

  it('stays silent about attempts when a work product took only one', async () => {
    const wrapper = await render([entry({
      attempts: [{ run_id: 'r1', run_status: 'completed', at: '2026-08-14T11:05:00+00:00', elapsed_ms: 60_000 }],
      measured_attempts: 1,
    })])

    expect(wrapper.text()).not.toContain('attempt')
  })

  it('reveals the individual runs behind a collapsed row on request', async () => {
    const wrapper = await render([entry()])

    expect(wrapper.find('.attempts').exists()).toBe(false)
    await wrapper.find('.tries').trigger('click')
    expect(wrapper.findAll('.attempts li')).toHaveLength(2)
  })

  it('links the filed artifact to the surface that opens it', async () => {
    const wrapper = await render([entry()])

    expect(wrapper.find('.card').attributes('href')).toBe('/findings')
  })

  it('does not link a destination this build does not know', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Something new', destination: 'not-a-surface', unit: '', unit_plural: '', count: null },
    })])

    const card = wrapper.find('.card')
    expect(card.exists()).toBe(true)
    expect(card.attributes('href')).toBeUndefined()
  })

  it('shows a capability with no artifact mapping without inventing one', async () => {
    const wrapper = await render([entry({ filed: null, headline: 'A stage the record has never seen' })])

    expect(wrapper.find('.none').exists()).toBe(true)
    expect(wrapper.text()).toContain('A stage the record has never seen')
  })

  it('omits the size of a work product that has no meaningful count', async () => {
    const wrapper = await render([entry({
      filed: { label: 'Audit planning memorandum', destination: 'apm', unit: '', unit_plural: '', count: null },
    })])

    expect(wrapper.text()).toContain('Audit planning memorandum')
    expect(wrapper.find('.mt em').exists()).toBe(false)
  })

  it('reports the runs that filed nothing rather than dropping them silently', async () => {
    const wrapper = await render([entry()])

    // 32 runs, 19 of which filed something.
    expect(wrapper.text()).toContain('13 runs filed nothing')
  })

  it('breaks the ledger by day only where the day changes', async () => {
    const wrapper = await render([
      entry({ id: 'a', at: '2026-08-14T09:00:00+00:00' }),
      entry({ id: 'b', at: '2026-08-14T15:00:00+00:00' }),
      entry({ id: 'c', at: '2026-08-15T09:00:00+00:00' }),
    ])

    expect(wrapper.findAll('.daybreak')).toHaveLength(2)
  })
})
