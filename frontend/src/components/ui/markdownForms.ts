import type {
  Blockquote, List, Nodes, Paragraph, PhrasingContent, RootContent, Strong, Table,
} from 'mdast'

/**
 * The forms a written work product takes, decided from the shape its Markdown
 * already has.
 *
 * The memorandum, the memo and the report are read as documents, and a
 * document has forms a README does not: a label beside its value, a numbered
 * register, a process drawn as steps, a risk named in a hanging column, a
 * ruled table with its severities. Each is decided here, over the parsed tree,
 * and each falls back to plain prose when the shape is not quite there — the
 * memorandum is model-written and arrives in more than one shape, so nothing
 * may depend on one of them.
 *
 * Deciding is kept apart from drawing because two things draw: the sheet on
 * screen (`markdownHtml.ts`) and the Word export (`documentDocx.ts`). Both ask
 * this module, so a document cannot be one shape on screen and another in the
 * file a reviewer is sent.
 */

/** How one cited procedure renders: a link to its exhibit, or a chip with its result. */
export interface Citation {
  /** The exhibit number it is drawn as in this document, if it is drawn at all. */
  exhibit?: number
  tone?: 'ok' | 'warn' | 'bad' | 'neutral'
  title?: string
}

export interface DocumentOptions {
  /** The cycle's step names: an ordered bold-led list naming exactly these is the process. */
  stepNames?: string[]
  /** Procedure ids a memo may cite, and how each renders. */
  citations?: Record<string, Citation>
}

/**
 * The sentences the report writes into a section nobody wrote. A gap in the
 * document, drawn as one rather than as prose a reader might take for a
 * statement — `report.py` writes the first three; the fourth is the wording
 * reports generated before September 2026 carry.
 */
export const GAP_SENTENCES = [
  'Not stated.',
  'Content to be completed by the auditor.',
  'Not established by the evidence obtained; pending auditor follow-up.',
  'Root cause pending auditor follow-up.',
]
const GAPS = new Set(GAP_SENTENCES.map(text => text.toLowerCase()))

/** The report template's severity vocabulary, most severe first. */
export const SEVERITIES = ['critical', 'high', 'medium', 'low']

export function plainText(node: Nodes): string {
  if ('value' in node && typeof node.value === 'string') return node.value
  if ('children' in node && Array.isArray(node.children)) return (node.children as Nodes[]).map(plainText).join('')
  return ''
}

export function normalised(text: string): string {
  return text.toLowerCase().replace(/\s+/g, ' ').replace(/[\s.:;—–-]+$/, '').trim()
}

/** A paragraph's opening bold run and what follows it, when it opens with one. */
export function boldLead(paragraph: Paragraph): { lead: Strong; rest: PhrasingContent[] } | null {
  const [first, ...rest] = paragraph.children
  if (first?.type !== 'strong' || !plainText(first).trim()) return null
  return { lead: first, rest }
}

/** Drop the dash or colon a writer put between a bold lead and its sentence. */
export function afterSeparator(rest: PhrasingContent[]): PhrasingContent[] {
  const [first, ...others] = rest
  if (first?.type !== 'text') return rest
  const value = first.value.replace(/^\s*(?:[—–]|-{1,2}|:)?\s*/, '')
  return value ? [{ ...first, value }, ...others] : others
}

export interface LedItem { lead: Strong; rest: PhrasingContent[]; others: RootContent[] }

/** Every item of a list, when every one opens with bold text. */
function ledItems(list: List): LedItem[] | null {
  if (list.children.length < 2) return null
  const items: LedItem[] = []
  for (const item of list.children) {
    if (typeof item.checked === 'boolean') return null
    const [first, ...others] = item.children
    if (first?.type !== 'paragraph') return null
    const led = boldLead(first)
    if (!led) return null
    items.push({ lead: led.lead, rest: afterSeparator(led.rest), others: others as RootContent[] })
  }
  return items
}

