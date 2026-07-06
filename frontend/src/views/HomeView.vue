<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'

import { api, ApiError } from '../api'
import type { WorkspaceListItem, WorkspaceSummary } from '../types'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const workspaces = ref<WorkspaceListItem[]>([])
const loading = ref(true)
const showCreate = ref(false)
const creating = ref(false)
const name = ref('')
const description = ref('')

async function load() {
  loading.value = true
  try {
    workspaces.value = await api.get<WorkspaceListItem[]>('/api/workspaces')
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Failed to load workspaces', detail: String(error), life: 5000 })
  } finally {
    loading.value = false
  }
}

async function create() {
  creating.value = true
  try {
    const ws = await api.post<WorkspaceSummary>('/api/workspaces', {
      name: name.value,
      description: description.value,
    })
    showCreate.value = false
    router.push(`/workspace/${ws.id}`)
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : String(error)
    toast.add({ severity: 'error', summary: 'Could not create workspace', detail, life: 5000 })
  } finally {
    creating.value = false
  }
}

function remove(ws: WorkspaceListItem) {
  confirm.require({
    header: 'Delete workspace',
    message: `Delete "${ws.name}" and all its data files? This cannot be undone.`,
    icon: 'pi pi-exclamation-triangle',
    acceptProps: { label: 'Delete', severity: 'danger' },
    rejectProps: { label: 'Cancel', severity: 'secondary', outlined: true },
    accept: async () => {
      await api.del(`/api/workspaces/${ws.id}`)
      await load()
    },
  })
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1>Workspaces</h1>
        <p class="muted">
          A workspace holds one engagement's data files. Open one to profile,
          explore and test the data — everything runs on this machine.
        </p>
      </div>
      <Button label="New workspace" icon="pi pi-plus" @click="showCreate = true" />
    </div>

    <p v-if="loading" class="muted">Loading…</p>
    <p v-else-if="workspaces.length === 0" class="muted empty">
      No workspaces yet — create one and drop in a CSV or Excel file.
    </p>

    <div class="grid">
      <Card v-for="ws in workspaces" :key="ws.id" class="ws-card">
        <template #title>
          <div class="card-title">
            <span>{{ ws.name }}</span>
            <Tag :value="`${ws.table_count} table(s)`" severity="secondary" />
          </div>
        </template>
        <template #subtitle>Created {{ ws.created || '—' }}</template>
        <template #content>
          <p class="desc">{{ ws.description || 'No description.' }}</p>
        </template>
        <template #footer>
          <div class="card-actions">
            <Button label="Open" icon="pi pi-arrow-right" size="small" @click="router.push(`/workspace/${ws.id}`)" />
            <Button icon="pi pi-trash" severity="danger" text size="small" v-tooltip.bottom="'Delete workspace'" @click="remove(ws)" />
          </div>
        </template>
      </Card>
    </div>

    <Dialog v-model:visible="showCreate" header="New workspace" modal :style="{ width: '28rem' }">
      <div class="field">
        <label for="ws-name">Name</label>
        <InputText id="ws-name" v-model="name" placeholder="e.g. FY26 Revenue Audit" autofocus @keyup.enter="create" />
      </div>
      <div class="field" style="margin-top: 0.75rem">
        <label for="ws-desc">Description (optional)</label>
        <Textarea id="ws-desc" v-model="description" rows="3" />
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" text @click="showCreate = false" />
        <Button label="Create" icon="pi pi-check" :loading="creating" :disabled="!name.trim()" @click="create" />
      </template>
    </Dialog>
  </div>
</template>

<style scoped>
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin-bottom: 1.5rem;
}

h1 {
  margin: 0 0 0.35rem;
  font-size: 1.5rem;
}

.header-row p {
  margin: 0;
  max-width: 48rem;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 1rem;
}

.card-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.desc {
  margin: 0;
  min-height: 2.5rem;
  color: var(--p-surface-600);
}

.card-actions {
  display: flex;
  justify-content: space-between;
}

.empty {
  padding: 2rem 0;
}
</style>
