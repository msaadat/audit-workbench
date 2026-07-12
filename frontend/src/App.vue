<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'

const route = useRoute()
const inWorkspace = computed(() => route.name === 'workspace')
</script>

<template>
  <header class="app-header">
    <router-link to="/" class="brand">
      <span class="brand-mark"><i class="pi pi-verified" /></span>
      <span class="brand-copy">
        <strong>Audit Workbench</strong>
        <small>Local analysis console</small>
      </span>
    </router-link>
    <span class="header-spacer" />
    <div class="local-status" title="Workspace data is processed on this device">
      <span class="status-dot" />
      <span>Local-only processing</span>
    </div>
    <router-link v-if="inWorkspace" to="/" class="all-workspaces">
      <i class="pi pi-th-large" /> All workspaces
    </router-link>
  </header>
  <main :class="{ 'workspace-main': inWorkspace }">
    <router-view />
  </main>
  <Toast position="bottom-right" />
  <ConfirmDialog />
</template>

<style scoped>
.app-header {
  flex: 0 0 3.5rem;
  display: flex;
  align-items: center;
  gap: 0.85rem;
  min-height: 3.5rem;
  padding: 0.45rem 1.25rem;
  background: #10233f;
  border-bottom: 1px solid #203a5d;
  color: #fff;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.7rem;
  color: #fff;
  text-decoration: none;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border-radius: 6px;
  color: #10233f;
  background: #5eead4;
  font-size: 1rem;
}

.brand-copy { display: flex; flex-direction: column; line-height: 1.15; }
.brand-copy strong { font-size: 0.96rem; letter-spacing: 0.01em; }
.brand-copy small { margin-top: 0.12rem; color: #a9b9ce; font-size: 0.66rem; font-weight: 500; }
.header-spacer { flex: 1; }

.local-status,
.all-workspaces {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.75rem;
}
.local-status { color: #c7d3e2; }
.status-dot { width: 0.45rem; height: 0.45rem; border-radius: 50%; background: #5eead4; box-shadow: 0 0 0 3px rgb(94 234 212 / 12%); }
.all-workspaces { color: #e6edf6; text-decoration: none; padding: 0.35rem 0.55rem; border-radius: 6px; }
.all-workspaces:hover { background: rgb(255 255 255 / 8%); }

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

@media (max-width: 640px) {
  .app-header { padding-inline: 1rem; }
  .local-status span:last-child, .all-workspaces { font-size: 0; }
  .all-workspaces i { font-size: 1rem; }
}
</style>
