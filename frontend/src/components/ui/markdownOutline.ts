/**
 * The headings of a Markdown document, as the thing you navigate it by.
 *
 * The memorandum is 2,900 words and the report is 41,000 characters; both were
 * rendered as one unbroken column with no way to reach a section except
 * scrolling. An outline is the cheapest fix, but only if the anchor it links to
 * and the anchor the document renders are derived the same way — so the slug
 * lives here and `MarkdownView` imports it rather than inventing its own.
 */

export interface OutlineEntry {
  /** The `id` on the rendered heading, and the fragment the link targets. */
  id: string
  /** 1–4, as written. The views draw `h2` flush and `h3` indented. */
  level: number
  text: string
}

/**
 * A stable id for one heading.
 *
 * Deliberately not a hash: a reader who copies a link to `#audit-conclusion`
 * has something that still reads as the section it names. Duplicate headings
 * are disambiguated by the caller, which is the only place that knows how many
 * came before.
 */
export function headingSlug(text: string): string {
  const slug = text
    .toLowerCase()
    .replace(/[`*_]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return slug || 'section'
}

/**
 * Every `#`–`####` heading, in document order, with the ids `MarkdownView`
 * renders.
 *
 * Fenced code is skipped: a comment line starting with `#` inside a Python
 * block is not a section of the report, and the memoranda the assistant drafts
 * do carry code fences.
 */
export function markdownOutline(markdown: string): OutlineEntry[] {
  const entries: OutlineEntry[] = []
  const seen = new Map<string, number>()
  let fenced = false
  for (const raw of (markdown || '').split('\n')) {
    const line = raw.trimEnd()
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; continue }
    if (fenced) continue
    const match = /^(#{1,4})\s+(.+?)\s*$/.exec(line)
    if (!match) continue
    const text = match[2].replace(/[`*]/g, '').trim()
    const base = headingSlug(text)
    const count = (seen.get(base) ?? 0) + 1
    seen.set(base, count)
    entries.push({ id: count === 1 ? base : `${base}-${count}`, level: match[1].length, text })
  }
  return entries
}
