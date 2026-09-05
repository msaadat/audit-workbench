<script setup lang="ts">
import { computed, inject, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { FILE_SECTIONS, useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import PlanningTab from '../components/PlanningTab.vue'
import DataTestsTab from '../components/DataTestsTab.vue'
import DocTestsTab from '../components/DocTestsTab.vue'
import ChainView from '../components/planning/ChainView.vue'
import CycleTab from '../components/planning/CycleTab.vue'
import FindingsTab from '../components/FindingsTab.vue'
import ReportTab from '../components/ReportTab.vue'

/**
 * One work product, opened from the engagement record.
 *
 * This was the "Audit file" surface, with a rail listing every work product
 * beside whichever one you were reading. The rail is gone: the record is the
 * index now, and it lists the same seven things with what each one cost, what
 * it left open, and what has not run — none of which a rail could say.
 *
 * What replaces it is the bar below: where you are, and one click back to the
 * index. That bar belongs to this surface rather than to the pages under it —
 * every page here has the same parent, so none of them should have to be told
 * what it is.
 */

const props = defineProps<{ id: string; section: string }>()
const router = useRouter()
const nav = useWorkspaceNav()
const { workspace, reload, reloadStatus } = inject(workspaceContextKey)!

/** The paths this host answers for.
 *
 * Read from the navigation module rather than restated, so the section list and
 * the destinations it must resolve to cannot drift apart — they did while a
 * rail here kept its own copy. `useWorkspaceNavigation` owns which surface each
 * section lives on; this owns which component answers for it.
 */
const known: readonly string[] = FILE_SECTIONS
const section = computed(() => (known.includes(props.section) ? props.section : 'apm'))

// A bookmarked or hand-edited section that no longer exists lands on the first
// entry rather than an empty surface.
watch(() => props.section, value => {
  if (!known.includes(value)) void router.replace(nav.to('apm'))
}, { immediate: true })

/**
 * What the bar calls each section.
 *
 * These are the record's own row labels, so the name in the bar is the name on
 * the row that was clicked — a reader should not have to work out that "RCM"
 * and "Risk and control matrix" are the same thing. They are stated here rather
 * than read from the record because the bar must not wait on a request to say
 * where you are, and a wrong label is a smaller failure than a bar that arrives
 * late or empty.
 */
const SECTION_LABEL: Record<string, string> = {
  apm: 'Audit planning memorandum',
  cycle: 'Cycle',
  coverage: 'Risk and control matrix',
  'data-tests': 'Test programme',
  'doc-tests': 'Document test results',
  findings: 'Findings register',
  chain: 'Chain',
  report: 'Report',
}

const title = computed(() => SECTION_LABEL[section.value] ?? '')
</script>

<template>
  <div class="ui-surface ui-surface--stacked">
    <!-- What the rail used to do, done in one row: where you are, and the way
         back. -->
    <nav class="crumb" aria-label="Breadcrumb">
      <RouterLink :to="nav.to('record')" class="crumb__back">
        <i class="pi pi-arrow-left" aria-hidden="true" />Engagement record
      </RouterLink>
      <span class="crumb__sep" aria-hidden="true">/</span>
      <span class="crumb__cur" aria-current="page">{{ title }}</span>
    </nav>

    <div class="ui-surface__panel">
      <PlanningTab v-if="section === 'apm'" :workspace="workspace" section="apm" @changed="reloadStatus" />
      <CycleTab v-else-if="section === 'cycle'" :workspace="workspace" @changed="reloadStatus" />
      <PlanningTab v-else-if="section === 'coverage'" :workspace="workspace" section="rcm" @changed="reloadStatus" />
      <DataTestsTab v-else-if="section === 'data-tests'" :workspace="workspace" @changed="reload" />
      <DocTestsTab v-else-if="section === 'doc-tests'" :workspace="workspace" @changed="reloadStatus" />
      <FindingsTab v-else-if="section === 'findings'" :workspace="workspace" @changed="reload" />
      <ChainView v-else-if="section === 'chain'" :workspace="workspace" />
      <ReportTab v-else-if="section === 'report'" :workspace="workspace" @changed="reloadStatus" />
    </div>
  </div>
</template>
