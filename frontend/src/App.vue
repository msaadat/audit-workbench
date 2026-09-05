<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'

import AppHeader from './components/shell/AppHeader.vue'

/**
 * The application shell: one bar, then whatever the route renders.
 *
 * There were two headers here — this one, hidden on an allowlist of four route
 * names, and a second inside `WorkspaceView` with the same brand mark and the
 * same rules copied. The allowlist had to be kept in step with the router by
 * hand and the two disagreed about what belonged in a header. `AppHeader` is
 * the only one now, on every route including the diagnostics view.
 */

const route = useRoute()

/**
 * Workspace surfaces manage their own scrolling, so their `main` is bounded by
 * the window rather than growing with content. Keyed on the path rather than
 * on a list of route names: every workspace route lives under this prefix, so
 * nothing has to be kept in step with `router.ts`.
 */
const inWorkspace = computed(() => route.path.startsWith('/workspace/'))
</script>

<template>
  <AppHeader />
  <main :class="{ 'workspace-main': inWorkspace }">
    <router-view />
  </main>
  <Toast position="bottom-right" />
  <ConfirmDialog />
</template>

<style scoped>
main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.workspace-main {
  height: 0;
  overflow: hidden;
}
</style>
