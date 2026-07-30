<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { DashboardPhase, EngagementStatusPayload, WorkspaceSummary } from '../types'
import AgentDrawer from '../components/agent/AgentDrawer.vue'
import ImportDialog from '../components/ImportDialog.vue'
import { collectDroppedFiles, dragHasFiles } from '../composables/useFileDrop'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace shell. It owns the engagement header, the surface switcher, the
 * folder-import dialog and drop target, and the workspace/phase state every
 * surface reads. Surfaces themselves are routes — see docs/agentic-ux-plan.md.
 */

const props = defineProps<{ id: string }>()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const nav = useWorkspaceNav()

const workspace = ref<WorkspaceSummary | null>(null)
const folderImportOpen = ref(false)
const importDialogRef = ref<InstanceType<typeof ImportDialog> | null>(null)
const dropActive = ref(false)
let dragDepth = 0
const phases = ref<DashboardPhase[]>([])
const phaseById = computed(() => Object.fromEntries(phases.value.map(phase => [phase.id, phase])) as Partial<Record<DashboardPhase['id'], DashboardPhase>>)

const agent = useAgentRun(props.id)

// The console owns the assistant full-width; every other surface keeps it as a
// sidecar so a question is always one click away without leaving the record.
const onConsole = computed(() => route.name === 'workspace')
const surface = computed(() => {
  if (route.name === 'workspace-file') return 'file'
  if (route.name === 'workspace-bench') return 'bench'
  return 'console'
})

async function loadEngagementStatus() {
  try {
    const payload = await api.get<EngagementStatusPayload>(`/api/workspaces/${props.id}/dashboard/status`)
    phases.value = payload.phases
  } catch {
    phases.value = []
  }
}

async function loadWorkspace() {
  try {
    workspace.value = await api.get<WorkspaceSummary>(`/api/workspaces/${props.id}`)
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Workspace not found', detail: String(error), life: 5000 })
  }
}

async function reload() {
  await Promise.all([loadWorkspace(), loadEngagementStatus()])
}

async function handleImported() {
  await reload()
}

provide(workspaceContextKey, {
  // Surfaces only render below `v-if="workspace"`, so the non-null assertion
  // holds for every consumer.
  workspace: workspace as unknown as import('vue').Ref<WorkspaceSummary>,
  phases,
  phaseById,
  reload,
  reloadStatus: loadEngagementStatus,
  requestImport: () => { folderImportOpen.value = true },
})

// Workspace-wide desktop drop target. Listeners live on window so a drop on
// any part of the page (including the dialog mask portal) never triggers the
// browser's default file navigation.
function onWindowDragEnter(event: DragEvent) {
  if (!dragHasFiles(event) || folderImportOpen.value) return
  dragDepth += 1
  dropActive.value = true
}

function onWindowDragOver(event: DragEvent) {
  if (dragHasFiles(event)) event.preventDefault()
}

function onWindowDragLeave(event: DragEvent) {
  if (!dragHasFiles(event) || folderImportOpen.value) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dropActive.value = false
}

async function onWindowDrop(event: DragEvent) {
  if (!dragHasFiles(event)) return
  event.preventDefault()
  dragDepth = 0
  dropActive.value = false
  // The open dialog's own dropzone handles its drops.
  if (folderImportOpen.value) return
  const files = await collectDroppedFiles(event)
  if (!files.length) return
  importDialogRef.value?.stageExternal(files)
  folderImportOpen.value = true
}

onMounted(async () => {
  window.addEventListener('dragenter', onWindowDragEnter)
  window.addEventListener('dragover', onWindowDragOver)
  window.addEventListener('dragleave', onWindowDragLeave)
  window.addEventListener('drop', onWindowDrop)
  await reload()
  if (route.query.import === '1') {
    folderImportOpen.value = true
    const query = { ...route.query }
    delete query.import
    await router.replace({ query })
  }
})

// A workspace revision is the universal invalidation boundary for agent
// commits. Refresh the shell counts and phase status; surfaces subscribe
// separately for their own content.
const unsubscribe = agent.onWorkspaceInvalidated(() => { void reload() })
onUnmounted(() => {
  unsubscribe()
  window.removeEventListener('dragenter', onWindowDragEnter)
  window.removeEventListener('dragover', onWindowDragOver)
  window.removeEventListener('dragleave', onWindowDragLeave)
  window.removeEventListener('drop', onWindowDrop)
})
</script>

