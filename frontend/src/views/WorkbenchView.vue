<script setup lang="ts">
import { computed, inject, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { BENCH_SECTIONS, useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import DocumentsTab from '../components/DocumentsTab.vue'
import DataTab from '../components/DataTab.vue'
import QueryTab from '../components/QueryTab.vue'
import AnalysisTab from '../components/AnalysisTab.vue'

/**
 * The sources this engagement holds, and the bench for working them by hand.
 *
 * This was the "Workbench" surface with a rail of its own. The rail is gone for
 * the same reason the audit file's went: every section here is a door on the
 * engagement record — documents and tables from the Sources row, the analysis
 * library from its own, and Query as the tool beside it.
 *
 * Tables and Query stay mounted while this surface is open. They did under the
 * tab shell too, and both hold enough in-memory state that unmounting them on a
 * section change would lose the auditor's work — which is why the two are kept
 * in one host rather than split across the record's two doors.
 */

const props = defineProps<{ id: string; section: string }>()
const router = useRouter()
const nav = useWorkspaceNav()
const { workspace, reload, requestImport } = inject(workspaceContextKey)!

/** The paths this host answers for, read from the module that owns them. */
const known: readonly string[] = BENCH_SECTIONS

/** What the crumb calls each section — the record's own words for the door. */
const SECTION_LABEL: Record<string, string> = {
  documents: 'Documents',
  tables: 'Source tables',
  query: 'Query',
  analysis: 'Analysis library',
}
const title = computed(() => SECTION_LABEL[section.value] ?? '')
const section = computed(() => (known.includes(props.section) ? props.section : 'documents'))

watch(() => props.section, value => {
  if (!known.includes(value)) void router.replace(nav.to('documents'))
}, { immediate: true })
</script>

<template>
  <div class="ui-surface ui-surface--stacked">
    <nav class="crumb" aria-label="Breadcrumb">
      <RouterLink :to="nav.to('record')" class="crumb__back">
        <i class="pi pi-arrow-left" aria-hidden="true" />Engagement record
      </RouterLink>
      <span class="crumb__sep" aria-hidden="true">/</span>
      <span class="crumb__cur" aria-current="page">{{ title }}</span>
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
