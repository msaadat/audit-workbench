import { describe, expect, it } from 'vitest'

import { ratingClaim, severityCounts } from './reportStatus'

describe('the rating a report asserts', () => {
  // The shapes `_RATING_CLAIM` in report.py accepts, which this must mirror.
  it('reads a rating in bold, or after the word rating', () => {
    expect(ratingClaim('**Rating: Fair**\n\nThis is the most favourable rating.')).toBe('fair')
    expect(ratingClaim('**Marginal**. The control environment is weak.')).toBe('marginal')
    expect(ratingClaim('Overall rating: Unsatisfactory.')).toBe('unsatisfactory')
    // Words between are not a rating claim — to report.py either.
    expect(ratingClaim('The overall rating is Fair.')).toBeNull()
  })

  it('reads no rating where none is assigned', () => {
    expect(ratingClaim('No overall rating is assigned because fieldwork remains open.')).toBeNull()
    expect(ratingClaim('')).toBeNull()
  })
})

describe('the summary of findings', () => {
  it('reads the counts off the table the report assembles', () => {
    const body = '| Unit | Critical | High | Medium | Low |\n| --- | --- | --- | --- | --- |\n| Treasury | 0 | 2 | 2 | 0 |'

    expect(severityCounts(body)).toEqual([
      { severity: 'critical', count: 0 },
      { severity: 'high', count: 2 },
      { severity: 'medium', count: 2 },
      { severity: 'low', count: 0 },
    ])
  })

  it('reads nothing from a section with no table', () => {
    expect(severityCounts('No auditor-confirmed findings have been recorded.')).toEqual([])
  })
})
