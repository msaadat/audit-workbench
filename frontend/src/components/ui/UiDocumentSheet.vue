<script setup lang="ts">
import { ref } from 'vue'

import type { DocumentCover, SheetLine } from './documentSheet'

/**
 * A written work product as a sheet on a desk.
 *
 * The memorandum, the memo and the report were a rounded card in the interface
 * face — which is how a README looks, and nothing about them said "document".
 * This is the frame all three share: the desk a step below the canvas, a sheet
 * the width of A4 lifted off it, a running head, a cover that says what the
 * document is and who wrote it, and an end mark and footer. What goes on the
 * sheet is the caller's: one component renders Markdown blocks, the memo
 * renders prose between embedded results.
 */
withDefaults(defineProps<{
  cover?: DocumentCover | null
  /** The `id` on the cover's title, so the outline can reach the top. */
  titleId?: string
  runningHead?: SheetLine | null
  footer?: SheetLine | null
  /** `End of memorandum`: where the document stops, which a scroller never says. */
  endMark?: string
}>(), { cover: null, titleId: undefined, runningHead: null, footer: null, endMark: '' })

const sheet = ref<HTMLElement | null>(null)
/** The sheet itself, which is what Print hands the printer. */
defineExpose({ sheet })
</script>

<template>
  <div class="doc-desk">
    <article ref="sheet" class="doc-sheet">
      <div v-if="runningHead" class="doc-runhead">
        <span>{{ runningHead[0] }}</span>
        <span>{{ runningHead[1] }}</span>
      </div>

      <header v-if="cover" class="doc-cover">
        <p v-if="cover.eyebrow || cover.tag" class="doc-eyebrow">
          <span v-if="cover.eyebrow">{{ cover.eyebrow }}</span>
          <span v-if="cover.tag" class="doc-tag" :data-tone="cover.tag.tone">{{ cover.tag.label }}</span>
        </p>
        <h1 v-if="cover.title" :id="titleId" class="doc-title">{{ cover.title }}</h1>
        <dl v-if="cover.fields?.length" class="doc-fields">
          <div v-for="field in cover.fields" :key="field.label">
            <dt>{{ field.label }}</dt>
            <dd :data-tone="field.tone">
              {{ field.value }}
              <small v-if="field.note">{{ field.note }}</small>
            </dd>
          </div>
        </dl>
        <!-- What the page has to say about the document as a whole. -->
        <slot name="cover-notes" />
      </header>

      <div class="doc-body aw-type-document"><slot /></div>

      <p v-if="endMark" class="doc-end"><i aria-hidden="true" />{{ endMark }}<i aria-hidden="true" /></p>
      <footer v-if="footer" class="doc-foot">
        <span>{{ footer[0] }}</span>
        <span>{{ footer[1] }}</span>
      </footer>
    </article>
  </div>
</template>

<style scoped>
.doc-desk {
  min-width: 0;
  padding: var(--aw-space-5) var(--aw-space-4) 3rem;
  border-radius: var(--aw-radius-surface);
  background: var(--aw-desk);
}

/* The sheet's margin is padding, not a width on each block, so every child
   shares one left edge and a wide table can still take the room it needs.
   `--doc-hang` is the gutter the section numbers hang in; the margin is wider
   than it, so a number never touches the sheet's edge. */
.doc-sheet {
  --doc-hang: 2.5rem;
  container: doc-sheet / inline-size;
  max-width: var(--aw-sheet-width);
  margin: 0 auto;
  padding: 2.75rem clamp(1.25rem, 9%, 4.5rem) 3rem;
  border-radius: 3px;
  background: var(--aw-sheet);
  box-shadow: var(--aw-sheet-shadow);
  color: var(--aw-ink);
}

.doc-runhead {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: .25rem 1rem;
  margin-bottom: 3rem; padding-bottom: .625rem;
  border-bottom: 1px solid var(--aw-border);
  color: var(--aw-muted); font-size: var(--aw-text-2xs);
}

.doc-cover { margin-bottom: 1.25rem; }
.doc-eyebrow {
  display: flex; flex-wrap: wrap; align-items: center; gap: .625rem;
  margin: 0 0 .75rem;
  color: var(--aw-teal); font-size: var(--aw-text-sm); font-weight: 650;
}
.doc-tag {
  padding: 0 .55rem;
  border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-pill);
  color: var(--aw-ink-soft); font-size: var(--aw-text-xs); font-weight: 650;
}
.doc-tag[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.doc-tag[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); color: var(--aw-ok); }
.doc-title {
  margin: 0; scroll-margin-top: .75rem;
  color: var(--aw-ink-strong);
  font-family: var(--aw-font-serif); font-size: var(--aw-text-3xl); font-weight: 600;
  letter-spacing: -0.012em; line-height: 1.12;
}
/* Document control. A rule above it, as a printed cover has, rather than a
   card: it is part of the sheet, not something placed on it. */
.doc-fields {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr));
  margin: 1.875rem 0 0;
  border-top: 2px solid var(--aw-ink);
}
.doc-fields > div { min-width: 0; padding: .625rem .875rem 0 0; }
.doc-fields dt { margin: 0 0 .2rem; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.doc-fields dd {
  display: flex; flex-direction: column;
  margin: 0; color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 600; line-height: 1.35;
}
.doc-fields dd small { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 400; }
.doc-fields dd[data-tone='ok'] { color: var(--aw-ok); }
.doc-fields dd[data-tone='warn'] { color: var(--aw-warn-ink); }
.doc-fields dd[data-tone='agent'] { color: var(--aw-accent); }

.doc-body { min-width: 0; }

.doc-end {
  display: flex; align-items: center; gap: .75rem;
  margin: 2.5rem 0 0;
  color: var(--aw-muted); font-size: var(--aw-text-2xs);
}
.doc-end i { flex: 1; height: 1px; background: var(--aw-border); }
.doc-foot {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: .5rem 1rem;
  margin-top: 1.5rem; padding-top: .625rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-muted); font-size: var(--aw-text-2xs);
}

@media print {
  .doc-desk { padding: 0; border-radius: 0; background: none; }
  .doc-sheet { max-width: none; padding: 0; border-radius: 0; box-shadow: none; }
  .doc-runhead { margin-bottom: 2rem; }
}
</style>
