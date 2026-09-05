<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import type {
  AnalysisDetail,
  AnalysisExceptions,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import ChartView from '../ChartView.vue'
import FrameTable from '../FrameTable.vue'
import MarkdownView from '../MarkdownView.vue'
import { headingCount, markdownBlocks } from '../ui/markdownBlocks'
import type { OutlineEntry } from '../ui/markdownOutline'
import { classificationTone, foundSummary } from './analysisStatus'

// The memo is one markdown document with embedded result references. It is
// rendered by splitting at the embed fences and putting the real component
// between the prose segments — `MarkdownView` emits a single v-html string and
// cannot host a component inline, and teaching it to would mean parsing
// markdown twice.
//
// The prose goes through the same heading split the memorandum uses, so every
// `##` is an element with the id the outline beside it links to. The entries
// are computed once over the whole memo and handed down: a document rendered
// in pieces must number its duplicate headings across the whole of itself.
//
// Only what the memo actually cites is fetched, one bounded request per embed.
// A memo cites a handful of results, not every procedure in the engagement.
const props = defineProps<{
  workspace: WorkspaceSummary
  markdown: string
  analyses: SavedAnalysis[]
  /** The whole memo's headings, in document order. */
  entries: OutlineEntry[]
  /** The document's eyebrow, as `UiMarkdownDocument` draws it. */
  eyebrow?: string
}>()
const emit = defineEmits<{ open: [analysisId: string] }>()

type Embed = { analysis: string; as: string; caption?: string }
type Segment = { kind: 'prose'; text: string } | { kind: 'embed'; embed: Embed }

const EMBED_BLOCK = /^```embed[ \t]*\n([\s\S]*?)^```[ \t]*$/gm

function parseEmbed(body: string): Embed | null {
  const fields: Record<string, string> = {}
  for (const line of body.split('\n')) {
    const match = /^([a-z_]+)\s*:\s*(.*)$/.exec(line.trim())
    if (match) fields[match[1]] = match[2].trim()
  }
  if (!fields.analysis) return null
  return { analysis: fields.analysis, as: fields.as || 'summary_table', caption: fields.caption }
}

const segments = computed<Segment[]>(() => {
  const out: Segment[] = []
  const source = props.markdown || ''
  let cursor = 0
  EMBED_BLOCK.lastIndex = 0
  for (let match = EMBED_BLOCK.exec(source); match; match = EMBED_BLOCK.exec(source)) {
    const prose = source.slice(cursor, match.index)
    if (prose.trim()) out.push({ kind: 'prose', text: prose })
    const embed = parseEmbed(match[1])
    // A malformed directive is dropped rather than rendered as stray markup:
    // the memo still reads correctly without it.
    if (embed) out.push({ kind: 'embed', embed })
    cursor = match.index + match[0].length
  }
  const tail = source.slice(cursor)
  if (tail.trim()) out.push({ kind: 'prose', text: tail })
  return out
})

const byId = computed(() => {
  const map: Record<string, SavedAnalysis> = {}
  for (const item of props.analyses) map[item.id] = item
  return map
})

const details = ref<Record<string, AnalysisDetail>>({})
const flagged = ref<Record<string, AnalysisExceptions>>({})
const failed = ref<Set<string>>(new Set())

/** Fetch only what each embed needs: a chart wants a frame, a table wants rows. */
async function load(embeds: Embed[]) {
  const base = `/api/workspaces/${props.workspace.id}/analyses`
  const wantDetail = embeds
    .filter(item => item.as === 'chart' || item.as === 'summary_table')
    .map(item => item.analysis)
    .filter(id => !(id in details.value) && !failed.value.has(id))
  const wantFlagged = embeds
    .filter(item => item.as === 'exception_table')
    .map(item => item.analysis)
    .filter(id => !(id in flagged.value) && !failed.value.has(id))

  const [detailed, exceptions] = await Promise.all([
    Promise.all(
      [...new Set(wantDetail)].map(id =>
        api.get<AnalysisDetail>(`${base}/${id}`)
          .then(value => ({ id, value }))
          .catch(() => ({ id, value: null })),
      ),
    ),
    Promise.all(
      [...new Set(wantFlagged)].map(id =>
        api.get<AnalysisExceptions>(`${base}/${id}/exceptions`)
          .then(value => ({ id, value }))
          .catch(() => ({ id, value: null })),
      ),
    ),
  ])

  const nextDetails = { ...details.value }
  const nextFlagged = { ...flagged.value }
  const nextFailed = new Set(failed.value)
  for (const entry of detailed) {
    if (entry.value) nextDetails[entry.id] = entry.value
    else nextFailed.add(entry.id)
  }
  for (const entry of exceptions) {
    if (entry.value) nextFlagged[entry.id] = entry.value
    else nextFailed.add(entry.id)
  }
  details.value = nextDetails
  flagged.value = nextFlagged
  failed.value = nextFailed
}

watch(
  () => segments.value.filter(item => item.kind === 'embed').map(item => (item as { embed: Embed }).embed),
  embeds => void load(embeds),
  { immediate: true, deep: true },
)

function statsFor(id: string) {
  return byId.value[id]?.last_result?.stats ?? []
}

/**
 * Each prose segment, split at its headings against the whole memo's entries.
 *
 * The offset is what keeps the ids in step with the outline: a segment does
 * not know how many headings came before it, so it is told.
 */
const prose = computed(() => {
  const out = new Map<number, ReturnType<typeof markdownBlocks>>()
  let offset = 0
  segments.value.forEach((segment, index) => {
    if (segment.kind !== 'prose') return
    out.set(index, markdownBlocks(segment.text, props.entries.slice(offset)))
    offset += headingCount(segment.text)
  })
  return out
})
</script>

<template>
  <article class="memo document">
    <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
    <template v-for="(segment, index) in segments" :key="index">
      <template v-if="segment.kind === 'prose'">
        <template v-for="(block, position) in prose.get(index) ?? []" :key="position">
          <component
            :is="`h${block.entry.level}`"
            v-if="block.entry"
            :id="block.entry.id"
            :class="`heading level-${block.entry.level}`"
          >{{ block.entry.text }}</component>
          <MarkdownView v-if="block.body" :markdown="block.body" class="body" />
        </template>
      </template>

      <!-- A cited result, drawn as a citation: which procedure produced it,
           what it concluded, and the way back to it. The embed fence used to
           drop a bare table between two paragraphs with the title alone above
           it, so a reader could not tell a failing result from a clean one
           without opening it. -->
      <figure v-else class="memo-embed">
        <button
          type="button"
          class="memo-embed-head"
          @click="emit('open', segment.embed.analysis)"
        >
          <span
            v-if="byId[segment.embed.analysis]"
            class="memo-embed-dot"
            :data-tone="classificationTone(byId[segment.embed.analysis]!.classification)"
            aria-hidden="true"
          />
          <span class="memo-embed-id">{{ segment.embed.analysis }}</span>
          <span class="memo-embed-title">
            {{ byId[segment.embed.analysis]?.title || segment.embed.analysis }}
          </span>
          <span class="memo-embed-grow" />
          <span v-if="byId[segment.embed.analysis]" class="memo-embed-found aw-figure">
            {{ foundSummary(byId[segment.embed.analysis]!) }}
          </span>
          <i class="pi pi-chevron-right" aria-hidden="true" />
        </button>

        <div v-if="failed.has(segment.embed.analysis)" class="memo-embed-error">
          <i class="pi pi-exclamation-triangle" /> Could not load this result
        </div>

        <template v-else-if="segment.embed.as === 'stats'">
          <dl class="memo-stats">
            <div v-for="stat in statsFor(segment.embed.analysis)" :key="stat.label">
              <dt>{{ stat.label }}</dt>
              <dd>{{ stat.value }}</dd>
            </div>
          </dl>
        </template>

        <template v-else-if="segment.embed.as === 'exception_table'">
          <FrameTable
            v-if="flagged[segment.embed.analysis]?.frame"
            :frame="flagged[segment.embed.analysis]!.frame!"
            scrollHeight="260px"
          />
          <p v-else-if="flagged[segment.embed.analysis]" class="memo-embed-note">
            This result recorded no flagged rows.
          </p>
          <div v-else class="memo-embed-loading"><i class="pi pi-spin pi-spinner" /></div>
        </template>

        <template v-else>
          <ChartView
            v-if="segment.embed.as === 'chart' && details[segment.embed.analysis]?.frame"
            :frame="details[segment.embed.analysis]!.frame!"
            :viz="details[segment.embed.analysis]!.viz"
            height="240px"
          />
          <FrameTable
            v-else-if="details[segment.embed.analysis]?.frame"
            :frame="details[segment.embed.analysis]!.frame!"
            scrollHeight="260px"
          />
          <div v-else class="memo-embed-loading"><i class="pi pi-spin pi-spinner" /></div>
        </template>

        <figcaption v-if="segment.embed.caption">{{ segment.embed.caption }}</figcaption>
      </figure>
    </template>

    <p v-if="!segments.length" class="memo-empty">This summary is empty.</p>
  </article>
</template>

<style scoped>
/* The memorandum's own card, on the same measure and the same padding, because
   it is the same kind of thing: a written work product with a provenance
   story. The measure is padding rather than a width, so the prose sits in one
   column and a wide evidence table can still use the room beside it — which is
   what the uncapped full-bleed version was really for. */
.document {
  min-width: 0;
  padding-block: 1.5rem;
  padding-inline: max(1.25rem, calc((100% - 96ch) / 2));
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  font-size: var(--aw-text-md);
  line-height: 1.65;
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

/* Tables the memo writes itself — a data-received summary, a coverage matrix —
   scroll rather than forcing the page to. */
.memo :deep(.markdown-view) { overflow-x: auto; }
.memo :deep(.markdown-view table) { min-width: max-content; }
.memo-embed {
  margin: var(--aw-space-4) 0;
  padding: var(--aw-space-3);
  overflow: hidden;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
.memo-embed-head {
  display: flex;
  align-items: center;
  gap: var(--aw-space-2);
  width: 100%;
  margin: calc(var(--aw-space-3) * -1) calc(var(--aw-space-3) * -1) var(--aw-space-2);
  padding: var(--aw-space-2) var(--aw-space-3);
  width: calc(100% + var(--aw-space-3) * 2);
  border: 0; border-bottom: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control) var(--aw-radius-control) 0 0;
  background: var(--aw-raised);
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.memo-embed-dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.memo-embed-dot[data-tone='ok'] { background: var(--aw-ok); }
.memo-embed-dot[data-tone='warn'] { background: var(--aw-warn); }
.memo-embed-dot[data-tone='bad'] { background: var(--aw-danger); }
.memo-embed-id { flex: none; color: var(--aw-ink-soft); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap; }
.memo-embed-title { min-width: 0; overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.memo-embed-grow { flex: 1; }
.memo-embed-found { color: var(--aw-muted); font-size: var(--aw-text-xs); white-space: nowrap; }
.memo-embed-head .pi { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.memo-embed-head:hover .memo-embed-title { text-decoration: underline; }
.memo-embed figcaption {
  margin-top: var(--aw-space-2);
  color: var(--aw-muted);
  font-size: var(--aw-text-xs);
}
.memo-embed-loading, .memo-embed-error {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.4rem;
  min-height: 6rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-sm);
}
.memo-embed-error { color: var(--aw-danger); }
.memo-embed-note { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.memo-stats { display: flex; flex-wrap: wrap; gap: var(--aw-space-4); margin: 0; }
.memo-stats dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.memo-stats dd { margin: 0; font-size: var(--aw-text-md); font-weight: 600; }
.memo-empty { color: var(--aw-muted); }
</style>
