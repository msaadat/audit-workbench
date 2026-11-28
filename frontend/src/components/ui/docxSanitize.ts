/**
 * What `docx-preview` may leave in the page once it has rendered a client's
 * document.
 *
 * A `.docx` is client-supplied evidence, so it is untrusted input rendered into
 * the application's own origin. The renderer is called with alt-chunks off
 * (`DOCX_RENDER_OPTIONS`) — an alt-chunk is raw HTML the library would write
 * into an unsandboxed iframe — and this pass removes whatever else could run or
 * navigate: embedded frames and scripts, inline event handlers, and any link
 * that is not to the web, to an address, or to a place in the document itself.
 */

export const DOCX_RENDER_OPTIONS = { renderAltChunks: false } as const

const ACTIVE_ELEMENTS = 'script, iframe, frame, frameset, object, embed, applet, base, meta, link, form, portal'
const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:'])

/** A link target the viewer may follow, or `null` for one it must not. */
export function safeDocxHref(raw: string | null): string | null {
  const href = (raw ?? '').trim()
  if (!href) return null
  if (href.startsWith('#')) return href
  try {
    const url = new URL(href)
    return SAFE_PROTOCOLS.has(url.protocol) ? url.href : null
  } catch {
    // Relative or malformed: it would resolve against the app, never the
    // document's own location, so it has nowhere legitimate to go.
    return null
  }
}

export function sanitizeRenderedDocx(container: HTMLElement): void {
  for (const element of [...container.querySelectorAll(ACTIVE_ELEMENTS)]) element.remove()
  for (const element of [container, ...container.querySelectorAll('*')]) {
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      if (name.startsWith('on') || name === 'srcdoc' || name === 'formaction') element.removeAttribute(attribute.name)
    }
  }
  for (const element of [...container.querySelectorAll('[href], [xlink\\:href]')]) {
    const isAnchor = element instanceof HTMLAnchorElement
    const raw = element.getAttribute('href') ?? element.getAttribute('xlink:href')
    const href = safeDocxHref(raw)
    element.removeAttribute('xlink:href')
    if (!href) {
      element.removeAttribute('href')
      continue
    }
    element.setAttribute('href', href)
    if (isAnchor && !href.startsWith('#')) {
      element.setAttribute('target', '_blank')
      element.setAttribute('rel', 'noopener noreferrer')
    }
  }
}
