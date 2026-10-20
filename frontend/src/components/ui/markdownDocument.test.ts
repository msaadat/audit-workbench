import { describe, expect, it } from 'vitest'

import type { DocumentOptions } from './markdownHtml'
import { markdownToHtml } from './markdownHtml'

/**
 * Document mode: the forms a written work product draws, each decided from the
 * shape the Markdown already has. The inputs are the shapes the three real
 * documents carry — the procurement memorandum and memo, the treasury report —
 * including the ones the memorandum's model writes differently run to run.
 */
const doc = (markdown: string, document: DocumentOptions = {}) => markdownToHtml(markdown, { document })

describe('document mode', () => {
  it('leaves every other caller exactly as it was', () => {
    const markdown = '- **Lead** — text\n- **Other** — more'

    expect(markdownToHtml(markdown)).toBe('<ul><li><strong>Lead</strong> — text</li><li><strong>Other</strong> — more</li></ul>')
  })

  it('sets the engagement list as labels beside their values', () => {
    const html = doc('- Entity: Global Bank\n- Period: January 2024 to January 2025\n- Objective & Scope: A risk-based review.')

    expect(html).toBe(
      '<dl class="doc-kv"><dt>Entity</dt><dd>Global Bank</dd>'
      + '<dt>Period</dt><dd>January 2024 to January 2025</dd>'
      + '<dt>Objective &amp; Scope</dt><dd>A risk-based review.</dd></dl>',
    )
  })

  it('reads a run of bold labels as one definition list, and one alone as a run-in', () => {
    expect(doc('**Objective:** To assess the controls.\n\n**Scope:** Sections 4, 5 and 7.')).toBe(
      '<dl class="doc-kv"><dt>Objective</dt><dd>To assess the controls.</dd><dt>Scope</dt><dd>Sections 4, 5 and 7.</dd></dl>',
    )
    expect(doc('**Data quality limits** — Results rest on small populations.')).toBe(
      '<p class="doc-runin"><strong>Data quality limits</strong> — Results rest on small populations.</p>',
    )
  })

  it('numbers a bold-led ordered list as a register', () => {
    const html = doc([
      '1. **Invoices billed above approved amounts** — Three paid invoices.',
      '2. **Payments to ineligible vendors** — Two paid invoices.',
    ].join('\n'))

    expect(html).toContain('<ol class="doc-register">')
    expect(html).toContain(
      '<span class="doc-k" aria-hidden="true">1</span><div class="doc-item">'
      + '<p class="doc-lead">Invoices billed above approved amounts</p><p>Three paid invoices.</p>',
    )
  })

  it("draws the process as steps when the list names the cycle's steps", () => {
    const markdown = [
      '1. **Requisition initiation and approval** — Raised in the ERP.',
      '2. **Purchase Order** — Raised on approval.',
    ].join('\n')
    const stepNames = ['Requisition initiation and approval', 'Purchase Order', 'Goods receipt and inspection']

    expect(doc(markdown, { stepNames })).toContain('<ol class="doc-steps">')
    // A list naming a step the cycle does not have is a register, not the process.
    expect(doc(markdown, { stepNames: ['Purchase Order'] })).toContain('<ol class="doc-register">')
  })

  it("hangs a bold-led bullet's name beside what it says", () => {
    const html = doc([
      "- **Incentive and pressure** — The Board's growth strategy.",
      '- **Management override of controls** — Presumed present.',
    ].join('\n'))

    expect(html).toBe(
      '<ul class="doc-hang"><li><div class="doc-lab">Incentive and pressure</div>'
      + "<div class=\"doc-item\"><p>The Board's growth strategy.</p></div></li>"
      + '<li><div class="doc-lab">Management override of controls</div>'
      + '<div class="doc-item"><p>Presumed present.</p></div></li></ul>',
    )
  })

  it('falls back to a plain list when only some items lead in bold', () => {
    expect(doc('- **Lead** — text\n- no lead here')).toBe('<ul><li><strong>Lead</strong> — text</li><li>no lead here</li></ul>')
  })

  it('rules a table, right-aligns its figures and mutes its dashes', () => {
    const html = doc([
      '| Source table | Records | Period covered |',
      '| --- | --- | --- |',
      '| po_data | 52 | 2024-01-18 to 2025-01-18 |',
      '| staff_details | 20 | — |',
    ].join('\n'))

    expect(html).toContain('<table class="doc-table">')
    expect(html).toContain('<th class="num">Records</th>')
    expect(html).toContain('<td class="num">52</td>')
    expect(html).toContain('<td class="nil">—</td>')
    expect(html).toContain('<td>2024-01-18 to 2025-01-18</td>')
  })

  it('draws a risk level as its severity, and tints a severity count', () => {
    const keyFindings = doc('| # | Key Finding | Risk Level |\n| - | --- | --- |\n| 2 | No confirmation. | High |')
    expect(keyFindings).toContain('<span class="aw-severity" data-sev="high">High</span>')

    const summary = doc('| Unit | Critical | High | Medium | Low |\n| --- | --- | --- | --- | --- |\n| Treasury | 0 | 2 | 2 | 0 |')
    expect(summary).toContain('<td class="count zero">0</td>')
    expect(summary).toContain('<td class="count sev-count" data-sev="high">2</td>')
    expect(summary).toContain('<td class="count sev-count" data-sev="medium">2</td>')
  })

  it('sets the preliminary notice as a band, and leaves other quotes alone', () => {
    expect(doc('> **Preliminary working draft:** fieldwork remains open.')).toBe(
      '<div class="doc-band" role="note"><p class="doc-runin"><strong>Preliminary working draft:</strong> fieldwork remains open.</p></div>',
    )
    expect(doc('> A quoted policy clause.')).toBe('<blockquote><p>A quoted policy clause.</p></blockquote>')
  })

  it('draws the sentences the report writes into an empty section as gaps', () => {
    expect(doc('Not established by the evidence obtained; pending auditor follow-up.'))
      .toBe('<p class="doc-gap">Not established by the evidence obtained; pending auditor follow-up.</p>')
    expect(doc('_Root cause pending auditor follow-up._'))
      .toBe('<p class="doc-gap"><em>Root cause pending auditor follow-up.</em></p>')
    expect(doc('Not stated.')).toBe('<p class="doc-gap">Not stated.</p>')
    expect(doc('Not stated by the policy.')).toBe('<p>Not stated by the policy.</p>')
  })

  it('turns a citation of exhibited procedures into a superscript to the exhibit', () => {
    const citations = { 'A-8BFCE4A3': { exhibit: 1 }, 'A-A69AB394': { exhibit: 2 } }

    expect(doc('The same rows breach both tests (A-8BFCE4A3, A-A69AB394).', { citations })).toBe(
      '<p>The same rows breach both tests<sup class="doc-xref"><a href="#exhibit-1" data-jump="exhibit-1">Ex. 1–2</a></sup>.</p>',
    )
  })

  it('turns a procedure with no exhibit into a chip with its result', () => {
    const citations = { 'A-B8A820A7': { tone: 'ok' as const, title: 'Vendor invoice number reused' } }

    expect(doc('It found no reuse (A-B8A820A7). A-UNKNOWN stays text.', { citations })).toBe(
      '<p>It found no reuse <a class="doc-cite" href="#" data-procedure="A-B8A820A7" data-tone="ok" '
      + 'title="Vendor invoice number reused"><i aria-hidden="true"></i>A-B8A820A7</a>. A-UNKNOWN stays text.</p>',
    )
  })

  it('writes exhibit ranges the way a reader expects them', () => {
    const citations = Object.fromEntries([3, 4, 5, 6, 8].map((n, i) => [`A-${i}`, { exhibit: n }]))

    expect(doc('Seen across frames (A-0, A-1, A-2, A-3, A-4).', { citations })).toContain('Ex. 3–6, 8')
  })

  it('still escapes everything the document wrote', () => {
    const html = doc('1. **<script>x</script>** — <b>y</b>\n2. **Two** — z')

    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;x&lt;/script&gt;')
  })
})
