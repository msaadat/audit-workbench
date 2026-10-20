import type {
  Blockquote, List, ListItem, Nodes, Paragraph, Parents, PhrasingContent, RootContent, Strong, Table,
} from 'mdast'
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import { unified } from 'unified'

/**
 * One Markdown document, rendered to HTML by the parser the editor uses.
 *
 * View mode used to run a hand-rolled line scanner, and the two modes drifted
 * the moment anybody edited a document. Milkdown does not hand back the text
 * it was given — it re-serialises the whole document in its own style — and
 * that style is valid GFM the scanner could not read. A Key Findings table
 * whose `#` column is one character wide serialises its delimiter row as
 * `| - | ... |`, and the scanner required three hyphens; the table stopped
 * being a table and every row after it collapsed into one run-on paragraph.
 * Blockquotes, rules, fences and nested lists were never supported at all.
 *
 * So the parse is no longer this file's opinion. `remark-parse` + `remark-gfm`
 * is exactly what Milkdown parses with, which is what makes "fine in edit,
 * broken in view" structurally impossible rather than merely fixed once.
 *
 * What this file does own is the *serialisation*, because the output goes
 * through `v-html` over text an auditor or a model wrote. Every tag emitted
 * below is written by this module; no tag ever comes from the document. That
 * is the whole XSS argument, and it is an allowlist by construction rather
 * than a filter over attacker-supplied markup.
 */

function escapeHtml(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')
}

/**
 * The links this app will emit: a workspace route, a tab switch, or an
 * explicit web address. Anything else — `javascript:`, `data:`, a bare scheme
 * we have no reason to follow — renders as its own text, so the words survive
 * and the navigation does not.
 */
function safeHref(value: string): string | null {
  const href = value.trim()
  return href.startsWith('?tab=') || href.startsWith('/') || href.startsWith('https://') || href.startsWith('http://') ? href : null
}

/**
 * Milkdown writes an empty paragraph out as a literal `<br />` and strips it
 * back out on parse (its `remark-preserve-empty-line` plugin), so the marker
 * is invisible in the editor and was visible everywhere else. These are the
 * four spellings that plugin recognises; matching it exactly is the point.
 */
const EMPTY_LINE_HTML = new Set(['<br />', '<br>', '<br >', '<br/>'])

/**
 * Raw HTML is shown as text, never parsed.
 *
 * This is not only the safe choice, it is what the editor already does —
 * Milkdown's `html` node sets `textContent` to the source, so `<b>x</b>` reads
 * as those literal characters in edit mode. Dropping the node instead would
 * silently delete content from an audit document, which is a worse failure
 * than showing it plainly.
 */
function rawHtml(value: string): string {
  return EMPTY_LINE_HTML.has(value.trim()) ? '' : escapeHtml(value)
}

/** A list is loose if it or any of its items is spread; tight items shed their paragraph wrapper. */
function isTight(list: List): boolean {
  return !list.spread && !list.children.some(item => item.spread)
}

function alignment(table: Table, column: number): string {
  const align = table.align?.[column]
  return align ? ` style="text-align:${align}"` : ''
}

function children(node: Parents, tight = false): string {
  return node.children.map(child => render(child as RootContent, tight)).join('')
}

function listItem(item: ListItem, tight: boolean): string {
  // A GFM task item carries its own checkbox. It is disabled because this is a
  // rendered document: the checklist is edited in the editor, not here.
  const box = typeof item.checked === 'boolean'
    ? `<input type="checkbox" disabled${item.checked ? ' checked' : ''} /> `
    : ''
  return `<li>${box}${children(item, tight)}</li>`
}

/* =========================================================================
   Document mode.

   The memorandum, the memo and the report are read as documents, and a
   document has forms a README does not: a label beside its value, a numbered
   register, a process drawn as steps, a risk named in a hanging column. Each
   form is decided here from the shape the Markdown already has, and each falls
   back to the plain rendering above when the shape is not quite there — the
   memorandum is model-written and arrives in more than one shape, so nothing
   may depend on one of them.

   Every tag and class below is still a literal written by this module, and
   every piece of document text still passes through `escapeHtml`. Document
   mode changes what the module writes, never where its markup comes from.
   ========================================================================= */

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

