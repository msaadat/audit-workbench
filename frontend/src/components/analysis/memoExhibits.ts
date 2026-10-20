import type { Citation } from '../ui/markdownHtml'
import type { SavedAnalysis } from '../../types'
import { classificationTone } from './analysisStatus'

/**
 * The memo as a document of prose and exhibits.
 *
 * The memo is one Markdown string with embed fences between its paragraphs;
 * the assembler (`workers/analysis._render_summary_markdown`) writes every
 * fence itself, after the finding it supports, so their order is the order a
 * reader meets them. That is all the numbering needs: the n-th fence is
 * Exhibit n, and fences with nothing written between them are one set.
 */

export interface Embed { analysis: string; as: string; caption?: string }
export type Segment = { kind: 'prose'; text: string } | { kind: 'embed'; embed: Embed }

const EMBED_BLOCK = /^```embed[ \t]*\n([\s\S]*?)^```[ \t]*$/gm

function parseEmbed(body: string): Embed | null {
  const fields: Record<string, string> = {}
  for (const line of body.split('\n')) {
    const match = /^([a-z_]+)\s*:\s*(.*)$/.exec(line.trim())
    if (match) fields[match[1]] = match[2].trim()
  }
  if (!fields.analysis) return null
  return { analysis: fields.analysis, as: fields.as || 'summary_table', caption: fields.caption }
}

/** Prose and embeds, in order. A malformed fence is dropped: the memo reads without it. */
export function memoSegments(markdown: string): Segment[] {
  const out: Segment[] = []
  const source = markdown || ''
  let cursor = 0
  EMBED_BLOCK.lastIndex = 0
  for (let match = EMBED_BLOCK.exec(source); match; match = EMBED_BLOCK.exec(source)) {
    const prose = source.slice(cursor, match.index)
    if (prose.trim()) out.push({ kind: 'prose', text: prose })
    const embed = parseEmbed(match[1])
    if (embed) out.push({ kind: 'embed', embed })
    cursor = match.index + match[0].length
  }
  const tail = source.slice(cursor)
  if (tail.trim()) out.push({ kind: 'prose', text: tail })
  return out
}

export interface Exhibit { number: number; embed: Embed }
export type MemoUnit =
  | { kind: 'prose'; text: string; index: number }
  | { kind: 'exhibits'; exhibits: Exhibit[]; key: string }

/** Number every embed, and gather the ones with nothing between them into one set. */
export function memoUnits(segments: Segment[]): MemoUnit[] {
  const out: MemoUnit[] = []
  let number = 0
  segments.forEach((segment, index) => {
    if (segment.kind === 'prose') { out.push({ kind: 'prose', text: segment.text, index }); return }
    number += 1
    const last = out[out.length - 1]
    const exhibit = { number, embed: segment.embed }
    if (last?.kind === 'exhibits') last.exhibits.push(exhibit)
    else out.push({ kind: 'exhibits', exhibits: [exhibit], key: `exhibit-set-${number}` })
  })
  return out
}

/**
 * How each procedure is cited in the memo's prose: a superscript to its first
 * exhibit where it is drawn as one, otherwise a chip with its result's dot.
 */
export function memoCitations(analyses: SavedAnalysis[], units: MemoUnit[]): Record<string, Citation> {
  const exhibitOf = new Map<string, number>()
  for (const unit of units) {
    if (unit.kind !== 'exhibits') continue
    for (const exhibit of unit.exhibits) {
      if (!exhibitOf.has(exhibit.embed.analysis)) exhibitOf.set(exhibit.embed.analysis, exhibit.number)
    }
  }
  const out: Record<string, Citation> = {}
  for (const analysis of analyses) {
    out[analysis.id] = {
      exhibit: exhibitOf.get(analysis.id),
      tone: classificationTone(analysis.classification),
      title: analysis.title,
    }
  }
  return out
}

/**
 * The columns a procedure tested, read from its own spec rather than from the
 * caption: `compare_columns` names `column` and `other`, a duplicate test its
 * `columns` or `key`. Any parameter naming a column of the flagged frame
 * counts, in the order the spec names them, so a new analytic needs no entry
 * here to have its columns put first.
 */
export function testedColumns(analysis: SavedAnalysis | undefined, columns: string[]): string[] {
  const params = (analysis?.spec?.params ?? {}) as Record<string, unknown>
  const known = new Set(columns)
  const out: string[] = []
  const take = (value: unknown) => {
    if (typeof value === 'string' && known.has(value) && !out.includes(value)) out.push(value)
    else if (Array.isArray(value)) value.forEach(take)
  }
  Object.values(params).forEach(take)
  return out
}
