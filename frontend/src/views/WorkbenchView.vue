<script setup lang="ts">
import { computed, inject, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useWorkspaceNav, type WorkspaceDestination } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import DocumentsTab from '../components/DocumentsTab.vue'
import DataTab from '../components/DataTab.vue'
import QueryTab from '../components/QueryTab.vue'
import AnalysisTab from '../components/AnalysisTab.vue'

/**
 * The workbench: the same sources and tools the agent uses, driven by hand.
 * Tables and Query stay mounted while the workbench is open — they did under
 * the tab shell too, and both hold enough in-memory state that unmounting them
 * on a section change would lose the auditor's work.
 */

const props = defineProps<{ id: string; section: string }>()
const router = useRouter()
const nav = useWorkspaceNav()
const { workspace, reload, requestImport } = inject(workspaceContextKey)!

const rail: Array<{ section: string; destination: WorkspaceDestination; label: string; icon: string }> = [
  { section: 'documents', destination: 'documents', label: 'Documents', icon: 'pi pi-folder' },
  { section: 'tables', destination: 'data', label: 'Tables', icon: 'pi pi-database' },
  { section: 'query', destination: 'query', label: 'Query', icon: 'pi pi-search' },
  { section: 'analysis', destination: 'analysis', label: 'Analysis', icon: 'pi pi-chart-bar' },
]

const known = rail.map(entry => entry.section)
const section = computed(() => (known.includes(props.section) ? props.section : 'documents'))

watch(() => props.section, value => {
  if (!known.includes(value)) void router.replace(nav.to('documents'))
}, { immediate: true })
</script>

<template>
  <div class="ui-surface">
    <nav class="ui-surface__rail" aria-label="Workbench sections">
      <p class="ui-surface__label">Workbench</p>
      <router-link
        v-for="entry in rail"
        :key="entry.section"
        :to="nav.to(entry.destination)"
        class="ui-surface__item"
        :class="{ active: section === entry.section }"
      >
        <i :class="entry.icon" />
        <span>{{ entry.label }}</span>
      </router-link>
    </nav>

    <div class="ui-surface__panel">
      <DocumentsTab v-if="section === 'documents'" :workspace="workspace" @changed="reload" @import-requested="requestImport" />
      <AnalysisTab v-else-if="section === 'analysis'" :workspace="workspace" />
      <!-- Wrapped because both render a fragment, and `v-show` needs a single
           root element to hide. The tab shell wrapped them in a panel div too. -->
      <div v-show="section === 'tables'">
        <DataTab :workspace="workspace" @changed="reload" @import-requested="requestImport" />
      </div>
      <div v-show="section === 'query'">
        <QueryTab :workspace="workspace" />
      </div>
    </div>
  </div>
</template>
