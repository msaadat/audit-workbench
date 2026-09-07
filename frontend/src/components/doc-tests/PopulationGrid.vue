<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'

import { api } from '../../api'
import type {
  AuditFinding,
  ControlConclusion,
  DocTest,
  PopulationGrid,
  PopulationGridRow,
  PopulationRowOutcome,
} from '../../types'
import UiEmptyState from '../ui/UiEmptyState.vue'
import UiTestStatus from '../ui/UiTestStatus.vue'

/**
 * One population item as a table of records.
 *
 * The detail pane for a Q&A item was a stacked list of answer cards, one per
 * attached document. At eighty-four records that is eighty-four cards, and the
 * five that matter are somewhere in them. The server sorts exceptions first,
 * then what nobody could settle, then what has not run, then the accepted
 * majority — so the page reads as *the five that matter, then the rest*.
 *
 * Every number above the table is stated rather than implied. A run that
 * covered 79 of 84 records, or a population three of whose documents carry no
 * current reading, must never present itself as full coverage.
 */

const props = defineProps<{
  workspaceId: string
  testId: string
  itemId: string
  /** The test itself, for the two records it carries once the rows are settled:
   *  the control conclusion and the finding. The grid stands in for the item
   *  detail on a population item, so it has to carry them. */
  test: DocTest
  findings: AuditFinding[]
  busy: boolean
  running: boolean
}>()
const emit = defineEmits<{
  error: [summary: string, error: unknown]
  changed: []
  run: []
  openDocument: [documentId: string]
  saveConclusion: []
  generateFinding: [regenerate: boolean]
  openFinding: [findingId: string]
  openRcm: [rcmId: string]
}>()

const CONTROL_CONCLUSIONS: Array<{ label: string; value: ControlConclusion }> = [
  { label: 'Not concluded', value: 'no_conclusion' },
  { label: 'Effective', value: 'effective' },
  { label: 'Partially effective', value: 'partially_effective' },
  { label: 'Ineffective', value: 'ineffective' },
  { label: 'Not applicable', value: 'not_applicable' },
]
const savedConclusion = ref(props.test.control_conclusion)
watch(() => props.test.id, () => { savedConclusion.value = props.test.control_conclusion })
watch(() => props.test.control_conclusion, value => {
  if (working.value === null && !loading.value) savedConclusion.value = savedConclusion.value ?? value
})
const conclusionChanged = computed(
  () => props.test.control_conclusion !== savedConclusion.value,
)
/** Every record has a call, so the test is ready to be concluded over. */
const undispositioned = computed(() =>
  (payload.value?.rows ?? []).filter(row => row.disposition === 'pending').length,
)

const payload = ref<PopulationGrid | null>(null)
const loading = ref(false)
const loadError = ref<string | null>(null)
const offset = ref(0)
const limit = ref(50)
const outcomeFilter = ref<'' | PopulationRowOutcome>('')
const search = ref('')
const selectedKey = ref<string | null>(null)
const extraColumns = ref<string[]>([])
const working = ref<string | null>(null)
const resolving = ref(false)

const pageSizes = [25, 50, 100, 200]
const OUTCOMES: Array<{ value: '' | PopulationRowOutcome; label: string }> = [
  { value: '', label: 'All records' },
  { value: 'exception', label: 'Exceptions' },
  { value: 'needs_manual_check', label: 'Needs review' },
  { value: 'not_run', label: 'Not assessed' },
  { value: 'accepted', label: 'Accepted' },
]

