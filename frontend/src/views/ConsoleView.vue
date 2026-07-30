<script setup lang="ts">
import { inject } from 'vue'

import ConsoleThread from '../components/agent/ConsoleThread.vue'
import EngagementState from '../components/agent/EngagementState.vue'
import { workspaceContextKey } from '../composables/useWorkspaceContext'

/**
 * The workspace landing surface: the agent, at full width, with the engagement
 * state beside it. The plan spine takes the left column in phase 2 of
 * docs/agentic-ux-plan.md; until it can render the real capability graph the
 * console is a two-column layout rather than a placeholder three.
 */

const { workspace, phases } = inject(workspaceContextKey)!
</script>

<template>
  <div class="console-body">
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
