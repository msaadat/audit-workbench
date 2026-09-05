<script setup lang="ts">
import { computed } from 'vue'

import MarkdownView from '../MarkdownView.vue'
import { markdownOutline } from './markdownOutline'
import type { OutlineEntry } from './markdownOutline'

/**
 * A long Markdown work product rendered as a document, section by section.
 *
 * One `MarkdownView` over the whole string would be shorter, and would give
 * the page nowhere to put anything. The report has to attach a problem to the
 * heading it is about — a rating nobody can support belongs under
 * `Audit Conclusion`, not in a list of fifty-six codes in another column — and
 * the memorandum has to be reachable by section. Both need the headings as
 * addressable elements, so the split happens here, once, and the heading is
 * rendered by this component rather than by the Markdown renderer.
 */

const props = withDefaults(defineProps<{
  markdown: string
  /** The document's eyebrow, e.g. `AUDIT PLANNING MEMORANDUM · PROCUREMENT`. */
  eyebrow?: string
}>(), { eyebrow: '' })

/**
 * The entries the outline beside this document should show. Derived here and
 * handed up, so the two can never be built from different text.
 */
const entries = computed<OutlineEntry[]>(() => markdownOutline(props.markdown))
defineExpose({ entries })

interface Block { entry: OutlineEntry | null; body: string }

/** Split at every heading, so each heading is an element with an id. */
const blocks = computed<Block[]>(() => {
  const out: Block[] = []
  let current: Block = { entry: null, body: '' }
  const lines: string[] = []
  let index = 0
  let fenced = false
  function flush() {
    current.body = lines.join('\n').trim()
    if (current.entry || current.body) out.push({ ...current })
    lines.length = 0
  }
  for (const raw of (props.markdown || '').split('\n')) {
    const line = raw.trimEnd()
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced
    if (!fenced && /^#{1,4}\s+/.test(line)) {
      flush()
      current = { entry: entries.value[index] ?? null, body: '' }
      index += 1
      continue
    }
    lines.push(line)
  }
  flush()
  return out
})
</script>

<template>
  <article class="document">
    <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
    <template v-for="(block, index) in blocks" :key="block.entry?.id ?? `lead-${index}`">
      <component
        :is="`h${block.entry.level}`"
        v-if="block.entry"
        :id="block.entry.id"
        :class="`heading level-${block.entry.level}`"
      >{{ block.entry.text }}</component>
      <!-- What the page wants to say about this heading, said under it. -->
      <slot name="after-heading" :entry="block.entry" />
      <MarkdownView v-if="block.body" :markdown="block.body" class="body" />
    </template>
    <p v-if="!blocks.length" class="empty">Nothing drafted yet.</p>
  </article>
</template>

<style scoped>
.document {
  /* The same panel and the same margins the editor uses, because it is the
     same document: the measure is padding rather than a width, so the text
     sits in one column and a wide table can still use the room beside it.
     Switching to Edit must not move a single line. */
  min-width: 0;
  padding-block: 1.5rem;
  padding-inline: max(1.25rem, calc((100% - 96ch) / 2));
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  font-size: var(--aw-text-md); line-height: 1.65;
}
.eyebrow {
  margin: 0 0 1rem;
  color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 700;
  letter-spacing: .09em; text-transform: uppercase;
}
.heading { color: var(--aw-ink-strong); scroll-margin-top: .75rem; }
.heading.level-1 { margin: 0 0 1rem; font-size: var(--aw-text-2xl); font-weight: 700; letter-spacing: -0.02em; }
.heading.level-2 { margin: 1.75rem 0 .5rem; font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em; }
.heading.level-3 { margin: 1.25rem 0 .35rem; font-size: var(--aw-text-md); font-weight: 600; }
.heading.level-4 { margin: 1rem 0 .3rem; font-size: var(--aw-text-base); font-weight: 600; }
.document > .heading:first-of-type { margin-top: 0; }
.body { color: var(--aw-ink); }
.empty { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
</style>
