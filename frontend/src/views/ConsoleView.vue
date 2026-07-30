<script setup lang="ts">
import { inject, ref } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

import ConsoleThread from '../components/agent/ConsoleThread.vue'
import EngagementState from '../components/agent/EngagementState.vue'
import PlanSpine from '../components/agent/PlanSpine.vue'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace landing surface: what the agent plans to do, what it is saying,
 * and where the engagement stands — left to right.
 */

const { workspace, phases } = inject(workspaceContextKey)!
const planOpen = ref(false)
</script>

<template>
  <div class="console-body">
    <PlanSpine :workspaceId="workspace.id" />
    <ConsoleThread :workspace="workspace">
      <template #head-actions>
        <Button class="mobile-plan" label="Plan" icon="pi pi-list-check" text size="small" severity="secondary" @click="planOpen = true" />
      </template>
    </ConsoleThread>
    <EngagementState :workspace="workspace" :phases="phases" />
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
.mobile-plan{display:none}
@container console-body (max-width: 52rem) {
  .mobile-plan{display:inline-flex}
}
</style>
