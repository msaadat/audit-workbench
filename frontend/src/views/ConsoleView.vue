<script setup lang="ts">
import { inject, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import { useToast } from 'primevue/usetoast'

import { api } from '../api'
import ChatHistoryPanel from '../components/agent/ChatHistoryPanel.vue'
import ConsoleThread from '../components/agent/ConsoleThread.vue'
import EngagementState from '../components/agent/EngagementState.vue'
import type { PhaseAction } from '../components/agent/engagementStatus'
import PlanSpine from '../components/agent/PlanSpine.vue'
import { plural } from '../format'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace landing surface: the chat list, what the agent is saying, and
 * where the engagement stands — left to right. Plan sits under Progress in the
 * right rail rather than beside the thread, since it is read far less often
 * than either.
 */

const { workspace, phases, sectionById, reload, requestImport } = inject(workspaceContextKey)!
const chats = useAssistantChat(workspace.value.id)
const toast = useToast()
const planOpen = ref(false)
const historyOpen = ref(false)
const threadRef = ref<InstanceType<typeof ConsoleThread> | null>(null)
const running = ref(false)

/**
 * The rail only offers work it can finish here. Running data tests is
 * deterministic and synchronous, so the console can do it without handing the
 * reader to another surface; anything needing the assistant or per-row context
 * stays a link to the page whose bar owns it.
 */
async function runStatusAction(action: PhaseAction) {
  if (action.key === 'import') return requestImport()
  running.value = true
  try {
    const batch = await api.post<{
      total: number
      failed: Array<{ data_test_id: string; error: string }>
    }>(`/api/workspaces/${workspace.value.id}/data-tests/run-all`, { test_ids: action.ids ?? [] })
    // Counts, conclusions and phase gates all move on a run, so the rail has to
    // come back from the server rather than guess at its own new state.
    await reload()
    toast.add({
      severity: batch.failed.length ? 'warn' : 'success',
      summary: `Ran ${batch.total - batch.failed.length} of ${plural(batch.total, 'test')}`,
      detail: batch.failed.length
        ? `${plural(batch.failed.length, 'test')} could not run.`
        : undefined,
      life: 6000,
    })
  } catch (error) {
    toast.add({
      severity: 'error',
      summary: 'Could not run the tests',
      detail: error instanceof Error ? error.message : String(error),
      life: 8000,
    })
  } finally { running.value = false }
}
</script>

<template>
  <div class="console-body">
    <aside class="chat-rail">
      <ChatHistoryPanel
        docked
        :chats="chats.state.summaries"
        :activeId="chats.state.activeChatId"
        @select="chats.switchChat($event)"
        @create="chats.createChat()"
        @rename="threadRef?.rename()"
        @remove="threadRef?.remove()"
      />
    </aside>
    <ConsoleThread ref="threadRef" :workspace="workspace" dockedHistory>
      <template #head-actions>
        <Button class="mobile-toggle" label="Chats" icon="pi pi-comments" text size="small" severity="secondary" @click="historyOpen = true" />
        <Button class="mobile-toggle" label="Plan" icon="pi pi-list-check" text size="small" severity="secondary" @click="planOpen = true" />
      </template>
    </ConsoleThread>
    <aside class="right-rail">
      <EngagementState
        :phases="phases"
        :sections="sectionById"
        :busy="running"
        @action="runStatusAction"
      />
      <PlanSpine :workspaceId="workspace.id" />
    </aside>
    <Dialog v-model:visible="historyOpen" modal header="Chats" :style="{ width: 'min(92vw, 24rem)' }">
      <ChatHistoryPanel
        docked
        :chats="chats.state.summaries"
        :activeId="chats.state.activeChatId"
        @select="chats.switchChat($event); historyOpen = false"
        @create="chats.createChat(); historyOpen = false"
        @rename="threadRef?.rename(); historyOpen = false"
        @remove="threadRef?.remove(); historyOpen = false"
      />
    </Dialog>
    <Dialog v-model:visible="planOpen" modal header="Plan" :style="{ width: 'min(92vw, 28rem)' }">
      <PlanSpine :workspaceId="workspace.id" overlay />
    </Dialog>
  </div>
</template>

<style scoped>
.console-body {
  container: console-body / inline-size;
  flex: 1;
  display: flex;
  align-items: stretch;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--aw-canvas);
}
.chat-rail {
  flex: 0 0 16.5rem;
  min-height: 0;
  overflow-y: auto;
  padding: 0.9rem 0.7rem;
  border-right: 1px solid var(--aw-border);
  background: var(--aw-raised);
}
.right-rail {
  flex: 0 0 17rem;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
  padding: 0.9rem 0.8rem;
  border-left: 1px solid var(--aw-border);
  background: var(--aw-raised);
}
.mobile-toggle{display:none}
@container console-body (max-width: 52rem) {
  .chat-rail, .right-rail { display: none; }
  .mobile-toggle{display:inline-flex}
}
</style>
