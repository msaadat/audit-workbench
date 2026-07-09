<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'

import { api } from '../api'
import type { WorkspaceSummary } from '../types'
import DashboardTab from '../components/DashboardTab.vue'
import DataTab from '../components/DataTab.vue'
import QueryTab from '../components/QueryTab.vue'
import AnalysisTab from '../components/AnalysisTab.vue'

const props = defineProps<{ id: string }>()
const toast = useToast()

const workspace = ref<WorkspaceSummary | null>(null)
const activeTab = ref('data')
const initialized = ref(false)

async function reload() {
  try {
    workspace.value = await api.get<WorkspaceSummary>(`/api/workspaces/${props.id}`)
    if (!initialized.value) {
      // Land on the dashboard when the workspace already has pinned work.
      activeTab.value = (workspace.value.tile_count ?? 0) > 0 ? 'dashboard' : 'data'
      initialized.value = true
    }
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Workspace not found', detail: String(error), life: 5000 })
  }
}

onMounted(reload)
</script>

<template>
  <div class="page" v-if="workspace">
    <div class="ws-header">
      <div>
        <h1>{{ workspace.name }}</h1>
        <p class="muted">{{ workspace.description || 'No description.' }}</p>
      </div>
    </div>

    <Tabs v-model:value="activeTab">
      <TabList>
        <Tab value="dashboard"><i class="pi pi-th-large" /> Dashboard</Tab>
        <Tab value="data"><i class="pi pi-database" /> Data</Tab>
        <Tab value="query"><i class="pi pi-search" /> Query</Tab>
        <Tab value="analysis"><i class="pi pi-shield" /> Analysis</Tab>
      </TabList>
      <TabPanels>
        <TabPanel value="dashboard">
          <DashboardTab v-if="activeTab === 'dashboard'" :workspace="workspace" />
        </TabPanel>
        <TabPanel value="data">
          <DataTab :workspace="workspace" @changed="reload" />
        </TabPanel>
        <TabPanel value="query">
          <QueryTab :workspace="workspace" />
        </TabPanel>
        <TabPanel value="analysis">
          <!-- KeepAlive so an in-progress AI chat survives visiting other tabs. -->
          <KeepAlive>
            <AnalysisTab v-if="activeTab === 'analysis'" :workspace="workspace" />
          </KeepAlive>
        </TabPanel>
      </TabPanels>
    </Tabs>
  </div>
</template>

<style scoped>
.ws-header {
  margin-bottom: 1rem;
}

h1 {
  margin: 0 0 0.25rem;
  font-size: 1.4rem;
}

.ws-header p {
  margin: 0;
}

:deep(.p-tab) {
  display: flex;
  align-items: center;
  gap: 0.45rem;
}
</style>
