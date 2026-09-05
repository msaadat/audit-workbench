<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import Menu from 'primevue/menu'
import type { MenuItem } from 'primevue/menuitem'

import SectionSwitcher from './SectionSwitcher.vue'
import { useAppearance } from '../../composables/useAppearance'
import { useSession } from '../../composables/useSession'
import { useShell } from '../../composables/useShell'

/**
 * One bar, on every page.
 *
 * It replaces two headers and three crumb bars. The two headers said the same
 * things in different words — the index offered an account menu and `About`,
 * the workspace a sign-out icon and six more — and had to be kept apart by a
 * route allowlist in `App.vue` that nothing enforced. The crumb bar under them
 * spent a whole row on one link and one label.
 *
 * The trail is the whole of it: the brand mark reaches the index, the
 * engagement's name reaches its record, and the piece after it says which work
 * product you are reading. That is what the second header and the crumb bar
 * were both for, in 44 px rather than 99.
 *
 * The chevron beside the engagement's name opens the one navigation control in
 * the app: every view in the engagement, with the one you are on marked. The
 * surface rails that used to do that were removed when the record became the
 * index, which left moving between two work products a trip back to the record
 * — right the first time you open one, wrong the fifth.
 *
 * The right cluster holds one labelled button and a kebab. The eight icons it
 * had are things nobody does twice in a sitting — theme, size, diagnostics,
 * about, the index, sign out — and each of them cost exactly as much room as
 * `Assistant`, which is used constantly. They are in the kebab. `Assistant`
 * itself is not drawn here at all: it belongs to the engagement, so the
 * workspace shell teleports it in with its own state.
 */

const route = useRoute()
const router = useRouter()
const session = useSession()
const { trail, engagement } = useShell()
const { theme, presenting, cycleTheme } = useAppearance()

const switcherOpen = ref(false)
const engagementWrap = ref<HTMLElement | null>(null)
const kebab = ref<InstanceType<typeof Menu> | null>(null)

/** Single-user installations have nobody to switch to, so the account rows go. */
const showAccount = computed(() => !session.state.singleUser && session.state.user !== null)

const THEME_LABEL = { system: 'System', light: 'Light', dark: 'Dark' } as const
const THEME_ICON = {
  system: 'pi pi-desktop',
  light: 'pi pi-sun',
  dark: 'pi pi-moon',
} as const

/**
 * Everything the header used to spell out in icons, in one menu.
 *
 * `Theme` cycles on click rather than opening three radio rows: it is one row
 * either way, and the label already says which of the three it is on.
 */
const kebabItems = computed<MenuItem[]>(() => {
  const current = engagement.value
  const items: MenuItem[] = [
    {
      label: 'Presentation size',
      icon: 'pi pi-search-plus',
      class: presenting.value ? 'shell-menu-item--on' : undefined,
      command: () => { presenting.value = !presenting.value },
    },
    {
      label: `Theme · ${THEME_LABEL[theme.value]}`,
      icon: THEME_ICON[theme.value],
      command: () => cycleTheme(),
    },
    { separator: true },
  ]
  if (current) {
    items.push({
      label: 'Diagnostics',
      icon: 'pi pi-code',
      command: () => { void router.push(`/workspace/${current.id}/debug`) },
    })
  }
  items.push({ label: 'About Audit Workbench', icon: 'pi pi-info-circle', url: '/about.html' })
  if (showAccount.value) {
    items.push(
      { separator: true },
      { label: `Signed in as ${session.state.user?.email ?? ''}`, disabled: true },
      { label: 'Sign out', icon: 'pi pi-sign-out', command: () => { void signOut() } },
    )
  }
  return items
})

async function signOut() {
  await session.signOut()
  await router.replace({ name: 'login' })
}

function toggleSwitcher() {
  switcherOpen.value = !switcherOpen.value
}

/** A popover closes on the next click outside it and on Escape. */
function onDocumentPointerDown(event: PointerEvent) {
  if (!engagementWrap.value?.contains(event.target as Node)) switcherOpen.value = false
}
function onDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') switcherOpen.value = false
}

watch(switcherOpen, open => {
  if (open) {
    document.addEventListener('pointerdown', onDocumentPointerDown)
    document.addEventListener('keydown', onDocumentKeydown)
  } else {
    document.removeEventListener('pointerdown', onDocumentPointerDown)
    document.removeEventListener('keydown', onDocumentKeydown)
  }
})
// Navigating away from the page the menu was opened over closes it.
watch(() => route.fullPath, () => { switcherOpen.value = false })
onUnmounted(() => {
  document.removeEventListener('pointerdown', onDocumentPointerDown)
  document.removeEventListener('keydown', onDocumentKeydown)
})
</script>

