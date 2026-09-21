import { describe, expect, it } from 'vitest'

import { markdownToHtml } from './markdownHtml'

/**
 * View mode and edit mode have to agree about what a document says. They did
 * not, because view mode ran its own line scanner over Markdown that Milkdown
 * had re-serialised, and the two disagreed about perfectly ordinary GFM. The
 * cases below are the ones that were actually wrong on the treasury report,
 * plus the constructs the scanner never supported at all.
 */

describe('Markdown the editor writes back', () => {
  /**
   * The defect that started this. Milkdown pads a table's delimiter row to the
   * column width, so a `#` column one character wide serialises as `| - |`.
   * That is legal GFM; the old scanner demanded three hyphens, failed to see a
   * table, and folded every row into one paragraph.
   */
  it('reads a delimiter row whose cell is a single hyphen', () => {
    const html = markdownToHtml([
      '| # | Process               | Risk |',
      '| - | --------------------- | ---- |',
      '| 2 | Confirmation issuance | High |',
    ].join('\n'))

    expect(html).toContain('<div class="table-scroll"><table>')
    expect(html).toContain('<th>#</th>')
    expect(html).toContain('<td>Confirmation issuance</td>')
    expect(html).not.toContain('---')
  })

  it('keeps the alignment the delimiter row asked for', () => {
    const html = markdownToHtml('| A | B |\n| :- | --: |\n| 1 | 2 |')

    expect(html).toContain('<th style="text-align:left">A</th>')
    expect(html).toContain('<td style="text-align:right">2</td>')
  })

  /**
   * Milkdown writes an empty paragraph out as a literal `<br />` and strips it
   * back on parse, so the marker was invisible in the editor and printed as
   * text everywhere else.
   */
  it('drops the empty-paragraph marker the editor leaves behind', () => {
    for (const marker of ['<br />', '<br>', '<br/>', '<br >']) {
      expect(markdownToHtml(`Before.\n\n${marker}\n\nAfter.`)).not.toContain('br')
    }
  })

  it('renders a blockquote as a quote rather than a literal angle bracket', () => {
    const html = markdownToHtml('> **Preliminary working draft:** fieldwork remains open.')

    expect(html).toContain('<blockquote>')
    expect(html).toContain('<strong>Preliminary working draft:</strong>')
    expect(html).not.toContain('&gt;')
  })

  it('supports the blocks the old scanner had no rule for', () => {
    expect(markdownToHtml('---')).toContain('<hr />')
    expect(markdownToHtml('~~withdrawn~~')).toContain('<del>withdrawn</del>')
    expect(markdownToHtml('```python\nx = 1\n```')).toContain('<pre><code>x = 1</code></pre>')
    expect(markdownToHtml('- a\n  - nested')).toContain('<ul><li>a<ul><li>nested</li></ul></li></ul>')
  })
})

describe('what the renderer refuses to emit', () => {
  /**
   * The output goes through `v-html` over text an auditor or a model wrote, so
   * no tag may ever originate in the document. Raw HTML renders as its own
   * characters — which is also what Milkdown does with it in edit mode, so the
   * two modes still agree, and nothing is silently deleted from a report.
   */
  it('shows raw HTML as text instead of parsing it', () => {
    expect(markdownToHtml('Text with <b>raw</b> markup.'))
      .toBe('<p>Text with &lt;b&gt;raw&lt;/b&gt; markup.</p>')
  })

  it('neutralises a script tag', () => {
    const html = markdownToHtml('<script>alert(1)</script>')

    expect(html).not.toContain('<script')
    expect(html).toContain('&lt;script&gt;')
  })

  it('keeps the words of a link it will not follow', () => {
    // eslint-disable-next-line no-script-url
    const html = markdownToHtml('[click me](javascript:alert(1))')

    expect(html).toBe('<p>click me</p>')
  })

  it('follows a workspace route, a tab switch, and an explicit web address', () => {
    expect(markdownToHtml('[F-1](/workspace/ws_1/findings?finding=F-1)'))
      .toContain('href="/workspace/ws_1/findings?finding=F-1"')
    expect(markdownToHtml('[Data](?tab=data)')).toContain('href="?tab=data"')
    expect(markdownToHtml('[Policy](https://example.org/p)')).toContain('href="https://example.org/p"')
  })

  it('escapes a quote inside link text so it cannot close an attribute', () => {
    expect(markdownToHtml('[a "b" c](/x)')).toBe('<p><a href="/x">a &quot;b&quot; c</a></p>')
  })
})

describe('prose the assistant hard-wraps', () => {
  it('joins wrapped lines into one paragraph', () => {
    expect(markdownToHtml('One sentence that was\nwrapped across two lines.'))
      .toBe('<p>One sentence that was\nwrapped across two lines.</p>')
  })

  it('keeps a wrapped bullet in its own item', () => {
    const html = markdownToHtml('- A bullet that runs\n  onto a second line.\n- A second bullet.')

    expect(html).toBe('<ul><li>A bullet that runs\nonto a second line.</li><li>A second bullet.</li></ul>')
  })

  it('gives a loose list paragraphs and a tight one none', () => {
    expect(markdownToHtml('- a\n- b')).toBe('<ul><li>a</li><li>b</li></ul>')
    expect(markdownToHtml('- a\n\n- b')).toBe('<ul><li><p>a</p></li><li><p>b</p></li></ul>')
  })

  it('renders an empty document as nothing', () => {
    expect(markdownToHtml('')).toBe('')
  })
})
