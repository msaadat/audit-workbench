<script setup lang="ts">
/**
 * Two sentences and the controls that answer them, at the top of a detail.
 *
 * Every fieldwork detail used to state its verdict three or four times over —
 * a status chip, a headline, a rail heading, a Result block — each in a
 * different vocabulary, none of them the same fact. There are only ever two
 * facts, and they are different in kind:
 *
 *   1. What the run found. A statement about the data. It does not change when
 *      somebody disagrees with it.
 *   2. What is recorded. A statement about the file — who decided, and whether
 *      a person has read it.
 *
 * They sit on two lines because reading one for the other is the mistake the
 * old layouts invited: a green chip beside an unread agent conclusion says the
 * work is done when nobody has looked at it.
 *
 * The right column belongs to the page, because agreeing with a run means
 * something different on each of them. The stale strip is attached rather than
 * floated: it qualifies everything in the card above it, and a separate banner
 * elsewhere in the detail is one the reader meets after the conclusion it
 * invalidates.
 */

defineProps<{
  /** The run's own reading, as a dot: `ok`, `warn`, `bad`, or nothing yet. */
  tone: 'ok' | 'warn' | 'bad' | 'neutral'
  /** One sentence, shown under the card when the record is out of date. */
  stale?: string
}>()
</script>

<template>
  <section class="verdict-bar">
    <div class="body">
      <div class="lines">
        <p class="found">
          <span class="dot" :data-tone="tone" aria-hidden="true" />
          <slot name="found" />
        </p>
        <p class="recorded"><slot name="recorded" /></p>
      </div>
      <div class="actions"><slot name="actions" /></div>
    </div>

    <p v-if="stale" class="stale">
      <i class="pi pi-exclamation-triangle" aria-hidden="true" />{{ stale }}
    </p>
  </section>
</template>

<style scoped>
/* Raised rather than white-on-white: the detail panel is already a card, and
   this has to read as a band inside it rather than as another panel. */
.verdict-bar {
  display: flex; flex-direction: column; min-width: 0;
  overflow: hidden;
  border: 1px solid var(--aw-border); border-radius: var(--aw-radius-surface);
  background: var(--aw-raised);
}
.body {
  display: grid; grid-template-columns: minmax(0, 1fr) auto;
  align-items: center; gap: 1rem;
  padding: .75rem 1rem;
}
.lines { display: flex; flex-direction: column; gap: .25rem; min-width: 0; }

.found {
  display: flex; align-items: center; flex-wrap: wrap; gap: .5rem;
  margin: 0; color: var(--aw-ink-strong);
  font-size: var(--aw-text-base); font-weight: 600; line-height: 1.35;
}
.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='ok'] { background: var(--aw-ok); }
.dot[data-tone='warn'] { background: var(--aw-warn); }
.dot[data-tone='bad'] { background: var(--aw-danger); }

.recorded { margin: 0; min-width: 0; color: var(--aw-ink-soft); font-size: var(--aw-text-sm); line-height: 1.45; }
.recorded:empty { display: none; }

.actions { display: flex; align-items: center; gap: .5rem; flex: none; }

.stale {
  display: flex; align-items: center; gap: .5rem; margin: 0;
  padding: .5rem 1rem;
  border-top: 1px solid var(--aw-warn-line);
  background: var(--aw-warn-soft); color: var(--aw-warn-ink);
  font-size: var(--aw-text-sm); line-height: 1.4;
}
.stale .pi { flex: none; font-size: var(--aw-text-sm); }

@container master-detail-content (max-width: 34rem) {
  .body { grid-template-columns: minmax(0, 1fr); }
  .actions { justify-content: flex-start; }
}
</style>
