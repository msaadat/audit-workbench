import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ReviewChip, StatusFilterGroup, StatusLane } from './statusLanes'
import UiReviewBar from './UiReviewBar.vue'

/** A Popover that renders its slot inline, so the vocabulary can be asserted. */
const PopoverStub = {
  template: '<div class="popover"><slot /></div>',
  methods: { toggle() { /* opened by the stub simply existing */ } },
}

const GROUPS: StatusFilterGroup[] = [
  {
    key: 'execution',
    label: 'Execution',
    options: [
      { key: 'with_exceptions', label: 'With exceptions', value: 10, tone: 'bad' },
      { key: 'passed', label: 'No exception', value: 20, tone: 'ok' },
      { key: 'blocked', label: 'Blocked', value: 0, tone: 'bad' },
    ],
  },
  {
    key: 'source',
    label: 'Concluded by',
    options: [{ key: 'agent_concluded', label: 'Agent', value: 29, tone: 'warn' }],
  },
  {
    key: 'findings',
    label: 'Findings',
    options: [{ key: 'missing_finding', label: 'No finding written', value: 6, tone: 'bad' }],
  },
]

const CHIPS: ReviewChip[] = [
  { filter: 'with_exceptions', tone: 'bad', label: 'Exceptions open' },
  { filter: 'missing_finding', tone: 'bad', label: 'Findings to draft' },
  { filter: 'blocked', tone: 'bad' },
  { filter: 'agent_concluded', tone: 'agent', label: 'Agent-set, unread' },
  { filter: 'passed', tone: 'ok', label: 'No exception' },
]

function lane(overrides: Partial<StatusLane> = {}): StatusLane {
  return {
    key: 'execution', label: 'Execution', state: 'done',
    value: '30', total: '30', caption: 'of 30 tests run',
    segments: [{ tone: 'ok', portion: 100 }],
    chips: [], actions: [], rest: '',
    ...overrides,
  }
}

function mountBar(props: Record<string, unknown> = {}) {
  return mount(UiReviewBar, {
    props: {
      lanes: [lane()],
      chips: CHIPS,
      filters: GROUPS,
      allLabel: 'All tests',
      total: 30,
      filter: [],
      ...props,
    },
    global: { stubs: { Popover: PopoverStub } },
  })
}

function chipText(wrapper: ReturnType<typeof mountBar>) {
  return wrapper.findAll('.chip').map(node => node.text())
}

describe('UiReviewBar chips', () => {
  it('leads with All and draws at most six named chips beside it', () => {
    const wrapper = mountBar()

    expect(chipText(wrapper)[0]).toBe('30All tests')
    // Six is the cap: past it the row wraps and stops being scannable.
    expect(wrapper.findAll('.chip').length).toBeLessThanOrEqual(7)

    const many = mountBar({
      chips: Array.from({ length: 9 }, () => ({ filter: 'agent_concluded', tone: 'agent' })),
    })
    expect(many.findAll('.chip')).toHaveLength(7)
  })

  it('does not draw a chip counting nothing', () => {
    // A zero chip is a filter that can only produce an empty list.
    expect(chipText(mountBar()).some(text => text.includes('Blocked'))).toBe(false)
  })

  it('keeps a zero chip that is the narrowing in force', () => {
    // A chip must not vanish from under the click that selected it.
    const wrapper = mountBar({ filter: ['blocked'] })

    expect(chipText(wrapper).some(text => text.includes('Blocked'))).toBe(true)
  })

  it('takes the chip’s own name over the menu’s where the view gives one', () => {
    // The menu names an axis member; the bar states a fact about the page.
    expect(chipText(mountBar())).toContain('10Exceptions open')
  })

  it('presses All while nothing is filtered, and exactly one chip once something is', () => {
    const resting = mountBar()
    expect(resting.findAll('.chip[aria-pressed="true"]').map(node => node.text()))
      .toEqual(['30All tests'])

    const filtered = mountBar({ filter: ['with_exceptions'] })
    expect(filtered.findAll('.chip[aria-pressed="true"]').map(node => node.text()))
      .toEqual(['10Exceptions open'])
  })

  it('applies a chip’s narrowing, and clears everything from All', async () => {
    const wrapper = mountBar()
    await wrapper.findAll('.chip')[1].trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual([['with_exceptions']])

    const filtered = mountBar({ filter: ['with_exceptions'] })
    await filtered.findAll('.chip')[0].trigger('click')
    expect(filtered.emitted('filter')?.[0]).toEqual([[]])
  })

  it('narrows further across axes and replaces within one, from the popover', async () => {
    const wrapper = mountBar({ filter: ['with_exceptions'] })
    const rows = wrapper.findAll('.menu .row')

    await rows.find(node => node.text().includes('Agent'))!.trigger('click')
    expect(wrapper.emitted('filter')?.[0]).toEqual([['with_exceptions', 'agent_concluded']])

    await rows.find(node => node.text().includes('No exception'))!.trigger('click')
    // A test is not both an exception and clean, so the second wins.
    expect(wrapper.emitted('filter')?.[1]).toEqual([['passed']])
  })

  it('keeps the rest of the vocabulary reachable behind the pressed chip', () => {
    const wrapper = mountBar()
    const rows = wrapper.findAll('.menu .row').map(node => node.text())

    // `blocked` earns no chip and is still selectable — once it counts anything.
    expect(rows.some(text => text.includes('Blocked'))).toBe(false)
    expect(rows.some(text => text.includes('Agent'))).toBe(true)
    expect(wrapper.find('.menu .clear').exists()).toBe(false)
    expect(mountBar({ filter: ['passed'] }).find('.menu .clear').text())
      .toContain('Show all all tests')
  })

  it('gives the page somewhere to put its settle action', () => {
    const wrapper = mount(UiReviewBar, {
      props: {
        lanes: [lane()], chips: CHIPS, filters: GROUPS,
        allLabel: 'All rows', total: 24, filter: [],
      },
      slots: { settle: '<button class="settle">Mark 24 reviewed</button>' },
      global: { stubs: { Popover: PopoverStub } },
    })

    expect(wrapper.find('.chips .settle').text()).toBe('Mark 24 reviewed')
  })
})

describe('UiReviewBar meters', () => {
  it('names each lane by what its number is out of, and paints its segments', () => {
    const wrapper = mountBar({
      lanes: [
        lane(),
        lane({
          key: 'conclusion', label: 'Control conclusion', value: '30', total: '30',
          segments: [{ tone: 'ok', portion: 67 }, { tone: 'bad', portion: 33 }],
        }),
        lane({ key: 'findings', label: 'Findings', value: '16', total: '22', segments: [] }),
      ],
    })
    const meters = wrapper.findAll('.meter')

    expect(meters.map(node => node.find('.meter-label').text()))
      .toEqual(['Run 30/30', 'Concluded 30/30', 'Findings 16/22'])
    expect(meters[1].findAll('.meter-track i').map(node => node.attributes('style')))
      .toEqual(['width: 67%;', 'width: 33%;'])
    // An empty meter is a meter of nothing, not a full one.
    expect(meters[2].findAll('.meter-track i')).toHaveLength(0)
  })

  it('falls back to the sentence’s own number where a lane counts no population', () => {
    const wrapper = mountBar({
      lanes: [lane({ key: 'findings', label: 'Findings', value: '0', total: undefined })],
    })

    expect(wrapper.find('.meter-label').text()).toBe('Findings 0')
  })
})
