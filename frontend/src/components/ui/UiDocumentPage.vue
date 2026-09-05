<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { OutlineEntry } from './markdownOutline'

/**
 * A long work product: what is in it, the document itself, and what it rests on.
 *
 * The memorandum and the report are the same shape of thing — one Markdown
 * document with a provenance story — and were built as two different pages:
 * the APM as an always-open editor beside a rail, the report as a two-mode
 * `SelectButton` where reading it and checking it were separate screens. Both
 * asked the reader to scroll 40,000 characters with no way to reach a section.
 *
 * The frame is shared and the contents are not: what the outline marks, what
 * the document is, and which cards the rail carries are each a slot, because
 * those are the parts the two pages genuinely disagree about.
 */

const props = withDefaults(defineProps<{
  entries: OutlineEntry[]
  /** What the outline is titled: `On this report`, `On this memorandum`. */
  outlineLabel: string
  /** Headings a caller wants marked — the report dots its problem sections. */
  marks?: Record<string, 'bad' | 'warn'>
  /** Badges beside a heading, e.g. `excluded` on the detailed findings. */
  badges?: Record<string, string>
  /** The rail column's width; the report's issue rail is wider than the APM's. */
  railWidth?: string
  /**
   * The middle column's content fills the height and owns its own scrolling —
   * which is what an editor needs. Left off, the column scrolls a document
   * that is as tall as it is; on, an editor inside a scrolling column would
   * give the page two scrollbars for one text.
   */
  fills?: boolean
}>(), { marks: () => ({}), badges: () => ({}), railWidth: '20rem', fills: false })

const scroller = ref<HTMLElement | null>(null)
const active = ref<string>('')

/** Where a jumped-to heading is parked, and the line tracking reads against. */
const HEADING_OFFSET = 12

/**
 * The heading element for one id, inside the scroller.
 *
 * `CSS.escape` is for identifiers, not for the string inside an attribute
 * selector: it rewrote `3-audit-conclusion` — a heading numbered in the
 * template — as `\33 -audit-conclusion`, which matched nothing, so every
 * numbered section silently refused to scroll. Only the quote and the
 * backslash need escaping here.
 */
function headingNode(root: HTMLElement, id: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[id="${id.replace(/["\\]/g, '\\$&')}"]`)
}

/**
 * Which heading the reader is at, by proximity to the top of the scroller.
 *
 * An `IntersectionObserver` would answer "which headings are visible", which is
 * a different question: in a document whose sections are longer than the
 * viewport the answer is routinely none of them.
 */
function trackActive() {
  const root = scroller.value
  if (!root) return
  // Generous enough to include a heading the outline just parked, or the entry
  // you clicked would light up the section above it.
  const top = root.getBoundingClientRect().top + HEADING_OFFSET + 8
  let current = props.entries[0]?.id ?? ''
  for (const entry of props.entries) {
    const node = headingNode(root, entry.id)
    if (node && node.getBoundingClientRect().top <= top) current = entry.id
  }
  active.value = current
}

let frame = 0
function onScroll() {
  if (frame) return
  frame = requestAnimationFrame(() => { frame = 0; trackActive() })
}
watch(scroller, (node, previous) => {
  previous?.removeEventListener('scroll', onScroll)
  node?.addEventListener('scroll', onScroll, { passive: true })
  if (node) trackActive()
})
watch(() => props.entries, () => trackActive())
onBeforeUnmount(() => {
  if (frame) cancelAnimationFrame(frame)
  scroller.value?.removeEventListener('scroll', onScroll)
})

function jump(id: string) {
  const root = scroller.value
  const node = root ? headingNode(root, id) : null
  if (!root || !node) return
  // Measured rather than read off `offsetTop`: the heading's offset parent is
  // the document card, not the scroller, so the two numbers are not in the
  // same coordinate space and their difference scrolled nowhere.
  const delta = node.getBoundingClientRect().top - root.getBoundingClientRect().top
  // Instant, not smooth: an outline entry is a jump rather than a tour, and
  // `behavior: 'smooth'` is a silent no-op in embedded browsers and under
  // reduced-motion settings — a link that does nothing is worse than one that
  // arrives without an animation.
  root.scrollTop = root.scrollTop + delta - HEADING_OFFSET
  active.value = id
}

const columns = computed(() => `13.75rem minmax(0, 1fr) ${props.railWidth}`)
</script>

<template>
  <div class="document-page" :style="{ gridTemplateColumns: columns }">
    <nav class="outline" :aria-label="outlineLabel">
      <p class="aw-label">{{ outlineLabel }}</p>
      <button
        v-for="entry in entries"
        :key="entry.id"
        type="button"
        :class="['entry', `level-${entry.level}`, { active: entry.id === active }]"
        @click="jump(entry.id)"
      >
        <span v-if="marks[entry.id]" class="mark" :data-tone="marks[entry.id]" aria-hidden="true" />
        <span class="text">{{ entry.text }}</span>
        <span v-if="badges[entry.id]" class="badge">{{ badges[entry.id] }}</span>
      </button>
      <p v-if="!entries.length" class="empty">Nothing drafted yet.</p>
      <slot name="outline-foot" />
    </nav>

    <div ref="scroller" class="middle" :class="{ fills }"><slot /></div>

    <aside class="rail"><slot name="rail" /></aside>
  </div>
</template>

<style scoped>
.document-page {
  display: grid; gap: 1.75rem;
  flex: 1; min-height: 0; min-width: 0;
  align-items: start;
}

/* Sticky rather than scrolling: the outline is shorter than the document by
   construction, and a second scrollbar beside the first is a way to lose your
   place in both. */
.outline {
  position: sticky; top: 0;
  display: flex; flex-direction: column; gap: 1px;
  min-width: 0; max-height: 100%; overflow-y: auto;
  padding-right: .5rem;
}
.outline .aw-label { margin: 0 0 .5rem; }
.entry {
  display: flex; align-items: baseline; gap: .375rem;
  width: 100%; min-width: 0;
  padding: .25rem .5rem;
  border: 0; border-left: 2px solid transparent;
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer;
}
.entry:hover { color: var(--aw-ink-strong); background: var(--aw-raised); }
.entry:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.entry.active { border-left-color: var(--aw-teal); color: var(--aw-teal-strong); font-weight: 600; }
.entry.level-3, .entry.level-4 { padding-left: 1.25rem; font-size: var(--aw-text-xs); }
.entry .text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mark { width: 7px; height: 7px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.mark[data-tone='bad'] { background: var(--aw-danger); }
.mark[data-tone='warn'] { background: var(--aw-warn); }
.badge {
  flex: none; padding: 0 .3rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-size: var(--aw-text-2xs); font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
}
.outline .empty { margin: 0; padding: .25rem .5rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }

.middle {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-height: 100%; overflow-y: auto;
  scrollbar-gutter: stable;
}
.middle.fills { overflow: hidden; }
.middle.fills > :deep(*) { flex: 1; min-height: 0; }

.rail {
  display: flex; flex-direction: column; gap: .875rem;
  min-width: 0; max-height: 100%; overflow-y: auto;
  padding: .875rem;
  border-radius: var(--aw-radius-surface); background: var(--aw-raised);
}

@container workspace-panel (max-width: 70rem) {
  .document-page { grid-template-columns: minmax(0, 1fr) !important; }
  .outline { position: static; flex-direction: row; flex-wrap: wrap; max-height: none; }
  .outline .aw-label { width: 100%; }
  .middle, .rail { max-height: none; overflow: visible; }
}
</style>
