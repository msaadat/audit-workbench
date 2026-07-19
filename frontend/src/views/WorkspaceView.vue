<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import Button from 'primevue/button'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { DashboardPhase, EngagementStatusPayload, WorkspaceSummary } from '../types'
import AgentDrawer from '../components/agent/AgentDrawer.vue'
import DashboardTab from '../components/DashboardTab.vue'
import DataTab from '../components/DataTab.vue'
import QueryTab from '../components/QueryTab.vue'
import DataTestsTab from '../components/DataTestsTab.vue'
import ImportDialog from '../components/ImportDialog.vue'
import { collectDroppedFiles, dragHasFiles } from '../composables/useFileDrop'
import { cleanWorkspaceQuery, workspaceQuery } from '../composables/useWorkspaceNavigation'
import PlanningTab from '../components/PlanningTab.vue'
import DocumentsTab from '../components/DocumentsTab.vue'
import DocTestsTab from '../components/DocTestsTab.vue'
import FindingsTab from '../components/FindingsTab.vue'
import ReportTab from '../components/ReportTab.vue'

const props = defineProps<{ id: string }>()
const toast = useToast()
const route = useRoute()
const router = useRouter()

const workspace = ref<WorkspaceSummary | null>(null)
const requestedTab = String(route.query.tab || 'dashboard')
const activeTab = ref(['analysis', 'validation'].includes(requestedTab) ? 'data-tests' : requestedTab)
const initialized = ref(false)
const folderImportOpen = ref(false)
const importDialogRef = ref<InstanceType<typeof ImportDialog> | null>(null)
const dropActive = ref(false)
let dragDepth = 0
const dashboardRef = ref<{ load: () => Promise<void> } | null>(null)
const phaseStatus = ref<Partial<Record<DashboardPhase['id'], DashboardPhase>>>({})

const agent = useAgentRun(props.id)
const phaseStateIcon: Record<DashboardPhase['state'], string> = {
  not_started: 'pi pi-circle',
  in_progress: 'pi pi-clock',
  complete: 'pi pi-check-circle',
  attention: 'pi pi-exclamation-triangle',
}
const phaseStateLabel: Record<DashboardPhase['state'], string> = {
  not_started: 'Not started',
  in_progress: 'In progress',
  complete: 'Complete',
  attention: 'Needs attention',
}

async function loadEngagementStatus() {
  try {
    const payload = await api.get<EngagementStatusPayload>(`/api/workspaces/${props.id}/dashboard/status`)
    phaseStatus.value = Object.fromEntries(payload.phases.map(phase => [phase.id, phase]))
  } catch {
    phaseStatus.value = {}
  }
}

function statusLabel(phase: DashboardPhase) {
  return `${phase.label}: ${phaseStateLabel[phase.state]}`
}

async function reload() {
  try {
    workspace.value = await api.get<WorkspaceSummary>(`/api/workspaces/${props.id}`)
    if (!initialized.value) {
      // Dashboard is the engagement home, including onboarding for empty workspaces.
      if (!route.query.tab) activeTab.value = 'dashboard'
      initialized.value = true
    }
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Workspace not found', detail: String(error), life: 5000 })
  }
}

async function handleImported() {
  await Promise.all([reload(), loadEngagementStatus()])
  if (activeTab.value === 'dashboard') await dashboardRef.value?.load()
}

async function reloadWorkspaceAndStatus() {
  await Promise.all([reload(), loadEngagementStatus()])
}

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
  await Promise.all([reload(), loadEngagementStatus()])
  if (route.query.import === '1') {
    folderImportOpen.value = true
    const query = { ...route.query }
    delete query.import
    await router.replace({ query })
  }
  // Normalize old/bookmarked URLs that accumulated state from several tabs.
  if (route.query.tab) await router.replace({ query: cleanWorkspaceQuery(activeTab.value, route.query) })
})
watch(activeTab, tab => {
  if (route.query.tab !== tab) void router.replace({ query: workspaceQuery(tab) })
  void loadEngagementStatus()
})
watch(() => route.query.tab, tab => {
  const value = ['analysis', 'validation'].includes(String(tab)) ? 'data-tests' : String(tab || '')
  if (value && value !== activeTab.value) activeTab.value = value
})

