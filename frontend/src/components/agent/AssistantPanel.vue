<script setup lang="ts">
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import Popover from 'primevue/popover'

import { affordableMode, useAgentRun } from '../../composables/useAgentRun'
import { useAssistantChat } from '../../composables/useAssistantChat'
import { workspaceContextKey } from '../../composables/useWorkspaceContext'
import type { WorkspaceSummary } from '../../types'
import ChatList from './ChatList.vue'
import ConsoleThread from './ConsoleThread.vue'
import EngagementState from './EngagementState.vue'
import PlanSpine from './PlanSpine.vue'
import PlanStrip from './PlanStrip.vue'
import RunContextCard from './RunContextCard.vue'
import type { AssistantContextProjection, AssistantRunProjection } from '../../types'

/**
 * One assistant, in three widths.
 *
 * It was two: a route (`/console`) and a sidecar (`AgentDrawer`), showing the
 * same thread with different chrome — docked chats against a dropdown, a plan
 * rail against a plan modal, and a status rail no other surface had. The header
 * called the assistant a *place* while the drawer made it a *state* of every
 * other place, and pressing `Assistant` with the drawer open showed you what
 * you were already looking at, wider.
 *
 * Width is the only thing the route provided that the sidecar could not, and
 * width is a property a panel can have. So: closed, docked beside the page, or
 * expanded over it — with the page still mounted underneath, which is what
 * `Esc` returns you to and what the console could never offer, since leaving it
 * was a navigation.
 */

const props = defineProps<{ workspace: WorkspaceSummary }>()
const route = useRoute()
const agent = useAgentRun(props.workspace.id)
const chats = useAssistantChat(props.workspace.id)
const shell = inject(workspaceContextKey, undefined)

const threadRef = ref<InstanceType<typeof ConsoleThread> | null>(null)
const chatMenu = ref<InstanceType<typeof Popover> | null>(null)
const width = ref(440)
const resizing = ref(false)

const MIN_WIDTH = 360
const MAX_WIDTH = 720
/** A surface rail plus a two-pane detail layout that still reads. */
const MIN_CONTENT_WIDTH = 880
const WIDTH_KEY = `audit-workbench:assistant-width:${props.workspace.id}`

const mode = computed(() => agent.state.panelMode)
const expanded = computed(() => mode.value === 'expanded')

onMounted(() => {
  // Guarded like the write below: storage is unavailable in hardened and
  // private browsing contexts, and a remembered width is not worth failing to
  // mount over.
  try {
    const saved = Number(window.localStorage?.getItem(WIDTH_KEY))
    if (Number.isFinite(saved) && saved > 0) width.value = clamp(saved)
  } catch { /* the default width stands */ }
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  stopResize()
  window.removeEventListener('keydown', onKey)
})

/**
 * `Esc` in the expanded panel goes back to docked, which is the page you were
 * on with the conversation still open. In docked it does nothing — the
 * composer owns the key there.
 */
function onKey(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !expanded.value) return
  event.preventDefault()
  agent.setPanelMode('docked')
}

/**
 * A link out of the transcript docks the panel, because otherwise it does
 * nothing a reader can see: the expanded panel covers the workspace area, so
 * the page underneath changed and the reader was still looking at the chat.
 * Docking is the answer rather than closing — the conversation that sent you
 * there is usually the reason you are reading the page.
 *
 * The panel's own query keys are exempt: `WorkspaceView` strips `?assistant`
 * and `?chat` once it has applied them, and that replace must not read as a
 * navigation.
 */
function isPanelQueryStrip(previous: string, next: string): boolean {
  const url = new URL(previous, 'http://x')
  url.searchParams.delete('assistant')
  url.searchParams.delete('chat')
  return `${url.pathname}${url.search}` === next
}
watch(() => route.fullPath, (next, previous) => {
  if (!expanded.value || !previous || isPanelQueryStrip(previous, next)) return
  // Below the docking width there is no third state to fall back to: docking
  // would be answered with expansion, and the link would once again appear to
  // do nothing. There, following a link closes the panel.
  agent.setPanelMode(affordableMode('docked') === 'docked' ? 'docked' : 'closed')
})

