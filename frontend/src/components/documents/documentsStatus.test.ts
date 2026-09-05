import { describe, expect, it } from 'vitest'

import type { AuditDocument, DocumentVocabulary } from '../../types'
import {
  DOCUMENT_CHIPS, documentMeta, documentTone, documentsStatus, filterDocuments,
} from './documentsStatus'
import type { DocumentsFacts } from './documentsStatus'

function document(id: string, overrides: Partial<AuditDocument> = {}): AuditDocument {
  return {
    id, file: `${id}.pdf`, source: `${id}.pdf`, source_id: null, relative_path: null,
    title: id, category: 'evidence', pages: 1, sha1: 'x',
    text_state: 'extracted', note: '', created: '', updated: null, created_by: 'user',
    agent_run_id: null,
    analysis_run_state: 'idle', analysis_coverage_state: 'complete',
    analysis_validity_state: 'current', analysis_updated_at: null,
    analysis_review_state: 'needs_review', has_analysis_overrides: false,
    candidate_analysis_id: null, analysis_resumable_run_id: null,
    search_index_state: 'ready', analysis_vision_used: false,
    classification: { document_type: 'vendor_invoice', assigned_by: 'model' },
    ...overrides,
  } as AuditDocument
}

const FACTS: DocumentsFacts = {
  vocabulary: [{ document_type: 'vendor_invoice', thin: true } as DocumentVocabulary],
  visionAvailable: true,
}

describe('documents status', () => {
  it('separates read, analysed and reviewed, which fail independently', () => {
    const model = documentsStatus([
      document('a'),
      document('b', { analysis_review_state: 'reviewed' }),
      document('c', { analysis_coverage_state: 'none', analysis_validity_state: null }),
      document('d', { text_state: 'pending', analysis_coverage_state: 'none', analysis_validity_state: null }),
    ], FACTS)
    const byKey = Object.fromEntries(model.lanes.map(lane => [lane.key, lane]))

    expect(`${byKey.read.value}/${byKey.read.total}`).toBe('3/4')
    expect(`${byKey.analysed.value}/${byKey.analysed.total}`).toBe('2/4')
    // The one the old page could not state at all.
    expect(`${byKey.reviewed.value}/${byKey.reviewed.total}`).toBe('1/4')
  })

  it('promotes only filters it derives', () => {
    const known = new Set(
      (documentsStatus([], FACTS).filters ?? []).flatMap(group => group.options.map(option => option.key)),
    )
    for (const chip of DOCUMENT_CHIPS) expect(known.has(chip.filter)).toBe(true)
    expect(DOCUMENT_CHIPS).toHaveLength(6)
  })
})

describe('narrowing the corpus', () => {
  const documents = [
    document('analysed'),
    document('fresh', { analysis_coverage_state: 'none', analysis_validity_state: null }),
    document('unidentified', { classification: { document_type: null, assigned_by: null } as AuditDocument['classification'] }),
    document('broken', { text_state: 'failed' }),
  ]
  const ids = (filter: Parameters<typeof filterDocuments>[1]) =>
    filterDocuments(documents, filter, FACTS).map(item => item.id)

  it('selects what each chip counts', () => {
    expect(ids('not_analysed')).toEqual(['fresh'])
    expect(ids('needs_review')).toEqual(['analysed', 'fresh', 'unidentified', 'broken'])
    expect(ids('unidentified')).toEqual(['unidentified'])
    expect(ids('attention')).toEqual(['broken'])
    expect(ids('thin_vocabulary')).toEqual(['analysed', 'fresh', 'broken'])
  })
})

describe('what a row says', () => {
  it('carries what the document is, and nothing the dot or the chips say', () => {
    // Not the page count, which distinguishes nothing, and not the analysis
    // state, which the dot carries and the chips count.
    expect(documentMeta(document('a'), FACTS)).toEqual([{ text: 'vendor invoice' }])
    expect(documentMeta(document('a', { text_state: 'failed' }), FACTS))
      .toEqual([{ text: 'vendor invoice' }])
  })

  it('says a document with no type has none, where a type is asked for', () => {
    expect(documentMeta(
      document('a', { classification: { document_type: null, assigned_by: null } as AuditDocument['classification'] }),
      FACTS,
    )).toEqual([{ text: 'not identified', tone: 'warn' }])
  })

  it('says nothing at all about a policy, which is not read under a type', () => {
    expect(documentMeta(
      document('a', { category: 'policy', classification: { document_type: null, assigned_by: null } as AuditDocument['classification'] }),
      FACTS,
    )).toEqual([])
  })

  it('reads the dot off the same status the meta line does', () => {
    expect(documentTone(document('a'), FACTS)).toBe('ok')
    expect(documentTone(document('a', { text_state: 'failed' }), FACTS)).toBe('bad')
    expect(documentTone(document('a', { text_state: 'pending' }), FACTS)).toBe('info')
  })
})
