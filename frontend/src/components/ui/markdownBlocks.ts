import type { OutlineEntry } from './markdownOutline'

/**
 * A Markdown document split at its headings, so each heading is an element the
 * outline beside it can reach.
 *
 * `UiMarkdownDocument` did this inline, which was fine while it was the only
 * document on the app. The analysis memo is a document too, but one whose
 * prose is interrupted by embedded results, so it renders in several passes
 * and cannot hand the whole string to one component. Both need the same split
 * against the same entries — the ids have to match the outline's, and the
 * outline is computed once over the whole document — so the split lives here
 * and neither derives its own.
 */

export interface MarkdownBlock {
  /** The heading this block sits under, or null for text before the first. */
  entry: OutlineEntry | null
  body: string
}

/**
 * Split `markdown` at every heading, pairing each with the matching entry.
 *
 * `entries` are supplied rather than derived because a document rendered in
 * pieces has to number its duplicate headings across the whole of itself: the
 * second "Exceptions" is `exceptions-2` whether or not the piece it lands in
 * happens to be the first one to contain it.
 */
export function markdownBlocks(markdown: string, entries: OutlineEntry[]): MarkdownBlock[] {
  const out: MarkdownBlock[] = []
  let current: MarkdownBlock = { entry: null, body: '' }
  const lines: string[] = []
  let index = 0
  let fenced = false
  function flush() {
    current.body = lines.join('\n').trim()
    if (current.entry || current.body) out.push({ ...current })
    lines.length = 0
  }
  for (const raw of (markdown || '').split('\n')) {
    const line = raw.trimEnd()
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced
    if (!fenced && /^#{1,4}\s+/.test(line)) {
      flush()
      current = { entry: entries[index] ?? null, body: '' }
      index += 1
      continue
    }
    lines.push(line)
  }
  flush()
  return out
}

/** How many headings one piece of a document contains, for slicing entries. */
export function headingCount(markdown: string): number {
  let count = 0
  let fenced = false
  for (const raw of (markdown || '').split('\n')) {
    const line = raw.trimEnd()
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; continue }
    if (!fenced && /^#{1,4}\s+/.test(line)) count += 1
  }
  return count
}