function clamp(value: number) {
  return Math.min(
    Math.max(value, MIN_WIDTH), MAX_WIDTH,
    Math.max(MIN_WIDTH, window.innerWidth - MIN_CONTENT_WIDTH),
  )
}
function startResize(event: PointerEvent) {
  if (expanded.value) return
  event.preventDefault()
  resizing.value = true
  document.body.classList.add('agent-drawer-resizing')
  window.addEventListener('pointermove', resize)
  window.addEventListener('pointerup', stopResize)
}
function resize(event: PointerEvent) { width.value = clamp(window.innerWidth - event.clientX) }
function stopResize() {
  if (!resizing.value) return
  resizing.value = false
  document.body.classList.remove('agent-drawer-resizing')
  window.removeEventListener('pointermove', resize)
  window.removeEventListener('pointerup', stopResize)
  try { window.localStorage?.setItem(WIDTH_KEY, String(width.value)) } catch { /* private mode */ }
}

function pickChat(id: string) {
  void chats.switchChat(id)
  chatMenu.value?.hide()
}

/**
 * The run the rail describes: the one still working, else the newest. Resolved
 * from the open conversation rather than from the agent store, which holds one
 * workspace-wide run and would put another chat's plan beside this transcript.
 */
const ACTIVE = new Set([
  'queued', 'interpreting', 'executing', 'awaiting_approval',
  'awaiting_input', 'verifying', 'paused', 'interrupted',
])
const watched = computed<AssistantRunProjection | null>(() => {
  const runs = chats.state.chat?.runs ?? []
  return runs.find(item => ACTIVE.has(item.status)) ?? runs[0] ?? null
})
/**
 * What was read, and for what. The transcript carries this as a card the reader
 * scrolls past once; while a run is working it is a live question, so the rail
 * keeps the latest one.
 *
 * Preferring the watched run's own context matters: a conversation holds
 * several runs, and labelling an older run's manifest "this run" would be a
 * lie about which decision rests on which documents. Where the watched run
 * read nothing of its own, the card names the step the manifest belongs to
 * instead of claiming the run.
 */
const contextRead = computed(() => {
  const items = (chats.state.chat?.transcript ?? [])
    .filter((item): item is AssistantContextProjection => item.type === 'context')
  if (!items.length) return null
  const mine = items.filter(item => item.run_id === watched.value?.run_id).at(-1)
  if (mine) return { context: mine.context, label: 'Read for this run' }
  const latest = items.at(-1)!
  return {
    context: latest.context,
    label: latest.context.stage_title ? `Read for ${latest.context.stage_title.toLowerCase()}` : 'Read earlier',
  }
})
</script>

