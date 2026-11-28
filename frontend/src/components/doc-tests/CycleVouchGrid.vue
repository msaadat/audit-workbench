<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'

import { api } from '../../api'
import type {
  AuditFinding,
  CycleAssertionVerdict,
  CycleEvaluationState,
  CycleVouchGridComparison,
  CycleVouchGridPayload,
  CycleVouchMetadata,
} from '../../types'
import { CONTROL_CONCLUSIONS, useConclusionDraft } from './conclusionDraft'
import type { ConclusionChanges } from './conclusionDraft'
import DocTestStoryStrip from './DocTestStoryStrip.vue'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'
import {
  EMPTY_CYCLE_GRID_FILTERS,
  cycleGridPageLabel,
  filterCycleGridRows,
  type CycleGridFilters,
} from './cycleGridState'
import { buildStageSpans, planCycleColumns } from './cycleColumnOrg'

const props = defineProps<{
  workspaceId: string
  testId: string
  running: boolean
  busy: boolean
  metadata: CycleVouchMetadata | null
  findings: AuditFinding[]
}>()
const emit = defineEmits<{
  close: []
  error: [summary: string, error: unknown]
  openDetail: [itemId: string, assertionKey: string | null]
  openRules: []
  run: []
  changed: []
  saveConclusion: [changes: ConclusionChanges]
  generateFinding: [regenerate: boolean]
  openFinding: [findingId: string]
}>()

const scopeDisclosure = computed(() => payload.value?.scope_limitation_disclosure ?? '')
/** Records still carrying no call. A warning beside the conclusion, never a
 *  block: concluding over open records is the auditor's to make, and the
 *  backend records what was open as a scope limitation. */
const undispositioned = computed(() => payload.value?.pending_dispositions ?? 0)

const payload = ref<CycleVouchGridPayload | null>(null)
// One draft for the conclusion and the scope limitation, saved together. The
// grid reloads after every save; a field the auditor has moved survives that
// reload, which is what the two separate Saves did not do.
const draft = useConclusionDraft(() => payload.value
  ? {
      key: props.testId,
      conclusion: payload.value.control_conclusion,
      scopeNote: payload.value.scope_limitation_note ?? '',
    }
  : null)
function saveDraft() {
  if (draft.dirty) emit('saveConclusion', draft.changes())
}
const loading = ref(false)
const loadError = ref<string | null>(null)
const offset = ref(0)
const limit = ref(100)
const filters = reactive<CycleGridFilters>({ ...EMPTY_CYCLE_GRID_FILTERS })
const filtersOpen = ref(false)
const activeFilterCount = computed(() => [
  filters.evaluation,
  filters.disposition,
  filters.missingRole,
  filters.assertionKey,
  filters.assertionVerdict,
].filter(Boolean).length)
const selectedCell = ref<{ itemId: string; assertionKey: string } | null>(null)
const scrollContainer = ref<HTMLElement | null>(null)
const columnMode = ref<'relevant' | 'flagged' | 'all' | 'stage'>('all')
const selectedStageId = ref<string | null>(null)
const initializedScopeFor = ref<string | null>(null)

const columnPlan = computed(() => (payload.value ? planCycleColumns(payload.value) : null))
const hasFocusedSubset = computed(() => {
  const plan = columnPlan.value
  return Boolean(plan && plan.relevantKeys.length >= 1 && plan.relevantKeys.length < plan.orderedColumns.length)
})
const showOrganizer = computed(() => Boolean(columnPlan.value && columnPlan.value.orderedColumns.length > 1))
const visibleColumns = computed(() => {
  const plan = columnPlan.value
  if (!plan) return []
  let cols = plan.orderedColumns
  if (columnMode.value === 'relevant' && hasFocusedSubset.value) {
    const keep = new Set(plan.relevantKeys)
    cols = plan.orderedColumns.filter(c => keep.has(c.key))
  } else if (columnMode.value === 'flagged' && plan.flaggedKeys.length) {
    const keep = new Set(plan.flaggedKeys)
    cols = plan.orderedColumns.filter(c => keep.has(c.key))
  } else if (columnMode.value === 'stage' && selectedStageId.value) {
    const stage = plan.stages.find(s => s.id === selectedStageId.value)
    if (stage) cols = stage.columns
  }
  // If an explicit assertion filter is active and not in the current slice, include it.
  if (filters.assertionKey && !cols.some(c => c.key === filters.assertionKey)) {
    const match = plan.orderedColumns.find(c => c.key === filters.assertionKey)
    if (match) cols = [match, ...cols]
  }
  return cols
})
const stageSpans = computed(() => (
  columnPlan.value ? buildStageSpans(visibleColumns.value, columnPlan.value) : []
))
const showStageSuperHeader = computed(() => showOrganizer.value && stageSpans.value.length > 1)

function selectScope(mode: 'relevant' | 'flagged' | 'all') {
  columnMode.value = mode
  selectedStageId.value = null
}
function selectStage(stageId: string) {
  if (columnMode.value === 'stage' && selectedStageId.value === stageId) {
    columnMode.value = hasFocusedSubset.value ? 'relevant' : 'all'
    selectedStageId.value = null
    return
  }
  columnMode.value = 'stage'
  selectedStageId.value = stageId
}

const CONCLUSION_LABELS: Record<string, string> = {
  effective: 'Effective',
  partially_effective: 'Partially effective',
  ineffective: 'Ineffective',
  not_applicable: 'Not applicable',
}

