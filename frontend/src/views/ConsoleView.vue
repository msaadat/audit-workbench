<script setup lang="ts">
import { inject } from 'vue'

import ConsoleThread from '../components/agent/ConsoleThread.vue'
import EngagementState from '../components/agent/EngagementState.vue'
import PlanSpine from '../components/agent/PlanSpine.vue'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace landing surface: what the agent plans to do, what it is saying,
 * and where the engagement stands — left to right.
 */

const { workspace, phases } = inject(workspaceContextKey)!
</script>

<template>
  <div class="console-body">
    <PlanSpine :workspaceId="workspace.id" />
    <ConsoleThread :workspace="workspace" />
    <EngagementState :workspace="workspace" :phases="phases" />
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
</style>
