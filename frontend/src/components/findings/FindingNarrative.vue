<script setup lang="ts">
import { computed } from 'vue'

import MarkdownView from '../MarkdownView.vue'

/**
 * The narrative as the document the report will copy.
 *
 * It used to be a 26rem Markdown editor that was open whether or not anybody
 * was writing — so the page's largest surface showed the finding as source
 * rather than as the paragraphs it becomes, and the section headings the
 * template guarantees were `## Condition` in a monospace box.
 *
 * Rendering it by section rather than in one pass buys the one thing a single
 * `MarkdownView` cannot do: the deferred root cause is a state of that
 * section, and saying so anywhere else — a checkbox under the editor, which is
 * where it lived — separates the fact from the heading it qualifies.
 */

const props = defineProps<{
  markdown: string
  causePending: boolean
}>()
defineEmits<{ recordCause: [] }>()

/** The same set the backend defers with, in the same comparison. */
const CAUSE_KEYS = new Set(['cause', 'root cause'])
function sectionKey(heading: string) {
  return heading.split(/\s+/).filter(Boolean).join(' ').toLowerCase()
}

/**
 * Sections split at `##`, with the authoring comments the template carries
 * removed — `MarkdownView` escapes HTML, so a guidance comment left in would
 * render as visible instructions in the middle of report text.
 */
const sections = computed(() => {
  const clean = (props.markdown || '').replace(/<!--[\s\S]*?-->/g, '')
  const out: Array<{ heading: string; body: string; cause: boolean }> = []
  let current: { heading: string; body: string[]; cause: boolean } | null = null
  for (const line of clean.split('\n')) {
    const heading = /^##\s+(.+?)\s*$/.exec(line)
    if (heading) {
      if (current) out.push({ heading: current.heading, body: current.body.join('\n').trim(), cause: current.cause })
      current = { heading: heading[1], body: [], cause: CAUSE_KEYS.has(sectionKey(heading[1])) }
      continue
    }
    if (current) current.body.push(line)
    // Anything before the first heading is preamble the template does not
    // declare; it is still the auditor's text, so it is shown unheaded.
    else if (line.trim()) out.push({ heading: '', body: line, cause: false })
  }
  if (current) out.push({ heading: current.heading, body: current.body.join('\n').trim(), cause: current.cause })
  return out
})
</script>

<template>
  <div class="narrative">
    <section v-for="(section, index) in sections" :key="`${section.heading}:${index}`">
      <h3 v-if="section.heading">{{ section.heading }}</h3>
      <!-- The deferral replaces the section's body rather than sitting beside
           it: while the cause is pending there is nothing else true to say
           there, and the report carries the section empty. -->
      <p v-if="section.cause && causePending" class="pending">
        <span>Pending auditor follow-up. The report will carry this section empty until a cause is recorded.</span>
        <button type="button" @click="$emit('recordCause')">Record the cause</button>
      </p>
      <MarkdownView
        v-if="section.body && !(section.cause && causePending)"
        :markdown="section.body"
        class="body"
      />
    </section>
    <p v-if="!sections.length" class="empty">This finding has no narrative yet.</p>
  </div>
</template>

<style scoped>
.narrative { display: flex; flex-direction: column; gap: .875rem; min-width: 0; }
.narrative h3 {
  margin: 0 0 .35rem;
  color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600;
  letter-spacing: -0.01em;
}
.body { color: var(--aw-ink); font-size: var(--aw-text-base); line-height: 1.6; }
.body :deep(p:first-child) { margin-top: 0; }
.body :deep(table) { border-radius: var(--aw-radius-control); overflow: hidden; }

.pending {
  display: flex; align-items: center; gap: .75rem; flex-wrap: wrap;
  margin: .25rem 0 0;
  padding: .5rem .75rem;
  border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-size: var(--aw-text-sm); line-height: 1.45;
}
.pending button {
  flex: none; padding: 0; border: 0; background: none;
  color: var(--aw-warn-ink); font: inherit; font-weight: 700;
  text-decoration: underline; text-underline-offset: 2px; cursor: pointer;
}
.empty { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
</style>