const cycleStory = computed(() => {
  const p = payload.value
  if (!p) return null
  const stageLabels = columnPlan.value?.stages.map(s => s.label) ?? []
  const roles = stageLabels.length
    ? stageLabels
    : Array.from(new Set(p.columns.flatMap(col => col.applicable_roles))).map(label)
  const scopeLabel = roles.length ? roles.join(' → ') : (p.population.table || 'Transaction evidence')
  const matched = p.assertion_counts.match ?? 0
  const mismatched = p.assertion_counts.mismatch ?? 0
  const review = (p.assertion_counts.cannot_determine ?? 0)
    + (p.assertion_counts.missing_evidence ?? 0)
    + (p.assertion_counts.invalid_extraction ?? 0)
    + (p.assertion_counts.ambiguous ?? 0)
  const total = p.assertion_counts.total ?? (matched + mismatched + review)
  const notRun = p.assertion_counts.not_run ?? 0
  const ran = total > 0 && notRun < total
  const pending = undispositioned.value
  const conclusion = p.control_conclusion && p.control_conclusion !== 'no_conclusion'
    ? (CONCLUSION_LABELS[p.control_conclusion] ?? p.control_conclusion)
    : null

  let humanTitle = 'Awaiting voucher execution'
  let humanDetail = 'Run the test to compare extracted fields across each transaction pack.'
  let humanTone: 'ok' | 'warn' | 'bad' | 'neutral' = 'neutral'
  let humanBadge = 'Human in the loop'

  if (conclusion && pending === 0) {
    humanTitle = `Signed off · ${conclusion}`
    humanDetail = `All ${p.page.total} transaction${p.page.total === 1 ? '' : 's'} ruled and control concluded.`
    humanTone = 'ok'
    humanBadge = 'Auditor signed off'
  } else if (ran && pending > 0) {
    humanTitle = `${pending} of ${p.page.total} transaction${p.page.total === 1 ? '' : 's'} awaiting call`
    humanDetail = 'Confirm or flag each transaction below, then record the control conclusion.'
    humanTone = 'warn'
    humanBadge = 'Human review'
  } else if (ran) {
    humanTitle = 'Ready for control conclusion'
    humanDetail = 'All transactions are ruled — record the control conclusion in the footer below.'
    humanTone = 'warn'
    humanBadge = 'Sign-off needed'
  }

  return {
    ruleTitle: `${p.columns.length} cross-document assertion${p.columns.length === 1 ? '' : 's'}`,
    scopeLabel,
    ran,
    matched,
    mismatched,
    review,
    total,
    coverageMeta: `${p.page.total} transaction${p.page.total === 1 ? '' : 's'} vouched · ${p.tested_item_counts.failed ?? 0} failed`,
    humanTitle,
    humanDetail,
    humanTone,
    humanBadge,
  }
})

const evaluationOptions: Array<{ value: '' | CycleEvaluationState; label: string }> = [
  { value: '', label: 'All evaluations' },
  { value: 'not_run', label: 'Not run' },
  { value: 'passed', label: 'Passed' },
  { value: 'failed', label: 'Failed' },
  { value: 'incomplete', label: 'Incomplete' },
  { value: 'needs_review', label: 'Needs review' },
  { value: 'stale', label: 'Stale' },
]
const dispositionOptions: Array<{ value: CycleGridFilters['disposition']; label: string }> = [
  { value: '', label: 'All dispositions' },
  { value: 'pending', label: 'Pending' },
  { value: 'confirmed', label: 'Confirmed' },
  { value: 'exception', label: 'Exception' },
  { value: 'stale', label: 'Stale sign-off' },
]
const verdictOptions: Array<{ value: '' | CycleAssertionVerdict; label: string }> = [
  { value: '', label: 'Any verdict' },
  { value: 'match', label: 'Match' },
  { value: 'mismatch', label: 'Mismatch' },
  { value: 'cannot_determine', label: 'Could not determine' },
  { value: 'missing_evidence', label: 'Missing evidence' },
  { value: 'invalid_extraction', label: 'Invalid extraction' },
  { value: 'ambiguous', label: 'Ambiguous' },
  { value: 'not_run', label: 'Not run' },
]
const pageSizes = [25, 50, 100, 200]

const rows = computed(() => filterCycleGridRows(payload.value?.rows ?? [], filters))
const missingRoleOptions = computed(() => Array.from(new Set(
  (payload.value?.rows ?? []).flatMap(row => row.missing_roles),
)).sort())
const pageLabel = computed(() => {
  const page = payload.value?.page
  return page ? cycleGridPageLabel(page.offset, page.limit, page.total) : 'No items'
})
const hasPrevious = computed(() => offset.value > 0)
const hasNext = computed(() => {
  const page = payload.value?.page
  return Boolean(page && page.offset + page.limit < page.total)
})

function label(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, character => character.toUpperCase())
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') {
    try { return JSON.stringify(value) }
    catch { return String(value) }
  }
  return String(value)
}

function dispositionStatus(row: CycleVouchGridPayload['rows'][number]): string {
  if (row.disposition_stale) return 'stale'
  // Evaluated and not yet decided is the auditor's turn, not "Not run".
  if (row.disposition_state === 'pending' && ['passed', 'failed'].includes(String(row.evaluation_state ?? ''))) {
    return 'awaiting_call'
  }
  return row.disposition_state
}

