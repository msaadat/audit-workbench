<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'

import type { FindingRollups, RcmRow } from '../../types'
import { coverageColumns, nextMove, testCount } from './coverage'

/**
 * The RCM as a statement about assurance rather than a spreadsheet.
 *
 * The grid shows eleven columns of stored state per row, which at rest means
 * seventeen rows all reading NOT ASSESSED / NO_CONCLUSION / DRAFT — technically
 * complete and unreadable at a glance. This groups the same records by where
 * they stand and prints the agent's next move on each card, so "two critical
 * risks have no test" is the first thing on the screen rather than a cell.
 *
 * It renders the same `rows` array the grid does and owns no state of its own.
 */

const props = defineProps<{
  rows: RcmRow[]
  findingRollups?: FindingRollups
  generating?: boolean
  canGenerate?: boolean
}>()
const emit = defineEmits<{ open: [RcmRow]; generate: [string[]] }>()

const columns = computed(() => coverageColumns(props.rows))
const uncovered = computed(() => columns.value[0].rows)
const uncoveredSevere = computed(() => columns.value[0].severe)
</script>

<template>
  <div class="board-wrap">
    <div v-if="uncovered.length" class="board-alert" :class="{ severe: uncoveredSevere > 0 }">
      <i :class="uncoveredSevere ? 'pi pi-exclamation-triangle' : 'pi pi-info-circle'" />
      <span>
        <strong v-if="uncoveredSevere">{{ uncoveredSevere }} critical or high risk{{ uncoveredSevere === 1 ? '' : 's' }} have no test.</strong>
        <strong v-else>{{ uncovered.length }} risk{{ uncovered.length === 1 ? '' : 's' }} have no test.</strong>
        Nothing in the file supports a conclusion on {{ uncoveredSevere ? 'them' : 'these rows' }}.
      </span>
      <Button
        label="Ask the agent to cover them"
        icon="pi pi-sparkles"
        size="small"
        :loading="generating"
        :disabled="!canGenerate"
        @click="emit('generate', uncovered.map(row => row.id))"
      />
    </div>

    <div class="board">
      <section v-for="column in columns" :key="column.state" class="column" :data-state="column.state">
        <header>
          <strong>{{ column.label }}</strong>
          <em>{{ column.rows.length }}</em>
          <span v-if="column.state === 'no_coverage' && column.severe" class="severe-chip">
            {{ column.severe }} critical/high
          </span>
        </header>

        <button
          v-for="row in column.rows"
          :key="row.id"
          class="card"
          :data-rating="row.risk_rating"
          @click="emit('open', row)"
        >
          <span class="card-id">{{ row.id }}</span>
          <span class="card-title">{{ row.risk || row.process || 'Untitled risk' }}</span>
          <span class="card-tags">
            <span class="rating" :data-rating="row.risk_rating">{{ row.risk_rating }}</span>
            <span class="chip">{{ testCount(row) }} test{{ testCount(row) === 1 ? '' : 's' }}</span>
            <span v-if="row.execution_rollup.exceptions" class="chip bad">
              {{ row.execution_rollup.exceptions }} exception{{ row.execution_rollup.exceptions === 1 ? '' : 's' }}
            </span>
            <span v-for="finding in findingRollups?.by_rcm[row.id] ?? []" :key="finding.id" class="chip finding">
              {{ finding.id }}
            </span>
          </span>
          <span class="card-next">{{ nextMove(row, column.state) }}</span>
        </button>

        <p v-if="!column.rows.length" class="column-empty">None</p>
      </section>
    </div>
  </div>
</template>

<style scoped>
.board-wrap { display: flex; flex-direction: column; gap: 0.8rem; min-width: 0; }

.board-alert {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem 0.8rem;
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-muted);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  font-size: var(--aw-text-sm);
}
.board-alert.severe { border-left-color: var(--aw-danger); background: var(--aw-danger-soft); }
.board-alert > i { color: var(--aw-muted); }
.board-alert.severe > i { color: var(--aw-danger); }
.board-alert > span { flex: 1; min-width: 0; color: var(--aw-ink-soft); }
.board-alert strong { color: var(--aw-ink); }

.board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.7rem; align-items: start; }
.column { display: flex; flex-direction: column; gap: 0.45rem; min-width: 0; }
.column > header { display: flex; align-items: center; gap: 0.4rem; padding: 0 0.15rem 0.15rem; }
.column > header strong { font-size: var(--aw-text-sm); font-weight: 650; }
.column > header em { color: var(--aw-muted); font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-style: normal; }
.severe-chip {
  margin-left: auto;
  padding: 0.05rem 0.4rem;
  border-radius: var(--aw-radius-pill);
  background: var(--aw-danger-soft);
  color: var(--aw-danger);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
}

.card {
  display: grid;
  gap: 0.25rem;
  padding: 0.55rem 0.6rem;
  border: 1px solid var(--aw-border);
  border-left: 3px solid var(--aw-border-strong);
  border-radius: var(--aw-radius-control);
  background: var(--aw-panel);
  text-align: left;
  cursor: pointer;
  transition: border-color .12s, transform .12s;
}
.card:hover { border-color: var(--aw-teal); transform: translateY(-1px); }
.card:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.card[data-rating='critical'] { border-left-color: var(--aw-danger-ink); }
.card[data-rating='high'] { border-left-color: var(--aw-danger); }
.card[data-rating='medium'] { border-left-color: var(--aw-warn); }
.card[data-rating='low'] { border-left-color: var(--aw-low); }

.card-id { color: var(--aw-muted-strong); font-family: var(--aw-font-mono); font-size: var(--aw-text-2xs); letter-spacing: 0.02em; }
.card-title {
  display: -webkit-box;
  overflow: hidden;
  color: var(--aw-ink);
  font-size: var(--aw-text-sm);
  line-height: 1.35;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}
.card-tags { display: flex; flex-wrap: wrap; gap: 0.25rem; margin-top: 0.1rem; }
.rating {
  padding: 0.05rem 0.35rem;
  border-radius: var(--aw-radius-pill);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
  text-transform: capitalize;
  background: var(--aw-raised);
  color: var(--aw-ink-soft);
}
.rating[data-rating='critical'] { background: var(--aw-danger-soft); color: var(--aw-danger-ink); }
.rating[data-rating='high'] { background: var(--aw-danger-soft); color: var(--aw-danger); }
.rating[data-rating='medium'] { background: var(--aw-warn-soft); color: var(--aw-warn); }
.rating[data-rating='low'] { background: var(--aw-low-soft); color: var(--aw-low-ink); }
.chip { padding: 0.05rem 0.35rem; border-radius: var(--aw-radius-pill); background: var(--aw-raised); color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.chip.bad { background: var(--aw-danger-soft); color: var(--aw-danger); }
.chip.finding { background: var(--aw-warn-soft); color: var(--aw-warn-ink); font-family: var(--aw-font-mono); }

.card-next {
  margin-top: 0.15rem;
  padding-top: 0.3rem;
  border-top: 1px dashed var(--aw-border);
  color: var(--aw-teal);
  font-size: var(--aw-text-xs);
  line-height: 1.35;
}
.column[data-state='needs_you'] .card-next { color: var(--aw-warn); }
.column[data-state='concluded'] .card-next { color: var(--aw-ok); text-transform: capitalize; }

.column-empty { margin: 0; padding: 0.5rem 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }

@container workspace-panel (max-width: 68rem) {
  .board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@container workspace-panel (max-width: 38rem) {
  .board { grid-template-columns: minmax(0, 1fr); }
  .board-alert { flex-wrap: wrap; }
}
</style>
