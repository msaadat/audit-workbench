import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { DashboardPhase, DashboardSection } from '../../types'
import EngagementState from './EngagementState.vue'

vi.mock('../../composables/useWorkspaceNavigation', () => ({
  useWorkspaceNav: () => ({ target: (value: { tab: string }) => `/go/${value.tab}` }),
}))

function phase(
  id: DashboardPhase['id'],
  state: DashboardPhase['state'],
  counts: Record<string, number> = {},
  issues: string[] = [],
): DashboardPhase {
  return {
    id,
    label: id[0].toUpperCase() + id.slice(1),
    state,
    complete: state === 'complete',
    summary: '',
    counts,
    issues,
    target: { tab: id === 'fieldwork' ? 'doc-tests' : id, query: {} },
    sub: [],
  } as DashboardPhase
}

function render(
  phases: DashboardPhase[],
  sections: Record<string, DashboardSection> = {},
  busy = false,
) {
  return mount(EngagementState, {
    props: { phases, sections, busy },
    global: { stubs: { 'router-link': { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
  })
}

const partWorked = () => [
  phase('planning', 'complete', { rcm_rows: 27, tests: 60 }),
  phase('fieldwork', 'attention', { tests_linked: 60, tests_concluded: 47 }, [
    'Coverage gate: 2 issues.', '3 data tests have never run.', '5 items await evidence.',
  ]),
  phase('report', 'not_started', {}),
]

const dataSection = (overrides: Partial<DashboardSection> = {}): DashboardSection => ({
  id: 'data-tests', label: 'Data tests', state: 'attention', complete: false, issues: [],
  counts: { total: 39, concluded: 36 }, ...overrides,
})

describe('EngagementState', () => {
  it('draws the phase in flight open and the finished one collapsed', () => {
    const wrapper = render(partWorked())
    const rows = wrapper.findAll('.phase')

    expect(rows.map(row => row.attributes('data-display')))
      .toEqual(['collapsed', 'open', 'pending'])
    // A collapsed row keeps its totals and spends no lines on anything else.
    expect(rows[0].find('.tail').text()).toBe('27 rows · 60 tests')
    expect(rows[0].find('.figure').exists()).toBe(false)
    expect(rows[1].find('.figure').text()).toContain('47 / 60')
  })

  it('opens a resting phase in place, without navigating', () => {
    const wrapper = render(partWorked())
    const planning = wrapper.findAll('.phase')[0]

    planning.find('.head-row').trigger('click')
    return wrapper.vm.$nextTick().then(() => {
      expect(planning.classes()).toContain('open')
      // Still on the console: the label is the only link in the head.
      expect(planning.find('.head-row').element.tagName).toBe('BUTTON')
    })
  })

  it('states the fraction once, never as both a tail and a figure', () => {
    const wrapper = render(partWorked())
    const fieldwork = wrapper.findAll('.phase')[1]

    // The gap between the number and its caption is layout, not a text node.
    expect(fieldwork.find('.figure').text()).toBe('47 / 60tests concluded')
    expect(fieldwork.find('.tail').exists()).toBe(false)
  })

  it('shows two issues and counts the rest rather than dropping them', () => {
    const wrapper = render(partWorked())
    const fieldwork = wrapper.findAll('.phase')[1]

    expect(fieldwork.findAll('.issue').map(node => node.text())).toEqual([
      'Coverage gate: 2 issues.', '3 data tests have never run.',
    ])
    expect(fieldwork.find('.more').text()).toBe('1 more')
  })

  it('emits the action with the ids it is scoped to', async () => {
    const wrapper = render(partWorked(), {
      'data-tests': dataSection({ unrun_test_ids: ['DAT-1', 'DAT-2'] }),
    })

    const button = wrapper.findAll('.phase')[1].find('.act')
    expect(button.text()).toBe('Run 2 tests')
    await button.trigger('click')

    expect(wrapper.emitted('action')?.[0][0]).toMatchObject({
      key: 'run_data_tests', ids: ['DAT-1', 'DAT-2'],
    })
  })

  it('holds the action back while a run is in flight', () => {
    const wrapper = render(
      partWorked(),
      { 'data-tests': dataSection({ unrun_test_ids: ['DAT-1'] }) },
      true,
    )
    expect(wrapper.findAll('.phase')[1].find('.act').attributes('disabled')).toBeDefined()
  })

  it('qualifies the ticks without moving them', () => {
    const wrapper = render([
      phase('planning', 'complete', { rcm_rows: 27, tests: 60 }),
      phase('fieldwork', 'complete', {
        tests_linked: 60, tests_concluded: 60, unreviewed_agent_conclusions: 41,
      }),
      phase('report', 'complete', { findings: 35, findings_awaiting_followup: 35 }),
    ])

    expect(wrapper.find('.position').text()).toBe('Ready for review')
    expect(wrapper.findAll('.phase').map(row => row.attributes('data-state')))
      .toEqual(['complete', 'complete', 'complete'])
    expect(wrapper.findAll('.disclosure').map(node => node.text())).toEqual([
      '35 of 35 findings have no root cause or management response. Open',
      '41 of 60 conclusions were set by the assistant and never read. Open',
    ])
  })

  it('says so plainly when there is no status to draw', () => {
    const wrapper = render([])
    expect(wrapper.find('.empty').text()).toBe('Status is unavailable.')
    expect(wrapper.find('.arc').exists()).toBe(false)
  })
})
