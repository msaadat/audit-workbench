<script setup lang="ts">
import { computed, inject, watch } from 'vue'
import { useRouter } from 'vue-router'

import { FILE_SECTIONS, sectionLabel, useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { useTrail } from '../composables/useShell'
import { workspaceContextKey } from '../composables/useWorkspaceContext'
import ApmView from './ApmView.vue'
import PlanningTab from '../components/PlanningTab.vue'
import DataTestsTab from '../components/DataTestsTab.vue'
import DocTestsTab from '../components/DocTestsTab.vue'
import ChainView from '../components/planning/ChainView.vue'
import CycleTab from '../components/planning/CycleTab.vue'
import FindingsTab from '../components/FindingsTab.vue'
import ReportView from './ReportView.vue'

/**
 * One work product, opened from the engagement record.
 *
 * This was the "Audit file" surface, with a rail listing every work product
 * beside whichever one you were reading. The rail is gone: the record is the
 * index now, and it lists the same seven things with what each one cost, what
 * it left open, and what has not run — none of which a rail could say.
 *
 * What replaces it is the trail in the shell bar: where you are, and one click
 * back — to the record through the engagement's own name, which is the way
 * back from every page in the app rather than a link this surface has to draw.
 * The bar that used to sit here spent a whole row on that one link, and was
 * written again in the workbench and on the row page.
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
 * Where the trail says you are.
 *
 * The name comes from the navigation module, which is also where the record's
 * doors resolve, rather than from a map kept here — there were two such maps,
 * one here and one in the workbench, and they had already drifted from the
 * pages they named.
 */
const title = computed(() => sectionLabel('file', section.value))

useTrail(() => [{ label: title.value }])
</script>

<template>
  <div class="ui-surface ui-surface--stacked">
    <div class="ui-surface__panel">
      <ApmView v-if="section === 'apm'" :workspace="workspace" @changed="reloadStatus" />
      <CycleTab v-else-if="section === 'cycle'" :workspace="workspace" @changed="reloadStatus" />
      <PlanningTab v-else-if="section === 'coverage'" :workspace="workspace" @changed="reloadStatus" />
      <DataTestsTab v-else-if="section === 'data-tests'" :workspace="workspace" @changed="reload" />
      <DocTestsTab v-else-if="section === 'doc-tests'" :workspace="workspace" @changed="reloadStatus" />
      <FindingsTab v-else-if="section === 'findings'" :workspace="workspace" @changed="reload" />
      <ChainView v-else-if="section === 'chain'" :workspace="workspace" />
      <ReportView v-else-if="section === 'report'" :workspace="workspace" @changed="reloadStatus" />
    </div>
  </div>
</template>
