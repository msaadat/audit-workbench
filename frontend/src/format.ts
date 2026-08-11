/**
 * Shared value formatting.
 *
 * Everything here exists because the same construction was being written inline
 * in dozens of components and getting it subtly wrong each time — "1 warnings",
 * "0 MB" for every small table. A helper is not an abstraction for its own sake
 * when the alternative is 46 hand-written variants.
 */

/**
 * `3 findings`, `1 finding`. Irregular plurals pass their own second form.
 *
 * The count is always rendered, because every caller here is reporting a
 * quantity — a bare noun would lose the number the sentence is about.
 */
export function plural(count: number, singular: string, pluralForm?: string): string {
  const word = count === 1 ? singular : (pluralForm ?? `${singular}s`)
  return `${count.toLocaleString()} ${word}`
}

/** The noun alone, for when the caller renders the number separately. */
export function pluralWord(count: number, singular: string, pluralForm?: string): string {
  return count === 1 ? singular : (pluralForm ?? `${singular}s`)
}

/**
 * Subject-verb agreement for a sentence built around `plural`.
 *
 * `` `${plural(n, 'item')} ${verb(n)} attention` `` reads correctly at one and
 * at many, which the "(s)" construction never managed.
 */
export function verb(count: number, singular = 'needs', pluralForm = 'need'): string {
  return count === 1 ? singular : pluralForm
}

const SIZE_UNITS = ['bytes', 'KB', 'MB', 'GB', 'TB'] as const

/**
 * A byte count in the largest unit it survives.
 *
 * The profiler used to report megabytes rounded to two decimals, so every table
 * under about 5 KB — which is most reference tables — displayed as "0 MB".
 */
export function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 bytes'
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), SIZE_UNITS.length - 1)
  const value = bytes / 1024 ** exponent
  // Bytes are whole; larger units keep one decimal until the number is big
  // enough that the decimal is noise.
  const digits = exponent === 0 ? 0 : value < 10 ? 1 : 0
  return `${value.toFixed(digits)} ${SIZE_UNITS[exponent]}`
}
