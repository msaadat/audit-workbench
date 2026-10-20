<script setup lang="ts">
import { computed } from 'vue'

import MarkdownView from '../MarkdownView.vue'
import UiDocumentHeading from './UiDocumentHeading.vue'
import UiDocumentSheet from './UiDocumentSheet.vue'
import type { DocumentCover, SheetLine } from './documentSheet'
import type { DocumentOptions } from './markdownHtml'
import { markdownBlocks } from './markdownBlocks'
import { markdownOutline } from './markdownOutline'
import type { OutlineEntry } from './markdownOutline'

/**
 * A long Markdown work product rendered as a document, section by section, on
 * its sheet.
 *
 * One `MarkdownView` over the whole string would be shorter, and would give
 * the page nowhere to put anything. The report has to attach a problem to the
 * heading it is about — a rating nobody can support belongs under
 * `Audit Conclusion`, not in a list of fifty-six codes in another column — and
 * the memorandum has to be reachable by section. Both need the headings as
 * addressable elements, so the split happens here, once, and the heading is
 * rendered by this component rather than by the Markdown renderer.
 *
 * The split is also what lets a document look like one. The `#` title becomes
 * the cover; a lettered `##` a part; a numbered heading hangs its number in the
 * margin; and a finding the report assembled — a `###` whose first line is
 * its severity and reference, with `####` sections under it — is set as a
 * finding is in a printed report, its sections in a labelled column.
 */

const props = withDefaults(defineProps<{
  markdown: string
  /** The cover's eyebrow, where the page gives no `cover` of its own. */
  eyebrow?: string
  /** What the page knows about the document: who wrote it, when, for whom. */
  cover?: DocumentCover | null
  runningHead?: SheetLine | null
  footer?: SheetLine | null
  endMark?: string
  /** Document-mode rendering options: the cycle's steps, the citations. */
  document?: DocumentOptions
}>(), { eyebrow: '', cover: null, runningHead: null, footer: null, endMark: '', document: () => ({}) })

const emit = defineEmits<{ 'open-finding': [findingId: string] }>()

/**
 * The entries the outline beside this document should show. Derived here and
 * handed up, so the two can never be built from different text.
 */
const entries = computed<OutlineEntry[]>(() => markdownOutline(props.markdown))
defineExpose({ entries })

/** Split at every heading, so each heading is an element with an id. */
const blocks = computed(() => markdownBlocks(props.markdown, entries.value))

const titleBlock = computed(() => blocks.value.find(block => block.entry?.level === 1) ?? null)
const titleEntry = computed(() => titleBlock.value?.entry ?? null)
const hasParts = computed(() => entries.value.some(entry => entry.part))

const sheetCover = computed<DocumentCover | null>(() => {
  const title = titleEntry.value?.text ?? props.cover?.title
  if (!props.cover && !title && !props.eyebrow) return null
  return { eyebrow: props.eyebrow || undefined, ...props.cover, title }
})

interface FindingRow { entry: OutlineEntry; body: string }
interface FindingReference { id: string; href: string }
interface FindingBlock {
  severity: string
  reference: FindingReference | null
  intro: string
  rows: FindingRow[]
  response: string
  consolidated: string
}
type Unit =
  | { kind: 'lead'; body: string }
  | { kind: 'section'; entry: OutlineEntry; body: string }
  | { kind: 'finding'; entry: OutlineEntry; finding: FindingBlock }

/** The line `report.py` writes under every finding heading it assembles. */
const SEVERITY_LINE = /^\*\*Severity:\*\*\s*([A-Za-z]+)[^\n]*?(?:\*\*Reference:\*\*\s*([^\n]*))?\s*(?:\n|$)/
const LINK = /\[([^\]]+)\]\(([^)\s]+)\)/
const TRAILER = /^\*\*(Supporting procedures|Management response):\*\*[ \t]*/gm

