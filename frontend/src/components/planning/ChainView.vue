<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'

import { api, ApiError } from '../../api'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type {
  AuditDocument, CriterionRef, DataTest, FindingSummary, PlanningPayload, RcmRow, WorkspaceSummary,
} from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiReviewBar from '../ui/UiReviewBar.vue'
import {
  CHAIN_CHIPS, chainLinks, chainStatus, chainSummary, chainTone, findingsFor, ranked,
  visibleRows,
} from './chainStatus'
import type { ChainFilter } from './chainStatus'
import { plural } from '../../format'

/**
 * One risk, followed from the sentence it rests on to the finding it produced.
 *
 * Every other screen in the audit file is organised by artifact kind — all the
 * tests, all the findings — which is how the work is filed and not how it is
 * questioned. The question a reviewer actually asks is vertical: what is this
 * control criterion based on, what did we do about it, and what did that show.
 * Each hop below is a record the workspace already holds; nothing here is
 * derived beyond joining them by the references they carry.
 *
 * It asks that of the whole matrix as well as of one row. The spine drew one
 * chain and said nothing about the set, so a reviewer could see that *this*
 * risk had no test and had no way to ask how many did — which on the
 * engagement this was built against is thirty of thirty-two. The review bar's
 * three lanes are the same three questions, asked of every row.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const route = useRoute()
const toast = useToast()
const nav = useWorkspaceNav()

const data = ref<PlanningPayload | null>(null)
const documents = ref<AuditDocument[]>([])
const selectedId = ref<string | null>(String(route.query.rcm || '') || null)
const search = ref('')
const anchorOpen = ref(false)
const anchor = ref<CriterionRef | null>(null)

function fail(summary: string, error: unknown) {
  toast.add({
    severity: 'error', life: 6000, summary,
    detail: error instanceof ApiError ? error.message : String(error),
  })
}

onMounted(async () => {
  try {
    const [planning, catalogue] = await Promise.all([
      api.get<PlanningPayload>(`/api/workspaces/${props.workspace.id}/planning`),
      api.get<{ items: AuditDocument[] }>(`/api/workspaces/${props.workspace.id}/documents`)
        .then(result => result.items)
        .catch(() => [] as AuditDocument[]),
    ])
    data.value = planning
    documents.value = catalogue
    if (!selectedId.value || !planning.rcm.some(row => row.id === selectedId.value)) {
      selectedId.value = shown.value[0]?.row.id ?? null
    }
  } catch (error) { fail('Could not load the chain', error) }
})

const rows = computed(() => data.value?.rcm ?? [])
const selected = computed(() => rows.value.find(row => row.id === selectedId.value) ?? null)

function documentName(id: string) {
  const found = documents.value.find(item => item.id === id)
  return found?.source || found?.title || id
}
/**
 * A risk's first sentence, which is the one that names it.
 *
 * The character cap that used to follow is gone: the row clamps to two lines
 * in CSS, so cutting the text at 120 characters as well truncated it twice —
 * once at a width the layout had not measured.
 */
function shortRisk(row: RcmRow) {
  const text = (row.risk || row.control || row.process || row.id).trim()
  return text.split(/(?<=[.!?])\s+/)[0] ?? text
}

const rollups = computed(() => data.value?.finding_rollups ?? null)

/** The lanes, the chips and the whole filter vocabulary, from one tally. */
const status = computed(() => chainStatus(rows.value, rollups.value))

// Narrowings compose across axes: "an uncovered risk that cites no source" is
// two questions about the same row and neither answers the other.
const filters = ref<ChainFilter[]>([])

const shown = computed(() => {
  const narrowed = visibleRows(rows.value, filters.value, rollups.value)
  const term = search.value.trim().toLowerCase()
  const matched = term
    ? narrowed.filter(row => `${row.id} ${row.risk} ${row.control} ${row.process}`
      .toLowerCase().includes(term))
    : narrowed
  return ranked(matched, rollups.value)
})

function pickFilters(keys: ChainFilter[]) {
  filters.value = keys
  // Keep a selection that is still visible; otherwise open the first match, so
  // a narrowing never leaves the spine showing a row it excluded.
  if (!shown.value.some(entry => entry.row.id === selectedId.value)) {
    selectedId.value = shown.value[0]?.row.id ?? null
  }
}

const linkedDataTests = computed<DataTest[]>(() =>
  (data.value?.data_tests ?? []).filter(test => test.rcm_id === selectedId.value))