<template>
  <header class="app-shell">
    <div class="app-shell__trail">
      <RouterLink to="/" class="app-shell__mark" aria-label="All engagements">
        <i class="pi pi-verified" aria-hidden="true" />
      </RouterLink>
      <!-- The wordmark only where there is no trail. Inside an engagement the
           engagement's name is the identity, and two of them is the 330 px the
           old header spent saying nothing about where you were. -->
      <strong v-if="!engagement" class="app-shell__wordmark">Audit Workbench</strong>

      <template v-if="engagement">
        <i class="app-shell__sep pi pi-chevron-right" aria-hidden="true" />
        <span ref="engagementWrap" class="app-shell__engagement" :class="{ open: switcherOpen }">
          <RouterLink
            :to="`/workspace/${engagement.id}`"
            class="app-shell__crumb app-shell__crumb--engagement"
            :class="{ 'app-shell__crumb--current': !trail.length }"
            :aria-current="trail.length ? undefined : 'page'"
          >{{ engagement.name }}</RouterLink>
          <button
            type="button"
            class="app-shell__switch"
            aria-label="Go to another view in this engagement"
            aria-haspopup="menu"
            :aria-expanded="switcherOpen"
            @click="toggleSwitcher"
          ><i class="pi pi-chevron-down" aria-hidden="true" /></button>

          <SectionSwitcher
            v-if="switcherOpen"
            class="app-shell__popover"
            :workspaceId="engagement.id"
            @close="switcherOpen = false"
          />
        </span>
      </template>

      <template v-for="(crumb, index) in trail" :key="index">
        <i class="app-shell__sep pi pi-chevron-right" aria-hidden="true" />
        <RouterLink
          v-if="crumb.to"
          :to="crumb.to"
          class="app-shell__crumb"
          :class="{ 'app-shell__crumb--mono': crumb.mono }"
        >{{ crumb.label }}</RouterLink>
        <span
          v-else
          class="app-shell__crumb app-shell__crumb--current"
          :class="{ 'app-shell__crumb--mono': crumb.mono }"
          aria-current="page"
        >{{ crumb.label }}</span>
      </template>
    </div>

    <div class="app-shell__actions">
      <!-- Where the workspace shell teleports its `Assistant` toggle. It is
           `display: contents`, so what lands here is a flex item of the cluster
           and takes its gap; empty, it takes no room at all. -->
      <div id="shell-actions" class="app-shell__slot" />
      <button
        type="button"
        class="app-shell__kebab"
        aria-label="More"
        aria-haspopup="true"
        @click="kebab?.toggle($event)"
      ><i class="pi pi-ellipsis-v" aria-hidden="true" /></button>
      <Menu ref="kebab" :model="kebabItems" popup />
    </div>
  </header>
</template>

<style scoped>
.app-shell {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: var(--aw-space-2);
  height: var(--aw-shell-height);
  padding: 0 var(--aw-space-4);
  background: linear-gradient(180deg, var(--aw-navy-900) 0%, var(--aw-navy-950) 100%);
  box-shadow: var(--aw-shadow-sm);
  color: var(--aw-on-dark);
}

.app-shell__trail {
  display: flex;
  align-items: center;
  gap: var(--aw-space-1);
  flex: 1;
  min-width: 0;
}

.app-shell__mark {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 1.625rem;
  height: 1.625rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-navy-950);
  background: linear-gradient(135deg, var(--aw-mint) 0%, var(--aw-mint-600) 100%);
  box-shadow: 0 0 0 1px rgb(94 234 212 / 25%);
  font-size: var(--aw-text-sm);
  text-decoration: none;
}
.app-shell__wordmark {
  margin-left: var(--aw-space-1);
  color: var(--aw-on-dark);
  font-size: var(--aw-text-base);
  font-weight: 700;
  letter-spacing: -0.01em;
  white-space: nowrap;
}

.app-shell__sep {
  flex: 0 0 auto;
  color: var(--aw-on-dark-sep);
  font-size: var(--aw-text-xs);
}

