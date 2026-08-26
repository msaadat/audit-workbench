<script setup lang="ts">
import { computed, inject, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { useAgentRun } from '../composables/useAgentRun'
import { useWorkspaceNav, type WorkspaceDestination } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import type { DashboardPhase } from '../types'
import DashboardTab from '../components/DashboardTab.vue'
import PlanningTab from '../components/PlanningTab.vue'
import DataTestsTab from '../components/DataTestsTab.vue'
import DocTestsTab from '../components/DocTestsTab.vue'
import ChainView from '../components/planning/ChainView.vue'
import FindingsTab from '../components/FindingsTab.vue'
import ReportTab from '../components/ReportTab.vue'

/**
 * The audit file: the engagement's work product, from planning through report.
 * One section renders at a time, which is what the tab shell effectively did
 * with its per-panel `v-if`.
 */

const props = defineProps<{ id: string; section: string }>()
const router = useRouter()
const nav = useWorkspaceNav()
const { workspace, phaseById, sectionById, reload, reloadStatus, requestImport } = inject(workspaceContextKey)!
const agent = useAgentRun(props.id)
const dashboardRef = ref<{ load: () => Promise<void> } | null>(null)

interface RailEntry {
  section: string
  destination: WorkspaceDestination
  label: string
  icon: string
  phase?: DashboardPhase['id']
  count?: () => number
}

const rail: RailEntry[] = [
  { section: 'dashboard', destination: 'dashboard', label: 'Dashboard', icon: 'pi pi-th-large' },
  { section: 'apm', destination: 'apm', label: 'APM', icon: 'pi pi-map', phase: 'planning' },
  { section: 'coverage', destination: 'rcm', label: 'RCM', icon: 'pi pi-table', phase: 'planning' },
  { section: 'data-tests', destination: 'data-tests', label: 'Data tests', icon: 'pi pi-shield' },
  { section: 'doc-tests', destination: 'doc-tests', label: 'Document tests', icon: 'pi pi-verified', phase: 'fieldwork' },
  { section: 'findings', destination: 'findings', label: 'Findings', icon: 'pi pi-flag', count: () => workspace.value.finding_count ?? 0 },
  { section: 'chain', destination: 'chain', label: 'Chain', icon: 'pi pi-sitemap' },
  { section: 'report', destination: 'report', label: 'Report', icon: 'pi pi-file-edit', phase: 'report' },
]

const known = rail.map(entry => entry.section)
const section = computed(() => (known.includes(props.section) ? props.section : 'dashboard'))

// A bookmarked or hand-edited section that no longer exists lands on the first
// entry rather than an empty surface.
watch(() => props.section, value => {
  if (!known.includes(value)) void router.replace(nav.to('dashboard'))
}, { immediate: true })

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
// A section state, where one exists, answers for the tab the badge sits on. The
// phase behind it is broader — "Fieldwork" spans data tests and RCM coverage —
// so badging the phase on Document tests reported work that lives elsewhere.
function statusFor(entry: RailEntry) {
  return sectionById.value[entry.section]
    ?? (entry.phase ? phaseById.value[entry.phase] : undefined)
}
// Every issue, not the first one. A tooltip that showed one of five said the
// same thing whether one thing or five were outstanding, and the rail badge it
// explains is the only warning some of them ever get.
function statusLabel(entry: RailEntry) {
  const status = statusFor(entry)
  if (!status) return entry.label
  const detail = (status.issues ?? []).join(' ')
  return `${status.label}: ${phaseStateLabel[status.state]}${detail ? ` — ${detail}` : ''}`
}

// The dashboard is a projection of committed work, so an agent commit has to
// refresh it while it is on screen.
const unsubscribe = agent.onWorkspaceInvalidated(() => {
  if (section.value === 'dashboard') void dashboardRef.value?.load()
})
onUnmounted(unsubscribe)
</script>

<template>
  <div class="ui-surface">
    <nav class="ui-surface__rail" aria-label="Audit file sections">
      <p class="ui-surface__label">Audit file</p>
      <router-link
        v-for="entry in rail"
        :key="entry.section"
        :to="nav.to(entry.destination)"
        class="ui-surface__item"
        :class="{ active: section === entry.section }"
        :aria-label="statusLabel(entry)"
        v-tooltip.right="statusLabel(entry)"
      >
        <i :class="entry.icon" />
        <span>{{ entry.label }}</span>
        <small v-if="entry.count && entry.count()">{{ entry.count() }}</small>
        <i
          v-else-if="statusFor(entry)"
          class="phase-status"
          :class="phaseStateIcon[statusFor(entry)!.state]"
          :data-state="statusFor(entry)!.state"
          aria-hidden="true"
        />
      </router-link>
    </nav>

    <div class="ui-surface__panel">
      <DashboardTab v-if="section === 'dashboard'" ref="dashboardRef" :workspace="workspace" @import-requested="requestImport" />
      <PlanningTab v-else-if="section === 'apm'" :workspace="workspace" section="apm" @changed="reloadStatus" />
      <PlanningTab v-else-if="section === 'coverage'" :workspace="workspace" section="rcm" @changed="reloadStatus" />
      <DataTestsTab v-else-if="section === 'data-tests'" :workspace="workspace" @changed="reload" />
      <DocTestsTab v-else-if="section === 'doc-tests'" :workspace="workspace" @changed="reloadStatus" />
      <FindingsTab v-else-if="section === 'findings'" :workspace="workspace" @changed="reload" />
      <ChainView v-else-if="section === 'chain'" :workspace="workspace" />
      <ReportTab v-else-if="section === 'report'" :workspace="workspace" @changed="reloadStatus" />
    </div>
  </div>
</template>
