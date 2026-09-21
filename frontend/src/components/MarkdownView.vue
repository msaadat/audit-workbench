<script setup lang="ts">
import { computed } from 'vue'

import { markdownToHtml } from './ui/markdownHtml'

/**
 * A Markdown fragment, rendered. The parse and the HTML it becomes live in
 * `ui/markdownHtml.ts` — the same `remark-parse` + `remark-gfm` the editor
 * parses with — so this component is the styling and nothing else.
 */
const props = defineProps<{ markdown: string }>()

const html = computed(() => markdownToHtml(props.markdown))
</script>

<template><div class="markdown-view" v-html="html" /></template>

<style scoped>
.markdown-view { line-height: 1.55; overflow-wrap: anywhere; }
.markdown-view :deep(h1) { font-size: var(--aw-text-xl); margin: 0 0 0.8rem; }
.markdown-view :deep(h2) { font-size: var(--aw-text-lg); margin: 1rem 0 0.4rem; }
.markdown-view :deep(h3), .markdown-view :deep(h4) { font-size: var(--aw-text-md); margin: 0.8rem 0 0.3rem; }
.markdown-view :deep(h5), .markdown-view :deep(h6) { font-size: var(--aw-text-base); margin: 0.7rem 0 0.25rem; }
.markdown-view :deep(p) { margin: 0.35rem 0; }
.markdown-view :deep(ul), .markdown-view :deep(ol) { margin: 0.35rem 0; padding-left: 1.25rem; }
.markdown-view :deep(li) { line-height: 1.55; }
/* A task list carries its own markers, so the bullet beside them is noise. */
.markdown-view :deep(li:has(> input[type="checkbox"])) { list-style: none; margin-left: -1.25rem; }
.markdown-view :deep(li > input[type="checkbox"]) { margin-right: 0.35rem; accent-color: var(--aw-teal); }
.markdown-view :deep(code) { background: var(--aw-raised); border-radius: var(--aw-radius-control); padding: 0 0.25rem; font-family: var(--aw-font-mono); }
.markdown-view :deep(pre) { margin: 0.75rem 0; padding: var(--aw-space-3); border-radius: var(--aw-radius-control); background: var(--aw-raised); overflow-x: auto; }
.markdown-view :deep(pre code) { padding: 0; background: none; }
/* Matches the editor's quote: a teal rule rather than a grey block. */
.markdown-view :deep(blockquote) { margin: 0.75rem 0; padding-left: var(--aw-space-3); border-left: 2px solid var(--aw-teal-line); color: var(--aw-ink-soft); }
.markdown-view :deep(hr) { margin: 1rem 0; border: 0; border-top: 1px solid var(--aw-border); }
.markdown-view :deep(del) { color: var(--aw-ink-soft); }
.markdown-view :deep(img) { max-width: 100%; height: auto; }
.markdown-view :deep(a) { color: var(--aw-teal, var(--aw-teal)); text-decoration: underline; text-underline-offset: 2px; }
.markdown-view :deep(.table-scroll) { margin: 0.75rem 0; overflow-x: auto; }
.markdown-view :deep(table) { width: 100%; border-collapse: collapse; font-size: var(--aw-text-sm); }
.markdown-view :deep(th), .markdown-view :deep(td) { padding: 0.45rem 0.55rem; border: 1px solid var(--aw-border, var(--aw-border)); text-align: left; vertical-align: top; }
/* `anywhere` on the container keeps a long identifier inside the measure, but
   in a table it also lets a column shrink below its longest word — five
   columns of prose came out as one letter per line. Cells break long words
   only when a word genuinely does not fit, which is what the editor does. */
.markdown-view :deep(th), .markdown-view :deep(td) { overflow-wrap: break-word; }
.markdown-view :deep(th) { background: var(--aw-raised, var(--aw-canvas)); }
.markdown-view :deep(.footnote) { margin-top: 0.75rem; padding-top: 0.5rem; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); color: var(--aw-ink-soft); }
</style>
