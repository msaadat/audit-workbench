<script setup lang="ts">
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'
</script>

<template>
  <header class="app-header">
    <router-link to="/" class="brand">
      <i class="pi pi-verified" />
      <span>Audit Workbench</span>
    </router-link>
    <span class="tagline">Local-first analysis for auditors</span>
  </header>
  <main>
    <router-view />
  </main>
  <Toast position="bottom-right" />
  <ConfirmDialog />
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: baseline;
  gap: 1rem;
  padding: 0.85rem 1.5rem;
  background: var(--p-surface-0);
  border-bottom: 1px solid var(--p-surface-200);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--p-primary-600);
  text-decoration: none;
}

.tagline {
  font-size: 0.8rem;
  color: var(--p-surface-500);
}
</style>
