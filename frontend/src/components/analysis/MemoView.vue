<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import { api } from '../../api'
import type {
  AnalysisDetail,
  AnalysisExceptions,
  SavedAnalysis,
  WorkspaceSummary,
} from '../../types'
import MarkdownView from '../MarkdownView.vue'
import UiDocumentHeading from '../ui/UiDocumentHeading.vue'
import UiDocumentSheet from '../ui/UiDocumentSheet.vue'
import type { DocumentCover, SheetLine } from '../ui/documentSheet'
import { headingCount, markdownBlocks } from '../ui/markdownBlocks'
import type { OutlineEntry } from '../ui/markdownOutline'
import { classificationTone, foundSummary } from './analysisStatus'
import MemoExhibitBody from './MemoExhibitBody.vue'
import { memoCitations, memoSegments, memoUnits } from './memoExhibits'
import type { Embed, Exhibit, MemoUnit } from './memoExhibits'

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
// On the sheet each embed is an exhibit, numbered in the order the reader
// meets it, and embeds with nothing written between them are one set — the
// memo cites one finding's procedures together, and eight near-identical
// one-row tables in a column are a wall, not evidence. A procedure the prose
// cites becomes a superscript to its exhibit, or a chip where it has none.
//
// Only what the memo actually cites is fetched, one bounded request per embed.
// A memo cites a handful of results, not every procedure in the engagement.
const props = withDefaults(defineProps<{
  workspace: WorkspaceSummary
  markdown: string
  analyses: SavedAnalysis[]
  /** The whole memo's headings, in document order. */
  entries: OutlineEntry[]
  cover?: DocumentCover | null
  runningHead?: SheetLine | null
  footer?: SheetLine | null
  endMark?: string
}>(), { cover: null, runningHead: null, footer: null, endMark: '' })
const emit = defineEmits<{ open: [analysisId: string] }>()

const segments = computed(() => memoSegments(props.markdown))
const units = computed(() => memoUnits(segments.value))
const renderOptions = computed(() => ({ citations: memoCitations(props.analyses, units.value) }))

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

/** The memo opens with its summary paragraph before any heading: the lede. */
const firstProse = computed(() => units.value.find(unit => unit.kind === 'prose') as
  Extract<MemoUnit, { kind: 'prose' }> | undefined)
function isLede(unit: MemoUnit, position: number): boolean {
  return unit === firstProse.value && position === 0 && !prose.value.get(unit.index)?.[0]?.entry
}

/** The figures beside the lede, counted off the procedures the page holds. */
const figures = computed(() => {
  const count = (classification: string) => props.analyses.filter(item => item.classification === classification).length
  const unusual = count('unusual')
  return [
    // Received, not derived: a join is a frame the analysis built, and the
    // memo's own data-received table counts only what was imported.
    { value: props.workspace.tables.filter(table => table.kind === 'file').length, label: 'tables received' },
    { value: props.analyses.filter(item => item.classification !== 'not_run').length, label: 'procedures run' },
    { value: count('exception'), label: 'flag exceptions', tone: 'bad' },
    { value: count('clear'), label: unusual ? `clean, ${unusual} unusual` : 'clean', tone: 'ok' },
  ]
})

/** Which exhibit of each set is open. The first is, until the reader picks another. */
const openExhibit = ref<Record<string, number>>({})
function isOpen(unit: Extract<MemoUnit, { kind: 'exhibits' }>, exhibit: Exhibit): boolean {
  return (openExhibit.value[unit.key] ?? unit.exhibits[0].number) === exhibit.number
}
function toggle(unit: Extract<MemoUnit, { kind: 'exhibits' }>, exhibit: Exhibit) {
  openExhibit.value = { ...openExhibit.value, [unit.key]: isOpen(unit, exhibit) ? 0 : exhibit.number }
}

function exhibitTitle(exhibit: Exhibit): string {
  return byId.value[exhibit.embed.analysis]?.title || exhibit.embed.analysis
}
function exhibitFound(exhibit: Exhibit): string {
  const analysis = byId.value[exhibit.embed.analysis]
  return analysis ? foundSummary(analysis) : ''
}
function exhibitTone(exhibit: Exhibit): string {
  const analysis = byId.value[exhibit.embed.analysis]
  return analysis ? classificationTone(analysis.classification) : 'neutral'
}
/** A set's title says what it holds without restating each procedure. */
function setTitle(unit: Extract<MemoUnit, { kind: 'exhibits' }>): string {
  const titles = new Set(unit.exhibits.map(exhibitTitle))
  return titles.size === 1 ? [...titles][0] : `${unit.exhibits.length} procedures`
}