// Dispositioning from the grid. The column already showed the decision; opening
// each row to change it was the only thing standing between a 40-row cycle test
// and signing it off in one pass.
const dispositioning = ref<string | null>(null)

function canDisposition(row: CycleVouchGridPayload['rows'][number]): boolean {
  return !['not_run', 'stale'].includes(String(row.evaluation_state ?? 'not_run'))
}

async function setDisposition(
  row: CycleVouchGridPayload['rows'][number],
  state: 'confirmed' | 'exception' | 'pending',
) {
  dispositioning.value = row.item_id
  try {
    await api.patch(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}/items/${row.item_id}`,
      { state },
    )
    await loadGrid()
    emit('changed')
  } catch (error) {
    emit('error', 'Could not record the disposition', error)
  } finally {
    dispositioning.value = null
  }
}

function selectCell(itemId: string, assertionKey: string) {
  const selected = selectedCell.value
  selectedCell.value = selected?.itemId === itemId && selected.assertionKey === assertionKey
    ? null
    : { itemId, assertionKey }
}

function isSelectedCell(itemId: string, assertionKey: string): boolean {
  return selectedCell.value?.itemId === itemId && selectedCell.value.assertionKey === assertionKey
}

function focusSelectedCell() {
  const selected = selectedCell.value
  if (!selected) return
  const cell = Array.from(
    scrollContainer.value?.querySelectorAll<HTMLElement>('.assertion-cell[data-assertion-key]') ?? [],
  ).find(node =>
    node.dataset.assertionKey === selected.assertionKey
    && node.closest<HTMLElement>('tr')?.dataset.itemId === selected.itemId,
  )
  cell?.querySelector<HTMLButtonElement>('.cell-trigger')?.focus({ preventScroll: true })
}

function comparisonTitle(comparison: CycleVouchGridComparison): string {
  return [comparison.role ?? comparison.side ?? 'Comparison', comparison.document_id]
    .filter(Boolean)
    .join(' · ')
}

function applyColumnFilter(assertionKey: string, verdict: CycleAssertionVerdict) {
  filters.assertionKey = assertionKey
  filters.assertionVerdict = verdict
}

function clearFilters() {
  Object.assign(filters, EMPTY_CYCLE_GRID_FILTERS)
}

async function loadGrid() {
  loading.value = true
  loadError.value = null
  try {
    payload.value = await api.get<CycleVouchGridPayload>(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}/grid?offset=${offset.value}&limit=${limit.value}`,
    )
    if (payload.value.page.offset !== offset.value) offset.value = payload.value.page.offset
    if (initializedScopeFor.value !== props.testId) {
      initializedScopeFor.value = props.testId
      const plan = planCycleColumns(payload.value)
      if (plan.relevantKeys.length >= 1 && plan.relevantKeys.length < plan.orderedColumns.length) {
        columnMode.value = 'relevant'
      } else {
        columnMode.value = 'all'
      }
      selectedStageId.value = null
    }
  } catch (error) {
    payload.value = null
    loadError.value = error instanceof Error ? error.message : String(error)
    emit('error', 'Could not load the Cycle vouch grid', error)
  } finally {
    loading.value = false
  }
}

function previousPage() {
  offset.value = Math.max(0, offset.value - limit.value)
}

function nextPage() {
  if (hasNext.value) offset.value += limit.value
}

function changePageSize(event: Event) {
  limit.value = Number((event.target as HTMLSelectElement).value)
  offset.value = 0
}

function openDetail(itemId: string, assertionKey: string | null) {
  emit('openDetail', itemId, assertionKey)
}

watch(
  () => [props.testId, offset.value, limit.value] as const,
  ([testId], previous) => {
    if (previous && previous[0] !== testId) {
      offset.value = 0
      selectedCell.value = null
      clearFilters()
    }
    void loadGrid()
  },
  { immediate: true },
)

defineExpose({ filters, focusSelectedCell, loadGrid, offset, scrollContainer, selectedCell })
</script>

