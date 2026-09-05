/**
 * Auditor-facing vocabulary for what a saved analysis concluded.
 *
 * The labels deliberately match the Data tests and Document tests rails: a
 * procedure that ran and found nothing says "No exception" on every surface,
 * not "Clear" here and "No exception" next door. Only the states genuinely
 * unique to a saved procedure — staleness, informational output — keep names
 * of their own.
 *
 * The server classifies every procedure once, from its durable result
 * (`analysis_results.analysis_state`). This module only names those
 * classifications — it never re-derives one from a live recomputation, which is
 * what used to let the rail and the summary disagree about the same procedure.
 *
 * Freshness is a second axis, not a seventh classification. `stale` used to sit
 * in this enum and displace the verdict, so a procedure that had recorded four
 * breaches and then had its definition edited read as "Rerun required" and its
 * four breaches were counted nowhere. `AnalysisResultState` answers that
 * question now, and `freshnessMeta` below names it.
 */
import type { AnalysisResultState, AnalysisSummaryClassification, SavedAnalysis } from '../../types'
import { stamp } from '../report/reportStatus'

interface ClassificationMeta {
  label: string
  /** Tag severity, matching PrimeVue's scale. */
  severity: 'danger' | 'warn' | 'success' | 'info' | 'secondary'
  icon: string
  /** What the auditor is being told, in one line. */
  hint: string
}

const META: Record<AnalysisSummaryClassification, ClassificationMeta> = {
  exception: {
    label: 'Exception',
    severity: 'danger',
    icon: 'pi pi-times-circle',
    hint: 'This procedure concluded that the population contains exceptions.',
  },
  unusual: {
    label: 'Need review',
    severity: 'warn',
    icon: 'pi pi-exclamation-triangle',
    hint: 'This procedure returned something worth a look, short of an exception.',
  },
  execution_error: {
    label: 'Blocked',
    severity: 'danger',
    icon: 'pi pi-ban',
    hint: 'The definition could not run. Fix the spec, then run it again.',
  },
  not_run: {
    label: 'Not run',
    severity: 'secondary',
    icon: 'pi pi-clock',
    hint: 'This procedure has never been executed, so it has concluded nothing.',
  },
  clear: {
    label: 'No exception',
    severity: 'success',
    icon: 'pi pi-check-circle',
    hint: 'This procedure ran and found nothing to report.',
  },
  informational: {
    label: 'Informational',
    severity: 'info',
    icon: 'pi pi-info-circle',
    hint: 'This procedure returns context rather than a pass/fail conclusion.',
  },
}

export function classificationMeta(
  classification: AnalysisSummaryClassification,
): ClassificationMeta {
  return META[classification] ?? META.informational
}

/** Triage buckets, mirroring the server's `CLASSIFICATION_BUCKETS`. */
export const BUCKET_CLASSIFICATIONS: Record<string, AnalysisSummaryClassification[]> = {
  exception: ['exception'],
  unusual: ['unusual'],
  errors: ['execution_error'],
  clear: ['clear'],
  informational: ['informational'],
  not_run: ['not_run'],
}

/**
 * What a procedure's freshness is called, where a reader can see it.
 *
 * `current` has no label: a result that still stands is the unremarkable case,
 * and marking twenty-three rows "current" says nothing a reader can act on.
 */
const FRESHNESS: Record<AnalysisResultState, { label: string; icon: string } | null> = {
  current: null,
  stale: { label: 'Rerun required', icon: 'pi pi-refresh' },
  not_run: { label: 'Not run', icon: 'pi pi-clock' },
}

export function freshnessMeta(state: AnalysisResultState) {
  return FRESHNESS[state] ?? null
}

/** Neither has concluded anything current, so both are what "run it" targets. */
export function isOutstanding(analysis: Pick<SavedAnalysis, 'state'>): boolean {
  return analysis.state === 'stale' || analysis.state === 'not_run'
}

/**
 * What a procedure *is*, from its kind and who wrote it.
 *
 * Source alone is not enough: the assistant authors both library tests and
 * Polars code, so labelling every `ai` procedure "AI code" mislabels a
 * configured analytics test as a script.
 */
export function provenance(
  analysis: Pick<SavedAnalysis, 'kind' | 'source'>,
): { icon: string; label: string } {
  if (analysis.kind === 'python') {
    return analysis.source === 'code'
      ? { icon: 'pi pi-code', label: 'Custom code' }
      : { icon: 'pi pi-sparkles', label: 'Assistant code' }
  }
  return analysis.source === 'ai'
    ? { icon: 'pi pi-sparkles', label: 'Assistant test' }
    : { icon: 'pi pi-book', label: 'Library test' }
}

/**
 * When a result was recorded, in the stamp every other work product uses.
 *
 * It was a bare `toLocaleString()`, which gave the analysis pages a date
 * format nothing else in the engagement wrote.
 */
export function formatExecutedAt(value: string | null | undefined): string {
  return stamp(value)
}
