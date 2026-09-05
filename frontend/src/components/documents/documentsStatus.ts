import { portion } from '../ui/statusLanes'
import type {
  ReviewChip, StatusFilterGroup, StatusLane, StatusModel,
} from '../ui/statusLanes'
import { documentStatus } from '../../composables/documentStatus'
import type { AuditDocument, DocumentVocabulary } from '../../types'

/**
 * Whether the corpus is ready to be relied on, in three answers.
 *
 * Nothing on the page said any of them. Eight documents were analysed and no
 * person had read a single analysis, and the only place that fact appeared was
 * a `needs review` tag on the Analysis tab of one document at a time — so a
 * corpus nobody had checked looked exactly like a corpus that was finished.
 *
 * Read, analysed, reviewed: three states of the same document that fail
 * independently. A document can be read and never analysed, analysed against
 * an earlier version of itself, or analysed and never read by a person, and
 * the last of those is the one an engagement quietly ships.
 */

export type DocumentsFilter =
  | 'not_analysed' | 'needs_review' | 'attention' | 'unidentified'
  | 'model_typed' | 'thin_vocabulary' | 'stale_analysis'

export interface DocumentsFacts {
  /** Vocabulary per document type, for the `thin` chip. */
  vocabulary: DocumentVocabulary[]
  visionAvailable: boolean
}

/** Text is out of it, whatever the analysis then did. */
export function isRead(document: AuditDocument): boolean {
  return document.text_state === 'extracted' || document.text_state === 'partial'
    || (document.text_state === 'image_only' && document.analysis_vision_used)
}

/** An analysis exists and belongs to the file as it now stands. */
export function isAnalysed(document: AuditDocument): boolean {
  return document.analysis_coverage_state !== 'none' && document.analysis_validity_state === 'current'
}

export function isReviewed(document: AuditDocument): boolean {
  return document.analysis_review_state === 'reviewed'
}

export function needsReview(document: AuditDocument): boolean {
  return document.analysis_review_state === 'needs_review'
}

/** Typed by the model and never confirmed by a person. */
export function isModelTyped(document: AuditDocument): boolean {
  return document.classification?.assigned_by === 'model'
}

export function isUnidentified(document: AuditDocument): boolean {
  return document.category === 'evidence' && !document.classification?.document_type
}

export function hasThinVocabulary(document: AuditDocument, facts: DocumentsFacts): boolean {
  const type = document.classification?.document_type
  if (!type) return false
  return Boolean(facts.vocabulary.find(item => item.document_type === type)?.thin)
}

export function needsAttention(document: AuditDocument, facts: DocumentsFacts): boolean {
  return documentStatus(document, { visionAvailable: facts.visionAvailable }).level === 'attention'
}

interface Counts {
  total: number
  read: number
  analysed: number
  reviewed: number
  notAnalysed: number
  needsReview: number
  attention: number
  unidentified: number
  modelTyped: number
  thin: number
  stale: number
  types: number
}

function tally(documents: AuditDocument[], facts: DocumentsFacts): Counts {
  const counts: Counts = {
    total: documents.length, read: 0, analysed: 0, reviewed: 0, notAnalysed: 0,
    needsReview: 0, attention: 0, unidentified: 0, modelTyped: 0, thin: 0, stale: 0,
    types: 0,
  }
  const types = new Set<string>()
  for (const document of documents) {
    if (isRead(document)) counts.read += 1
    if (isAnalysed(document)) counts.analysed += 1
    else counts.notAnalysed += 1
    if (isReviewed(document)) counts.reviewed += 1
    if (needsReview(document)) counts.needsReview += 1
    if (needsAttention(document, facts)) counts.attention += 1
    if (isUnidentified(document)) counts.unidentified += 1
    if (isModelTyped(document)) counts.modelTyped += 1
    if (hasThinVocabulary(document, facts)) counts.thin += 1
    if (document.analysis_validity_state === 'stale') counts.stale += 1
    const type = document.classification?.document_type
    if (type) types.add(type)
  }
  counts.types = types.size
  return counts
}

function lane(key: string, label: string, value: number, total: number, caption: string): StatusLane {
  return {
    key, label, state: total === 0 ? 'idle' : value === total ? 'done' : 'gap',
    value: String(value), total: String(total), caption,
    segments: [{ tone: value === total ? 'ok' : 'warn', portion: portion(value, total) }],
    chips: [], actions: [], rest: '',
  }
}

