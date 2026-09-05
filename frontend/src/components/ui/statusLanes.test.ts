import { describe, expect, it } from 'vitest'

import { statusActions, toggleFilter } from './statusLanes'
import type { StatusFilterGroup, StatusLane } from './statusLanes'

function lane(): StatusLane {
  return {
    key: 'execution', label: 'Execution', state: 'gap',
    value: '4', total: '6', caption: 'of 6 tests run',
    segments: [{ tone: 'ok', portion: 66 }],
    chips: [{ key: 'not_run', label: '2 not run', tone: 'warn' }],
    actions: [{ key: 'run', label: 'Run 2 tests', tone: 'primary', ids: ['T1', 'T2'] }],
    rest: '',
  }
}

describe('statusActions', () => {
  it('collects every lane action in lane order, for the page header to render', () => {
    expect(statusActions({ lanes: [lane()], disclosures: [] })).toEqual([
      { key: 'run', label: 'Run 2 tests', tone: 'primary', ids: ['T1', 'T2'] },
    ])
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
