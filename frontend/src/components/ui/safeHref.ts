/**
 * The links this app will emit: a workspace route, a tab switch, or an
 * explicit web address. Anything else — `javascript:`, `data:`, a bare scheme
 * we have no reason to follow — renders as its own text, so the words survive
 * and the navigation does not.
 *
 * A path must be a path on this site: `//host/...` starts with a slash too,
 * and is an address on someone else's.
 */
export function safeHref(value: string): string | null {
  const href = value.trim()
  if (href.startsWith('?tab=')) return href
  if (href.startsWith('/')) return href.startsWith('//') || href.startsWith('/\\') ? null : href
  return href.startsWith('https://') || href.startsWith('http://') ? href : null
}
