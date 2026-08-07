<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Tag from 'primevue/tag'

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
import { classificationMeta } from './classification'

// The memo is one markdown document with embedded result references. It is
// rendered by splitting at the embed fences and putting the real component
// between the prose segments — `MarkdownView` emits a single v-html string and
// cannot host a component inline, and teaching it to would mean parsing
// markdown twice.
//
// Only what the memo actually cites is fetched, one bounded request per embed.
// A memo cites a handful of results, not every procedure in the engagement.
const props = defineProps<{
  workspace: WorkspaceSummary
  markdown: string
  analyses: SavedAnalysis[]
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
</script>

<template>
  <article class="memo">
    <template v-for="(segment, index) in segments" :key="index">
      <MarkdownView v-if="segment.kind === 'prose'" :markdown="segment.text" />

      <figure v-else class="memo-embed">
        <button
          type="button"
          class="memo-embed-head"
          @click="emit('open', segment.embed.analysis)"
        >
          <span class="memo-embed-title">
            {{ byId[segment.embed.analysis]?.title || segment.embed.analysis }}
          </span>
          <Tag
            v-if="byId[segment.embed.analysis]"
            :value="classificationMeta(byId[segment.embed.analysis]!.classification).label"
            :severity="classificationMeta(byId[segment.embed.analysis]!.classification).severity"
          />
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
.memo { max-width: 62rem; }
.memo-embed {
  margin: var(--aw-space-4) 0;
  padding: var(--aw-space-3);
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
}
.memo-embed-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aw-space-3);
  width: 100%;
  margin-bottom: var(--aw-space-2);
  padding: 0;
  background: none;
  border: 0;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.memo-embed-title { font-weight: 600; font-size: var(--aw-text-sm); }
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