<template>
  <aside
    v-if="mode !== 'closed'"
    class="assistant-panel"
    :class="{ expanded, resizing }"
    :style="expanded ? undefined : { flexBasis: `${width}px`, width: `${width}px` }"
  >
    <div v-if="!expanded" class="resize-handle" role="separator" aria-label="Resize the assistant" @pointerdown="startResize" />

    <!-- Expanded: chats on the left, the thread in the middle, and what the
         engagement and the run stand at on the right. -->
    <div v-if="expanded" class="chats-column">
      <ChatList
        searchable
        :chats="chats.state.summaries"
        :activeId="chats.state.activeChatId"
        @select="pickChat"
        @create="chats.createChat()"
        @rename="threadRef?.rename()"
        @remove="threadRef?.remove()"
      />
    </div>

    <div class="thread-column">
      <PlanStrip v-if="!expanded" :workspaceId="workspace.id" />
      <ConsoleThread ref="threadRef" :workspace="workspace">
        <template #head-actions>
          <Button
            icon="pi pi-comments"
            text
            size="small"
            severity="secondary"
            aria-label="Chats"
            v-tooltip.bottom="'Chats'"
            @click="chatMenu?.toggle($event)"
          />
          <Button
            :icon="expanded ? 'pi pi-window-minimize' : 'pi pi-window-maximize'"
            text
            size="small"
            severity="secondary"
            :aria-label="expanded ? 'Dock the assistant' : 'Expand the assistant'"
            v-tooltip.bottom="expanded ? 'Dock' : 'Expand'"
            @click="agent.setPanelMode(expanded ? 'docked' : 'expanded')"
          />
          <Button
            icon="pi pi-times"
            text
            size="small"
            severity="secondary"
            aria-label="Close the assistant"
            @click="agent.togglePanel()"
          />
        </template>
      </ConsoleThread>
    </div>

    <!-- Three cards, in the order the questions are asked: where the
         engagement stands, what this run is doing, and what it read to do it. -->
    <aside v-if="expanded" class="rail-column">
      <!-- One card, not the record again: the record page is the index, and it
           is one click from here. -->
      <EngagementState
        v-if="shell"
        compact
        :phases="shell.phases.value"
        :sections="shell.sectionById.value"
      />
      <PlanSpine :workspaceId="workspace.id" card />
      <RunContextCard v-if="contextRead" :context="contextRead.context" :label="contextRead.label" />
    </aside>

    <Popover ref="chatMenu">
      <div class="chat-popover">
        <ChatList
          :chats="chats.state.summaries"
          :activeId="chats.state.activeChatId"
          @select="pickChat"
          @create="chats.createChat()"
          @rename="threadRef?.rename()"
          @remove="threadRef?.remove()"
        />
      </div>
    </Popover>
  </aside>
</template>

<style scoped>
/* Docked, the panel is a real layout column: it reserves its width and pushes
   the page rather than covering it. Overlaying hid the right edge of every
   master/detail surface at ordinary laptop widths. */
.assistant-panel {
  position: relative;
  display: flex; flex-direction: column;
  flex: 0 0 auto; min-width: 0;
  overflow: hidden;
  border-left: 1px solid var(--aw-border); background: var(--aw-panel);
}
.assistant-panel.resizing { transition: none; user-select: none; }
:global(body.agent-drawer-resizing) { cursor: col-resize; user-select: none; }
.resize-handle { position: absolute; z-index: 7; inset: 0 auto 0 -.3rem; width: .6rem; cursor: col-resize; }

/* Expanded, it takes the workspace area — and the page stays mounted beneath,
   so `Esc` is a return rather than a navigation. */
.assistant-panel.expanded {
  position: absolute; inset: 0; z-index: 8;
  display: grid; grid-template-columns: 16.5rem minmax(0, 1fr) 20rem;
  border-left: 0;
}
.chats-column { display: flex; flex-direction: column; min-width: 0; min-height: 0; border-right: 1px solid var(--aw-border); background: var(--aw-panel); }
.thread-column { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
.rail-column {
  display: flex; flex-direction: column; gap: .875rem;
  min-width: 0; min-height: 0; overflow-y: auto;
  padding: .875rem;
  border-left: 1px solid var(--aw-border); background: var(--aw-raised);
}
/* Each answer is a card, so the column reads as three questions rather than as
   one long strip of headings. */
.rail-column > :deep(*) {
  display: flex; flex-direction: column; gap: .3rem;
  min-width: 0; flex: none;
  padding: .625rem .75rem;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
}
.rail-column :deep(header) {
  display: flex; align-items: baseline; justify-content: space-between; gap: .5rem;
  padding-bottom: .35rem; border-bottom: 1px solid var(--aw-border);
}
.rail-column :deep(header h3) { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rail-column :deep(.count) { flex: none; color: var(--aw-muted); font-size: var(--aw-text-2xs); }
.rail-card :deep(.plan-spine.card) { padding: 0; }
.chat-popover { width: min(20rem, 80vw); max-height: 24rem; display: flex; }
.chat-popover .chat-list { flex: 1; min-height: 0; }

@container workspace-panel (max-width: 60rem) {
  .assistant-panel.expanded { grid-template-columns: minmax(0, 1fr); }
  .chats-column, .rail-column { display: none; }
}
</style>
