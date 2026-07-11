<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'

import { api, ApiError } from '../api'
import type { DashboardTile, WorkspaceSummary } from '../types'
import ChartView from './ChartView.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()

const toast = useToast()
const confirm = useConfirm()

const tiles = ref<DashboardTile[]>([])
const loading = ref(true)
const editing = ref<DashboardTile | null>(null)
const editTitle = ref('')
const editNote = ref('')
const verdictCounts = computed(() => ({
  ok: tiles.value.filter((tile) => tile.verdict === 'ok').length,
  warn: tiles.value.filter((tile) => tile.verdict === 'warn').length,
  fail: tiles.value.filter((tile) => tile.verdict === 'fail' || tile.error).length,
}))

const verdictSeverity: Record<string, string> = {
  ok: 'success',
  warn: 'warn',
  fail: 'danger',
  info: 'info',
}

const tileIcon: Record<string, string> = {
  analytics: 'pi pi-shield',
  query: 'pi pi-search',
  python: 'pi pi-code',
  pivot: 'pi pi-table',
  validation: 'pi pi-check-square',
}

const tileKindLabel: Record<string, string> = {
  analytics: 'Analytics test',
  query: 'Query',
  python: 'Python snippet',
  pivot: 'Pivot table',
  validation: 'Validation rules',
}

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

async function load() {
  loading.value = true
  try {
    const board = await api.get<{ tiles: DashboardTile[] }>(
      `/api/workspaces/${props.workspace.id}/dashboard`,
    )
    tiles.value = board.tiles
  } catch (error) {
    fail('Dashboard failed to load', error)
  } finally {
    loading.value = false
  }
}

async function patchTile(tile: DashboardTile, changes: Record<string, unknown>) {
  try {
    await api.patch(`/api/workspaces/${props.workspace.id}/tiles/${tile.id}`, changes)
    await load()
  } catch (error) {
    fail('Update failed', error)
  }
}

function openEdit(tile: DashboardTile) {
  editing.value = tile
  editTitle.value = tile.title
  editNote.value = tile.note
}

async function saveEdit() {
  if (!editing.value) return
  await patchTile(editing.value, { title: editTitle.value, note: editNote.value })
  editing.value = null
}

