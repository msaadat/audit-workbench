<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Popover from 'primevue/popover'

import { toggleFilter } from './statusLanes'
import type { StatusFilterGroup } from './statusLanes'

/**
 * The page's whole filter vocabulary, behind one control.
 *
 * Filtering is asked while working, not on arrival, so the rows of chips that
 * used to sit permanently open above every list are in here instead. The
 * button carries the active narrowing, which is the only part worth standing
 * space when nothing is filtered.
 *
 * Axes compose and options within an axis do not, which is why the groups are
 * a shape the model declares rather than a heading the menu invents: picking
 * "Ineffective" while "Effective" is on replaces it, but picking "Agent" while
 * "Ineffective" is on narrows further.
 */

const props = defineProps<{
  groups: StatusFilterGroup[]
  active: string[]
  /** How the active narrowing reads, in the page's words. */
  activeLabel?: string
}>()
const emit = defineEmits<{ select: [string[]] }>()

const panel = ref<InstanceType<typeof Popover> | null>(null)

/**
 * An option nothing matched is a filter that can only produce an empty list.
 * The active one always survives: an option must not vanish from under the
 * click that selected it.
 */
const visible = computed(() => props.groups
  .map(group => ({
    ...group,
    options: group.options.filter(
      option => option.value > 0 || props.active.includes(option.key),
    ),
  }))
  .filter(group => group.options.length))

/** One narrowing reads as itself; several are only worth a count on a button. */
const buttonLabel = computed(() => {
  if (!props.active.length) return 'Filter'
  if (props.active.length > 1) return `${props.active.length} filters`
  return props.activeLabel || 'Filter'
})

function pick(key: string) {
  emit('select', toggleFilter(props.active, key, props.groups))
}
function clear(event: Event) {
  event.stopPropagation()
  emit('select', [])
}
</script>

<template>
  <div v-if="visible.length" class="ui-filter-menu">
    <Button
      size="small"
      outlined
      :severity="active.length ? undefined : 'secondary'"
      icon="pi pi-filter"
      :label="buttonLabel"
      :class="{ 'is-active': active.length > 0 }"
      aria-haspopup="true"
      @click="panel?.toggle($event)"
    />
    <!-- Clearing is its own control rather than a row in the menu: the menu is
         where you choose a narrowing, and having to open it to undo one is the
         slowest way back to the whole list. -->
    <button
      v-if="active.length"
      type="button"
      class="clear"
      aria-label="Clear filter"
      @click="clear"
    >
      <i class="pi pi-times" />
    </button>

    <Popover ref="panel">
      <div class="menu" role="group" aria-label="Filter the list">
        <section v-for="group in visible" :key="group.key">
          <h4>{{ group.label }}</h4>
          <button
            v-for="option in group.options"
            :key="option.key"
            type="button"
            class="row"
            :data-tone="option.tone"
            :aria-pressed="active.includes(option.key)"
            @click="pick(option.key)"
          >
            <span class="dot" aria-hidden="true" />
            <span class="label">{{ option.label }}</span>
            <span class="value aw-figure">{{ option.value }}</span>
          </button>
        </section>
      </div>
    </Popover>
  </div>
</template>

<style scoped>
.ui-filter-menu { display: inline-flex; align-items: center; gap: 0.25rem; min-width: 0; }
.ui-filter-menu :deep(.p-button) { max-width: 18rem; }
.ui-filter-menu :deep(.p-button-label) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.clear {
  display: inline-flex; align-items: center; justify-content: center;
  width: var(--aw-control-height-sm); height: var(--aw-control-height-sm);
  padding: 0; border: 1px solid var(--aw-border); border-radius: var(--aw-radius-control);
  background: var(--aw-panel); color: var(--aw-muted); cursor: pointer;
}
.clear:hover { border-color: var(--aw-border-strong); color: var(--aw-ink); }
.clear:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: 1px; }
.clear .pi { font-size: var(--aw-text-xs); }

.menu { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0 var(--aw-space-4); min-width: 12rem; max-width: 40rem; }
.menu section { display: flex; flex-direction: column; gap: 0.1rem; min-width: 0; padding: 0.2rem 0 0.5rem; }
.menu h4 {
  margin: 0 0 0.25rem; padding: 0 0.4rem;
  color: var(--aw-muted-strong); font-size: var(--aw-text-2xs); font-weight: 700;
  letter-spacing: 0.09em; text-transform: uppercase;
}

.row {
  display: grid; grid-template-columns: 0.45rem minmax(0, 1fr) auto;
  align-items: center; gap: 0.45rem;
  padding: 0.3rem 0.4rem; border: 0; border-radius: var(--aw-radius-control);
  background: none; color: var(--aw-ink-soft);
  font: inherit; font-size: var(--aw-text-sm); text-align: left; cursor: pointer;
}
.row:hover { background: var(--aw-raised); }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.row[aria-pressed='true'] { background: var(--aw-teal-soft); color: var(--aw-teal); font-weight: 600; }

.row .dot { width: 0.45rem; height: 0.45rem; border-radius: var(--aw-radius-pill); background: var(--aw-border-strong); }
.row[data-tone='ok'] .dot { background: var(--aw-ok); }
.row[data-tone='warn'] .dot { background: var(--aw-warn); }
.row[data-tone='bad'] .dot { background: var(--aw-danger); }
.row .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row .value { color: var(--aw-ink); font-weight: 700; font-variant-numeric: tabular-nums; }
.row[aria-pressed='true'] .value { color: var(--aw-teal); }
</style>
