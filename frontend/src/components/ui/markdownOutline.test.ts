import { describe, expect, it } from 'vitest'

import { headingSlug, markdownOutline } from './markdownOutline'

describe('markdownOutline', () => {
  it('lists every heading in document order with its level', () => {
    const entries = markdownOutline('# Report\n\nLead.\n\n## A. Scope\n\n### 1. Period\n')

    expect(entries).toEqual([
      { id: 'report', level: 1, text: 'Report', label: 'Report' },
      { id: 'a-scope', level: 2, text: 'A. Scope', label: 'Scope', number: 'A', part: true },
      { id: '1-period', level: 3, text: '1. Period', label: 'Period', number: '1' },
    ])
  })

  it('numbers a memorandum that numbers nothing, subsections under their section', () => {
    const entries = markdownOutline([
      '# Audit Planning Memorandum',
      '## Engagement',
      '## Key risks and planned response',
      '### Authorisation against stated limits',
      '### Completeness of what was recorded',
      '## Planning assumptions',
    ].join('\n\n'))

    expect(entries.map(entry => entry.number)).toEqual([undefined, '1', '2', '2.1', '2.2', '3'])
    expect(entries[3].label).toBe('Authorisation against stated limits')
  })

  it('keeps the numbers a document wrote, and invents none beside them', () => {
    const entries = markdownOutline([
      '## A. Executive Summary', '### 1. Introduction', '### 3. Audit Conclusion',
      '## B. Detailed Findings', '### 1. Dealers confirmed trades', '#### Condition',
    ].join('\n\n'))

    expect(entries.map(entry => [entry.number, entry.label])).toEqual([
      ['A', 'Executive Summary'], ['1', 'Introduction'], ['3', 'Audit Conclusion'],
      ['B', 'Detailed Findings'], ['1', 'Dealers confirmed trades'], [undefined, 'Condition'],
    ])
    expect(entries.filter(entry => entry.part).map(entry => entry.number)).toEqual(['A', 'B'])
  })

  it('numbers the memo\'s sections and keeps its numbered findings', () => {
    const entries = markdownOutline([
      '## Data received and its limitations', '## What the analysis found',
      '### 1. Invoices billed above approved amounts', '### 2. Payments to vendors not eligible',
      '## Further work required',
    ].join('\n\n'))

    expect(entries.map(entry => entry.number)).toEqual(['1', '2', '1', '2', '3'])
  })

  it('does not read a heading\'s first word as its number', () => {
    const entries = markdownOutline('## A review of approvals\n\n## 2024 results\n')

    expect(entries.map(entry => [entry.number, entry.label])).toEqual([
      ['1', 'A review of approvals'], ['2', '2024 results'],
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