export interface RenderOptions {
  /** Render as a written work product on its sheet. Off for chat, notes and narratives. */
  document?: DocumentOptions
}

/** Set for the duration of one synchronous `markdownToHtml` call. */
let documentOptions: DocumentOptions | null = null
let citationPattern: RegExp | null = null

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
const SEVERITIES = ['critical', 'high', 'medium', 'low']

function plainText(node: Nodes): string {
  if ('value' in node && typeof node.value === 'string') return node.value
  if ('children' in node && Array.isArray(node.children)) return (node.children as Nodes[]).map(plainText).join('')
  return ''
}

function normalised(text: string): string {
  return text.toLowerCase().replace(/\s+/g, ' ').replace(/[\s.:;—–-]+$/, '').trim()
}

/** A paragraph's opening bold run and what follows it, when it opens with one. */
function boldLead(paragraph: Paragraph): { lead: Strong; rest: PhrasingContent[] } | null {
  const [first, ...rest] = paragraph.children
  if (first?.type !== 'strong' || !plainText(first).trim()) return null
  return { lead: first, rest }
}

/** Drop the dash or colon a writer put between a bold lead and its sentence. */
function afterSeparator(rest: PhrasingContent[]): PhrasingContent[] {
  const [first, ...others] = rest
  if (first?.type !== 'text') return rest
  const value = first.value.replace(/^\s*(?:[—–]|-{1,2}|:)?\s*/, '')
  return value ? [{ ...first, value }, ...others] : others
}

function phrasing(nodes: PhrasingContent[]): string {
  return nodes.map(node => render(node as RootContent)).join('')
}

/** A bold lead, rendered, without the colon it may end on. */
function leadHtml(lead: Strong): string {
  return children(lead).replace(/\s*:\s*$/, '')
}

interface LedItem { lead: Strong; rest: PhrasingContent[]; others: RootContent[] }

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

function labelled(paragraph: Paragraph): { label: string; value: PhrasingContent[] } | null {
  const [first, ...rest] = paragraph.children
  if (first?.type === 'strong') {
    const label = plainText(first).trim()
    const value = afterSeparator(rest)
    if (!label.endsWith(':') || label.length > 41 || !value.length) return null
    return { label: escapeHtml(label.slice(0, -1).trim()), value }
  }
  if (first?.type !== 'text') return null
  const match = SHORT_LABEL.exec(first.value)
  if (!match) return null
  return { label: escapeHtml(match[1].trim()), value: [{ ...first, value: match[2] }, ...rest] }
}

function definitionList(pairs: Array<{ label: string; value: string }>): string {
  return `<dl class="doc-kv">${pairs.map(pair => `<dt>${pair.label}</dt><dd>${pair.value}</dd>`).join('')}</dl>`
}

function documentList(list: List): string | null {
  // `Label: value` on every item is a definition, whichever bullet it wears.
  if (!list.ordered && list.children.length >= 2) {
    const pairs = list.children.map(item => {
      const [first, ...others] = item.children
      if (first?.type !== 'paragraph' || typeof item.checked === 'boolean') return null
      const pair = labelled(first)
      if (!pair) return null
      return { label: pair.label, value: phrasing(pair.value) + others.map(child => render(child as RootContent)).join('') }
    })
    if (pairs.every(Boolean)) return definitionList(pairs as Array<{ label: string; value: string }>)
  }

  const items = ledItems(list)
  if (!items) return null
  const body = (item: LedItem) => {
    const rest = item.rest.length ? `<p>${phrasing(item.rest)}</p>` : ''
    return rest + item.others.map(child => render(child)).join('')
  }
  if (list.ordered) {
    const start = typeof list.start === 'number' ? list.start : 1
    // The process the memorandum describes, when the list names the cycle's
    // own steps. The cycle is read out of this very section, so the test is
    // equality rather than a guess about what a "process" heading looks like.
    const steps = new Set((documentOptions?.stepNames ?? []).map(normalised))
    const isProcess = steps.size > 0 && items.every(item => steps.has(normalised(plainText(item.lead))))
    const kind = isProcess ? 'doc-steps' : 'doc-register'
    return `<ol class="${kind}">${items.map((item, index) =>
      `<li><span class="doc-k" aria-hidden="true">${start + index}</span><div class="doc-item"><p class="doc-lead">${leadHtml(item.lead)}</p>${body(item)}</div></li>`,
    ).join('')}</ol>`
  }
  return `<ul class="doc-hang">${items.map(item =>
    `<li><div class="doc-lab">${leadHtml(item.lead)}</div><div class="doc-item">${body(item)}</div></li>`,
  ).join('')}</ul>`
}

