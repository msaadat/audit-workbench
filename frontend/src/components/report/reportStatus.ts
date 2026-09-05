import type { OutlineEntry } from '../ui/markdownOutline'
import type { AuditReport, ReportContext, ReportQualityIssue } from '../../types'
import { plural } from '../../format'

/**
 * What the quality check found, said as facts about the report rather than as
 * a list of codes.
 *
 * The old panel printed fifty-six issue cards in one column, most of them the
 * same three sentences about eighteen findings, under a heading that said
 * `Deterministic checks`. Fifty-six is not a number anybody acts on. There are
 * only two kinds of problem here — something is wrong with the report, or a
 * finding cannot go into it — and the second kind is eighteen facts wearing
 * fifty-four coats.
 */

/**
 * Quality codes are enum identifiers. Title-casing them ("Stale Evidence",
 * "Finding Draft") made them legible but not meaningful — "Finding Draft" does
 * not name the problem. Anything unmapped still degrades to the humanised code.
 */
export const ISSUE_HEADINGS: Record<string, string> = {
  broken_evidence: 'A finding cites evidence that cannot be resolved',
  broken_rcm_ref: 'A finding references an RCM row that no longer exists',
  broken_report_citation: 'The report cites a finding that no longer exists',
  broken_test_ref: 'A finding references a test that no longer exists',
  duplicate_finding: 'Two findings report nearly the same thing',
  editorial_unavailable: 'Editorial review could not run',
  finding_draft: 'A finding is still a draft',
  finding_missing_from_report: 'A confirmed finding is not cited in the report',
  missing_limitations: 'Recorded scope limitations are not disclosed',
  preliminary_label_missing: 'The report is not labelled as a preliminary draft',
  report_arithmetic: "The report's finding count does not match the register",
  report_empty: 'The report has not been drafted yet',
  report_rating_unsupported: 'The report asserts a rating nothing supports',
  report_risk_arithmetic: "The report's risk counts disagree with the RCM",
  stale_evidence: 'Evidence has changed since it was cited',
  unresolved_exception: 'An exception has no recorded disposition',
  unsupported_finding: 'A finding has no supporting test result',
}

export function issueHeading(code: string): string {
  return ISSUE_HEADINGS[code] ?? code.replaceAll('_', ' ')
}

/** The finding an issue is about, where it is about one. */
export function issueFinding(issue: ReportQualityIssue): string | null {
  const ref = issue.refs.find(value => value.startsWith('finding:'))
  return ref ? ref.slice('finding:'.length) : null
}

export interface ReportIssues {
  /** Problems with the document itself — the ones an editor fixes. */
  aboutReport: ReportQualityIssue[]
  /** Finding id → the issues raised about it, in the order they were raised. */
  byFinding: Map<string, ReportQualityIssue[]>
  all: ReportQualityIssue[]
  checkedAt: string
}

export function reportIssues(report: AuditReport | null): ReportIssues {
  const all = [...(report?.quality.issues ?? []), ...(report?.quality.editorial ?? [])]
  const aboutReport: ReportQualityIssue[] = []
  const byFinding = new Map<string, ReportQualityIssue[]>()
  for (const issue of all) {
    const finding = issueFinding(issue)
    if (!finding) { aboutReport.push(issue); continue }
    const existing = byFinding.get(finding)
    if (existing) existing.push(issue)
    else byFinding.set(finding, [issue])
  }
  return { aboutReport, byFinding, all, checkedAt: report?.quality.checked_at ?? '' }
}

/**
 * Which heading each report-level issue belongs under.
 *
 * The check knows what is wrong; it does not know where in the document to say
 * so, because it reads the report as one string. The map is by intent —
 * a missing preliminary label is a fact about the title page, an unsupported
 * rating a fact about the conclusion — and is matched against the headings the
 * document actually has, so a report written to a different template loses the
 * strip rather than putting it somewhere untrue.
 */
const ISSUE_SECTION: Record<string, RegExp> = {
  preliminary_label_missing: /^$/,
  report_rating_unsupported: /conclusion|opinion|rating/i,
  report_arithmetic: /summary|overview|results/i,
  report_risk_arithmetic: /summary|overview|risk/i,
  missing_limitations: /scope|limitation|approach/i,
  finding_missing_from_report: /detailed finding|findings/i,
  broken_report_citation: /detailed finding|findings/i,
}

/**
 * Issues attached to the heading they are about, plus the ones that belong to
 * the title — which is the document itself rather than any section of it.
 */
