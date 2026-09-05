import { describe, expect, it } from 'vitest'

import { headingCount, markdownBlocks } from './markdownBlocks'
import { markdownOutline } from './markdownOutline'

/**
 * The memorandum hands the whole document to one component; the analysis memo
 * renders in pieces, because its prose is interrupted by embedded results. Both
 * split against the same entries, and the ids have to match the outline beside
 * them or every link in it points at nothing.
 */

describe('splitting a document at its headings', () => {
  const markdown = [
    'A lead paragraph before any heading.',
    '',
    '## Data received',
    '',
    'Six tables.',
    '',
    '### 1. Invoices',
    '',
    'Three of them.',
  ].join('\n')

  it('pairs each heading with its entry and keeps the text before the first', () => {
    const blocks = markdownBlocks(markdown, markdownOutline(markdown))

    expect(blocks.map(block => block.entry?.id ?? null))
      .toEqual([null, 'data-received', '1-invoices'])
    expect(blocks[0].body).toBe('A lead paragraph before any heading.')
    expect(blocks[2].entry!.level).toBe(3)
  })

  it('does not treat a comment in a code fence as a section', () => {
    const fenced = '## Real\n\n```python\n# not a heading\n```\n\n## Also real'
    expect(headingCount(fenced)).toBe(2)
    expect(markdownBlocks(fenced, markdownOutline(fenced)).map(block => block.entry?.text))
      .toEqual(['Real', 'Also real'])
  })

  /**
   * The reason the entries are passed in rather than derived. A document
   * rendered in pieces has to number its duplicate headings across the whole of
   * itself: the second "Exceptions" is `exceptions-2` even when it is the first
   * heading in the piece it lands in.
   */
  it('numbers duplicate headings across pieces, not within them', () => {
    const whole = '## Exceptions\n\nOne.\n\n## Exceptions\n\nTwo.'
    const entries = markdownOutline(whole)
    const [first, second] = ['## Exceptions\n\nOne.', '## Exceptions\n\nTwo.']

    const firstBlocks = markdownBlocks(first, entries)
    const offset = headingCount(first)
    const secondBlocks = markdownBlocks(second, entries.slice(offset))

    expect(firstBlocks[0].entry!.id).toBe('exceptions')
    expect(secondBlocks[0].entry!.id).toBe('exceptions-2')
  })
})
