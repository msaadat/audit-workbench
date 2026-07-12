<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Tag from 'primevue/tag'
import Skeleton from 'primevue/skeleton'

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
const totalTables = computed(() => workspaces.value.reduce((sum, ws) => sum + ws.table_count, 0))

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
    <div class="home-hero">
      <div>
        <p class="eyebrow">Engagement index</p>
        <h1>Your audit workspaces</h1>
        <p class="muted">
          Load, validate and analyse engagement data without moving raw records
          off this device.
        </p>
      </div>
      <Button label="New workspace" icon="pi pi-plus" @click="showCreate = true" />
    </div>

    <div v-if="!loading && workspaces.length" class="portfolio-strip">
      <span><strong>{{ workspaces.length }}</strong><small>Engagements</small></span>
      <span><strong>{{ totalTables }}</strong><small>Data tables</small></span>
      <span class="privacy-fact"><i class="pi pi-lock" /><span><strong>Private by design</strong><small>Raw data stays local</small></span></span>
    </div>

    <div v-if="loading" class="loading-grid">
      <Skeleton v-for="n in 3" :key="n" height="12rem" borderRadius="8px" />
    </div>
    <div v-else-if="workspaces.length === 0" class="empty-state">
      <div>
        <span class="empty-state-icon"><i class="pi pi-folder-open" /></span>
        <h3>Start your first engagement</h3>
        <p>Create a workspace, then add CSV, TSV or Excel files. Profiling and audit tests run entirely on this machine.</p>
        <Button label="Create workspace" icon="pi pi-plus" @click="showCreate = true" />
      </div>
    </div>

    <div class="grid">
      <Card v-for="ws in workspaces" :key="ws.id" class="ws-card" @click="router.push(`/workspace/${ws.id}`)">
        <template #title>
          <div class="card-title">
            <span class="workspace-icon"><i class="pi pi-briefcase" /></span>
            <span class="workspace-name">{{ ws.name }}</span>
            <i class="pi pi-arrow-up-right card-arrow" />
          </div>
        </template>
        <template #content>
          <p class="desc">{{ ws.description || 'No description.' }}</p>
          <div class="workspace-meta">
            <span><i class="pi pi-database" /> {{ ws.table_count }} table{{ ws.table_count === 1 ? '' : 's' }}</span>
            <span><i class="pi pi-calendar" /> Created {{ ws.created || '—' }}</span>
          </div>
        </template>
        <template #footer>
          <div class="card-actions">
            <Tag :value="ws.table_count ? 'Ready' : 'Setup needed'" :severity="ws.table_count ? 'success' : 'warn'" />
            <Button icon="pi pi-trash" severity="danger" text size="small" v-tooltip.bottom="'Delete workspace'" @click.stop="remove(ws)" />
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
.home-hero {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin: 0.5rem 0 1.5rem;
  padding: 1.6rem 1.75rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-lg);
  background: linear-gradient(118deg, #fff 0%, var(--aw-teal-soft) 130%);
  box-shadow: var(--aw-shadow-sm);
}

h1 {
  margin: 0 0 0.4rem;
  font-size: var(--aw-text-2xl);
}

.home-hero p {
  margin: 0;
  max-width: 48rem;
}

.portfolio-strip { display: flex; align-items: stretch; margin-bottom: 1.2rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-md); background: #fff; box-shadow: var(--aw-shadow-sm); overflow: hidden; }
.portfolio-strip > span { display: flex; flex-direction: column; min-width: 10rem; padding: 0.8rem 1.2rem; border-right: 1px solid var(--aw-border); }
.portfolio-strip strong { font-size: 1.2rem; font-weight: 600; color: var(--aw-ink); }
.portfolio-strip small { margin-top: 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.portfolio-strip .privacy-fact { margin-left: auto; flex-direction: row; align-items: center; gap: 0.6rem; border-right: 0; }
.privacy-fact > i { color: var(--aw-teal); }
.privacy-fact > span { display: flex; flex-direction: column; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 0.9rem;
}

.loading-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
:deep(.ws-card) { border: 1px solid var(--aw-border); border-radius: var(--aw-radius-md); box-shadow: var(--aw-shadow-sm); cursor: pointer; transition: transform .15s, border-color .15s, box-shadow .15s; }
:deep(.ws-card:hover) { transform: translateY(-2px); border-color: var(--aw-teal); box-shadow: var(--aw-shadow-md); }
:deep(.ws-card .p-card-body) { padding: 1.05rem 1.1rem 0.85rem; }
:deep(.ws-card .p-card-content) { padding-block: 0.7rem; }
:deep(.ws-card .p-card-footer) { padding-top: 0.55rem; border-top: 1px solid #edf1f5; }

.card-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}
.workspace-icon { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: 6px; background: var(--aw-teal-soft); color: var(--aw-teal); font-size: 0.9rem; }
.workspace-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 1rem; }
.card-arrow { color: #94a3b8; font-size: 0.85rem; }

.desc {
  margin: 0;
  min-height: 2.5rem;
  color: var(--p-surface-600);
}

.workspace-meta { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; color: var(--aw-muted); font-size: 0.74rem; }
.workspace-meta span { display: flex; align-items: center; gap: 0.3rem; }

.card-actions {
  display: flex;
  justify-content: space-between;
}

.empty {
  padding: 2rem 0;
}

@media (max-width: 720px) {
  .home-hero { flex-direction: column; }
  .portfolio-strip { overflow-x: auto; }
  .loading-grid { grid-template-columns: 1fr; }
}
</style>
