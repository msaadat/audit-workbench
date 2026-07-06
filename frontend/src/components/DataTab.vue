<script setup lang="ts">
import { ref } from 'vue'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Dialog from 'primevue/dialog'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import type { FramePayload, TableInfo, WorkspaceSummary } from '../types'
import FrameTable from './FrameTable.vue'
import JoinDialog from './JoinDialog.vue'

const props = defineProps<{ workspace: WorkspaceSummary }>()
const emit = defineEmits<{ changed: [] }>()

const toast = useToast()
const confirm = useConfirm()

const fileInput = ref<HTMLInputElement>()
const uploading = ref(false)
const showJoin = ref(false)
const preview = ref<{ table: string; frame: FramePayload; total: number } | null>(null)
const previewLoading = ref(false)

function fail(summary: string, error: unknown) {
  const detail = error instanceof ApiError ? error.message : String(error)
  toast.add({ severity: 'error', summary, detail, life: 6000 })
}

async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length) return
  uploading.value = true
  try {
    await api.upload(`/api/workspaces/${props.workspace.id}/tables`, files)
    emit('changed')
  } catch (error) {
    fail('Upload failed', error)
  } finally {
    uploading.value = false
  }
}

async function openPreview(table: TableInfo) {
  previewLoading.value = true
  try {
    const frame = await api.get<FramePayload & { total_rows: number }>(
      `/api/workspaces/${props.workspace.id}/tables/${table.name}/preview?rows=100`,
    )
    preview.value = { table: table.name, frame, total: frame.total_rows }
  } catch (error) {
    fail('Preview failed', error)
  } finally {
    previewLoading.value = false
  }
}

function removeTable(table: TableInfo) {
  confirm.require({
    header: `Remove ${table.kind === 'join' ? 'join' : 'table'}`,
    message:
      table.kind === 'join'
        ? `Remove join "${table.name}"?`
        : `Remove "${table.name}" and delete its file from the workspace?`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Remove', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      try {
        const endpoint = table.kind === 'join' ? 'joins' : 'tables'
        await api.del(`/api/workspaces/${props.workspace.id}/${endpoint}/${table.name}`)
        emit('changed')
      } catch (error) {
        fail('Remove failed', error)
      }
    },
  })
}
</script>

<template>
  <div class="toolbar">
    <input ref="fileInput" type="file" multiple accept=".csv,.tsv,.xlsx,.xlsm,.xls" hidden @change="upload" />
    <Button label="Add files" icon="pi pi-upload" :loading="uploading" @click="fileInput?.click()" />
    <Button
      label="Add join"
      icon="pi pi-link"
      severity="secondary"
      :disabled="workspace.tables.length < 2"
      v-tooltip.bottom="workspace.tables.length < 2 ? 'Load at least two tables first' : ''"
      @click="showJoin = true"
    />
    <span class="muted">CSV, TSV and Excel files. Types are inferred automatically.</span>
  </div>

  <p v-if="workspace.tables.length === 0" class="muted">
    No tables yet — add the engagement's data files to get started.
  </p>

  <DataTable v-else :value="workspace.tables" size="small" stripedRows>
    <Column field="name" header="Table">
      <template #body="{ data }">
        <strong>{{ data.name }}</strong>
      </template>
    </Column>
    <Column field="kind" header="Kind">
      <template #body="{ data }">
        <Tag :value="data.kind" :severity="data.kind === 'join' ? 'info' : 'secondary'" />
      </template>
    </Column>
    <Column field="source" header="Source" />
    <Column field="rows" header="Rows">
      <template #body="{ data }">
        <span v-if="data.error" class="error-text" v-tooltip.bottom="data.error">error</span>
        <span v-else>{{ data.rows?.toLocaleString() }}</span>
      </template>
    </Column>
    <Column field="columns" header="Columns" />
    <Column header="" style="width: 7rem">
      <template #body="{ data }">
        <Button icon="pi pi-eye" text size="small" :loading="previewLoading" v-tooltip.bottom="'Preview first 100 rows'" @click="openPreview(data)" />
        <Button icon="pi pi-trash" text size="small" severity="danger" v-tooltip.bottom="'Remove'" @click="removeTable(data)" />
      </template>
    </Column>
  </DataTable>

  <Dialog
    :visible="preview !== null"
    modal
    :header="`${preview?.table} — first ${preview?.frame.rows.length} of ${preview?.total.toLocaleString()} rows`"
    :style="{ width: '90vw' }"
    @update:visible="preview = null"
  >
    <FrameTable v-if="preview" :frame="preview.frame" scrollHeight="60vh" />
  </Dialog>

  <JoinDialog
    v-model:visible="showJoin"
    :workspace="workspace"
    @saved="emit('changed')"
  />
</template>

<style scoped>
.error-text {
  color: var(--p-red-500);
  font-weight: 600;
}
</style>