function documentParagraph(paragraph: Paragraph): string {
  if (GAPS.has(plainText(paragraph).trim().toLowerCase())) {
    return `<p class="doc-gap">${children(paragraph)}</p>`
  }
  // A paragraph that opens in bold and goes on: a run-in head. The words stay
  // exactly as written; only the head is set as structure.
  const led = boldLead(paragraph)
  if (led && plainText({ type: 'paragraph', children: led.rest }).trim()) {
    return `<p class="doc-runin">${children(paragraph)}</p>`
  }
  return `<p>${children(paragraph)}</p>`
}

function documentBlockquote(node: Blockquote): string | null {
  const [first] = node.children
  if (first?.type !== 'paragraph') return null
  const led = boldLead(first)
  if (!led || !/^preliminary\b/i.test(plainText(led.lead).trim())) return null
  return `<div class="doc-band" role="note">${children(node)}</div>`
}

const NIL = new Set(['—', '–', '-', 'n/a'])
const NUMERIC = /^[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$/

function documentTable(table: Table): string {
  const [head, ...body] = table.children
  const headers = head ? head.children.map(cell => plainText(cell).trim()) : []
  const cellText = (row: number, column: number) => plainText(body[row]?.children[column] ?? { type: 'text', value: '' }).trim()
  const kinds = headers.map((header, column) => {
    if (/^(risk level|risk|severity|rating)$/i.test(header)) return 'severity'
    if (SEVERITIES.includes(header.toLowerCase())) return 'count'
    const values = body.map((_, row) => cellText(row, column)).filter(value => value && !NIL.has(value.toLowerCase()))
    return values.length && values.every(value => NUMERIC.test(value)) ? 'num' : ''
  })
  const classFor = (column: number, extra = '') => {
    const names = [kinds[column] === 'num' || kinds[column] === 'count' ? kinds[column] : '', extra].filter(Boolean)
    return names.length ? ` class="${names.join(' ')}"` : ''
  }
  const headCells = head
    ? head.children.map((cell, column) => `<th${classFor(column)}${alignment(table, column)}>${children(cell)}</th>`).join('')
    : ''
  const rows = body.map((row, rowIndex) => `<tr>${row.children.map((cell, column) => {
    const text = cellText(rowIndex, column)
    if (NIL.has(text.toLowerCase())) return `<td${classFor(column, 'nil')}${alignment(table, column)}>${children(cell)}</td>`
    if (kinds[column] === 'severity' && SEVERITIES.includes(text.toLowerCase())) {
      return `<td${alignment(table, column)}><span class="aw-severity" data-sev="${text.toLowerCase()}">${children(cell)}</span></td>`
    }
    if (kinds[column] === 'count' && NUMERIC.test(text)) {
      const severity = headers[column].toLowerCase()
      const zero = Number(text.replace(/,/g, '')) === 0
      return `<td${classFor(column, zero ? 'zero' : 'sev-count')}${zero ? '' : ` data-sev="${severity}"`}${alignment(table, column)}>${children(cell)}</td>`
    }
    return `<td${classFor(column)}${alignment(table, column)}>${children(cell)}</td>`
  }).join('')}</tr>`).join('')
  return `<div class="table-scroll"><table class="doc-table"><thead><tr>${headCells}</tr></thead><tbody>${rows}</tbody></table></div>`
}

/** `Ex. 3–8, 11`: exhibit numbers as the ranges a reader expects. */
function exhibitRanges(numbers: number[]): string {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b)
  const parts: string[] = []
  for (let index = 0; index < sorted.length; index += 1) {
    const start = sorted[index]
    while (index + 1 < sorted.length && sorted[index + 1] === sorted[index] + 1) index += 1
    parts.push(start === sorted[index] ? String(start) : `${start}–${sorted[index]}`)
  }
  return parts.join(', ')
}