const summary = computed(() => payload.value?.summary ?? null)
const columns = computed(() => {
  if (!payload.value) return []
  return [
    ...payload.value.identifier_columns,
    ...payload.value.field_columns,
    ...extraColumns.value,
  ].filter((name, index, all) => all.indexOf(name) === index)
})
const addableColumns = computed(() => {
  if (!payload.value) return []
  return payload.value.available_columns.filter(name => !columns.value.includes(name))
})
/** Client-side only, over the page the server already bounded. */
const rows = computed(() => {
  const needle = search.value.trim().toLowerCase()
  if (!needle) return payload.value?.rows ?? []
  return (payload.value?.rows ?? []).filter(row =>
    [row.document_title, row.answer, ...Object.values(row.values)]
      .join(' ')
      .toLowerCase()
      .includes(needle),
  )
})
const selected = computed(() =>
  rows.value.find(row => row.key === selectedKey.value)
  ?? payload.value?.rows.find(row => row.key === selectedKey.value)
  ?? null,
)
const acceptedUndispositioned = computed(() =>
  (payload.value?.rows ?? []).filter(
    row => row.outcome === 'accepted' && row.disposition === 'pending',
  ),
)
const hasPrevious = computed(() => offset.value > 0)
const hasNext = computed(() => {
  const page = payload.value?.page
  return Boolean(page && page.offset + page.limit < page.total)
})
const pageLabel = computed(() => {
  const page = payload.value?.page
  if (!page || !page.total) return 'No records'
  const first = page.offset + 1
  const last = Math.min(page.offset + page.limit, page.total)
  return `rows ${first}–${last} of ${page.total}`
})

/** The coverage strip: what the numbers below it rest on. */
const coverage = computed(() => {
  const value = summary.value
  if (!value) return null
  const unread = value.unread_documents.length
  const records = `${value.document_type} record${value.resolved === 1 ? '' : 's'}`
  // Before a run there is no coverage to characterise, and leading with "Full
  // population" over "0 of 13 assessed" reads as a claim contradicting itself.
  // The scope is stated once the run it describes exists.
  const parts = value.assessed
    ? [
        value.assurance_scope === 'full_population' ? 'Full population' : 'Partial coverage',
        `${value.assessed} of ${value.resolved} ${records} assessed`,
      ]
    : [`${value.resolved} ${records} resolved`, 'none assessed yet']
  if (value.omitted_records) {
    parts.push(
      value.capped
        ? `${value.omitted_records} beyond the cap not drawn`
        : `${value.omitted_records} outside the sample`,
    )
  }
  if (unread) {
    parts.push(`${unread} document${unread === 1 ? '' : 's'} of this type carry no current reading`)
  }
  return {
    headline: parts.join(' · '),
    behind: !value.run_current,
    unreadTitles: value.unread_documents.map(entry => entry.title || entry.document_id),
    outstanding: value.resolved - value.assessed,
  }
})

function displayValue(row: PopulationGridRow, column: string): string {
  if (row.missing_fields.includes(column)) return '—'
  return row.values[column] ?? '—'
}

function isMissing(row: PopulationGridRow, column: string): boolean {
  return row.missing_fields.includes(column)
}

function addColumn(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value) extraColumns.value = [...extraColumns.value, value]
  ;(event.target as HTMLSelectElement).value = ''
}

function removeColumn(name: string) {
  extraColumns.value = extraColumns.value.filter(value => value !== name)
}

async function loadGrid() {
  loading.value = true
  loadError.value = null
  try {
    const query = new URLSearchParams({
      offset: String(offset.value),
      limit: String(limit.value),
    })
    if (outcomeFilter.value) query.set('outcome', outcomeFilter.value)
    payload.value = await api.get<PopulationGrid>(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}`
      + `/items/${props.itemId}/grid?${query.toString()}`,
    )
    if (payload.value.page.offset !== offset.value) offset.value = payload.value.page.offset
  } catch (error) {
    payload.value = null
    loadError.value = error instanceof Error ? error.message : String(error)
    emit('error', 'Could not load the population grid', error)
  } finally {
    loading.value = false
  }
}

async function disposition(
  entries: Array<{ key: string; state: 'confirmed' | 'exception' | 'needs_review' | 'pending' }>,
  label: string,
) {
  if (!entries.length) return
  working.value = label
  try {
    await api.post(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}`
      + `/items/${props.itemId}/record-dispositions`,
      { dispositions: entries },
    )
    await loadGrid()
    emit('changed')
  } catch (error) {
    emit('error', 'Could not record the disposition', error)
  } finally {
    working.value = null
  }
}

function setRow(row: PopulationGridRow, state: 'confirmed' | 'exception' | 'needs_review' | 'pending') {
  return disposition([{ key: row.key, state }], row.key)
}

function confirmAllAccepted() {
  return disposition(
    acceptedUndispositioned.value.map(row => ({ key: row.key, state: 'confirmed' as const })),
    'bulk',
  )
}

