<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ status: string; showLabel?: boolean }>(), {
  showLabel: false,
})

const meta: Record<string, { icon: string; tone: string; label: string }> = {
  draft: { icon: 'pi-file-edit', tone: 'secondary', label: 'Draft' },
  ready: { icon: 'pi-clock', tone: 'secondary', label: 'Ready' },
  in_progress: { icon: 'pi-spin pi-spinner', tone: 'info', label: 'In progress' },
  review_required: { icon: 'pi-eye', tone: 'warn', label: 'Review required' },
  blocked: { icon: 'pi-ban', tone: 'danger', label: 'Blocked' },
  completed: { icon: 'pi-check-circle', tone: 'success', label: 'Completed' },
  completed_no_exception: { icon: 'pi-check-circle', tone: 'success', label: 'No exception' },
  completed_with_exception: { icon: 'pi-exclamation-triangle', tone: 'danger', label: 'Exception' },
  not_applicable: { icon: 'pi-minus-circle', tone: 'secondary', label: 'Not applicable' },
  pending: { icon: 'pi-clock', tone: 'secondary', label: 'Not run' },
  agent_checked: { icon: 'pi-android', tone: 'info', label: 'Agent checked' },
  confirmed: { icon: 'pi-check-circle', tone: 'success', label: 'Confirmed' },
  exception: { icon: 'pi-exclamation-triangle', tone: 'danger', label: 'Exception' },
  manual_review: { icon: 'pi-eye', tone: 'warn', label: 'Manual review' },
  error: { icon: 'pi-times-circle', tone: 'danger', label: 'Error' },
  // Auditor dispositions share the vocabulary so one component covers both.
  awaiting_signoff: { icon: 'pi-user-edit', tone: 'warn', label: 'Awaiting sign-off' },
  accepted: { icon: 'pi-check-circle', tone: 'success', label: 'Accepted' },
  needs_manual_check: { icon: 'pi-eye', tone: 'warn', label: 'Needs manual check' },
  follow_up: { icon: 'pi-flag', tone: 'warn', label: 'Follow up' },
  invalid_test_or_result: { icon: 'pi-ban', tone: 'secondary', label: 'Test not valid' },
  awaiting_evidence: { icon: 'pi-inbox', tone: 'warn', label: 'Awaiting evidence' },
  not_run: { icon: 'pi-clock', tone: 'secondary', label: 'Not run' },
}

const info = computed(() => meta[props.status] ?? {
  icon: 'pi-circle',
  tone: 'secondary',
  label: props.status.replaceAll('_', ' ').replace(/^./, char => char.toUpperCase()),
})
</script>

<template>
  <span
    v-if="showLabel"
    class="status-chip"
    :class="`tone-${info.tone}`"
    role="img"
    :aria-label="info.label"
  >
    <i class="pi" :class="info.icon" />{{ info.label }}
  </span>
  <i
    v-else
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

/* The labelled form exists because an icon alone cannot be scanned down a
   list of 30 worklist items, and a tooltip is not a substitute for a word. */
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  flex-shrink: 0;
  padding: 0.12rem 0.45rem 0.12rem 0.35rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
}
.status-chip .pi { font-size: 0.72rem; }

.status-icon.tone-secondary, .status-chip.tone-secondary { color: var(--aw-muted); background: var(--aw-raised); }
.status-icon.tone-success, .status-chip.tone-success { color: var(--aw-ok); background: var(--aw-ok-soft); }
.status-icon.tone-warn, .status-chip.tone-warn { color: var(--aw-warn); background: var(--aw-warn-soft); }
.status-icon.tone-danger, .status-chip.tone-danger { color: var(--aw-danger); background: var(--aw-danger-soft); }
.status-icon.tone-info, .status-chip.tone-info { color: var(--aw-teal); background: var(--aw-teal-soft); }
</style>
