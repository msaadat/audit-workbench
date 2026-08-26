<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Button from 'primevue/button'

import { api } from '../../api'
import type {
  CycleAssertionMutationResponse,
  CycleAssertionVerdict,
  CycleEvaluationState,
  CycleVouchGridComparison,
  CycleVouchGridPayload,
  CycleVouchMetadata,
} from '../../types'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'
import CycleAssertionDialog from './CycleAssertionDialog.vue'
import {
  EMPTY_CYCLE_GRID_FILTERS,
  cycleGridPageLabel,
  filterCycleGridRows,
  type CycleGridFilters,
} from './cycleGridState'

const props = defineProps<{
  workspaceId: string
  testId: string
  running: boolean
  busy: boolean
  metadata: CycleVouchMetadata | null
}>()
const emit = defineEmits<{
  close: []
  error: [summary: string, error: unknown]
  openDetail: [itemId: string, assertionKey: string | null]
  run: []
  changed: []
}>()

const payload = ref<CycleVouchGridPayload | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const offset = ref(0)
const limit = ref(100)
const filters = reactive<CycleGridFilters>({ ...EMPTY_CYCLE_GRID_FILTERS })
const selectedCell = ref<{ itemId: string; assertionKey: string } | null>(null)
const scrollContainer = ref<HTMLElement | null>(null)
const authorOpen = ref(false)

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
  return row.disposition_stale ? 'stale' : row.disposition_state
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

async function assertionSaved(_response: CycleAssertionMutationResponse) {
  selectedCell.value = null
  await loadGrid()
  emit('changed')
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
        <Button label="All document work" icon="pi pi-arrow-left" text size="small" @click="emit('close')" />
        <p class="eyebrow">Cycle vouch · {{ payload?.population.table ?? 'Loading population' }}</p>
        <h3>{{ payload?.title ?? 'Cycle vouch review' }}</h3>
      </div>
      <div class="grid-actions">
        <Button
          label="Add or change assertion"
          icon="pi pi-plus"
          size="small"
          outlined
          :disabled="busy || !payload || !metadata"
          @click="authorOpen = true"
        />
        <Button
          label="Run test"
          icon="pi pi-play"
          size="small"
          :loading="running"
          :disabled="busy"
          @click="emit('run')"
        />
      </div>
    </header>

    <div v-if="payload" class="grid-summary" aria-label="Cycle grid summary">
      <span><strong>{{ payload.page.total }}</strong> selected items</span>
      <span><strong>{{ payload.columns.length }}</strong> assertions</span>
      <span><strong>{{ payload.tested_item_counts.failed ?? 0 }}</strong> failed items</span>
      <span><strong>{{ payload.assertion_counts.mismatch ?? 0 }}</strong> mismatched cells</span>
    </div>

    <div v-if="payload" class="grid-filters" aria-label="Grid filters">
      <label class="search-filter">
        <span>Search bounded grid fields</span>
        <input v-model="filters.search" type="search" placeholder="Transaction, document, role, or display value" />
      </label>
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
          <option v-for="column in payload.columns" :key="column.key" :value="column.key">{{ column.label }}</option>
        </select>
      </label>
      <label>
        <span>Assertion verdict</span>
        <select v-model="filters.assertionVerdict" :disabled="!filters.assertionKey">
          <option v-for="option in verdictOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        </select>
      </label>
      <Button label="Clear filters" icon="pi pi-filter-slash" text size="small" @click="clearFilters" />
    </div>

    <div v-if="payload" class="page-bar">
      <span>{{ rows.length }} matching item{{ rows.length === 1 ? '' : 's' }} on this page · {{ pageLabel }}</span>
      <div>
        <label class="page-size">Rows <select :value="limit" @change="changePageSize"><option v-for="size in pageSizes" :key="size" :value="size">{{ size }}</option></select></label>
        <Button icon="pi pi-chevron-left" text rounded aria-label="Previous grid page" :disabled="!hasPrevious || loading" @click="previousPage" />
        <Button icon="pi pi-chevron-right" text rounded aria-label="Next grid page" :disabled="!hasNext || loading" @click="nextPage" />
      </div>
    </div>

    <div v-if="payload" ref="scrollContainer" class="grid-scroll" tabindex="0" aria-label="Scrollable Cycle vouch results">
      <table>
        <thead>
          <tr>
            <th class="sticky transaction-column" scope="col">Transaction</th>
            <th class="sticky evaluation-column" scope="col">Evaluation</th>
            <th class="sticky disposition-column" scope="col">Disposition</th>
            <th v-for="column in payload.columns" :key="column.key" class="assertion-column" scope="col">
              <strong>{{ column.label }}</strong>
              <small>{{ label(column.operator) }}<template v-if="column.applicable_roles.length"> · {{ column.applicable_roles.map(label).join(', ') }}</template></small>
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
                  icon="pi pi-check"
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
                  icon="pi pi-exclamation-triangle"
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
                  icon="pi pi-refresh"
                  text
                  rounded
                  size="small"
                  severity="secondary"
                  :aria-label="`Clear the disposition on ${row.label || row.item_id}`"
                  v-tooltip.top="'Clear'"
                  :disabled="busy || dispositioning === row.item_id || row.disposition_state === 'pending'"
                  @click="setDisposition(row, 'pending')"
                />
              </span>
            </td>
            <td
              v-for="column in payload.columns"
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
                  <Button icon="pi pi-times" text rounded size="small" aria-label="Close comparison detail" @click="selectedCell = null" />
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
                <Button label="Open assertion evidence" icon="pi pi-arrow-right" size="small" @click="openDetail(row.item_id, column.key)" />
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <UiEmptyState v-if="!rows.length" icon="pi pi-filter-slash" title="No items match" description="Clear or adjust the grid filters to see this page." compact />
    </div>

    <UiEmptyState v-else-if="loadError" icon="pi pi-exclamation-triangle" title="Cycle grid unavailable" :description="loadError" compact>
      <Button label="Try again" icon="pi pi-refresh" size="small" outlined @click="loadGrid" />
    </UiEmptyState>
    <UiEmptyState v-else icon="pi pi-spin pi-spinner" title="Loading Cycle vouch grid" description="Reading the bounded grid projection." compact />

    <CycleAssertionDialog
      v-if="payload"
      v-model="authorOpen"
      :workspaceId="workspaceId"
      :testId="testId"
      :expectedTestSha1="payload.test_sha1"
      :metadata="metadata"
      @saved="assertionSaved"
      @error="(summary, error) => emit('error', summary, error)"
    />
  </section>
</template>

<style scoped>
.cycle-grid { display: flex; flex-direction: column; gap: var(--aw-space-4); min-width: 0; padding: 1rem; border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.grid-head, .grid-actions, .grid-summary, .page-bar, .page-bar > div { display: flex; align-items: center; }
.grid-head { justify-content: space-between; gap: 1rem; }
.grid-head h3 { margin: .15rem 0 0; font-size: var(--aw-text-xl); }
.eyebrow { margin: .15rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; text-transform: uppercase; }
.grid-summary { flex-wrap: wrap; gap: .45rem; }
.grid-summary span { padding: .35rem .6rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-xs); }
.grid-summary strong { color: var(--aw-ink); }
.grid-filters { display: grid; grid-template-columns: minmax(14rem, 2fr) repeat(5, minmax(8rem, 1fr)) auto; gap: .55rem; align-items: end; }
.grid-filters label { display: grid; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.grid-filters input, .grid-filters select, .page-size select { width: 100%; min-width: 0; min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
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
