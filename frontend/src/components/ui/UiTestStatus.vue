<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

const meta: Record<string, { icon: string; tone: string; label: string }> = {
  draft: { icon: 'pi-file-edit', tone: 'secondary', label: 'Draft' },
  ready: { icon: 'pi-clock', tone: 'secondary', label: 'Ready' },
  in_progress: { icon: 'pi-spin pi-spinner', tone: 'info', label: 'In progress' },
  review_required: { icon: 'pi-eye', tone: 'warn', label: 'Review required' },
  blocked: { icon: 'pi-ban', tone: 'danger', label: 'Blocked' },
  completed: { icon: 'pi-check-circle', tone: 'success', label: 'Completed' },
  completed_no_exception: { icon: 'pi-check-circle', tone: 'success', label: 'Completed · no exception' },
  completed_with_exception: { icon: 'pi-exclamation-triangle', tone: 'danger', label: 'Completed · exception' },
  not_applicable: { icon: 'pi-minus-circle', tone: 'secondary', label: 'Not applicable' },
  pending: { icon: 'pi-clock', tone: 'secondary', label: 'Pending' },
  agent_checked: { icon: 'pi-android', tone: 'info', label: 'Agent checked' },
  confirmed: { icon: 'pi-check-circle', tone: 'success', label: 'Confirmed' },
  exception: { icon: 'pi-exclamation-triangle', tone: 'danger', label: 'Exception' },
  manual_review: { icon: 'pi-eye', tone: 'warn', label: 'Manual review' },
  error: { icon: 'pi-times-circle', tone: 'danger', label: 'Error' },
}

const info = computed(() => meta[props.status] ?? {
  icon: 'pi-circle',
  tone: 'secondary',
  label: props.status.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()),
})
</script>

<template>
  <i
    class="status-icon pi"
    :class="[info.icon, `tone-${info.tone}`]"
    v-tooltip.left="info.label"
    :aria-label="info.label"
    role="img"
  />
</template>

<style scoped>
.status-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.6rem;
  height: 1.6rem;
  border-radius: 50%;
  font-size: 0.8rem;
  flex-shrink: 0;
}
.status-icon.tone-secondary { color: var(--aw-muted); background: var(--aw-raised); }
.status-icon.tone-success { color: var(--aw-ok); background: var(--aw-ok-soft); }
.status-icon.tone-warn { color: var(--aw-warn); background: var(--aw-warn-soft); }
.status-icon.tone-danger { color: var(--aw-danger); background: var(--aw-danger-soft); }
.status-icon.tone-info { color: var(--aw-teal); background: var(--aw-teal-soft); }
</style>
