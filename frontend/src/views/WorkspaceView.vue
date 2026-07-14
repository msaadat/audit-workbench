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
import type { WorkspaceSummary } from '../types'
import AgentDrawer from '../components/agent/AgentDrawer.vue'
import DashboardTab from '../components/DashboardTab.vue'
import DataTab from '../components/DataTab.vue'
import QueryTab from '../components/QueryTab.vue'
import AnalysisTab from '../components/AnalysisTab.vue'
import ValidationTab from '../components/validation/ValidationTab.vue'
import FolderImportDialog from '../components/FolderImportDialog.vue'
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
const activeTab = ref(String(route.query.tab || 'dashboard'))
const initialized = ref(false)
const folderImportOpen = ref(false)
const dashboardRef = ref<{ load: () => Promise<void> } | null>(null)

const agent = useAgentRun(props.id)

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
  await reload()
  if (activeTab.value === 'dashboard') await dashboardRef.value?.load()
}

onMounted(async () => {
  await reload()
  if (route.query.import === '1') {
    folderImportOpen.value = true
    const query = { ...route.query }
    delete query.import
    await router.replace({ query })
  }
})
watch(activeTab, tab => { if (route.query.tab !== tab) void router.replace({ query: { ...route.query, tab } }) })
watch(() => route.query.tab, tab => { if (tab && tab !== activeTab.value) activeTab.value = String(tab) })

// Agent-created tables/joins change the workspace summary every tab reads.
const unsubscribe = agent.onWorkspaceChanged((change) => {
  if (change.kind === 'join' || change.kind === 'table') void reload()
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="page workspace-page" v-if="workspace">
    <div class="workspace-bar">
      <div>
        <p class="eyebrow">Audit engagement</p>
        <h1>{{ workspace.name }}</h1>
        <p class="muted">{{ workspace.description || 'No description.' }}</p>
      </div>
      <div class="workspace-facts">
        <span><strong>{{ workspace.tables.length }}</strong> tables</span>
        <span><strong>{{ workspace.tables.reduce((sum, table) => sum + (table.rows ?? 0), 0).toLocaleString() }}</strong> rows</span>
        <span><i class="pi pi-lock" /> Rows processed locally</span>
        <Button label="Import files" icon="pi pi-folder-open" size="small" severity="secondary" @click="folderImportOpen = true" />
      </div>
    </div>

    <div class="workspace-layout">
      <Tabs v-model:value="activeTab" class="workspace-tabs">
        <div class="workspace-body">
          <TabList class="workspace-nav">
            <p class="nav-label">Overview</p>
            <Tab value="dashboard"><i class="pi pi-th-large" /><span>Dashboard</span></Tab>
            <p class="nav-label nav-group">Plan</p>
            <Tab value="planning"><i class="pi pi-map" /><span>Planning</span></Tab>
            <Tab value="documents"><i class="pi pi-folder" /><span>Documents</span></Tab>
            <p class="nav-label nav-group">Fieldwork</p>
            <Tab value="doc-tests"><i class="pi pi-verified" /><span>Document tests</span></Tab>
            <Tab value="data"><i class="pi pi-database" /><span>Data</span></Tab>
            <Tab value="query"><i class="pi pi-search" /><span>Query</span></Tab>
            <Tab value="validation"><i class="pi pi-check-square" /><span>Validation</span></Tab>
            <Tab value="analysis"><i class="pi pi-shield" /><span>Analysis</span></Tab>
            <p class="nav-label nav-group output-label">Output</p>
            <Tab value="findings"><i class="pi pi-flag" /><span>Findings</span><small v-if="workspace.finding_count">{{ workspace.finding_count }}</small></Tab>
            <Tab value="report"><i class="pi pi-file-edit" /><span>Report</span></Tab>
            <div class="nav-privacy"><i class="pi pi-shield" /><span>{{ workspace.settings?.doc_llm_optin ? 'Rows stay local; confirmed document pages may be disclosed.' : 'Raw data and documents remain on this device.' }}</span></div>
          </TabList>
          <TabPanels class="workspace-panels">
          <TabPanel value="dashboard">
            <DashboardTab v-if="activeTab === 'dashboard'" ref="dashboardRef" :workspace="workspace" @import-requested="folderImportOpen = true" />
          </TabPanel>
          <TabPanel value="planning">
            <PlanningTab v-if="activeTab === 'planning'" :workspace="workspace" />
          </TabPanel>
          <TabPanel value="documents">
            <DocumentsTab v-if="activeTab === 'documents'" :workspace="workspace" @changed="reload" @planning-started="activeTab = 'planning'" />
          </TabPanel>
          <TabPanel value="doc-tests">
            <DocTestsTab v-if="activeTab === 'doc-tests'" :workspace="workspace" />
          </TabPanel>
          <TabPanel value="data">
            <DataTab :workspace="workspace" @changed="reload" />
          </TabPanel>
          <TabPanel value="query">
            <QueryTab :workspace="workspace" />
          </TabPanel>
          <TabPanel value="validation">
            <!-- KeepAlive so an unsaved rule-set draft survives visiting other tabs. -->
            <KeepAlive>
              <ValidationTab v-if="activeTab === 'validation'" :workspace="workspace" />
            </KeepAlive>
          </TabPanel>
          <TabPanel value="analysis">
            <KeepAlive>
              <AnalysisTab v-if="activeTab === 'analysis'" :workspace="workspace" />
            </KeepAlive>
          </TabPanel>
          <TabPanel value="findings">
            <FindingsTab v-if="activeTab === 'findings'" :workspace="workspace" @changed="reload" />
          </TabPanel>
          <TabPanel value="report">
            <ReportTab v-if="activeTab === 'report'" :workspace="workspace" />
          </TabPanel>
          </TabPanels>
        </div>
      </Tabs>
      <AgentDrawer :workspace="workspace" />
    </div>
    <FolderImportDialog
      v-model="folderImportOpen"
      :workspaceId="props.id"
      :documentAiEnabled="Boolean(workspace.settings?.doc_llm_optin)"
      @imported="handleImported"
      @settings-changed="reload"
      @planning-started="activeTab = 'planning'"
    />
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

.workspace-bar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 2rem;
  min-height: 5.5rem;
  padding: 0.9rem 2rem;
  background: #fff;
  border-bottom: 1px solid var(--aw-border);
  box-shadow: var(--aw-shadow-sm);
}

h1 {
  margin: 0 0 0.18rem;
  font-size: var(--aw-text-xl);
}

.workspace-bar p {
  margin: 0;
}

.workspace-facts { display: flex; align-items: center; gap: 1.2rem; color: var(--aw-muted); font-size: 0.75rem; }
.workspace-facts span { display: flex; align-items: center; gap: 0.35rem; white-space: nowrap; }
.workspace-facts strong { color: var(--aw-ink); font-size: 0.9rem; }

.workspace-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}
.workspace-tabs { flex: 1; min-width: 0; min-height: 0; height: 100%; }
.workspace-layout > .agent-drawer { height: 100%; min-height: 0; }

