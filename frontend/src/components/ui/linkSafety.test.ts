import { describe, expect, it } from 'vitest'

import { documentLayout } from './documentUnits'
import { safeDocxHref, sanitizeRenderedDocx } from './docxSanitize'
import { markdownOutline } from './markdownOutline'
import { safeHref } from './safeHref'

/**
 * A rendered client `.docx`, a report and a finding narrative all reach the
 * page as anchors in the application's own origin. What they may link to is
 * held to the same rule: the web, this site's own paths, and nothing that runs.
 */

describe('safeHref', () => {
  it('allows workspace paths, tab switches and web addresses', () => {
    expect(safeHref('/workspace/ws_1/findings?finding=F-1')).toBe('/workspace/ws_1/findings?finding=F-1')
    expect(safeHref('?tab=rcm')).toBe('?tab=rcm')
    expect(safeHref('https://example.com')).toBe('https://example.com')
  })

  it('refuses a protocol-relative address, which starts with a slash but leaves the site', () => {
    expect(safeHref('//attacker.example/pixel')).toBeNull()
    expect(safeHref('/\\attacker.example')).toBeNull()
  })

  it('refuses script and data URLs', () => {
    expect(safeHref('javascript:alert(1)')).toBeNull()
    expect(safeHref('data:text/html,<script>')).toBeNull()
  })
})

function findingReference(link: string) {
  const markdown = [
    '# Report',
    '## B. Detailed Findings',
    '### 1. A finding',
    `**Severity:** High · **Reference:** ${link}`,
    'Body.',
  ].join('\n\n')
  const layout = documentLayout(markdown, markdownOutline(markdown))
  const unit = layout.units.find(item => item.kind === 'finding')
  return unit?.kind === 'finding' ? unit.finding.reference : null
}

describe('a finding reference', () => {
  it('keeps the link the report writes', () => {
    expect(findingReference('[Finding F-1](/workspace/ws_1/findings?finding=F-1)'))
      .toEqual({ id: 'F-1', href: '/workspace/ws_1/findings?finding=F-1' })
  })

  it('does not bind a script URL to the anchor a middle-click follows', () => {
    expect(findingReference('[Finding F-2](javascript:alert(document.cookie))')?.href).toBe('#')
  })

  it('survives malformed percent-encoding instead of throwing out of the layout', () => {
    expect(() => findingReference('[Finding F-3](/workspace/ws_1/findings?finding=%ZZ)')).not.toThrow()
    expect(findingReference('[Finding F-3](/workspace/ws_1/findings?finding=%ZZ)')?.id).toBe('F-3')
  })
})

describe('a rendered client .docx', () => {
  it('follows only web, mail and in-document links', () => {
    expect(safeDocxHref('https://example.com/a')).toBe('https://example.com/a')
    expect(safeDocxHref('mailto:a@example.com')).toBe('mailto:a@example.com')
    expect(safeDocxHref('#bookmark')).toBe('#bookmark')
    expect(safeDocxHref('javascript:alert(1)')).toBeNull()
    expect(safeDocxHref(' JaVaScRiPt:alert(1)')).toBeNull()
    expect(safeDocxHref('data:text/html,x')).toBeNull()
    expect(safeDocxHref('/api/workspaces')).toBeNull()
  })

  it('removes frames, scripts and handlers, and neutralises unsafe links', () => {
    const container = document.createElement('div')
    container.innerHTML = [
      '<iframe srcdoc="<script>parent.x=1</script>"></iframe>',
      '<script>window.y = 1</script>',
      '<object data="x"></object>',
      '<p onclick="alert(1)">text</p>',
      '<a id="bad" href="javascript:alert(1)">bad</a>',
      '<a id="good" href="https://example.com">good</a>',
      '<a id="anchor" href="#section">anchor</a>',
    ].join('')

    sanitizeRenderedDocx(container)

    expect(container.querySelector('iframe, script, object')).toBeNull()
    expect(container.querySelector('p')?.hasAttribute('onclick')).toBe(false)
    expect(container.querySelector('#bad')?.hasAttribute('href')).toBe(false)
    const good = container.querySelector('#good')
    expect(good?.getAttribute('href')).toBe('https://example.com/')
    expect(good?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(good?.getAttribute('target')).toBe('_blank')
    expect(container.querySelector('#anchor')?.getAttribute('href')).toBe('#section')
    expect(container.querySelector('#anchor')?.hasAttribute('target')).toBe(false)
  })
})