function citationChip(id: string, citation: Citation): string {
  const tone = citation.tone ?? 'neutral'
  const title = citation.title ? ` title="${escapeHtml(citation.title)}"` : ''
  return `<a class="doc-cite" href="#" data-procedure="${escapeHtml(id)}" data-tone="${tone}"${title}><i aria-hidden="true"></i>${escapeHtml(id)}</a>`
}

function exhibitLink(numbers: number[]): string {
  return `<sup class="doc-xref"><a href="#exhibit-${numbers[0]}" data-jump="exhibit-${numbers[0]}">Ex. ${exhibitRanges(numbers)}</a></sup>`
}

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
function citedText(value: string): string {
  const citations = documentOptions?.citations
  if (!citations || !citationPattern) return escapeHtml(value)
  let out = ''
  let cursor = 0
  citationPattern.lastIndex = 0
  for (let match = citationPattern.exec(value); match; match = citationPattern.exec(value)) {
    out += escapeHtml(value.slice(cursor, match.index))
    const ids = (match[1] ?? match[0]).split(/\s*,\s*/).map(id => id.trim())
    const exhibits = ids.map(id => citations[id]?.exhibit)
    if (exhibits.every(number => typeof number === 'number')) {
      out += exhibitLink(exhibits as number[])
    } else {
      out += (match[1] ? ' ' : '') + ids.map(id => citationChip(id, citations[id] ?? {})).join(' ')
    }
    cursor = match.index + match[0].length
  }
  return out + escapeHtml(value.slice(cursor))
}

function compileCitations(citations: Record<string, Citation> | undefined): RegExp | null {
  const ids = Object.keys(citations ?? {}).filter(Boolean)
  if (!ids.length) return null
  const one = `(?:${ids.sort((a, b) => b.length - a.length).map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`
  // A parenthesis holding nothing but ids is the citation itself; a bare id is
  // cited where it stands. Word boundaries keep `A-1` out of `A-12`.
  return new RegExp(`\\s*\\((${one}(?:\\s*,\\s*${one})*)\\)|(?<![\\w-])${one}(?![\\w-])`, 'g')
}

/**
 * A run of `**Label:** value` paragraphs is a definition list — the report's
 * `Objective` and `Scope`. One on its own is only a run-in head.
 */
function documentChildren(node: Parents): string {
  const out: string[] = []
  const nodes = node.children as RootContent[]
  for (let index = 0; index < nodes.length; index += 1) {
    const pairs: Array<{ label: string; value: string }> = []
    let next = index
    while (next < nodes.length) {
      const candidate = nodes[next]
      const pair = candidate.type === 'paragraph' && candidate.children[0]?.type === 'strong' ? labelled(candidate) : null
      if (!pair) break
      pairs.push({ label: pair.label, value: phrasing(pair.value) })
      next += 1
    }
    if (pairs.length >= 2) {
      out.push(definitionList(pairs))
      index = next - 1
      continue
    }
    out.push(render(nodes[index]))
  }
  return out.join('')
}

