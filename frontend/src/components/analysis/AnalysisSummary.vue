<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'

import { api } from '../../api'
import type { AnalysisMemo, SavedAnalysis, WorkspaceSummary } from '../../types'
import { plural } from '../../format'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import MemoView from './MemoView.vue'
import UiDocumentPage from '../ui/UiDocumentPage.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiVerdictBar from '../ui/UiVerdictBar.vue'
import { markdownOutline } from '../ui/markdownOutline'
import { stamp } from '../report/reportStatus'
import { classificationTone, foundSummary, holdsExceptions } from './analysisStatus'

/**
 * The Summary screen: an auditor's account of the analysis performed, the
 * exceptions noted, and what remains.
 *
 * It is a long written work product with a provenance story, which is exactly
 * what the audit planning memorandum is — so it takes that page. It used to be
 * rendered full-bleed across the pane: 1,363 words at whatever measure the
 * window gave it, with no outline to reach a section, no card, and nothing
 * anywhere saying what it was written from or what it left out.
 *
 * It is deliberately not a gallery of every procedure. That is the Procedures
 * screen, and a wall of charts is a worse answer to "what did the analysis
 * find" than four paragraphs that say so. Nothing is recomputed to draw this
 * page except the handful of results the memo actually embeds.
 */
const props = defineProps<{ workspace: WorkspaceSummary; analyses: SavedAnalysis[] }>()
const emit = defineEmits<{
  open: [analysisId: string]
  regenerate: []
  /** The header owns the primary, so it has to be told whether one exists. */
  loaded: [hasMemo: boolean]
}>()

const nav = useWorkspaceNav()
const memo = ref<AnalysisMemo | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    memo.value = await api.get<AnalysisMemo>(
      `/api/workspaces/${props.workspace.id}/analyses/memo`,
    )
  } catch {
    memo.value = null
  } finally {
    loading.value = false
    emit('loaded', hasMemo.value)
  }
}
watch(() => [props.workspace.id, props.analyses.length], () => void load(), { immediate: true })

const hasMemo = computed(() => Boolean(memo.value?.markdown?.trim()))
const markdown = computed(() => memo.value?.markdown ?? '')
const entries = computed(() => markdownOutline(markdown.value))
// The same stamp the memorandum and the report use, so one engagement does
// not date its work products three different ways.
const written = computed(() => stamp(memo.value?.generated_at))
const words = computed(() => markdown.value.split(/\s+/).filter(Boolean).length)
const sections = computed(() => entries.value.filter(entry => entry.level <= 2).length)

const byId = computed(() => {
  const map: Record<string, SavedAnalysis> = {}
  for (const item of props.analyses) map[item.id] = item
  return map
})
const cited = computed(() =>
  (memo.value?.cited_analysis_ids ?? []).map(id => byId.value[id]).filter(Boolean))
/**
 * The procedures the memo does not mention.
 *
 * A clean one changes nothing by its absence; one that found something and is
 * not cited is a gap in the account, which is why the card names what each of
 * them concluded rather than only counting them.
 */
const uncited = computed(() => {
  const seen = new Set(memo.value?.cited_analysis_ids ?? [])
  return props.analyses.filter(item => !seen.has(item.id))
})
const uncitedFlagging = computed(() => uncited.value.filter(holdsExceptions))

/** A section reporting an exception takes a dot, as the report's do. */
const marks = computed(() => {
  const out: Record<string, 'bad' | 'warn'> = {}
  const flagged = new Set(cited.value.filter(holdsExceptions).map(item => item.id))
  if (!flagged.size) return out
  let heading: string | null = null
  let fenced = false
  for (const raw of markdown.value.split('\n')) {
    const line = raw.trimEnd()
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; continue }
    if (fenced) {
      // Which procedures an embed fence names is what marks the heading above
      // it: the prose cites ids in brackets too, but a fence is the memo
      // asserting that this section rests on this result.
      const match = /^analysis:\s*(\S+)/.exec(line)
      if (match && heading && flagged.has(match[1])) out[heading] = 'bad'
      continue
    }
    const found = entries.value.find(entry => line === `${'#'.repeat(entry.level)} ${entry.text}`)
    if (found) heading = found.id
  }
  return out
})

const outstanding = computed(() => props.analyses.filter(item => item.state !== 'current').length)
const staleSentence = computed(() => {
  if (memo.value?.stale) {
    return 'The procedures have changed since this was written. Regenerate it to describe '
      + 'the current results.'
  }
  if (!outstanding.value) return ''
  return `${plural(outstanding.value, 'procedure')} `
    + `${outstanding.value === 1 ? 'has' : 'have'} no current result. Run them, then `
    + 'regenerate, or this describes results that no longer stand.'
})
</script>