/**
 * A citation in the prose: a superscript scrolls to its exhibit (and opens it
 * in its set), a chip opens the procedure. One listener on the sheet, because
 * the links are inside rendered Markdown that no component owns.
 */
const root = ref<InstanceType<typeof UiDocumentSheet> | null>(null)
async function onClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const jump = target?.closest<HTMLElement>('[data-jump]')
  if (jump) {
    event.preventDefault()
    const number = Number(jump.dataset.jump?.replace('exhibit-', ''))
    const unit = units.value.find(item => item.kind === 'exhibits' && item.exhibits.some(exhibit => exhibit.number === number))
    if (unit?.kind === 'exhibits' && unit.exhibits.length > 1) openExhibit.value = { ...openExhibit.value, [unit.key]: number }
    await nextTick()
    root.value?.sheet?.querySelector(`#exhibit-${number}`)?.scrollIntoView({ block: 'start' })
    return
  }
  const procedure = target?.closest<HTMLElement>('[data-procedure]')
  if (procedure?.dataset.procedure) {
    event.preventDefault()
    emit('open', procedure.dataset.procedure)
  }
}
</script>

<template>
  <UiDocumentSheet
    ref="root"
    class="memo"
    :cover="cover"
    :runningHead="runningHead"
    :footer="footer"
    :endMark="endMark"
    @click="onClick"
  >
    <template v-for="unit in units" :key="unit.kind === 'prose' ? `prose-${unit.index}` : unit.key">
      <template v-if="unit.kind === 'prose'">
        <template v-for="(block, position) in prose.get(unit.index) ?? []" :key="position">
          <template v-if="isLede(unit, position)">
            <div class="memo-lede">
              <p class="memo-lede-label">In brief</p>
              <MarkdownView :markdown="block.body" :document="renderOptions" class="memo-lede-text" />
            </div>
            <dl class="memo-figures">
              <div v-for="figure in figures" :key="figure.label">
                <dt>{{ figure.label }}</dt>
                <dd :data-tone="figure.value ? figure.tone : undefined">{{ figure.value }}</dd>
              </div>
            </dl>
          </template>
          <template v-else>
            <UiDocumentHeading v-if="block.entry" :entry="block.entry" />
            <MarkdownView v-if="block.body" :markdown="block.body" :document="renderOptions" />
          </template>
        </template>
      </template>

      <!-- One exhibit: which procedure produced it, what it concluded, and
           the way back to it, above the rows themselves. -->
      <figure v-else-if="unit.exhibits.length === 1" :id="`exhibit-${unit.exhibits[0].number}`" class="exhibit">
        <button type="button" class="exhibit-head" @click="emit('open', unit.exhibits[0].embed.analysis)">
          <span class="exhibit-no">Exhibit {{ unit.exhibits[0].number }}</span>
          <span class="exhibit-title">{{ exhibitTitle(unit.exhibits[0]) }}</span>
          <span v-if="exhibitFound(unit.exhibits[0])" class="exhibit-found" :data-tone="exhibitTone(unit.exhibits[0])">{{ exhibitFound(unit.exhibits[0]) }}</span>
          <span class="exhibit-id">{{ unit.exhibits[0].embed.analysis }}</span>
        </button>
        <MemoExhibitBody
          :embed="unit.exhibits[0].embed"
          :number="unit.exhibits[0].number"
          :analysis="byId[unit.exhibits[0].embed.analysis]"
          :detail="details[unit.exhibits[0].embed.analysis]"
          :flagged="flagged[unit.exhibits[0].embed.analysis]"
          :failed="failed.has(unit.exhibits[0].embed.analysis)"
        />
      </figure>

      <!-- A set: the procedures one passage cites together, one open and the
           rest a line each. Print opens them all. -->
      <figure v-else class="exhibit exhibit-set">
        <div class="exhibit-head is-static">
          <span class="exhibit-no">Exhibits {{ unit.exhibits[0].number }}–{{ unit.exhibits[unit.exhibits.length - 1].number }}</span>
          <span class="exhibit-title">{{ setTitle(unit) }}</span>
        </div>
        <template v-for="exhibit in unit.exhibits" :key="exhibit.number">
          <button
            :id="`exhibit-${exhibit.number}`"
            type="button"
            :class="['exhibit-row', { open: isOpen(unit, exhibit) }]"
            :aria-expanded="isOpen(unit, exhibit)"
            @click="toggle(unit, exhibit)"
          >
            <i :class="['aw-icon', isOpen(unit, exhibit) ? 'aw-icon-chevron-down' : 'aw-icon-chevron-right']" aria-hidden="true" />
            <span class="exhibit-no">{{ exhibit.number }}</span>
            <span class="exhibit-title">{{ exhibitTitle(exhibit) }}</span>
            <span v-if="exhibitFound(exhibit)" class="exhibit-found" :data-tone="exhibitTone(exhibit)">{{ exhibitFound(exhibit) }}</span>
            <span class="exhibit-id">{{ exhibit.embed.analysis }}</span>
          </button>
          <div v-show="isOpen(unit, exhibit)" class="exhibit-row-body">
            <MemoExhibitBody
              :embed="exhibit.embed"
              :number="exhibit.number"
              :analysis="byId[exhibit.embed.analysis]"
              :detail="details[exhibit.embed.analysis]"
              :flagged="flagged[exhibit.embed.analysis]"
              :failed="failed.has(exhibit.embed.analysis)"
            />
          </div>
        </template>
      </figure>
    </template>

    <p v-if="!segments.length" class="memo-empty">This summary is empty.</p>
  </UiDocumentSheet>
