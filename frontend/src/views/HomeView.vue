<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import Skeleton from 'primevue/skeleton'
import type { MenuItem } from 'primevue/menuitem'

import { api } from '../api'
import type { WorkspaceListItem } from '../types'
import NewEngagementDialog from '../components/NewEngagementDialog.vue'
import UiOverflowMenu from '../components/ui/UiOverflowMenu.vue'

const router = useRouter()
const confirm = useConfirm()
const toast = useToast()

const workspaces = ref<WorkspaceListItem[]>([])
const loading = ref(true)
const showCreate = ref(false)

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

// A new engagement lands on the import dialog rather than an empty console:
// the agent needs the audit folder before any of its plan can start.
async function onCreated({ id, withImport }: { id: string; withImport: boolean }) {
  await router.push({ path: `/workspace/${id}`, query: withImport ? { import: '1' } : {} })
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

function workspaceActions(ws: WorkspaceListItem): MenuItem[] {
  return [{
    label: `Delete ${ws.name}`,
    icon: 'pi pi-trash',
    command: () => remove(ws),
  }]
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="home-hero">
      <div>
        <p class="eyebrow">Engagement index</p>
        <h1>Your audit workspaces</h1>
        <p class="home-lede">An engagement holds the audit file — the planning memorandum, the risk and control matrix, the tests and their evidence, and the report they support. Bring in the folder and the assistant works through it with you.</p>
      </div>
      <!-- One primary action per screen. While the list is empty the empty
           state below owns it; a hero button here made two. -->
      <Button v-if="workspaces.length" label="New engagement" icon="pi pi-plus" @click="showCreate = true" />
    </div>

    <div v-if="loading" class="loading-grid">
      <Skeleton v-for="n in 3" :key="n" height="12rem" borderRadius="8px" />
    </div>
    <div v-else-if="workspaces.length === 0" class="empty-state">
      <div>
        <span class="empty-state-icon"><i class="pi pi-folder-open" /></span>
        <h3>Start your first engagement</h3>
        <p>Name it, point it at the audit folder, and the assistant proposes the plan before it changes anything.</p>
        <Button label="New engagement" icon="pi pi-plus" @click="showCreate = true" />
        <p class="empty-aside"><a href="/about.html">Why this exists <i class="pi pi-arrow-up-right" /></a></p>
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
          <p v-if="ws.description" class="desc">{{ ws.description }}</p>
          <div class="workspace-meta">
            <span><i class="pi pi-database" /> {{ ws.table_count }} table{{ ws.table_count === 1 ? '' : 's' }}</span>
            <span><i class="pi pi-calendar" /> Created {{ ws.created || '—' }}</span>
          </div>
        </template>
        <template #footer>
          <div class="card-actions">
            <Tag :value="ws.table_count ? 'Data ready' : 'Setup needed'" :severity="ws.table_count ? 'success' : 'warn'" />
            <span @click.stop><UiOverflowMenu :items="workspaceActions(ws)" :tooltip="`Actions for ${ws.name}`" /></span>
          </div>
        </template>
      </Card>
    </div>

    <NewEngagementDialog v-model:visible="showCreate" @created="onCreated" />
  </div>
</template>

<style scoped>
.home-hero {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin: 0.25rem 0 1.1rem;
  padding: 1.2rem 1.35rem;
  border: 1px solid var(--aw-border);
  border-radius: var(--aw-radius-surface);
  background: linear-gradient(118deg, var(--aw-panel) 0%, var(--aw-teal-soft) 130%);
}

h1 {
  margin: 0 0 0.4rem;
  font-size: var(--aw-text-2xl);
}

.home-lede { max-width: 58ch; margin: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-base); line-height: 1.5; }
.empty-aside { margin: 0.9rem 0 0; font-size: var(--aw-text-sm); }
.empty-aside a { display: inline-flex; align-items: center; gap: 0.3rem; color: var(--aw-teal); text-decoration: none; }
.empty-aside a:hover { text-decoration: underline; }
.empty-aside i { font-size: var(--aw-text-2xs); }

.portfolio-strip { display: flex; align-items: stretch; margin-bottom: 1.2rem; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); background: var(--aw-panel); overflow: hidden; }
.portfolio-strip > span { display: flex; flex-direction: column; min-width: 10rem; padding: 0.8rem 1.2rem; border-right: 1px solid var(--aw-border); }
.portfolio-strip strong { font-size: var(--aw-text-lg); font-weight: 600; color: var(--aw-ink); }
.portfolio-strip small { margin-top: 0.15rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 600; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0.9rem;
}

.loading-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
:deep(.ws-card) { border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface); cursor: pointer; transition: transform .15s, border-color .15s, box-shadow .15s; }
:deep(.ws-card:hover) { transform: translateY(-2px); border-color: var(--aw-teal); box-shadow: var(--aw-shadow-md); }
:deep(.ws-card .p-card-body) { padding: 0.85rem 0.95rem 0.7rem; }
:deep(.ws-card .p-card-content) { padding-block: 0.55rem; }
:deep(.ws-card .p-card-footer) { padding-top: 0.55rem; border-top: 1px solid var(--aw-border); }

.card-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}
.workspace-icon { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: var(--aw-radius-control); background: var(--aw-teal-soft); color: var(--aw-teal); font-size: var(--aw-text-base); }
.workspace-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--aw-text-md); }
.card-arrow { color: var(--aw-muted-strong); font-size: var(--aw-text-base); }

.desc {
  margin: 0;
  color: var(--aw-ink-soft);
}

.workspace-meta { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; color: var(--aw-muted); font-size: var(--aw-text-xs); }
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