async function resolvePopulation() {
  resolving.value = true
  try {
    await api.post(
      `/api/workspaces/${props.workspaceId}/doc-tests/${props.testId}`
      + `/items/${props.itemId}/resolve`,
      {},
    )
    await loadGrid()
    emit('changed')
  } catch (error) {
    emit('error', 'Could not re-resolve the population', error)
  } finally {
    resolving.value = false
  }
}

watch(
  () => [props.testId, props.itemId, offset.value, limit.value, outcomeFilter.value] as const,
  (current, previous) => {
    if (previous && (previous[0] !== current[0] || previous[1] !== current[1])) {
      offset.value = 0
      selectedKey.value = null
      extraColumns.value = []
    }
    void loadGrid()
  },
  { immediate: true },
)

defineExpose({ loadGrid })
</script>

<template>
  <section class="population-grid" aria-label="Population grid review">
    <template v-if="payload">
      <header class="grid-head">
        <div>
          <p class="eyebrow">Population · {{ payload.document_type }}</p>
          <h3>{{ payload.label || payload.item_id }}</h3>
          <p class="question">{{ payload.question }}</p>
          <p v-if="payload.criteria.length" class="criteria">
            Criteria:
            <span v-for="excerpt in payload.criteria" :key="excerpt.label" class="criteria-ref">
              {{ excerpt.label }}
            </span>
          </p>
        </div>
        <div class="grid-actions">
          <!-- The row this work counts as coverage of. Stated here because the
               grid stands in for the item detail on a population item, and the
               chip it carries is the only place the link is visible. -->
          <Button
            v-if="test.rcm_id"
            :label="test.rcm_id"
            icon="pi pi-map"
            size="small"
            outlined
            class="rcm-link"
            @click="emit('openRcm', test.rcm_id)"
          />
          <span v-else class="unlinked">Not linked to an RCM row</span>
          <Button
            label="Re-resolve"
            icon="pi pi-refresh"
            size="small"
            outlined
            :loading="resolving"
            :disabled="busy"
            @click="resolvePopulation"
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

      <!-- What the numbers rest on. A partial run must never read as full
           coverage, so the strip states the shortfall before the table. -->
      <div v-if="coverage" class="coverage" :data-behind="coverage.behind">
        <p class="coverage-line">{{ coverage.headline }}</p>
        <p v-if="coverage.behind" class="coverage-warn">
          {{ coverage.outstanding }} record{{ coverage.outstanding === 1 ? '' : 's' }} not yet assessed — run to update.
        </p>
        <p v-if="coverage.unreadTitles.length" class="coverage-warn">
          No current reading: {{ coverage.unreadTitles.join(', ') }}
        </p>
      </div>

      <div class="grid-filters" aria-label="Population grid filters">
        <label>
          <span>Outcome</span>
          <select v-model="outcomeFilter">
            <option v-for="option in OUTCOMES" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </label>
        <label class="search-filter">
          <span>Search this page</span>
          <input v-model="search" type="search" placeholder="Identifier, value, or answer" />
        </label>
        <label v-if="addableColumns.length">
          <span>Add a field column</span>
          <select @change="addColumn">
            <option value="">Choose a schema field</option>
            <option v-for="name in addableColumns" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
        <Button
          label="Confirm all accepted"
          icon="pi pi-check"
          size="small"
          outlined
          :disabled="busy || !acceptedUndispositioned.length || working === 'bulk'"
          :loading="working === 'bulk'"
          @click="confirmAllAccepted"
        />
      </div>

      <div class="page-bar">
        <span>{{ pageLabel }}</span>
        <div>
          <label class="page-size">
            Rows
            <select :value="limit" @change="limit = Number(($event.target as HTMLSelectElement).value); offset = 0">
              <option v-for="size in pageSizes" :key="size" :value="size">{{ size }}</option>
            </select>
          </label>
          <Button icon="pi pi-chevron-left" text rounded aria-label="Previous page" :disabled="!hasPrevious || loading" @click="offset = Math.max(0, offset - limit)" />
          <Button icon="pi pi-chevron-right" text rounded aria-label="Next page" :disabled="!hasNext || loading" @click="offset += limit" />
        </div>
      </div>

      <div class="grid-body">
        <div class="grid-scroll" tabindex="0" aria-label="Scrollable population records">
          <table>
            <thead>
              <tr>
                <th class="status-column" scope="col">Verdict</th>
                <th v-for="column in columns" :key="column" scope="col">
                  {{ column }}
                  <button
                    v-if="extraColumns.includes(column)"
                    type="button"
                    class="drop-column"
                    :aria-label="`Remove the ${column} column`"
                    @click="removeColumn(column)"
                  >×</button>
                </th>
                <th class="reason-column" scope="col">Reason</th>
                <th class="call-column" scope="col">Your call</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row.key"
                :class="{ selected: row.key === selectedKey }"
                @click="selectedKey = row.key === selectedKey ? null : row.key"
              >
                <td class="status-column"><UiTestStatus :status="row.outcome" /></td>
                <td v-for="column in columns" :key="column" :class="{ missing: isMissing(row, column) }">
                  <span :title="displayValue(row, column)">{{ displayValue(row, column) }}</span>
                  <small v-if="isMissing(row, column)">not stated</small>
                </td>
                <td class="reason-column"><span :title="row.conclusion || row.answer">{{ row.conclusion || row.answer || '—' }}</span></td>
                <td class="call-column" @click.stop>
                  <UiTestStatus v-if="row.disposition !== 'pending'" :status="row.disposition" showLabel />
                  <span class="call-actions">
                    <Button
                      icon="pi pi-check"
                      text rounded size="small"
                      v-tooltip.top="'Confirm'"
                      :aria-label="`Confirm ${row.document_title} record ${row.record_index}`"
                      :disabled="busy || row.outcome === 'not_run' || working === row.key"
                      :class="{ 'is-current': row.disposition === 'confirmed' }"
                      @click="setRow(row, 'confirmed')"
                    />
                    <Button
                      icon="pi pi-exclamation-triangle"
                      text rounded size="small" severity="danger"
                      v-tooltip.top="'Exception'"
                      :aria-label="`Mark ${row.document_title} record ${row.record_index} an exception`"
                      :disabled="busy || row.outcome === 'not_run' || working === row.key"
                      :class="{ 'is-current': row.disposition === 'exception' }"
                      @click="setRow(row, 'exception')"
                    />
                    <Button
                      icon="pi pi-eye"
                      text rounded size="small" severity="warning"
                      v-tooltip.top="'Needs review'"
                      :aria-label="`Send ${row.document_title} record ${row.record_index} to review`"
                      :disabled="busy || row.outcome === 'not_run' || working === row.key"
                      :class="{ 'is-current': row.disposition === 'needs_review' }"
                      @click="setRow(row, 'needs_review')"
                    />
                    <Button
                      icon="pi pi-refresh"
                      text rounded size="small" severity="secondary"
                      v-tooltip.top="'Clear'"
                      :aria-label="`Clear the call on ${row.document_title} record ${row.record_index}`"
                      :disabled="busy || row.disposition === 'pending' || working === row.key"
                      @click="setRow(row, 'pending')"
                    />
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
          <UiEmptyState
            v-if="!rows.length"
            icon="pi pi-filter-slash"
            title="No record matches"
            description="Clear the filter or the search to see this page."
            compact
          />
        </div>

        <!-- The per-record answer card, as a pane rather than one of eighty-four
             stacked cards. -->
        <aside v-if="selected" class="detail-pane" aria-label="Record assessment">
          <header>
            <div>
              <strong>{{ selected.document_title || selected.document_id }}</strong>
              <small>record {{ selected.record_index }} · {{ selected.document_id }}</small>
            </div>
            <Button icon="pi pi-times" text rounded size="small" aria-label="Close record detail" @click="selectedKey = null" />
          </header>
          <UiTestStatus :status="selected.outcome" showLabel />
          <p v-if="selected.answer" class="answer">{{ selected.answer }}</p>
          <p v-else class="muted">This record has not been assessed yet.</p>
          <dl v-if="Object.keys(selected.values).length" class="fields">
            <template v-for="column in columns" :key="column">
              <dt>{{ column }}</dt>
              <dd>
                {{ displayValue(selected, column) }}
                <small v-if="selected.field_citations[column]">cited {{ selected.field_citations[column] }}</small>
              </dd>
            </template>
          </dl>
          <ul v-if="selected.evidence_refs.length" class="anchors">
            <li v-for="anchor in selected.evidence_refs" :key="anchor.id ?? anchor.excerpt">
              <span>{{ anchor.excerpt }}</span>
              <small>page {{ anchor.page }}</small>
            </li>
          </ul>
          <p v-if="selected.disposition_note" class="muted">Your note: {{ selected.disposition_note }}</p>
          <Button
            label="Open the document"
            icon="pi pi-external-link"
            size="small"
            outlined
            @click="emit('openDocument', selected.document_id)"
          />
        </aside>
      </div>

      <!-- The two records the test carries once its rows are answered for. The
           grid stands in for the item detail here, so they stand here too. -->
      <div class="footer-row">
        <div class="footer-cell">
          <p class="aw-label">Conclusion</p>
          <select v-model="test.control_conclusion" class="conclusion-select">
            <option v-for="option in CONTROL_CONCLUSIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <Button
            v-if="conclusionChanged"
            label="Save"
            icon="pi pi-check"
            size="small"
            :disabled="busy"
            @click="emit('saveConclusion')"
          />
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
              :disabled="busy || !test.rcm_id"
              @click="emit('generateFinding', true)"
            />
          </template>
          <template v-else>
            <span class="footer-note">{{ test.rcm_id ? 'None yet.' : 'Not linked to an RCM row.' }}</span>
            <Button
              label="Generate finding"
              icon="pi pi-sparkles"
              size="small" text severity="secondary"
              :disabled="busy || !test.rcm_id"
              @click="emit('generateFinding', false)"
            />
          </template>
        </div>
        <!-- A warning, not a block: concluding over open records is the
             auditor's call, and the backend records what was open. -->
        <p v-if="undispositioned" class="footer-warn">
          {{ undispositioned }} of {{ payload.page.total }} records carry no call yet. You can
          still conclude — it will be recorded as a scope limitation.
        </p>
      </div>
    </template>

    <UiEmptyState
      v-else-if="loadError"
      icon="pi pi-exclamation-triangle"
      title="Population grid unavailable"
      :description="loadError"
      compact
    >
      <Button label="Try again" icon="pi pi-refresh" size="small" outlined @click="loadGrid" />
    </UiEmptyState>
    <UiEmptyState
      v-else
      icon="pi pi-spin pi-spinner"
      title="Loading the population"
      description="Resolving the type against the documents this engagement holds."
      compact
    />
  </section>
