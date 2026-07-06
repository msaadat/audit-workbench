<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import ProgressBar from 'primevue/progressbar'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Message from 'primevue/message'

import { api } from '../api'
import type { ColumnProfile, TableProfile, WorkspaceSummary } from '../types'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const toast = useToast()

const table = ref<string | null>(null)
const profile = ref<TableProfile | null>(null)
const loading = ref(false)
const expandedRows = ref({})

const tableOptions = computed(() => props.workspace.tables.map((t) => t.name))

const typeSeverity: Record<string, string> = {
  numeric: 'success',
  date: 'info',
  categorical: 'warn',
  id: 'contrast',
  boolean: 'secondary',
  text: 'secondary',
  empty: 'danger',
}

async function load() {
  if (!table.value) return
  loading.value = true
  profile.value = null
  try {
    profile.value = await api.get<TableProfile>(
      `/api/workspaces/${props.workspace.id}/tables/${table.value}/profile`,
    )
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Profiling failed', detail: String(error), life: 6000 })
  } finally {
    loading.value = false
  }
}

watch(table, load)
watch(
  () => props.workspace.tables.length,
  () => {
    if (!table.value && tableOptions.value.length) table.value = tableOptions.value[0]
  },
  { immediate: true },
)

function rangeText(p: ColumnProfile): string {
  if (p.min === null && p.max === null) return ''
  const range = `${p.min ?? '?'} – ${p.max ?? '?'}`
  return p.mean !== null ? `${range} (mean ${p.mean})` : range
}
</script>

<template>
  <div class="toolbar">
    <div class="field">
      <label>Table</label>
      <Select v-model="table" :options="tableOptions" placeholder="Pick a table" style="min-width: 16rem" />
    </div>
  </div>

  <p v-if="loading" class="muted">Profiling…</p>

  <template v-if="profile && !loading">
    <div class="stat-cards" style="margin-bottom: 1rem">
      <div class="stat-card">
        <div class="label">Rows</div>
        <div class="value">{{ profile.rows.toLocaleString() }}</div>
      </div>
      <div class="stat-card">
        <div class="label">Columns</div>
        <div class="value">{{ profile.columns }}</div>
      </div>
      <div class="stat-card">
        <div class="label">Duplicate rows</div>
        <div class="value" :class="{ warn: profile.duplicate_rows > 0 }">
          {{ profile.duplicate_rows.toLocaleString() }}
        </div>
      </div>
      <div class="stat-card">
        <div class="label">In-memory size</div>
        <div class="value">{{ profile.estimated_size_mb.toLocaleString() }} MB</div>
      </div>
    </div>

    <Message v-if="profile.sampled" severity="info" :closable="false" style="margin-bottom: 1rem">
      Column statistics are computed on the first {{ profile.sample_rows.toLocaleString() }} rows.
    </Message>

    <DataTable
      :value="profile.column_profiles"
      v-model:expandedRows="expandedRows"
      dataKey="name"
      size="small"
      stripedRows
    >
      <Column expander style="width: 3rem" />
      <Column field="name" header="Column">
        <template #body="{ data }">
          <strong>{{ data.name }}</strong>
          <span class="muted dtype"> {{ data.dtype }}</span>
        </template>
      </Column>
      <Column field="inferred_type" header="Type">
        <template #body="{ data }">
          <Tag :value="data.inferred_type" :severity="typeSeverity[data.inferred_type] ?? 'secondary'" />
        </template>
      </Column>
      <Column field="blank_pct" header="Blank" style="width: 14rem">
        <template #body="{ data }">
          <div class="blank-cell">
            <ProgressBar :value="data.blank_pct" :showValue="false" style="height: 6px; flex: 1" />
            <span>{{ data.blank_pct }}%</span>
          </div>
        </template>
      </Column>
      <Column field="distinct_count" header="Distinct">
        <template #body="{ data }">
          {{ data.distinct_count.toLocaleString() }}
          <span class="muted">({{ data.distinct_pct }}%)</span>
        </template>
      </Column>
      <Column header="Range / Mean">
        <template #body="{ data }">{{ rangeText(data) }}</template>
      </Column>

      <template #expansion="{ data }">
        <div class="top-values">
          <p v-if="data.top_values.length === 0" class="muted">No values.</p>
          <div v-for="tv in data.top_values" :key="tv.value ?? ''" class="tv-row">
            <span class="tv-value">{{ tv.value ?? '∅' }}</span>
            <ProgressBar :value="tv.pct" :showValue="false" style="height: 6px; flex: 1" />
            <span class="tv-count">{{ tv.count.toLocaleString() }} ({{ tv.pct }}%)</span>
          </div>
        </div>
      </template>
    </DataTable>
  </template>
</template>

<style scoped>
.dtype {
  font-size: 0.75rem;
}

.blank-cell {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.warn {
  color: var(--p-orange-500);
}

.top-values {
  padding: 0.5rem 1rem 0.5rem 3rem;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  max-width: 44rem;
}

.tv-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.tv-value {
  width: 14rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: Consolas, monospace;
  font-size: 0.85rem;
}

.tv-count {
  width: 9rem;
  text-align: right;
  font-size: 0.85rem;
  color: var(--p-surface-500);
}
</style>
