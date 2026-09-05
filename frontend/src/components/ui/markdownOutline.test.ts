import { describe, expect, it } from 'vitest'

import { headingSlug, markdownOutline } from './markdownOutline'

describe('markdownOutline', () => {
  it('lists every heading in document order with its level', () => {
    const entries = markdownOutline('# Report\n\nLead.\n\n## A. Scope\n\n### 1. Period\n')

    expect(entries).toEqual([
      { id: 'report', level: 1, text: 'Report' },
      { id: 'a-scope', level: 2, text: 'A. Scope' },
      { id: '1-period', level: 3, text: '1. Period' },
    ])
  })

  it('disambiguates repeated headings, which a findings report is full of', () => {
    const entries = markdownOutline('### Condition\n\n### Condition\n\n### Condition\n')

    expect(entries.map(entry => entry.id)).toEqual(['condition', 'condition-2', 'condition-3'])
  })

  it('ignores comment lines inside fenced code', () => {
    const entries = markdownOutline('## Method\n\n```python\n# not a heading\n```\n\n## Result\n')

    expect(entries.map(entry => entry.text)).toEqual(['Method', 'Result'])
  })

  it('keeps a slug readable, and never empty', () => {
    expect(headingSlug('3. Audit Conclusion')).toBe('3-audit-conclusion')
    expect(headingSlug('**Bold** heading')).toBe('bold-heading')
    expect(headingSlug('!!!')).toBe('section')
  })
})
