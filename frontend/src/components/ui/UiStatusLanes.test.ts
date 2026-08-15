import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { StatusDisclosure, StatusLane } from './statusLanes'
import UiStatusLanes from './UiStatusLanes.vue'

/** A Button that keeps the props the bar sets, so a test can assert on them. */
const ButtonStub = {
  props: ['label', 'disabled', 'outlined', 'severity'],
  emits: ['click'],
  template: '<button :disabled="disabled" :data-severity="severity"'
    + ' @click="$emit(\'click\')">{{ label }}</button>',
}

function lane(overrides: Partial<StatusLane> = {}): StatusLane {
  return {
    key: 'execution', label: 'Execution', state: 'gap',
    value: '4', caption: 'of 6 tests run',
    segments: [{ tone: 'ok', portion: 66 }],
    chips: [{ key: 'not_run', label: '2 not run', tone: 'warn' }],
    actions: [{ key: 'run', label: 'Run 2 tests', tone: 'primary', ids: ['T1', 'T2'] }],
    rest: '',
    ...overrides,
  }
}

function mountLanes(lanes: StatusLane[], props: Record<string, unknown> = {}) {
  return mount(UiStatusLanes, {
    props: { lanes, filter: null, canRunAgent: true, ...props },
    global: { stubs: { Button: ButtonStub } },
  })
}

describe('UiStatusLanes', () => {
  it('hands the whole action back so the page can scope what it starts', async () => {
    const wrapper = mountLanes([lane()])

    await wrapper.find('.lane-actions button').trigger('click')
    expect(wrapper.emitted('action')?.[0][0]).toMatchObject({ key: 'run', ids: ['T1', 'T2'] })
  })

  it('turns a count into a filter, and the pressed count back into no filter', async () => {
    const wrapper = mountLanes([lane()])
    await wrapper.find('.chip').trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual(['not_run'])

    const active = mountLanes([lane()], { filter: 'not_run', filterLabel: 'tests not run' })
    expect(active.find('.chip').attributes('aria-pressed')).toBe('true')
    expect(active.find('.filter-banner').text()).toContain('tests not run')
    await active.find('.chip').trigger('click')
    expect(active.emitted('filter')?.[0]).toEqual([null])
  })

  it('shows the resting state instead of a disabled button once nothing is owed', () => {
    const wrapper = mountLanes([lane({ state: 'done', actions: [], rest: 'All work has run' })])

    expect(wrapper.find('.lane-actions').exists()).toBe(false)
    expect(wrapper.find('.lane-rest').text()).toContain('All work has run')
  })

  it('holds agent-driven actions back without blocking deterministic ones', () => {
    const wrapper = mountLanes(
      [lane({
        actions: [
          { key: 'run', label: 'Run 2 tests', tone: 'primary' },
          { key: 'draft', label: 'Draft 2 findings', tone: 'warn', needsAgent: true },
        ],
      })],
      { canRunAgent: false },
    )
    const buttons = wrapper.findAll('.lane-actions button')

    expect(buttons[0].attributes('disabled')).toBeUndefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
  })

  it('disables every action while work is already in flight', () => {
    const wrapper = mountLanes([lane()], { busy: true })
    expect(wrapper.find('.lane-actions button').attributes('disabled')).toBeDefined()
  })

  it('drops the disclosure strip when there is nothing to disclose', () => {
    expect(mountLanes([lane()]).find('.disclosures').exists()).toBe(false)
    expect(mountLanes([lane()], { disclosures: [] }).find('.disclosures').exists()).toBe(false)
  })

  it('offers a disclosure as a filter and says so once it is the active one', async () => {
    const disclosure: StatusDisclosure = {
      key: 'agent', mark: 'Agent', tone: 'agent',
      message: '4 conclusions were set by the agent.', filter: 'agent_concluded',
    }

    const wrapper = mountLanes([lane()], { disclosures: [disclosure] })
    expect(wrapper.find('.disclosure .link').text()).toBe('Show rows')
    await wrapper.find('.disclosure .link').trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual(['agent_concluded'])

    const active = mountLanes([lane()], { disclosures: [disclosure], filter: 'agent_concluded' })
    expect(active.find('.disclosure .link').text()).toBe('Clear')
  })
})
