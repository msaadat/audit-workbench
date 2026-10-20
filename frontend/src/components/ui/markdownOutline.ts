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
  /**
   * The section number the document hangs in its margin and the outline
   * prints beside the entry: the heading's own (`A`, `3`) where it was written
   * with one, otherwise assigned in order (`5`, `7.2`). Absent where a heading
   * carries none — the title, a finding's `Condition`.
   */
  number?: string
  /** The heading without its own number, which `number` now carries. */
  label?: string
  /** A lettered `##` (`A. Executive Summary`): a part of the document, not a section. */
  part?: boolean
}

/**
 * A number a heading was written with. A letter needs its full stop (`A.`) and
 * so does a single figure (`3.`); a dotted figure may drop it (`2.1 Scope`).
 * Without those, "A review of…" and "2024 results" would lose their first word.
 */
const OWN_NUMBER = /^(?:([A-Z])[.)]|(\d+)[.)]|(\d+(?:\.\d+)+)\.?)\s+(\S.*)$/

function ownNumber(text: string): { number: string; label: string; letter: boolean } | null {
  const match = OWN_NUMBER.exec(text)
  if (!match) return null
  const number = match[1] ?? match[2] ?? match[3]
  return { number, label: match[4], letter: Boolean(match[1]) }
}

/**
 * Give every section its number, once, so the outline and the document cannot
 * disagree about it.
 *
 * The decision is per document, not per heading. The report numbers its own
 * sections (`A.`, `1.`) and must keep them; the memorandum numbers none, and a
 * reader of a printed memorandum needs `5` and `7.2` to point at. A document
 * that numbers any `##` itself is left to its own numbering throughout — mixing
 * an assigned `4` into a document that wrote `3.` and `5.` would invent a
 * section. A `###` is numbered under its parent only when the parent's number
 * was assigned and none of its siblings carries one of its own.
 */
function numberEntries(entries: OutlineEntry[]): OutlineEntry[] {
  const authored = entries.some(entry => entry.level === 2 && ownNumber(entry.text))
  let section = 0
  let parent: OutlineEntry | null = null
  let child = 0
  const childrenAuthored = new Map<OutlineEntry, boolean>()
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index]
    if (entry.level !== 2) continue
    let own = false
    for (let next = index + 1; next < entries.length && entries[next].level > 2; next += 1) {
      if (entries[next].level === 3 && ownNumber(entries[next].text)) own = true
    }
    childrenAuthored.set(entry, own)
  }
  return entries.map(entry => {
    const own = entry.level <= 3 ? ownNumber(entry.text) : null
    if (entry.level === 2) {
      parent = entry
      child = 0
      if (own) return { ...entry, number: own.number, label: own.label, part: own.letter }
      if (authored) return { ...entry, label: entry.text }
      section += 1
      return { ...entry, number: String(section), label: entry.text }
    }
    if (entry.level === 3) {
      if (own) return { ...entry, number: own.number, label: own.label }
      const parentNumber: string | undefined = parent && !authored && !childrenAuthored.get(parent)
        ? String(section)
        : undefined
      if (!parentNumber) return { ...entry, label: entry.text }
      child += 1
      return { ...entry, number: `${parentNumber}.${child}`, label: entry.text }
    }
    return { ...entry, label: entry.text }
  })
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
  return numberEntries(entries)
}
