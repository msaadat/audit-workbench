<script lang="ts">
/**
 * The five states a piece of the audit file can be in, and how each is drawn.
 *
 * A state is an icon *and* a word, never a colour alone: the word is the
 * accessible name and the tooltip, and a caller with room for it prints it.
 * The record used to say all five with an 8 px dot whose fill or dash was the
 * only difference — which is how a stage holding sixty-three analyses that had
 * never run came to wear the same filled dot as a finished one.
 *
 *   done          filed and current
 *   next          the one step to take now
 *   attention     filed, but wrong or stale — a reason goes beside it
 *   waiting       blocked by an earlier stage
 *   not_started   nothing filed yet, and nothing in the way
 */
export type UiState = 'done' | 'next' | 'attention' | 'waiting' | 'not_started'

export const STATE_LABEL: Record<UiState, string> = {
  done: 'Done',
  next: 'Next',
  attention: 'Needs attention',
  waiting: 'Waiting',
  not_started: 'Not started',
}

const GLYPH: Record<UiState, string> = {
  done: 'aw-icon-circle-check',
  next: 'aw-icon-circle-dot',
  attention: 'aw-icon-triangle-alert',
  waiting: 'aw-icon-lock',
  not_started: 'aw-icon-circle',
}
</script>

<script setup lang="ts">
const props = withDefaults(defineProps<{ state: UiState; label?: string; showLabel?: boolean }>(), {
  showLabel: false,
})
</script>

<template>
  <span class="ui-state" :data-state="props.state">
    <i
      class="aw-icon"
      :class="GLYPH[props.state]"
      role="img"
      :aria-label="props.label ?? STATE_LABEL[props.state]"
      :title="props.label ?? STATE_LABEL[props.state]"
    />
    <span v-if="props.showLabel" class="ui-state__label">{{ props.label ?? STATE_LABEL[props.state] }}</span>
  </span>
</template>

<style scoped>
.ui-state { display: inline-flex; align-items: center; gap: .375rem; font-size: var(--aw-text-sm); line-height: 1; }
.ui-state__label { font-weight: 600; }
.ui-state[data-state='done'] { color: var(--aw-ok); }
.ui-state[data-state='next'] { color: var(--aw-teal); }
.ui-state[data-state='attention'] { color: var(--aw-warn); }
.ui-state[data-state='attention'] .ui-state__label { color: var(--aw-warn-ink); }
.ui-state[data-state='waiting'],
.ui-state[data-state='not_started'] { color: var(--aw-muted); }
</style>
