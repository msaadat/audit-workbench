<script setup lang="ts">
import { onMounted, ref } from 'vue'
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

const verdictSeverity: Record<string, string> = {
  ok: 'success',
  warn: 'warn',
  fail: 'danger',
  info: 'info',
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
  <div class="toolbar">
    <span class="muted">
      Tiles re-run their saved query or test on the current data every time the
      dashboard loads — pin results from the Explore and Analytics tabs.
    </span>
    <Button icon="pi pi-refresh" label="Refresh" severity="secondary" size="small" :loading="loading" @click="load" />
  </div>

  <p v-if="!loading && tiles.length === 0" class="muted empty">
    Nothing pinned yet. Run a query in <b>Explore</b> or a test in
    <b>Analytics</b>, then hit <i class="pi pi-thumbtack" /> Pin.
  </p>

  <div class="tile-grid">
    <div v-for="(tile, index) in tiles" :key="tile.id" class="tile" :class="{ 'tile-error': tile.error }">
      <div class="tile-head">
        <div class="tile-title">
          <i :class="tile.kind === 'analytics' ? 'pi pi-shield' : 'pi pi-search'" v-tooltip.bottom="tile.kind === 'analytics' ? 'Analytics test' : 'Explore query'" />
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
        {{ tile.table }}
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
.toolbar {
  justify-content: space-between;
}

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
  border-radius: 10px;
  padding: 0.9rem 1rem 1rem;
  min-width: 0;
}

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
</style>
