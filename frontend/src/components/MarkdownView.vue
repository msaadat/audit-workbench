<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ markdown: string }>()

function escapeHtml(text: string): string {
  return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;')
}

function plainInline(text: string): string {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

function safeHref(value: string): string | null {
  const href = value.trim()
  return href.startsWith('?tab=') || href.startsWith('/') || href.startsWith('https://') || href.startsWith('http://') ? href : null
}

function inline(text: string): string {
  const out: string[] = []
  const links = /\[([^\]]+)\]\(([^)]+)\)/g
  let start = 0
  for (const match of text.matchAll(links)) {
    const index = match.index ?? 0
    out.push(plainInline(text.slice(start, index)))
    const href = safeHref(match[2])
    out.push(href ? `<a href="${escapeHtml(href)}">${plainInline(match[1])}</a>` : plainInline(match[1]))
    start = index + match[0].length
  }
  out.push(plainInline(text.slice(start)))
  return out.join('')
}

function cells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim())
}

const html = computed(() => {
  const out: string[] = []
  let listTag: 'ul' | 'ol' | null = null
  /**
   * Consecutive non-blank lines are one paragraph, ended by a blank line or by
   * any other block. Markdown that is hard-wrapped — which generated prose
   * routinely is — otherwise rendered as a stack of one-line paragraphs, each
   * carrying its own margin, so flowing text came out as a ragged column with
   * gaps between every line.
   */
  let paragraph: string[] = []
  function flushParagraph() {
    if (!paragraph.length) return
    out.push(`<p>${inline(paragraph.join(' '))}</p>`)
    paragraph = []
  }
  function closeList() {
    if (listTag) { out.push(`</${listTag}>`); listTag = null }
  }
  function pushItem(tag: 'ul' | 'ol', content: string) {
    if (listTag !== tag) { closeList(); out.push(`<${tag}>`); listTag = tag }
    out.push(`<li>${inline(content)}</li>`)
  }
  /**
   * Continue the open list item with a wrapped line (CommonMark lazy
   * continuation). Without this, a bullet spanning two source lines rendered as
   * a one-item list plus a stray paragraph, and hard-wrapped prose — which is
   * what a generated memo or APM contains — came out as a run of single-item
   * lists.
   */
  function continueItem(content: string) {
    const last = out.pop() ?? ''
    out.push(last.replace(/<\/li>$/, ` ${inline(content)}</li>`))
  }
  const lines = (props.markdown || '').split('\n')
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index]
    const line = raw.trimEnd()
    const next = lines[index + 1]?.trim() ?? ''
    if (line.includes('|') && /^\|?\s*:?-{3,}/.test(next) && next.includes('|')) {
      flushParagraph()
      closeList()
      const headers = cells(line)
      out.push(`<table><thead><tr>${headers.map(cell => `<th>${inline(cell)}</th>`).join('')}</tr></thead><tbody>`)
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        out.push(`<tr>${cells(lines[index]).map(cell => `<td>${inline(cell)}</td>`).join('')}</tr>`)
        index += 1
      }
      index -= 1
      out.push('</tbody></table>')
      continue
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    const bullet = /^[-*]\s+(.*)$/.exec(line)
    const ordered = /^\d{1,3}[.)]\s+(.*)$/.exec(line)
    if (bullet) { flushParagraph(); pushItem('ul', bullet[1]); continue }
    if (ordered) { flushParagraph(); pushItem('ol', ordered[1]); continue }
    // A non-blank line directly under an open item is that item's wrapped
    // remainder. A blank line, a heading, or a table falls through and ends
    // the list, so a paragraph that genuinely follows one stays its own block.
    if (listTag && line.trim() && !heading) { continueItem(line.trim()); continue }
    closeList()
    if (heading) {
      flushParagraph()
      out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`)
    } else if (line.trim()) paragraph.push(line.trim())
    else flushParagraph()
  }
  flushParagraph()
  closeList()
  return out.join('\n')
})
</script>

<template><div class="markdown-view" v-html="html" /></template>

<style scoped>
.markdown-view { line-height: 1.55; overflow-wrap: anywhere; }
.markdown-view :deep(h1) { font-size: var(--aw-text-xl); margin: 0 0 0.8rem; }
.markdown-view :deep(h2) { font-size: var(--aw-text-lg); margin: 1rem 0 0.4rem; }
.markdown-view :deep(h3), .markdown-view :deep(h4) { font-size: var(--aw-text-md); margin: 0.8rem 0 0.3rem; }
.markdown-view :deep(p) { margin: 0.35rem 0; }
.markdown-view :deep(ul), .markdown-view :deep(ol) { margin: 0.35rem 0; padding-left: 1.25rem; }
.markdown-view :deep(code) { background: var(--aw-raised); border-radius: var(--aw-radius-control); padding: 0 0.25rem; }
.markdown-view :deep(a) { color: var(--aw-teal, var(--aw-teal)); text-decoration: underline; text-underline-offset: 2px; }
.markdown-view :deep(table) { width: 100%; margin: 0.75rem 0; border-collapse: collapse; font-size: var(--aw-text-sm); }
.markdown-view :deep(th), .markdown-view :deep(td) { padding: 0.45rem 0.55rem; border: 1px solid var(--aw-border, var(--aw-border)); text-align: left; vertical-align: top; }
.markdown-view :deep(th) { background: var(--aw-raised, var(--aw-canvas)); }
</style>
