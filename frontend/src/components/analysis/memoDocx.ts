import { ImageRun, Packer, Paragraph, TabStopType, TableRow, TextRun, Tab } from 'docx'
import type { ParagraphChild } from 'docx'

import type { FramePayload } from '../../types'
import { figureColumnSet, formatCell, numericColumnSet } from '../frameFormat'
import {
  BORDER, cell, coverBlocks, DANGER, DANGER_INK, DANGER_SOFT, headingBlocks, INK, INK_SOFT, INK_STRONG, line,
  markdownBlocksWord, MONO, MUTED, para, RAISED, ruledTable, run, SANS, SERIF, shareWidths, spacer, TEAL,
  TEXT_WIDTH, TONE_FILL, wordContext, wordDocument,
} from '../ui/documentDocx'
import type { Block, WordContext, WordSheet } from '../ui/documentDocx'
import type { DocumentOptions } from '../ui/markdownForms'
import { headingCount, markdownBlocks } from '../ui/markdownBlocks'
import type { OutlineEntry } from '../ui/markdownOutline'
import { memoSegments, memoUnits } from './memoExhibits'
import type { Exhibit } from './memoExhibits'

/**
 * The analysis memo as a Word document, as the sheet draws it: the lede and
 * its figures, the sections, and every exhibit numbered where the reader meets
 * it. An exhibit set is printed expanded, as the print stylesheet prints it.
 *
 * The rows, the results and any chart are what the memo page already holds —
 * it hands them over through `exhibit`, so nothing is fetched twice and the
 * file shows what the page showed.
 */

/** What the page knows about one exhibit, in the terms Word needs. */
export interface ExhibitWord {
  title: string
  found: string
  tone: string
  id: string
  as: string
  caption?: string
  failed: boolean
  frame: FramePayload | null
  tested: string[]
  /** A chart, as the page drew it: a PNG data URI and its size in pixels. */
  image: { data: string; width: number; height: number } | null
  stats: Array<{ label: string; value: string }>
}

/**
 * An exhibit of flagged rows on a portrait page. The sheet scrolls a wide
 * frame sideways; paper cannot, so the tested columns come first, then as many
 * of the rest as stay legible, and the caption says how many were left off.
 */
const MAX_COLUMNS = 8

function frameTable(frame: FramePayload, tested: string[]): { table: Block; hidden: number } {
  const ordered = [
    ...tested.filter(column => frame.columns.includes(column)),
    ...frame.columns.filter(column => !tested.includes(column)),
  ]
  const shown = ordered.slice(0, Math.max(MAX_COLUMNS, tested.length))
  const positions = shown.map(column => frame.columns.indexOf(column))
  const numeric = numericColumnSet(frame)
  const figures = figureColumnSet(frame)
  const testedSet = new Set(tested)
  const values = frame.rows.map(row => positions.map((position, index) => formatCell(row[position], numeric.has(shown[index]))))
  const widths = shareWidths(
    shown.map((column, index) => Math.max(column.length * 0.8, ...values.map(row => row[index].length))),
    TEXT_WIDTH,
  )
  const headRow = new TableRow({
    tableHeader: true,
    children: shown.map((column, index) => cell(
      [para([run(column, { font: SANS, size: 13, bold: true, allCaps: true, color: testedSet.has(column) ? DANGER_INK : MUTED })], { spacing: { after: 0 } })],
      widths[index],
      { fill: testedSet.has(column) ? DANGER_SOFT : undefined, borders: { bottom: line(6, INK_SOFT) }, margins: { left: 60, right: 60 } },
    )),
  })
  const bodyRows = values.map(row => new TableRow({
    cantSplit: true,
    children: row.map((value, index) => {
      const column = shown[index]
      const isTested = testedSet.has(column)
      return cell(
        [para([run(value, {
          font: figures.has(column) ? MONO : SANS,
          size: figures.has(column) ? 15 : 16,
          bold: isTested,
          color: isTested ? DANGER : INK,
        })], { spacing: { after: 0 }, alignment: numeric.has(column) ? 'right' : 'left' })],
        widths[index],
        { fill: isTested ? 'FEF5F4' : undefined, margins: { left: 60, right: 60, top: 40, bottom: 40 } },
      )
    }),
  }))
  return {
    table: ruledTable([headRow, ...bodyRows], widths, { inside: line(4, BORDER) }),
    hidden: ordered.length - shown.length,
  }
}

function exhibitHead(number: string, title: string, exhibit: Pick<ExhibitWord, 'found' | 'tone' | 'id'> | null, fill: string): Paragraph {
  const children: ParagraphChild[] = [
    run(number, { font: SANS, size: 17, bold: true, color: TEAL }),
    run('   '),
    run(title, { font: SANS, size: 19, bold: true, color: INK_STRONG }),
  ]
  if (exhibit?.found) {
    const tone = TONE_FILL[exhibit.tone] ?? TONE_FILL.neutral
    children.push(run('   '), run(` ${exhibit.found} `, { font: SANS, size: 15, bold: true, color: tone.text, shading: { type: 'clear', color: 'auto', fill: tone.fill } }))
  }
  if (exhibit?.id) children.push(new TextRun({ children: [new Tab(), exhibit.id], font: MONO, size: 15, color: TEAL }))
  return para(children, {
    shading: { type: 'clear', color: 'auto', fill },
    tabStops: [{ type: TabStopType.RIGHT, position: TEXT_WIDTH - 120 }],
    border: { top: line(4, BORDER, 'single', 4), bottom: line(4, BORDER, 'single', 4) },
    spacing: { before: 0, after: 0 }, keepNext: true, indent: { left: 60, right: 60 },
  })
}