function reference(text: string | undefined): FindingReference | null {
  const match = LINK.exec(text ?? '')
  if (!match) return null
  const fromHref = /[?&]finding=([^&#]+)/.exec(match[2])?.[1]
  const id = fromHref ? decodeURIComponent(fromHref) : match[1].trim().split(/\s+/).pop() ?? ''
  return id ? { id, href: match[2] } : null
}

/**
 * What the report appends to a finding's narrative: the procedures it absorbed
 * and the response to it. They are read off the end of the last section, where
 * the assembler put them, so the narrative itself is left as written.
 */
function trailers(text: string): { body: string; consolidated: string; response: string } {
  const hits = [...text.matchAll(TRAILER)]
  if (!hits.length) return { body: text, consolidated: '', response: '' }
  let consolidated = ''
  let response = ''
  hits.forEach((hit, index) => {
    const end = index + 1 < hits.length ? hits[index + 1].index : text.length
    const content = text.slice((hit.index ?? 0) + hit[0].length, end).trim()
    if (hit[1] === 'Supporting procedures') {
      consolidated = content.replace(/^the following findings were consolidated into this one\.?\s*/i, '')
    } else {
      response = content
    }
  })
  return { body: text.slice(0, hits[0].index).trimEnd(), consolidated, response }
}

const units = computed<Unit[]>(() => {
  const out: Unit[] = []
  const list = blocks.value.filter(block => block !== titleBlock.value)
  let part: OutlineEntry | null = null
  for (let index = 0; index < list.length; index += 1) {
    const block = list[index]
    if (!block.entry) { if (block.body) out.push({ kind: 'lead', body: block.body }); continue }
    if (block.entry.level <= 2) part = block.entry.part ? block.entry : (block.entry.level === 2 ? null : part)
    const severity = block.entry.level === 3 ? SEVERITY_LINE.exec(block.body) : null
    const inFindings = !hasParts.value || /finding/i.test(part?.label ?? part?.text ?? '')
    if (!severity || !inFindings) {
      out.push({ kind: 'section', entry: block.entry, body: block.body })
      continue
    }
    const rows: FindingRow[] = []
    while (index + 1 < list.length && (list[index + 1].entry?.level ?? 0) >= 4) {
      index += 1
      rows.push({ entry: list[index].entry as OutlineEntry, body: list[index].body })
    }
    let intro = block.body.slice(severity[0].length).trim()
    let tail = { body: '', consolidated: '', response: '' }
    if (rows.length) {
      tail = trailers(rows[rows.length - 1].body)
      rows[rows.length - 1] = { ...rows[rows.length - 1], body: tail.body }
    } else {
      tail = trailers(intro)
      intro = tail.body
    }
    out.push({
      kind: 'finding',
      entry: block.entry,
      finding: {
        severity: severity[1].toLowerCase(),
        reference: reference(severity[2]),
        intro,
        rows,
        response: tail.response,
        consolidated: tail.consolidated,
      },
    })
  }
  return out
})

function sentence(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1) : value
}
</script>

<template>
  <UiDocumentSheet
    :cover="sheetCover"
    :titleId="titleEntry?.id"
    :runningHead="runningHead"
    :footer="footer"
    :endMark="endMark"
  >
    <template #cover-notes>
      <!-- What the page wants to say about the document as a whole. -->
      <slot v-if="titleEntry" name="after-heading" :entry="titleEntry" />
    </template>

    <MarkdownView v-if="titleBlock?.body" :markdown="titleBlock.body" :document="document" />

    <template v-for="(unit, index) in units" :key="unit.kind === 'lead' ? `lead-${index}` : unit.entry.id">
      <MarkdownView v-if="unit.kind === 'lead'" :markdown="unit.body" :document="document" />

      <template v-else-if="unit.kind === 'section'">
        <UiDocumentHeading :entry="unit.entry" :parts="hasParts" />
        <!-- What the page wants to say about this heading, said under it. -->
        <slot name="after-heading" :entry="unit.entry" />
        <MarkdownView v-if="unit.body" :markdown="unit.body" :document="document" />
        <slot name="after-body" :entry="unit.entry" :body="unit.body" />
      </template>

      <section v-else class="doc-finding">
        <div class="doc-finding-head">
          <span v-if="unit.entry.number" class="doc-finding-no">Finding {{ unit.entry.number }}</span>
          <span class="aw-severity" :data-sev="unit.finding.severity">{{ sentence(unit.finding.severity) }}</span>
          <a
            v-if="unit.finding.reference"
            class="doc-finding-ref"
            :href="unit.finding.reference.href"
            @click.prevent="emit('open-finding', unit.finding.reference.id)"
          >{{ unit.finding.reference.id }}</a>
        </div>
        <h3 :id="unit.entry.id" class="doc-finding-title">{{ unit.entry.label ?? unit.entry.text }}</h3>
        <slot name="after-heading" :entry="unit.entry" />
        <MarkdownView v-if="unit.finding.intro" :markdown="unit.finding.intro" :document="document" />
        <dl v-if="unit.finding.rows.length || unit.finding.response" class="doc-finding-grid">
          <template v-for="row in unit.finding.rows" :key="row.entry.id">
            <dt :id="row.entry.id" :class="{ act: /recommend|remed/i.test(row.entry.text) }">{{ row.entry.label ?? row.entry.text }}</dt>
            <dd><MarkdownView v-if="row.body" :markdown="row.body" :document="document" /></dd>
          </template>
          <template v-if="unit.finding.response">
            <dt>Management response</dt>
            <dd><MarkdownView :markdown="unit.finding.response" :document="document" /></dd>
          </template>
        </dl>
        <div v-if="unit.finding.consolidated" class="doc-consolidated">
          <p class="doc-consolidated-label">Consolidated from</p>
          <MarkdownView :markdown="unit.finding.consolidated" />
        </div>
      </section>
    </template>

    <p v-if="!units.length && !titleEntry" class="empty">Nothing drafted yet.</p>
  </UiDocumentSheet>
</template>

<style scoped>
.empty { margin: 0; color: var(--aw-muted); font-family: var(--aw-font-sans); font-size: var(--aw-text-sm); }

/* A finding as a printed report sets one: a rule, its number, severity and
   reference on one line, the title, and its sections in a labelled column. */
.doc-finding { margin: 2rem 0 0; padding-top: .75rem; border-top: 2px solid var(--aw-ink); }
.doc-finding-head {
  display: flex; flex-wrap: wrap; align-items: center; gap: .625rem;
  font-family: var(--aw-font-sans);
  break-after: avoid;
}
.doc-finding-no { color: var(--aw-teal); font-size: var(--aw-text-xs); font-weight: 700; }
.doc-finding-ref {
  margin-left: auto;
  color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs);
  text-decoration: none;
}
.doc-finding-ref:hover { text-decoration: underline; }
.doc-finding-title {
  margin: .625rem 0 .75rem; scroll-margin-top: .75rem;
  color: var(--aw-ink-strong);
  font-family: var(--aw-font-serif); font-size: var(--aw-text-xl); font-weight: 600; line-height: 1.3;
  break-after: avoid;
}
.doc-finding-grid {
  display: grid; grid-template-columns: minmax(5.5rem, 7.75rem) minmax(0, 1fr); gap: 0 1.25rem;
  margin: 0;
}
.doc-finding-grid > dt, .doc-finding-grid > dd { margin: 0; padding: .75rem 0; border-top: 1px solid var(--aw-border); min-width: 0; }
.doc-finding-grid > dt {
  scroll-margin-top: .75rem;
  color: var(--aw-ink-soft); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-weight: 650; line-height: 1.6;
}
.doc-finding-grid > dt.act { color: var(--aw-teal); }
.doc-finding-grid > dd :deep(.markdown-view > :last-child) { margin-bottom: 0; }
.doc-consolidated {
  margin-top: .75rem; padding-top: .625rem;
  border-top: 1px solid var(--aw-border);
  color: var(--aw-ink-soft); font-family: var(--aw-font-sans); font-size: var(--aw-text-sm);
}
.doc-consolidated-label { margin: 0 0 .25rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 650; }
.doc-consolidated :deep(ul) { margin: 0; padding-left: 1.1rem; }

@container doc-sheet (max-width: 34rem) {
  .doc-finding-grid { grid-template-columns: minmax(0, 1fr); }
  .doc-finding-grid > dd { padding-top: 0; border-top: 0; }
}
</style>