const linkedDocTests = computed(() =>
  (data.value?.document_tests ?? []).filter(test =>
    test.rcm_id === selectedId.value || (test.rcm_refs ?? []).includes(selectedId.value ?? '')))
// The same index the rail counts, so a row can never show "2 find" beside an
// empty Findings hop.
const linkedFindings = computed<FindingSummary[]>(() =>
  (selectedId.value ? findingsFor(selectedId.value, rollups.value) : []))
const selectedLinks = computed(() =>
  (selected.value ? chainLinks(selected.value, rollups.value) : null))

function openAnchor(item: CriterionRef) {
  anchor.value = item
  anchorOpen.value = true
}
</script>

<template>
  <div v-if="data" class="chain-view">
    <!-- The title and nothing else: this page files nothing, so it has no
         primary, and the lede that explained what a chain was is what the
         three lanes below now say in figures. -->
    <header class="page-head">
      <h1>Chain</h1>
      <span class="grow" />
    </header>

    <!-- The spine's three questions, asked of the whole matrix. -->
    <UiReviewBar
      v-if="rows.length"
      :lanes="status.lanes"
      :chips="CHAIN_CHIPS"
      :filters="status.filters"
      allLabel="All risks"
      :total="rows.length"
      :filter="filters"
      @filter="pickFilters($event as ChainFilter[])"
    />

    <div v-if="rows.length" class="layout">
      <section class="list-panel">
        <div class="list-head">
          <IconField>
            <InputIcon class="pi pi-search" />
            <InputText v-model="search" size="small" placeholder="Search risks and controls" fluid />
          </IconField>
        </div>
        <div class="list-body">
          <div class="list">
            <button
              v-for="entry in shown"
              :key="entry.row.id"
              type="button"
              class="row"
              :class="{ active: entry.row.id === selectedId }"
              @click="selectedId = entry.row.id"
            >
              <span class="dot" :data-tone="chainTone(entry.links)" aria-hidden="true" />
              <span class="copy">
                <span class="title">{{ shortRisk(entry.row) }}</span>
                <span class="meta aw-figure">{{ chainSummary(entry.links) }}</span>
              </span>
            </button>
            <p v-if="!shown.length" class="list-empty">No risk matches this view.</p>
          </div>
        </div>
      </section>

      <section v-if="selected && selectedLinks" class="detail">
        <header class="detail-head">
          <div class="detail-copy">
            <p class="detail-id">{{ selected.id }}<template v-if="selected.process"> · {{ selected.process }}</template></p>
            <h2>{{ selected.risk }}</h2>
            <p v-if="selected.control" class="objective">{{ selected.control }}</p>
          </div>
          <Button
            label="Open in the matrix"
            icon="pi pi-map"
            size="small"
            outlined
            severity="secondary"
            @click="nav.push('rcm', { rcm: selected.id })"
          />
        </header>

        <div class="spine">
        <!-- 1 · What the criterion rests on. -->
        <article class="hop" :class="{ empty: !selectedLinks.sources }">
          <span class="dot"><i class="pi pi-file" /></span>
          <div class="hop-body">
            <h4>Source</h4>
            <template v-if="selected.criteria_refs?.length">
              <button
                v-for="item in selected.criteria_refs"
                :key="item.id"
                class="anchor"
                @click="openAnchor(item)"
              >
                <strong>{{ documentName(item.source_id) }}</strong>
                <code v-if="item.citation_id">{{ item.citation_id }}</code>
                <small v-if="item.page">page {{ item.page }}</small>
                <q v-if="item.excerpt">{{ item.excerpt }}</q>
              </button>
            </template>
            <p v-else class="muted">
              No cited source. The criterion reads
              <em v-if="selected.criteria">“{{ selected.criteria }}”</em>
              <em v-else>as unset</em>, which points at a document in prose but
              carries no anchor to open.
            </p>
          </div>
        </article>

        <!-- 2 · What was done about it. The criterion itself was a hop here
             and is the detail head now: the risk and the control were being
             stated twice on one screen. -->
        <article class="hop" :class="{ empty: !selectedLinks.tests }">
          <span class="dot"><i class="pi pi-shield" /></span>
          <div class="hop-body">
            <h4>Tests <span v-if="selectedLinks.tests">{{ selectedLinks.tests }}</span></h4>
            <button
              v-for="test in linkedDataTests"
              :key="test.id"
              class="linked"
              @click="nav.push('data-tests', { test: test.id })"
            ><i class="pi pi-chart-bar" /><span>{{ test.title }}</span><small>{{ plural(test.exception_count, 'exception') }}</small></button>
            <button
              v-for="test in linkedDocTests"
              :key="test.id"
              class="linked"
              @click="nav.push('doc-tests', { test: test.id })"
            ><i class="pi pi-file-check" /><span>{{ test.title }}</span><small>{{ test.status.replaceAll('_', ' ') }}</small></button>
            <p v-if="!linkedDataTests.length && !linkedDocTests.length" class="muted">
              No test covers this row, so it cannot pass coverage.
            </p>
          </div>
        </article>

        <!-- 3 · What that showed. -->
        <article class="hop" :class="{ empty: !selectedLinks.exceptions }">
          <span class="dot" :class="{ bad: selectedLinks.exceptions }"><i class="pi pi-exclamation-triangle" /></span>
          <div class="hop-body">
            <h4>Result</h4>
            <p v-if="selectedLinks.exceptions" class="verdict bad">
              {{ plural(selectedLinks.exceptions, 'exception') }} across
              {{ plural(selectedLinks.tests, 'test') }}<template v-if="selectedLinks.conclusion">
              — control concluded {{ selectedLinks.conclusion.replaceAll('_', ' ') }}</template>.
            </p>
            <p v-else-if="selectedLinks.tests" class="verdict ok">
              No exceptions recorded<template v-if="selectedLinks.conclusion">
              — control concluded {{ selectedLinks.conclusion.replaceAll('_', ' ') }}</template>.
            </p>
            <p v-else class="muted">Nothing has run against this row yet.</p>
          </div>
        </article>

        <!-- 4 · What it became. -->
        <article class="hop last" :class="{ empty: !linkedFindings.length }">
          <span class="dot" :class="{ bad: linkedFindings.length }"><i class="pi pi-flag" /></span>
          <div class="hop-body">
            <h4>Findings <span v-if="linkedFindings.length">{{ linkedFindings.length }}</span></h4>
            <button
              v-for="item in linkedFindings"
              :key="item.id"
              class="linked"
              @click="nav.push('findings', { finding: item.id })"
            ><i class="pi pi-flag" /><span>{{ item.title }}</span><small>{{ item.severity }}</small></button>
            <p v-if="!linkedFindings.length" class="muted">No finding has been drafted from this row.</p>
          </div>
        </article>
        </div>
      </section>

      <UiEmptyState
        v-else
        icon="pi pi-sitemap"
        title="No risk selected"
        description="Pick a row to follow it from its source through to its finding."
      />
    </div>

    <UiEmptyState
      v-else
      icon="pi pi-sitemap"
      title="Nothing to follow yet"
      description="The chain runs from a risk's cited source through the tests built from it to the finding they produced. It starts with the risk and control matrix."
    >
      <Button label="Open the matrix" icon="pi pi-map" @click="nav.push('rcm')" />
    </UiEmptyState>

    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" :documents="documents" />
  </div>