function filtersFor(counts: Counts): StatusFilterGroup[] {
  return [
    {
      key: 'analysis',
      label: 'Analysis',
      options: [
        { key: 'not_analysed', label: 'Not analysed', value: counts.notAnalysed, tone: 'warn' },
        { key: 'needs_review', label: 'Analysis to review', value: counts.needsReview, tone: 'warn' },
        { key: 'stale_analysis', label: 'Analysis stale', value: counts.stale, tone: 'warn' },
      ],
    },
    {
      key: 'condition',
      label: 'Condition',
      options: [
        { key: 'attention', label: 'Needs attention', value: counts.attention, tone: 'bad' },
      ],
    },
    {
      key: 'type',
      label: 'What it is read as',
      options: [
        { key: 'unidentified', label: 'Not identified', value: counts.unidentified, tone: 'warn' },
        { key: 'model_typed', label: 'Typed by the model', value: counts.modelTyped, tone: 'neutral' },
        { key: 'thin_vocabulary', label: 'Thin vocabulary', value: counts.thin, tone: 'warn' },
      ],
    },
  ]
}

export function documentsStatus(documents: AuditDocument[], facts: DocumentsFacts): StatusModel {
  const counts = tally(documents, facts)
  return {
    lanes: [
      lane('read', 'Read', counts.read, counts.total, `of ${counts.total} have text extracted`),
      lane('analysed', 'Analysed', counts.analysed, counts.total, `of ${counts.total} carry a current analysis`),
      lane('reviewed', 'Reviewed', counts.reviewed, counts.total, `of ${counts.total} analyses a person has read`),
    ],
    disclosures: [],
    filters: filtersFor(counts),
  }
}

/**
 * The six narrowings worth a permanent chip, in reading order: what has not
 * been read, what nobody has checked, what is broken, what has no type, what
 * the model typed, and what the type cannot support.
 */
export const DOCUMENT_CHIPS: ReviewChip[] = [
  { filter: 'not_analysed', tone: 'warn', label: 'Not analysed' },
  { filter: 'needs_review', tone: 'warn', label: 'Analysis to review' },
  { filter: 'attention', tone: 'bad', label: 'Needs attention' },
  { filter: 'unidentified', tone: 'warn', label: 'Not identified' },
  { filter: 'model_typed', tone: 'agent', label: 'Typed by the model' },
  { filter: 'thin_vocabulary', tone: 'warn', label: 'Thin vocabulary' },
]

export function filterDocuments(
  documents: AuditDocument[], filter: DocumentsFilter | null, facts: DocumentsFacts,
): AuditDocument[] {
  if (!filter) return documents
  return documents.filter(document => {
    switch (filter) {
      case 'not_analysed': return !isAnalysed(document)
      case 'needs_review': return needsReview(document)
      case 'stale_analysis': return document.analysis_validity_state === 'stale'
      case 'attention': return needsAttention(document, facts)
      case 'unidentified': return isUnidentified(document)
      case 'model_typed': return isModelTyped(document)
      case 'thin_vocabulary': return hasThinVocabulary(document, facts)
      default: return true
    }
  })
}

/**
 * The readiness dot: `ok` ready, `warn` attention, `info` processing, neutral
 * before anything has been read.
 */
export function documentTone(
  document: AuditDocument, facts: DocumentsFacts,
): 'ok' | 'warn' | 'bad' | 'info' | 'neutral' {
  const status = documentStatus(document, { visionAvailable: facts.visionAvailable })
  if (status.level === 'processing') return 'info'
  if (status.level === 'attention') return status.failed ? 'bad' : 'warn'
  return isRead(document) ? 'ok' : 'neutral'
}

/**
 * What one row says under the filename: what the document is, and nothing else.
 *
 * It carried the page count, the type and the analysis state, which on a list
 * of nine documents is the same three words nine times — the dot already says
 * whether a document needs attention, and the chips above say how many do. The
 * type is the one fact that differs from row to row, so it is the only one
 * kept; a piece of evidence with no type at all is a gap rather than a state,
 * and is still named.
 */
export function documentMeta(document: AuditDocument, facts: DocumentsFacts): Array<{
  text: string
  tone?: 'warn' | 'bad' | 'agent'
}> {
  void facts
  const type = document.classification?.document_type
  if (type) return [{ text: type.replace(/_/g, ' ') }]
  if (document.category === 'evidence') return [{ text: 'not identified', tone: 'warn' }]
  return []
}