.app-shell__crumb {
  display: inline-flex;
  align-items: center;
  max-width: 22.5rem;
  padding: 0.25rem 0.5rem;
  border-radius: 6px;
  overflow: hidden;
  color: var(--aw-on-navy);
  font-size: var(--aw-text-sm);
  font-weight: 600;
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
}
a.app-shell__crumb:hover { background: var(--aw-on-dark-hover); color: var(--aw-on-dark); }
.app-shell__crumb--current { color: var(--aw-on-dark); }
.app-shell__crumb--mono { font-family: var(--aw-font-mono); font-size: var(--aw-text-xs); }

/* The engagement is a split control: the name opens the record, the chevron
   opens the switcher. Drawn as one pill so neither half hides the other. */
.app-shell__engagement {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-width: 0;
  border-radius: 6px;
}
.app-shell__engagement.open { background: rgb(255 255 255 / 12%); }
.app-shell__crumb--engagement { max-width: 17.5rem; padding-right: 0.25rem; border-radius: 6px 0 0 6px; }
.app-shell__switch {
  display: inline-flex;
  align-items: center;
  padding: 0.375rem 0.375rem 0.375rem 0.125rem;
  border: 0;
  border-radius: 0 6px 6px 0;
  background: transparent;
  color: var(--aw-on-navy-muted);
  cursor: pointer;
  font-size: var(--aw-text-xs);
}
.app-shell__switch:hover { background: var(--aw-on-dark-hover); color: var(--aw-on-dark); }
.app-shell__popover { position: absolute; z-index: 1200; top: calc(100% + 0.4rem); left: 0; }

.app-shell__actions {
  display: flex;
  align-items: center;
  gap: var(--aw-space-2);
  flex: 0 0 auto;
}
.app-shell__slot { display: contents; }

.app-shell__kebab {
  display: grid;
  place-items: center;
  width: var(--aw-shell-control);
  height: var(--aw-shell-control);
  border: 1px solid var(--aw-on-dark-line);
  border-radius: var(--aw-radius-control);
  background: var(--aw-on-dark-wash);
  color: var(--aw-on-dark);
  cursor: pointer;
  font-size: var(--aw-text-sm);
}
.app-shell__kebab:hover { background: var(--aw-on-dark-hover); }

.app-shell :where(a, button):focus-visible { outline: 2px solid var(--aw-mint); outline-offset: 2px; }

/* --- the teleported button ----------------------------------------------- */
/* It is rendered by the workspace shell, which owns its state, and lands here
   as a descendant — so the bar styles it, and the shell does not have to know
   what a header looks like. */
.app-shell__actions :deep(.p-button) {
  height: var(--aw-shell-control);
  padding: 0 0.7rem;
  border-radius: var(--aw-radius-control);
  font-size: var(--aw-text-sm);
  white-space: nowrap;
}
/* The assistant is the one control the bar exists to offer, so it is filled
   rather than translucent; a ghost pill beside a filled one read as disabled.
   Its text is `--aw-on-accent` rather than white because the accent itself
   flips: dark teal on a light ground, light teal on a dark one. */
.app-shell__actions :deep(.assistant-toggle.p-button) {
  border-color: var(--aw-teal);
  background: var(--aw-teal);
  color: var(--aw-on-accent);
}
.app-shell__actions :deep(.assistant-toggle.p-button:hover) {
  border-color: var(--aw-teal-600);
  background: var(--aw-teal-600);
}
/* Open reads as pressed. */
.app-shell__actions :deep(.assistant-toggle.on.p-button) {
  border-color: var(--aw-teal-600);
  background: var(--aw-teal-600);
}
/* A run that needs the auditor is the one state worth breaking colour for. */
.app-shell__actions :deep(.assistant-toggle.attention.p-button) {
  border-color: var(--aw-warn);
  background: var(--aw-warn);
  color: var(--aw-on-accent);
}

/* --- narrow ------------------------------------------------------------- */
/* The labelled button drops to an icon and the trail truncates its middle
   piece first, its current piece last. Nothing wraps and nothing is lost: the
   label survives as the tooltip and the accessible name it already carries. */
@media (max-width: 1280px) {
  .app-shell__actions :deep(.p-button) { width: var(--aw-shell-control); padding: 0; }
  .app-shell__actions :deep(.p-button .p-button-label) { display: none; }
  .app-shell__actions :deep(.p-button .p-button-icon.p-button-icon-left) { margin: 0; }
  .app-shell__crumb--engagement { max-width: 11.25rem; }
  .app-shell__crumb { max-width: 14rem; }
}
@media (max-width: 46rem) {
  .app-shell { padding-inline: var(--aw-space-3); }
  .app-shell__crumb--engagement { max-width: 8rem; }
  .app-shell__crumb { max-width: 10rem; }
}
</style>
