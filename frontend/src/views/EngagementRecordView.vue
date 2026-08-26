<script setup lang="ts">
import { inject } from 'vue'

import { workspaceContextKey } from '../composables/useWorkspaceContext'
import EngagementRecordTab from '../components/EngagementRecordTab.vue'

/**
 * The workspace landing surface.
 *
 * It used to be the chat, whose most prominent element was a list of
 * auto-titled conversations — twenty-seven of them on the demo engagement,
 * twenty-four repeating the last. That answered "what have I asked for", which
 * is not the question anyone arrives with. The record answers "what was done
 * here, from what, and what did it cost", and the chat is still one click away
 * in the sidecar drawer that every other surface already carries.
 *
 * No rail: this surface has one thing on it.
 */

const { workspace } = inject(workspaceContextKey)!
</script>

<template>
  <div class="ui-surface">
    <div class="ui-surface__panel">
      <EngagementRecordTab :workspace="workspace" />
    </div>
  </div>
</template>
