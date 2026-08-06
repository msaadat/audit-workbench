<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ verdict: string }>()

const meta: Record<string, { icon: string; tone: string; label: string }> = {
  ok: { icon: 'pi-check-circle', tone: 'success', label: 'OK' },
  warn: { icon: 'pi-exclamation-triangle', tone: 'warn', label: 'Warning' },
  fail: { icon: 'pi-times-circle', tone: 'danger', label: 'Failed' },
  info: { icon: 'pi-info-circle', tone: 'info', label: 'Info' },
  error: { icon: 'pi-times-circle', tone: 'danger', label: 'Error' },
  skipped: { icon: 'pi-minus-circle', tone: 'secondary', label: 'Skipped' },
}

const info = computed(() => meta[props.verdict] ?? {
  icon: 'pi-circle',
  tone: 'secondary',
  label: props.verdict.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()),
})
</script>

<template>
  <i
    class="verdict-icon pi"
    :class="[info.icon, `tone-${info.tone}`]"
    v-tooltip.left="info.label"
    :aria-label="info.label"
    role="img"
  />
</template>

<style scoped>
.verdict-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 50%;
  font-size: var(--aw-text-sm);
  flex-shrink: 0;
}
.verdict-icon.tone-secondary { color: var(--aw-muted); background: var(--aw-raised); }
.verdict-icon.tone-success { color: var(--aw-ok); background: var(--aw-ok-soft); }
.verdict-icon.tone-warn { color: var(--aw-warn); background: var(--aw-warn-soft); }
.verdict-icon.tone-danger { color: var(--aw-danger); background: var(--aw-danger-soft); }
.verdict-icon.tone-info { color: var(--aw-teal); background: var(--aw-teal-soft); }
</style>
