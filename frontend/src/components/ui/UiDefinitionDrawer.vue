<script setup lang="ts">
import Button from 'primevue/button'
import Drawer from 'primevue/drawer'

/**
 * One drawer for authoring a test, whether it exists yet or not.
 *
 * `New test` was a 56rem modal and `Edit definition` a right drawer, for the
 * same fields — two surfaces, two layouts and two footers for one act. The
 * only real difference between them is what the footer promises, so that is
 * all this asks for.
 *
 * The consequence sentence belongs beside the buttons rather than in a banner
 * at the top: it is a fact about what Save does, and it is read when Save is
 * about to be pressed. There is no blocker sentence — a missing required value
 * outlines its own control, where the value goes.
 */

const visible = defineModel<boolean>({ required: true })
const props = withDefaults(defineProps<{
  /** `DEFINITION · DAT-…`, or `NEW TEST`. */
  eyebrow: string
  title: string
  /** Whether the record exists: it decides what the primary promises. */
  editing?: boolean
  /** True once the definition could actually run. */
  ready?: boolean
  saving?: boolean
  running?: boolean
  /** Said only where it is true — an edit invalidates what was concluded. */
  consequence?: string
}>(), { editing: false, ready: false, saving: false, running: false })
const emit = defineEmits<{ save: [run: boolean] }>()
</script>

<template>
  <Drawer
    v-model:visible="visible"
    position="right"
    class="aw-drawer--bare"
    :style="{ width: 'min(37.5rem, 96vw)' }"
  >
    <div class="shell">
      <header class="head">
        <div class="copy">
          <p class="aw-label">{{ eyebrow }}</p>
          <strong>{{ title }}</strong>
        </div>
        <button type="button" class="close" aria-label="Close" @click="visible = false">
          <i class="pi pi-times" />
        </button>
      </header>

      <div class="body"><slot /></div>

      <footer class="foot">
        <p v-if="props.editing && consequence" class="consequence">{{ consequence }}</p>
        <span class="grow" />
        <Button label="Cancel" size="small" outlined severity="secondary" @click="visible = false" />
        <Button
          label="Save only"
          size="small"
          outlined
          severity="secondary"
          :loading="saving"
          :disabled="!ready"
          @click="emit('save', false)"
        />
        <Button
          :label="props.editing ? 'Save and run' : 'Create and run'"
          icon="pi pi-play"
          size="small"
          :loading="saving || running"
          :disabled="!ready"
          @click="emit('save', true)"
        />
      </footer>
    </div>
  </Drawer>
</template>

<style scoped>
.shell { display: flex; flex-direction: column; min-width: 0; height: 100%; }
.head { display: flex; align-items: center; gap: .75rem; padding: 1rem 1.375rem; border-bottom: 1px solid var(--aw-border); }
.copy { display: flex; flex-direction: column; gap: .125rem; min-width: 0; }
.copy strong { color: var(--aw-ink-strong); font-size: var(--aw-text-md); font-weight: 600; }
.close { display: grid; place-items: center; width: 1.75rem; height: 1.75rem; margin-left: auto; padding: 0; border: 0; border-radius: var(--aw-radius-control); background: none; color: var(--aw-muted); cursor: pointer; }
.close:hover { background: var(--aw-raised); color: var(--aw-ink); }

.body { display: flex; flex-direction: column; gap: 1.25rem; flex: 1; min-height: 0; overflow-y: auto; padding: 1.125rem 1.375rem; }

.foot { display: flex; align-items: center; gap: .625rem; flex-wrap: wrap; padding: .875rem 1.375rem; border-top: 1px solid var(--aw-border); background: var(--aw-canvas); }
.grow { flex: 1; }
/* Its own row: at 600px the sentence and three buttons do not fit on one, and
   a wrap that leaves the primary alone on the second line reads as an
   afterthought rather than as the thing being pressed. */
.consequence { flex: 1 0 100%; margin: 0; color: var(--aw-warn-ink); font-size: var(--aw-text-xs); line-height: 1.4; }
</style>
