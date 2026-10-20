/**
 * The furniture of a written work product on its sheet: what the cover says
 * about the document, and the lines a printed report carries above and below
 * its text. None of it comes from the Markdown — it is what the page already
 * knows about the document (who wrote it, when, for which entity) — so it is
 * passed in, and the Markdown stays the document.
 */

export interface CoverField {
  label: string
  value: string
  /** A second, quieter line under the value: a date, a qualifier. */
  note?: string
  tone?: 'ok' | 'warn' | 'agent'
}

export interface DocumentCover {
  /** The line above the title: the kind of document, and for whom. */
  eyebrow?: string
  /** A status set beside the eyebrow, e.g. `Preliminary`. */
  tag?: { label: string; tone: 'warn' | 'ok' | 'neutral' }
  /** The title, where the document carries no `#` heading of its own. */
  title?: string
  /** Document control: prepared, edited, period, rating. */
  fields?: CoverField[]
}

/** Left and right halves of a running head or a footer line. */
export type SheetLine = [string, string]

/**
 * A date as a document carries one — `1 Sep 2026`. The rail's `stamp` gives
 * the time of day, which is a fact about a run; a cover dates a document.
 */
export function documentDate(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}
