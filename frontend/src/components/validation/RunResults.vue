<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Tag from 'primevue/tag'

import { api, ApiError } from '../../api'
import type {
  RuleResult,
  RunSummary,
  ValidationDetail,
  ValidationRule,
  ValidationRun,
  WorkspaceSummary,
} from '../../types'
import FrameTable from '../FrameTable.vue'

// Results of one validation run: verdict banner, stat chips, and one row per
// rule with drill-in to the failing rows. Failing rows are fetched lazily per
// rule (and cached until the next run) so the run response itself stays small.
const props = defineProps<{
  workspace: WorkspaceSummary
  run: ValidationRun
  rules: ValidationRule[]
  boundTable: string | null
  // Summary-only history of saved-spec runs (newest last, from the backend).
  history?: RunSummary[]
}>()
const emit = defineEmits<{ rebind: [string] }>()
const toast = useToast()

const expanded = ref<string | null>(null)
const details = ref<Record<string, ValidationDetail>>({})
const loadingDetail = ref<string | null>(null)
const exportingId = ref<string | null>(null)

const banner: Record<string, { text: string; severity: string; icon: string }> = {
  ok: { text: 'PASS', severity: 'success', icon: 'pi pi-check-circle' },
  warn: { text: 'PASS WITH WARNINGS', severity: 'warn', icon: 'pi pi-exclamation-triangle' },
  fail: { text: 'FAIL', severity: 'danger', icon: 'pi pi-times-circle' },
  info: { text: 'NO RULES RUN', severity: 'info', icon: 'pi pi-info-circle' },
}

const verdictSeverity: Record<string, string> = {
  ok: 'success', warn: 'warn', fail: 'danger', error: 'danger', skipped: 'secondary',
}

const stats = computed(() => {
  const chips = [
    { label: 'Rows checked', value: props.run.rows.toLocaleString() },
    { label: 'Rules passed', value: String(props.run.counts.passed) },
    { label: 'Warnings', value: String(props.run.counts.warned) },
    { label: 'Failed', value: String(props.run.counts.failed) },
  ]
  if (props.run.counts.errored) chips.push({ label: 'Errored', value: String(props.run.counts.errored) })
  if (props.run.counts.skipped) chips.push({ label: 'Skipped', value: String(props.run.counts.skipped) })
  return chips
})

const runAt = computed(() => new Date(props.run.run_at).toLocaleString())
const overridden = computed(() => !!props.boundTable && props.run.table !== props.boundTable)
// Newest first for reading; the trend meter keeps chronological order.
const pastRuns = computed(() => [...(props.history ?? [])].reverse())

function expandable(result: RuleResult): boolean {
  return result.fail_count > 0 && result.check !== 'row_count' && result.verdict !== 'skipped'
}

function counts(result: RuleResult): string {
  if (result.verdict === 'skipped') return 'disabled'
  if (result.verdict === 'error') return ''
  if (result.check === 'row_count') {
    return result.fail_count ? 'row count outside bounds' : 'row count within bounds'
  }
  const failing = `${result.fail_count.toLocaleString()} of ${result.checked_rows.toLocaleString()} rows`
  return result.pass_pct === null ? failing : `${failing} — ${result.pass_pct}% pass`
}

function draftRule(result: RuleResult): ValidationRule | null {
  return props.rules.find((r) => r.id === result.rule_id) ?? null
}

async function toggleDetail(result: RuleResult) {
  const id = result.rule_id ?? ''
  if (expanded.value === id) {
    expanded.value = null
    return
  }
  expanded.value = id
  if (details.value[id]) return
  const rule = draftRule(result)
  if (!rule) return
  loadingDetail.value = id
  try {
    details.value[id] = await api.post<ValidationDetail>(
      `/api/workspaces/${props.workspace.id}/tables/${props.run.table}/validate/detail`,
      { rule },
    )
  } catch (error) {
    expanded.value = null
    fail('Could not load failing rows', error)
  } finally {
    loadingDetail.value = null
  }
}

