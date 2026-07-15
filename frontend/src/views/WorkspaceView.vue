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
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'

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
const savingDocumentAi = ref(false)

const agent = useAgentRun(props.id)
const { launchMode } = agent
const agentModeOptions = [
  { label: 'Auto', value: 'auto' },
  { label: 'Ask', value: 'permission' },
]

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

async function setDocumentAi(enabled: boolean) {
  if (!workspace.value || savingDocumentAi.value) return
  const previous = Boolean(workspace.value.settings?.doc_llm_optin)
  savingDocumentAi.value = true
  try {
    const settings = await api.patch<NonNullable<WorkspaceSummary['settings']>>(
      `/api/workspaces/${props.id}/settings`,
      { doc_llm_optin: enabled },
    )
    workspace.value.settings = settings
    toast.add({
      severity: enabled ? 'success' : 'secondary',
      summary: `Document AI ${enabled ? 'enabled' : 'disabled'}`,
      life: 2500,
    })
  } catch (error) {
    if (workspace.value.settings) workspace.value.settings.doc_llm_optin = previous
    toast.add({ severity: 'error', summary: 'Could not update Document AI', detail: String(error), life: 5000 })
  } finally {
    savingDocumentAi.value = false
  }
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
      <div class="header-setting document-ai-setting" :class="{ saving: savingDocumentAi }">
        <label for="document-ai">Document AI</label>
        <ToggleSwitch
          inputId="document-ai"
          :modelValue="Boolean(workspace.settings?.doc_llm_optin)"
          :disabled="savingDocumentAi"
          @update:modelValue="setDocumentAi"
        />
      </div>
      <div class="header-setting agent-mode-setting">
        <span>Agent</span>
        <SelectButton
          v-model="launchMode"
          :options="agentModeOptions"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          size="small"
          aria-label="Agent permission mode"
        />
      </div>
      <Button label="Import folder" icon="pi pi-folder-open" size="small" severity="secondary" @click="folderImportOpen = true" />
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
.header-setting { display: flex; align-items: center; gap: 0.5rem; color: #c7d3e2; font-size: 0.7rem; font-weight: 600; white-space: nowrap; }
.header-setting.saving { opacity: 0.65; }
.header-setting :deep(.p-toggleswitch) { width: 2.25rem; height: 1.25rem; }
.header-setting :deep(.p-toggleswitch-slider::before) { width: 0.85rem; height: 0.85rem; margin-top: -0.425rem; }
.agent-mode-setting :deep(.p-selectbutton) { display: flex; }
.agent-mode-setting :deep(.p-togglebutton) { min-width: 2.7rem; padding: 0.32rem 0.55rem; border-color: rgb(255 255 255 / 20%); background: rgb(255 255 255 / 7%); color: #c7d3e2; font-size: 0.68rem; }
.agent-mode-setting :deep(.p-togglebutton.p-togglebutton-checked) { border-color: var(--aw-mint); background: var(--aw-mint); color: var(--aw-navy-950); }
.agent-mode-setting :deep(.p-togglebutton-checked .p-togglebutton-content) { background: transparent; box-shadow: none; }
.workspace-header :deep(.p-button-secondary) { border-color: rgb(255 255 255 / 18%); background: rgb(255 255 255 / 9%); color: #fff; }
.header-link { display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: var(--aw-radius-sm); color: #e6edf6; text-decoration: none; transition: background .15s; }
.header-link:hover { background: rgb(255 255 255 / 10%); }

@media (max-width: 1180px) {
  .brand strong { display: none; }
  .engagement-title h1 { max-width: 14rem; }
}

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
  .workspace-header { padding-inline: 0.85rem; }
  .header-divider, .engagement-title small, .document-ai-setting { display: none; }
  .engagement-title h1 { max-width: 11rem; }
  .workspace-layout { flex-direction: column; overflow: hidden; }
  .workspace-layout > .agent-drawer { height: 32rem; min-height: 0; border-left: 0; border-top: 1px solid #d5dde7; }
  .workspace-layout > .agent-drawer.collapsed { flex: 0 0 3.25rem; height: 3.25rem; }
  .workspace-body { flex-direction: column; }
  .workspace-nav { flex: none; width: 100%; flex-direction: row; overflow-x: auto; padding: 0.55rem 0.75rem; border-right: 0; border-bottom: 1px solid #d5dde7; }
  .nav-label, .nav-privacy { display: none; }
  :deep(.workspace-nav .p-tab) { width: auto; white-space: nowrap; }
  .workspace-panels { padding: 1rem; }
}

@media (max-width: 640px) {
  .brand, .agent-mode-setting, .workspace-header > .p-button { display: none; }
  .engagement-title h1 { max-width: none; }
}
</style>
