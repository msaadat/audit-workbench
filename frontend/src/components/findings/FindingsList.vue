<script setup lang="ts">
import { computed, ref } from 'vue'

import type { AuditFinding, FindingSeverity } from '../../types'
import { SEVERITY_ORDER, openItems } from './findingsStatus'

/**
 * The register in severity order, one line of title and one of fact per row.
 *
 * Severity was a `Tag` on every row — a coloured word repeated eighteen times
 * down a 300px column, which said nothing about any one finding that its
 * neighbours did not also say. As a group heading it is stated once and
 * orders the list, which is the only thing severity is for on arrival.
 *
 * What the row spends its second line on instead is what the finding still
 * owes. `F-0571DE · no risk · cause pending` distinguishes it from the
 * seventeen findings beside it; `Agent` did not.
 */

const props = defineProps<{
  findings: AuditFinding[]
  selectedId: string | null
}>()
defineEmits<{ select: [finding: AuditFinding] }>()

/** Collapsed groups, by severity. Everything starts open. */
const collapsed = ref<Set<string>>(new Set())
function toggle(severity: string) {
  const next = new Set(collapsed.value)
  if (!next.delete(severity)) next.add(severity)
  collapsed.value = next
}

const TONES: Record<FindingSeverity, string> = {
  critical: 'critical', high: 'high', medium: 'medium', low: 'low', info: 'info',
}

const groups = computed(() => SEVERITY_ORDER
  .map(severity => ({
    severity,
    items: props.findings.filter(item => item.severity === severity),
  }))
  .filter(group => group.items.length))
</script>

<template>
  <div class="list">
    <section v-for="group in groups" :key="group.severity">
      <button
        type="button"
        class="group"
        :aria-expanded="!collapsed.has(group.severity)"
        @click="toggle(group.severity)"
      >
        <i class="pi" :class="collapsed.has(group.severity) ? 'pi-chevron-right' : 'pi-chevron-down'" aria-hidden="true" />
        <span class="severity" :data-tone="TONES[group.severity]">{{ group.severity }}</span>
        <span class="count aw-figure">{{ group.items.length }}</span>
      </button>
      <template v-if="!collapsed.has(group.severity)">
        <button
          v-for="item in group.items"
          :key="item.id"
          type="button"
          class="row"
          :class="{ active: item.id === selectedId }"
          @click="$emit('select', item)"
        >
          <span class="dot" :data-tone="TONES[item.severity]" aria-hidden="true" />
          <span class="copy">
            <span class="title">{{ item.title }}</span>
            <!-- The id, and the first thing the finding owes. Listing all of
                 them put the same four words on all eighteen rows; the verdict
                 bar states them in full for the one finding being read, and
                 the chips above count each of them across the register. -->
            <span class="meta aw-figure">
              <span class="id">{{ item.id }}</span>
              <template v-if="!item.auditor_confirmed"> · <span class="draft">draft</span></template>
              <template v-else-if="openItems(item)[0]">
                · <span :data-tone="openItems(item)[0].tone">{{ openItems(item)[0].short }}</span>
              </template>
            </span>
          </span>
        </button>
      </template>
    </section>
    <p v-if="!findings.length" class="empty">No finding matches this view.</p>
  </div>
</template>

<style scoped>
.list { display: flex; flex-direction: column; min-width: 0; }

.group {
  display: flex; align-items: center; gap: .4rem;
  width: 100%; min-width: 0;
  padding: .5rem .75rem .375rem;
  border: 0; border-top: 1px solid var(--aw-border);
  background: none; font: inherit; text-align: left; cursor: pointer;
}
.group:first-child, section:first-child .group { border-top: 0; }
.group:hover { background: var(--aw-raised); }
.group:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.group .pi { color: var(--aw-muted); font-size: .625rem; }
.severity {
  color: var(--aw-ink-strong); font-size: var(--aw-text-xs); font-weight: 700;
  letter-spacing: .06em; text-transform: uppercase;
}
.severity[data-tone='critical'] { color: var(--aw-danger-ink); }
.severity[data-tone='high'] { color: var(--aw-danger); }
.severity[data-tone='medium'] { color: var(--aw-warn-ink); }
.severity[data-tone='low'] { color: var(--aw-low-ink); }
.count { color: var(--aw-muted); font-size: var(--aw-text-xs); }

.row {
  display: flex; align-items: center; gap: .625rem;
  width: 100%; min-width: 0;
  padding: .5rem .75rem;
  border: 0; border-left: 3px solid transparent;
  background: none; color: inherit; font: inherit; text-align: left; cursor: pointer;
}
.row:hover:not(.active) { background: var(--aw-raised); }
.row:focus-visible { outline: 2px solid var(--aw-teal); outline-offset: -2px; }
.row.active { border-left-color: var(--aw-teal); background: var(--aw-teal-soft); }

.dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: var(--aw-border-strong); }
.dot[data-tone='critical'] { background: var(--aw-danger-ink); }
.dot[data-tone='high'] { background: var(--aw-danger); }
.dot[data-tone='medium'] { background: var(--aw-warn); }
.dot[data-tone='low'] { background: var(--aw-low); }

.copy { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.title { overflow: hidden; color: var(--aw-ink); font-size: var(--aw-text-base); font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.row.active .title { color: var(--aw-ink-strong); font-weight: 600; }
.meta { overflow: hidden; color: var(--aw-muted); font-size: var(--aw-text-xs); text-overflow: ellipsis; white-space: nowrap; }
.meta .id { font-family: var(--aw-font-mono); }
.meta [data-tone='bad'] { color: var(--aw-danger); }
.meta [data-tone='warn'] { color: var(--aw-warn-ink); }
.meta .draft { color: var(--aw-muted); }

.empty { padding: 1rem .75rem; color: var(--aw-muted); font-size: var(--aw-text-sm); text-align: center; }
</style>