<template>
  <section class="cycle-grid" aria-label="Cycle vouch grid review">
    <header class="grid-head">
      <div>
        <Button label="All document work" icon="aw-icon aw-icon-arrow-left" text size="small" @click="emit('close')" />
        <p class="eyebrow">Cycle vouch · {{ payload?.population.table ?? 'Loading population' }}</p>
        <h3>{{ payload?.title ?? 'Cycle vouch review' }}</h3>
      </div>
      <div class="grid-actions">
        <!-- The assertions are part of what the auditor approved. Changing
             them here would produce rules nobody stood behind, so the affordance
             points at where they are actually changed. -->
        <Button
          label="Cycle rules"
          icon="aw-icon aw-icon-network"
          size="small"
          outlined
          @click="emit('openRules')"
        />
        <Button
          label="Run test"
          icon="aw-icon aw-icon-play"
          size="small"
          :loading="running"
          :disabled="busy"
          @click="emit('run')"
        />
      </div>
    </header>

    <DocTestStoryStrip
      v-if="cycleStory"
      :ai-authored="true"
      engine-label="3-way cycle vouch"
      :rule-title="cycleStory.ruleTitle"
      :scope-label="cycleStory.scopeLabel"
      :ran="cycleStory.ran"
      :passed-count="cycleStory.matched"
      :failed-count="cycleStory.mismatched"
      :review-count="cycleStory.review"
      :total-count="cycleStory.total"
      unit-label="checks"
      evidence-badge="Page-cited extractions"
      :coverage-meta="cycleStory.coverageMeta"
      :human-title="cycleStory.humanTitle"
      :human-detail="cycleStory.humanDetail"
      :human-tone="cycleStory.humanTone"
      :human-badge="cycleStory.humanBadge"
    />

    <section v-if="showOrganizer && columnPlan" class="cycle-organizer" aria-label="Organize cycle assertions">
      <div class="scope-row" role="tablist" aria-label="Assertion scope">
        <button
          v-if="hasFocusedSubset"
          type="button"
          role="tab"
          class="scope-pill"
          :class="{ active: columnMode === 'relevant' }"
          :aria-selected="columnMode === 'relevant'"
          @click="selectScope('relevant')"
        >
          <i class="aw-icon aw-icon-sparkles" aria-hidden="true" />
          This test <span class="count aw-figure">{{ columnPlan.relevantKeys.length }}</span>
        </button>
        <button
          v-if="columnPlan.flaggedKeys.length"
          type="button"
          role="tab"
          class="scope-pill flagged"
          :class="{ active: columnMode === 'flagged' }"
          :aria-selected="columnMode === 'flagged'"
          @click="selectScope('flagged')"
        >
          <i class="aw-icon aw-icon-triangle-alert" aria-hidden="true" />
          Flagged <span class="count aw-figure">{{ columnPlan.flaggedKeys.length }}</span>
        </button>
        <button
          type="button"
          role="tab"
          class="scope-pill"
          :class="{ active: columnMode === 'all' }"
          :aria-selected="columnMode === 'all'"
          @click="selectScope('all')"
        >
          All cycle checks <span class="count aw-figure">{{ columnPlan.orderedColumns.length }}</span>
        </button>
        <span class="scope-meta aw-figure">
          Showing {{ visibleColumns.length }} of {{ columnPlan.orderedColumns.length }} assertions
        </span>
      </div>

      <div v-if="columnPlan.stages.length > 1" class="stage-strip" role="list" aria-label="Cycle document stages">
        <template v-for="(stage, idx) in columnPlan.stages" :key="stage.id">
          <i v-if="idx > 0" class="stage-arrow aw-icon aw-icon-chevron-right" aria-hidden="true" />
          <button
            type="button"
            role="listitem"
            class="stage-chip"
            :class="{ active: columnMode === 'stage' && selectedStageId === stage.id }"
            :data-tone="stage.tone"
            :aria-pressed="columnMode === 'stage' && selectedStageId === stage.id"
            @click="selectStage(stage.id)"
          >
            <span class="stage-step">{{ stage.stepIndex }}</span>
            <span class="stage-label">{{ stage.label }}</span>
            <span class="stage-stats aw-figure">
              {{ stage.columns.length }}
              <template v-if="stage.mismatched > 0">
                · <b class="bad">{{ stage.mismatched }} ✗</b>
              </template>
              <template v-else-if="stage.review > 0">
                · <b class="warn">{{ stage.review }} ?</b>
              </template>
              <template v-else-if="stage.matched > 0">
                · <b class="ok">✓</b>
              </template>
            </span>
          </button>
        </template>
      </div>
    </section>

    <div v-if="payload" class="grid-toolbar" aria-label="Grid filters">
      <div class="toolbar-primary">
        <label class="search-filter">
          <span class="sr-only">Search bounded grid fields</span>
          <input v-model="filters.search" type="search" placeholder="Search transaction, document, role, or value…" />
        </label>
        <Button
          :label="activeFilterCount ? `Filters (${activeFilterCount})` : 'Filters'"
          icon="aw-icon aw-icon-sliders-horizontal"
          size="small"
          outlined
          severity="secondary"
          :aria-expanded="filtersOpen || activeFilterCount > 0"
          @click="filtersOpen = !filtersOpen"
        />
        <Button
          v-if="activeFilterCount || filters.search"
          label="Clear filters"
          icon="aw-icon aw-icon-funnel-x"
          text
          size="small"
          @click="clearFilters"
        />
      </div>
      <div v-if="filtersOpen || activeFilterCount > 0" class="grid-filters">
        <label>
          <span>Evaluation</span>
          <select v-model="filters.evaluation">
            <option v-for="option in evaluationOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <label>
          <span>Auditor disposition</span>
          <select v-model="filters.disposition">
            <option v-for="option in dispositionOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <label>
          <span>Missing role</span>
          <select v-model="filters.missingRole">
            <option value="">Any role</option>
            <option v-for="role in missingRoleOptions" :key="role" :value="role">{{ label(role) }}</option>
          </select>
        </label>
        <label>
          <span>Assertion</span>
          <select v-model="filters.assertionKey">
            <option value="">Any assertion</option>
            <option v-for="column in (columnPlan?.orderedColumns ?? payload.columns)" :key="column.key" :value="column.key">{{ column.label }}</option>
          </select>
        </label>
        <label>
          <span>Assertion verdict</span>
          <select v-model="filters.assertionVerdict" :disabled="!filters.assertionKey">
            <option v-for="option in verdictOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
      </div>
    </div>

    <div v-if="payload" class="page-bar">
      <span>{{ rows.length }} matching item{{ rows.length === 1 ? '' : 's' }} on this page · {{ pageLabel }}</span>
      <div>
        <label class="page-size">Rows <select :value="limit" @change="changePageSize"><option v-for="size in pageSizes" :key="size" :value="size">{{ size }}</option></select></label>
        <Button icon="aw-icon aw-icon-chevron-left" text rounded aria-label="Previous grid page" :disabled="!hasPrevious || loading" @click="previousPage" />
        <Button icon="aw-icon aw-icon-chevron-right" text rounded aria-label="Next grid page" :disabled="!hasNext || loading" @click="nextPage" />
      </div>
    </div>

    <!-- Dispositions are withheld while a run is live (paused included), so
         say so: disabled buttons are skipped by the keyboard and read as no
         control at all. -->
    <p v-if="payload && busy" class="calls-paused">
      Dispositions are unavailable while the assistant is running or paused. Stop or finish the run to record them.
    </p>
    <div v-if="payload" ref="scrollContainer" class="grid-scroll" tabindex="0" aria-label="Scrollable Cycle vouch results">
      <table :class="{ 'has-super-header': showStageSuperHeader }">
        <thead>
          <tr v-if="showStageSuperHeader" class="stage-super-row">
            <th colspan="3" class="sticky super-spacer" scope="colgroup">Transaction &amp; disposition</th>
            <th
              v-for="span in stageSpans"
              :key="`${span.stageId}-${span.stepIndex}`"
              :colspan="span.span"
              class="stage-group-head"
              :data-tone="span.tone"
              scope="colgroup"
            >
              <span class="stage-group-num">{{ span.stepIndex }}</span>
              <span>{{ span.label }}</span>
              <span class="stage-group-count aw-figure">({{ span.span }})</span>
            </th>
          </tr>
          <tr class="col-head-row">
            <th class="sticky transaction-column" scope="col">Transaction</th>
            <th class="sticky evaluation-column" scope="col">Evaluation</th>
            <th class="sticky disposition-column" scope="col">Disposition</th>
            <th v-for="column in visibleColumns" :key="column.key" class="assertion-column" scope="col">
              <strong>{{ column.label }}</strong>
              <small>{{ column.requirement }}<template v-if="column.applicable_roles.length"> · {{ column.applicable_roles.map(label).join(', ') }}</template></small>
              <small v-if="column.in_scope === false" class="column-context">Cycle context · does not decide this test</small>
              <div class="column-counts" :aria-label="`${column.label} full-test verdict summary`">
                <button
                  v-for="option in verdictOptions.filter(option => option.value && column.counts[option.value])"
                  :key="option.value"
                  type="button"
                  :data-verdict="option.value"
                  :aria-label="`Filter ${column.label} to ${option.label}: ${column.counts[option.value as CycleAssertionVerdict]}`"
                  @click="applyColumnFilter(column.key, option.value as CycleAssertionVerdict)"
                >{{ option.label }} {{ column.counts[option.value as CycleAssertionVerdict] }}</button>
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.item_id" :data-item-id="row.item_id">
            <th class="sticky transaction-column" scope="row">
              <button type="button" class="transaction-link" @click="openDetail(row.item_id, null)">
                <strong>{{ row.label || row.item_id }}</strong>
                <small>{{ row.item_id }}</small>
              </button>
              <small v-if="row.missing_roles.length" class="missing-role-text">Missing {{ row.missing_roles.map(label).join(', ') }}</small>
            </th>
            <td class="sticky evaluation-column"><UiTestStatus :status="row.evaluation_state" showLabel /></td>
            <td class="sticky disposition-column">
              <UiTestStatus :status="dispositionStatus(row)" showLabel />
              <span v-if="canDisposition(row)" class="disposition-actions">
                <Button
                  icon="aw-icon aw-icon-check"
                  text
                  rounded
                  size="small"
                  :aria-label="`Confirm ${row.label || row.item_id}`"
                  v-tooltip.top="'Confirm'"
                  :disabled="busy || dispositioning === row.item_id"
                  :class="{ 'is-current': row.disposition_state === 'confirmed' && !row.disposition_stale }"
                  @click="setDisposition(row, 'confirmed')"
                />
                <Button
                  icon="aw-icon aw-icon-triangle-alert"
                  text
                  rounded
                  size="small"
                  severity="danger"
                  :aria-label="`Mark ${row.label || row.item_id} an exception`"
                  v-tooltip.top="'Exception'"
                  :disabled="busy || dispositioning === row.item_id"
                  :class="{ 'is-current': row.disposition_state === 'exception' && !row.disposition_stale }"
                  @click="setDisposition(row, 'exception')"
                />
                <Button
                  icon="aw-icon aw-icon-undo-2"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  :aria-label="`Clear the disposition on ${row.label || row.item_id}`"
                  v-tooltip.top="'Clear your call'"
                  :disabled="busy || dispositioning === row.item_id || row.disposition_state === 'pending'"
                  @click="setDisposition(row, 'pending')"
                />
              </span>
            </td>
            <td
              v-for="column in visibleColumns"
              :key="column.key"
              class="assertion-cell"
              :data-assertion-key="column.key"
              :data-verdict="row.cells[column.key]?.verdict ?? 'not_run'"
              :class="{ selected: isSelectedCell(row.item_id, column.key) }"
            >
              <button
                type="button"
                class="cell-trigger"
                :aria-expanded="isSelectedCell(row.item_id, column.key)"
                :aria-label="`${column.label} for ${row.label}: ${label(row.cells[column.key]?.verdict ?? 'not_run')}`"
                @click="selectCell(row.item_id, column.key)"
              >
                <UiTestStatus :status="row.cells[column.key]?.verdict ?? 'not_run'" />
                <span>{{ row.cells[column.key]?.display || label(row.cells[column.key]?.verdict ?? 'not_run') }}</span>
                <small>{{ row.cells[column.key]?.comparison_count ?? 0 }} comparison{{ row.cells[column.key]?.comparison_count === 1 ? '' : 's' }}</small>
              </button>
              <div v-if="isSelectedCell(row.item_id, column.key)" class="cell-popover" role="dialog" :aria-label="`${column.label} comparison detail`">
                <header>
                  <div><strong>{{ column.label }}</strong><small>{{ row.label }}</small></div>
                  <Button icon="aw-icon aw-icon-x" text rounded size="small" aria-label="Close comparison detail" @click="selectedCell = null" />
                </header>
                <p v-if="row.cells[column.key]?.display" class="cell-display">{{ row.cells[column.key].display }}</p>
                <div v-if="row.cells[column.key]?.comparisons.length" class="comparison-list">
                  <article v-for="(comparison, index) in row.cells[column.key].comparisons" :key="index">
                    <div><strong>{{ comparisonTitle(comparison) }}</strong><UiTestStatus :status="comparison.verdict ?? comparison.state ?? 'not_run'" showLabel /></div>
                    <p>{{ comparison.display_values.length ? comparison.display_values.map(displayValue).join(' · ') : 'No display value projected' }}</p>
                    <small>
                      {{ comparison.entry_count }} extracted entr{{ comparison.entry_count === 1 ? 'y' : 'ies' }} ·
                      {{ comparison.evidence_count }} evidence reference{{ comparison.evidence_count === 1 ? '' : 's' }}
                      <template v-if="comparison.record_ids.length"> · {{ comparison.record_ids.join(', ') }}</template>
                    </small>
                  </article>
                </div>
                <p v-else class="muted">No per-document comparison has been projected for this cell.</p>
                <Button label="Open assertion evidence" icon="aw-icon aw-icon-arrow-right" size="small" @click="openDetail(row.item_id, column.key)" />
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <UiEmptyState v-if="!rows.length" icon="aw-icon aw-icon-funnel-x" title="No items match" description="Clear or adjust the grid filters to see this page." compact />
    </div>

    <UiEmptyState v-else-if="loadError" icon="aw-icon aw-icon-triangle-alert" title="Cycle grid unavailable" :description="loadError" compact>
      <Button label="Try again" icon="aw-icon aw-icon-refresh-cw" size="small" outlined @click="loadGrid" />
    </UiEmptyState>
    <UiEmptyState v-else icon="aw-icon aw-icon-spin aw-icon-loader-circle" title="Loading Cycle vouch grid" description="Reading the bounded grid projection." compact />

    <!-- Its own `v-if` rather than a place in the chain above: the empty states
         are the `v-else` arms of the grid itself, and a sibling between them
         would break that. -->
    <div v-if="payload" class="footer-row">
      <div class="footer-cell">
        <p class="aw-label">Conclusion</p>
        <select v-model="draft.conclusion" class="conclusion-select" aria-label="Control conclusion">
          <option v-for="option in CONTROL_CONCLUSIONS" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </div>
      <div class="footer-cell">
        <p class="aw-label">Finding</p>
        <template v-if="findings.length">
          <button
            v-for="finding in findings"
            :key="finding.id"
            type="button"
            class="finding-chip"
            @click="emit('openFinding', finding.id)"
          >
            <span class="finding-id">{{ finding.id }}</span>{{ finding.severity }}
          </button>
          <Button
            label="Regenerate"
            size="small" text severity="secondary"
            :disabled="busy || !payload.rcm_id"
            @click="emit('generateFinding', true)"
          />
        </template>
        <template v-else>
          <span class="footer-note">{{ payload.rcm_id ? 'None yet.' : 'Not linked to an RCM row.' }}</span>
          <Button
            label="Generate finding"
            icon="aw-icon aw-icon-sparkles"
            size="small" text severity="secondary"
            :disabled="busy || !payload.rcm_id"
            @click="emit('generateFinding', false)"
          />
        </template>
      </div>
      <div class="footer-cell scope-cell">
        <p class="aw-label">Scope limitation</p>
        <input
          v-model="draft.scopeNote"
          class="scope-input"
          type="text"
          aria-label="Scope limitation"
          placeholder="What the evidence could not establish"
          @keydown.enter.prevent="saveDraft"
        />
      </div>
      <div v-if="draft.dirty" class="footer-cell footer-save">
        <Button label="Save" icon="aw-icon aw-icon-check" size="small" :disabled="busy" aria-label="Save conclusion and scope limitation" @click="saveDraft" />
        <Button label="Discard" size="small" text severity="secondary" :disabled="busy" @click="draft.discard()" />
      </div>
      <p v-if="scopeDisclosure" class="footer-disclosure">{{ scopeDisclosure }}</p>
      <p v-if="undispositioned" class="footer-warn">
        {{ undispositioned }} of {{ payload.page.total }} records carry no call yet. You can
        still conclude — it will be recorded as a scope limitation.
      </p>
    </div>

  </section>
