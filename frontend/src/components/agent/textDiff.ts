export interface DiffLine {
  kind: 'same' | 'del' | 'add' | 'skip'
  text: string
}

const CONTEXT_LINES = 2
const MAX_LCS_CELLS = 250_000

function rawLineDiff(beforeLines: string[], afterLines: string[]): DiffLine[] {
  const m = beforeLines.length
  const n = afterLines.length
  if (m * n > MAX_LCS_CELLS) {
    return [
      ...beforeLines.map(text => ({ kind: 'del' as const, text })),
      ...afterLines.map(text => ({ kind: 'add' as const, text })),
    ]
  }
  const dp: Uint32Array[] = Array.from({ length: m + 1 }, () => new Uint32Array(n + 1))
  for (let i = m - 1; i >= 0; i -= 1) {
    const row = dp[i]
    const next = dp[i + 1]
    for (let j = n - 1; j >= 0; j -= 1) {
      row[j] = beforeLines[i] === afterLines[j] ? next[j + 1] + 1 : Math.max(next[j], row[j + 1])
    }
  }
  const result: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < m && j < n) {
    if (beforeLines[i] === afterLines[j]) {
      result.push({ kind: 'same', text: beforeLines[i] })
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ kind: 'del', text: beforeLines[i] })
      i += 1
    } else {
      result.push({ kind: 'add', text: afterLines[j] })
      j += 1
    }
  }
  while (i < m) {
    result.push({ kind: 'del', text: beforeLines[i] })
    i += 1
  }
  while (j < n) {
    result.push({ kind: 'add', text: afterLines[j] })
    j += 1
  }
  return result
}

/**
 * Compute a compact line-by-line unified diff between two text passages,
 * collapsing long runs of unchanged context lines.
 */
export function diffLines(before: string, after: string): DiffLine[] {
  const beforeText = String(before ?? '')
  const afterText = String(after ?? '')
  if (beforeText === afterText) return []
  const beforeLines = beforeText ? beforeText.split(/\r?\n/) : []
  const afterLines = afterText ? afterText.split(/\r?\n/) : []
  const raw = rawLineDiff(beforeLines, afterLines)
  const changed = new Set<number>()
  raw.forEach((line, idx) => {
    if (line.kind !== 'same') changed.add(idx)
  })
  if (!changed.size) return []

  const keep = new Set<number>()
  for (const idx of changed) {
    for (let offset = -CONTEXT_LINES; offset <= CONTEXT_LINES; offset += 1) {
      const target = idx + offset
      if (target >= 0 && target < raw.length) keep.add(target)
    }
  }

  const compact: DiffLine[] = []
  let skipped = 0
  for (let idx = 0; idx < raw.length; idx += 1) {
    if (keep.has(idx)) {
      if (skipped > 0) {
        compact.push({ kind: 'skip', text: `… ${skipped} unchanged line${skipped === 1 ? '' : 's'}` })
        skipped = 0
      }
      compact.push(raw[idx])
    } else {
      skipped += 1
    }
  }
  if (skipped > 0) {
    compact.push({ kind: 'skip', text: `… ${skipped} unchanged line${skipped === 1 ? '' : 's'}` })
  }
  return compact
}
