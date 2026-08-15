import { describe, expect, it } from 'vitest'

import { conclusionCounts, dataTestConclusion } from './conclusionFacet'
import type { DataTest } from '../types'

function test(overrides: Partial<DataTest>): DataTest {
  return {
    conclusion: '',
    control_conclusion: 'no_conclusion',
    conclusion_source: 'none',
    control_conclusion_source: 'none',
    control_conclusion_stale: false,
    ...overrides,
  } as DataTest
}

describe('dataTestConclusion', () => {
  it('separates nothing recorded from a run that concluded for itself', () => {
    expect(dataTestConclusion(test({}))).toBe('none')
    expect(dataTestConclusion(test({
      control_conclusion: 'effective', control_conclusion_source: 'agent',
    }))).toBe('agent')
    expect(dataTestConclusion(test({
      control_conclusion: 'effective', control_conclusion_source: 'auditor',
    }))).toBe('auditor')
  })

  it('leaves a written reason with no control conclusion outstanding', () => {
    expect(dataTestConclusion(test({
      conclusion: 'Two exceptions, both cleared with the client.',
      conclusion_source: 'auditor',
    }))).toBe('none')
  })

  it('reports a conclusion the evidence moved out from under as stale', () => {
    expect(dataTestConclusion(test({
      control_conclusion: 'effective',
      control_conclusion_source: 'auditor',
      control_conclusion_stale: true,
    }))).toBe('stale')
  })
})

describe('conclusionCounts', () => {
  it('offers every state so the active chip survives its own count falling to zero', () => {
    const counts = conclusionCounts(['none', 'none', 'auditor'])
    expect(counts.map(count => [count.key, count.value])).toEqual([
      ['all', 3], ['none', 2], ['stale', 0], ['agent', 0], ['auditor', 1],
    ])
  })
})