// Agent mutations can change both workspace counts and engagement status.
const unsubscribe = agent.onWorkspaceChanged((change) => {
  if (change.kind === 'join' || change.kind === 'table') void reload()
  if (['table', 'join', 'planning', 'rcm', 'planned_test', 'datatest', 'doctest', 'observation', 'evidence_request', 'ruleset', 'analysis', 'tile', 'finding', 'report'].includes(change.kind)) {
    void loadEngagementStatus()
  }
})
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
      <span class="header-spacer" />
      <Button label="Import" icon="pi pi-upload" size="small" severity="secondary" @click="folderImportOpen = true" />
      <a href="/about.html" class="header-link" aria-label="About" title="About"><i class="pi pi-info-circle" /></a>
      <router-link to="/" class="header-link" aria-label="All workspaces" title="All workspaces"><i class="pi pi-th-large" /></router-link>
    </header>

    <div class="workspace-layout">
      <Tabs v-model:value="activeTab" class="workspace-tabs">
        <div class="workspace-body">
          <TabList class="workspace-nav">
            <p class="nav-label">Overview</p>
            <Tab value="dashboard"><i class="pi pi-th-large" /><span>Dashboard</span></Tab>
            <p class="nav-label nav-group">Plan</p>
            <Tab value="planning" :aria-label="phaseStatus.planning ? statusLabel(phaseStatus.planning) : 'Planning'" v-tooltip.right="phaseStatus.planning ? statusLabel(phaseStatus.planning) : 'Planning'"><i class="pi pi-map" /><span>Planning</span><i v-if="phaseStatus.planning" class="phase-status" :class="phaseStateIcon[phaseStatus.planning.state]" :data-state="phaseStatus.planning.state" aria-hidden="true" /></Tab>
            <Tab value="documents"><i class="pi pi-folder" /><span>Documents</span></Tab>
            <p class="nav-label nav-group">Fieldwork</p>
            <Tab value="doc-tests" :aria-label="phaseStatus.fieldwork ? statusLabel(phaseStatus.fieldwork) : 'Document tests'" v-tooltip.right="phaseStatus.fieldwork ? statusLabel(phaseStatus.fieldwork) : 'Document tests'"><i class="pi pi-verified" /><span>Document tests</span><i v-if="phaseStatus.fieldwork" class="phase-status" :class="phaseStateIcon[phaseStatus.fieldwork.state]" :data-state="phaseStatus.fieldwork.state" aria-hidden="true" /></Tab>
            <Tab value="data"><i class="pi pi-database" /><span>Data</span></Tab>
            <Tab value="query"><i class="pi pi-search" /><span>Query</span></Tab>
            <Tab value="data-tests"><i class="pi pi-shield" /><span>Data tests</span></Tab>
            <p class="nav-label nav-group output-label">Output</p>
            <Tab value="findings"><i class="pi pi-flag" /><span>Findings</span><small v-if="workspace.finding_count">{{ workspace.finding_count }}</small></Tab>
            <Tab value="report" :aria-label="phaseStatus.report ? statusLabel(phaseStatus.report) : 'Report'" v-tooltip.right="phaseStatus.report ? statusLabel(phaseStatus.report) : 'Report'"><i class="pi pi-file-edit" /><span>Report</span><i v-if="phaseStatus.report" class="phase-status" :class="phaseStateIcon[phaseStatus.report.state]" :data-state="phaseStatus.report.state" aria-hidden="true" /></Tab>
          </TabList>
          <TabPanels class="workspace-panels">
          <TabPanel value="dashboard">
            <DashboardTab v-if="activeTab === 'dashboard'" ref="dashboardRef" :workspace="workspace" @import-requested="folderImportOpen = true" />
          </TabPanel>
          <TabPanel value="planning">
            <PlanningTab v-if="activeTab === 'planning'" :workspace="workspace" @changed="loadEngagementStatus" />
          </TabPanel>
          <TabPanel value="documents">
            <DocumentsTab v-if="activeTab === 'documents'" :workspace="workspace" @changed="reloadWorkspaceAndStatus" @import-requested="folderImportOpen = true" />
          </TabPanel>
          <TabPanel value="doc-tests">
            <DocTestsTab v-if="activeTab === 'doc-tests'" :workspace="workspace" @changed="loadEngagementStatus" />
          </TabPanel>
          <TabPanel value="data">
            <DataTab :workspace="workspace" @changed="reloadWorkspaceAndStatus" @import-requested="folderImportOpen = true" />
          </TabPanel>
          <TabPanel value="query">
            <QueryTab :workspace="workspace" />
          </TabPanel>
          <TabPanel value="data-tests">
            <DataTestsTab v-if="activeTab === 'data-tests'" :workspace="workspace" @changed="reloadWorkspaceAndStatus" />
          </TabPanel>
          <TabPanel value="findings">
            <FindingsTab v-if="activeTab === 'findings'" :workspace="workspace" @changed="reloadWorkspaceAndStatus" />
          </TabPanel>
          <TabPanel value="report">
            <ReportTab v-if="activeTab === 'report'" :workspace="workspace" @changed="loadEngagementStatus" />
          </TabPanel>
          </TabPanels>
        </div>
      </Tabs>
      <AgentDrawer :workspace="workspace" />
    </div>
    <ImportDialog
      ref="importDialogRef"
      v-model="folderImportOpen"
      :workspaceId="props.id"
      @imported="handleImported"
      @planning-started="activeTab = 'planning'"
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
.engagement-title h1 { max-width: 22rem; margin: 0.15rem 0 0; overflow: hidden; color: #fff; font-size: 0.96rem; text-overflow: ellipsis; white-space: nowrap; }
.header-spacer { flex: 1; }
.workspace-header :deep(.p-button-secondary) { border-color: rgb(255 255 255 / 18%); background: rgb(255 255 255 / 9%); color: #fff; }
.header-link { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: var(--aw-radius-sm); color: #e6edf6; text-decoration: none; transition: background .15s; }
.header-link:hover { background: rgb(255 255 255 / 10%); }

@media (max-width: 1180px) {
  .brand strong { display: none; }
  .engagement-title h1 { max-width: 14rem; }
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
.workspace-tabs { flex: 1; min-width: 0; min-height: 0; height: 100%; }

.workspace-body { display: flex; height: 100%; min-height: 0; }
.workspace-nav { flex: 0 0 12.5rem; display: flex; flex-direction: column; align-items: stretch; gap: 0.16rem; padding: 1rem 0.65rem; background: var(--aw-raised); border-right: 1px solid var(--aw-border); }
.nav-label { margin: 0 0.6rem 0.45rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; }
.nav-group { margin-top: 0.7rem; margin-bottom: 0.18rem; }
.workspace-nav :deep(.p-tab small) { margin-left:auto; min-width:1.25rem; padding:.1rem .35rem; border-radius:999px; background:var(--p-primary-50); color:var(--aw-teal); text-align:center; }
.workspace-nav :deep(.phase-status) { margin-left:auto; font-size:.78rem; }
.workspace-nav :deep(.phase-status[data-state='not_started']) { color:#94a3b8; }
.workspace-nav :deep(.phase-status[data-state='in_progress']) { color:#2563eb; }
.workspace-nav :deep(.phase-status[data-state='complete']) { color:#16855b; }
.workspace-nav :deep(.phase-status[data-state='attention']) { color:#d97706; }
.workspace-panels { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 1rem 1.25rem 1.35rem; background: var(--aw-canvas); }
:deep(.workspace-nav .p-tab) {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 0.65rem;
  width: 100%;
  min-height: 2.35rem;
  padding: 0.5rem 0.7rem;
  border: 0;
  border-radius: var(--aw-radius-sm);
  color: #485b74;
  font-weight: 600;
  transition: background .15s, color .15s;
}
:deep(.workspace-nav .p-tab:hover:not([data-p-active='true'])) { background: rgb(255 255 255 / 55%); color: var(--aw-ink); }
:deep(.workspace-nav .p-tab[data-p-active='true']) { color: var(--aw-teal); background: #fff; box-shadow: var(--aw-shadow-sm); }
:deep(.workspace-nav .p-tab[data-p-active='true'] .pi) { color: var(--aw-teal); }
:deep(.workspace-nav .p-tablist-tab-list) { display: contents; }
:deep(.workspace-nav .p-tablist-active-bar) { display: none; }
:deep(.workspace-panels .p-tabpanel) { padding: 0; }

@media (max-width: 900px) {
  .workspace-page { height: 100%; overflow: hidden; }
  .workspace-header { padding-inline: 0.85rem; }
  .header-divider, .engagement-title small { display: none; }
  .engagement-title h1 { max-width: 11rem; }
  .workspace-layout { overflow: hidden; }
  .workspace-tabs { width: 100%; }
  .workspace-body { flex-direction: column; }
  .workspace-nav { flex: none; display: block; width: 100%; padding: 0.45rem 0.65rem; border-right: 0; border-bottom: 1px solid #d5dde7; }
  .nav-label { display: none; }
  :deep(.workspace-nav .p-tablist-content) { overflow-x: auto; scrollbar-width: thin; }
  :deep(.workspace-nav .p-tablist-tab-list) { display: flex; flex-direction: row; gap: 0.16rem; width: max-content; }
  :deep(.workspace-nav .p-tab) { flex: 0 0 auto; width: auto; white-space: nowrap; }
  .workspace-panels { padding: 1rem 1rem calc(4.25rem); }
}

@media (max-width: 640px) {
  .brand, .workspace-header > .p-button { display: none; }
  .engagement-title h1 { max-width: none; }
}
</style>
