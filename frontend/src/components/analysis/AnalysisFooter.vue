<script setup lang="ts">
import { computed } from 'vue'

import type { SavedAnalysis } from '../../types'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import { holdsExceptions } from './analysisStatus'

/**
 * The row that closes the record: has anyone answered for what this found?
 *
 * A saved procedure is computed outside the audit graph. Its result supports no
 * finding until it has been carried into a data test against an RCM row, which
 * is what `analysis_promotion` records — and what, until now, nothing on this
 * page could say. The engagement that capability was written for lost an
 * invoice of 80,000,000 billed against a purchase order of 8,000,000 exactly
 * that way: an analysis found it, the memorandum called it the most
 * significant analytic result of the engagement, and no test was ever written.
 *
 * It states the answer rather than offering a button for it. Promotion is a
 * fitting turn the assistant makes during fieldwork, over every unanswered
 * procedure at once; a per-row control here would imply a decision this page
 * cannot commit.
 */

const props = defineProps<{ analysis: SavedAnalysis }>()
const nav = useWorkspaceNav()

const disposition = computed(() => props.analysis.promotion ?? null)
const owed = computed(() => holdsExceptions(props.analysis) && !disposition.value)
</script>

<template>
  <div class="footer-row">
    <p class="aw-label">Answered for</p>

    <template v-if="disposition?.state === 'promoted'">
      <button
        v-if="disposition.test_id"
        type="button"
        class="chip"
        @click="nav.push('data-tests', { test: disposition.test_id })"
      >
        <i class="pi pi-shield" aria-hidden="true" />
        <span class="id">{{ disposition.test_id }}</span>
      </button>
      <button
        v-if="disposition.rcm_id"
        type="button"
        class="chip"
        @click="nav.push('rcm-row', { rcm: disposition.rcm_id })"
      >
        <i class="pi pi-map" aria-hidden="true" />
        <span class="id">{{ disposition.rcm_id }}</span>
      </button>
      <span class="note">Carried into a data test, so what it found can carry a finding.</span>
    </template>

    <template v-else-if="disposition?.state === 'declined'">
      <span class="note">
        Judged not a control test<template v-if="disposition.reason">: {{ disposition.reason }}</template>
      </span>
    </template>

    <template v-else-if="owed">
      <span class="note owed">
        Nothing yet. Until this is carried into a test against an RCM row, what
        it found supports no finding.
      </span>
    </template>

    <template v-else>
      <span class="note">
        Nothing to answer for: this procedure flagged no rows.
      </span>
    </template>
  </div>
</template>

<style scoped>
.footer-row {
  display: flex; align-items: center; gap: .625rem; flex-wrap: wrap;
  margin-top: auto; padding-top: .875rem; border-top: 1px solid var(--aw-border);
}
.chip {
  display: inline-flex; align-items: center; gap: .375rem;
  padding: .2rem .5rem;
  border: 1px solid var(--aw-teal-line); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-teal);
  font: inherit; cursor: pointer;
}
.chip:hover { background: var(--aw-teal-soft); }
.chip .id { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); font-weight: 600; }
.note { min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.45; }
.note.owed { color: var(--aw-warn-ink); }
</style>