</template>

<style scoped>
/* Tables the memo writes itself — a data-received summary, a coverage matrix —
   scroll rather than forcing the sheet to. */
.memo :deep(.markdown-view) { overflow-x: auto; }

.memo-lede { margin: 2rem 0 0; padding-top: 1.125rem; border-top: 1px solid var(--aw-border); }
.memo-lede-label { margin: 0 0 .375rem; color: var(--aw-teal); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); font-weight: 650; }
.memo-lede-text { color: var(--aw-ink-strong); font-size: 1.15em; line-height: 1.55; }

.memo-figures {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr));
  margin: .75rem 0 0;
  border-top: 1px solid var(--aw-border); border-bottom: 1px solid var(--aw-border);
}
.memo-figures > div { display: flex; flex-direction: column-reverse; padding: .875rem .875rem .875rem 0; }
.memo-figures dt { margin-top: .375rem; color: var(--aw-muted); font-family: var(--aw-font-sans); font-size: var(--aw-text-xs); }
.memo-figures dd {
  margin: 0; color: var(--aw-ink-strong);
  font-family: var(--aw-font-serif); font-size: var(--aw-text-2xl); font-weight: 500; line-height: 1;
}
.memo-figures dd[data-tone='bad'] { color: var(--aw-danger); }
.memo-figures dd[data-tone='ok'] { color: var(--aw-ok); }

/* An exhibit, numbered in the order the reader meets it. */
.exhibit {
  margin: 1.125rem 0 1.25rem;
  border: 1px solid var(--aw-border); border-radius: 6px;
  overflow: hidden;
  font-family: var(--aw-font-sans);
  scroll-margin-top: .75rem;
  break-inside: avoid;
}
.exhibit-head, .exhibit-row {
  display: flex; flex-wrap: wrap; align-items: center; gap: .375rem .75rem;
  width: 100%; padding: .55rem .875rem;
  border: 0; background: var(--aw-panel-wash);
  color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.exhibit-head { border-bottom: 1px solid var(--aw-border); background: var(--aw-raised); }
.exhibit-head.is-static { cursor: default; }
.exhibit-head:not(.is-static):hover .exhibit-title { text-decoration: underline; }
.exhibit-row { border-top: 1px solid var(--aw-border); scroll-margin-top: .75rem; }
.exhibit-head + .exhibit-row { border-top: 0; }
.exhibit-row:hover { background: var(--aw-raised); }
.exhibit-row.open { background: var(--aw-raised); }
.exhibit-row .aw-icon { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.exhibit-row-body { border-top: 1px solid var(--aw-border); }
.exhibit-no { color: var(--aw-teal); font-size: var(--aw-text-xs); font-weight: 700; white-space: nowrap; }
.exhibit-title { flex: 1 1 12rem; min-width: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 650; line-height: 1.35; }
.exhibit-row .exhibit-title { font-weight: 600; }
.exhibit-found {
  padding: 0 .5rem;
  border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-pill);
  color: var(--aw-ink-soft); font-size: var(--aw-text-2xs); font-weight: 600; white-space: nowrap;
}
.exhibit-found[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.exhibit-found[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.exhibit-found[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); color: var(--aw-ok); }
.exhibit-id { color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); white-space: nowrap; }

.memo-empty { color: var(--aw-muted); font-family: var(--aw-font-sans); }

@media print {
  .exhibit-row-body { display: block !important; }
  .exhibit-row .aw-icon { display: none; }
  .exhibit-set { break-inside: auto; }
}
</style>
