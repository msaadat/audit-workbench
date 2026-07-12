<script setup lang="ts">
import { computed } from 'vue'
import Tag from 'primevue/tag'

import type { AgentFinding } from '../../types'

// The final analyst summary: structured findings (evidence-linked) plus the
// markdown narrative, rendered with a deliberately tiny formatter — headings,
// bullets, bold, inline code — to keep the bundle dependency-free.
const props = defineProps<{ markdown: string; findings: AgentFinding[] }>()

const severitySeverity: Record<string, string> = {
  high: 'danger',
  medium: 'warn',
  low: 'info',
  info: 'secondary',
}

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
}

function inline(text: string): string {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

const html = computed(() => {
  const out: string[] = []
  let inList = false
  for (const raw of (props.markdown || '').split('\n')) {
    const line = raw.trimEnd()
    const heading = /^(#{1,4})\s+(.*)$/.exec(line)
    const bullet = /^[-*]\s+(.*)$/.exec(line)
    if (bullet) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${inline(bullet[1])}</li>`)
      continue
    }
    if (inList) {
      out.push('</ul>')
      inList = false
    }
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6)
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`)
    } else if (line.trim()) {
      out.push(`<p>${inline(line)}</p>`)
    }
  }
  if (inList) out.push('</ul>')
  return out.join('\n')
})
</script>

<template>
  <div class="summary">
    <div v-if="findings.length" class="findings">
      <p class="section-title">Findings</p>
      <div v-for="finding in findings" :key="finding.id" class="finding">
        <Tag :value="finding.severity" :severity="severitySeverity[finding.severity]" />
        <div class="finding-body">
          <span>{{ finding.statement }}</span>
          <small>
            {{ finding.basis === 'observed' ? 'Observed' : 'Interpretation' }}
            <template v-if="finding.evidence_refs.length">
              · evidence: {{ finding.evidence_refs.join(', ') }}
            </template>
          </small>
        </div>
      </div>
    </div>
    <p class="section-title">Analyst summary</p>
    <div class="narrative" v-html="html" />
  </div>
</template>

<style scoped>
.summary { font-size: 0.85rem; }
.section-title {
  margin: 0.6rem 0 0.35rem;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--p-surface-500);
}
.finding {
  display: flex;
  gap: 0.55rem;
  align-items: flex-start;
  padding: 0.45rem 0.5rem;
  border: 1px solid var(--p-surface-200);
  border-radius: 7px;
  background: var(--p-surface-0);
  margin-bottom: 0.4rem;
}
.finding-body { display: flex; flex-direction: column; gap: 0.15rem; line-height: 1.35; }
.finding-body small { color: var(--p-surface-500); font-size: 0.7rem; }
.narrative { line-height: 1.5; }
.narrative :deep(h3), .narrative :deep(h4), .narrative :deep(h5), .narrative :deep(h6) {
  margin: 0.8rem 0 0.3rem;
  font-size: 0.9rem;
}
.narrative :deep(p) { margin: 0.3rem 0; }
.narrative :deep(ul) { margin: 0.3rem 0; padding-left: 1.2rem; }
.narrative :deep(code) {
  background: var(--p-surface-100);
  border-radius: 4px;
  padding: 0 0.25rem;
  font-size: 0.78rem;
}
</style>
