import {
  AlignmentType, BorderStyle, Document, ExternalHyperlink, Footer, Header, HeadingLevel, HeightRule,
  LevelFormat, Packer, PageNumber, Paragraph, ShadingType, Tab, Table, TableCell, TableLayoutType,
  TableRow, TabStopType, TextRun, VerticalAlignTable, WidthType,
} from 'docx'
import type { IBorderOptions, IParagraphOptions, IRunOptions, ParagraphChild, TableVerticalAlign } from 'docx'
import type { List, PhrasingContent, RootContent, Strong, Table as MdTable } from 'mdast'

import { documentLayout } from './documentUnits'
import type { DocumentUnit } from './documentUnits'
import type { DocumentCover, SheetLine } from './documentSheet'
import { parseMarkdown } from './markdownHtml'
import {
  citationTokens, compileCitations, exhibitRanges, isNil, isNumber, isPreliminaryQuote, isSeverity,
  listForm, paragraphForm, plainText, tableShape, topBlocks,
} from './markdownForms'
import type { DocumentOptions, LabelledPair, LedItem } from './markdownForms'
import { markdownOutline } from './markdownOutline'
import type { OutlineEntry } from './markdownOutline'

/**
 * A written work product as a Word document, drawn the way the sheet draws it.
 *
 * The forms come from `markdownForms.ts` and the units from `documentUnits.ts`
 * — the same two modules the sheet reads — so the file a reviewer is sent and
 * the page the auditor read are the same document. What differs is only what
 * Word can draw: a pill is a shaded run, the steps' connecting line is a
 * rule down the cell, an exhibit set is every exhibit expanded, as in print.
 * The page's own remarks (quality issues, generation notes) are not part of
 * the document and are not written.
 *
 * This module is loaded only when someone exports, so `docx` never reaches
 * the bundle the app starts with.
 */

/* --- The light theme's values -------------------------------------------
   Word reads no CSS, so these are `style.css`'s light tokens restated, the
   way the print stylesheet prints light whatever the screen shows. */
export const INK = '0D2340'
export const INK_STRONG = '07162B'
export const INK_SOFT = '46576D'
export const MUTED = '5A6A81'
export const TEAL = '0F766E'
const TEAL_LINE = 'A7DED8'
export const BORDER = 'DCE5EE'
export const RAISED = 'EEF2F7'
const PANEL_WASH = 'F7F9FC'
const ACCENT = '7C3AED'
const WARN_INK = '8A4308'
const WARN_SOFT = 'FDF1E3'
const WARN_LINE = 'F0CF9F'
export const DANGER = 'B42318'
export const DANGER_INK = '7F1D1D'
export const DANGER_SOFT = 'FDECEA'
const LOW = 'EAB308'
const LOW_INK = '854D0E'
const LOW_SOFT = 'FEFCE8'
const OK = '147D55'
const OK_SOFT = 'E5F6EE'

/** Fonts every copy of Office has, so the file reads the same on any desk. */
export const SERIF = 'Georgia'
export const SANS = 'Calibri'
export const MONO = 'Consolas'

/** Severity → the fill and text a severity pill or count cell takes. */
export const SEVERITY_FILL: Record<string, { fill: string; text: string }> = {
  critical: { fill: DANGER, text: 'FFFFFF' },
  high: { fill: DANGER_SOFT, text: DANGER_INK },
  medium: { fill: LOW_SOFT, text: LOW_INK },
  low: { fill: OK_SOFT, text: OK },
}
/** The solid colour of each severity, as the report rail's risk bar draws it. */
const SEVERITY_SOLID: Record<string, string> = { critical: DANGER, high: DANGER_INK, medium: LOW, low: OK }
/** Result tone → the fill and text of a result chip or a found-count pill. */
export const TONE_FILL: Record<string, { fill: string; text: string }> = {
  bad: { fill: DANGER_SOFT, text: DANGER_INK },
  warn: { fill: WARN_SOFT, text: WARN_INK },
  ok: { fill: OK_SOFT, text: OK },
  neutral: { fill: RAISED, text: INK_SOFT },
}
const TONE_DOT: Record<string, string> = { bad: DANGER, warn: 'B45309', ok: OK, neutral: 'C5D2E0' }

/* --- The page ------------------------------------------------------------ */
const A4_WIDTH = 11906
const A4_HEIGHT = 16838
const MARGIN = 1418
/** The text column, in twips; the section numbers hang into the left margin. */
export const TEXT_WIDTH = A4_WIDTH - 2 * MARGIN
const HANG = 567

export type Block = Paragraph | Table

export interface WordSheet {
  cover: DocumentCover | null
  runningHead?: SheetLine | null
  footer?: SheetLine | null
  endMark?: string
}

