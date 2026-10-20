<script setup lang="ts">
import { computed } from 'vue'

import { markdownToHtml } from './ui/markdownHtml'
import type { DocumentOptions } from './ui/markdownHtml'

/**
 * A Markdown fragment, rendered. The parse and the HTML it becomes live in
 * `ui/markdownHtml.ts` — the same `remark-parse` + `remark-gfm` the editor
 * parses with — so this component is the styling and nothing else.
 *
 * `document` renders it as part of a written work product on its sheet: the
 * forms a report has (definitions, registers, ruled tables, severities) where
 * the Markdown's shape asks for them. Off, it is the plain rendering chat,
 * notes and narratives use, and none of the document styles below apply.
 */
const props = defineProps<{ markdown: string; document?: DocumentOptions | boolean }>()

const options = computed(() => props.document
  ? { document: props.document === true ? {} : props.document }
  : {})
const html = computed(() => markdownToHtml(props.markdown, options.value))
</script>

<template><div :class="['markdown-view', { doc: Boolean(document) }]" v-html="html" /></template>

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
.markdown-view:not(.doc) :deep(th), .markdown-view:not(.doc) :deep(td) { padding: 0.45rem 0.55rem; border: 1px solid var(--aw-border, var(--aw-border)); text-align: left; vertical-align: top; }
/* `anywhere` on the container keeps a long identifier inside the measure, but
   in a table it also lets a column shrink below its longest word — five
   columns of prose came out as one letter per line. Cells break long words
   only when a word genuinely does not fit, which is what the editor does. */
.markdown-view :deep(th), .markdown-view :deep(td) { overflow-wrap: break-word; }
.markdown-view:not(.doc) :deep(th) { background: var(--aw-raised, var(--aw-canvas)); }
.markdown-view :deep(.footnote) { margin-top: 0.75rem; padding-top: 0.5rem; border-top: 1px solid var(--aw-border); font-size: var(--aw-text-sm); color: var(--aw-ink-soft); }

/* =========================================================================
   On the sheet. The face, size and measure come from the sheet's body
   (`.aw-type-document`); what is here is the forms a report draws.
   Structure is set in the interface face — labels, numbers, tables — and the
   running text stays in the serif, as a printed report does it.
   ========================================================================= */
.doc { line-height: inherit; }
.doc :deep(p) { margin: 0 0 .75rem; }
.doc :deep(ul), .doc :deep(ol) { margin: .25rem 0 .9rem; padding-left: 1.35rem; }
.doc :deep(li) { margin: 0 0 .35rem; line-height: inherit; }
.doc :deep(li > p) { margin: 0 0 .35rem; }
.doc :deep(ol:not([class]) > li::marker) { color: var(--aw-teal); font-family: var(--aw-font-sans); font-size: .875em; font-weight: 650; }
.doc :deep(strong) { font-weight: 650; }
.doc :deep(code) { font-size: .85em; }
.doc :deep(blockquote) { margin: 1rem 0; }

/* A label beside its value: `Entity`, `Period`, `Objective`. */
.doc :deep(.doc-kv) {
  display: grid; grid-template-columns: minmax(6.5rem, 9.5rem) minmax(0, 1fr);
  margin: .25rem 0 1rem;
  border-top: 1px solid var(--aw-border);
}
.doc :deep(.doc-kv > dt), .doc :deep(.doc-kv > dd) { margin: 0; padding: .6rem 0; border-bottom: 1px solid var(--aw-border); }
.doc :deep(.doc-kv > dt) {
  padding-right: 1rem;
  color: var(--aw-ink-soft); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-weight: 600; line-height: 1.6;
}

/* A numbered register: the exceptions, the matters, the further work. */
.doc :deep(.doc-register), .doc :deep(.doc-steps), .doc :deep(.doc-hang) { list-style: none; margin: .375rem 0 1.125rem; padding: 0; }
.doc :deep(.doc-register) { border-top: 1px solid var(--aw-border); }
.doc :deep(.doc-register > li) {
  display: grid; grid-template-columns: 2.75rem minmax(0, 1fr);
  margin: 0; padding: .8rem 0;
  border-bottom: 1px solid var(--aw-border);
}
.doc :deep(.doc-register .doc-k) {
  color: var(--aw-teal);
  font-family: var(--aw-font-serif); font-size: var(--aw-text-2xl); font-weight: 500; line-height: 1;
  font-variant-numeric: lining-nums;
}
.doc :deep(.doc-lead) {
  margin: .1rem 0 .25rem;
  color: var(--aw-ink-strong); font-family: var(--aw-font-sans); font-size: var(--aw-text-md); font-weight: 650; line-height: 1.35;
}
.doc :deep(.doc-item) { min-width: 0; }
.doc :deep(.doc-item > p) { margin: 0 0 .4rem; }
.doc :deep(.doc-item > :last-child) { margin-bottom: 0; }

/* The process the memorandum describes, as the steps of it. */
.doc :deep(.doc-steps > li) {
  position: relative;
  display: grid; grid-template-columns: 2.625rem minmax(0, 1fr);
  margin: 0; padding-bottom: 1.1rem;
}
.doc :deep(.doc-steps > li::before) {
  content: ''; position: absolute; left: .85rem; top: 1.9rem; bottom: .15rem;
  width: 1.5px; background: var(--aw-teal-line);
}
.doc :deep(.doc-steps > li:last-child::before) { display: none; }
.doc :deep(.doc-steps .doc-k) {
  display: flex; align-items: center; justify-content: center;
  width: 1.75rem; height: 1.75rem;
  border: 1.5px solid var(--aw-teal); border-radius: 50%;
  background: var(--aw-sheet); color: var(--aw-teal);
  font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); font-weight: 650;
}