/** A short label and its value: `Entity: Global Bank`, or `**Entity:** Global Bank`. */
const SHORT_LABEL = /^([^:\n*_`[\]]{1,40}):\s+(\S[\s\S]*)$/

/** The label is plain text; whoever draws it escapes it. */
export function labelled(paragraph: Paragraph): { label: string; value: PhrasingContent[] } | null {
  const [first, ...rest] = paragraph.children
  if (first?.type === 'strong') {
    const label = plainText(first).trim()
    const value = afterSeparator(rest)
    if (!label.endsWith(':') || label.length > 41 || !value.length) return null
    return { label: label.slice(0, -1).trim(), value }
  }
  if (first?.type !== 'text') return null
  const match = SHORT_LABEL.exec(first.value)
  if (!match) return null
  return { label: match[1].trim(), value: [{ ...first, value: match[2] }, ...rest] }
}

export interface LabelledPair { label: string; value: PhrasingContent[]; others: RootContent[] }

export type ListForm =
  | { kind: 'kv'; pairs: LabelledPair[] }
  | { kind: 'register' | 'steps'; start: number; items: LedItem[] }
  | { kind: 'hang'; items: LedItem[] }

/**
 * What a list is on the sheet, or null for an ordinary list.
 *
 * `Label: value` on every item is a definition, whichever bullet it wears. An
 * ordered list whose every item opens in bold is a register — or the process,
 * when the bold leads are the cycle's own step names. The cycle is read out of
 * the very section that holds the list, so that test is equality rather than a
 * guess about what a "process" heading looks like. An unordered bold-led list
 * hangs each name beside what is said about it.
 */
export function listForm(list: List, options: DocumentOptions): ListForm | null {
  if (!list.ordered && list.children.length >= 2) {
    const pairs = list.children.map(item => {
      const [first, ...others] = item.children
      if (first?.type !== 'paragraph' || typeof item.checked === 'boolean') return null
      const pair = labelled(first)
      return pair ? { ...pair, others: others as RootContent[] } : null
    })
    if (pairs.every(Boolean)) return { kind: 'kv', pairs: pairs as LabelledPair[] }
  }
  const items = ledItems(list)
  if (!items) return null
  if (!list.ordered) return { kind: 'hang', items }
  const start = typeof list.start === 'number' ? list.start : 1
  const steps = new Set((options.stepNames ?? []).map(normalised))
  const isProcess = steps.size > 0 && items.every(item => steps.has(normalised(plainText(item.lead))))
  return { kind: isProcess ? 'steps' : 'register', start, items }
}

/** A gap the report left, a run-in head, or ordinary prose. */
export function paragraphForm(paragraph: Paragraph): 'gap' | 'runin' | 'plain' {
  if (GAPS.has(plainText(paragraph).trim().toLowerCase())) return 'gap'
  // A paragraph that opens in bold and goes on: a run-in head. The words stay
  // exactly as written; only the head is set as structure.
  const led = boldLead(paragraph)
  if (led && plainText({ type: 'paragraph', children: led.rest }).trim()) return 'runin'
  return 'plain'
}

/** The preliminary notice the report writes under its title: a band, not a quote. */
export function isPreliminaryQuote(node: Blockquote): boolean {
  const [first] = node.children
  if (first?.type !== 'paragraph') return false
  const led = boldLead(first)
  return Boolean(led && /^preliminary\b/i.test(plainText(led.lead).trim()))
}

const NIL = new Set(['—', '–', '-', 'n/a'])
const NUMERIC = /^[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$/

export function isNil(text: string): boolean { return NIL.has(text.trim().toLowerCase()) }
export function isNumber(text: string): boolean { return NUMERIC.test(text.trim()) }
export function isSeverity(text: string): boolean { return SEVERITIES.includes(text.trim().toLowerCase()) }

/**
 * What each column of a table holds: a severity per row (a Key Findings risk
 * level), a count per severity (the Summary of Findings), figures, or words.
 * A column of figures is right-aligned; dashes do not make a column wordy.
 */
export type ColumnKind = 'severity' | 'count' | 'num' | ''
export function tableShape(table: Table): { headers: string[]; kinds: ColumnKind[]; cell: (row: number, column: number) => string } {
  const [head, ...body] = table.children
  const headers = head ? head.children.map(cell => plainText(cell).trim()) : []
  const cell = (row: number, column: number) => plainText(body[row]?.children[column] ?? { type: 'text', value: '' }).trim()
  const kinds = headers.map((header, column): ColumnKind => {
    if (/^(risk level|risk|severity|rating)$/i.test(header)) return 'severity'
    if (SEVERITIES.includes(header.toLowerCase())) return 'count'
    const values = body.map((_, row) => cell(row, column)).filter(value => value && !isNil(value))
    return values.length && values.every(isNumber) ? 'num' : ''
  })
  return { headers, kinds, cell }
}

/** `3–8, 11`: exhibit numbers as the ranges a reader expects. */
export function exhibitRanges(numbers: number[]): string {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b)
  const parts: string[] = []
  for (let index = 0; index < sorted.length; index += 1) {
    const start = sorted[index]
    while (index + 1 < sorted.length && sorted[index + 1] === sorted[index] + 1) index += 1
    parts.push(start === sorted[index] ? String(start) : `${start}–${sorted[index]}`)
  }
  return parts.join(', ')
}

export function compileCitations(citations: Record<string, Citation> | undefined): RegExp | null {
  const ids = Object.keys(citations ?? {}).filter(Boolean)
  if (!ids.length) return null
  const one = `(?:${ids.sort((a, b) => b.length - a.length).map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`
  // A parenthesis holding nothing but ids is the citation itself; a bare id is
  // cited where it stands. Word boundaries keep `A-1` out of `A-12`.
  return new RegExp(`\\s*\\((${one}(?:\\s*,\\s*${one})*)\\)|(?<![\\w-])${one}(?![\\w-])`, 'g')
}

export type CitationToken =
  | { kind: 'text'; text: string }
  | { kind: 'exhibits'; numbers: number[] }
  | { kind: 'chips'; ids: string[]; spaced: boolean }

/**
 * Procedure ids in prose, as a reader can use them.
 *
 * The memo cites procedures in parentheses — `(A-8BFCE4A3, A-A69AB394)` —
 * because it is plain text. On the sheet a procedure drawn as an exhibit
 * becomes a superscript to that exhibit, and one that is not drawn becomes a
 * chip carrying its result's dot. Ids are matched against the procedures the
 * page holds, never against a pattern, so nothing that merely looks like an
 * id is turned into a link.
 */
export function citationTokens(value: string, citations: Record<string, Citation>, pattern: RegExp): CitationToken[] {
  const out: CitationToken[] = []
  let cursor = 0
  pattern.lastIndex = 0
  for (let match = pattern.exec(value); match; match = pattern.exec(value)) {
    if (match.index > cursor) out.push({ kind: 'text', text: value.slice(cursor, match.index) })
    const ids = (match[1] ?? match[0]).split(/\s*,\s*/).map(id => id.trim())
    const exhibits = ids.map(id => citations[id]?.exhibit)
    if (exhibits.every(number => typeof number === 'number')) out.push({ kind: 'exhibits', numbers: exhibits as number[] })
    else out.push({ kind: 'chips', ids, spaced: Boolean(match[1]) })
    cursor = match.index + match[0].length
  }
  if (cursor < value.length) out.push({ kind: 'text', text: value.slice(cursor) })
  return out
}

/**
 * A document's top-level blocks, with each run of two or more
 * `**Label:** value` paragraphs gathered into one definition list — the
 * report's `Objective` and `Scope`. One on its own is only a run-in head.
 */
export type TopBlock = { kind: 'kv'; pairs: LabelledPair[] } | { kind: 'node'; node: RootContent }
export function topBlocks(nodes: RootContent[]): TopBlock[] {
  const out: TopBlock[] = []
  for (let index = 0; index < nodes.length; index += 1) {
    const pairs: LabelledPair[] = []
    let next = index
    while (next < nodes.length) {
      const candidate = nodes[next]
      const pair = candidate.type === 'paragraph' && candidate.children[0]?.type === 'strong' ? labelled(candidate) : null
      if (!pair) break
      pairs.push({ ...pair, others: [] })
      next += 1
    }
    if (pairs.length >= 2) {
      out.push({ kind: 'kv', pairs })
      index = next - 1
      continue
    }
    out.push({ kind: 'node', node: nodes[index] })
  }
  return out
}
