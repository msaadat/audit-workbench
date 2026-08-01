<script setup lang="ts">
import { inject, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

import ChatHistoryPanel from '../components/agent/ChatHistoryPanel.vue'
import ConsoleThread from '../components/agent/ConsoleThread.vue'
import EngagementState from '../components/agent/EngagementState.vue'
import PlanSpine from '../components/agent/PlanSpine.vue'
import { useAssistantChat } from '../composables/useAssistantChat'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace landing surface: the chat list, what the agent is saying, and
 * where the engagement stands — left to right. Plan sits under Progress in the
 * right rail rather than beside the thread, since it is read far less often
 * than either.
 */

const { workspace, phases } = inject(workspaceContextKey)!
const chats = useAssistantChat(workspace.value.id)
const planOpen = ref(false)
const historyOpen = ref(false)
const threadRef = ref<InstanceType<typeof ConsoleThread> | null>(null)
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
      <EngagementState :phases="phases" />
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
