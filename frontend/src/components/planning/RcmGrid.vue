<script setup lang="ts">
import { computed, ref } from 'vue'

import type { FindingRollups, FindingSummary, RcmRow } from '../../types'
import { plural } from '../../format'

/**
 * The matrix as a matrix: what each risk is, what covers it, and what was
 * concluded.
 *
 * It used to be twelve columns of inline editors — textareas with resize
 * handles, a rating select, a review select, reorder arrows — inside a
 * horizontally scrolling `DataTable` with two frozen columns. The six that
 * carried the verdicts sat past the right edge at 1440px, so a reader saw risk
 * text and attribute text and none of the conclusions. Reading and editing are
 * different acts: this reads, and the drawer beside it edits.
 *
 * Rows group by process because that is how an engagement is walked and how a
 * gap reads — "two of the five requisition risks have no control" is a
 * sentence about a stage of the business, not about a matrix.
 */

const props = defineProps<{
  rows: RcmRow[]
  findingRollups?: FindingRollups
  /** The row the drawer beside the grid has open. */
  selectedId?: string | null
}>()
const emit = defineEmits<{ open: [RcmRow] }>()

const CONCLUSIONS: Record<string, { label: string; tone: string }> = {
  effective: { label: 'Effective', tone: 'ok' },
  partially_effective: { label: 'Partially effective', tone: 'warn' },
  ineffective: { label: 'Ineffective', tone: 'bad' },
  not_applicable: { label: 'Not applicable', tone: 'neutral' },
}

const collapsed = ref<Set<string>>(new Set())
function toggle(process: string) {
  const next = new Set(collapsed.value)
  if (next.has(process)) next.delete(process)
  else next.add(process)
  collapsed.value = next
}

/**
 * Groups in first-seen order, so the grid keeps whatever order the matrix is
 * stored in rather than imposing an alphabet on a process sequence.
 */
const groups = computed(() => {
  const byProcess = new Map<string, RcmRow[]>()
  for (const row of props.rows) {
    const key = String(row.process ?? '').trim() || 'Unassigned'
    const bucket = byProcess.get(key)
    if (bucket) bucket.push(row)
    else byProcess.set(key, [row])
  }
  return [...byProcess].map(([process, rows]) => ({
    process,
    rows,
    // The sentence the group header carries: how much, and what is wrong with
    // it. Clauses that count nothing are dropped rather than printed as zero.
    summary: [
      plural(rows.length, 'risk'),
      countOf(rows, row => conclusion(row) === 'ineffective', 'ineffective'),
      countOf(rows, row => !controlOf(row), 'without a control'),
      countOf(rows, row => findingsFor(row).length > 0, 'with a finding'),
    ].filter(Boolean).join(' · '),
  }))
})

function countOf(rows: RcmRow[], predicate: (row: RcmRow) => boolean, suffix: string) {
  const total = rows.filter(predicate).length
  return total ? `${total} ${suffix}` : ''
}
function controlOf(row: RcmRow) { return String(row.control ?? '').trim() }
function conclusion(row: RcmRow) {
  return String(row.execution_rollup.control_conclusion ?? '') || 'no_conclusion'
}
function conclusionMeta(row: RcmRow) {
  return CONCLUSIONS[conclusion(row)] ?? { label: 'No conclusion', tone: 'neutral' }
}
function testCount(row: RcmRow) { return row.execution_rollup.tests ?? row.test_refs.length }
function exceptions(row: RcmRow) { return row.execution_rollup.exceptions ?? 0 }
function findingsFor(row: RcmRow): FindingSummary[] {
  return props.findingRollups?.by_rcm[row.id] ?? []
}
/** The `RCM-` prefix is on every row, so it identifies nothing. */
function shortId(row: RcmRow) { return row.id.replace(/^RCM-/, '') }
</script>