async function exportFailures(result: RuleResult) {
  const rule = draftRule(result)
  if (!rule) return
  exportingId.value = result.rule_id
  try {
    await api.download(
      `/api/workspaces/${props.workspace.id}/tables/${props.run.table}/validate/export`,
      { rule },
      `${props.run.table}_failures.xlsx`,
    )
  } catch (error) {
    fail('Export failed', error)
  } finally {
    exportingId.value = null
  }
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

// A new run invalidates the cached drill-ins.
watch(
  () => props.run,
  () => {
    details.value = {}
    expanded.value = null
  },
)
</script>

<template>
  <div class="results">
    <div class="banner" :data-verdict="run.verdict">
      <i :class="banner[run.verdict]?.icon" />
      <div class="banner-text">
        <strong>{{ banner[run.verdict]?.text ?? run.verdict }}</strong>
        <span class="muted">
          {{ run.table }} · {{ run.rows.toLocaleString() }} rows · {{ runAt }}
        </span>
      </div>
      <Button
        v-if="overridden"
        :label="`Rebind to ${run.table}`"
        icon="pi pi-link"
        size="small"
        severity="secondary"
        outlined
        @click="emit('rebind', run.table)"
        v-tooltip.bottom="`This run used ${run.table}; the rule set is bound to ${boundTable}`"
      />
    </div>

    <p v-if="run.counts.errored" class="error-hint">
      <i class="pi pi-exclamation-circle" />
      {{ run.counts.errored }} rule(s) could not run — usually a column that is
      missing or renamed in this table. See the rows marked "error" below.
    </p>

    <div class="stat-cards" style="margin: 0.75rem 0 1rem">
      <div v-for="stat in stats" :key="stat.label" class="stat-card">
        <div class="label">{{ stat.label }}</div>
        <div class="value">{{ stat.value }}</div>
      </div>
    </div>

    <div class="rule-list">
      <template v-for="result in run.results" :key="result.rule_id ?? result.label">
        <div
          class="rule-row"
          :class="{ clickable: expandable(result), open: expanded === result.rule_id }"
          @click="expandable(result) && toggleDetail(result)"
        >
          <i
            v-if="expandable(result)"
            :class="expanded === result.rule_id ? 'pi pi-chevron-down' : 'pi pi-chevron-right'"
            class="chev"
          />
          <span v-else class="chev" />
          <span class="rule-field">{{ result.column ?? '(table)' }}</span>
          <span class="rule-label">{{ result.label }}</span>
          <span class="rule-counts muted">
            {{ counts(result) }}
            <em v-if="result.error">{{ result.error }}</em>
          </span>
          <span v-if="result.pass_pct !== null && result.verdict !== 'skipped'" class="meter">
            <span
              class="meter-fill"
              :data-verdict="result.verdict"
              :style="{ width: `${result.pass_pct}%` }"
            />
          </span>
          <span v-else class="meter" />
          <Tag :value="result.verdict" :severity="verdictSeverity[result.verdict] ?? 'info'" />
          <Button
            v-if="expandable(result)"
            icon="pi pi-file-excel"
            text
            size="small"
            :loading="exportingId === result.rule_id"
            @click.stop="exportFailures(result)"
            v-tooltip.left="'Export all failing rows (Excel)'"
          />
          <span v-else class="export-spacer" />
        </div>

        <div v-if="expanded === result.rule_id" class="rule-detail">
          <p v-if="loadingDetail === result.rule_id" class="muted">
            <i class="pi pi-spinner pi-spin" /> Loading failing rows…
          </p>
          <template v-else-if="details[result.rule_id ?? '']">
            <p class="muted detail-note">
              Failing rows
              <span v-if="details[result.rule_id ?? ''].detail_rows > details[result.rule_id ?? ''].detail.rows.length">
                (previewing {{ details[result.rule_id ?? ''].detail.rows.length }} of
                {{ details[result.rule_id ?? ''].detail_rows.toLocaleString() }} — export for all)
              </span>
            </p>
            <FrameTable :frame="details[result.rule_id ?? ''].detail" scrollHeight="30vh" />
          </template>
        </div>
      </template>
    </div>

    <template v-if="pastRuns.length">
      <h4 class="history-title">Previous runs</h4>
      <div class="history">
        <div v-for="(entry, index) in pastRuns" :key="`${entry.run_at}-${index}`" class="history-row">
          <Tag :value="entry.verdict" :severity="verdictSeverity[entry.verdict] ?? 'info'" />
          <span class="history-when">{{ new Date(entry.run_at).toLocaleString() }}</span>
          <span class="muted">{{ entry.table }} · {{ entry.rows.toLocaleString() }} rows</span>
          <span class="history-counts muted">
            {{ entry.counts.passed }} ok
            <template v-if="entry.counts.warned"> · {{ entry.counts.warned }} warn</template>
            <template v-if="entry.counts.failed + entry.counts.errored">
              · {{ entry.counts.failed + entry.counts.errored }} fail</template
            >
          </span>
        </div>
      </div>
      <p class="muted history-note">
        Saved runs only — running unsaved edits doesn't record history.
      </p>
    </template>
  </div>
</template>

<style scoped>
.banner {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.8rem 1rem;
  background: var(--p-surface-0);
}
.banner > i { font-size: 1.6rem; }
.banner[data-verdict='ok'] > i { color: var(--p-green-500); }
.banner[data-verdict='warn'] > i { color: var(--p-amber-500); }
.banner[data-verdict='fail'] > i { color: var(--p-red-500); }
.banner[data-verdict='info'] > i { color: var(--p-blue-400); }
.banner-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}
.banner-text strong { font-size: 1.05rem; letter-spacing: 0.02em; }
.banner-text span { font-size: 0.8rem; }

