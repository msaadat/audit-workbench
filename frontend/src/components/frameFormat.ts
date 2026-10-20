import type { FramePayload } from '../types'

/**
 * How a frame's cells read, shared by the interactive table and the memo's
 * exhibits so one engagement never prints the same amount two ways.
 */

// An identifier that happens to be stored as an integer is not a quantity:
// grouping its digits invents a value ("1,020") and right-aligning it sorts it
// with the money. Read the name, not the dtype.
const IDENTIFIER_NAME = /(^|_)(ID|NO|NUM|NUMBER|KEY|CODE|REF)$/i

/** The columns that hold quantities: numeric by type, and not named as an identifier. */
export function numericColumnSet(frame: FramePayload): Set<string> {
  const numeric = new Set<string>()
  frame.columns.forEach((column, index) => {
    const dtype = frame.dtypes[index] ?? ''
    if (/Int|Float|Decimal/.test(dtype) && !IDENTIFIER_NAME.test(column)) numeric.add(column)
  })
  return numeric
}

// Only figures get the mono ledger face. Putting whole tables in it — which is
// what the old global `td { font-family: mono }` did — set vendor names and
// status words in a code face and made every table read as noise.
export function figureColumnSet(frame: FramePayload): Set<string> {
  const figures = numericColumnSet(frame)
  frame.columns.forEach((column, index) => {
    const dtype = frame.dtypes[index] ?? ''
    if (/Date|Time|Bool/.test(dtype)) figures.add(column)
  })
  return figures
}

export function formatCell(value: unknown, numeric: boolean): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number' && numeric) {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  }
  return String(value)
}
