import JSZip from 'jszip'
import { describe, expect, it } from 'vitest'

import { memoWord } from '../analysis/memoDocx'
import type { ExhibitWord } from '../analysis/memoDocx'
import { markdownOutline } from './markdownOutline'
import { markdownWord } from './documentDocx'

/**
 * The Word export, opened and read back. Each case builds a real .docx from
 * the shapes the three real documents carry and checks what the file says —
 * the parts, the numbers, the forms — rather than how `docx` built it.
 */
async function open(blob: Blob): Promise<{ body: string; header: string; footer: string }> {
  const zip = await JSZip.loadAsync(blob)
  const read = async (prefix: string) => {
    const names = Object.keys(zip.files).filter(name => name.startsWith(prefix))
    return (await Promise.all(names.map(name => zip.file(name)!.async('string')))).join('\n')
  }
  return { body: await read('word/document.xml'), header: await read('word/header'), footer: await read('word/footer') }
}

/** The words of a Word body, in order, without the markup between them. */
function words(xml: string): string {
  return [...xml.matchAll(/<w:t(?: [^>]*)?>([^<]*)<\/w:t>/g)].map(match => match[1]).join(' ')
}

const REPORT = [
  '# Preliminary Internal Audit Working Draft',
  '> **Preliminary working draft:** fieldwork, evidence, review, or auditor judgment remains open.',
  '## A. Executive Summary',
  '### 2. Objective and Scope',
  '**Objective:** To assess the controls.\n\n**Scope:** Sections 4, 5 and 7.',
  '### 3. Audit Conclusion',
  'No overall rating is assigned because fieldwork remains open.',
  '### 5. Summary of Findings',
  '| Unit | Critical | High | Medium | Low |\n| --- | --- | --- | --- | --- |\n| Treasury | 0 | 2 | 2 | 0 |',
  '## B. Detailed Findings',
  '### 1. Dealers confirmed trades they executed',
  '**Severity:** High · **Reference:** [Finding F-3814A0](/workspace/ws_1/findings?finding=F-3814A0)',
  '#### Condition',
  '| Deal | Dealer |\n| --- | --- |\n| TD-2025-0167 | TS-006 |',
  '#### Root Cause',
  'Not established by the evidence obtained; pending auditor follow-up.',
  '#### Recommendation',
  'Configure the confirmation workflow.',
].join('\n\n')