</template>

<style scoped>
.footer-row { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1.5rem; padding-top: .75rem; border-top: 1px solid var(--aw-border); }
.footer-cell { display: flex; align-items: center; gap: .5rem; }
.footer-cell .aw-label { margin: 0; }
.calls-paused { margin: 0 0 .5rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.conclusion-select { min-width: 10rem; min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
.footer-note { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.scope-cell { flex: 1 1 22rem; }
.scope-input { flex: 1 1 auto; min-width: 12rem; min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
.footer-disclosure { flex: 1 1 100%; margin: 0; padding: .4rem .6rem; border-radius: var(--aw-radius-control); background: var(--aw-surface-sunken, var(--aw-panel)); color: var(--aw-muted); font-size: var(--aw-text-xs); white-space: pre-line; }
.footer-warn { flex: 1 1 100%; margin: 0; color: var(--aw-warn); font-size: var(--aw-text-xs); }
.finding-chip { display: inline-flex; align-items: center; gap: .35rem; padding: .2rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: inherit; font: inherit; cursor: pointer; }
.finding-id { color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }
.cycle-grid { display: flex; flex-direction: column; gap: var(--aw-space-4); min-width: 0; padding: 1rem; border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.grid-head, .grid-actions, .grid-summary, .page-bar, .page-bar > div { display: flex; align-items: center; }
.grid-head { justify-content: space-between; gap: 1rem; }
.grid-head h3 { margin: .15rem 0 0; font-size: var(--aw-text-xl); }
.eyebrow { margin: .15rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.grid-summary { flex-wrap: wrap; gap: .45rem; }
.grid-summary span { padding: .35rem .6rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-xs); }
.grid-summary strong { color: var(--aw-ink); }
.cycle-organizer {
  display: flex;
  flex-direction: column;
  gap: .5rem;
  padding: .65rem .75rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-control);
  background: var(--aw-canvas);
}
.scope-row { display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; }
.scope-pill {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .28rem .65rem;
  border: 1px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-pill);
  background: var(--aw-panel);
  color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600;
  cursor: pointer;
}
.scope-pill:hover:not(.active) { border-color: var(--aw-teal-line); color: var(--aw-ink-strong); }
.scope-pill.active {
  border-color: var(--aw-teal);
  background: var(--aw-teal-soft);
  color: var(--aw-teal-strong);
}
.scope-pill.flagged { border-color: var(--aw-danger-line); color: var(--aw-danger); }
.scope-pill.flagged.active { background: var(--aw-danger-soft); border-color: var(--aw-danger); color: var(--aw-danger-ink); }
.scope-pill .count {
  padding: .02rem .35rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-raised);
  color: inherit;
  font-size: var(--aw-text-2xs); font-weight: 700;
}
.scope-meta { margin-left: auto; color: var(--aw-muted); font-size: var(--aw-text-xs); }

.stage-strip { display: flex; align-items: center; gap: .3rem; overflow-x: auto; padding-bottom: .15rem; scrollbar-width: thin; }
.stage-arrow { flex: none; color: var(--aw-border-strong); font-size: var(--aw-text-xs); }
.stage-chip {
  display: inline-flex; align-items: center; gap: .4rem; flex: none;
  padding: .3rem .6rem;
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  color: var(--aw-ink);
  font: inherit; font-size: var(--aw-text-xs); font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.stage-chip[data-tone='ok'] { border-left-color: var(--aw-ok); }
.stage-chip[data-tone='warn'] { border-left-color: var(--aw-warn); }
.stage-chip[data-tone='bad'] { border-left-color: var(--aw-danger); }
.stage-chip:hover:not(.active) { background: var(--aw-raised); border-color: var(--aw-teal-line); }
.stage-chip.active { border-color: var(--aw-teal); border-left-color: var(--aw-teal); background: var(--aw-teal-soft); color: var(--aw-teal-strong); }
.stage-step {
  display: grid; place-items: center;
  width: 1.15rem; height: 1.15rem; border-radius: 50%;
  background: var(--aw-raised); color: var(--aw-ink-soft);
  font-size: var(--aw-text-2xs); font-weight: 700;
}
.stage-chip.active .stage-step { background: var(--aw-teal); color: var(--aw-panel); }
.stage-stats { color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 600; }
.stage-stats .ok { color: var(--aw-ok); }
.stage-stats .warn { color: var(--aw-warn-ink); }
.stage-stats .bad { color: var(--aw-danger); }

.stage-super-row th {
  position: sticky; top: 0; z-index: 6;
  height: 2.1rem; min-height: 2.1rem; padding: .28rem .6rem;
  background: var(--aw-canvas);
  border-bottom: 1px solid var(--aw-border-strong);
  color: var(--aw-ink-strong);
  font-size: var(--aw-text-xs); font-weight: 700;
  vertical-align: middle; white-space: nowrap;
}
.stage-super-row .super-spacer { left: 0; z-index: 8; color: var(--aw-muted); background: var(--aw-raised); box-shadow: .35rem 0 .45rem -.45rem rgba(13, 35, 64, .45); }
.stage-group-head[data-tone='ok'] { border-top: 2px solid var(--aw-ok); }
.stage-group-head[data-tone='warn'] { border-top: 2px solid var(--aw-warn); }
.stage-group-head[data-tone='bad'] { border-top: 2px solid var(--aw-danger); background: color-mix(in srgb, var(--aw-danger-soft) 50%, var(--aw-canvas)); }
.stage-group-num {
  display: inline-grid; place-items: center;
  width: 1.1rem; height: 1.1rem; margin-right: .35rem;
  border-radius: 50%; background: var(--aw-raised); color: var(--aw-ink-soft);
  font-size: var(--aw-text-2xs); font-weight: 700;
}
.stage-group-count { margin-left: .25rem; color: var(--aw-muted); font-weight: 500; }
table.has-super-header thead tr.col-head-row th { top: 2.1rem; }

.grid-toolbar { display: flex; flex-direction: column; gap: .5rem; }
.toolbar-primary { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.search-filter { flex: 1 1 16rem; min-width: 0; }
.search-filter input { width: 100%; min-width: 0; min-height: 2.1rem; padding: .3rem .6rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; font-size: var(--aw-text-sm); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.grid-filters { display: grid; grid-template-columns: repeat(5, minmax(8rem, 1fr)); gap: .55rem; align-items: end; padding: .6rem .75rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-canvas); }
.grid-filters label { display: grid; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.grid-filters select, .page-size select { width: 100%; min-width: 0; min-height: 2.1rem; padding: .3rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
.page-bar { justify-content: space-between; gap: .8rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.page-bar > div { gap: .2rem; }
.page-size { display: flex; align-items: center; gap: .35rem; }
.page-size select { width: 4.5rem; min-height: 2rem; }
.grid-scroll { position: relative; min-width: 0; max-height: min(62vh, 46rem); overflow: auto; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
table { width: max-content; min-width: 100%; border-collapse: separate; border-spacing: 0; table-layout: fixed; }
th, td { height: 4.7rem; padding: .55rem; border-right: 1px solid var(--aw-border); border-bottom: 1px solid var(--aw-border); background: var(--aw-panel); vertical-align: top; text-align: left; }
thead th { position: sticky; top: 0; z-index: 5; height: auto; min-height: 7rem; background: var(--aw-raised); }
thead th > strong, thead th > small { display: block; }
thead th > small { margin-top: .2rem; color: var(--aw-muted); font-size: var(--aw-text-2xs); font-weight: 500; }
.sticky { position: sticky; z-index: 3; }
.transaction-column { left: 0; width: 14rem; min-width: 14rem; max-width: 14rem; }
.evaluation-column { left: 14rem; width: 7.5rem; min-width: 7.5rem; max-width: 7.5rem; }
/* Widened from 7.5rem to fit the three inline controls under the chip; the
   column is sticky, so this is width taken from the assertion cells that
   scroll, not from the transaction label. */
.disposition-column { left: 21.5rem; width: 10rem; min-width: 10rem; max-width: 10rem; box-shadow: .35rem 0 .45rem -.45rem rgba(13, 35, 64, .45); }
.disposition-actions { display: flex; gap: .1rem; margin-top: .2rem; }
.disposition-actions :deep(.p-button) { width: 1.9rem; height: 1.9rem; }
.disposition-actions :deep(.p-button.is-current) { background: var(--aw-teal-soft); }
thead .sticky { z-index: 7; background: var(--aw-raised); }
tbody .sticky { background: var(--aw-panel); }
.assertion-column, .assertion-cell { width: 15rem; min-width: 15rem; max-width: 15rem; }
.column-context { display: block; margin-top: .2rem; color: var(--aw-muted); font-style: italic; }
.column-counts { display: flex; flex-wrap: wrap; gap: .2rem; margin-top: .45rem; }
.column-counts button { padding: .12rem .3rem; border: 0; border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: var(--aw-muted); font-size: .65rem; cursor: pointer; }
.column-counts button[data-verdict='mismatch'] { color: var(--aw-danger); background: var(--aw-danger-soft); }
.column-counts button[data-verdict='match'] { color: var(--aw-ok); background: var(--aw-ok-soft); }
.column-counts button[data-verdict='missing_evidence'], .column-counts button[data-verdict='invalid_extraction'], .column-counts button[data-verdict='ambiguous'] { color: var(--aw-warn); background: var(--aw-warn-soft); }
.transaction-link, .cell-trigger { width: 100%; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.transaction-link { display: grid; gap: .15rem; }
.transaction-link strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--aw-teal); }
.transaction-link small, .missing-role-text { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.missing-role-text { display: block; margin-top: .25rem; color: var(--aw-warn); }
.assertion-cell { position: relative; padding: .35rem; }
.assertion-cell.selected { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.cell-trigger { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .2rem .4rem; align-items: center; min-height: 3.5rem; padding: .2rem; border-radius: var(--aw-radius-control); }
.cell-trigger:hover { background: var(--aw-raised); }
.cell-trigger span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--aw-text-sm); }
.cell-trigger small { grid-column: 2; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.cell-popover { position: absolute; z-index: 20; top: calc(100% - .25rem); right: .25rem; display: grid; gap: .6rem; width: min(28rem, calc(100vw - 4rem)); max-height: 28rem; overflow: auto; padding: .75rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); box-shadow: var(--aw-shadow-lg); }
.cell-popover header, .cell-popover header > div, .comparison-list, .comparison-list article { display: grid; gap: .2rem; }
.cell-popover header { grid-template-columns: minmax(0, 1fr) auto; }
.cell-popover header small, .comparison-list small, .muted { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.cell-display, .muted { margin: 0; }
.comparison-list { gap: .4rem; }
.comparison-list article { padding: .5rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.comparison-list article > div { display: flex; align-items: center; justify-content: space-between; gap: .4rem; }
.comparison-list p { margin: .1rem 0; font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); overflow-wrap: anywhere; }
@container workspace-panel (max-width: 78rem) {
  .grid-filters { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .search-filter { grid-column: span 2; }
}
@media (max-width: 52rem) {
  .grid-filters { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .search-filter { grid-column: 1 / -1; }
  .page-bar { align-items: flex-start; flex-direction: column; }
}
</style>