function remove(tile: DashboardTile) {
  confirm.require({
    header: 'Remove tile',
    message: `Remove "${tile.title}" from the dashboard?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        await api.del(`/api/workspaces/${props.workspace.id}/tiles/${tile.id}`)
        await load()
      } catch (error) {
        fail('Remove failed', error)
      }
    },
  })
}

onMounted(load)
defineExpose({ load })
</script>

<template>
  <div class="dashboard-heading">
    <div><p class="eyebrow">Engagement overview</p><h2>Audit dashboard</h2><p class="muted">Pinned procedures recompute against the current data whenever this view opens.</p></div>
    <Button icon="pi pi-refresh" label="Refresh" severity="secondary" size="small" :loading="loading" @click="load" />
  </div>

  <div v-if="tiles.length" class="dashboard-summary surface-panel">
    <span><strong>{{ tiles.length }}</strong><small>Pinned procedures</small></span>
    <span class="summary-ok"><strong>{{ verdictCounts.ok }}</strong><small>Passed</small></span>
    <span class="summary-warn"><strong>{{ verdictCounts.warn }}</strong><small>Review</small></span>
    <span class="summary-fail"><strong>{{ verdictCounts.fail }}</strong><small>Exceptions</small></span>
    <span class="freshness"><i class="pi pi-sync" /><small>Live against current tables</small></span>
  </div>

  <div v-if="!loading && tiles.length === 0" class="empty-state dashboard-empty">
    <div><span class="empty-state-icon"><i class="pi pi-thumbtack" /></span><h3>Build an engagement overview</h3><p>Run a query, validation rule set or analysis, then pin the result here. Pinned work automatically recomputes against refreshed data.</p></div>
  </div>

  <div class="tile-grid">
    <div v-for="(tile, index) in tiles" :key="tile.id" class="tile" :class="{ 'tile-error': tile.error }" :data-verdict="tile.error ? 'fail' : tile.verdict">
      <div class="tile-head">
        <div class="tile-title">
          <i :class="tileIcon[tile.kind]" v-tooltip.bottom="tileKindLabel[tile.kind]" />
          <strong>{{ tile.title }}</strong>
          <Tag v-if="tile.verdict" :value="tile.verdict_text || tile.verdict" :severity="verdictSeverity[tile.verdict]" class="verdict-tag" />
        </div>
        <div class="tile-actions">
          <Button icon="pi pi-chevron-left" text size="small" :disabled="index === 0" v-tooltip.bottom="'Move left'" @click="patchTile(tile, { move: -1 })" />
          <Button icon="pi pi-chevron-right" text size="small" :disabled="index === tiles.length - 1" v-tooltip.bottom="'Move right'" @click="patchTile(tile, { move: 1 })" />
          <Button icon="pi pi-pencil" text size="small" v-tooltip.bottom="'Rename / note'" @click="openEdit(tile)" />
          <Button icon="pi pi-times" text size="small" severity="danger" v-tooltip.bottom="'Remove tile'" @click="remove(tile)" />
        </div>
      </div>

      <p class="tile-meta muted">
        {{ tile.table || (tile.kind === 'python' ? 'Python' : '') }}
        <template v-if="tile.total_rows !== undefined"> · {{ tile.total_rows.toLocaleString() }} rows</template>
        · pinned {{ tile.created }}
      </p>
      <p v-if="tile.note" class="tile-note">{{ tile.note }}</p>

      <div v-if="tile.error" class="tile-error-body">
        <i class="pi pi-exclamation-triangle" />
        <span>{{ tile.error }}</span>
      </div>
      <template v-else>
        <div v-if="tile.stats?.length" class="tile-stats">
          <span v-for="stat in tile.stats" :key="stat.label" class="tile-stat">
            <span class="label">{{ stat.label }}</span>
            <span class="value">{{ stat.value }}</span>
          </span>
        </div>
        <ChartView v-if="tile.frame" :frame="tile.frame" :viz="tile.viz" height="240px" />
      </template>
    </div>
  </div>

  <Dialog :visible="editing !== null" modal header="Edit tile" :style="{ width: '26rem' }" @update:visible="editing = null">
    <div class="field">
      <label>Title</label>
      <InputText v-model="editTitle" />
    </div>
    <div class="field" style="margin-top: 0.75rem">
      <label>Note</label>
      <Textarea v-model="editNote" rows="2" />
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="editing = null" />
      <Button label="Save" icon="pi pi-check" :disabled="!editTitle.trim()" @click="saveEdit" />
    </template>
  </Dialog>
</template>

<style scoped>
.dashboard-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }
.dashboard-heading h2 { margin: 0; font-size: 1.25rem; }
.dashboard-heading p:last-child { margin: .25rem 0 0; }
.dashboard-summary { display: flex; align-items: stretch; margin-bottom: 1rem; overflow: hidden; box-shadow: none; }
.dashboard-summary > span { display: flex; flex-direction: column; justify-content: center; min-width: 9rem; padding: .7rem 1rem; border-right: 1px solid var(--aw-border); }
.dashboard-summary strong { font-size: 1.15rem; }
.dashboard-summary small { color: var(--aw-muted); font-size: .7rem; }
.dashboard-summary .summary-ok strong { color: #15805c; }
.dashboard-summary .summary-warn strong { color: #b45309; }
.dashboard-summary .summary-fail strong { color: #b42318; }
.dashboard-summary .freshness { margin-left: auto; flex-direction: row; align-items: center; gap: .45rem; border: 0; color: var(--aw-teal); }
.dashboard-empty { min-height: 20rem; }

.empty {
  padding: 2rem 0;
}

.tile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
  gap: 1rem;
}

.tile {
  background: var(--p-surface-0);
  border: 1px solid var(--p-surface-200);
  border-radius: 8px;
  padding: 0.9rem 1rem 1rem;
  min-width: 0;
  box-shadow: var(--aw-shadow);
  border-top: 3px solid #94a3b8;
}
.tile[data-verdict='ok'] { border-top-color: #16855b; }
.tile[data-verdict='warn'] { border-top-color: #d97706; }
.tile[data-verdict='fail'] { border-top-color: #dc2626; }
.tile[data-verdict='info'] { border-top-color: #3b82f6; }

.tile-error {
  border-color: var(--p-red-200);
}

.tile-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.5rem;
}

.tile-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  min-width: 0;
}

.tile-title i {
  color: var(--p-primary-500);
}

.verdict-tag {
  font-size: 0.7rem;
}

.tile-actions {
  display: flex;
  flex-shrink: 0;
}
.tile:not(:hover) .tile-actions { opacity: .35; }
.tile-actions { transition: opacity .15s; }

.tile-meta {
  margin: 0.15rem 0 0.4rem;
  font-size: 0.78rem;
}

.tile-note {
  margin: 0 0 0.5rem;
  font-size: 0.85rem;
  color: var(--p-surface-600);
  font-style: italic;
}

.tile-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.1rem;
  margin-bottom: 0.6rem;
}

.tile-stat {
  display: flex;
  flex-direction: column;
}

.tile-stat .label {
  font-size: 0.68rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-surface-500);
}

.tile-stat .value {
  font-size: 0.95rem;
  font-weight: 600;
}

.tile-error-body {
  display: flex;
  gap: 0.5rem;
  align-items: flex-start;
  color: var(--p-red-600);
  font-size: 0.9rem;
  padding: 0.75rem 0;
}

@media (max-width: 760px) {
  .dashboard-summary { overflow-x: auto; }
  .dashboard-heading { flex-direction: column; }
  .tile-grid { grid-template-columns: 1fr; }
}
</style>
