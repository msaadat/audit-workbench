<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ContextRead } from '../../types'
import { plural } from '../../format'

/**
 * What the run in flight read, lifted out of the transcript into the rail.
 *
 * In the transcript it is a card the reader scrolls past once and then cannot
 * find again — and while a run is working, "which documents is this being
 * decided from" is a live question, not a historical one. The transcript keeps
 * it for finished runs, where it is a receipt.
 *
 * What the step declined is on the card too, in the warn tone: a scope
 * decision is the most audit-literate thing the run makes, and a list of what
 * was read says nothing about what was not.
 */

const props = withDefaults(defineProps<{
  context: ContextRead
  /** What the card is the manifest *for*; the panel decides, because only it
      knows whether this is the run being watched. */
  label?: string
}>(), { label: 'Read for this run' })

const SHOWN = 4
const expanded = ref(false)

const CATEGORY_LABELS: Record<string, string> = {
  background: 'background', policy: 'policy', regulation: 'regulation',
  contract: 'contract', minutes: 'minutes', voucher: 'voucher',
  evidence: 'evidence', prior_report: 'prior report',
  correspondence: 'letter', other: 'document',
}

/**
 * A short count line, not the manifest's prose.
 *
 * `context.sentence` is the whole manifest read aloud — it is built for
 * assistive technology, and it names every file the rows below then name
 * again. The card states the shape instead and keeps the sentence as the
 * card's description, so a screen reader still gets the full reading.
 */
const shape = computed(() => {
  const parts: string[] = []
  if (props.context.artifacts.length) parts.push(plural(props.context.artifacts.length, 'work product'))
  parts.push(plural(props.context.documents.length, 'document'))
  if (props.context.supporting.length) parts.push(plural(props.context.supporting.length, 'supporting source'))
  return parts.join(' · ')
})

const documents = computed(() => (expanded.value
  ? props.context.documents
  : props.context.documents.slice(0, SHOWN)))
const hidden = computed(() => Math.max(0, props.context.documents.length - SHOWN))

/** What was held back, named where there is one and counted where there are many. */
const withheld = computed(() => {
  const items = props.context.withheld ?? []
  if (!items.length) return ''
  const reason = items[0].reason || "outside this step's scope"
  return items.length === 1
    ? `Held back: ${items[0].name} — ${reason}.`
    : `Held back: ${plural(items.length, 'document')} — ${reason}.`
})

function when(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? ''
    : date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <section class="rail-card">
    <header>
      <h3 class="aw-label">{{ label }}</h3>
      <span v-if="when(context.at)" class="count aw-figure">at {{ when(context.at) }}</span>
    </header>

    <p class="sentence aw-figure">{{ shape }}</p>
    <p v-if="context.sentence" class="reading">{{ context.sentence }}</p>

    <button v-for="document in documents" :key="document.document_id" type="button" class="row">
      <i class="pi pi-file" aria-hidden="true" />
      <span class="name">{{ document.name }}</span>
      <span v-if="document.category" class="tag">{{ CATEGORY_LABELS[document.category] || document.category }}</span>
    </button>

    <button v-if="hidden" type="button" class="more" @click="expanded = !expanded">
      <i class="pi" :class="expanded ? 'pi-chevron-down' : 'pi-chevron-right'" aria-hidden="true" />
      {{ expanded ? 'Fewer documents' : plural(hidden, 'more document') }}
    </button>

    <p v-if="withheld" class="withheld">{{ withheld }}</p>
  </section>
</template>

<style scoped>
.sentence { margin: 0 0 .25rem; color: var(--aw-ink-soft); font-size: var(--aw-text-xs); line-height: 1.45; }
/* The manifest read aloud. It names every file the rows below name, so it is
   the screen reader's copy rather than the page's. */
.reading { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.row {
  display: flex; align-items: center; gap: .4rem;
  width: 100%; min-width: 0;
  padding: .3rem 0;
  border: 0; border-top: 1px solid var(--aw-border);
  background: none; color: var(--aw-ink); font: inherit; font-size: var(--aw-text-xs);
  text-align: left; cursor: default;
}
.row .pi { color: var(--aw-teal); font-size: .7rem; }
.row .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag { flex: none; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.more {
  display: inline-flex; align-items: center; gap: .3rem;
  margin-top: .25rem; padding: 0;
  border: 0; background: none; color: var(--aw-teal);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600; cursor: pointer;
}
.more .pi { font-size: .55rem; }
.withheld {
  margin: .4rem 0 0; padding-top: .4rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-warn-ink); font-size: var(--aw-text-2xs); line-height: 1.45;
}
</style>
