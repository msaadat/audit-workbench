<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Toast from 'primevue/toast'
import ConfirmDialog from 'primevue/confirmdialog'

import UiOverflowMenu from './components/ui/UiOverflowMenu.vue'
import { useSession } from './composables/useSession'

const route = useRoute()
const router = useRouter()
const session = useSession()

// Single-user installations have nobody to switch to, so the account control
// stays hidden and the local-first product looks exactly as it did.
const showAccount = computed(() => !session.state.singleUser && session.state.user !== null)
const accountLabel = computed(
  () => session.state.user?.display_name || session.state.user?.email || 'Account',
)
const accountActions = computed(() => [
  { label: session.state.user?.email ?? '', disabled: true },
  { separator: true },
  {
    label: 'Sign out',
    icon: 'pi pi-sign-out',
    command: async () => {
      await session.signOut()
      await router.replace({ name: 'login' })
    },
  },
])
// Every workspace surface brings its own engagement header; the debug console
// deliberately keeps the global one.
const WORKSPACE_ROUTES = [
  'workspace', 'workspace-console', 'workspace-file', 'workspace-bench',
]
const inWorkspace = computed(() => WORKSPACE_ROUTES.includes(String(route.name ?? '')))
</script>

<template>
  <header v-if="!inWorkspace" class="app-header">
    <router-link to="/" class="brand">
      <span class="brand-mark"><i class="pi pi-verified" /></span>
      <strong>Audit Workbench</strong>
    </router-link>
    <span class="header-spacer" />
    <a href="/about.html" class="about-link">
      <i class="pi pi-info-circle" /> About
    </a>
    <UiOverflowMenu
      v-if="showAccount"
      :items="accountActions"
      :label="accountLabel"
      tooltip="Account"
    />
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
  padding: 0.45rem 1.5rem;
  background: linear-gradient(180deg, var(--aw-navy-900) 0%, var(--aw-navy-950) 100%);
  box-shadow: var(--aw-shadow-sm);
  color: var(--aw-on-dark);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.7rem;
  color: var(--aw-on-dark);
  text-decoration: none;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-navy-950);
  background: linear-gradient(135deg, var(--aw-mint) 0%, var(--aw-mint-600) 100%);
  box-shadow: 0 0 0 1px rgb(94 234 212 / 25%), 0 2px 8px rgb(45 212 191 / 30%);
  font-size: var(--aw-text-md);
}

.brand strong { font-size: var(--aw-text-md); font-weight: 700; letter-spacing: -0.01em; }
.header-spacer { flex: 1; }

.about-link {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: var(--aw-text-sm);
}
.about-link { color: var(--aw-on-navy); text-decoration: none; padding: 0.35rem 0.6rem; border-radius: var(--aw-radius-control); transition: background .15s; }
.about-link:hover { background: rgb(255 255 255 / 10%); }

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
  .about-link { font-size: 0; }
  .about-link i { font-size: var(--aw-text-md); }
}
</style>