<template>
  <div class="rcm-grid">
    <div class="head" role="row">
      <span>Risk</span><span>Statement</span><span>Control</span><span>Tests</span>
      <span>Conclusion</span><span>Finding</span><span>Review</span><span />
    </div>

    <template v-for="group in groups" :key="group.process">
      <button
        type="button"
        class="group"
        :aria-expanded="!collapsed.has(group.process)"
        @click="toggle(group.process)"
      >
        <i class="pi" :class="collapsed.has(group.process) ? 'pi-chevron-right' : 'pi-chevron-down'" />
        <span class="group-name">{{ group.process }}</span>
        <span class="group-summary aw-figure">{{ group.summary }}</span>
      </button>

      <template v-if="!collapsed.has(group.process)">
        <button
          v-for="row in group.rows"
          :key="row.id"
          type="button"
          class="row"
          :class="{ selected: row.id === selectedId }"
          @click="emit('open', row)"
        >
          <span class="identity">
            <span class="row-id">{{ shortId(row) }}</span>
            <span class="rating" :data-rating="row.risk_rating">
              <span class="rating-dot" />{{ row.risk_rating }}
            </span>
          </span>

          <span class="statement">{{ row.risk }}</span>

          <span v-if="controlOf(row)" class="control">{{ controlOf(row) }}</span>
          <span v-else class="no-control">
            <i class="pi pi-info-circle" />No control identified
          </span>

          <span class="tests aw-figure">
            {{ plural(testCount(row), 'test') }} ·
            <b v-if="exceptions(row)" class="exceptions">{{ exceptions(row) }} exc</b>
            <template v-else>0 exc</template>
          </span>

          <span>
            <span class="pill" :data-tone="conclusionMeta(row).tone">{{ conclusionMeta(row).label }}</span>
          </span>

          <span class="findings">
            <template v-if="findingsFor(row).length">
              <!-- A span, not a nested button: the row itself is the control,
                   and the finding opens from the drawer or the row page. -->
              <span class="finding-chip">
                <span class="finding-id">{{ findingsFor(row)[0].id }}</span>{{ findingsFor(row)[0].severity }}
              </span>
              <span v-if="findingsFor(row).length > 1" class="more">+{{ findingsFor(row).length - 1 }}</span>
            </template>
            <span v-else class="none">—</span>
          </span>

          <span class="review" :data-reviewed="row.review_status === 'reviewed'">
            <i v-if="row.review_status === 'reviewed'" class="pi pi-check-circle" />
            <span v-else class="draft-ring" />
            {{ row.review_status === 'reviewed' ? 'Reviewed' : 'Draft' }}
          </span>

          <i class="pi pi-chevron-right go" />
        </button>
      </template>
    </template>

    <p v-if="!rows.length" class="empty">No row matches this filter.</p>
  </div>
</template>

<style scoped>
.rcm-grid {
  display: flex; flex-direction: column; min-width: 0; overflow: hidden;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}

/* One template, shared by the header and every row, so the columns line up
   without a table and without the frozen-column shadows the old grid needed. */
.head, .row {
  display: grid;
  grid-template-columns: 6rem minmax(0, 1.2fr) minmax(0, 1.2fr) 7.5rem 8.125rem 8rem 5.25rem 1.75rem;
  gap: 0 .875rem;
  align-items: center;
  min-width: 0;
}
.head {
  padding: .5rem 1rem;
  background: var(--aw-raised);
  color: var(--aw-muted);
  font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
}