.workspace-body { display: flex; height: 100%; min-height: 0; }
.workspace-nav { flex: 0 0 13.5rem; display: flex; flex-direction: column; align-items: stretch; gap: 0.22rem; padding: 1.25rem 0.75rem; background: var(--aw-raised); border-right: 1px solid var(--aw-border); }
.nav-label { margin: 0 0.6rem 0.45rem; color: var(--aw-muted); font-size: var(--aw-text-xs); font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; }
.nav-group { margin-top: 0.7rem; margin-bottom: 0.18rem; }
.workspace-nav :deep(.p-tab small) { margin-left:auto; min-width:1.25rem; padding:.1rem .35rem; border-radius:999px; background:var(--p-primary-50); color:var(--aw-teal); text-align:center; }
.workspace-panels { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; padding: 1.25rem 1.5rem 1.75rem; background: var(--aw-canvas); }
.nav-privacy { margin-top: auto; display: flex; gap: 0.55rem; padding: 0.8rem; border-top: 1px solid var(--aw-border); color: var(--aw-muted); font-size: var(--aw-text-xs); line-height: 1.35; }
.nav-privacy i { color: var(--aw-teal); margin-top: 0.1rem; }

:deep(.workspace-nav .p-tab) {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 0.65rem;
  width: 100%;
  padding: 0.7rem 0.8rem;
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
  .workspace-bar { padding-inline: 1.25rem; align-items: flex-start; }
  .workspace-facts span:nth-child(2) { display: none; }
  .workspace-layout { flex-direction: column; overflow: hidden; }
  .workspace-layout > .agent-drawer { height: 32rem; min-height: 0; border-left: 0; border-top: 1px solid #d5dde7; }
  .workspace-layout > .agent-drawer.collapsed { flex: 0 0 3.25rem; height: 3.25rem; }
  .workspace-body { flex-direction: column; }
  .workspace-nav { flex: none; width: 100%; flex-direction: row; overflow-x: auto; padding: 0.55rem 0.75rem; border-right: 0; border-bottom: 1px solid #d5dde7; }
  .nav-label, .nav-privacy { display: none; }
  :deep(.workspace-nav .p-tab) { width: auto; white-space: nowrap; }
  .workspace-panels { padding: 1rem; }
}
</style>