<template>
  <template v-if="hasMemo">
    <!-- Who wrote it and what it rests on, in the band the memorandum uses.
         Staleness is its strip rather than a separate banner above the page:
         it qualifies everything below it. -->
    <UiVerdictBar tone="ok" :stale="staleSentence">
      <template #found>
        <span>Written by the assistant {{ written }}</span>
        <span class="meta aw-figure">
          · {{ words.toLocaleString() }} words in {{ plural(sections, 'section') }}
          · cites {{ cited.length }} of {{ analyses.length }} procedures
        </span>
      </template>
      <template #recorded>
        <template v-if="uncitedFlagging.length">
          <b>{{ plural(uncitedFlagging.length, 'procedure') }}</b> that found something
          {{ uncitedFlagging.length === 1 ? 'is' : 'are' }} not cited.
        </template>
        <template v-else-if="uncited.length">
          {{ plural(uncited.length, 'procedure') }} not cited, none of which found anything.
        </template>
        <template v-else>Every procedure is cited.</template>
      </template>
      <template #actions>
        <Button
          label="Regenerate"
          icon="pi pi-refresh"
          size="small"
          outlined
          severity="secondary"
          @click="emit('regenerate')"
        />
      </template>
    </UiVerdictBar>

    <UiDocumentPage
      :entries="entries"
      :marks="marks"
      outlineLabel="On this summary"
      railWidth="18.75rem"
    >
      <MemoView
        :workspace="workspace"
        :markdown="markdown"
        :analyses="analyses"
        :entries="entries"
        :eyebrow="`Analysis summary · ${workspace.name}`"
        @open="id => emit('open', id)"
      />

      <template #rail>
        <section class="card">
          <h3 class="aw-label">Results it cites <span class="count">{{ cited.length }} of {{ analyses.length }}</span></h3>
          <button
            v-for="item in cited.slice(0, 4)"
            :key="item.id"
            type="button"
            class="row"
            @click="emit('open', item.id)"
          >
            <span class="dot" :data-tone="classificationTone(item.classification)" aria-hidden="true" />
            <span class="copy">
              <span class="name">{{ item.title }}</span>
              <span class="found aw-figure">{{ item.id }} · {{ foundSummary(item) }}</span>
            </span>
          </button>
          <p v-if="cited.length > 4" class="line">
            {{ plural(cited.length - 4, 'more cited result') }}
          </p>
          <p v-if="!cited.length" class="line">None</p>
        </section>

        <section v-if="uncited.length" class="card">
          <h3 class="aw-label">Not cited <span class="count">{{ uncited.length }}</span></h3>
          <button
            v-for="item in uncited.slice(0, 3)"
            :key="item.id"
            type="button"
            class="row"
            @click="emit('open', item.id)"
          >
            <span class="dot" :data-tone="classificationTone(item.classification)" aria-hidden="true" />
            <span class="copy">
              <span class="name">{{ item.title }}</span>
              <span class="found aw-figure">{{ item.id }} · {{ foundSummary(item) }}</span>
            </span>
          </button>
          <p v-if="uncited.length > 3" class="line">
            {{ plural(uncited.length - 3, 'more') }}
          </p>
        </section>

        <section v-if="memo" class="card">
          <h3 class="aw-label">Written</h3>
          <dl class="stats">
            <div><dt>Written</dt><dd data-tone="agent">assistant · {{ written }}</dd></div>
            <div v-if="memo.run_id"><dt>Run</dt><dd>{{ memo.run_id }}</dd></div>
            <div><dt>Results read</dt><dd>{{ cited.length }}</dd></div>
          </dl>
        </section>

        <section class="card">
          <h3 class="aw-label">What this rests on</h3>
          <button type="button" class="feeds" @click="nav.push('data')">
            <span class="name">{{ plural(workspace.tables.length, 'source table') }}</span>
          </button>
          <button type="button" class="feeds" @click="nav.push('findings')">
            <span class="name">Findings</span>
          </button>
        </section>
      </template>
    </UiDocumentPage>
  </template>

  <UiEmptyState
    v-else-if="loading"
    icon="pi pi-hourglass"
    title="Loading the summary"
    description="Reading the analysis summary recorded for this engagement."
  />

  <UiEmptyState
    v-else
    icon="pi pi-file-edit"
    title="No analysis summary yet"
    description="Once the procedures have run, the assistant can write up what the analysis found — the population, the exceptions, and the work still outstanding — and cite each result where it uses it."
  >
    <Button label="Write the summary" icon="pi pi-sparkles" @click="emit('regenerate')" />
  </UiEmptyState>
</template>

<style scoped>
.meta { color: var(--aw-muted); font-size: var(--aw-text-sm); font-weight: 500; }

.card { display: flex; flex-direction: column; gap: .3rem; min-width: 0; }
.card h3 { display: flex; align-items: baseline; gap: .5rem; margin: 0 0 .1rem; }
.card h3 .count { margin-left: auto; color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); font-weight: 600; letter-spacing: 0; text-transform: none; }

.row {
  display: flex; align-items: flex-start; gap: .45rem;
  width: 100%; min-width: 0;
  padding: .35rem .5rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-ink);
  font: inherit; text-align: left; cursor: pointer;
}
.row:hover { border-color: var(--aw-teal-line); background: var(--aw-teal-soft); }
.dot { width: 8px; height: 8px; flex: none; margin-top: .35rem; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }
.copy { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1; }
.name { overflow: hidden; font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.found { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-2xs); text-overflow: ellipsis; white-space: nowrap; }

.line { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.5; }

.stats { display: grid; gap: 0; margin: 0; }
.stats > div { display: flex; align-items: baseline; justify-content: space-between; gap: .75rem; padding: .25rem 0; border-bottom: 1px solid var(--aw-border); }
.stats > div:last-child { border-bottom: 0; }
.stats dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.stats dd { margin: 0; color: var(--aw-ink); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; text-align: right; overflow-wrap: anywhere; }
.stats dd[data-tone='agent'] { color: var(--aw-accent); }

.feeds {
  display: flex; flex-direction: column; gap: .1rem;
  width: 100%; min-width: 0;
  padding: .45rem .55rem;
  border: 1px solid var(--aw-border); border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel); font: inherit; text-align: left; cursor: pointer;
}
.feeds:hover { border-color: var(--aw-teal-line); border-left-color: var(--aw-teal); }
.feeds .name { color: var(--aw-ink-strong); font-size: var(--aw-text-sm); font-weight: 600; white-space: normal; }
</style>
