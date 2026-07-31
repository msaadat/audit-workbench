<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

import { useAgentRun } from '../../composables/useAgentRun'
import type { WorkspaceSummary } from '../../types'
import ConsoleThread from './ConsoleThread.vue'
import PlanSpine from './PlanSpine.vue'

/**
 * The assistant as a resizable sidecar, for the surfaces that are not the
 * console. It owns the frame — collapse state, width, resize — and nothing
 * else; the thread itself lives in `ConsoleThread`.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const agent = useAgentRun(props.workspace.id)
const drawerWidth = ref(416)
const resizing = ref(false)
// The drawer has no room for the plan rail the console shows beside the thread,
// so the same rail is reachable here as an overlay.
const planOpen = ref(false)
const MIN_WIDTH = 340
const MAX_WIDTH = 680
// A surface rail plus a two-pane detail layout that still reads.
const MIN_CONTENT_WIDTH = 880
const WIDTH_KEY = `audit-workbench:agent-drawer-width:${props.workspace.id}`

onMounted(() => {
  const saved = Number(window.localStorage.getItem(WIDTH_KEY))
  if (Number.isFinite(saved) && saved > 0) drawerWidth.value = clamp(saved)
})
onUnmounted(stopResize)

// The drawer pushes rather than overlays, so its width is capped to leave the
// surface rail plus a workable two-pane content area standing.
function clamp(value: number) { return Math.min(Math.max(value, MIN_WIDTH), MAX_WIDTH, Math.max(MIN_WIDTH, window.innerWidth - MIN_CONTENT_WIDTH)) }
function startResize(event: PointerEvent) { if (!agent.state.drawerOpen) return; event.preventDefault(); resizing.value = true; document.body.classList.add('agent-drawer-resizing'); window.addEventListener('pointermove', resize); window.addEventListener('pointerup', stopResize) }
function resize(event: PointerEvent) { drawerWidth.value = clamp(window.innerWidth - event.clientX) }
function stopResize() { if (!resizing.value) return; resizing.value = false; document.body.classList.remove('agent-drawer-resizing'); window.removeEventListener('pointermove', resize); window.removeEventListener('pointerup', stopResize); window.localStorage.setItem(WIDTH_KEY, String(drawerWidth.value)) }
</script>

<template>
  <aside class="agent-drawer" :class="{ collapsed: !agent.state.drawerOpen, resizing }" :style="agent.state.drawerOpen ? { flexBasis: `${drawerWidth}px`, width: `${drawerWidth}px` } : undefined">
    <!-- The entire collapsed rail expands the assistant, not just the icon. -->
    <button v-if="!agent.state.drawerOpen" class="collapsed-toggle" aria-label="Expand audit assistant" @click="agent.toggleDrawer()">
      <i class="pi pi-sparkles" />
    </button>

    <template v-else>
      <div class="resize-handle" role="separator" aria-label="Resize audit assistant" @pointerdown="startResize" />
      <ConsoleThread :workspace="workspace">
        <template #head-actions>
          <Button icon="pi pi-list-check" text size="small" severity="secondary" aria-label="Show the plan" v-tooltip.bottom="'Plan'" @click="planOpen = true" />
          <Button icon="pi pi-angle-right" text size="small" severity="secondary" aria-label="Collapse assistant" @click="agent.toggleDrawer()" />
        </template>
      </ConsoleThread>
      <Dialog v-model:visible="planOpen" modal header="Plan" :style="{ width: 'min(92vw, 28rem)' }">
        <PlanSpine :workspaceId="workspace.id" overlay />
      </Dialog>
    </template>
  </aside>
</template>

<style scoped>
/* The drawer is always a real layout column: it reserves its own width and
   pushes the surface content instead of covering it. Overlaying it hid the
   right edge of every master/detail surface at ordinary laptop widths. */
.agent-drawer{position:relative;flex:0 0 auto;display:flex;flex-direction:column;min-width:0;overflow:hidden;border-left:1px solid var(--aw-border);background:var(--p-surface-0)}
.agent-drawer.collapsed{flex:0 0 3.25rem}
.agent-drawer.resizing{transition:none;user-select:none}
:global(body.agent-drawer-resizing){cursor:col-resize;user-select:none}
.resize-handle{position:absolute;z-index:7;inset:0 auto 0 -.3rem;width:.6rem;cursor:col-resize}
.collapsed-toggle{flex:1;display:grid;place-items:center;width:100%;border:0;background:transparent;color:var(--aw-teal);font-size:1.05rem;cursor:pointer}
.collapsed-toggle:hover{background:var(--aw-teal-soft)}
</style>
