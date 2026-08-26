<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../../api'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type {
  AuditDocument, CriterionRef, DataTest, FindingSummary, PlanningPayload, RcmRow, WorkspaceSummary,
} from '../../types'
import EvidenceAnchorDialog from '../EvidenceAnchorDialog.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiMasterDetail from '../ui/UiMasterDetail.vue'
import UiPageHeader from '../ui/UiPageHeader.vue'
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
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const route = useRoute()
const toast = useToast()
const nav = useWorkspaceNav()

const data = ref<PlanningPayload | null>(null)
const documents = ref<AuditDocument[]>([])
const selectedId = ref<string | null>(String(route.query.rcm || '') || null)
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
      selectedId.value = ranked.value[0]?.row.id ?? null
    }
  } catch (error) { fail('Could not load the chain', error) }
})

const rows = computed(() => data.value?.rcm ?? [])
const selected = computed(() => rows.value.find(row => row.id === selectedId.value) ?? null)

function documentName(id: string) {
  const found = documents.value.find(item => item.id === id)
  return found?.source || found?.title || id
}
function shortRisk(row: RcmRow) {
  const text = (row.risk || row.control || row.process || row.id).trim()
  const first = text.split(/(?<=[.!?])\s+/)[0] ?? text
  return first.length > 120 ? `${first.slice(0, 119).trimEnd()}…` : first
}

/**
 * A finding points at the rows it was written against; a row does not point
 * back. `RcmRow.finding_refs` exists in the shape but nothing populates it —
 * it is empty on every row the planning payload returns — so the server sends
 * `finding_rollups.by_rcm` as the index to read instead, which is what the
 * matrix grid uses. Reading the row field here counted zero findings on every
 * row, which both emptied the rail and flattened the ordering below.
 */
function findingsFor(rcmId: string): FindingSummary[] {
  return data.value?.finding_rollups?.by_rcm?.[rcmId] ?? []
}

/** How much of a row's chain actually exists, for ordering and for the rail. */
function linksOf(row: RcmRow) {
  const rollup = row.execution_rollup ?? {}
  return {
    sources: row.criteria_refs?.length ?? 0,
    tests: rollup.tests ?? row.test_refs.length ?? 0,
    exceptions: rollup.exceptions ?? 0,
    findings: findingsFor(row.id).length,
    conclusion: rollup.control_conclusion ?? '',
  }
}
/** Complete chains first: the rows worth looking at are the ones that ran. */
const ranked = computed(() => rows.value
  .map(row => ({ row, links: linksOf(row) }))
  .sort((left, right) => {
    const depth = (item: typeof left) =>
      (item.links.findings ? 8 : 0) + (item.links.exceptions ? 4 : 0)
      + (item.links.tests ? 2 : 0) + (item.links.sources ? 1 : 0)
    return depth(right) - depth(left) || left.row.id.localeCompare(right.row.id)
  }))

const linkedDataTests = computed<DataTest[]>(() =>
  (data.value?.data_tests ?? []).filter(test => test.rcm_id === selectedId.value))
const linkedDocTests = computed(() =>
  (data.value?.document_tests ?? []).filter(test =>
    test.rcm_id === selectedId.value || (test.rcm_refs ?? []).includes(selectedId.value ?? '')))
// The same index the rail counts, so a row can never show "2 find" beside an
// empty Findings hop.
const linkedFindings = computed<FindingSummary[]>(() =>
  (selectedId.value ? findingsFor(selectedId.value) : []))
const selectedLinks = computed(() => (selected.value ? linksOf(selected.value) : null))

function openAnchor(item: CriterionRef) {
  anchor.value = item
  anchorOpen.value = true
}
</script>

<template>
  <div v-if="data" class="chain-view">
    <UiPageHeader title="Chain" />
    <p class="lede">
      Every other view files the work by kind. This one follows one risk down:
      the sentence it rests on, the tests built from it, what they found, and
      the finding that resulted.
    </p>

    <UiMasterDetail railWidth="21rem" class="layout">
      <template #rail>
        <div class="rail">
          <button
            v-for="entry in ranked"
            :key="entry.row.id"
            class="rail-row"
            :class="{ active: entry.row.id === selectedId }"
            @click="selectedId = entry.row.id"
          >
            <span class="rail-risk">{{ shortRisk(entry.row) }}</span>
            <span class="rail-links">
              <em :class="{ off: !entry.links.sources }">{{ entry.links.sources }} src</em>
              <em :class="{ off: !entry.links.tests }">{{ entry.links.tests }} test</em>
              <em :class="{ bad: entry.links.exceptions }">{{ entry.links.exceptions }} exc</em>
              <em :class="{ bad: entry.links.findings }">{{ entry.links.findings }} find</em>
            </span>
          </button>
        </div>
      </template>

      <section v-if="selected && selectedLinks" class="spine">
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

        <!-- 2 · What was required of it. -->
        <article class="hop">
          <span class="dot"><i class="pi pi-map" /></span>
          <div class="hop-body">
            <h4>Criterion · {{ selected.id }}</h4>
            <p class="risk">{{ selected.risk }}</p>
            <p class="muted">{{ selected.control }}</p>
            <button class="jump" @click="nav.push('rcm', { rcm: selected.id })">Open in the matrix</button>
          </div>
        </article>

        <!-- 3 · What was done about it. -->
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

        <!-- 4 · What that showed. -->
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

        <!-- 5 · What it became. -->
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
      </section>
      <UiEmptyState
        v-else
        icon="pi pi-sitemap"
        title="No risk selected"
        description="Pick a row to follow it from its source through to its finding."
      />
    </UiMasterDetail>

    <EvidenceAnchorDialog v-model="anchorOpen" :anchor="anchor" :documents="documents" />
  </div>
</template>

<style scoped>
.chain-view { display: flex; flex-direction: column; gap: var(--aw-section-gap); min-height: 100% }
.lede { max-width: 62ch; margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.55 }
.layout { flex: 1; min-height: 0 }

.rail { display: grid; gap: .4rem; padding: .1rem }
.rail-row { display: grid; gap: .3rem; padding: .6rem .7rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); text-align: left; cursor: pointer; color: inherit }
.rail-row:hover { border-color: var(--aw-teal) }
.rail-row.active { border-color: var(--aw-teal); background: var(--aw-teal-soft) }
.rail-row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px }
.rail-risk { font-size: var(--aw-text-sm); line-height: 1.4 }
.rail-links { display: flex; flex-wrap: wrap; gap: .3rem .55rem }
.rail-links em { color: var(--aw-ink-soft); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); font-style: normal; font-variant-numeric: tabular-nums }
.rail-links em.off { color: var(--aw-muted); opacity: .6 }
.rail-links em.bad { color: var(--aw-danger); font-weight: 700 }

/* The spine is the point: one continuous line through five hops, so the
   chain reads as a chain rather than as five stacked cards. */
.spine { display: grid; gap: 0; max-width: 54rem }
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
.jump { justify-self: start; padding: 0; border: 0; background: none; color: var(--aw-teal); font-size: var(--aw-text-sm); text-decoration: underline; cursor: pointer }

@media (max-width: 900px) { .spine { max-width: none } }
</style>
