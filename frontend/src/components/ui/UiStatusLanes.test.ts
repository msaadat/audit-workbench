import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { StatusDisclosure, StatusFilterGroup, StatusLane } from './statusLanes'
import { statusActions, toggleFilter } from './statusLanes'
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
    props: { lanes, filter: [], canRunAgent: true, ...props },
    global: { stubs: { Button: ButtonStub } },
  })
}

/** The chips live in the expanded card; the resting bar is one line. */
async function expand(wrapper: ReturnType<typeof mountLanes>) {
  await wrapper.find('.expander').trigger('click')
  return wrapper
}

describe('UiStatusLanes', () => {
  it('rests as one line, carrying each lane as a ratio and its meter', () => {
    const wrapper = mountLanes([lane({ total: '6' })])

    // "4 / 6", not "4 of 6 tests run" — the label names the population and the
    // sentence is what Details is for.
    expect(wrapper.find('.sum b').text().replace(/\s+/g, '')).toBe('4/6')
    expect(wrapper.find('.sum').attributes('title')).toBe('Execution — 4 of 6 tests run')
    expect(wrapper.find('.sum .meter-sm i').attributes('style')).toContain('66%')
    // The card that used to stand open is a click away, not a fifth of the page.
    expect(wrapper.find('.lanes').exists()).toBe(false)
    expect(wrapper.find('.chip').exists()).toBe(false)
  })

  it('keeps the sentence for a lane that counts no population', () => {
    // "0 exceptions to write up" is not a ratio; there is nothing to be out of.
    const wrapper = mountLanes([lane({ value: '0', caption: 'exceptions to write up', total: undefined })])

    expect(wrapper.find('.sum').text()).toContain('exceptions to write up')
  })

  it('opens the full card on demand and folds it away again', async () => {
    const wrapper = await expand(mountLanes([lane()]))

    expect(wrapper.find('.lanes').exists()).toBe(true)
    expect(wrapper.find('.lane-count').text()).toContain('4of 6 tests run')

    await wrapper.find('.expander').trigger('click')
    expect(wrapper.find('.lanes').exists()).toBe(false)
  })

  it('leaves the lane actions to the page header', async () => {
    const wrapper = await expand(mountLanes([lane()]))

    // The one urgent control belongs beside the page's other buttons, so it is
    // not the last thing read. The page collects it from the model instead.
    expect(wrapper.find('.lane-actions').exists()).toBe(false)
    expect(statusActions({ lanes: [lane()], disclosures: [] })).toEqual([
      { key: 'run', label: 'Run 2 tests', tone: 'primary', ids: ['T1', 'T2'] },
    ])
  })

  it('turns a count into a filter, and the pressed count back into no filter', async () => {
    const wrapper = await expand(mountLanes([lane()]))
    await wrapper.find('.chip').trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual([['not_run']])

    const active = await expand(
      mountLanes([lane()], { filter: ['not_run'], filterLabel: 'tests not run' }),
    )
    expect(active.find('.chip').attributes('aria-pressed')).toBe('true')
    expect(active.find('.filter-banner').text()).toContain('tests not run')
    await active.find('.chip').trigger('click')
    expect(active.emitted('filter')?.[0]).toEqual([[]])
  })

  it('shows the resting state once nothing is owed', async () => {
    const wrapper = await expand(
      mountLanes([lane({ state: 'done', actions: [], rest: 'All work has run' })]),
    )

    expect(wrapper.find('.lane-rest').text()).toContain('All work has run')
  })

  it('hands the banner to the filter menu on a page that declares one', () => {
    const groups: StatusFilterGroup[] = [{
      key: 'execution',
      label: 'Execution',
      options: [{ key: 'not_run', label: 'Not run', value: 2, tone: 'warn' }],
    }]
    const wrapper = mountLanes([lane()], {
      filters: groups, filter: ['not_run'], filterLabel: 'tests not run',
    })

    expect(wrapper.find('.filter-banner').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'UiFilterMenu' }).exists()).toBe(true)
  })

  it('drops the disclosure strip when there is nothing to disclose', () => {
    expect(mountLanes([lane()]).find('.disclosures').exists()).toBe(false)
    expect(mountLanes([lane()], { disclosures: [] }).find('.disclosures').exists()).toBe(false)
  })

  it('states a disclosure whether or not the card is open', async () => {
    const disclosure: StatusDisclosure = {
      key: 'agent', mark: 'Agent', tone: 'agent',
      message: '4 conclusions were set by the agent.', filter: 'agent_concluded',
    }

    const wrapper = mountLanes([lane()], { disclosures: [disclosure] })
    // A qualification behind a disclosure control is one the reader has to
    // already suspect to find, so it survives the collapse.
    expect(wrapper.find('.disclosure').text()).toContain('set by the agent')
    expect(wrapper.find('.disclosure .link').text()).toBe('Show rows')
    await wrapper.find('.disclosure .link').trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual([['agent_concluded']])

    const active = mountLanes([lane()], {
      disclosures: [disclosure], filter: ['agent_concluded'],
    })
    expect(active.find('.disclosure .link').text()).toBe('Clear')
  })

  it('hands back a disclosure action, and shows none where the strip offers none', async () => {
    const settled: StatusDisclosure = {
      key: 'review', mark: 'Sign-off', tone: 'muted',
      message: '1 of 3 rows reviewed.', filter: 'unreviewed_row',
      action: { key: 'mark_reviewed', label: 'Mark 2 rows reviewed', tone: 'ghost', ids: ['R2', 'R3'] },
    }

    const wrapper = mountLanes([lane()], { disclosures: [settled] })
    await wrapper.find('.disclosure .settle').trigger('click')
    expect(wrapper.emitted('action')?.[0][0]).toMatchObject({
      key: 'mark_reviewed', ids: ['R2', 'R3'],
    })

    // A disclosure with nothing to do about it keeps its filter and no button.
    const stated = mountLanes([lane()], { disclosures: [{ ...settled, action: undefined }] })
    expect(stated.find('.disclosure .settle').exists()).toBe(false)
    expect(stated.find('.disclosure .link').exists()).toBe(true)
  })

  it('stops a disclosure action while work is in flight', () => {
    const wrapper = mountLanes(
      [lane()],
      {
        busy: true,
        disclosures: [{
          key: 'review', mark: 'Sign-off', tone: 'muted',
          message: '1 of 3 rows reviewed.', filter: 'unreviewed_row',
          action: { key: 'mark_reviewed', label: 'Mark 2 rows reviewed', tone: 'ghost' },
        }],
      },
    )
    expect(wrapper.find('.disclosure .settle').attributes('disabled')).toBeDefined()
  })
})