/** What a page adds to its document — the Word side of the slots it fills on screen. */
export type WordInsert =
  | {
      kind: 'rating'
      value: string
      note?: string
      scale: Array<{ label: string; on: boolean; tone: string }>
      figures: Array<{ label: string; value: string }>
    }
  | { kind: 'severity-bar'; counts: Array<{ severity: string; count: number }> }

export interface WordInserts {
  afterHeading?: (entry: OutlineEntry) => WordInsert[]
  afterBody?: (entry: OutlineEntry, body: string) => WordInsert[]
}

type RunStyle = Omit<IRunOptions, 'text' | 'children'>

export interface WordContext {
  options: DocumentOptions
  pattern: RegExp | null
  /** The running text's face, size and colour where this content sits. */
  base: RunStyle
  /** The width a table here may take, in twips. */
  width: number
  /** Restarts each ordered list's numbering. */
  ordered: { next: number }
}

export function wordContext(options: DocumentOptions = {}): WordContext {
  return {
    options,
    pattern: compileCitations(options.citations),
    base: { font: SERIF, size: 22, color: INK },
    width: TEXT_WIDTH,
    ordered: { next: 1 },
  }
}

/* --- Runs and paragraphs ---------------------------------------------------- */

export function run(text: string, style: RunStyle = {}): TextRun {
  return new TextRun({ text, ...style })
}

const shade = (fill: string) => ({ type: ShadingType.CLEAR, color: 'auto', fill })
export const line = (size: number, color: string, style: IBorderOptions['style'] = BorderStyle.SINGLE, space = 0): IBorderOptions =>
  ({ style, size, color, space })
const NONE: IBorderOptions = { style: BorderStyle.NONE, size: 0, color: 'FFFFFF' }

export function para(children: ParagraphChild[], options: Omit<IParagraphOptions, 'children'> = {}): Paragraph {
  return new Paragraph({ spacing: { after: 120, line: 288 }, ...options, children })
}

const EMPTY_LINE = new Set(['<br />', '<br>', '<br >', '<br/>'])

function textRuns(value: string, style: RunStyle, ctx: WordContext): ParagraphChild[] {
  const citations = ctx.options.citations
  if (!citations || !ctx.pattern) return [run(value, style)]
  return citationTokens(value, citations, ctx.pattern).flatMap((token): ParagraphChild[] => {
    if (token.kind === 'text') return [run(token.text, style)]
    if (token.kind === 'exhibits') {
      return [run(`Ex. ${exhibitRanges(token.numbers)}`, { font: SANS, size: 15, bold: true, color: TEAL, superScript: true })]
    }
    const out: ParagraphChild[] = token.spaced ? [run(' ', style)] : []
    token.ids.forEach((id, index) => {
      if (index) out.push(run(' ', style))
      out.push(run('● ', { font: SANS, size: 12, color: TONE_DOT[citations[id]?.tone ?? 'neutral'] }))
      out.push(run(id, { font: MONO, size: 16, color: INK }))
    })
    return out
  })
}

