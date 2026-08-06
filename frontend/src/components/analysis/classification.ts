/**
 * Auditor-facing vocabulary for what a saved analysis concluded.
 *
 * The server classifies every procedure once, from its durable result
 * (`analysis_results.analysis_state`). This module only names those
 * classifications — it never re-derives one from a live recomputation, which is
 * what used to let the rail and the summary disagree about the same procedure.
 */
import type { AnalysisSummaryClassification, SavedAnalysis } from '../../types'

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
    label: 'Unusual result',
    severity: 'warn',
    icon: 'pi pi-exclamation-triangle',
    hint: 'This procedure returned something worth a look, short of an exception.',
  },
  execution_error: {
    label: 'Execution issue',
    severity: 'danger',
    icon: 'pi pi-ban',
    hint: 'The definition could not run. Fix the spec, then run it again.',
  },
  stale: {
    label: 'Rerun required',
    severity: 'warn',
    icon: 'pi pi-refresh',
    hint: 'The definition or its source data changed after this result was recorded.',
  },
  not_run: {
    label: 'Not run',
    severity: 'secondary',
    icon: 'pi pi-clock',
    hint: 'This procedure has never been executed, so it has concluded nothing.',
  },
  clear: {
    label: 'Clear',
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
  stale: ['stale'],
  clear: ['clear'],
  informational: ['informational'],
  not_run: ['not_run'],
}

/** Neither has concluded anything current, so both are what "run it" targets. */
export const OUTSTANDING: AnalysisSummaryClassification[] = ['stale', 'not_run']

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

export function formatExecutedAt(value: string | null | undefined): string {
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? '' : parsed.toLocaleString()
}