.error-hint {
  margin: 0.6rem 0 0;
  font-size: 0.82rem;
  color: var(--p-red-600);
}
.error-hint i { margin-right: 0.25rem; }

.rule-list {
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  background: var(--p-surface-0);
  overflow: hidden;
}

.rule-row {
  display: grid;
  grid-template-columns: 1.1rem minmax(7rem, 12rem) minmax(9rem, 1.2fr) 1.6fr 6rem auto 2.2rem;
  gap: 0.6rem;
  align-items: center;
  padding: 0.45rem 0.8rem;
  border-bottom: 1px solid var(--p-surface-100);
}
.rule-row:last-child { border-bottom: none; }
.rule-row.clickable { cursor: pointer; }
.rule-row.clickable:hover { background: var(--p-surface-50); }
.rule-row.open { background: var(--p-surface-50); }

.chev { font-size: 0.65rem; color: var(--p-surface-500); width: 1rem; }
.rule-field {
  font-weight: 500;
  font-size: 0.86rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rule-label {
  font-size: 0.84rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rule-counts {
  font-size: 0.78rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rule-counts em { color: var(--p-red-600); font-style: normal; }

.meter {
  height: 6px;
  border-radius: 999px;
  background: var(--p-surface-200);
  overflow: hidden;
}
.meter-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--p-green-500);
}
.meter-fill[data-verdict='warn'] { background: var(--p-amber-500); }
.meter-fill[data-verdict='fail'] { background: var(--p-red-500); }

.export-spacer { width: 2rem; }

.rule-detail {
  padding: 0.6rem 0.9rem 0.9rem 2.4rem;
  border-bottom: 1px solid var(--p-surface-100);
  background: var(--p-surface-50);
}
.detail-note { margin: 0 0 0.4rem; font-size: 0.78rem; }

.history-title { margin: 1.1rem 0 0.4rem; font-size: 0.9rem; }
.history {
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  background: var(--p-surface-0);
  overflow: hidden;
}
.history-row {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 0.35rem 0.8rem;
  border-bottom: 1px solid var(--p-surface-100);
  font-size: 0.8rem;
}
.history-row:last-child { border-bottom: none; }
.history-when { font-weight: 500; }
.history-counts { margin-left: auto; }
.history-note { margin: 0.35rem 0 0; font-size: 0.74rem; }
</style>
