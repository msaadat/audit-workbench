import type { List, ListItem, Nodes, Parents, RootContent, Table } from 'mdast'
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

function render(node: RootContent, tight = false): string {
  switch (node.type) {
    case 'text':
      return escapeHtml(node.value)
    case 'paragraph':
      // Inside a tight list the paragraph is structural only — wrapping it
      // would put a margin on every bullet.
      return tight ? children(node) : `<p>${children(node)}</p>`
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
      return `<blockquote>${children(node)}</blockquote>`
    case 'thematicBreak':
      return '<hr />'
    case 'break':
      return '<br />'
    case 'list': {
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

export function markdownToHtml(markdown: string): string {
  if (!markdown) return ''
  return children(parser.parse(markdown))
}