<template>
  <div class="page workspace-page" v-if="workspace">
    <header class="workspace-header">
      <router-link to="/" class="brand" aria-label="Audit Workbench home">
        <span class="brand-mark"><i class="pi pi-verified" /></span>
        <strong>Audit Workbench</strong>
      </router-link>
      <span class="header-divider" />
      <div class="engagement-title">
        <small>Engagement</small>
        <h1>{{ workspace.name }}</h1>
      </div>

      <nav class="surface-switcher" aria-label="Workspace surfaces">
        <router-link :to="nav.to('console')" :class="{ active: surface === 'console' }">
          <i class="pi pi-sparkles" /><span>Console</span>
        </router-link>
        <router-link :to="nav.to('dashboard')" :class="{ active: surface === 'file' }">
          <i class="pi pi-book" /><span>Audit file</span>
        </router-link>
        <router-link :to="nav.to('documents')" :class="{ active: surface === 'bench' }">
          <i class="pi pi-wrench" /><span>Workbench</span>
        </router-link>
      </nav>

      <span class="header-spacer" />
      <Button label="Import" icon="pi pi-upload" size="small" severity="secondary" @click="folderImportOpen = true" />
      <router-link :to="`/workspace/${props.id}/debug`" class="header-link" aria-label="Debug console" title="Debug console"><i class="pi pi-code" /></router-link>
      <a href="/about.html" class="header-link" aria-label="About" title="About"><i class="pi pi-info-circle" /></a>
      <router-link to="/" class="header-link" aria-label="All workspaces" title="All workspaces"><i class="pi pi-th-large" /></router-link>
    </header>

    <div class="workspace-layout">
      <router-view />
      <AgentDrawer v-if="!onConsole" :workspace="workspace" />
    </div>

    <ImportDialog
      ref="importDialogRef"
      v-model="folderImportOpen"
      :workspaceId="props.id"
      @imported="handleImported"
      @planning-started="router.push(nav.to('apm'))"
    />
    <div v-if="dropActive" class="drop-overlay" aria-hidden="true">
      <div class="drop-overlay-card">
        <i class="pi pi-cloud-upload" />
        <strong>Drop to import</strong>
        <span>Files and folders are staged for review before anything is added.</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.workspace-page {
  flex: 1;
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workspace-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 0.85rem;
  min-height: 3.75rem;
  padding: 0.5rem 1.5rem;
  color: #fff;
  background: linear-gradient(180deg, var(--aw-navy-900) 0%, var(--aw-navy-950) 100%);
  border-bottom: 1px solid rgb(94 234 212 / 14%);
  box-shadow: var(--aw-shadow-sm);
}

.brand { display: inline-flex; align-items: center; gap: 0.65rem; flex: 0 0 auto; color: #fff; text-decoration: none; }
.brand-mark { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: var(--aw-radius-sm); color: var(--aw-navy-950); background: linear-gradient(135deg, var(--aw-mint) 0%, #2dd4bf 100%); box-shadow: 0 0 0 1px rgb(94 234 212 / 25%), 0 2px 8px rgb(45 212 191 / 30%); }
.brand strong { font-size: 0.95rem; white-space: nowrap; }
.header-divider { align-self: stretch; width: 1px; margin: 0.15rem 0.1rem; background: rgb(255 255 255 / 16%); }
.engagement-title { min-width: 0; line-height: 1.1; }
.engagement-title small { color: #8fa6c2; font-size: 0.6rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
.engagement-title h1 { max-width: 18rem; margin: 0.15rem 0 0; overflow: hidden; color: #fff; font-size: 0.96rem; text-overflow: ellipsis; white-space: nowrap; }
.header-spacer { flex: 1; }

/* The surface switcher is the primary navigation now, so it sits with the
   engagement identity rather than in the utility cluster on the right. */
.surface-switcher { display: flex; gap: 0.15rem; margin-left: 0.5rem; padding: 0.15rem; border-radius: var(--aw-radius-sm); background: rgb(255 255 255 / 8%); }
.surface-switcher a {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.7rem;
  border-radius: 6px;
  color: #c6d6ea;
  font-size: 0.78rem;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
  transition: background .15s, color .15s;
}
.surface-switcher a:hover { background: rgb(255 255 255 / 10%); color: #fff; }
.surface-switcher a.active { background: #fff; color: var(--aw-navy-900); box-shadow: var(--aw-shadow-sm); }
.surface-switcher i { font-size: 0.8rem; }
/* The count is the point of the Decisions entry, so it survives the narrow
   breakpoint that drops the labels. */
.surface-switcher em {
  min-width: 1.15rem;
  padding: 0.02rem 0.3rem;
  border-radius: 999px;
  background: var(--aw-mint);
  color: var(--aw-navy-950);
  font-size: 0.66rem;
  font-style: normal;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-align: center;
}
.surface-switcher a.active em { background: var(--aw-navy-900); color: #fff; }

.workspace-header :deep(.p-button-secondary) { border-color: rgb(255 255 255 / 18%); background: rgb(255 255 255 / 9%); color: #fff; }
.header-link { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: var(--aw-radius-sm); color: #e6edf6; text-decoration: none; transition: background .15s; }
.header-link:hover { background: rgb(255 255 255 / 10%); }

@media (max-width: 1280px) {
  .brand strong { display: none; }
  .engagement-title h1 { max-width: 12rem; }
  .surface-switcher span { display: none; }
  .surface-switcher a { padding-inline: 0.55rem; }
}

.drop-overlay {
  position: fixed;
  z-index: 1300;
  inset: 0;
  display: grid;
  place-items: center;
  background: rgb(7 22 43 / 45%);
  pointer-events: none;
}
.drop-overlay-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.35rem;
  padding: 2.2rem 3rem;
  border: 2px dashed var(--aw-mint);
  border-radius: 14px;
  background: var(--aw-navy-950);
  color: #fff;
  box-shadow: 0 18px 50px rgb(2 10 22 / 45%);
}
.drop-overlay-card i { font-size: 2.4rem; color: var(--aw-mint); }
.drop-overlay-card strong { font-size: 1.05rem; }
.drop-overlay-card span { color: #8fa6c2; font-size: 0.78rem; }

.workspace-layout {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}
</style>