describe('toggleFilter', () => {
  const groups: StatusFilterGroup[] = [
    {
      key: 'conclusion',
      label: 'Control conclusion',
      options: [
        { key: 'effective', label: 'Effective', value: 3, tone: 'ok' },
        { key: 'ineffective', label: 'Ineffective', value: 1, tone: 'bad' },
      ],
    },
    {
      key: 'source',
      label: 'Concluded by',
      options: [{ key: 'agent_concluded', label: 'Agent', value: 4, tone: 'warn' }],
    },
  ]

  it('narrows further across axes and replaces within one', () => {
    // "Ineffective, and set by the agent" is two questions about the same test.
    expect(toggleFilter(['ineffective'], 'agent_concluded', groups))
      .toEqual(['ineffective', 'agent_concluded'])
    // A test is not both effective and ineffective, so the second wins.
    expect(toggleFilter(['ineffective', 'agent_concluded'], 'effective', groups))
      .toEqual(['agent_concluded', 'effective'])
  })

  it('clears the narrowing it is given back', () => {
    expect(toggleFilter(['effective', 'agent_concluded'], 'effective', groups))
      .toEqual(['agent_concluded'])
  })

  it('replaces on a page that declares no axes, as those pages always did', () => {
    expect(toggleFilter(['agent_authored'], 'no_response')).toEqual(['no_response'])
  })
})