</template>

<style scoped>
.population-grid { display: flex; flex-direction: column; gap: var(--aw-space-4); min-width: 0; padding: 1rem; border-radius: var(--aw-radius-surface); background: var(--aw-panel); }
.grid-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.grid-head h3 { margin: .15rem 0 0; font-size: var(--aw-text-xl); }
.grid-actions { display: flex; align-items: center; gap: .4rem; }
.rcm-link { flex: 0 0 auto; border-color: var(--aw-teal-line); color: var(--aw-teal); white-space: nowrap; }
.unlinked { color: var(--aw-warn-ink); font-size: var(--aw-text-sm); white-space: nowrap; }
.eyebrow { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; text-transform: uppercase; }
.question { margin: .3rem 0 0; color: var(--aw-ink); font-size: var(--aw-text-sm); }
.criteria { margin: .3rem 0 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.criteria-ref { margin-left: .35rem; padding: .1rem .4rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); }

.coverage { padding: .55rem .7rem; border-left: 3px solid var(--aw-ok); border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.coverage[data-behind='true'] { border-left-color: var(--aw-warn); }
.coverage-line { margin: 0; color: var(--aw-ink); font-size: var(--aw-text-sm); }
.coverage-warn { margin: .25rem 0 0; color: var(--aw-warn); font-size: var(--aw-text-xs); }

.grid-filters { display: grid; grid-template-columns: minmax(9rem, 1fr) minmax(12rem, 2fr) minmax(9rem, 1fr) auto; gap: .55rem; align-items: end; }
.grid-filters label { display: grid; gap: .25rem; min-width: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
.grid-filters input, .grid-filters select, .page-size select { width: 100%; min-width: 0; min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }

.page-bar { display: flex; align-items: center; justify-content: space-between; gap: .8rem; color: var(--aw-muted); font-size: var(--aw-text-sm); }
.page-bar > div { display: flex; align-items: center; gap: .2rem; }
.page-size { display: flex; align-items: center; gap: .35rem; }
.page-size select { width: 4.5rem; min-height: 2rem; }

.grid-body { display: grid; grid-template-columns: minmax(0, 1fr); gap: .75rem; min-width: 0; }
.grid-body:has(.detail-pane) { grid-template-columns: minmax(0, 1fr) minmax(18rem, 24rem); }
.grid-scroll { min-width: 0; max-height: min(58vh, 42rem); overflow: auto; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
table { width: max-content; min-width: 100%; border-collapse: separate; border-spacing: 0; }
th, td { max-width: 18rem; padding: .5rem .55rem; border-right: 1px solid var(--aw-border); border-bottom: 1px solid var(--aw-border); background: var(--aw-panel); text-align: left; vertical-align: top; }
thead th { position: sticky; top: 0; z-index: 5; background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; }
tbody tr { cursor: pointer; }
tbody tr:hover td { background: var(--aw-raised); }
tbody tr.selected td { background: var(--aw-teal-soft); }
td span { display: block; overflow: hidden; font-size: var(--aw-text-sm); text-overflow: ellipsis; white-space: nowrap; }
td small { color: var(--aw-muted); font-size: var(--aw-text-2xs); }
td.missing span { color: var(--aw-muted); }
.status-column { width: 3.5rem; min-width: 3.5rem; }
.reason-column { width: 20rem; min-width: 14rem; }
.call-column { width: 11rem; min-width: 11rem; }
.call-actions { display: flex; gap: .05rem; margin-top: .2rem; }
.call-actions :deep(.p-button) { width: 1.85rem; height: 1.85rem; }
.call-actions :deep(.p-button.is-current) { background: var(--aw-teal-soft); }
.drop-column { margin-left: .3rem; padding: 0 .25rem; border: 0; border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: var(--aw-muted); cursor: pointer; }

.detail-pane { display: flex; flex-direction: column; gap: .6rem; align-content: start; max-height: min(58vh, 42rem); overflow: auto; padding: .8rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); }
.detail-pane header { display: flex; align-items: flex-start; justify-content: space-between; gap: .5rem; }
.detail-pane header small { display: block; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.answer { margin: 0; color: var(--aw-ink); font-size: var(--aw-text-sm); }
.muted { margin: 0; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.fields { display: grid; grid-template-columns: minmax(0, auto) minmax(0, 1fr); gap: .2rem .6rem; margin: 0; }
.fields dt { color: var(--aw-muted); font-size: var(--aw-text-xs); }
.fields dd { margin: 0; font-family: var(--aw-font-mono); font-size: var(--aw-text-sm); overflow-wrap: anywhere; }
.fields dd small { display: block; color: var(--aw-muted); font-family: var(--aw-font-sans, inherit); font-size: var(--aw-text-2xs); }
.anchors { display: grid; gap: .35rem; margin: 0; padding: 0; list-style: none; }
.anchors li { padding: .4rem .5rem; border-radius: var(--aw-radius-control); background: var(--aw-raised); }
.anchors li span { font-size: var(--aw-text-sm); }
.anchors li small { color: var(--aw-muted); font-size: var(--aw-text-2xs); }

.footer-row { display: flex; flex-wrap: wrap; align-items: center; gap: .75rem 1.5rem; padding-top: .75rem; border-top: 1px solid var(--aw-border); }
.footer-cell { display: flex; align-items: center; gap: .5rem; }
.footer-cell .aw-label { margin: 0; }
.conclusion-select { min-width: 10rem; min-height: 2.25rem; padding: .35rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-control); background: var(--aw-panel); color: var(--aw-ink); font: inherit; }
.footer-note { color: var(--aw-muted); font-size: var(--aw-text-sm); }
.footer-warn { flex: 1 1 100%; margin: 0; color: var(--aw-warn); font-size: var(--aw-text-xs); }
.finding-chip { display: inline-flex; align-items: center; gap: .35rem; padding: .2rem .5rem; border: 1px solid var(--aw-border-strong); border-radius: var(--aw-radius-pill); background: var(--aw-panel); color: inherit; font: inherit; cursor: pointer; }
.finding-id { color: var(--aw-teal); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }

@container workspace-panel (max-width: 78rem) {
  .grid-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-body:has(.detail-pane) { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 52rem) {
  .grid-head { flex-direction: column; }
  .page-bar { align-items: flex-start; flex-direction: column; }
}
</style>