.group {
  display: flex; align-items: center; gap: .625rem;
  width: 100%; padding: .4375rem 1rem;
  border: 0; border-top: 1px solid var(--aw-border);
  background: var(--aw-canvas); color: inherit; font: inherit;
  text-align: left; cursor: pointer;
}
.group:hover { background: var(--aw-raised); }
.group:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.group .pi { flex: none; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.group-name { color: var(--aw-ink-strong); font-size: var(--aw-text-base); font-weight: 600; }
.group-summary { color: var(--aw-muted); font-size: var(--aw-text-xs); }

.row {
  width: 100%; padding: .625rem 1rem;
  border: 0; border-top: 1px solid var(--aw-border);
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.row:hover:not(.selected) { background: var(--aw-raised); }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.row.selected { background: var(--aw-teal-soft); }

.identity { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.row-id { color: var(--aw-ink-strong); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; }
.rating { display: inline-flex; align-items: center; gap: .3125rem; font-size: var(--aw-text-xs); font-weight: 600; text-transform: capitalize; }
.rating-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--aw-muted); }
.rating[data-rating='critical'] { color: var(--aw-danger-ink); }
.rating[data-rating='critical'] .rating-dot { background: var(--aw-danger-ink); }
.rating[data-rating='high'] { color: var(--aw-danger); }
.rating[data-rating='high'] .rating-dot { background: var(--aw-danger); }
.rating[data-rating='medium'] { color: var(--aw-warn-ink); }
.rating[data-rating='medium'] .rating-dot { background: var(--aw-warn); }
.rating[data-rating='low'] { color: var(--aw-low-ink); }
.rating[data-rating='low'] .rating-dot { background: var(--aw-low); }

/* Two lines, clamped: one row with a six-line risk statement must not set the
   height of every row in the matrix. The whole text is in the drawer. */
.statement, .control {
  display: -webkit-box; overflow: hidden;
  font-size: var(--aw-text-base); line-height: 1.4;
  -webkit-box-orient: vertical; -webkit-line-clamp: 2;
}
.statement { color: var(--aw-ink); }
.control { color: var(--aw-ink-soft); }
.no-control { display: inline-flex; align-items: center; gap: .375rem; color: var(--aw-warn-ink); font-size: var(--aw-text-base); font-style: italic; }
.no-control .pi { font-size: var(--aw-text-xs); }

/* One line: "2 tests · 0 exc" wrapping made a row half a line taller than the
   ones around it, which is the raggedness the fixed clamps above exist to
   prevent. */
.tests { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); white-space: nowrap; }
.exceptions { color: var(--aw-danger); font-weight: 600; }

.pill {
  display: inline-flex; padding: .125rem .5625rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-pill);
  background: var(--aw-panel); color: var(--aw-ink-soft);
  font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap;
}
.pill[data-tone='ok'] { border-color: var(--aw-ok-line); background: var(--aw-ok-soft); color: var(--aw-ok); }
.pill[data-tone='warn'] { border-color: var(--aw-warn-line); background: var(--aw-warn-soft); color: var(--aw-warn-ink); }
.pill[data-tone='bad'] { border-color: var(--aw-danger-line); background: var(--aw-danger-soft); color: var(--aw-danger-ink); }

.findings { display: flex; align-items: center; gap: .375rem; min-width: 0; }
.finding-chip {
  display: inline-flex; align-items: center; gap: .3125rem;
  padding: .125rem .5rem;
  border: 1px solid var(--aw-warn-line); border-radius: var(--aw-radius-control);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-size: var(--aw-text-xs); font-weight: 600; white-space: nowrap;
}
.finding-chip .finding-id { font-family: var(--aw-font-mono); }
.findings .more { color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }
.findings .none { color: var(--aw-border-strong); font-size: var(--aw-text-sm); }

.review { display: inline-flex; align-items: center; gap: .3125rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
.review[data-reviewed='true'] { color: var(--aw-ok); }
.review .pi { font-size: var(--aw-text-sm); }
.draft-ring { width: 12px; height: 12px; border: 1.5px dashed var(--aw-border-strong); border-radius: 50%; }

.go { color: var(--aw-border-strong); font-size: var(--aw-text-sm); }
.row.selected .go { color: var(--aw-teal); }

.empty { margin: 0; padding: 1.5rem 1rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }

/* Below this the verdict columns are what survive: the two prose columns are
   the ones a reader can open the row for. */
@container workspace-panel (max-width: 66rem) {
  .head, .row { grid-template-columns: 6rem minmax(0, 1.4fr) 6.875rem 8.125rem 5.25rem 1.75rem; }
  .head > :nth-child(3), .row > :nth-child(3) { display: none; }
  .head > :nth-child(6), .row > :nth-child(6) { display: none; }
}
</style>