/* A risk, a matter, a lens: its name in a hanging column beside what is said. */
.doc :deep(.doc-hang > li) {
  display: grid; grid-template-columns: minmax(7.5rem, 12.5rem) minmax(0, 1fr); gap: .25rem 1.375rem;
  margin: 0; padding: .9rem 0;
  border-top: 1px solid var(--aw-border);
}
.doc :deep(.doc-hang > li:last-child) { border-bottom: 1px solid var(--aw-border); }
.doc :deep(.doc-lab) {
  color: var(--aw-ink-strong); font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); font-weight: 650; line-height: 1.4;
}

.doc :deep(.doc-runin > strong:first-child) {
  color: var(--aw-ink-strong); font-family: var(--aw-font-sans); font-size: .875em; font-weight: 650;
}

/* What the document says about its own standing. A band, not a quote. */
.doc :deep(.doc-band) {
  margin: 1rem 0 1.25rem; padding: .75rem 1rem;
  border-radius: 4px; background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); line-height: 1.5;
}
.doc :deep(.doc-band p) { margin: 0; }
.doc :deep(.doc-band strong) { color: inherit; }

/* A section nobody has written yet, drawn as the gap it is. */
.doc :deep(.doc-gap) {
  padding: .5rem .75rem;
  border: 1px dashed var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); font-style: normal;
}
.doc :deep(.doc-gap em) { font-style: normal; }

/* Ruled tables: a rule above and below, none between columns. The column
   head is the one uppercase label the foundations allow. */
.doc :deep(.table-scroll) { margin: .5rem 0 1.125rem; }
.doc :deep(.doc-table) { font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); line-height: 1.45; }
.doc :deep(.doc-table th) {
  padding: .45rem .875rem .45rem 0;
  border-top: 1.5px solid var(--aw-ink); border-bottom: 1px solid var(--aw-ink);
  color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 650;
  letter-spacing: .03em; text-align: left; text-transform: uppercase; white-space: nowrap;
  vertical-align: bottom;
}
.doc :deep(.doc-table td) { padding: .5rem .875rem .5rem 0; border-bottom: 1px solid var(--aw-border); text-align: left; vertical-align: top; }
.doc :deep(.doc-table tbody tr:last-child td) { border-bottom: 1.5px solid var(--aw-ink); }
.doc :deep(.doc-table .num) { text-align: right; font-variant-numeric: tabular-nums; }
.doc :deep(.doc-table .count) { padding-right: 0; text-align: center; font-variant-numeric: tabular-nums; }
.doc :deep(.doc-table .nil), .doc :deep(.doc-table .zero) { color: var(--aw-muted); }
.doc :deep(.doc-table td.sev-count) { font-weight: 700; }
.doc :deep(.doc-table td.sev-count[data-sev='critical']) { background: var(--aw-danger-soft); color: var(--aw-danger); }
.doc :deep(.doc-table td.sev-count[data-sev='high']) { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.doc :deep(.doc-table td.sev-count[data-sev='medium']) { background: var(--aw-low-soft); color: var(--aw-low-ink); }
.doc :deep(.doc-table td.sev-count[data-sev='low']) { background: var(--aw-ok-soft); color: var(--aw-ok); }

/* A procedure in prose: a superscript to its exhibit, or a chip with its dot. */
.doc :deep(.doc-xref) { line-height: 0; }
.doc :deep(.doc-xref a) {
  color: var(--aw-teal); font-family: var(--aw-font-sans); font-size: var(--aw-text-2xs); font-weight: 650;
  text-decoration: none; white-space: nowrap;
}
.doc :deep(.doc-xref a:hover) { text-decoration: underline; }
.doc :deep(.doc-cite) {
  display: inline-flex; align-items: center; gap: .25rem;
  padding: 0 .4rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill);
  color: var(--aw-ink); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); line-height: 1.6;
  text-decoration: none; vertical-align: 1px; white-space: nowrap;
}
.doc :deep(.doc-cite:hover) { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.doc :deep(.doc-cite i) { width: 6px; height: 6px; border-radius: 50%; background: var(--aw-border-strong); }
.doc :deep(.doc-cite[data-tone='ok'] i) { background: var(--aw-ok); }
.doc :deep(.doc-cite[data-tone='warn'] i) { background: var(--aw-warn); }
.doc :deep(.doc-cite[data-tone='bad'] i) { background: var(--aw-danger); }

@container doc-sheet (max-width: 34rem) {
  .doc :deep(.doc-hang > li), .doc :deep(.doc-kv) { grid-template-columns: minmax(0, 1fr); }
  .doc :deep(.doc-kv > dt) { padding-bottom: 0; border-bottom: 0; }
}

@media print {
  .doc :deep(.doc-table tr), .doc :deep(.doc-register > li), .doc :deep(.doc-steps > li), .doc :deep(.doc-hang > li) { break-inside: avoid; }
}
</style>
