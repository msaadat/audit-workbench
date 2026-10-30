import { markdownBlocks } from './markdownBlocks'
import type { MarkdownBlock } from './markdownBlocks'
import type { OutlineEntry } from './markdownOutline'

/**
 * A long work product as the units a reader meets: its title, the prose before
 * any heading, its sections, and the findings the report assembled.
 *
 * A finding is the one unit that is more than a heading and its text. The
 * report writes each one as a `###` whose first line is its severity and
 * reference, with `####` sections under it and, at the end, the procedures it
 * absorbed and the response to it. Set as a printed report sets a finding, its
 * sections go in a labelled column — on screen (`UiMarkdownDocument`) and in
 * the Word export alike, which is why the reading of it lives here.
 */

export interface FindingRow { entry: OutlineEntry; body: string }
export interface FindingReference { id: string; href: string }
export interface FindingBlock {
  severity: string
  reference: FindingReference | null
  intro: string
  rows: FindingRow[]
  response: string
  consolidated: string
}
export type DocumentUnit =
  | { kind: 'lead'; body: string }
  | { kind: 'section'; entry: OutlineEntry; body: string }
  | { kind: 'finding'; entry: OutlineEntry; finding: FindingBlock }

export interface DocumentLayout {
  /** The `#` title and the text directly under it (the report's preliminary notice). */
  title: MarkdownBlock | null
  units: DocumentUnit[]
  /** The document is divided into lettered parts, so its `###` are sections. */
  hasParts: boolean
}

/** The line `report.py` writes under every finding heading it assembles. */
const SEVERITY_LINE = /^\*\*Severity:\*\*\s*([A-Za-z]+)[^\n]*?(?:\*\*Reference:\*\*\s*([^\n]*))?\s*(?:\n|$)/
const LINK = /\[([^\]]+)\]\(([^)\s]+)\)/
const TRAILER = /^\*\*(Supporting procedures|Management response):\*\*[ \t]*/gm

function reference(text: string | undefined): FindingReference | null {
  const match = LINK.exec(text ?? '')
  if (!match) return null
  const fromHref = /[?&]finding=([^&#]+)/.exec(match[2])?.[1]
  const id = fromHref ? decodeURIComponent(fromHref) : match[1].trim().split(/\s+/).pop() ?? ''
  return id ? { id, href: match[2] } : null
}

/**
 * What the report appends to a finding's narrative: the procedures it absorbed
 * and the response to it. They are read off the end of the last section, where
 * the assembler put them, so the narrative itself is left as written.
 */
function trailers(text: string): { body: string; consolidated: string; response: string } {
  const hits = [...text.matchAll(TRAILER)]
  if (!hits.length) return { body: text, consolidated: '', response: '' }
  let consolidated = ''
  let response = ''
  hits.forEach((hit, index) => {
    const end = index + 1 < hits.length ? hits[index + 1].index : text.length
    const content = text.slice((hit.index ?? 0) + hit[0].length, end).trim()
    if (hit[1] === 'Supporting procedures') {
      consolidated = content.replace(
        /^the following (?:findings were consolidated into this one|procedures observed this issue and are reported in this finding)\.?\s*/i,
        '',
      )
    } else {
      response = content
    }
  })
  return { body: text.slice(0, hits[0].index).trimEnd(), consolidated, response }
}

export function documentLayout(markdown: string, entries: OutlineEntry[]): DocumentLayout {
  const blocks = markdownBlocks(markdown, entries)
  const title = blocks.find(block => block.entry?.level === 1) ?? null
  const hasParts = entries.some(entry => entry.part)
  const list = blocks.filter(block => block !== title)
  const units: DocumentUnit[] = []
  let part: OutlineEntry | null = null
  for (let index = 0; index < list.length; index += 1) {
    const block = list[index]
    if (!block.entry) { if (block.body) units.push({ kind: 'lead', body: block.body }); continue }
    if (block.entry.level <= 2) part = block.entry.part ? block.entry : (block.entry.level === 2 ? null : part)
    const severity = block.entry.level === 3 ? SEVERITY_LINE.exec(block.body) : null
    const inFindings = !hasParts || /finding/i.test(part?.label ?? part?.text ?? '')
    if (!severity || !inFindings) {
      units.push({ kind: 'section', entry: block.entry, body: block.body })
      continue
    }
    const rows: FindingRow[] = []
    while (index + 1 < list.length && (list[index + 1].entry?.level ?? 0) >= 4) {
      index += 1
      rows.push({ entry: list[index].entry as OutlineEntry, body: list[index].body })
    }
    let intro = block.body.slice(severity[0].length).trim()
    let tail = { body: '', consolidated: '', response: '' }
    if (rows.length) {
      tail = trailers(rows[rows.length - 1].body)
      rows[rows.length - 1] = { ...rows[rows.length - 1], body: tail.body }
    } else {
      tail = trailers(intro)
      intro = tail.body
    }
    units.push({
      kind: 'finding',
      entry: block.entry,
      finding: {
        severity: severity[1].toLowerCase(),
        reference: reference(severity[2]),
        intro,
        rows,
        response: tail.response,
        consolidated: tail.consolidated,
      },
    })
  }
  return { title, units, hasParts }
}
