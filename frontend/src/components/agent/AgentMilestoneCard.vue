<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { useWorkspaceNav, type WorkspaceDestination } from '../../composables/useWorkspaceNavigation'
import type { AgentMilestone } from '../../types'

const props = defineProps<{ milestone: AgentMilestone }>()
const nav = useWorkspaceNav()

function display(value: string | number | boolean | null) {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return value == null ? '—' : String(value)
}

interface ArtifactLink {
  label: string
  icon: string
  destination: WorkspaceDestination
}

/** Turn the durable artifact references on a milestone into workspace links. */
function artifactLink(ref: string): ArtifactLink | null {
  const [kind, id] = ref.split(':')
  if (!kind) return null

  if (kind === 'planning' && (id === 'apm' || id === 'context')) {
    return { label: 'View APM', icon: 'pi pi-map', destination: 'apm' }
  }
  if (kind === 'rcm' || kind === 'observation') {
    return { label: 'View RCM', icon: 'pi pi-table', destination: 'rcm' }
  }
  if (kind === 'datatest') {
    return { label: 'View data tests', icon: 'pi pi-chart-bar', destination: 'data-tests' }
  }
  if (kind === 'doctest' || kind === 'document_test' || kind === 'doctest_item') {
    return { label: 'View document tests', icon: 'pi pi-file-check', destination: 'doc-tests' }
  }
  if (kind === 'finding') {
    return { label: 'View findings', icon: 'pi pi-flag', destination: 'findings' }
  }
  if (kind === 'analysis') {
    return { label: 'View analyses', icon: 'pi pi-chart-line', destination: 'analysis' }
  }
  if (kind === 'document') {
    return { label: 'View documents', icon: 'pi pi-file', destination: 'documents' }
  }
  if (kind === 'report') {
    return { label: 'View report', icon: 'pi pi-file-edit', destination: 'report' }
  }
  if (kind === 'dashboard' || kind === 'tile' || (kind === 'audit' && id === 'verification')) {
    return { label: 'View dashboard', icon: 'pi pi-th-large', destination: 'dashboard' }
  }
  return null
}

const links = computed(() => {
  const seen = new Set<WorkspaceDestination>()
  return props.milestone.artifact_refs
    .map(artifactLink)
    .filter((item): item is ArtifactLink => item !== null)
    .filter(item => !seen.has(item.destination) && seen.add(item.destination))
})
</script>

<template>
  <article class="milestone" :data-status="props.milestone.status">
    <span class="icon">
      <i :class="props.milestone.status === 'completed' ? 'pi pi-check' : 'pi pi-exclamation-circle'" />
    </span>
    <div class="body">
      <strong>{{ props.milestone.headline }}</strong>
      <p>{{ props.milestone.summary }}</p>
      <dl v-if="props.milestone.metrics.length" class="metrics">
        <div v-for="item in props.milestone.metrics" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd>{{ display(item.value) }}</dd>
        </div>
      </dl>
      <ul v-if="props.milestone.highlights.length" class="highlights">
        <li v-for="item in props.milestone.highlights" :key="`${item.label}:${item.detail}`" :data-severity="item.severity">
          <i :class="item.severity === 'error' ? 'pi pi-times-circle' : 'pi pi-exclamation-triangle'" />
          <span><b>{{ item.label }}</b><small>{{ item.detail }}</small></span>
        </li>
      </ul>
      <nav v-if="links.length" class="artifact-links" aria-label="Related work products">
        <RouterLink
          v-for="link in links"
          :key="link.destination"
          :to="nav.to(link.destination)"
          class="artifact-link"
          :aria-label="link.label"
        >
          <i :class="link.icon" aria-hidden="true" />
          <span>{{ link.label }}</span>
          <i class="pi pi-arrow-up-right" aria-hidden="true" />
        </RouterLink>
      </nav>
    </div>
  </article>
</template>

<style scoped>
/* A milestone is the briefing, not a status line: what the stage established
   and the two or three things worth saying out loud. It was drawn at the
   opposite hierarchy — headline below body size, summary smaller again,
   highlights smallest of all — so the most valuable text on the screen was
   the hardest to read. The scale below runs the other way. */
.milestone{display:grid;grid-template-columns:2rem minmax(0,1fr);gap:.7rem;align-self:flex-start;width:min(92%,42rem);padding:.9rem 1rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-surface);background:var(--aw-panel);box-shadow:var(--aw-shadow-sm)}
.icon{display:grid;place-items:center;width:2rem;height:2rem;border-radius:var(--aw-radius-control);background:var(--aw-teal-soft);color:var(--aw-teal);font-size:var(--aw-text-sm)}
.milestone[data-status='completed_with_issues'] .icon,.milestone[data-status='needs_review'] .icon{background:var(--aw-warn-soft);color:var(--aw-warn)}
.body{display:grid;gap:.4rem;min-width:0}
.body>strong{font-size:var(--aw-text-lg);font-weight:600;line-height:1.25;letter-spacing:-.01em;color:var(--aw-ink-strong);text-wrap:balance}
.body>p{margin:0;color:var(--aw-ink-soft);font-size:var(--aw-text-md);line-height:1.5;max-width:62ch}
.metrics{display:flex;flex-wrap:wrap;gap:.3rem 1.1rem;margin:.15rem 0 0}.metrics div{display:flex;align-items:baseline;gap:.35rem;font-size:var(--aw-text-xs)}.metrics dt{color:var(--aw-muted)}.metrics dd{margin:0;font-weight:700;font-variant-numeric:tabular-nums;color:var(--aw-ink-strong)}
/* Severity carries the weight; the text stays at reading size. */
.highlights{display:grid;gap:.45rem;margin:.35rem 0 0;padding:.6rem 0 0;border-top:1px solid var(--aw-border);list-style:none}
.highlights li{display:grid;grid-template-columns:1rem minmax(0,1fr);gap:.45rem;color:var(--aw-warn);font-size:var(--aw-text-base)}
.highlights li[data-severity='error']{color:var(--aw-danger)}
.highlights i{padding-top:.2rem;font-size:var(--aw-text-xs)}
.highlights span{display:grid;gap:.1rem;min-width:0}
.highlights b{font-weight:600;line-height:1.35}
.highlights small{color:var(--aw-ink-soft);font-size:var(--aw-text-sm);line-height:1.45;max-width:60ch}
.artifact-links{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.3rem}.artifact-link{display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .45rem;border:1px solid var(--aw-border);border-radius:var(--aw-radius-control);background:var(--aw-panel);color:var(--aw-teal);font-size:var(--aw-text-xs);font-weight:600;text-decoration:none}.artifact-link:hover{border-color:var(--aw-teal);background:var(--aw-teal-soft)}.artifact-link:focus-visible{outline:2px solid var(--aw-teal);outline-offset:1px}.artifact-link>i:last-child{font-size:var(--aw-text-2xs);color:var(--aw-muted)}
</style>