/** Inline Markdown as runs, carrying bold, italic and the rest down the tree. */
export function inline(nodes: PhrasingContent[], style: RunStyle, ctx: WordContext): ParagraphChild[] {
  const out: ParagraphChild[] = []
  for (const node of nodes) {
    switch (node.type) {
      case 'text': out.push(...textRuns(node.value, style, ctx)); break
      case 'strong': out.push(...inline(node.children, { ...style, bold: true }, ctx)); break
      case 'emphasis': out.push(...inline(node.children, { ...style, italics: true }, ctx)); break
      case 'delete': out.push(...inline(node.children, { ...style, strike: true }, ctx)); break
      case 'inlineCode': out.push(run(node.value, { ...style, font: MONO, size: Math.max(14, Number(style.size ?? 22) - 3), shading: shade(RAISED) })); break
      case 'break': out.push(new TextRun({ break: 1 })); break
      case 'link': {
        const kids = inline(node.children, { ...style, color: TEAL, underline: {} }, ctx)
        // An app route means nothing outside the app; only a web address links.
        if (/^https?:\/\//.test(node.url)) out.push(new ExternalHyperlink({ link: node.url, children: kids }))
        else out.push(...inline(node.children, style, ctx))
        break
      }
      case 'html': if (!EMPTY_LINE.has(node.value.trim())) out.push(run(node.value, style)); break
      case 'image': out.push(run(node.alt ?? '', style)); break
      case 'footnoteReference': out.push(run(node.label ?? node.identifier, { ...style, superScript: true })); break
      default: out.push(run(plainText(node), style))
    }
  }
  return out
}

/** A bold lead without the colon it may end on. */
function leadNodes(lead: Strong): PhrasingContent[] {
  const nodes = [...lead.children]
  const last = nodes[nodes.length - 1]
  if (last?.type === 'text') nodes[nodes.length - 1] = { ...last, value: last.value.replace(/\s*:\s*$/, '') }
  return nodes
}

/* --- Tables ------------------------------------------------------------------ */

const CELL_MARGINS = { top: 70, bottom: 70, left: 90, right: 90 }

export function cell(children: Block[], width: number, options: {
  fill?: string
  borders?: Partial<Record<'top' | 'bottom' | 'left' | 'right', IBorderOptions>>
  margins?: { top?: number; bottom?: number; left?: number; right?: number }
  align?: TableVerticalAlign
} = {}): TableCell {
  // Word wants every cell to end on a paragraph, including one that ends on a table.
  const content = children.length && children[children.length - 1] instanceof Paragraph
    ? children
    : [...children, para([], { spacing: { after: 0 } })]
  return new TableCell({
    children: content,
    width: { size: width, type: WidthType.DXA },
    shading: options.fill ? shade(options.fill) : undefined,
    borders: options.borders,
    margins: { ...CELL_MARGINS, ...options.margins },
    verticalAlign: options.align,
  })
}

/** A table ruled as the sheet rules one: lines across, none between columns. */
export function ruledTable(rows: TableRow[], widths: number[], rules: {
  top?: IBorderOptions; bottom?: IBorderOptions; inside?: IBorderOptions; sides?: IBorderOptions
} = {}): Table {
  return new Table({
    rows,
    width: { size: widths.reduce((sum, width) => sum + width, 0), type: WidthType.DXA },
    columnWidths: widths,
    layout: TableLayoutType.FIXED,
    borders: {
      top: rules.top ?? NONE,
      bottom: rules.bottom ?? NONE,
      left: rules.sides ?? NONE,
      right: rules.sides ?? NONE,
      insideHorizontal: rules.inside ?? NONE,
      insideVertical: NONE,
    },
  })
}

/** Column widths shared out by how much each column holds, within `total`. */
export function shareWidths(lengths: number[], total: number): number[] {
  const weights = lengths.map(length => Math.min(48, Math.max(5, length)))
  const sum = weights.reduce((a, b) => a + b, 0) || 1
  const widths = weights.map(weight => Math.floor((total * weight) / sum))
  widths[widths.length - 1] += total - widths.reduce((a, b) => a + b, 0)
  return widths
}

const HAIRLINE = line(4, BORDER)
const RULE = line(12, INK)

function docTable(table: MdTable, ctx: WordContext): Block[] {
  const [head, ...body] = table.children
  if (!head) return []
  const { headers, kinds, cell: text } = tableShape(table)
  const columns = head.children.length
  const lengths = Array.from({ length: columns }, (_, column) =>
    Math.max(headers[column]?.length ?? 0, ...body.map((_, row) => text(row, column).length)))
  const widths = shareWidths(lengths, ctx.width)
  const alignOf = (column: number) => {
    const explicit = table.align?.[column]
    if (explicit === 'right') return AlignmentType.RIGHT
    if (explicit === 'center') return AlignmentType.CENTER
    if (explicit === 'left') return AlignmentType.LEFT
    if (kinds[column] === 'num') return AlignmentType.RIGHT
    if (kinds[column] === 'count') return AlignmentType.CENTER
    return AlignmentType.LEFT
  }
  const base: RunStyle = { font: SANS, size: 18, color: INK }
  const headRow = new TableRow({
    tableHeader: true,
    children: head.children.map((node, column) => cell(
      [para(inline(node.children, { font: SANS, size: 14, bold: true, color: INK_SOFT, allCaps: true }, ctx), { alignment: alignOf(column), spacing: { after: 0 } })],
      widths[column],
      { borders: { bottom: line(6, INK) }, align: VerticalAlignTable.BOTTOM },
    )),
  })
  const bodyRows = body.map((row, rowIndex) => new TableRow({
    cantSplit: true,
    children: Array.from({ length: columns }, (_, column) => {
      const node = row.children[column]
      const value = text(rowIndex, column)
      const children = node?.children ?? []
      let style: RunStyle = base
      let fill: string | undefined
      if (isNil(value)) style = { ...base, color: MUTED }
      else if (kinds[column] === 'severity' && isSeverity(value)) {
        const severity = SEVERITY_FILL[value.toLowerCase()]
        style = { ...base, bold: true, color: severity.text }
        fill = severity.fill
      } else if (kinds[column] === 'count' && isNumber(value)) {
        const zero = Number(value.replace(/,/g, '')) === 0
        const severity = SEVERITY_FILL[headers[column].toLowerCase()]
        style = zero ? { ...base, color: MUTED } : { ...base, bold: true, color: severity.text }
        fill = zero ? undefined : severity.fill
      }
      return cell([para(inline(children, style, ctx), { alignment: alignOf(column), spacing: { after: 0 } })], widths[column], { fill })
    }),
  }))
  return [ruledTable([headRow, ...bodyRows], widths, { top: RULE, bottom: RULE, inside: HAIRLINE }), spacer()]
}

export function spacer(after = 160): Paragraph {
  return para([], { spacing: { after, line: 120 } })
}

/* --- The forms a document takes ------------------------------------------------ */

function kvTable(pairs: LabelledPair[], ctx: WordContext): Block[] {
  const widths = [2268, ctx.width - 2268]
  const rows = pairs.map(pair => new TableRow({
    cantSplit: true,
    children: [
      cell([para([run(pair.label, { font: SANS, size: 17, bold: true, color: INK_SOFT })], { spacing: { after: 0 } })], widths[0], { margins: { left: 0 } }),
      cell([para(inline(pair.value, ctx.base, ctx), { spacing: { after: 60, line: 288 } }), ...blocks(pair.others, { ...ctx, width: widths[1] - 180 })], widths[1]),
    ],
  }))
  return [ruledTable(rows, widths, { top: HAIRLINE, bottom: HAIRLINE, inside: HAIRLINE }), spacer()]
}

function itemBody(item: LedItem, ctx: WordContext): Block[] {
  const rest = item.rest.length ? [para(inline(item.rest, ctx.base, ctx), { spacing: { after: 60, line: 288 } })] : []
  return [...rest, ...blocks(item.others, ctx)]
}

function registerTable(items: LedItem[], start: number, steps: boolean, ctx: WordContext): Block[] {
  const widths = [steps ? 680 : 794, ctx.width - (steps ? 680 : 794)]
  const inner = { ...ctx, width: widths[1] - 180 }
  const rows = items.map((item, index) => new TableRow({
    cantSplit: true,
    children: [
      cell(
        [para([run(String(start + index), steps
          ? { font: SANS, size: 20, bold: true, color: TEAL }
          : { font: SERIF, size: 36, color: TEAL })], { spacing: { after: 0 } })],
        widths[0],
        { margins: { left: 0 } },
      ),
      cell(
        [para(inline(leadNodes(item.lead), { font: SANS, size: 22, bold: true, color: INK_STRONG }, ctx), { spacing: { after: 40 }, keepNext: true }), ...itemBody(item, inner)],
        widths[1],
        // The process's steps hang off one rule, as the sheet draws them.
        steps ? { borders: { left: line(12, TEAL_LINE) }, margins: { left: 220, bottom: 160 } } : {},
      ),
    ],
  }))
  return [steps ? ruledTable(rows, widths) : ruledTable(rows, widths, { top: HAIRLINE, bottom: HAIRLINE, inside: HAIRLINE }), spacer()]
}

function hangTable(items: LedItem[], ctx: WordContext): Block[] {
  const widths = [2835, ctx.width - 2835]
  const inner = { ...ctx, width: widths[1] - 180 }
  const rows = items.map(item => new TableRow({
    cantSplit: true,
    children: [
      cell([para(inline(leadNodes(item.lead), { font: SANS, size: 19, bold: true, color: INK_STRONG }, ctx), { spacing: { after: 0, line: 264 } })], widths[0], { margins: { left: 0 } }),
      cell(itemBody(item, inner).length ? itemBody(item, inner) : [para([])], widths[1]),
    ],
  }))
  return [ruledTable(rows, widths, { top: HAIRLINE, bottom: HAIRLINE, inside: HAIRLINE }), spacer()]
}

function plainList(list: List, ctx: WordContext, depth: number): Block[] {
  const instance = list.ordered ? ctx.ordered.next++ : 0
  const out: Block[] = []
  for (const item of list.children) {
    const [first, ...rest] = item.children
    const box = typeof item.checked === 'boolean' ? [run(item.checked ? '☑ ' : '☐ ', ctx.base)] : []
    const marker = list.ordered
      ? { numbering: { reference: 'doc-ordered', level: Math.min(depth, 3), instance } }
      : { bullet: { level: Math.min(depth, 3) } }
    const lead = first?.type === 'paragraph' ? inline(first.children, ctx.base, ctx) : []
    out.push(para([...box, ...lead], { ...marker, spacing: { after: 60, line: 288 } }))
    const others = first?.type === 'paragraph' ? rest : item.children
    for (const child of others as RootContent[]) {
      if (child.type === 'list') out.push(...listBlocks(child, ctx, depth + 1))
      else if (child.type === 'paragraph') out.push(para(inline(child.children, ctx.base, ctx), { indent: { left: 360 * (depth + 2) } }))
      else out.push(...nodeBlocks(child, ctx))
    }
  }
  return out
}

function listBlocks(list: List, ctx: WordContext, depth = 0): Block[] {
  const form = listForm(list, ctx.options)
  if (!form) return [...plainList(list, ctx, depth), ...(depth ? [] : [spacer(60)])]
  if (form.kind === 'kv') return kvTable(form.pairs, ctx)
  if (form.kind === 'hang') return hangTable(form.items, ctx)
  return registerTable(form.items, form.start, form.kind === 'steps', ctx)
}

function nodeBlocks(node: RootContent, ctx: WordContext): Block[] {
  switch (node.type) {
    case 'paragraph': {
      const form = paragraphForm(node)
      if (form === 'gap') {
        // A section nobody has written yet, drawn as the gap it is.
        const dashed = line(6, WARN_LINE, BorderStyle.DASHED, 4)
        return [para(inline(node.children, { font: SANS, size: 19, color: WARN_INK, italics: false }, ctx), {
          shading: shade(WARN_SOFT), border: { top: dashed, bottom: dashed, left: dashed, right: dashed },
          spacing: { before: 60, after: 160 },
        })]
      }
      if (form === 'runin') {
        const [lead, ...rest] = node.children
        return [para([
          ...inline([lead], { ...ctx.base, font: SANS, size: Number(ctx.base.size ?? 22) - 2, bold: true, color: INK_STRONG }, ctx),
          ...inline(rest, ctx.base, ctx),
        ])]
      }
      return [para(inline(node.children, ctx.base, ctx))]
    }
    case 'heading':
      return [para(inline(node.children, { ...ctx.base, bold: true, color: INK_STRONG }, ctx), { keepNext: true })]
    case 'list':
      return listBlocks(node, ctx)
    case 'blockquote': {
      if (isPreliminaryQuote(node)) {
        // The preliminary notice the report writes: a band, not a quote.
        return node.children.map(child => para(
          child.type === 'paragraph' ? inline(child.children, { font: SANS, size: 19, color: WARN_INK }, ctx) : [run(plainText(child), { font: SANS, size: 19, color: WARN_INK })],
          { shading: shade(WARN_SOFT), spacing: { before: 120, after: 200, line: 276 }, indent: { left: 120, right: 120 } },
        ))
      }
      return node.children.flatMap(child => child.type === 'paragraph'
        ? [para(inline(child.children, { ...ctx.base, color: INK_SOFT }, ctx), { indent: { left: 360 }, border: { left: line(12, TEAL_LINE, BorderStyle.SINGLE, 10) } })]
        : nodeBlocks(child, ctx))
    }
    case 'code': {
      const lines = node.value.split('\n')
      return lines.map((text, index) => para([run(text || ' ', { font: MONO, size: 17, color: INK })], {
        shading: shade(RAISED), spacing: { after: index === lines.length - 1 ? 160 : 0, line: 240 },
      }))
    }
    case 'thematicBreak':
      return [para([], { border: { bottom: HAIRLINE }, spacing: { after: 200 } })]
    case 'table':
      return docTable(node, ctx)
    case 'html':
      return EMPTY_LINE.has(node.value.trim()) ? [] : [para([run(node.value, ctx.base)])]
    case 'definition':
      return []
    default:
      return [para([run(plainText(node), ctx.base)])]
  }
}

export function blocks(nodes: RootContent[], ctx: WordContext, top = false): Block[] {
  if (!top) return nodes.flatMap(node => nodeBlocks(node, ctx))
  return topBlocks(nodes).flatMap(block => block.kind === 'kv' ? kvTable(block.pairs, ctx) : nodeBlocks(block.node, ctx))
}

/** One Markdown body, as the blocks the sheet would draw for it. */
export function markdownBlocksWord(markdown: string, ctx: WordContext): Block[] {
  return markdown.trim() ? blocks(parseMarkdown(markdown).children as RootContent[], ctx, true) : []
}

/* --- Headings, cover, furniture ------------------------------------------------- */

/** One heading, numbered as the outline numbers it, the number hung in the margin. */
export function headingBlocks(entry: OutlineEntry, parts: boolean): Paragraph[] {
  const label = entry.label ?? entry.text
  if (entry.part) {
    return [
      para([run(`Part ${entry.number}`, { font: SANS, size: 18, bold: true, color: TEAL })], {
        border: { top: line(12, INK, BorderStyle.SINGLE, 8) }, spacing: { before: 600, after: 40 }, keepNext: true,
      }),
      new Paragraph({
        heading: HeadingLevel.HEADING_1,
        children: [run(label, { font: SERIF, size: 44, color: INK_STRONG })],
        spacing: { before: 0, after: 240 }, keepNext: true,
      }),
    ]
  }
  const level = entry.level
  const section = level === 2 || (level === 3 && parts)
  const look: RunStyle = section
    ? { font: SERIF, size: level === 2 ? 32 : 30, color: INK_STRONG }
    : level === 3 ? { font: SANS, size: 23, bold: true, color: INK_STRONG } : { font: SANS, size: 20, bold: true, color: INK_SOFT }
  const children: ParagraphChild[] = entry.number
    ? [
        run(entry.number, { font: SANS, size: section ? 21 : 18, bold: true, color: TEAL }),
        new TextRun({ children: [new Tab()] }),
        run(label, look),
      ]
    : [run(label, look)]
  return [new Paragraph({
    heading: level === 2 ? HeadingLevel.HEADING_1 : level === 3 ? HeadingLevel.HEADING_2 : HeadingLevel.HEADING_3,
    children,
    // A hanging indent puts the number in the margin and the title on the
    // text's own edge, with Word's implicit tab stop between them.
    indent: entry.number ? { left: 0, hanging: HANG } : undefined,
    spacing: { before: section ? 480 : 300, after: 140 },
    keepNext: true, keepLines: true,
  })]
}

const TAG_STYLE: Record<string, { fill: string; text: string }> = {
  warn: { fill: WARN_SOFT, text: WARN_INK },
  ok: { fill: OK_SOFT, text: OK },
  neutral: { fill: RAISED, text: INK_SOFT },
}
const FIELD_TONE: Record<string, string> = { ok: OK, warn: WARN_INK, agent: ACCENT }

/** The cover: what the document is, its title, and its document control. */
export function coverBlocks(cover: DocumentCover | null): Block[] {
  if (!cover) return []
  const out: Block[] = []
  const eyebrow: ParagraphChild[] = []
  if (cover.eyebrow) eyebrow.push(run(cover.eyebrow, { font: SANS, size: 19, bold: true, color: TEAL }))
  if (cover.tag) {
    const tag = TAG_STYLE[cover.tag.tone] ?? TAG_STYLE.neutral
    if (eyebrow.length) eyebrow.push(run('   '))
    eyebrow.push(run(` ${cover.tag.label} `, { font: SANS, size: 17, bold: true, color: tag.text, shading: shade(tag.fill) }))
  }
  if (eyebrow.length) out.push(para(eyebrow, { spacing: { after: 160 } }))
  if (cover.title) {
    out.push(new Paragraph({
      heading: HeadingLevel.TITLE,
      children: [run(cover.title, { font: SERIF, size: 56, color: INK_STRONG })],
      spacing: { after: 360, line: 252 },
    }))
  }
  const fields = cover.fields ?? []
  if (fields.length) {
    const widths = shareWidths(fields.map(() => 10), TEXT_WIDTH)
    out.push(ruledTable([new TableRow({
      children: fields.map((field, index) => cell([
        para([run(field.label, { font: SANS, size: 15, color: MUTED })], { spacing: { after: 20 } }),
        para([run(field.value, { font: SANS, size: 19, bold: true, color: field.tone ? FIELD_TONE[field.tone] : INK })], { spacing: { after: 0 } }),
        ...(field.note ? [para([run(field.note, { font: SANS, size: 17, color: MUTED })], { spacing: { after: 0 } })] : []),
      ], widths[index], { margins: { left: 0, top: 120 } })),
    })], widths, { top: line(16, INK) }))
    out.push(spacer(320))
  }
  return out
}

function lineParagraph(left: string, right: ParagraphChild[] | string, border: 'top' | 'bottom'): Paragraph {
  const style: RunStyle = { font: SANS, size: 15, color: MUTED }
  return new Paragraph({
    children: [run(left, style), new TextRun({ ...style, children: [new Tab(), ...(typeof right === 'string' ? [right] : [])] }), ...(typeof right === 'string' ? [] : right)],
    tabStops: [{ type: TabStopType.RIGHT, position: TEXT_WIDTH }],
    border: { [border]: line(4, BORDER, BorderStyle.SINGLE, 4) },
  })
}

export function endMark(text: string | undefined): Paragraph[] {
  if (!text) return []
  return [para([run(text, { font: SANS, size: 16, color: MUTED })], {
    alignment: AlignmentType.CENTER, border: { top: line(4, BORDER, BorderStyle.SINGLE, 6) }, spacing: { before: 600 },
  })]
}

/* --- What the report adds ------------------------------------------------------- */

const SCALE_TONE: Record<string, string> = { ok: OK, low: LOW, warn: 'B45309', bad: DANGER }

export function insertBlocks(insert: WordInsert): Block[] {
  if (insert.kind === 'severity-bar') {
    const counts = insert.counts.filter(item => item.count > 0)
    if (!counts.length) return []
    const total = counts.reduce((sum, item) => sum + item.count, 0)
    const widths = shareWidths(counts.map(item => (item.count / total) * 100), TEXT_WIDTH)
    const bar = ruledTable([new TableRow({
      height: { value: 110, rule: HeightRule.EXACT },
      children: counts.map((item, index) => cell([para([], { spacing: { after: 0, line: 100 } })], widths[index], {
        fill: SEVERITY_SOLID[item.severity], margins: { top: 0, bottom: 0, left: 0, right: 0 },
      })),
    })], widths)
    const legend = para(counts.flatMap(item => [
      run('■ ', { font: SANS, size: 16, color: SEVERITY_SOLID[item.severity] }),
      run(`${item.count} ${item.severity}    `, { font: SANS, size: 16, color: MUTED }),
    ]), { spacing: { before: 80, after: 200 } })
    return [bar, legend]
  }
  // The rating, on the scale the template allows, beside the counts it rests on.
  const left = TEXT_WIDTH * 0.58
  const right = TEXT_WIDTH - left
  const scaleWidths = shareWidths(insert.scale.map(() => 10), left - 400)
  const scale = ruledTable([new TableRow({
    children: insert.scale.map((step, index) => cell(
      [para([run(step.label, { font: SANS, size: 15, bold: step.on, color: step.on ? INK_STRONG : MUTED })], { spacing: { after: 0 } })],
      scaleWidths[index],
      { borders: { top: line(24, step.on ? SCALE_TONE[step.tone] ?? INK : BORDER) }, margins: { left: 0, right: 60, top: 60 } },
    )),
  })], scaleWidths)
  const figureWidths = shareWidths(insert.figures.map(() => 10), right - 400)
  const figures = ruledTable([new TableRow({
    children: insert.figures.map((figure, index) => cell([
      para([run(figure.value, { font: SERIF, size: 36, color: INK_STRONG })], { spacing: { after: 20 } }),
      para([run(figure.label, { font: SANS, size: 16, color: MUTED })], { spacing: { after: 0 } }),
    ], figureWidths[index], { margins: { left: 0 } })),
  })], figureWidths)
  const panel = ruledTable([new TableRow({
    cantSplit: true,
    children: [
      cell([
        para([run('Overall rating', { font: SANS, size: 17, bold: true, color: MUTED })], { spacing: { after: 40 } }),
        para([run(insert.value, { font: SERIF, size: 30, color: INK_STRONG })], { spacing: { after: 20 } }),
        ...(insert.note ? [para([run(insert.note, { font: SANS, size: 17, color: MUTED })], { spacing: { after: 120 } })] : []),
        scale,
      ], left, { fill: PANEL_WASH, margins: { left: 200, right: 200, top: 160, bottom: 160 } }),
      cell([figures], right, {
        fill: PANEL_WASH, align: VerticalAlignTable.CENTER,
        borders: { left: HAIRLINE }, margins: { left: 200, right: 200, top: 160, bottom: 160 },
      }),
    ],
  })], [left, right], { top: HAIRLINE, bottom: HAIRLINE, sides: HAIRLINE })
  return [panel, spacer(200)]
}

/* --- A finding ---------------------------------------------------------------- */

function findingBlocks(unit: Extract<DocumentUnit, { kind: 'finding' }>, ctx: WordContext): Block[] {
  const { entry, finding } = unit
  const severity = SEVERITY_FILL[finding.severity] ?? TONE_FILL.neutral
  const head: ParagraphChild[] = []
  if (entry.number) head.push(run(`Finding ${entry.number}`, { font: SANS, size: 18, bold: true, color: TEAL }), run('   '))
  head.push(run(` ${finding.severity[0]?.toUpperCase() ?? ''}${finding.severity.slice(1)} `, {
    font: SANS, size: 17, bold: true, color: severity.text, shading: shade(severity.fill),
  }))
  if (finding.reference) {
    head.push(new TextRun({ children: [new Tab(), finding.reference.id], font: MONO, size: 17, color: TEAL }))
  }
  const out: Block[] = [
    para(head, {
      border: { top: line(12, INK, BorderStyle.SINGLE, 8) },
      tabStops: [{ type: TabStopType.RIGHT, position: TEXT_WIDTH }],
      spacing: { before: 480, after: 100 }, keepNext: true,
    }),
    new Paragraph({
      heading: HeadingLevel.HEADING_2,
      children: [run(entry.label ?? entry.text, { font: SERIF, size: 30, color: INK_STRONG })],
      spacing: { after: 160 }, keepNext: true, keepLines: true,
    }),
    ...markdownBlocksWord(finding.intro, ctx),
  ]
  const widths = [1985, ctx.width - 1985]
  const inner = { ...ctx, width: widths[1] - 180 }
  const rows = [
    ...finding.rows.map(row => ({ label: row.entry.label ?? row.entry.text, body: row.body })),
    ...(finding.response ? [{ label: 'Management response', body: finding.response }] : []),
  ].map(row => new TableRow({
    children: [
      cell([para([run(row.label, { font: SANS, size: 17, bold: true, color: /recommend|remed/i.test(row.label) ? TEAL : INK_SOFT })], { spacing: { after: 0 } })], widths[0], { margins: { left: 0 } }),
      cell(markdownBlocksWord(row.body, inner), widths[1]),
    ],
  }))
  if (rows.length) out.push(ruledTable(rows, widths, { top: HAIRLINE, bottom: HAIRLINE, inside: HAIRLINE }))
  if (finding.consolidated) {
    out.push(para([run('Consolidated from', { font: SANS, size: 16, bold: true, color: MUTED })], { spacing: { before: 160, after: 40 } }))
    out.push(...markdownBlocksWord(finding.consolidated, { ...ctx, base: { font: SANS, size: 17, color: INK_SOFT } }))
  }
  out.push(spacer())
  return out
}

/* --- The document ---------------------------------------------------------------- */

const HEADING_STYLES = [
  { id: 'Title', name: 'Title', run: { font: SERIF, size: 56, color: INK_STRONG } },
  { id: 'Heading1', name: 'Heading 1', run: { font: SERIF, size: 32, color: INK_STRONG } },
  { id: 'Heading2', name: 'Heading 2', run: { font: SERIF, size: 30, color: INK_STRONG } },
  { id: 'Heading3', name: 'Heading 3', run: { font: SANS, size: 20, bold: true, color: INK_SOFT } },
].map(style => ({ ...style, basedOn: 'Normal', next: 'Normal', quickFormat: true, paragraph: { keepNext: true } }))

export function wordDocument(sheet: WordSheet, children: Block[]): Document {
  return new Document({
    creator: 'Audit Workbench',
    title: sheet.cover?.title ?? '',
    styles: {
      default: { document: { run: { font: SERIF, size: 22, color: INK }, paragraph: { spacing: { after: 120, line: 288 } } } },
      paragraphStyles: HEADING_STYLES,
    },
    numbering: {
      config: [{
        reference: 'doc-ordered',
        levels: [0, 1, 2, 3].map(level => ({
          level,
          format: LevelFormat.DECIMAL,
          text: `%${level + 1}.`,
          alignment: AlignmentType.START,
          style: {
            paragraph: { indent: { left: 360 * (level + 1), hanging: 360 } },
            run: { font: SANS, bold: true, color: TEAL },
          },
        })),
      }],
    },
    sections: [{
      properties: {
        page: {
          size: { width: A4_WIDTH, height: A4_HEIGHT },
          margin: { top: 1247, bottom: 1247, left: MARGIN, right: MARGIN, header: 624, footer: 624 },
        },
      },
      headers: sheet.runningHead ? { default: new Header({ children: [lineParagraph(sheet.runningHead[0], sheet.runningHead[1], 'bottom')] }) } : undefined,
      footers: {
        default: new Footer({
          children: [lineParagraph(sheet.footer?.[0] ?? '', [
            new TextRun({ font: SANS, size: 15, color: MUTED, children: ['Page ', PageNumber.CURRENT, ' of ', PageNumber.TOTAL_PAGES] }),
          ], 'top')],
        }),
      },
      children: [...children, ...endMark(sheet.endMark)],
    }],
  })
}

/**
 * The memorandum or the report as a Word document: the cover, then every unit
 * the sheet shows, in the order it shows them, with what the page adds.
 */
export async function markdownWord(input: {
  markdown: string
  sheet: WordSheet
  options?: DocumentOptions
  inserts?: WordInserts
}): Promise<Blob> {
  const entries = markdownOutline(input.markdown)
  const layout = documentLayout(input.markdown, entries)
  const ctx = wordContext(input.options)
  const title = layout.title?.entry?.text ?? input.sheet.cover?.title
  const children: Block[] = [...coverBlocks(input.sheet.cover || title ? { ...input.sheet.cover, title } : null)]
  if (layout.title?.body) children.push(...markdownBlocksWord(layout.title.body, ctx))
  for (const unit of layout.units) {
    if (unit.kind === 'lead') { children.push(...markdownBlocksWord(unit.body, ctx)); continue }
    if (unit.kind === 'finding') { children.push(...findingBlocks(unit, ctx)); continue }
    children.push(...headingBlocks(unit.entry, layout.hasParts))
    for (const insert of input.inserts?.afterHeading?.(unit.entry) ?? []) children.push(...insertBlocks(insert))
    children.push(...markdownBlocksWord(unit.body, ctx))
    for (const insert of input.inserts?.afterBody?.(unit.entry, unit.body) ?? []) children.push(...insertBlocks(insert))
  }
  return Packer.toBlob(wordDocument(input.sheet, children))
}

/** Hand the browser a finished file. */
export function saveWord(blob: Blob, filename: string): void {
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = filename.endsWith('.docx') ? filename : `${filename}.docx`
  link.click()
  URL.revokeObjectURL(link.href)
}