describe('the Word export', () => {
  it('sets the report as the sheet sets it: cover, parts, numbers, finding', async () => {
    const blob = await markdownWord({
      markdown: REPORT,
      sheet: {
        cover: { eyebrow: 'Internal audit report', tag: { label: 'Preliminary', tone: 'warn' }, fields: [{ label: 'Entity', value: 'Meridian Bank Limited' }] },
        runningHead: ['Meridian Bank Limited · Treasury', 'Internal audit report · preliminary draft'],
        footer: ['Treasury', 'Audit Workbench'],
        endMark: 'End of report',
      },
      inserts: {
        afterHeading: entry => /conclusion/i.test(entry.text)
          ? [{ kind: 'rating', value: 'Not assigned', scale: [{ label: 'Satisfactory', on: false, tone: 'ok' }], figures: [{ label: 'tests run', value: '38' }] }]
          : [],
        afterBody: entry => /summary of findings/i.test(entry.text)
          ? [{ kind: 'severity-bar', counts: [{ severity: 'high', count: 2 }, { severity: 'medium', count: 2 }] }]
          : [],
      },
    })
    const { body, header, footer } = await open(blob)
    const text = words(body)

    expect(text).toContain('Internal audit report')
    expect(text).toContain('Preliminary Internal Audit Working Draft')
    expect(text).toContain('Meridian Bank Limited')
    expect(text).toContain('Part A')
    expect(text).toContain('Executive Summary')
    // The report's own numbers, hung before the title.
    expect(text).toContain('3 Audit Conclusion')
    expect(text).toContain('Overall rating')
    expect(text).toContain('Finding 1')
    expect(text).toContain('F-3814A0')
    expect(text).toContain('End of report')
    // The page's own remarks are not part of the document.
    expect(text).not.toContain('Supporting procedures')
    // Word's own heading styles, so the navigation pane and a contents field work.
    expect(body).toContain('w:val="Title"')
    expect(body).toContain('w:val="Heading1"')
    expect(body).toContain('w:val="Heading2"')
    // A high severity is the shaded cell it is on screen.
    expect(body).toContain('w:fill="FDECEA"')
    // The gap the report left, drawn as one.
    expect(body).toContain('w:val="dashed"')
    expect(words(header)).toContain('Internal audit report · preliminary draft')
    expect(footer).toContain('PAGE')
    expect(footer).toContain('NUMPAGES')
  })

  it('sets the memorandum\'s forms: labels, steps, register', async () => {
    const markdown = [
      '# Audit Planning Memorandum',
      '## Engagement',
      '- Entity: Global Bank\n- Period: January 2024 to January 2025',
      '## Process flow and understanding',
      '1. **Requisition initiation and approval** — Raised in the ERP.\n2. **Purchase Order** — Raised on approval.',
      '## Data analytics performed',
      '1. **Invoices billed above approved amounts** — Three paid invoices.\n2. **Payments to ineligible vendors** — Two.',
    ].join('\n\n')
    const { body } = await open(await markdownWord({
      markdown,
      sheet: { cover: { eyebrow: 'Internal audit · Procurement' } },
      options: { stepNames: ['Requisition initiation and approval', 'Purchase Order'] },
    }))
    const text = words(body)

    expect(text).toContain('1 Engagement')
    expect(text).toContain('Entity Global Bank')
    expect(text).toContain('Requisition initiation and approval Raised in the ERP.')
    expect(text).toContain('Invoices billed above approved amounts Three paid invoices.')
    // A number hung in the margin is a hanging indent.
    expect(body).toMatch(/w:hanging="567"/)
  })

  it('numbers the memo\'s exhibits and puts the tested columns first', async () => {
    const markdown = [
      'The population is small.',
      '## What the analysis found',
      '### 1. Invoices billed above approved amounts',
      'Three invoices exceed both totals (A-8BFCE4A3).',
      '```embed\nanalysis: A-8BFCE4A3\nas: exception_table\ncaption: 3 rows of 52 breach INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT\n```',
    ].join('\n\n')
    const columns = ['INVOICE_ID', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'PO_TOTAL_AMOUNT', 'INVOICE_AMOUNT']
    const exhibit: ExhibitWord = {
      title: 'Invoiced amount exceeds the linked PO total', found: '3 of 52 failed', tone: 'bad', id: 'A-8BFCE4A3',
      as: 'exception_table', caption: '3 rows of 52 breach INVOICE_AMOUNT ≤ PO_TOTAL_AMOUNT', failed: false,
      frame: { columns, dtypes: columns.map(() => 'String'), rows: [columns.map(column => `${column}-1`)] },
      tested: ['INVOICE_AMOUNT', 'PO_TOTAL_AMOUNT'], image: null, stats: [],
    }
    const { body } = await open(await memoWord({
      sheet: { cover: { title: 'Data analysis summary' } },
      markdown,
      entries: markdownOutline(markdown),
      options: { citations: { 'A-8BFCE4A3': { exhibit: 1, tone: 'bad' } } },
      figures: [{ label: 'tables received', value: 6 }],
      exhibit: () => exhibit,
    }))
    const text = words(body)

    expect(text).toContain('In brief')
    expect(text).toContain('Exhibit 1')
    // The citation is a superscript to its exhibit, as on the sheet.
    expect(text).toContain('Ex. 1')
    expect(body).toContain('w:vertAlign w:val="superscript"')
    // Tested columns lead; the rest that do not fit are counted, not dropped silently.
    expect(text.indexOf('INVOICE_AMOUNT')).toBeLessThan(text.indexOf('INVOICE_ID'))
    expect(text).toContain('3 more columns in the app')
  })
})
