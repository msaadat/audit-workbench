import type { AuditDocument } from '../types'

export type DocumentReadiness = 'processing' | 'ready' | 'attention'

export interface DocumentStatusSummary {
  level: DocumentReadiness
  label: string
  detail: string
  failed: boolean
}

type StatusFields = Pick<AuditDocument, 'text_state' | 'analysis_run_state' | 'analysis_coverage_state' | 'analysis_validity_state' | 'search_index_state'>

/**
 * Collapses the three per-document pipeline states (extraction, analysis, search
 * indexing) into a single user-facing readiness summary. "Ready" covers the
 * quiet cases, including documents that simply have not been analyzed yet;
 * only in-flight work or problems surface a distinct label.
 */
export function documentStatus(doc: StatusFields): DocumentStatusSummary {
  const working: string[] = []
  if (doc.text_state === 'pending') working.push('Extracting text')
  if (doc.analysis_run_state === 'queued' || doc.analysis_run_state === 'analyzing') working.push('Analyzing')
  if (doc.search_index_state === 'pending' || doc.search_index_state === 'indexing') working.push('Indexing for search')

  const problems: string[] = []
  if (doc.text_state === 'failed') problems.push('Extraction failed')
  else if (doc.text_state === 'partial') problems.push('Partially extracted')
  else if (doc.text_state === 'image_only') problems.push('Image-only source')
  if (doc.analysis_run_state === 'failed') problems.push('Analysis failed')
  else if (doc.analysis_run_state === 'paused' || doc.analysis_run_state === 'interrupted') problems.push('Analysis incomplete')
  else if (doc.analysis_validity_state === 'stale') problems.push('Analysis stale')
  else if (doc.analysis_coverage_state === 'partial') problems.push('Partial analysis')
  if (doc.search_index_state === 'failed') problems.push('Search indexing failed')
  else if (doc.search_index_state === 'stale') problems.push('Search index stale')

  const analysisDetail = doc.analysis_coverage_state === 'none'
    ? 'not analyzed'
    : `${doc.analysis_coverage_state}${doc.analysis_validity_state ? ` (${doc.analysis_validity_state})` : ''}`
  const detail = `Extraction: ${doc.text_state.replace('_', ' ')} · Analysis: ${analysisDetail} · Search: ${doc.search_index_state}`

  if (working.length) return { level: 'processing', label: `${working[0]}…`, detail, failed: false }
  if (problems.length) return { level: 'attention', label: problems[0], detail, failed: problems.some(item => item.includes('failed')) }
  return { level: 'ready', label: 'Ready', detail, failed: false }
}
