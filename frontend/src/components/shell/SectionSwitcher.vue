<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  BENCH_SECTIONS, FILE_SECTIONS, destinationForSection, destinationLabel, workspaceRoute,
} from '../../composables/useWorkspaceNavigation'
import type { WorkspaceDestination } from '../../composables/useWorkspaceNavigation'

/**
 * Where else you can go in this engagement, under the engagement crumb.
 *
 * Every work product is reached from a row of the engagement record, which is
 * the right first answer — the record says what each one cost and what it left
 * open, which no menu can. But moving between two of them meant going back to
 * the record and picking again, and the surface rails that used to make that a
 * single click were removed with the rails. This is what replaced them: one
 * control, in the same place on every page, that names every view in the
 * engagement and marks the one you are on.
 *
 * It is grouped as the record groups them — the record itself, then the work
 * products in the order the file holds them, then the sources and the bench —
 * so the menu and the page it opens from list the same things in the same
 * order.
 */

const props = defineProps<{ workspaceId: string }>()
const emit = defineEmits<{ close: [] }>()

const route = useRoute()

/** An icon per view, taken from what the work product *is* — as the record does. */
const ICONS: Partial<Record<WorkspaceDestination, string>> = {
  record: 'pi pi-book',
  apm: 'pi pi-map',
  cycle: 'pi pi-sitemap',
  rcm: 'pi pi-table',
  'data-tests': 'pi pi-shield',
  'doc-tests': 'pi pi-verified',
  findings: 'pi pi-flag',
  chain: 'pi pi-share-alt',
  report: 'pi pi-file-edit',
  documents: 'pi pi-folder-open',
  data: 'pi pi-database',
  query: 'pi pi-search',
  analysis: 'pi pi-chart-bar',
}

interface Entry {
  destination: WorkspaceDestination
  label: string
  icon: string
  path: string
}

function entry(destination: WorkspaceDestination): Entry {
  const target = workspaceRoute(props.workspaceId, destination) as { path: string }
  return {
    destination,
    label: destinationLabel(destination),
    icon: ICONS[destination] ?? 'pi pi-circle',
    path: target.path,
  }
}

function entriesFor(sections: readonly string[], surface: 'file' | 'bench'): Entry[] {
  return sections
    .map(section => destinationForSection(surface, section))
    .filter((value): value is WorkspaceDestination => value !== null)
    .map(entry)
}

const groups = computed(() => [
  { key: 'record', label: '', entries: [entry('record')] },
  { key: 'file', label: 'Work products', entries: entriesFor(FILE_SECTIONS, 'file') },
  { key: 'bench', label: 'Sources and bench', entries: entriesFor(BENCH_SECTIONS, 'bench') },
])

/**
 * The page you are on, by path rather than by route name: a row page lives
 * under the matrix's own section, so the matrix is what its own trail names
 * and what this should mark.
 */
function isCurrent(item: Entry): boolean {
  if (item.destination === 'record') return route.path === item.path
  return route.path === item.path || route.path.startsWith(`${item.path}/`)
}
</script>

<template>
  <div class="switcher" role="menu" aria-label="Go to">
    <template v-for="group in groups" :key="group.key">
      <p v-if="group.label" class="switcher__eyebrow">{{ group.label }}</p>
      <RouterLink
        v-for="item in group.entries"
        :key="item.destination"
        :to="item.path"
        class="switcher__row"
        :class="{ 'switcher__row--current': isCurrent(item) }"
        :aria-current="isCurrent(item) ? 'page' : undefined"
        role="menuitem"
        @click="emit('close')"
      >
        <i :class="item.icon" aria-hidden="true" />
        <span class="switcher__name">{{ item.label }}</span>
      </RouterLink>
      <hr v-if="group.key !== 'bench'" class="switcher__rule">
    </template>
  </div>
</template>

<style scoped>
.switcher {
  width: 15.5rem;
  padding: 0.375rem;
  border-radius: var(--aw-radius-surface);
  background: var(--aw-panel);
  box-shadow: var(--aw-shadow-md);
  /* A popover floats, so it takes a shadow rather than a border — except that
     on a dark ground a shadow alone leaves no edge, which is what this is. */
  border: 1px solid var(--aw-border);
}
.switcher__eyebrow {
  margin: 0.35rem 0.6rem 0.2rem;
  color: var(--aw-muted);
  font-size: var(--aw-text-2xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.switcher__row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  width: 100%;
  padding: 0.35rem 0.6rem;
  border-radius: var(--aw-radius-control);
  color: var(--aw-ink);
  font-size: var(--aw-text-sm);
  text-decoration: none;
}
.switcher__row:hover { background: var(--aw-raised); }
.switcher__row > i { color: var(--aw-ink-soft); font-size: var(--aw-text-sm); }
.switcher__name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.switcher__row--current {
  background: var(--aw-teal-soft);
  color: var(--aw-teal);
  font-weight: 600;
}
.switcher__row--current > i { color: var(--aw-teal); }
.switcher__row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.switcher__rule { height: 1px; margin: 0.3rem 0; border: 0; background: var(--aw-border); }
</style>