export function placeIssues(
  entries: OutlineEntry[], issues: ReportQualityIssue[],
): { title: ReportQualityIssue[]; bySection: Map<string, ReportQualityIssue[]> } {
  const title: ReportQualityIssue[] = []
  const bySection = new Map<string, ReportQualityIssue[]>()
  // The title strip carries what is true of the whole document; `h1` is not a
  // section a reader scrolls to, so it is never a target.
  const sections = entries.filter(entry => entry.level > 1)
  for (const issue of issues) {
    const pattern = ISSUE_SECTION[issue.code]
    const match = pattern && pattern.source !== '^$'
      ? sections.find(entry => pattern.test(entry.text))
      : undefined
    if (!match) { title.push(issue); continue }
    const existing = bySection.get(match.id)
    if (existing) existing.push(issue)
    else bySection.set(match.id, [issue])
  }
  return { title, bySection }
}

/** A red dot on every heading an issue points at, for the outline. */
export function outlineMarks(
  bySection: Map<string, ReportQualityIssue[]>,
): Record<string, 'bad' | 'warn'> {
  const marks: Record<string, 'bad' | 'warn'> = {}
  for (const [id, issues] of bySection) {
    marks[id] = issues.some(issue => issue.severity === 'error') ? 'bad' : 'warn'
  }
  return marks
}

export function stamp(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

/**
 * The verdict bar's first line: how much is wrong, split the way the reader
 * has to act on it — the report's own problems are edits, and a finding that
 * cannot be included is fieldwork.
 */
export function verdictLine(issues: ReportIssues): string {
  if (!issues.checkedAt) return 'Not yet checked against the register'
  const report = issues.aboutReport.length
  const findings = issues.byFinding.size
  if (!report && !findings) return `No issues · checked ${stamp(issues.checkedAt)}`
  const parts: string[] = []
  if (report) parts.push(`${plural(report, 'issue')} with the report`)
  if (findings) parts.push(`${plural(findings, 'finding')} it cannot include`)
  return `${parts.join(' and ')} · checked ${stamp(issues.checkedAt)}`
}

export function verdictTone(issues: ReportIssues): 'ok' | 'warn' | 'bad' | 'neutral' {
  if (!issues.checkedAt) return 'neutral'
  if (issues.all.some(issue => issue.severity === 'error')) return 'bad'
  return issues.all.length ? 'warn' : 'ok'
}

/**
 * The stale strip: the report-level facts that decide what this draft is
 * allowed to claim, in one sentence rather than as three separate checks.
 */
export function staleSentence(
  context: ReportContext | null, issues: ReportIssues,
): string | undefined {
  if (!context?.preliminary) return undefined
  const coverage = (context.completion?.coverage ?? {}) as Record<string, unknown>
  const untested = (coverage.rows_without_tests as unknown[] | undefined)?.length ?? 0
  const stale = issues.all.filter(issue => issue.code === 'stale_evidence').length
  const clauses: string[] = []
  if (untested) clauses.push(`${plural(untested, 'risk')} ${untested === 1 ? 'has' : 'have'} no test run`)
  if (stale) {
    const findings = new Set(
      issues.all.filter(issue => issue.code === 'stale_evidence').map(issueFinding),
    ).size
    clauses.push(`${plural(findings, 'finding')} ${findings === 1 ? 'has' : 'have'} lost evidence since generation`)
  }
  const opening = clauses.length ? `Fieldwork is still open: ${clauses.join(' and ')}.` : 'Fieldwork is still open.'
  return `${opening} The draft must be labelled preliminary and cannot carry an overall rating.`
}

/**
 * The outline, with the detailed findings folded.
 *
 * A report carrying eighteen findings spends five headings on each of them, so
 * the outline becomes ninety entries of `Condition`, `Criteria`, `Risk` — the
 * same six words repeated, none of which locates anything. Three findings is
 * enough to show the shape; the rest are a count, and the section heading
 * itself still scrolls there.
 */
export function collapseFindings(entries: OutlineEntry[], limit = 3): OutlineEntry[] {
  const start = entries.findIndex(entry => entry.level === 2 && /detailed finding/i.test(entry.text))
  if (start < 0) return entries
  const end = entries.findIndex((entry, index) => index > start && entry.level <= 2)
  const tail = end < 0 ? entries.length : end
  const inner = entries.slice(start + 1, tail)
  const kept = inner.filter(entry => entry.level === 3).slice(0, limit)
  const hidden = inner.filter(entry => entry.level === 3).length - kept.length
  return [
    ...entries.slice(0, start + 1),
    ...kept,
    // The row scrolls to where the hidden ones start, so it is a way in rather
    // than a label that looks clickable and is not.
    ...(hidden > 0
      ? [{ id: inner.filter(entry => entry.level === 3)[limit].id, level: 3, text: `… ${hidden} more` }]
      : []),
    ...entries.slice(tail),
  ]
}