function render(node: RootContent, tight = false): string {
  switch (node.type) {
    case 'text':
      return documentOptions ? citedText(node.value) : escapeHtml(node.value)
    case 'paragraph':
      // Inside a tight list the paragraph is structural only — wrapping it
      // would put a margin on every bullet.
      if (tight) return children(node)
      return documentOptions ? documentParagraph(node) : `<p>${children(node)}</p>`
    case 'heading':
      return `<h${node.depth}>${children(node)}</h${node.depth}>`
    case 'strong':
      return `<strong>${children(node)}</strong>`
    case 'emphasis':
      return `<em>${children(node)}</em>`
    case 'delete':
      return `<del>${children(node)}</del>`
    case 'inlineCode':
      return `<code>${escapeHtml(node.value)}</code>`
    case 'code':
      return `<pre><code>${escapeHtml(node.value)}</code></pre>`
    case 'blockquote':
      return (documentOptions && documentBlockquote(node)) || `<blockquote>${children(node)}</blockquote>`
    case 'thematicBreak':
      return '<hr />'
    case 'break':
      return '<br />'
    case 'list': {
      const shaped = documentOptions ? documentList(node) : null
      if (shaped) return shaped
      const tag = node.ordered ? 'ol' : 'ul'
      // `start` is carried so a numbered list that resumes at 4 still says 4.
      const start = node.ordered && typeof node.start === 'number' && node.start !== 1 ? ` start="${node.start}"` : ''
      const items = node.children.map(item => listItem(item, isTight(node))).join('')
      return `<${tag}${start}>${items}</${tag}>`
    }
    case 'listItem':
      return listItem(node, tight)
    case 'link': {
      const href = safeHref(node.url)
      return href ? `<a href="${escapeHtml(href)}">${children(node)}</a>` : children(node)
    }
    case 'image': {
      const src = safeHref(node.url)
      const alt = escapeHtml(node.alt ?? '')
      return src ? `<img src="${escapeHtml(src)}" alt="${alt}" />` : alt
    }
    case 'table': {
      if (documentOptions) return documentTable(node)
      const [head, ...body] = node.children
      const headCells = head
        ? head.children.map((cell, column) => `<th${alignment(node, column)}>${children(cell)}</th>`).join('')
        : ''
      const rows = body
        .map(row => `<tr>${row.children.map((cell, column) => `<td${alignment(node, column)}>${children(cell)}</td>`).join('')}</tr>`)
        .join('')
      // Wrapped so a table too wide for the column scrolls on its own rather
      // than pushing the whole document sideways. A Key Findings table is five
      // columns of prose and a rail beside it; there is not always room.
      return `<div class="table-scroll"><table><thead><tr>${headCells}</tr></thead><tbody>${rows}</tbody></table></div>`
    }
    case 'html':
      return rawHtml(node.value)
    case 'footnoteReference':
      return `<sup><a href="#fn-${escapeHtml(node.identifier)}">${escapeHtml(node.label ?? node.identifier)}</a></sup>`
    case 'footnoteDefinition':
      return `<section class="footnote" id="fn-${escapeHtml(node.identifier)}">${children(node)}</section>`
    case 'definition':
      // A link reference definition is addressing, not content. It renders as
      // nothing, which is what every Markdown renderer does with it.
      return ''
    default: {
      /**
       * Anything the parser produces that has no case above. Recursing into
       * children — or escaping a literal's value — keeps the words on the page
       * instead of dropping a paragraph nobody would notice was missing. An
       * unstyled fallback is a cosmetic bug; silent loss in an audit report is
       * not.
       */
      const unknown = node as Nodes
      if ('children' in unknown && Array.isArray(unknown.children)) return children(unknown as Parents, tight)
      return 'value' in unknown && typeof unknown.value === 'string' ? escapeHtml(unknown.value) : ''
    }
  }
}

/**
 * Parsing is synchronous on purpose: this is read from a `computed`, and a
 * document that arrives one tick late makes the page jump on every keystroke
 * in the reconcile view. `.parse()` applies remark-gfm's micromark extensions
 * without running the transformer pipeline, which is all a renderer needs.
 */
const parser = unified().use(remarkParse).use(remarkGfm)

export function markdownToHtml(markdown: string, options: RenderOptions = {}): string {
  if (!markdown) return ''
  const tree = parser.parse(markdown)
  if (!options.document) return children(tree)
  documentOptions = options.document
  citationPattern = compileCitations(options.document.citations)
  try {
    return documentChildren(tree)
  } finally {
    documentOptions = null
    citationPattern = null
  }
}
