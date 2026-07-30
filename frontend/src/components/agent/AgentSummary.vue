<script setup lang="ts">
import Tag from 'primevue/tag'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'

import { api, ApiError } from '../../api'
import { useWorkspaceNav } from '../../composables/useWorkspaceNavigation'
import type { AgentAuditOutcome, AgentFinding, AuditFinding } from '../../types'
import MarkdownView from '../MarkdownView.vue'

// The final analyst summary: structured findings (evidence-linked) plus the
// markdown narrative, rendered with a deliberately tiny formatter — headings,
// bullets, bold, inline code — to keep the bundle dependency-free.
const props = defineProps<{
  markdown: string
  findings: AgentFinding[]
  auditOutcome?: AgentAuditOutcome
  workspaceId: string
  runId: string
}>()
const nav = useWorkspaceNav()
const toast = useToast()

const severitySeverity: Record<string, string> = {
  high: 'danger',
  medium: 'warn',
  low: 'info',
  info: 'secondary',
}

async function promote(finding: AgentFinding) {
  try {
    const saved = await api.post<AuditFinding>(`/api/workspaces/${props.workspaceId}/findings/promote`, { run_id: props.runId, finding_id: finding.id })
    toast.add({ severity: 'success', summary: 'Draft finding created', detail: 'Review and complete the IIA fields as needed.', life: 2600 })
    await nav.replace('findings', { finding: saved.id })
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Could not promote the observation', detail: error instanceof ApiError ? error.message : String(error), life: 6000 })
  }
}

</script>

<template>
  <div class="summary">
    <div v-if="auditOutcome" class="audit-outcome">
      <p class="section-title">Audit outcome</p>
      <div class="outcome-grid">
        <span>Tests<strong>{{ auditOutcome.tests_completed }}/{{ auditOutcome.tests_total }}</strong></span>
        <span>Data Tests run<strong>{{ auditOutcome.data_tests_executed }}</strong></span>
        <span>Document Tests run<strong>{{ auditOutcome.document_tests_executed }}</strong></span>
        <span>Unspecified<strong>{{ auditOutcome.tests_unspecified }}</strong></span>
        <span>Open gates<strong>{{ auditOutcome.open_gate_count }}</strong></span>
        <span>Recorded exceptions<strong>{{ auditOutcome.recorded_exception_observations }}</strong></span>
        <span>Supported findings<strong>{{ auditOutcome.supported_findings }}</strong></span>
        <span>Draft findings<strong>{{ auditOutcome.draft_findings }}</strong></span>
      </div>
      <Tag
        :value="auditOutcome.audit_complete ? 'audit complete' : auditOutcome.completion_status.replaceAll('_', ' ')"
        :severity="auditOutcome.audit_complete ? 'success' : 'warn'"
      />
    </div>
    <div v-if="findings.length" class="findings">
      <p class="section-title">Analyst observations</p>
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
          <Button label="Promote to finding" icon="pi pi-flag" size="small" text @click="promote(finding)" />
        </div>
      </div>
    </div>
    <p class="section-title">Analyst summary</p>
    <MarkdownView class="narrative" :markdown="markdown" />
  </div>
</template>

<style scoped>
.summary { font-size: 0.85rem; }
.audit-outcome { margin-bottom: .6rem; }
.outcome-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .35rem; margin-bottom: .45rem; }
.outcome-grid span { display: grid; gap: .12rem; padding: .4rem; border: 1px solid var(--p-surface-200); border-radius: 6px; color: var(--p-surface-500); font-size: .68rem; }
.outcome-grid strong { color: var(--p-text-color); font-size: .82rem; }
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