</template>

<style scoped>
.chain-view {
  display: flex; flex-direction: column; gap: .75rem;
  min-width: 0; max-width: 100%; min-height: 0; height: 100%;
}

.page-head { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; min-height: 2.25rem }
.page-head h1 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-xl); font-weight: 700; letter-spacing: -0.01em }
.grow { flex: 1 }

.layout { display: grid; grid-template-columns: 21rem minmax(0, 1fr); gap: .875rem; flex: 1; min-height: 12rem }

.list-panel { display: flex; flex-direction: column; min-width: 0; overflow: hidden; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel) }
.list-head { display: flex; flex-direction: column; gap: .5rem; padding: .625rem .75rem; border-bottom: 1px solid var(--aw-border) }
.list-head :deep(.p-iconfield), .list-head :deep(.p-inputtext) { width: 100% }
.list-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable }

/* The fieldwork row: a dot for what it found, a title, and one line of fact.
   It was four mono counters — `1 src 0 test 0 exc 0 find` — three of which
   read zero on most rows, so thirty of them said nothing about which to open. */
.list { display: flex; flex-direction: column; min-width: 0 }
.row {
  display: flex; align-items: flex-start; gap: .625rem;
  width: 100%; min-width: 0;
  padding: .625rem .75rem;
  border: 0; border-top: 1px solid var(--aw-border); border-left: 3px solid transparent;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.row:first-child { border-top: 0 }
.row:hover:not(.active) { background: var(--aw-raised) }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft) }
.row .dot { width: 9px; height: 9px; flex: none; margin-top: .3rem; border-radius: 50%; background: var(--aw-border-strong) }
.row .dot[data-tone='ok'] { background: var(--aw-ok) }
.row .dot[data-tone='warn'] { background: var(--aw-warn) }
.row .dot[data-tone='bad'] { background: var(--aw-danger) }
.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 }
/* Two lines, not one: a risk is a sentence, and truncating it to a single
   line loses the clause that tells one row from the next. */
