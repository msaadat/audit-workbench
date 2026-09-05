<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import Button from 'primevue/button'

import { api } from '../api'
import { useAgentRun } from '../composables/useAgentRun'
import type { EngagementPhase, EngagementSection, EngagementStatusPayload, WorkspaceSummary } from '../types'
import AssistantPanel from '../components/agent/AssistantPanel.vue'
import ImportDialog from '../components/ImportDialog.vue'
import { collectDroppedFiles, dragHasFiles } from '../composables/useFileDrop'
import { useEngagementCrumb } from '../composables/useShell'
import { useWorkspaceNav } from '../composables/useWorkspaceNavigation'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace shell. It owns the folder-import dialog and drop target and
 * the workspace/phase state every surface reads. Surfaces themselves are
 * routes — see docs/agentic-ux-plan.md.
 *
 * It used to draw a header of its own as well: a second navy bar with the same
 * brand mark, the engagement's name as plain text, and eight icon-only
 * controls whose only label was a tooltip. There is one bar now, in `App.vue`.
 * What this still owns is the *state* the bar shows — which engagement, and
 * what the assistant is doing — so it publishes the engagement to the shell
 * and teleports the assistant toggle into the bar with its bindings intact.
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
const phases = ref<EngagementPhase[]>([])
const sectionById = ref<Record<string, EngagementSection>>({})

const agent = useAgentRun(props.id)

/**
 * The panel's state, in the header, on every page.
 *
 * It is the only trace of the assistant when it is closed — the collapsed rail
 * that cost 52px of every surface for one icon is gone — so it has to say
 * whether a run is live without being opened.
 */
const assistantOpen = computed(() => agent.state.panelMode !== 'closed')
const assistantAttention = computed(() => {
  const status = agent.state.run?.status ?? ''
  return status === 'awaiting_approval' || status === 'awaiting_input' || status === 'failed'
})
const assistantLive = computed(() => agent.isActive.value)

/**
 * A deep link can carry the panel: `?assistant=full` on any workspace route,
 * and `?chat=<id>` for the conversation it should open. Applied once and then
 * stripped, so the page's own history entries stay clean.
 */
watch(() => route.query, (query: Record<string, unknown>) => {
  const wanted = String(query.assistant || '')
  const chat = String(query.chat || '')
  if (!wanted && !chat) return
  if (wanted === 'full') agent.setPanelMode('expanded')
  else if (wanted) agent.openPanel()
  else if (chat) agent.openPanel()
  const rest = { ...route.query }
  delete rest.assistant
  delete rest.chat
  void router.replace({ ...route, query: rest })
}, { immediate: true, deep: true })

async function loadEngagementStatus() {
  try {
    const payload = await api.get<EngagementStatusPayload>(`/api/workspaces/${props.id}/engagement/status`)
    phases.value = payload.phases
    sectionById.value = payload.sections ?? {}
  } catch {
    phases.value = []
    sectionById.value = {}
  }
}

async function loadWorkspace() {
  try {
    workspace.value = await api.get<WorkspaceSummary>(`/api/workspaces/${props.id}`)
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Engagement not found', detail: String(error), life: 5000 })
  }
}

// The bar names the engagement as soon as the route does, and corrects the
// name once the workspace answers: an id in the trail is a worse first frame
// than the real name a moment later, but both beat an empty bar.
useEngagementCrumb(() => ({ id: props.id, name: workspace.value?.name || props.id }))

async function reload() {
  await Promise.all([loadWorkspace(), loadEngagementStatus()])
}

function handleImported() {
  // An import is a durable workspace mutation with no agent run behind it, so
  // nothing on the event stream announces it. Publishing on the shared bus is
  // what refreshes this shell (through its own subscription below) *and* every
  // surface listening on it — the engagement record's next step among them,
  // which is exactly the thing an import moves.
  agent.invalidateWorkspace()
}

provide(workspaceContextKey, {
  // Surfaces only render below `v-if="workspace"`, so the non-null assertion
  // holds for every consumer.
  workspace: workspace as unknown as import('vue').Ref<WorkspaceSummary>,
  phases,
  sectionById,
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
    <!-- The one control that belongs to the engagement, in the bar at the top
         of the app. It is rendered here, where its state lives, and lands in
         the header as one of its own children, so the bar shows what the
         assistant is doing without owning any of it.

         `Import` was beside it and is not any more. Importing is something an
         engagement needs a few times and then never again, and it was already
         offered everywhere it is actually wanted: the record's Sources row,
         `Add documents` and `Add files` on the two pages that hold them, the
         assistant's own prompt, and a drop anywhere on the window. A button on
         every page for it bought nothing and cost the bar its one-control
         right cluster. -->
    <Teleport to="#shell-actions">
      <Button
        :label="assistantAttention ? 'Assistant · needs you' : assistantLive ? 'Assistant · working' : 'Assistant'"
        icon="pi pi-sparkles"
        size="small"
        class="assistant-toggle"
        :class="{ on: assistantOpen, attention: assistantAttention }"
        :aria-pressed="assistantOpen"
        aria-label="Assistant"
        :title="assistantOpen ? 'Close the assistant' : 'Open the assistant'"
        @click="agent.togglePanel()"
      />
    </Teleport>

    <div class="workspace-layout">
      <router-view />
      <AssistantPanel :workspace="workspace" />
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
  border-radius: var(--aw-radius-surface);
  background: var(--aw-navy-950);
  color: var(--aw-on-dark);
  box-shadow: var(--aw-shadow-lg);
}
.drop-overlay-card i { font-size: var(--aw-text-3xl); color: var(--aw-mint); }
.drop-overlay-card strong { font-size: var(--aw-text-lg); }
.drop-overlay-card span { color: var(--aw-on-navy-muted); font-size: var(--aw-text-sm); }

.workspace-layout {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}
</style>
