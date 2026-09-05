import { describe, expect, it } from 'vitest'

import type { AuditReport, ReportContext, ReportQualityIssue } from '../../types'
import { markdownOutline } from '../ui/markdownOutline'
import {
  collapseFindings, outlineMarks, placeIssues, reportIssues,
  staleSentence, verdictLine, verdictTone,
} from './reportStatus'

function issue(code: string, overrides: Partial<ReportQualityIssue> = {}): ReportQualityIssue {
  return {
    code, severity: 'error', message: `${code} happened.`, refs: [], source: 'deterministic',
    ...overrides,
  } as ReportQualityIssue
}

function report(issues: ReportQualityIssue[], overrides: Partial<AuditReport> = {}): AuditReport {
  return {
    markdown: '# Internal Audit Report\n\nBody.\n',
    generated_markdown: '', generated_at: '2026-09-03T12:01:00Z', generated_by_run: null,
    edited: false, updated: null, generation_warnings: [],
    quality: {
      checked_at: '2026-09-05T06:47:00Z', issues, ok: false,
      counts: { error: issues.length, warning: 0, info: 0 },
    },
    ...overrides,
  } as AuditReport
}

describe('report issues', () => {
  it('splits what an editor fixes from what fieldwork owes', () => {
    const split = reportIssues(report([
      issue('preliminary_label_missing'),
      issue('finding_draft', { refs: ['finding:F-1'] }),
      issue('unsupported_finding', { refs: ['finding:F-1'] }),
      issue('stale_evidence', { refs: ['finding:F-2', 'EV-9'], severity: 'warning' }),
    ]))

    expect(split.aboutReport.map(item => item.code)).toEqual(['preliminary_label_missing'])
    // Three issues over two findings is two rows, because it is two facts.
    expect([...split.byFinding.keys()]).toEqual(['F-1', 'F-2'])
    expect(split.byFinding.get('F-1')).toHaveLength(2)
  })

  it('counts the two kinds separately in the verdict line', () => {
    const split = reportIssues(report([
      issue('preliminary_label_missing'),
      issue('report_rating_unsupported'),
      issue('finding_draft', { refs: ['finding:F-1'] }),
    ]))

    expect(verdictLine(split)).toContain('2 issues with the report and 1 finding it cannot include')
    expect(verdictTone(split)).toBe('bad')
  })

  it('says so plainly when the report has never been checked', () => {
    const never = reportIssues(report([], {
      quality: { checked_at: '', issues: [], ok: true, counts: { error: 0, warning: 0, info: 0 } },
    } as Partial<AuditReport>))

    expect(verdictLine(never)).toBe('Not yet checked against the register')
    expect(verdictTone(never)).toBe('neutral')
  })
})

describe('placing an issue in the document', () => {
  const entries = markdownOutline(
    '# Internal Audit Report\n## A. Executive Summary\n### 3. Audit Conclusion\n## B. Detailed Findings\n',
  )

  it('attaches a fact about the whole draft to the title, and a rating to the conclusion', () => {
    const placed = placeIssues(entries, [
      issue('preliminary_label_missing'),
      issue('report_rating_unsupported'),
    ])

    expect(placed.title.map(item => item.code)).toEqual(['preliminary_label_missing'])
    expect(placed.bySection.get('3-audit-conclusion')?.map(item => item.code))
      .toEqual(['report_rating_unsupported'])
    expect(outlineMarks(placed.bySection)).toEqual({ '3-audit-conclusion': 'bad' })
  })

  it('falls back to the title when the report has no heading the issue is about', () => {
    const placed = placeIssues(markdownOutline('# Report\n'), [issue('report_rating_unsupported')])

    expect(placed.title).toHaveLength(1)
    expect(placed.bySection.size).toBe(0)
  })
})

describe('the outline of a report full of findings', () => {
  it('keeps three findings and counts the rest', () => {
    const entries = markdownOutline([
      '# Report', '## B. Detailed Findings',
      '### One', '### Two', '### Three', '### Four', '### Five',
      '## C. Appendix',
    ].join('\n\n'))

    const collapsed = collapseFindings(entries)

    expect(collapsed.map(entry => entry.text)).toEqual([
      'Report', 'B. Detailed Findings', 'One', 'Two', 'Three', '… 2 more', 'C. Appendix',
    ])
    // The row is a way in, so it scrolls to where the hidden ones start.
    expect(collapsed[5].id).toBe('four')
  })

  it('leaves a short report alone', () => {
    const entries = markdownOutline('# Report\n\n## B. Detailed Findings\n\n### One\n')
    expect(collapseFindings(entries)).toEqual(entries)
  })
})

describe('the preliminary strip', () => {
  const context = {
    preliminary: true,
    completion: { coverage: { rows_without_tests: ['R1', 'R2'] } },
  } as unknown as ReportContext

  it('states what is still open and what that forbids', () => {
    const sentence = staleSentence(context, reportIssues(report([
      issue('stale_evidence', { refs: ['finding:F-1'], severity: 'warning' }),
      issue('stale_evidence', { refs: ['finding:F-1', 'EV-2'], severity: 'warning' }),
    ])))

    expect(sentence).toContain('2 risks have no test run')
    // Two issues, one finding: the sentence counts findings, not issues.
    expect(sentence).toContain('1 finding has lost evidence')
    expect(sentence).toContain('cannot carry an overall rating')
  })

  it('says nothing when fieldwork is closed', () => {
    expect(staleSentence({ ...context, preliminary: false } as ReportContext, reportIssues(report([]))))
      .toBeUndefined()
  })
})
