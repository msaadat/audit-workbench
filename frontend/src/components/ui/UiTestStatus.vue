<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ status: string; showLabel?: boolean }>(), {
  showLabel: false,
})

const meta: Record<string, { icon: string; tone: string; label: string }> = {
  draft: { icon: 'aw-icon-file-pen', tone: 'secondary', label: 'Draft' },
  ready: { icon: 'aw-icon-clock', tone: 'secondary', label: 'Ready' },
  in_progress: { icon: 'aw-icon-spin aw-icon-loader-circle', tone: 'info', label: 'In progress' },
  review_required: { icon: 'aw-icon-eye', tone: 'warn', label: 'Review required' },
  blocked: { icon: 'aw-icon-ban', tone: 'danger', label: 'Blocked' },
  completed: { icon: 'aw-icon-circle-check', tone: 'success', label: 'Completed' },
  completed_no_exception: { icon: 'aw-icon-circle-check', tone: 'success', label: 'No exception' },
  completed_with_exception: { icon: 'aw-icon-triangle-alert', tone: 'danger', label: 'Exception' },
  not_applicable: { icon: 'aw-icon-circle-minus', tone: 'secondary', label: 'Not applicable' },
  pending: { icon: 'aw-icon-clock', tone: 'secondary', label: 'Not run' },
  agent_checked: { icon: 'aw-icon-bot', tone: 'info', label: 'Agent checked' },
  confirmed: { icon: 'aw-icon-circle-check', tone: 'success', label: 'Confirmed' },
  exception: { icon: 'aw-icon-triangle-alert', tone: 'danger', label: 'Exception' },
  manual_review: { icon: 'aw-icon-eye', tone: 'warn', label: 'Manual review' },
  error: { icon: 'aw-icon-circle-x', tone: 'danger', label: 'Error' },
  needs_manual_check: { icon: 'aw-icon-eye', tone: 'warn', label: 'Needs manual check' },
  // The worker's own accept, which a population grid shows per record until an
  // auditor's disposition overrides it.
  accepted: { icon: 'aw-icon-circle-check', tone: 'success', label: 'Accepted' },
  awaiting_evidence: { icon: 'aw-icon-inbox', tone: 'warn', label: 'Awaiting evidence' },
  not_run: { icon: 'aw-icon-clock', tone: 'secondary', label: 'Not run' },
  passed: { icon: 'aw-icon-circle-check', tone: 'success', label: 'Passed' },
  failed: { icon: 'aw-icon-circle-x', tone: 'danger', label: 'Failed' },
  incomplete: { icon: 'aw-icon-inbox', tone: 'warn', label: 'Incomplete' },
  needs_review: { icon: 'aw-icon-eye', tone: 'warn', label: 'Needs review' },
  stale: { icon: 'aw-icon-history', tone: 'warn', label: 'Stale' },
  // The runner could run the check but not settle it — distinct from an
  // auditor's 'needs_review', which is a decision to defer.
  inconclusive: { icon: 'aw-icon-circle-help', tone: 'warn', label: 'Inconclusive' },
  match: { icon: 'aw-icon-check', tone: 'success', label: 'Match' },
  mismatch: { icon: 'aw-icon-x', tone: 'danger', label: 'Mismatch' },
  missing_evidence: { icon: 'aw-icon-inbox', tone: 'warn', label: 'Missing evidence' },
  invalid_extraction: { icon: 'aw-icon-circle-alert', tone: 'warn', label: 'Invalid extraction' },
  ambiguous: { icon: 'aw-icon-circle-help', tone: 'warn', label: 'Ambiguous' },
}

const info = computed(() => meta[props.status] ?? {
  icon: 'aw-icon-circle',
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
    <i class="aw-icon" :class="info.icon" />{{ info.label }}
  </span>
  <i
    v-else
    class="status-icon aw-icon"
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
  font-size: var(--aw-text-sm);
  flex-shrink: 0;
}

/* The labelled form is for places where the status is the content. Compact
   rails use the icon form beside a title, backed by a tooltip and aria-label. */
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  flex-shrink: 0;
  padding: 0.12rem 0.45rem 0.12rem 0.35rem;
  border-radius: var(--aw-radius-pill);
  font-size: var(--aw-text-xs);
  font-weight: 600;
  white-space: nowrap;
}
.status-chip .aw-icon { font-size: var(--aw-text-xs); }

.status-icon.tone-secondary, .status-chip.tone-secondary { color: var(--aw-muted); background: var(--aw-raised); }
.status-icon.tone-success, .status-chip.tone-success { color: var(--aw-ok); background: var(--aw-ok-soft); }
.status-icon.tone-warn, .status-chip.tone-warn { color: var(--aw-warn); background: var(--aw-warn-soft); }
.status-icon.tone-danger, .status-chip.tone-danger { color: var(--aw-danger); background: var(--aw-danger-soft); }
.status-icon.tone-info, .status-chip.tone-info { color: var(--aw-teal); background: var(--aw-teal-soft); }
</style>