.title { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-sm); line-height: 1.35 }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600 }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap }
.list-empty { padding: 1rem .75rem; margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center }

.detail {
  display: flex; flex-direction: column; gap: 1rem;
  min-width: 0; max-width: 100%; min-height: 100%;
  padding: 1.125rem 1.375rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  overflow-y: auto;
}
.detail-head { display: flex; align-items: flex-start; gap: 1rem; min-width: 0 }
.detail-copy { display: flex; flex-direction: column; gap: .25rem; flex: 1; min-width: 0 }
.detail-id { margin: 0; color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600 }
.detail-head h2 { margin: 0; color: var(--aw-ink-strong); font-size: var(--aw-text-lg); font-weight: 600; letter-spacing: -0.01em; line-height: 1.35 }
.objective { margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.45 }
.detail-head :deep(.p-button) { white-space: nowrap }

/* The spine is the point: one continuous line through five hops, so the
   chain reads as a chain rather than as five stacked cards. */
.spine { display: grid; gap: 0; max-width: 54rem; min-width: 0 }
.hop { display: grid; grid-template-columns: 2.2rem minmax(0, 1fr); gap: 0 .9rem; position: relative }
.hop::before { content: ""; position: absolute; left: 1.05rem; top: 0; bottom: 0; width: 2px; background: var(--aw-teal-line) }
.hop:first-child::before { top: 1.6rem }
.hop.last::before { bottom: calc(100% - 1.6rem) }
.hop.empty::before { background: var(--aw-border) }
.dot { position: relative; margin-top: .95rem; display: grid; place-items: center; width: 2.2rem; height: 2.2rem; border-radius: 50%; border: 2px solid var(--aw-teal); background: var(--aw-panel); color: var(--aw-teal); font-size: var(--aw-text-sm) }
.dot.bad { border-color: var(--aw-danger); color: var(--aw-danger) }
.hop.empty .dot { border-color: var(--aw-border-strong); color: var(--aw-muted) }
.hop-body { display: grid; gap: .4rem; align-content: start; padding: .9rem 0 1.6rem; min-width: 0 }
.hop-body h4 { display: flex; align-items: baseline; gap: .45rem; margin: 0; font-size: var(--aw-text-2xs); font-weight: 700; letter-spacing: .09em; text-transform: uppercase; color: var(--aw-muted); font-family: var(--aw-font-mono) }
.hop-body h4 span { color: var(--aw-ink-strong); font-variant-numeric: tabular-nums }
.risk { margin: 0; font-size: var(--aw-text-md); line-height: 1.5; color: var(--aw-ink-strong) }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-sm); line-height: 1.5 }
.muted em { font-style: normal; color: var(--aw-ink-soft) }

.anchor { display: grid; gap: .25rem; padding: .65rem .8rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); text-align: left; cursor: pointer; color: inherit }
.anchor:hover { border-color: var(--aw-teal); background: var(--aw-teal-soft) }
.anchor:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px }
.anchor strong { font-size: var(--aw-text-sm) }
.anchor code { color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs) }
.anchor small { color: var(--aw-muted); font-size: var(--aw-text-2xs) }
.anchor q { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.5 }

.linked { display: flex; align-items: center; gap: .5rem; padding: .5rem .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); text-align: left; cursor: pointer; color: inherit; font-size: var(--aw-text-sm) }
.linked:hover { border-color: var(--aw-teal) }
.linked:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px }
.linked > i { color: var(--aw-teal) }
.linked > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.linked > small { color: var(--aw-muted); font-size: var(--aw-text-2xs); white-space: nowrap }

.verdict { margin: 0; font-size: var(--aw-text-md); line-height: 1.5 }
.verdict.bad { color: var(--aw-danger); font-weight: 600 }
.verdict.ok { color: var(--aw-ok); font-weight: 600 }

@media (max-width: 900px) { .spine { max-width: none } }
</style>