function exhibitBody(number: number, exhibit: ExhibitWord): Block[] {
  const out: Block[] = []
  let hidden = 0
  if (exhibit.failed) {
    out.push(para([run('Could not load this result.', { font: SANS, size: 18, color: DANGER })]))
  } else if (exhibit.as === 'stats') {
    out.push(para(exhibit.stats.flatMap(stat => [
      run(`${stat.label} `, { font: SANS, size: 17, color: MUTED }),
      run(`${stat.value}    `, { font: SANS, size: 21, bold: true, color: INK }),
    ]), { spacing: { before: 120, after: 120 } }))
  } else if (exhibit.image) {
    const scale = Math.min(1, 600 / exhibit.image.width)
    out.push(para([new ImageRun({
      type: 'png',
      data: exhibit.image.data,
      transformation: { width: Math.round(exhibit.image.width * scale), height: Math.round(exhibit.image.height * scale) },
    })], { spacing: { before: 120, after: 120 } }))
  } else if (exhibit.frame && exhibit.frame.rows.length) {
    const table = frameTable(exhibit.frame, exhibit.tested)
    hidden = table.hidden
    out.push(table.table)
  } else {
    out.push(para([run('This result recorded no flagged rows.', { font: SANS, size: 18, color: MUTED })], { spacing: { before: 120 } }))
  }
  const caption: ParagraphChild[] = []
  if (exhibit.caption) caption.push(run(`Exhibit ${number}. ${exhibit.caption}`, { font: SERIF, size: 19, italics: true, color: INK_SOFT }))
  if (hidden > 0) {
    if (caption.length) caption.push(run('    '))
    caption.push(run(`The tested columns first · ${hidden} more ${hidden === 1 ? 'column' : 'columns'} in the app`, { font: SANS, size: 15, color: MUTED }))
  }
  out.push(caption.length
    ? para(caption, { border: { bottom: line(4, BORDER, 'single', 4) }, spacing: { before: 80, after: 280 } })
    : spacer(280))
  return out
}

/** The memo's lede and its figures, as the sheet sets them. */
function ledeBlocks(body: string, figures: Array<{ label: string; value: number; tone?: string }>, ctx: WordContext): Block[] {
  const out: Block[] = [
    para([run('In brief', { font: SANS, size: 18, bold: true, color: TEAL })], {
      border: { top: line(4, BORDER, 'single', 10) }, spacing: { before: 200, after: 60 }, keepNext: true,
    }),
    ...markdownBlocksWord(body, { ...ctx, base: { font: SERIF, size: 26, color: INK_STRONG } }),
  ]
  if (figures.length) {
    const widths = shareWidths(figures.map(() => 10), TEXT_WIDTH)
    const toneColour: Record<string, string> = { bad: DANGER, ok: '147D55' }
    out.push(ruledTable([new TableRow({
      children: figures.map((figure, index) => cell([
        para([run(String(figure.value), { font: SERIF, size: 40, color: figure.value && figure.tone ? toneColour[figure.tone] ?? INK_STRONG : INK_STRONG })], { spacing: { after: 20 } }),
        para([run(figure.label, { font: SANS, size: 16, color: MUTED })], { spacing: { after: 0 } }),
      ], widths[index], { margins: { left: 0, top: 120, bottom: 120 } })),
    })], widths, { top: line(4, BORDER), bottom: line(4, BORDER) }))
    out.push(spacer(240))
  }
  return out
}

export async function memoWord(input: {
  sheet: WordSheet
  markdown: string
  entries: OutlineEntry[]
  options: DocumentOptions
  figures: Array<{ label: string; value: number; tone?: string }>
  exhibit: (exhibit: Exhibit) => ExhibitWord
}): Promise<Blob> {
  const ctx = wordContext(input.options)
  const segments = memoSegments(input.markdown)
  const units = memoUnits(segments)
  // Each prose segment split at its headings against the whole memo's entries,
  // offset by the headings before it — exactly as the page splits it.
  const prose = new Map<number, ReturnType<typeof markdownBlocks>>()
  let offset = 0
  segments.forEach((segment, index) => {
    if (segment.kind !== 'prose') return
    prose.set(index, markdownBlocks(segment.text, input.entries.slice(offset)))
    offset += headingCount(segment.text)
  })

  const children: Block[] = [...coverBlocks(input.sheet.cover)]
  let first = true
  for (const unit of units) {
    if (unit.kind === 'prose') {
      ;(prose.get(unit.index) ?? []).forEach((block, position) => {
        if (first && position === 0 && !block.entry) {
          children.push(...ledeBlocks(block.body, input.figures, ctx))
          return
        }
        if (block.entry) children.push(...headingBlocks(block.entry, false))
        children.push(...markdownBlocksWord(block.body, ctx))
      })
      first = false
      continue
    }
    if (unit.exhibits.length === 1) {
      const [exhibit] = unit.exhibits
      const data = input.exhibit(exhibit)
      children.push(exhibitHead(`Exhibit ${exhibit.number}`, data.title, data, RAISED))
      children.push(...exhibitBody(exhibit.number, data))
      continue
    }
    const all = unit.exhibits.map(exhibit => ({ exhibit, data: input.exhibit(exhibit) }))
    const titles = new Set(all.map(item => item.data.title))
    const setTitle = titles.size === 1 ? [...titles][0] : `${all.length} procedures`
    children.push(exhibitHead(`Exhibits ${unit.exhibits[0].number}–${unit.exhibits[unit.exhibits.length - 1].number}`, setTitle, null, RAISED))
    for (const { exhibit, data } of all) {
      children.push(exhibitHead(String(exhibit.number), data.title, data, 'F7F9FC'))
      children.push(...exhibitBody(exhibit.number, data))
    }
  }
  return Packer.toBlob(wordDocument(input.sheet, children))
}
