<script setup lang="ts">
import type { OutlineEntry } from './markdownOutline'

/**
 * One heading of a document on its sheet, numbered the way the outline beside
 * it numbers it — both read `entry.number`, so they cannot disagree.
 *
 * A lettered `##` is a part (`Part A · Executive summary`), drawn as a divider
 * across the sheet. Below a part, a `###` is a section of the document and is
 * set like one; in a document without parts, a `###` is a subsection.
 */
withDefaults(defineProps<{
  entry: OutlineEntry
  /** The document is divided into lettered parts, so its `###` are sections. */
  parts?: boolean
}>(), { parts: false })
</script>

<template>
  <div v-if="entry.part" class="doc-part">
    <p class="doc-part-label">Part {{ entry.number }}</p>
    <h2 :id="entry.id" class="doc-part-title">{{ entry.label ?? entry.text }}</h2>
  </div>
  <component
    :is="`h${entry.level}`"
    v-else
    :id="entry.id"
    :class="['doc-h', `level-${entry.level}`, { numbered: Boolean(entry.number), section: parts && entry.level === 3 }]"
  >
    <span v-if="entry.number" class="doc-n">{{ entry.number }}</span>
    <span class="doc-t">{{ entry.label ?? entry.text }}</span>
  </component>
</template>

<style scoped>
.doc-part {
  margin: 3.5rem 0 0;
  padding-top: .75rem;
  border-top: 2px solid var(--aw-ink);
  break-after: avoid;
}
.doc-part-label {
  margin: 0;
  color: var(--aw-teal); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-weight: 650;
}
.doc-part-title {
  margin: .25rem 0 0; scroll-margin-top: .75rem;
  color: var(--aw-ink-strong);
  font-family: var(--aw-font-serif); font-size: var(--aw-text-2xl); font-weight: 600;
  letter-spacing: -0.01em; line-height: 1.15;
}

/* The number hangs in the sheet's margin, so the titles share the text's
   left edge and the numbers form their own column down the page. */
.doc-h {
  margin: 0; scroll-margin-top: .75rem;
  color: var(--aw-ink-strong);
  break-after: avoid;
}
.doc-h.numbered {
  display: grid; grid-template-columns: var(--doc-hang, 2.5rem) minmax(0, 1fr); align-items: baseline;
  margin-left: calc(-1 * var(--doc-hang, 2.5rem));
}
.doc-n {
  color: var(--aw-teal);
  font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); font-weight: 650;
  font-variant-numeric: tabular-nums; letter-spacing: 0;
}
.doc-h.level-2 {
  margin-top: 2.75rem; margin-bottom: .875rem;
  font-family: var(--aw-font-serif); font-size: var(--aw-text-xl); font-weight: 600;
  letter-spacing: -0.005em; line-height: 1.25;
}
.doc-h.level-3 {
  margin-top: 1.75rem; margin-bottom: .5rem;
  font-family: var(--aw-font-sans); font-size: var(--aw-text-md); font-weight: 650;
  letter-spacing: 0; line-height: 1.35;
}
.doc-h.level-3 .doc-n { font-size: var(--aw-text-xs); }
.doc-h.level-3.section {
  margin-top: 2.25rem; margin-bottom: .75rem;
  font-family: var(--aw-font-serif); font-size: var(--aw-text-xl); font-weight: 600;
  letter-spacing: -0.005em; line-height: 1.25;
}
.doc-h.level-3.section .doc-n { font-size: var(--aw-text-sm); }
.doc-h.level-4 {
  margin-top: 1.25rem; margin-bottom: .35rem;
  color: var(--aw-ink-soft);
  font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); font-weight: 650;
  letter-spacing: 0;
}

/* On a narrow sheet the margin is too slight to hang a number in. */
@container doc-sheet (max-width: 34rem) {
  .doc-h.numbered { grid-template-columns: auto minmax(0, 1fr); gap: .5rem; margin-left: 0; }
}
</style>
